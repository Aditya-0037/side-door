"""Taste Lift: rank the same athletes with and without the user's taste signal, and score the jump.

Pure functions, no I/O. Raw affinity is normalized per query, so it is never compared across two
requests -- only ranks and percentiles are. Missing data is UNKNOWN, never zero.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from backend.qloo.models import InsightEntity

DEFAULT_ALPHA = 0.3
# Popularity-bias penalty, decided from data (scripts/bias_check.py, Task 5). In the Day-1 K3
# fixtures, 2 of 5 very different personas (space-pop, dark-hiphop) got Lewis Hamilton -- the
# no-signal baseline #1 -- as their top-1 pick. Rule: >= 2 personas with top-1 inside the baseline
# top-3 means personal affinity leaks popularity -> beta = 0.15 (else 0.0).
DEFAULT_BETA = 0.15
MAX_PER_TEAM_IN_TOP3 = 1
TOP_K = 3


@dataclass(frozen=True)
class LiftedAthlete:
    rank: int  # final position, after the diversity rule
    name: str
    entity_id: str
    team: str | None
    personal_rank: int
    baseline_rank: int
    lift: int  # baseline_rank - personal_rank; positive = your taste pulled them up
    pct_personal: float
    fame: float
    final: float
    top_seed: str | None
    explainability: dict[str, float]


@dataclass(frozen=True)
class UnknownAthlete:
    entity_id: str
    name: str | None
    team: str | None
    reason: str  # missing_personal | missing_baseline | missing_both | no_score


@dataclass(frozen=True)
class TasteLiftResult:
    ranked: list[LiftedAthlete]
    unknown: list[UnknownAthlete]
    n: int  # size of the scored intersection


def score(entity: InsightEntity) -> float | None:
    """Affinity when the query had a signal; the no-signal baseline only has popularity."""
    return entity.affinity if entity.affinity is not None else entity.popularity


def _scored(entities: Iterable[InsightEntity], excluded: set[str]) -> dict[str, InsightEntity]:
    out: dict[str, InsightEntity] = {}
    for e in entities:
        if e.entity_id not in excluded and score(e) is not None and e.entity_id not in out:
            out[e.entity_id] = e
    return out


def ranks(entities: Iterable[InsightEntity]) -> dict[str, int]:
    """1-based rank by score desc; ties broken by entity_id."""
    ordered = sorted(entities, key=lambda e: (-(score(e) or 0.0), e.entity_id))
    return {e.entity_id: i for i, e in enumerate(ordered, 1)}


def percentiles(entities: Sequence[InsightEntity]) -> dict[str, float]:
    """Mid-rank percentile in [0, 1] within one query; 1.0 = highest score. Ties share a value."""
    scores = {e.entity_id: score(e) or 0.0 for e in entities}
    m = len(scores)
    if m == 1:
        return {k: 1.0 for k in scores}
    values = list(scores.values())
    return {k: (sum(v < s for v in values) + 0.5 * (sum(v == s for v in values) - 1)) / (m - 1)
            for k, s in scores.items()}


def top_seed(explainability: Mapping[str, float]) -> str | None:
    if not explainability:
        return None
    return min(explainability, key=lambda seed: (-explainability[seed], seed))


def _apply_diversity(ordered: list[LiftedAthlete], max_per_team: int) -> list[LiftedAthlete]:
    """No more than `max_per_team` athletes of one team in the top 3; the lower-scored one is pushed
    down to just below the top 3, keeping its relative order. Athletes with no team are unconstrained."""
    top: list[LiftedAthlete] = []
    pushed: list[LiftedAthlete] = []
    counts: dict[str, int] = {}
    for a in ordered:
        if len(top) == TOP_K:
            break
        if a.team is not None and counts.get(a.team, 0) >= max_per_team:
            pushed.append(a)
            continue
        top.append(a)
        if a.team is not None:
            counts[a.team] = counts.get(a.team, 0) + 1
    chosen = {a.entity_id for a in top}
    while len(top) < TOP_K and pushed:  # not enough distinct teams: fill in score order
        chosen.add(pushed[0].entity_id)
        top.append(pushed.pop(0))
    rest = [a for a in ordered if a.entity_id not in chosen]
    return top + rest


def compute_taste_lift(personal: Sequence[InsightEntity], baseline: Sequence[InsightEntity],
                       teams: Mapping[str, str | None] | None = None, *,
                       alpha: float = DEFAULT_ALPHA, beta: float = DEFAULT_BETA,
                       max_per_team_in_top3: int = MAX_PER_TEAM_IN_TOP3,
                       exclude_ids: Iterable[str] = ()) -> TasteLiftResult:
    """Score athletes present in BOTH result lists; everyone else in the candidate set is `unknown`.

    `teams` maps entity_id -> team for the whole candidate set (the roster). `exclude_ids` is the
    repair path ("I already know him"): those athletes are dropped before anything is ranked.

    final = pct_personal + alpha * lift / N - beta * fame
    """
    teams = teams or {}
    excluded = set(exclude_ids)
    pers = _scored(personal, excluded)
    base = _scored(baseline, excluded)

    pct_personal = percentiles(list(pers.values()))
    fame = percentiles(list(base.values()))

    both = [eid for eid in pers if eid in base]
    n = len(both)
    personal_rank = ranks(pers[eid] for eid in both)
    baseline_rank = ranks(base[eid] for eid in both)

    scored: list[LiftedAthlete] = []
    for eid in both:
        lift = baseline_rank[eid] - personal_rank[eid]
        final = pct_personal[eid] + alpha * lift / n - beta * fame[eid]
        scored.append(LiftedAthlete(
            rank=0, name=pers[eid].name, entity_id=eid, team=teams.get(eid),
            personal_rank=personal_rank[eid], baseline_rank=baseline_rank[eid], lift=lift,
            pct_personal=pct_personal[eid], fame=fame[eid], final=final,
            top_seed=top_seed(pers[eid].explainability),
            explainability=dict(pers[eid].explainability)))
    scored.sort(key=lambda a: (-a.final, a.entity_id))
    ordered = _apply_diversity(scored, max_per_team_in_top3)
    ranked = [LiftedAthlete(**{**a.__dict__, "rank": i}) for i, a in enumerate(ordered, 1)]

    names = {e.entity_id: e.name for e in [*personal, *baseline]}
    in_personal = {e.entity_id for e in personal}
    in_baseline = {e.entity_id for e in baseline}
    unknown = []
    for eid in sorted((set(teams) | in_personal | in_baseline) - set(both) - excluded):
        p, b = eid in in_personal, eid in in_baseline
        reason = ("no_score" if p and b else "missing_baseline" if p else
                  "missing_personal" if b else "missing_both")
        unknown.append(UnknownAthlete(entity_id=eid, name=names.get(eid), team=teams.get(eid), reason=reason))
    return TasteLiftResult(ranked=ranked, unknown=unknown, n=n)
