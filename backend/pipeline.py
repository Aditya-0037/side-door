"""The deterministic fast path: rank -> lift -> why_line -> event -> clips, with a step trace.

No LLM anywhere in here, so the main flow keeps working when Gemini is down or slow.
UI fields carry ranks and labels only; raw scores live under each card's `debug`.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from typing import Any

from backend.agent.tools import Ranking, SideDoorTools
from backend.ranking.taste_lift import NO_DATA_TITLE, LiftedAthlete

ALTERNATES = 2


class StepTrace:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []

    def add(self, step: str, started: float, ok: bool = True, detail: str = "", error: str | None = None) -> None:
        self.steps.append({"step": step, "detail": detail, "ms": round((time.perf_counter() - started) * 1000),
                           "ok": ok, "error": error})


def card(tools: SideDoorTools, a: LiftedAthlete, ranking: Ranking, window: str) -> dict[str, Any]:
    explained = tools.explain_pick(a, ranking.result.n, ranking.seeds)
    total = sum(a.seed_shares.values()) or 1.0
    return {
        "entity_id": a.entity_id, "name": a.name, "team": a.team,
        "crowd_rank": a.crowd_rank, "personal_rank": a.personal_rank, "lift": a.lift, "n_scored": ranking.result.n,
        "why_line": explained["why_line"], "distinctive_seed": explained.get("distinctive_seed"),
        "seed_shares_pct": [{"seed": ranking.seeds.get(s, s), "pct": round(100 * v / total)}
                            for s, v in sorted(a.seed_shares.items(), key=lambda kv: -kv[1])],
        "next_event": tools.next_watchable_event(a.entity_id, ranking.sport, window),
        "clips": tools.official_clips(a.entity_id, ranking.sport),
        "debug": {"final": a.final, "pct_personal": a.pct_personal, "crowd_pct": a.crowd_pct,
                  "distinctive_ratio": a.distinctive_ratio},
    }


def payload_from_ranking(tools: SideDoorTools, ranking: Ranking, window: str, featured_id: str | None = None,
                         trace: StepTrace | None = None) -> dict[str, Any]:
    trace = trace or StepTrace()
    ranked = ranking.ranked
    featured = next((a for a in ranked if a.entity_id == featured_id), ranked[0] if ranked else None)
    others = [a for a in ranked if featured is None or a.entity_id != featured.entity_id][:ALTERNATES]

    t = time.perf_counter()
    cards = [card(tools, a, ranking, window) for a in ([featured] if featured else []) + others]
    trace.add("why+events+clips", t, detail=f"{len(cards)} cards")

    roster = ranking.roster
    unknown = [{"entity_id": u.entity_id, "name": roster[u.entity_id].name if u.entity_id in roster else u.name,
                "team": u.team} for u in ranking.result.unknown]
    events_ok = any(c["next_event"] for c in cards) or not cards
    return {
        "sport": ranking.sport, "window": window,
        "seeds": [{"entity_id": sid, "name": name} for sid, name in ranking.seeds.items()],
        "featured": cards[0] if featured else None, "alternates": cards[1:] if featured else cards,
        "unknown": {"title": NO_DATA_TITLE, "athletes": unknown}, "n_scored": ranking.result.n,
        "events_available": events_ok, "trace": trace.steps,
        "cached": ranking.qloo_calls == 0, "precomputed": False,
    }


def side_door(tools: SideDoorTools, seed_ids: Sequence[str], sport: str, window: str,
              exclude_ids: Iterable[str] = ()) -> dict[str, Any]:
    trace = StepTrace()
    t = time.perf_counter()
    ranking = tools.rank_candidates(seed_ids, sport, exclude_ids)
    trace.add("rank (qloo insights + taste lift)", t,
              detail=f"{len(seed_ids)} seeds x {len(ranking.roster)} athletes, qloo_calls={ranking.qloo_calls}")
    return payload_from_ranking(tools, ranking, window, trace=trace)


def refresh_events(tools: SideDoorTools, payload: dict[str, Any], window: str) -> dict[str, Any]:
    """Re-attach next events (for this window) and clips to a precomputed payload; no Qloo calls."""
    t = time.perf_counter()
    out = {**payload, "window": window}

    def live(c: dict[str, Any]) -> dict[str, Any]:
        return {**c, "next_event": tools.next_watchable_event(c["entity_id"], out["sport"], window),
                "clips": tools.official_clips(c["entity_id"], out["sport"])}

    out["featured"] = live(out["featured"]) if out.get("featured") else None
    out["alternates"] = [live(c) for c in out.get("alternates", [])]
    cards = [c for c in [out["featured"], *out["alternates"]] if c]
    out["events_available"] = any(c["next_event"] for c in cards) or not cards
    trace = StepTrace()
    trace.add("events+clips (refresh)", t, detail=window)
    out["trace"] = [{"step": "precomputed payload", "detail": "0 live calls", "ms": 0, "ok": True, "error": None},
                    *trace.steps]
    return out
