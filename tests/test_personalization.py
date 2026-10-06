import json
from pathlib import Path

import pytest

from backend.qloo.models import InsightEntity
from backend.ranking.personalization import personalization_strength, spearman, verdict

DATA = Path(__file__).resolve().parents[1] / "data"


def people(scores: dict[str, float]) -> list[InsightEntity]:
    return [InsightEntity(entity_id=k, name=k, affinity=v) for k, v in scores.items()]


CROWD = [InsightEntity(entity_id=k, name=k, popularity=p) for k, p in
         {"A": 0.9, "B": 0.8, "C": 0.7, "D": 0.6}.items()]


def test_spearman_basics():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 2, 2, 3], [1, 2, 3, 4]) == pytest.approx(0.9486833, abs=1e-6)  # ties share a rank


def test_verdict_thresholds():
    assert verdict(0.9).startswith("weak")
    assert verdict(0.85).startswith("moderate") and verdict(0.6).startswith("moderate")
    assert verdict(0.59).startswith("strong")


def test_identical_personas_mean_a_weak_signal():
    same = {"A": 0.9, "B": 0.8, "C": 0.7, "D": 0.6}
    s = personalization_strength({"p1": people(same), "p2": people(same), "p3": people(same)}, CROWD)
    assert s.persona_persona_mean == pytest.approx(1.0)
    assert s.persona_crowd_mean == pytest.approx(1.0)  # ...and it is just the crowd order
    assert s.verdict.startswith("weak")
    assert s.top3_counts == {"A": 3, "B": 3, "C": 3, "D": 0}


def test_only_athletes_common_to_all_personas_are_compared():
    s = personalization_strength({
        "p1": people({"A": 0.9, "B": 0.8, "C": 0.7, "D": 0.6}),
        "p2": people({"D": 0.9, "C": 0.8, "B": 0.7}),  # A missing for p2
    }, CROWD)
    assert s.common_athletes == 3
    assert s.persona_persona_mean == pytest.approx(-1.0)
    assert s.verdict.startswith("strong")


def test_saved_report_matches_the_cached_sweep_data():
    report = json.loads((DATA / "personalization_strength.json").read_text(encoding="utf-8"))
    inputs = json.loads((DATA / "sweep_inputs.json").read_text(encoding="utf-8"))
    baseline = [InsightEntity(**e) for e in inputs["baseline"]]
    personas = {p: [InsightEntity(**e) for e in es] for p, es in inputs["personal"].items()}
    s = personalization_strength(personas, baseline)
    assert s.persona_persona_mean == pytest.approx(report["persona_persona_mean"])
    assert s.top3_counts == report["top3_counts"]
    assert sum(s.top3_counts.values()) == 3 * len(personas)
