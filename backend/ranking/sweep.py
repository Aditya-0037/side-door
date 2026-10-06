"""Offline parameter sweep for Taste Lift: how (alpha, beta) change the top-1 pick across personas.

Pure functions over already-fetched results; no I/O, no API calls.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from backend.qloo.models import InsightEntity
from backend.ranking.taste_lift import compute_taste_lift

ALPHAS = (0.0, 0.15, 0.3, 0.5)
BETAS = (0.0, 0.15, 0.3)
MIN_MEAN_PERSONAL_PCT = 0.75  # the pick must still be clearly "yours"


@dataclass(frozen=True)
class SweepRow:
    alpha: float
    beta: float
    distinct_top1: int  # how many different athletes end up #1 across personas
    crowd_top3_rate: float  # share of #1 picks that sit in the crowd top-3
    max_top1_share: float  # share of personas won by the single most frequent #1
    max_top1_athlete: str
    mean_personal_pct_of_top1: float  # personal-affinity percentile of the #1 pick, averaged
    top1: dict[str, str] = field(default_factory=dict)  # persona -> #1 athlete name


def evaluate(personas: Mapping[str, Sequence[InsightEntity]], baseline: Sequence[InsightEntity],
             teams: Mapping[str, str | None], alpha: float, beta: float) -> SweepRow:
    top1: dict[str, str] = {}
    crowd_hits, pct_sum = 0, 0.0
    for persona, personal in personas.items():
        ranked = compute_taste_lift(personal, baseline, teams, alpha=alpha, beta=beta).ranked
        if not ranked:
            continue
        pick = ranked[0]
        top1[persona] = pick.name
        crowd_hits += pick.crowd_rank <= 3
        pct_sum += pick.pct_personal
    n = len(top1)
    if n == 0:
        raise ValueError("no persona produced a ranking")
    athlete, wins = Counter(top1.values()).most_common(1)[0]
    return SweepRow(alpha=alpha, beta=beta, distinct_top1=len(set(top1.values())),
                    crowd_top3_rate=crowd_hits / n, max_top1_share=wins / n, max_top1_athlete=athlete,
                    mean_personal_pct_of_top1=pct_sum / n, top1=top1)


def sweep(personas: Mapping[str, Sequence[InsightEntity]], baseline: Sequence[InsightEntity],
          teams: Mapping[str, str | None], alphas: Sequence[float] = ALPHAS,
          betas: Sequence[float] = BETAS) -> list[SweepRow]:
    return [evaluate(personas, baseline, teams, a, b) for a in alphas for b in betas]


def preference_key(row: SweepRow) -> tuple[float, ...]:
    """Most distinct #1s, then the least dominant single #1; ties go to fewer crowd-top-3 picks,
    then picks that are more personal, then the simpler (smaller) parameters."""
    return (-row.distinct_top1, row.max_top1_share, row.crowd_top3_rate,
            -row.mean_personal_pct_of_top1, row.alpha + row.beta, row.alpha)


def ordered(rows: Sequence[SweepRow], min_pct: float = MIN_MEAN_PERSONAL_PCT) -> list[SweepRow]:
    """Feasible rows (mean personal pct >= min_pct) by preference, then the infeasible ones."""
    return sorted(rows, key=lambda r: (r.mean_personal_pct_of_top1 < min_pct, *preference_key(r)))


def choose(rows: Sequence[SweepRow], min_pct: float = MIN_MEAN_PERSONAL_PCT) -> SweepRow:
    best = ordered(rows, min_pct)[0]
    if best.mean_personal_pct_of_top1 < min_pct:
        raise ValueError(f"no (alpha, beta) keeps mean_personal_pct_of_top1 >= {min_pct}")
    return best
