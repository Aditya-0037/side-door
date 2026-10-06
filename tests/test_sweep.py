import json
from pathlib import Path

import pytest

from backend.qloo.models import InsightEntity
from backend.ranking.sweep import MIN_MEAN_PERSONAL_PCT, SweepRow, choose, evaluate, sweep
from backend.ranking.taste_lift import DEFAULT_ALPHA, DEFAULT_BETA

DATA = Path(__file__).resolve().parents[1] / "data"


def ent(eid: str, affinity: float | None = None, popularity: float | None = None) -> InsightEntity:
    return InsightEntity(entity_id=eid, name=eid, affinity=affinity, popularity=popularity)


BASELINE = [ent("STAR", popularity=0.99), ent("A", popularity=0.90), ent("B", popularity=0.80),
            ent("NEW", popularity=0.50)]
PERSONAS = {
    "p1": [ent("STAR", 0.90), ent("NEW", 0.89), ent("A", 0.50), ent("B", 0.40)],  # NEW a close 2nd
    "p2": [ent("B", 0.95), ent("STAR", 0.90), ent("A", 0.50), ent("NEW", 0.40)],
    "p3": [ent("STAR", 0.90), ent("A", 0.20), ent("B", 0.15), ent("NEW", 0.10)],  # clearly STAR
}


def row(alpha, beta, distinct, share, pct, crowd=0.0) -> SweepRow:
    return SweepRow(alpha=alpha, beta=beta, distinct_top1=distinct, crowd_top3_rate=crowd,
                    max_top1_share=share, max_top1_athlete="X", mean_personal_pct_of_top1=pct)


def test_metrics_without_correction_echo_the_crowd():
    r = evaluate(PERSONAS, BASELINE, {}, alpha=0.0, beta=0.0)
    assert r.top1 == {"p1": "STAR", "p2": "B", "p3": "STAR"}
    assert (r.distinct_top1, r.crowd_top3_rate, r.max_top1_athlete) == (2, 1.0, "STAR")
    assert r.max_top1_share == pytest.approx(2 / 3)
    assert r.mean_personal_pct_of_top1 == 1.0


def test_metrics_with_lift_and_crowd_penalty():
    # p1: NEW = 2/3 + 0.5*(4-2)/4 - 0 = 0.917 beats STAR = 1 + 0 - 0.3 = 0.7
    r = evaluate(PERSONAS, BASELINE, {}, alpha=0.5, beta=0.3)
    assert r.top1 == {"p1": "NEW", "p2": "B", "p3": "STAR"}
    assert r.distinct_top1 == 3
    assert r.max_top1_share == pytest.approx(1 / 3)
    assert r.crowd_top3_rate == pytest.approx(2 / 3)  # B is crowd #3, STAR crowd #1, NEW crowd #4
    assert r.mean_personal_pct_of_top1 == pytest.approx((2 / 3 + 1 + 1) / 3)


def test_sweep_covers_the_grid():
    rows = sweep(PERSONAS, BASELINE, {}, alphas=(0.0, 0.5), betas=(0.0, 0.3))
    assert [(r.alpha, r.beta) for r in rows] == [(0.0, 0.0), (0.0, 0.3), (0.5, 0.0), (0.5, 0.3)]


def test_choose_prefers_distinct_then_low_share_within_the_pct_floor():
    rows = [row(0.0, 0.0, 3, 0.5, 1.00), row(0.5, 0.3, 9, 0.1, 0.60),  # infeasible: pct < 0.75
            row(0.3, 0.15, 6, 0.30, 0.90), row(0.15, 0.15, 6, 0.25, 0.95)]
    best = choose(rows)
    assert (best.alpha, best.beta) == (0.15, 0.15)
    with pytest.raises(ValueError):
        choose([row(0.5, 0.3, 9, 0.1, 0.60)])


def test_saved_sweep_reproduces_offline_and_records_the_adopted_defaults():
    results = json.loads((DATA / "sweep_results.json").read_text(encoding="utf-8"))
    assert (DEFAULT_ALPHA, DEFAULT_BETA) == (0.15, 0.15)
    assert (results["adopted"]["alpha"], results["adopted"]["beta"]) == (DEFAULT_ALPHA, DEFAULT_BETA)

    inputs = json.loads((DATA / "sweep_inputs.json").read_text(encoding="utf-8"))
    teams = {r["entity_id"]: r["team"] for r in inputs["roster"]}
    baseline = [InsightEntity(**e) for e in inputs["baseline"]]
    personas = {p: [InsightEntity(**e) for e in es] for p, es in inputs["personal"].items()}
    rows = sweep(personas, baseline, teams)
    assert len(personas) == results["personas"] == 20

    # The mechanical rule still prefers the grid corner; we deliberately adopt (0.15, 0.15).
    rule = choose(rows)
    assert (rule.alpha, rule.beta) == (results["rule_choice"]["alpha"], results["rule_choice"]["beta"]) == (0.5, 0.3)
    adopted = next(r for r in rows if (r.alpha, r.beta) == (DEFAULT_ALPHA, DEFAULT_BETA))
    assert adopted.mean_personal_pct_of_top1 >= MIN_MEAN_PERSONAL_PCT
    assert (adopted.distinct_top1, adopted.max_top1_share) == (6, 0.25)
    assert adopted.crowd_top3_rate == pytest.approx(0.05)
    assert adopted.mean_personal_pct_of_top1 == pytest.approx(0.956, abs=1e-3)
