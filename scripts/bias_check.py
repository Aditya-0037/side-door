"""Task 5: popularity-bias check from the Day-1 K3 fixtures (no API calls).

Usage:
    $env:PYTHONUTF8="1"; .venv\\Scripts\\python.exe scripts/bias_check.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.inventory.rosters import ROOT, ROSTERS_PATH  # noqa: E402
from backend.qloo.models import InsightEntity, parse_insights  # noqa: E402
from backend.ranking.bias import bias_report, choose_beta  # noqa: E402
from backend.ranking.taste_lift import DEFAULT_ALPHA, DEFAULT_BETA, compute_taste_lift  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
PERSONAS = ["anime-indie", "space-pop", "dark-hiphop", "romance-pop", "rock-action"]
F1_TAG_HINTS = ("formula_one", "racing_driver", "racing_automobile_driver")


def load(name: str) -> list[InsightEntity]:
    return parse_insights(json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["body"])


def is_driver(e: InsightEntity) -> bool:
    return any(h in t.id for t in e.tags for h in F1_TAG_HINTS)


def print_report(personas: dict[str, list[InsightEntity]], baseline: list[InsightEntity]) -> float:
    report = bias_report(personas, baseline)
    print(f"  {'persona':12s} | {'top-3 (personal affinity)':62s} | top-1 in baseline top-3?")
    for r in report:
        print(f"  {r.persona:12s} | {', '.join(r.top3):62s} | {'YES' if r.top1_in_baseline_top3 else 'no'}")
    beta = choose_beta(report)
    hits = sum(r.top1_in_baseline_top3 for r in report)
    print(f"  => {hits}/5 personas have top-1 inside the baseline top-3 -> beta = {beta}")
    return beta


def main() -> None:
    baseline = load("k3-baseline")
    personas = {p: load(f"k3-{p}") for p in PERSONAS}
    base_top3 = [e.name for e in baseline[:3]]
    print(f"Baseline (no signal) top-3: {base_top3}\n")

    print("Raw K3 fixtures (as recorded):")
    beta = print_report(personas, baseline)

    # The Day-1 F1 set contains one wrong entity: "Kimi Antonelli" resolved to an actress.
    impostors = sorted({e.entity_id: e.name for es in [baseline, *personas.values()]
                        for e in es if not is_driver(e)}.items())
    print(f"\nNon-driver entities in the K3 set (no motorsport tags): {[n for _, n in impostors]}")
    print("Same check with them removed:")
    drop = {eid for eid, _ in impostors}
    clean = {p: [e for e in es if e.entity_id not in drop] for p, es in personas.items()}
    print_report(clean, [e for e in baseline if e.entity_id not in drop])

    print(f"\nChosen default beta: {beta}  (DEFAULT_BETA in backend/ranking/taste_lift.py = {DEFAULT_BETA})")
    if beta != DEFAULT_BETA:
        print("  !! DEFAULT_BETA does not match the evidence -- update the constant.")

    teams: dict[str, str | None] = {}
    if ROSTERS_PATH.exists():
        teams = {r["qloo_id"]: r["team"] for r in json.loads(ROSTERS_PATH.read_text(encoding="utf-8"))
                 if r.get("qloo_id")}
    print(f"\nK3 recomputed with Taste Lift (alpha={DEFAULT_ALPHA}, beta={beta}, impostors excluded, "
          "2026 teams from data/rosters.json where known):")
    for p, entities in personas.items():
        result = compute_taste_lift(entities, baseline, teams, alpha=DEFAULT_ALPHA, beta=beta, exclude_ids=drop)
        top = result.ranked[:3]
        old_top1 = clean[p][0].name if clean[p] else None
        print(f"  {p:12s} new top-1: {top[0].name:18s} (lift {top[0].lift:+d}, baseline #{top[0].baseline_rank})"
              f" | top-3: {[a.name for a in top]} | raw top-1 was {old_top1}")


if __name__ == "__main__":
    main()
