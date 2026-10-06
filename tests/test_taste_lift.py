import random

import pytest

from backend.qloo.models import InsightEntity
from backend.ranking.taste_lift import DEFAULT_BETA, compute_taste_lift, top_seed


def ent(eid: str, affinity: float | None = None, popularity: float | None = None,
        expl: dict[str, float] | None = None) -> InsightEntity:
    return InsightEntity(entity_id=eid, name=f"Athlete {eid}", affinity=affinity, popularity=popularity,
                         explainability=expl or {})


def personal(*pairs: tuple[str, float]) -> list[InsightEntity]:
    return [ent(eid, affinity=a) for eid, a in pairs]


def baseline(*pairs: tuple[str, float]) -> list[InsightEntity]:
    # The real no-signal call has no affinity -- only popularity.
    return [ent(eid, popularity=p) for eid, p in pairs]


def by_id(result):
    return {a.entity_id: a for a in result.ranked}


def test_lift_math():
    base = baseline(("A", 0.99), ("B", 0.98), ("C", 0.97), ("D", 0.96))
    pers = personal(("D", 0.9), ("C", 0.8), ("B", 0.7), ("A", 0.6))
    result = compute_taste_lift(pers, base, alpha=0.3, beta=0.0)

    assert result.n == 4
    d, a = by_id(result)["D"], by_id(result)["A"]
    assert (d.baseline_rank, d.personal_rank, d.lift) == (4, 1, 3)
    assert (a.baseline_rank, a.personal_rank, a.lift) == (1, 4, -3)
    assert d.pct_personal == 1.0 and a.pct_personal == 0.0
    assert d.fame == 0.0 and a.fame == 1.0
    assert d.final == pytest.approx(1.0 + 0.3 * 3 / 4)
    assert a.final == pytest.approx(0.0 - 0.3 * 3 / 4)
    assert [x.entity_id for x in result.ranked] == ["D", "C", "B", "A"]
    assert [x.rank for x in result.ranked] == [1, 2, 3, 4]


def test_missing_candidates_are_unknown_not_zero():
    pers = personal(("A", 0.9), ("C", 0.7), ("B", 0.5))
    base = baseline(("A", 0.9), ("B", 0.8), ("D", 0.7))
    teams = {"A": "X", "B": "Y", "C": "Z", "D": "W", "E": "V"}
    result = compute_taste_lift(pers, base, teams, beta=0.0)

    assert result.n == 2
    assert {a.entity_id for a in result.ranked} == {"A", "B"}
    assert sorted(a.personal_rank for a in result.ranked) == [1, 2]  # ranks within the intersection
    assert {(u.entity_id, u.reason) for u in result.unknown} == {
        ("C", "missing_baseline"), ("D", "missing_personal"), ("E", "missing_both")}
    # pct_personal is measured over the whole personal query: B is last of A, C, B
    assert by_id(result)["B"].pct_personal == 0.0


def test_diversity_rule_pushes_same_team_out_of_top3():
    pers = personal(("A", 0.9), ("B", 0.8), ("C", 0.7), ("D", 0.6), ("E", 0.5))
    base = baseline(("A", 0.5), ("B", 0.5), ("C", 0.5), ("D", 0.5), ("E", 0.5))
    teams = {"A": "McLaren", "B": "McLaren", "C": "Ferrari", "D": "Ferrari", "E": "Mercedes"}

    result = compute_taste_lift(pers, base, teams, alpha=0.0, beta=0.0)
    assert [a.entity_id for a in result.ranked] == ["A", "C", "E", "B", "D"]

    relaxed = compute_taste_lift(pers, base, teams, alpha=0.0, beta=0.0, max_per_team_in_top3=2)
    assert [a.entity_id for a in relaxed.ranked] == ["A", "B", "C", "D", "E"]


def test_diversity_rule_falls_back_when_teams_run_out():
    pers = personal(("A", 0.9), ("B", 0.8), ("C", 0.7), ("D", 0.6))
    base = baseline(("A", 0.5), ("B", 0.5), ("C", 0.5), ("D", 0.5))
    teams = {"A": "Ferrari", "B": "Ferrari", "C": "Ferrari", "D": "Haas"}
    result = compute_taste_lift(pers, base, teams, alpha=0.0, beta=0.0)
    assert [a.entity_id for a in result.ranked] == ["A", "D", "B", "C"]


def test_beta_penalises_fame():
    # X is personally #1 but also the most famous; Y is close behind and obscure.
    pers = personal(("X", 0.80), ("Y", 0.79), ("Z", 0.10))
    base = baseline(("X", 0.99), ("Z", 0.98), ("Y", 0.50))

    no_beta = compute_taste_lift(pers, base, alpha=0.0, beta=0.0)
    assert no_beta.ranked[0].entity_id == "X"

    with_beta = compute_taste_lift(pers, base, alpha=0.0, beta=DEFAULT_BETA)
    for eid, a in by_id(with_beta).items():
        assert a.final == pytest.approx(by_id(no_beta)[eid].final - DEFAULT_BETA * a.fame)

    strong = compute_taste_lift(pers, base, alpha=0.0, beta=0.6)
    assert strong.ranked[0].entity_id == "Y"


def test_ordering_is_deterministic_with_ties():
    pers = personal(("B", 0.5), ("A", 0.5), ("C", 0.5))
    base = baseline(("C", 0.5), ("B", 0.5), ("A", 0.5))
    first = compute_taste_lift(pers, base)
    assert [a.entity_id for a in first.ranked] == ["A", "B", "C"]  # tie-break by entity_id

    shuffled_p, shuffled_b = pers[:], base[:]
    random.Random(7).shuffle(shuffled_p)
    random.Random(8).shuffle(shuffled_b)
    assert compute_taste_lift(shuffled_p, shuffled_b) == first


def test_exclusion_repair_path():
    pers = personal(("A", 0.9), ("B", 0.8), ("C", 0.7))
    base = baseline(("A", 0.9), ("B", 0.8), ("C", 0.7))
    teams = {"A": "X", "B": "Y", "C": "Z"}

    result = compute_taste_lift(pers, base, teams, exclude_ids={"A"})
    assert result.n == 2
    assert [a.entity_id for a in result.ranked] == ["B", "C"]
    assert by_id(result)["B"].personal_rank == 1 and by_id(result)["B"].baseline_rank == 1
    assert all(u.entity_id != "A" for u in result.unknown)  # excluded is not "unknown"


def test_top_seed_from_explainability():
    assert top_seed({"s1": 0.2, "s2": 0.5, "s3": 0.3}) == "s2"
    assert top_seed({"s9": 0.4, "s1": 0.4}) == "s1"  # tie -> smallest id
    assert top_seed({}) is None

    pers = [ent("A", affinity=0.9, expl={"seed-1": 0.1, "seed-2": 0.6}), ent("B", affinity=0.5)]
    base = baseline(("A", 0.9), ("B", 0.8))
    result = by_id(compute_taste_lift(pers, base))
    assert result["A"].top_seed == "seed-2"
    assert result["A"].explainability == {"seed-1": 0.1, "seed-2": 0.6}
    assert result["B"].top_seed is None
