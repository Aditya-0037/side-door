"""Athlete rosters: loading data/rosters.json, fetching the current F1 grid from Jolpica, and the
name + sport checks used to verify Qloo search hits."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from backend.qloo.models import SearchResult

ROOT = Path(__file__).resolve().parents[2]
ROSTERS_PATH = ROOT / "data" / "rosters.json"
PL_SEED_PATH = ROOT / "data" / "pl_seed.json"
JOLPICA = "https://api.jolpi.ca/ergast/f1"

# Strong evidence beats weak: e.g. "Carlos Sainz" also matches his father, a rally (racing) driver.
SPORT_KEYWORDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "f1": (("formula one", "formula 1", "formula_one"),
           ("racing driver", "racing_driver", "racing automobile driver", "racing_automobile_driver",
            "motorsport")),
    "pl": (("association football", "association_football", "footballer", "soccer"), ()),
}
F1_ALIASES = {"Carlos Sainz": ["Carlos Sainz Jr."]}

_TRANSLIT = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "ß": "ss", "ł": "l", "Ł": "L",
                           "đ": "d", "Đ": "D", "þ": "th", "ı": "i", "œ": "oe"})
_SUFFIXES = {"jr", "sr", "ii", "iii"}


@dataclass(frozen=True)
class RosterAthlete:
    sport: str
    name: str
    team: str
    qloo_id: str | None
    verified: str  # "auto" | "needs_review"
    evidence: str | None = None
    qloo_name: str | None = None
    source: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)


def load_rosters(path: Path = ROSTERS_PATH, sport: str | None = None,
                 verified_only: bool = True) -> list[RosterAthlete]:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    athletes = [RosterAthlete(**row) for row in rows]
    return [a for a in athletes
            if (sport is None or a.sport == sport) and (not verified_only or (a.verified == "auto" and a.qloo_id))]


# ---------------------------------------------------------------- name matching


def name_tokens(name: str) -> tuple[str, ...]:
    """Accent-insensitive tokens: 'Martin Ødegaard' -> ('martin', 'odegaard'); drops Jr./Sr."""
    text = unicodedata.normalize("NFKD", name.translate(_TRANSLIT))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return tuple(t for t in re.sub(r"[^a-z0-9]+", " ", text).split() if t not in _SUFFIXES)


def names_match(query: str, candidate: str) -> bool:
    """Equal tokens, or one full name contained in the other with the same family name
    ('Andrea Kimi Antonelli' ~ 'Kimi Antonelli', but not 'Giovanna Antonelli')."""
    q, c = name_tokens(query), name_tokens(candidate)
    if not q or not c:
        return False
    if q == c:
        return True
    short, long = sorted((q, c), key=len)
    return len(short) >= 2 and q[-1] == c[-1] and set(short) <= set(long)


# ---------------------------------------------------------------- sport evidence


def sport_evidence(result: SearchResult, sport: str) -> tuple[int, str] | None:
    """(strength, evidence) where strength 2 = sport-specific, 1 = generic (e.g. 'racing driver')."""
    fields = [*(("properties.sport", s) for s in result.sports),
              ("short_description", result.short_description or ""),
              *(("tag", t.id) for t in result.tags)]
    for strength, keywords in zip((2, 1), SPORT_KEYWORDS[sport]):
        for source, value in fields:
            low = str(value).lower()
            if any(k in low for k in keywords):
                return strength, f"{source}: {value}"
    return None


@dataclass(frozen=True)
class Pick:
    status: str  # "auto" | "ambiguous" | "none"
    result: SearchResult | None = None
    evidence: str | None = None


def pick_candidate(names: str | Sequence[str], sport: str, results: Sequence[SearchResult]) -> Pick:
    """Auto-accept only when exactly one candidate (at the strongest evidence level) matches one of
    the athlete's names AND shows evidence of the right sport. Never guess between several."""
    names = [names] if isinstance(names, str) else list(names)
    matches: dict[str, tuple[int, str, SearchResult]] = {}
    for r in results:
        if r.entity_id in matches or not any(names_match(n, r.name) for n in names):
            continue
        ev = sport_evidence(r, sport)
        if ev:
            matches[r.entity_id] = (ev[0], ev[1], r)
    if not matches:
        return Pick("none")
    best = max(m[0] for m in matches.values())
    top = [m for m in matches.values() if m[0] == best]
    if len(top) > 1:
        return Pick("ambiguous")
    return Pick("auto", top[0][2], top[0][1])


def candidate_summary(r: SearchResult) -> dict[str, Any]:
    return {"name": r.name, "disambiguation": r.disambiguation,
            "short_description": r.short_description, "entity_id": r.entity_id}


# ---------------------------------------------------------------- sources


def f1_query_names(given: str, family: str) -> list[str]:
    """Jolpica's full name first, then shorter forms ('Andrea Kimi Antonelli' -> 'Kimi Antonelli')."""
    full = f"{given} {family}"
    names = [full]
    parts = given.split()
    if len(parts) > 1:
        names += [f"{parts[-1]} {family}", f"{parts[0]} {family}"]
    names += F1_ALIASES.get(full, [])
    return list(dict.fromkeys(names))


def fetch_f1_grid(cache_file: Path | None = None, refresh: bool = False) -> list[dict[str, Any]]:
    """Current grid = the starters of the latest race (Jolpica /current/last/results).

    drivers.json also lists FP1/reserve drivers and constructors/{id}/drivers.json includes them
    too, so the last race result is the cleanest "who drives for whom right now" source.
    """
    if cache_file and cache_file.exists() and not refresh:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    else:
        resp = httpx.get(f"{JOLPICA}/current/last/results.json", timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if cache_file:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    race = data["MRData"]["RaceTable"]["Races"][0]
    grid = []
    for row in race["Results"]:
        driver = row["Driver"]
        grid.append({"name": f"{driver['givenName']} {driver['familyName']}",
                     "team": row["Constructor"]["name"],
                     "queries": f1_query_names(driver["givenName"], driver["familyName"]),
                     "source": f"Jolpica {race['season']} R{race['round']} ({race['raceName']})"})
    return grid


def load_pl_seed(path: Path = PL_SEED_PATH) -> list[dict[str, Any]]:
    seed = json.loads(Path(path).read_text(encoding="utf-8"))
    return [{"name": p["name"], "team": p["team"], "queries": [p["name"], *p.get("aliases", [])],
             "source": "data/pl_seed.json (manually curated)"} for p in seed["players"]]
