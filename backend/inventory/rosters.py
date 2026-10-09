"""Athlete rosters: the F1 grid snapshot (Jolpica), the PL seed list, data/rosters.json, and the
evidence tiers used to verify Qloo search hits.

Evidence tiers (a candidate must always match the athlete's name first):
  tier 1 -- the athlete's current club/team appears in the candidate's team list;
  tier 2 -- sport evidence + matching nationality + plausible date of birth
            (the exact DOB when the source has one, as Jolpica does for F1);
  else   -- needs_review. Popularity is never used to choose between candidates.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from backend.qloo.models import SearchResult

ROOT = Path(__file__).resolve().parents[2]
ROSTERS_PATH = ROOT / "data" / "rosters.json"
PL_SEED_PATH = ROOT / "data" / "pl_seed.json"
F1_GRID_PATH = ROOT / "data" / "f1_grid.json"
OVERRIDES_PATH = ROOT / "data" / "roster_overrides.json"
JOLPICA = "https://api.jolpi.ca/ergast/f1"
VERIFIED = ("auto", "manual")

SPORT_KEYWORDS = {
    "f1": ("formula one", "formula 1", "formula_one", "racing driver", "racing_driver",
           "racing automobile driver", "racing_automobile_driver", "motorsport"),
    "pl": ("association football", "association_football", "footballer", "soccer"),
}
# Source demonym -> country names as Qloo writes them in properties.citizenship.
NATIONALITY_COUNTRIES = {
    "american": ("united states",), "argentine": ("argentina",), "australian": ("australia",),
    "belgian": ("belgium",), "brazilian": ("brazil",), "british": ("united kingdom", "england", "scotland", "wales"),
    "cameroonian": ("cameroon",), "canadian": ("canada",), "danish": ("denmark",), "dutch": ("netherlands",),
    "ecuadorian": ("ecuador",), "egyptian": ("egypt",), "english": ("united kingdom", "england"),
    "finnish": ("finland",), "french": ("france",), "german": ("germany",), "italian": ("italy",),
    "japanese": ("japan",), "mexican": ("mexico",), "monegasque": ("monaco",), "new zealander": ("new zealand",),
    "norwegian": ("norway",), "portuguese": ("portugal",), "spanish": ("spain",), "swedish": ("sweden",),
    "thai": ("thailand",),
}
PLAUSIBLE_AGE = (16, 45)  # active top-flight athlete, used when the source has no exact DOB
F1_ALIASES = {"Carlos Sainz": ["Carlos Sainz Jr."]}

_TRANSLIT = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "ß": "ss", "ł": "l", "Ł": "L",
                           "đ": "d", "Đ": "D", "þ": "th", "ı": "i", "œ": "oe"})
_SUFFIXES = {"jr", "sr", "ii", "iii"}
_CLUB_NOISE = {"f", "c", "fc", "afc", "cf", "f1", "formula", "one", "team", "racing", "scuderia"}


@dataclass(frozen=True)
class RosterAthlete:
    sport: str
    name: str
    team: str
    qloo_id: str | None
    verified: str  # "auto" | "manual" | "needs_review"
    tier: int | None = None  # evidence tier of qloo_id (1 or 2), None if no tier passed
    evidence: str | None = None
    qloo_name: str | None = None
    dob: str | None = None  # date of birth of the chosen Qloo entity (used to match Wikidata safely)
    source: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)


def load_rosters(path: Path = ROSTERS_PATH, sport: str | None = None,
                 verified_only: bool = True) -> list[RosterAthlete]:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    athletes = [RosterAthlete(**row) for row in rows]
    return [a for a in athletes if (sport is None or a.sport == sport)
            and (not verified_only or (a.verified in VERIFIED and a.qloo_id))]


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


# ---------------------------------------------------------------- evidence


@dataclass(frozen=True)
class Expected:
    """What our source says about an athlete, checked against each Qloo candidate."""
    names: list[str]  # canonical name first, then aliases
    team: str
    nationality: str | None = None
    dob: str | None = None  # exact YYYY-MM-DD when the source has it


def club_tokens(name: str) -> set[str]:
    """'Manchester United F.C.' -> {'manchester', 'united'}; 'Scuderia Ferrari' -> {'ferrari'}."""
    return set(name_tokens(name)) - _CLUB_NOISE


def club_evidence(result: SearchResult, team: str) -> str | None:
    wanted = club_tokens(team)
    if not wanted:
        return None
    teams = result.properties.get("member_of_sports_team") or []
    for t in teams if isinstance(teams, list) else [teams]:
        if wanted <= club_tokens(str(t)):
            return f"member_of_sports_team: {t}"
    return None


def sport_evidence(result: SearchResult, sport: str) -> str | None:
    fields = [*(("properties.sport", s) for s in result.sports),
              ("short_description", result.short_description or ""),
              *(("tag", t.id) for t in result.tags)]
    for source, value in fields:
        if any(k in str(value).lower() for k in SPORT_KEYWORDS[sport]):
            return f"{source}: {value}"
    return None


def nationality_evidence(result: SearchResult, nationality: str | None) -> str | None:
    if not nationality:
        return None
    demonym = nationality.lower()
    countries = NATIONALITY_COUNTRIES.get(demonym, ())
    citizenship = result.properties.get("citizenship") or []
    for c in citizenship if isinstance(citizenship, list) else [citizenship]:
        if any(country in str(c).lower() for country in countries):
            return f"citizenship: {c}"
    desc = (result.short_description or "").lower()
    if demonym in desc:
        return f"short_description: {result.short_description}"
    return None


def candidate_dob(result: SearchResult) -> str | None:
    dob = result.properties.get("date_of_birth") or result.disambiguation
    return str(dob) if dob and re.fullmatch(r"\d{4}(-\d{2}-\d{2})?", str(dob)) else None


def dob_evidence(result: SearchResult, expected_dob: str | None, today: date) -> str | None:
    dob = candidate_dob(result)
    if not dob:
        return None
    if expected_dob:
        return f"date_of_birth: {dob} (= source)" if dob == expected_dob else None
    age = today.year - int(dob[:4])
    return f"date_of_birth: {dob} (age ~{age})" if PLAUSIBLE_AGE[0] <= age <= PLAUSIBLE_AGE[1] else None


def tier_evidence(result: SearchResult, expected: Expected, sport: str,
                  today: date | None = None) -> tuple[int, str] | None:
    """(tier, evidence) for one candidate, or None when it reaches no tier."""
    club = club_evidence(result, expected.team)
    if club:
        return 1, club
    parts = [sport_evidence(result, sport), nationality_evidence(result, expected.nationality),
             dob_evidence(result, expected.dob, today or date.today())]
    if all(parts):
        return 2, "; ".join(p for p in parts if p)
    return None


@dataclass(frozen=True)
class Pick:
    status: str  # "auto" | "ambiguous" | "none"
    result: SearchResult | None = None
    tier: int | None = None
    evidence: str | None = None


def pick_candidate(expected: Expected, sport: str, results: Sequence[SearchResult],
                   today: date | None = None) -> Pick:
    """Among candidates matching one of the athlete's names, the best tier wins -- but only if
    exactly one candidate reaches it. Never guesses, never looks at popularity."""
    found: dict[str, tuple[int, str, SearchResult]] = {}
    for r in results:
        if r.entity_id in found or not any(names_match(n, r.name) for n in expected.names):
            continue
        ev = tier_evidence(r, expected, sport, today)
        if ev:
            found[r.entity_id] = (ev[0], ev[1], r)
    if not found:
        return Pick("none")
    best = min(tier for tier, _, _ in found.values())
    top = [f for f in found.values() if f[0] == best]
    if len(top) > 1:
        return Pick("ambiguous")
    tier, evidence, result = top[0]
    return Pick("auto", result, tier, evidence)


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


def fetch_f1_grid() -> dict[str, Any]:
    """Current grid = the starters of the latest race (Jolpica /current/last/results).

    drivers.json also lists FP1/reserve drivers and constructors/{id}/drivers.json includes them
    too, so the latest race result is the cleanest "who drives for whom right now" source.
    """
    resp = httpx.get(f"{JOLPICA}/current/last/results.json", timeout=30)
    resp.raise_for_status()
    race = resp.json()["MRData"]["RaceTable"]["Races"][0]
    return {
        "source": "Jolpica-F1 /ergast/f1/current/last/results.json (CC BY-NC-SA 4.0)",
        "season": race["season"], "round": int(race["round"]), "race": race["raceName"],
        "race_date": race["date"], "fetched_on": date.today().isoformat(),
        "drivers": [{"driver_id": row["Driver"]["driverId"],
                     "given_name": row["Driver"]["givenName"], "family_name": row["Driver"]["familyName"],
                     "team": row["Constructor"]["name"], "constructor_id": row["Constructor"]["constructorId"],
                     "nationality": row["Driver"].get("nationality"),
                     "date_of_birth": row["Driver"].get("dateOfBirth")}
                    for row in race["Results"]],
    }


def load_f1_grid(path: Path = F1_GRID_PATH) -> list[dict[str, Any]]:
    if not Path(path).exists():
        raise FileNotFoundError(f"{path} missing: run scripts/refresh_f1_grid.py first")
    grid = json.loads(Path(path).read_text(encoding="utf-8"))
    source = f"Jolpica {grid['season']} R{grid['round']} ({grid['race']})"
    return [{"name": f"{d['given_name']} {d['family_name']}", "team": d["team"],
             "queries": f1_query_names(d["given_name"], d["family_name"]),
             "nationality": d.get("nationality"), "dob": d.get("date_of_birth"), "source": source}
            for d in grid["drivers"]]


def load_pl_seed(path: Path = PL_SEED_PATH) -> list[dict[str, Any]]:
    seed = json.loads(Path(path).read_text(encoding="utf-8"))
    return [{"name": p["name"], "team": p["team"], "queries": [p["name"], *p.get("aliases", [])],
             "nationality": p.get("nationality"), "dob": None,
             "source": "data/pl_seed.json (manually curated)"} for p in seed["players"]]


def load_overrides(path: Path = OVERRIDES_PATH) -> dict[tuple[str, str], dict[str, Any]]:
    """Manual picks for namesake collisions: (sport, name) -> {qloo_id, reason}."""
    if not Path(path).exists():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {(o["sport"], o["name"]): o for o in data["overrides"]}
