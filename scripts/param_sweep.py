"""Taste Lift parameter sweep over the 20 personas in data/personas_eval.json.

Usage:
    $env:PYTHONUTF8="1"; .venv\\Scripts\\python.exe scripts/param_sweep.py [--max-calls 30]
    .venv\\Scripts\\python.exe scripts/param_sweep.py --offline     # recompute from data/sweep_inputs.json

Live mode resolves seeds (typed search, cached 30 days, Day-1 fixtures primed), fetches one
personal insights call per persona over the verified F1 roster (baseline from cache), and saves
the compact responses to data/sweep_inputs.json. The (alpha, beta) grid is then computed offline
and written to data/sweep_results.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.inventory.rosters import ROOT, load_rosters  # noqa: E402
from backend.qloo.cache import prime_from_fixtures  # noqa: E402
from backend.qloo.client import QlooClient  # noqa: E402
from backend.qloo.models import InsightEntity  # noqa: E402
from backend.qloo.seeds import resolve_seed  # noqa: E402
from backend.ranking.sweep import ALPHAS, BETAS, MIN_MEAN_PERSONAL_PCT, choose, ordered, sweep  # noqa: E402
from backend.ranking.taste_lift import DEFAULT_ALPHA, DEFAULT_BETA, compute_taste_lift  # noqa: E402

TTL = 30 * 24 * 3600.0
PERSONAS_PATH = ROOT / "data" / "personas_eval.json"
INPUTS_PATH = ROOT / "data" / "sweep_inputs.json"
RESULTS_PATH = ROOT / "data" / "sweep_results.json"
SPORT = "f1"


def compact(entities: list[InsightEntity]) -> list[dict[str, Any]]:
    return [e.model_dump(include={"entity_id", "name", "affinity", "popularity", "explainability"})
            for e in entities]


def fetch_inputs(max_calls: int) -> dict[str, Any]:
    client = QlooClient.dev(cache_ttl=TTL)
    start = client.budget.used
    client.budget.limit = min(client.budget.limit, start + max_calls)  # hard cap for this run
    fixtures = sorted((ROOT / "tests" / "fixtures").glob("seed-*.json"))
    print(f"Primed {prime_from_fixtures(client.cache, fixtures, '/search', TTL)} seed searches from fixtures")

    personas = json.loads(PERSONAS_PATH.read_text(encoding="utf-8"))["personas"]
    resolved: dict[tuple[str, str], dict[str, str] | None] = {}
    print("\nSeeds:")
    for p in personas:
        for kind, text in p["seeds"].items():
            if (kind, text) not in resolved:
                hit = resolve_seed(client, text, kind)
                resolved[(kind, text)] = {"entity_id": hit.entity_id, "name": hit.name} if hit else None
                print(f"  {kind:9s} {text:30s} -> {hit.name + '  ' + hit.entity_id if hit else 'NOT FOUND'}")

    roster = load_rosters(sport=SPORT)
    candidate_ids = [a.qloo_id for a in roster if a.qloo_id]
    baseline = client.baseline_people(candidate_ids)
    personal: dict[str, list[dict[str, Any]]] = {}
    seeds_by_persona: dict[str, list[dict[str, str]]] = {}
    for p in personas:
        seeds = [resolved[(kind, text)] for kind, text in p["seeds"].items()]
        if any(s is None for s in seeds):
            print(f"  skip {p['id']}: a seed did not resolve")
            continue
        seeds_by_persona[p["id"]] = seeds  # type: ignore[assignment]
        personal[p["id"]] = compact(client.insights_people([s["entity_id"] for s in seeds], candidate_ids))
    used = client.budget.used - start
    print(f"\nQloo calls this run: {used} (today: {client.budget.used})")
    return {"generated_on": date.today().isoformat(), "sport": SPORT, "qloo_calls": used,
            "roster": [{"entity_id": a.qloo_id, "name": a.name, "team": a.team} for a in roster],
            "seeds": seeds_by_persona, "baseline": compact(baseline), "personal": personal}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="recompute from data/sweep_inputs.json")
    parser.add_argument("--max-calls", type=int, default=30)
    args = parser.parse_args()

    if args.offline:
        inputs = json.loads(INPUTS_PATH.read_text(encoding="utf-8"))
    else:
        inputs = fetch_inputs(args.max_calls)
        INPUTS_PATH.write_text(json.dumps(inputs, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    teams = {r["entity_id"]: r["team"] for r in inputs["roster"]}
    baseline = [InsightEntity(**e) for e in inputs["baseline"]]
    personas = {p: [InsightEntity(**e) for e in es] for p, es in inputs["personal"].items()}
    scored_ids = {e.entity_id for e in baseline} & {e.entity_id for es in personas.values() for e in es}
    no_data = sorted(r["name"] for r in inputs["roster"] if r["entity_id"] not in scored_ids)
    print(f"\n{len(personas)} personas over {len(teams)} roster athletes; "
          f"excluded for lack of data (unknown, never ranked): {no_data}")

    rows = ordered(sweep(personas, baseline, teams))
    best = choose(rows)
    print(f"\n  {'alpha':>5} {'beta':>5}  {'distinct':>8}  {'crowd_top3':>10}  {'max_share':>9}  "
          f"{'mean_pct':>8}  most frequent #1")
    for r in rows:
        flag = " <= chosen" if r == best else ("  (pct < 0.75)" if r.mean_personal_pct_of_top1 < MIN_MEAN_PERSONAL_PCT else "")
        print(f"  {r.alpha:5.2f} {r.beta:5.2f}  {r.distinct_top1:8d}  {r.crowd_top3_rate:10.2f}  "
              f"{r.max_top1_share:9.2f}  {r.mean_personal_pct_of_top1:8.3f}  {r.max_top1_athlete}{flag}")

    print(f"\nChosen: alpha={best.alpha}, beta={best.beta}. #1 distribution:")
    for name, wins in Counter(best.top1.values()).most_common():
        print(f"  {wins:2d}  {name}")
    if (best.alpha, best.beta) != (DEFAULT_ALPHA, DEFAULT_BETA):
        print(f"  !! taste_lift.py defaults are alpha={DEFAULT_ALPHA}, beta={DEFAULT_BETA}: update them.")

    lifts = {p: compute_taste_lift(es, baseline, teams, alpha=best.alpha, beta=best.beta).ranked[0]
             for p, es in personas.items()}
    RESULTS_PATH.write_text(json.dumps({
        "generated_on": inputs["generated_on"], "sport": inputs["sport"], "personas": len(personas),
        "roster_size": len(teams), "excluded_no_data": no_data, "grid": {"alpha": ALPHAS, "beta": BETAS},
        "selection_rule": (f"keep mean_personal_pct_of_top1 >= {MIN_MEAN_PERSONAL_PCT}; then max distinct_top1, "
                           "min max_top1_share; ties: min crowd_top3_rate, max mean_personal_pct_of_top1, "
                           "smaller alpha+beta"),
        "chosen": {"alpha": best.alpha, "beta": best.beta},
        "chosen_top1": {p: {"name": a.name, "crowd_rank": a.crowd_rank, "personal_rank": a.personal_rank,
                            "lift": a.lift, "distinctive_seed": a.distinctive_seed} for p, a in lifts.items()},
        "rows": [asdict(r) for r in rows],
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nwrote {RESULTS_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
