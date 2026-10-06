"""Taste Lift: rank the same athletes with and without the user's taste signal, and score the jump.

Pure functions, no I/O. Raw affinity is normalized per query, so it is never compared across two
requests -- only ranks and percentiles are. "Crowd" is the no-signal baseline: Qloo popularity,
not real-world fame. Athletes without data are returned as `unknown`, never given a fake rank.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from backend.qloo.models import InsightEntity

# Adopted after the parameter sweep (scripts/param_sweep.py -> data/sweep_results.json; 20 personas
# over the verified 2026 F1 roster, alpha {0, .15, .3, .5} x beta {0, .15, .3}). Uncorrected (0, 0),
# Lewis Hamilton -- the crowd #1 -- is the #1 pick for 55% of personas. The sweep's mechanical rule
# picked (0.5, 0.3), but we use (0.15, 0.15): (0.5, 0.3) is a grid corner (overfitting risk on 20
# personas), creates a new "magnet" (Bottas is #1 for 30%), and promotes personal #5 athletes, which
# feels random to a real user; its 7 vs 6 distinct #1 picks is one persona, i.e. noise. (0.15, 0.15)
# keeps the pick at the 95.6th personal percentile on average with only 5% crowd-top-3 picks.
DEFAULT_ALPHA = 0.15
DEFAULT_BETA = 0.15
MAX_PER_TEAM_IN_TOP3 = 1
TOP_K = 3
# A seed is called "distinctive" only if it lifts this athlete >= 5% more than its average lift.
# In the sweep the best ratio had median 1.027, so most athletes get no distinctive seed at all.
DISTINCTIVE_MIN_RATIO = 1.05
NO_DATA_TITLE = "New on the grid — not enough taste data yet"


@dataclass(frozen=True)
class LiftedAthlete:
    rank: int  # final position, after the diversity rule
    name: str
    entity_id: str
    team: str | None
    personal_rank: int
    crowd_rank: int  # rank in the no-signal baseline (Qloo popularity)
    lift: int  # crowd_rank - personal_rank; positive = your taste pulled them up
    pct_personal: float
    crowd_pct: float  # 1.0 = the crowd's favourite
    final: float
    distinctive_seed: str | None  # set only when distinctive_ratio >= DISTINCTIVE_MIN_RATIO
    distinctive_ratio: float | None  # best seed score / that seed's mean score across athletes
    seed_shares: dict[str, float]  # each seed's share of this athlete's explainability (sums to 1)
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

    def to_payload(self, seed_names: Mapping[str, str],
                   no_data_title: str = NO_DATA_TITLE) -> dict[str, Any]:
        """JSON-ready result for the UI: the ranking with a factual `why_line` per athlete, plus the
        no-data athletes as their own section (they have no rank, on purpose). `distinctive_seed`
        is omitted unless it clears DISTINCTIVE_MIN_RATIO."""
        ranked = []
        for a in self.ranked:
            item = asdict(a)
            item["why_line"] = why_line(a, self.n, list(seed_names.values()))
            if a.distinctive_seed is None:
                del item["distinctive_seed"]
            else:
                item["distinctive_seed_name"] = seed_names.get(a.distinctive_seed, a.distinctive_seed)
            ranked.append(item)
        return {"n": self.n, "seeds": dict(seed_names), "ranked": ranked,
                "no_data": {"title": no_data_title, "athletes": [asdict(u) for u in self.unknown]}}


def why_line(athlete: LiftedAthlete, n: int, seed_names: Sequence[str]) -> str:
    """Only facts we have: ranks among people with overlapping taste vs the crowd. It never claims
    that one seed caused the pick (the per-seed contributions are near-uniform)."""
    return (f"People whose taste overlaps yours ({', '.join(seed_names)}) rank {athlete.name} "
            f"#{athlete.personal_rank} of {n}; the crowd ranks them #{athlete.crowd_rank}.")


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


def seed_means(entities: Iterable[InsightEntity]) -> dict[str, float]:
    """Mean explainability score of each seed across the returned athletes that carry
    explainability (a seed missing from one athlete's breakdown counts as 0 there)."""
    with_expl = [e.explainability for e in entities if e.explainability]
    seeds = {s for expl in with_expl for s in expl}
    return {s: sum(expl.get(s, 0.0) for expl in with_expl) / len(with_expl) for s in seeds}


def best_seed_ratio(explainability: Mapping[str, float],
                    means: Mapping[str, float]) -> tuple[str, float] | None:
    """argmax over seeds of score / mean score of that seed. A seed that lifts everyone (say,
    Valorant for every driver) stops winning by default. Ties -> smallest seed id."""
    ratios = {s: v / means[s] for s, v in explainability.items() if means.get(s, 0.0) > 0}
    if not ratios:
        return None
    seed = min(ratios, key=lambda s: (-ratios[s], s))
    return seed, ratios[seed]


def distinctive_seed(explainability: Mapping[str, float], means: Mapping[str, float],
                     min_ratio: float = DISTINCTIVE_MIN_RATIO) -> str | None:
    best = best_seed_ratio(explainability, means)
    return best[0] if best and best[1] >= min_ratio else None


def seed_shares(explainability: Mapping[str, float]) -> dict[str, float]:
    total = sum(explainability.values())
    if total <= 0:
        return {}
    return {s: v / total for s, v in explainability.items()}


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

    final = pct_personal + alpha * lift / N - beta * crowd_pct
    """
    teams = teams or {}
    excluded = set(exclude_ids)
    pers = _scored(personal, excluded)
    base = _scored(baseline, excluded)

    pct_personal = percentiles(list(pers.values()))
    crowd_pct = percentiles(list(base.values()))
    means = seed_means(pers.values())

    both = [eid for eid in pers if eid in base]
    n = len(both)
    personal_rank = ranks(pers[eid] for eid in both)
    crowd_rank = ranks(base[eid] for eid in both)

    scored: list[LiftedAthlete] = []
    for eid in both:
        expl = pers[eid].explainability
        lift = crowd_rank[eid] - personal_rank[eid]
        scored.append(LiftedAthlete(
            rank=0, name=pers[eid].name, entity_id=eid, team=teams.get(eid),
            personal_rank=personal_rank[eid], crowd_rank=crowd_rank[eid], lift=lift,
            pct_personal=pct_personal[eid], crowd_pct=crowd_pct[eid],
            final=pct_personal[eid] + alpha * lift / n - beta * crowd_pct[eid],
            distinctive_seed=distinctive_seed(expl, means),
            distinctive_ratio=(best[1] if (best := best_seed_ratio(expl, means)) else None),
            seed_shares=seed_shares(expl), explainability=dict(expl)))
    scored.sort(key=lambda a: (-a.final, a.entity_id))
    ordered = _apply_diversity(scored, max_per_team_in_top3)
    ranked = [replace(a, rank=i) for i, a in enumerate(ordered, 1)]

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
