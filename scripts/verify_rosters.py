"""Resolve every roster athlete to a Qloo entity and write data/rosters.json.

Usage:
    $env:PYTHONUTF8="1"; .venv\\Scripts\\python.exe scripts/verify_rosters.py [--sport f1|pl|all] [--refresh-jolpica]

Auto-accepts a Qloo candidate only if the name matches (accent-insensitive) AND its properties/tags
show the right sport; anything else is written as needs_review with its candidates. Search responses
are cached in .cache/qloo/ for 30 days, so re-runs cost no quota.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.inventory.rosters import (  # noqa: E402
    ROOT, ROSTERS_PATH, candidate_summary, fetch_f1_grid, load_pl_seed, pick_candidate,
)
from backend.qloo.cache import cache_key  # noqa: E402
from backend.qloo.client import PERSON, QlooClient  # noqa: E402
from backend.qloo.models import SearchResult  # noqa: E402

SEARCH_TTL = 30 * 24 * 3600.0
FIXTURES = ROOT / "tests" / "fixtures"


def prime_cache_from_fixtures(client: QlooClient) -> int:
    """The Day-1 kill tests searched with the exact same params (types=person, take=5): reuse them."""
    primed = 0
    for path in sorted(FIXTURES.glob("k1-*.json")):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        if fixture.get("status") != 200:
            continue
        key = cache_key("/search", {k: str(v) for k, v in fixture["params"].items()})
        if client.cache.get(key) is None:
            client.cache.set(key, fixture["body"], SEARCH_TTL)
            primed += 1
    return primed


def verify(entry: dict[str, Any], sport: str, client: QlooClient) -> dict[str, Any]:
    row: dict[str, Any] = {"sport": sport, "name": entry["name"], "team": entry["team"], "qloo_id": None,
                           "verified": "needs_review", "evidence": None, "qloo_name": None,
                           "source": entry["source"], "candidates": []}
    seen: dict[str, SearchResult] = {}
    for query in entry["queries"]:
        for r in client.search(query, types=PERSON, take=5):
            seen.setdefault(r.entity_id, r)
        # Judge everything seen so far, so an ambiguity found by one query isn't hidden by the next.
        pick = pick_candidate(entry["queries"], sport, list(seen.values()))
        if pick.status == "auto" and pick.result:
            row.update(qloo_id=pick.result.entity_id, verified="auto", evidence=pick.evidence,
                       qloo_name=pick.result.name)
            return row
        if pick.status == "ambiguous":
            break
    row["candidates"] = [candidate_summary(r) for r in list(seen.values())[:10]]
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sport", choices=["f1", "pl", "all"], default="all")
    parser.add_argument("--refresh-jolpica", action="store_true")
    args = parser.parse_args()

    client = QlooClient.dev(cache_ttl=SEARCH_TTL)
    calls_before = client.budget.used
    print(f"Primed {prime_cache_from_fixtures(client)} search responses from tests/fixtures/k1-*.json")

    sources: dict[str, list[dict[str, Any]]] = {}
    if args.sport in ("f1", "all"):
        sources["f1"] = fetch_f1_grid(ROOT / ".cache" / "jolpica_last_results.json", args.refresh_jolpica)
        print(f"F1 grid: {len(sources['f1'])} drivers from {sources['f1'][0]['source']}")
    if args.sport in ("pl", "all"):
        sources["pl"] = load_pl_seed()
        print(f"PL seed: {len(sources['pl'])} players (manually curated)")

    existing = json.loads(ROSTERS_PATH.read_text(encoding="utf-8")) if ROSTERS_PATH.exists() else []
    rows = [r for r in existing if r["sport"] not in sources]
    for sport, entries in sources.items():
        print(f"\n== {sport} ==")
        for entry in entries:
            row = verify(entry, sport, client)
            rows.append(row)
            if row["verified"] == "auto":
                print(f"  OK  {row['name']:26s} -> {row['qloo_name']:26s} [{row['evidence']}]")
            else:
                print(f"  ??  {row['name']:26s} NEEDS REVIEW. Candidates:")
                for c in row["candidates"][:5]:
                    print(f"        - {c['name']} | {c['disambiguation']} | {c['short_description']} | {c['entity_id']}")

    rows.sort(key=lambda r: (r["sport"], r["team"], r["name"]))
    ROSTERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ROSTERS_PATH.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print("\n== summary ==")
    for sport in sources:
        sport_rows = [r for r in rows if r["sport"] == sport]
        auto = sum(r["verified"] == "auto" for r in sport_rows)
        print(f"  {sport}: {auto}/{len(sport_rows)} auto-verified; needs_review: "
              f"{[r['name'] for r in sport_rows if r['verified'] != 'auto'] or 'none'}")
    print(f"  Qloo calls this run: {client.budget.used - calls_before} (today: {client.budget.used}/{client.budget.limit})")
    print(f"  wrote {ROSTERS_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
