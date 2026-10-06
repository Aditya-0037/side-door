"""How strongly does a taste signal reshape the ranking? Offline analysis over many personas.

If every persona's personal ranking looks alike (high persona-persona Spearman), the taste signal
is weak and Qloo is mostly reshuffling the crowd ranking.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from statistics import correlation, fmean

from backend.qloo.models import InsightEntity
from backend.ranking.taste_lift import ranks, score

WEAK_ABOVE = 0.85
STRONG_BELOW = 0.6


@dataclass(frozen=True)
class PersonalizationStrength:
    common_athletes: int
    persona_pairs: int
    persona_persona_mean: float  # mean pairwise Spearman between personas' personal rankings
    persona_persona_min: float
    persona_persona_max: float
    persona_crowd_mean: float  # mean Spearman between each personal ranking and the crowd ranking
    persona_crowd: dict[str, float]
    top3_counts: dict[str, int]  # athlete -> personas with them in their personal top-3
    verdict: str


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    """Spearman rank correlation; tied values share their average rank."""
    return correlation(x, y, method="ranked")


def verdict(mean_pairwise: float) -> str:
    if mean_pairwise > WEAK_ABOVE:
        return "weak: personas get nearly the same order, mostly a crowd reshuffle"
    if mean_pairwise >= STRONG_BELOW:
        return "moderate: personas share a common backbone but reorder it noticeably"
    return "strong: personas get clearly different orders"


def mean_pairwise_spearman(vectors: Mapping[str, Sequence[float]]) -> tuple[float, float, float, int]:
    """(mean, min, max, pairs) of Spearman over all persona pairs."""
    values = [spearman(vectors[a], vectors[b]) for a, b in combinations(sorted(vectors), 2)]
    return fmean(values), min(values), max(values), len(values)


def personalization_strength(personas: Mapping[str, Sequence[InsightEntity]],
                             baseline: Sequence[InsightEntity]) -> PersonalizationStrength:
    """Compare personal rankings over the athletes that every persona AND the crowd ranked."""
    scored = {p: {e.entity_id: e for e in es if score(e) is not None} for p, es in personas.items()}
    crowd = {e.entity_id: e for e in baseline if score(e) is not None}
    common = sorted(set(crowd).intersection(*(set(s) for s in scored.values())))
    if len(common) < 3:
        raise ValueError("need at least 3 athletes ranked by every persona and the crowd")

    vectors = {p: [score(s[eid]) or 0.0 for eid in common] for p, s in scored.items()}
    crowd_vector = [score(crowd[eid]) or 0.0 for eid in common]
    mean, low, high, pairs = mean_pairwise_spearman(vectors)
    vs_crowd = {p: spearman(v, crowd_vector) for p, v in vectors.items()}

    names = {eid: crowd[eid].name for eid in common}
    top3_counts = {names[eid]: 0 for eid in common}
    for s in scored.values():
        order = ranks(s[eid] for eid in common)
        for eid in common:
            if order[eid] <= 3:
                top3_counts[names[eid]] += 1
    top3_counts = dict(sorted(top3_counts.items(), key=lambda kv: (-kv[1], kv[0])))

    return PersonalizationStrength(
        common_athletes=len(common), persona_pairs=pairs, persona_persona_mean=mean,
        persona_persona_min=low, persona_persona_max=high, persona_crowd_mean=fmean(vs_crowd.values()),
        persona_crowd=vs_crowd, top3_counts=top3_counts, verdict=verdict(mean))
