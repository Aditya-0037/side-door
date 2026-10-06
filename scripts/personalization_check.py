"""Personalization-strength check: how different are the 20 personas' personal rankings?

Offline only (reads data/sweep_inputs.json, 0 API calls); writes data/personalization_strength.json.
    $env:PYTHONUTF8="1"; .venv\\Scripts\\python.exe scripts/personalization_check.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.inventory.rosters import ROOT  # noqa: E402
from backend.qloo.models import InsightEntity  # noqa: E402
from backend.ranking.personalization import (  # noqa: E402
    STRONG_BELOW, WEAK_ABOVE, mean_pairwise_spearman, personalization_strength, spearman,
)
from backend.ranking.taste_lift import DEFAULT_ALPHA, DEFAULT_BETA, compute_taste_lift  # noqa: E402

INPUTS_PATH = ROOT / "data" / "sweep_inputs.json"
OUT_PATH = ROOT / "data" / "personalization_strength.json"


def main() -> None:
    inputs = json.loads(INPUTS_PATH.read_text(encoding="utf-8"))
    teams = {r["entity_id"]: r["team"] for r in inputs["roster"]}
    baseline = [InsightEntity(**e) for e in inputs["baseline"]]
    personas = {p: [InsightEntity(**e) for e in es] for p, es in inputs["personal"].items()}
    s = personalization_strength(personas, baseline)

    # Reference: the same comparison after Taste Lift (final score) at the adopted defaults.
    finals = {}
    for p, es in personas.items():
        by_id = {a.entity_id: a.final for a in compute_taste_lift(es, baseline, teams).ranked}
        finals[p] = by_id
    common = sorted(set.intersection(*(set(f) for f in finals.values())))
    crowd = {e.entity_id: e.popularity or 0.0 for e in baseline}
    lift_vectors = {p: [f[eid] for eid in common] for p, f in finals.items()}
    lift_mean, _, _, _ = mean_pairwise_spearman(lift_vectors)
    lift_vs_crowd = sum(spearman(v, [crowd[eid] for eid in common]) for v in lift_vectors.values()) / len(lift_vectors)

    # How big are the affinity differences the reorderings rest on?
    ranges, gaps = [], []
    for es in personas.values():
        affs = sorted((e.affinity for e in es if e.affinity is not None), reverse=True)
        ranges.append(affs[0] - affs[-1])
        gaps += [a - b for a, b in zip(affs, affs[1:])]
    margins = {"affinity_range_per_persona_median": median(ranges), "neighbour_gap_median": median(gaps)}

    print(f"{len(personas)} personas, {s.common_athletes} athletes ranked by every persona and the crowd "
          f"({s.persona_pairs} persona pairs)\n")
    print("Personal (raw Qloo affinity) rankings:")
    print(f"  persona vs persona Spearman: mean {s.persona_persona_mean:.3f} "
          f"(min {s.persona_persona_min:.3f}, max {s.persona_persona_max:.3f})")
    print(f"  persona vs crowd   Spearman: mean {s.persona_crowd_mean:.3f}")
    print(f"  => taste signal is {s.verdict}  (weak > {WEAK_ABOVE}, strong < {STRONG_BELOW})")
    print(f"  but margins are thin: affinity range per persona (17 athletes) median "
          f"{margins['affinity_range_per_persona_median']:.3f}, neighbour gap median {margins['neighbour_gap_median']:.4f}")
    print(f"\nAfter Taste Lift (alpha={DEFAULT_ALPHA}, beta={DEFAULT_BETA}) for reference:")
    print(f"  persona vs persona Spearman: mean {lift_mean:.3f};  persona vs crowd: mean {lift_vs_crowd:.3f}")
    print("\nPersonas with the athlete in their personal top-3:")
    for name, count in s.top3_counts.items():
        print(f"  {count:2d}  {name}")

    OUT_PATH.write_text(json.dumps({
        "generated_on": inputs["generated_on"], "source": "data/sweep_inputs.json (cached, 0 API calls)",
        "personas": len(personas), "thresholds": {"weak_above": WEAK_ABOVE, "strong_below": STRONG_BELOW},
        **asdict(s),
        "margins": margins,
        "reference_after_taste_lift": {"alpha": DEFAULT_ALPHA, "beta": DEFAULT_BETA,
                                       "persona_persona_mean": lift_mean, "persona_crowd_mean": lift_vs_crowd},
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
