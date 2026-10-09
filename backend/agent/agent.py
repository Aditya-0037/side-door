"""The Side Door guide: one Google ADK agent over the SAME tool functions as the fast path.

Used for the conversational repair flow. Every tool call is traced (name, short args, ms, ok).
If Gemini fails or takes longer than 12 s, the deterministic swap result is returned anyway and
narration is marked unavailable. Narration is also dropped if it breaks the rules below.
"""

from __future__ import annotations

import asyncio
import functools
import os
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from backend.agent.tools import DEFAULT_GEMINI_MODEL, SideDoorTools, SwapResult
from backend.pipeline import StepTrace, payload_from_ranking

AGENT_TIMEOUT_S = 12.0
MAX_WORDS = 60
INSTRUCTION = """You are the Side Door guide. You help a newcomer find one athlete to follow, starting
from their taste in films, music and games.
Rules:
- Narrate ONLY facts returned by your tools. Never mention an athlete your tools did not return.
- Never say a single film, artist or game "caused" or "made" a pick; picks come from people whose
  overall taste overlaps the user's.
- Never show scores or decimals. Ranks like "#2 of 17" are fine.
- Keep every reply under 60 words, warm and plain.
For a repair request, call swap_pick exactly once with the given arguments, then summarise the new
pick, why (its why_line) and when to watch (its next event)."""

Runner = Callable[[str, list[Callable[..., Any]], str], Awaitable[str]]


def summarize_args(kwargs: dict[str, Any]) -> str:
    """Short, secret-free argument summary for the trace (IDs shortened, lists counted)."""
    parts = []
    for k, v in kwargs.items():
        if isinstance(v, (list, tuple)):
            parts.append(f"{k}=[{len(v)}]")
        elif isinstance(v, str) and re.fullmatch(r"[0-9A-F-]{36}", v):
            parts.append(f"{k}={v[:8]}")
        else:
            parts.append(f"{k}={str(v)[:24]}")
    return ", ".join(parts)


