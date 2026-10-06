"""Resolve every roster athlete to a Qloo entity and write data/rosters.json.

Usage:
    $env:PYTHONUTF8="1"; .venv\\Scripts\\python.exe scripts/verify_rosters.py [--sport f1|pl|all]

F1 comes from data/f1_grid.json (refresh it with scripts/refresh_f1_grid.py), PL from
data/pl_seed.json. A candidate is auto-accepted only at evidence tier 1 (current club in its team
list) or tier 2 (sport + nationality + plausible DOB), and only if it is the single candidate at
that tier. Manual picks in data/roster_overrides.json win and are marked verified: "manual".
Search responses are cached in .cache/qloo/ for 30 days, so re-runs cost no quota.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.inventory.rosters import (  # noqa: E402
    ROOT, ROSTERS_PATH, Expected, Pick, candidate_summary, load_f1_grid, load_overrides, load_pl_seed,
    pick_candidate, tier_evidence,
)
from backend.qloo.cache import prime_from_fixtures  # noqa: E402
from backend.qloo.client import PERSON, QlooClient  # noqa: E402
from backend.qloo.models import SearchResult  # noqa: E402

SEARCH_TTL = 30 * 24 * 3600.0
FIXTURES = ROOT / "tests" / "fixtures"


def verify(entry: dict[str, Any], sport: str, client: QlooClient,
           override: dict[str, Any] | None = None) -> dict[str, Any]:
    expected = Expected(names=entry["queries"], team=entry["team"],
                        nationality=entry.get("nationality"), dob=entry.get("dob"))
    row: dict[str, Any] = {"sport": sport, "name": entry["name"], "team": entry["team"], "qloo_id": None,
                           "verified": "needs_review", "tier": None, "evidence": None, "qloo_name": None,
                           "source": entry["source"], "candidates": []}
    seen: dict[str, SearchResult] = {}
    pick = Pick("none")
    for query in entry["queries"]:
        for r in client.search(query, types=PERSON, take=5):
            seen.setdefault(r.entity_id, r)
        # Judge everything seen so far, so an ambiguity found by one query isn't hidden by the next.
        pick = pick_candidate(expected, sport, list(seen.values()))
        if pick.status != "none":
            break

    if override:
        chosen = seen.get(override["qloo_id"])
        ev = tier_evidence(chosen, expected, sport) if chosen else None
        row.update(qloo_id=override["qloo_id"], verified="manual", tier=ev[0] if ev else None,
                   qloo_name=chosen.name if chosen else None,
                   evidence=f"manual: {override['reason']}" + (f" | tier {ev[0]}: {ev[1]}" if ev else ""))
        if pick.status == "auto" and pick.result and pick.result.entity_id != override["qloo_id"]:
            print(f"  !!  {entry['name']}: manual pick differs from the auto pick {pick.result.entity_id}")
    elif pick.status == "auto" and pick.result:
        row.update(qloo_id=pick.result.entity_id, verified="auto", tier=pick.tier, evidence=pick.evidence,
                   qloo_name=pick.result.name)
    else:
        row["candidates"] = [candidate_summary(r) for r in list(seen.values())[:10]]
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sport", choices=["f1", "pl", "all"], default="all")
    args = parser.parse_args()

    client = QlooClient.dev(cache_ttl=SEARCH_TTL)
    calls_before = client.budget.used
    primed = prime_from_fixtures(client.cache, sorted(FIXTURES.glob("k1-*.json")), "/search", SEARCH_TTL)
    print(f"Primed {primed} search responses from tests/fixtures/k1-*.json")

    sources: dict[str, list[dict[str, Any]]] = {}
    if args.sport in ("f1", "all"):
        sources["f1"] = load_f1_grid()
        print(f"F1 grid: {len(sources['f1'])} drivers from {sources['f1'][0]['source']}")
    if args.sport in ("pl", "all"):
        sources["pl"] = load_pl_seed()
        print(f"PL seed: {len(sources['pl'])} players (manually curated)")
    overrides = load_overrides()

    existing = json.loads(ROSTERS_PATH.read_text(encoding="utf-8")) if ROSTERS_PATH.exists() else []
    rows = [r for r in existing if r["sport"] not in sources]
    for sport, entries in sources.items():
        print(f"\n== {sport} ==")
        for entry in entries:
            row = verify(entry, sport, client, overrides.get((sport, entry["name"])))
            rows.append(row)
            if row["verified"] == "needs_review":
                print(f"  ??  {row['name']:24s} NEEDS REVIEW. Candidates:")
                for c in row["candidates"][:5]:
                    print(f"        - {c['name']} | {c['disambiguation']} | {c['short_description']} | {c['entity_id']}")
            else:
                label = "MAN" if row["verified"] == "manual" else f"T{row['tier']} "
                print(f"  {label} {row['name']:24s} -> {row['qloo_name'] or '?':24s} [{row['evidence']}]")

    rows.sort(key=lambda r: (r["sport"], r["team"], r["name"]))
    ROSTERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ROSTERS_PATH.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print("\n== summary ==")
    for sport in sources:
        sport_rows = [r for r in rows if r["sport"] == sport]
        count = {k: sum(r["verified"] == k for r in sport_rows) for k in ("auto", "manual", "needs_review")}
        tiers = {t: sum(r["verified"] == "auto" and r["tier"] == t for r in sport_rows) for t in (1, 2)}
        print(f"  {sport}: {count['auto']} auto (tier1 {tiers[1]}, tier2 {tiers[2]}), {count['manual']} manual, "
              f"{count['needs_review']} needs_review "
              f"{[r['name'] for r in sport_rows if r['verified'] == 'needs_review'] or ''}")
    print(f"  Qloo calls this run: {client.budget.used - calls_before} (today: {client.budget.used}/{client.budget.limit})")
    print(f"  wrote {ROSTERS_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
