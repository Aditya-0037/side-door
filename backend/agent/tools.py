"""Tool functions shared by the deterministic fast path and the ADK agent. Plain typed Python.

Nothing here needs the LLM except llm_only_rank (the fair "Without Qloo" baseline), and that one
degrades to {"available": False} instead of failing.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from backend.inventory.clips import clip_dicts, clips_for, load_media
from backend.inventory.events import WINDOWS, EventStore
from backend.inventory.rosters import ROOT, RosterAthlete, load_rosters, name_tokens
from backend.qloo.client import QlooClient
from backend.qloo.seeds import SEED_TYPES, resolve_seed
from backend.ranking.taste_lift import LiftedAthlete, TasteLiftResult, compute_taste_lift, why_line

SPORTS = ("f1", "pl")
SPORT_LABELS = {"f1": "Formula 1", "pl": "Premier League"}
WINDOW_LABELS = {"evenings": "evenings", "late_night_ok": "late-night", "weekends_only": "weekends-only"}
SWAP_REASONS = ("not_feeling_it", "bad_time")
KIND_LABELS = {"urn:entity:movie": "film", "urn:entity:artist": "artist", "urn:entity:videogame": "game"}
SEED_NAMES_PATH = ROOT / ".cache" / "seed_names.json"
LLM_CACHE_DIR = ROOT / ".cache" / "llm"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


@dataclass(frozen=True)
class ResolvedSeed:
    input_text: str
    type: str | None
    found: bool
    entity_id: str | None = None
    name: str | None = None
    matched_as: str | None = None  # "Pride & Prejudice — film, 2005": what we read the text as


@dataclass(frozen=True)
class Ranking:
    sport: str
    seeds: dict[str, str]  # seed entity_id -> name, in the user's order
    result: TasteLiftResult
    roster: dict[str, RosterAthlete]  # entity_id -> athlete
    qloo_calls: int

    @property
    def ranked(self) -> list[LiftedAthlete]:
        return self.result.ranked


@dataclass(frozen=True)
class SwapResult:
    sport: str
    ranking: Ranking
    featured_id: str | None
    what_changed: str
    rejected_ids: list[str] = field(default_factory=list)
    reason: str = ""


class LLMPick(BaseModel):
    featured: str
    alternates: list[str]
    reason: str


class JsonLLM(Protocol):
    model: str

    def generate_json(self, prompt: str) -> dict[str, Any]: ...


class GeminiJson:
    """Strict-JSON Gemini call (google-genai reads GOOGLE_API_KEY / GEMINI_API_KEY from the env)."""

    def __init__(self, model: str | None = None, timeout_s: float = 12.0) -> None:
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
        self.timeout_ms = int(timeout_s * 1000)

    def generate_json(self, prompt: str) -> dict[str, Any]:
        from google import genai
        from google.genai import types

        client = genai.Client(http_options=types.HttpOptions(timeout=self.timeout_ms))
        resp = client.models.generate_content(
            model=self.model, contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json",
                                               response_schema=LLMPick, temperature=0.0))
        return json.loads(resp.text or "{}")


def _short_error(exc: Exception) -> str:
    """Error label without message text (messages could echo request details)."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return f"{type(exc).__name__}{f' {code}' if code else ''}"