class Tracer:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.results: dict[str, Any] = {}

    def add(self, tool: str, args: str, started: float, ok: bool, error: str | None = None) -> None:
        self.entries.append({"tool": tool, "args": args, "ms": round((time.perf_counter() - started) * 1000),
                             "ok": ok, "error": error})

    def wrap(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def traced(**kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                result = fn(**kwargs)
            except Exception as exc:  # noqa: BLE001 -- report to the model, keep the run alive
                self.add(fn.__name__, summarize_args(kwargs), started, False, type(exc).__name__)
                return {"error": type(exc).__name__}
            self.add(fn.__name__, summarize_args(kwargs), started, True)
            return result
        return traced


def _swap_summary(swap: SwapResult) -> dict[str, Any]:
    """What the model may narrate: names, ranks, why-lines, event labels. No scores."""
    ranked = swap.ranking.ranked
    pick = next((a for a in ranked if a.entity_id == swap.featured_id), None)
    others = [a for a in ranked if a.entity_id != swap.featured_id][:2]
    def brief(a):
        return {"athlete_id": a.entity_id, "name": a.name, "team": a.team, "personal_rank": a.personal_rank,
                "crowd_rank": a.crowd_rank, "of": swap.ranking.result.n}
    return {"sport": swap.sport, "what_changed": swap.what_changed,
            "featured": brief(pick) if pick else None, "alternates": [brief(a) for a in others]}


def agent_tools(tools: SideDoorTools, tracer: Tracer, seed_ids: list[str]) -> list[Callable[..., Any]]:
    def swap_pick(sport: str, rejected_ids: list[str], reason: str, window: str,
                  allow_sport_switch: bool = False) -> dict:
        """Replace the user's current athlete. reason is "not_feeling_it" (re-rank without the rejected
        athletes) or "bad_time" (best-ranked athlete whose next event fits the watch window).
        window is "evenings", "late_night_ok" or "weekends_only". Returns the new pick and alternates."""
        swap = tools.swap_pick(seed_ids, sport, rejected_ids, reason, window, allow_sport_switch)
        tracer.results["swap_pick"] = swap
        return _swap_summary(swap)

    def explain_pick(athlete_id: str, sport: str) -> dict:
        """Factual one-line reason this athlete ranks where they do for the user's taste."""
        swap = tracer.results.get("swap_pick")
        ranking = swap.ranking if swap and swap.sport == sport else tools.rank_candidates(seed_ids, sport)
        a = next((x for x in ranking.ranked if x.entity_id == athlete_id), None)
        return tools.explain_pick(a, ranking.result.n, ranking.seeds) if a else {"error": "not ranked"}

    def next_watchable_event(athlete_id: str, sport: str, window: str) -> dict:
        """The athlete's next race/session or match in the watch window, with its IST time label."""
        event = tools.next_watchable_event(athlete_id, sport, window)
        return {k: event[k] for k in ("title", "kind", "label_ist", "fits_window", "suggestion")} if event else {}

    def official_clips(athlete_id: str, sport: str) -> dict:
        """Official video channel or website links for the athlete (never individual videos)."""
        return {"clips": [{"kind": c["kind"], "label": c["label"]} for c in tools.official_clips(athlete_id, sport)]}

    return [tracer.wrap(f) for f in (swap_pick, explain_pick, next_watchable_event, official_clips)]


async def adk_runner(prompt: str, tool_funcs: list[Callable[..., Any]], model: str) -> str:
    """Run one ADK agent turn and return its final text."""
    from google.adk.agents import LlmAgent
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    agent = LlmAgent(name="side_door_guide", model=model, instruction=INSTRUCTION, tools=tool_funcs)
    runner = InMemoryRunner(agent=agent, app_name="side_door")
    session = await runner.session_service.create_session(app_name="side_door", user_id="api")
    text = ""
    async for event in runner.run_async(user_id="api", session_id=session.id,
                                        new_message=types.Content(role="user", parts=[types.Part(text=prompt)])):
        if event.is_final_response() and event.content and event.content.parts:
            text = "".join(p.text or "" for p in event.content.parts)
    return text


DECIMAL = re.compile(r"\d\.\d{2,}")


def guard_narration(text: str | None, allowed_names: set[str], roster_names: set[str]) -> tuple[str | None, str | None]:
    """(narration, reason it was dropped). Enforces the instruction instead of trusting it."""
    if not text or not text.strip():
        return None, "empty"
    if DECIMAL.search(text):
        return None, "contained a score"
    if re.search(r"\b(caus|made you)", text, re.I):
        return None, "causal claim"
    mentioned = {n for n in roster_names if n.lower() in text.lower()}
    if mentioned - allowed_names:
        return None, "mentioned an athlete the tools did not return"
    words = text.split()
    return (" ".join(words[:MAX_WORDS]) + "…" if len(words) > MAX_WORDS else text.strip()), None


def repair_prompt(req: dict[str, Any]) -> str:
    return (f"Repair request. sport={req['sport']}, window={req['window']}, reason={req['reason']}, "
            f"rejected_ids={req['rejected_ids']}, allow_sport_switch={req.get('allow_sport_switch', False)}. "
            "Call swap_pick with exactly these arguments, then tell the user who to follow instead and when to watch.")


async def run_repair(tools: SideDoorTools, req: dict[str, Any], runner: Runner = adk_runner,
                     timeout_s: float = AGENT_TIMEOUT_S, model: str | None = None) -> dict[str, Any]:
    model = model or os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    tools.check_rejected(req["sport"], req["rejected_ids"])  # fail fast, before spending an LLM call
    tracer = Tracer()
    funcs = agent_tools(tools, tracer, list(req["seed_ids"]))

    started = time.perf_counter()
    narration, error = None, None
    try:
        narration = await asyncio.wait_for(runner(repair_prompt(req), funcs, model), timeout_s)
    except TimeoutError:
        error = f"timeout after {timeout_s:g}s"
    except Exception as exc:  # noqa: BLE001 -- Gemini down must never break the response
        error = type(exc).__name__
    tracer.add(f"agent ({model})", "repair", started, error is None, error)

    swap: SwapResult | None = tracer.results.get("swap_pick")
    if swap is not None and (swap.reason != req["reason"] or not set(req["rejected_ids"]) <= set(swap.rejected_ids)):
        swap, error = None, "agent called swap_pick with other arguments"  # never trust it over the request
    if swap is None:  # agent failed, skipped or misused the tool: run the same tool deterministically
        args = {"sport": req["sport"], "rejected_ids": list(req["rejected_ids"]), "reason": req["reason"],
                "window": req["window"], "allow_sport_switch": req.get("allow_sport_switch", False)}
        started = time.perf_counter()
        swap = tools.swap_pick(list(req["seed_ids"]), **args)  # real errors (e.g. budget) surface to the API
        tracer.add("swap_pick (deterministic fallback)", summarize_args(args), started, True)

    payload = payload_from_ranking(tools, swap.ranking, req["window"], swap.featured_id, StepTrace())
    shown = {c["name"] for c in [payload["featured"], *payload["alternates"]] if c}
    rejected_names = {swap.ranking.roster[r].name for r in swap.rejected_ids if r in swap.ranking.roster}
    roster_names = {a.name for s in ("f1", "pl") for a in tools.roster(s)}
    narration, dropped = guard_narration(narration, shown | rejected_names, roster_names) if error is None else (None, error)
    return {"payload": payload, "what_changed": swap.what_changed, "narration": narration,
            "narration_available": narration is not None, "narration_note": dropped, "trace": tracer.entries}