class SideDoorTools:
    def __init__(self, qloo: QlooClient, events: EventStore, media: dict[str, Any] | None = None,
                 llm: JsonLLM | None = None, rosters: dict[str, list[RosterAthlete]] | None = None,
                 seed_names_path: Path | None = SEED_NAMES_PATH, llm_cache_dir: Path | None = LLM_CACHE_DIR) -> None:
        self.qloo = qloo
        self.events = events
        self.media = media if media is not None else load_media()
        self.llm = llm
        self._rosters = rosters
        self._seed_names_path = seed_names_path
        self._llm_cache_dir = llm_cache_dir
        self._seed_names: dict[str, str] = {}
        if seed_names_path and seed_names_path.exists():
            self._seed_names = json.loads(seed_names_path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------ helpers

    def roster(self, sport: str) -> list[RosterAthlete]:
        if sport not in SPORTS:
            raise ValueError(f"sport must be one of {SPORTS}")
        if self._rosters is not None:
            return self._rosters.get(sport, [])
        return load_rosters(sport=sport)

    def athlete(self, athlete_id: str, sport: str) -> RosterAthlete | None:
        return next((a for a in self.roster(sport) if a.qloo_id == athlete_id), None)

    def remember_seeds(self, names: dict[str, str]) -> None:
        self._seed_names.update(names)
        if self._seed_names_path:
            self._seed_names_path.parent.mkdir(parents=True, exist_ok=True)
            self._seed_names_path.write_text(json.dumps(self._seed_names, ensure_ascii=False), encoding="utf-8")

    def seed_name(self, seed_id: str) -> str:
        return self._seed_names.get(seed_id, "one of your picks")

    def check_rejected(self, sport: str, rejected_ids: Iterable[str]) -> None:
        roster = {a.qloo_id for a in self.roster(sport)}
        unknown = [r for r in rejected_ids if r not in roster]
        if unknown:
            raise ValueError(f"rejected_ids not on the {SPORT_LABELS[sport]} roster: {unknown}")

    # ------------------------------------------------------------ tools

    def resolve_tastes(self, items: Sequence[dict[str, Any]]) -> list[ResolvedSeed]:
        """Resolve {text, type} items (type: movie | artist | videogame | None) to Qloo entities."""
        out = []
        for item in items:
            text, kind = str(item.get("text", "")).strip(), item.get("type")
            if kind is not None and kind not in SEED_TYPES:
                raise ValueError(f"type must be one of {sorted(SEED_TYPES)} or omitted")
            hit = resolve_seed(self.qloo, text, kind) if text else None
            if hit is None:
                out.append(ResolvedSeed(text, kind, False))
                continue
            label = next((KIND_LABELS[t] for t in hit.types if t in KIND_LABELS), "title")
            extra = hit.disambiguation if hit.disambiguation and hit.disambiguation.lower() != hit.name.lower() else None
            matched = f"{hit.name} — {label}" + (f", {extra}" if extra else "")
            out.append(ResolvedSeed(text, kind, True, hit.entity_id, hit.name, matched))
        self.remember_seeds({s.entity_id: s.name for s in out if s.entity_id and s.name})
        return out

    def rank_candidates(self, seed_ids: Sequence[str], sport: str, exclude_ids: Iterable[str] = ()) -> Ranking:
        """Taste Lift over the verified roster: featured = ranked[0], alternates = ranked[1:3]."""
        roster = {a.qloo_id: a for a in self.roster(sport) if a.qloo_id}
        teams = {eid: a.team for eid, a in roster.items()}
        excluded = set(exclude_ids)
        before = self.qloo.budget.used
        personal = self.qloo.insights_people(list(seed_ids), list(roster), explain=True, exclude_ids=excluded)
        baseline = self.qloo.baseline_people(list(roster))
        result = compute_taste_lift(personal, baseline, teams, exclude_ids=excluded)
        seeds = {sid: self.seed_name(sid) for sid in seed_ids}
        return Ranking(sport, seeds, result, roster, self.qloo.budget.used - before)

    def explain_pick(self, athlete: LiftedAthlete, n: int, seeds: dict[str, str]) -> dict[str, Any]:
        """Factual why-line; the distinctive seed only when it clears the 1.05 ratio."""
        out: dict[str, Any] = {"why_line": why_line(athlete, n, list(seeds.values()))}
        if athlete.distinctive_seed:
            out["distinctive_seed"] = seeds.get(athlete.distinctive_seed, athlete.distinctive_seed)
        return out

    def next_watchable_event(self, athlete_id: str, sport: str, window: str) -> dict[str, Any] | None:
        if window not in WINDOWS:
            raise ValueError(f"window must be one of {WINDOWS}")
        a = self.athlete(athlete_id, sport)
        event = self.events.next_watchable_event(sport, a.team if a else None, window)
        return event.to_dict() if event else None

    def official_clips(self, athlete_id: str, sport: str) -> list[dict[str, Any]]:
        a = self.athlete(athlete_id, sport)
        return clip_dicts(clips_for(athlete_id, sport, self.media, a.name if a else None))

    def swap_pick(self, seed_ids: Sequence[str], sport: str, rejected_ids: Sequence[str], reason: str,
                  window: str = "evenings", allow_sport_switch: bool = False) -> SwapResult:
        """not_feeling_it: drop the rejected athletes and re-rank. bad_time: the best-ranked athlete
        whose next event fits the window (optionally switching sport). Rejected IDs never return."""
        if reason not in SWAP_REASONS:
            raise ValueError(f"reason must be one of {SWAP_REASONS}")
        rejected = list(dict.fromkeys(rejected_ids))
        self.check_rejected(sport, rejected)
        roster = {a.qloo_id: a for a in self.roster(sport)}
        gone = ", ".join(roster[r].name for r in rejected)

        if reason == "not_feeling_it":
            ranking = self.rank_candidates(seed_ids, sport, exclude_ids=rejected)
            new = ranking.ranked[0] if ranking.ranked else None
            what = (f"Swapped out {gone}. Next for your taste: {new.name}, #{new.personal_rank} of "
                    f"{ranking.result.n} among people who share it." if new else
                    f"Swapped out {gone}; nobody else has enough taste data yet.")
            return SwapResult(sport, ranking, new.entity_id if new else None, what, rejected, reason)

        label = WINDOW_LABELS.get(window, window)
        other = [s for s in SPORTS if s != sport]
        for s in [sport, *(other if allow_sport_switch else [])]:
            ranking = self.rank_candidates(seed_ids, s, exclude_ids=rejected)
            for a in ranking.ranked:
                event = self.next_watchable_event(a.entity_id, s, window)
                if event and event["fits_window"]:
                    if s == sport:
                        what = f"{a.name}'s next {event['kind']} ({event['label_ist']}) fits your {label} window."
                    else:
                        what = (f"No {SPORT_LABELS[sport]} session fits your {label} window in the next 14 days, "
                                f"so here's a {SPORT_LABELS[s]} pick: {a.name}, {event['label_ist']}.")
                    return SwapResult(s, ranking, a.entity_id, what, rejected, reason)
        ranking = self.rank_candidates(seed_ids, sport, exclude_ids=rejected)
        new = ranking.ranked[0] if ranking.ranked else None
        what = f"Nothing fits your {label} window in the next 14 days. Here's your best match; catch the highlights."
        return SwapResult(sport, ranking, new.entity_id if new else None, what, rejected, reason)

    def llm_only_rank(self, seed_names: Sequence[str], sport: str) -> dict[str, Any]:
        """The fair Without-Qloo baseline: the LLM sees the SAME roster and the SAME seed names and
        returns strict JSON. Unknown names are dropped; successful results are cached by input."""
        names = [a.name for a in self.roster(sport)]
        model = self.llm.model if self.llm else os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
        base = {"sport": sport, "seed_names": list(seed_names), "model": model}
        key = hashlib.sha256(json.dumps({**base, "roster": names}, sort_keys=True).encode()).hexdigest()[:32]
        cache_file = self._llm_cache_dir / f"{key}.json" if self._llm_cache_dir else None
        if cache_file and cache_file.exists():
            return {**json.loads(cache_file.read_text(encoding="utf-8")), "cached": True}
        if self.llm is None:
            return {**base, "available": False, "error": "no LLM configured", "featured": None,
                    "alternates": [], "reason": "", "dropped": [], "cached": False}
        prompt = (f"A newcomer to {SPORT_LABELS[sport]} loves: {', '.join(seed_names)}.\n"
                  f"Pick the ONE athlete from this roster they would most enjoy following, plus 2 alternates.\n"
                  f"Choose ONLY from this list, spelled exactly as written: {json.dumps(names, ensure_ascii=False)}\n"
                  "Return JSON {\"featured\": name, \"alternates\": [name, name], \"reason\": one sentence <= 30 words}.")
        try:
            raw = self.llm.generate_json(prompt)
        except Exception as exc:  # noqa: BLE001 -- any LLM failure means "unavailable", never a crash
            return {**base, "available": False, "error": _short_error(exc), "featured": None,
                    "alternates": [], "reason": "", "dropped": [], "cached": False}
        by_tokens = {name_tokens(n): n for n in names}
        proposed = [raw.get("featured"), *(raw.get("alternates") or [])]
        valid, dropped = [], []
        for p in proposed:
            match = by_tokens.get(name_tokens(str(p))) if p else None
            if match and match not in valid:
                valid.append(match)
            elif p and not match:
                dropped.append(str(p))
        result = {**base, "available": True, "error": None, "featured": valid[0] if valid else None,
                  "alternates": valid[1:3], "reason": str(raw.get("reason", ""))[:300], "dropped": dropped}
        if cache_file:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return {**result, "cached": False}
