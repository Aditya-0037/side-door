from backend.qloo.models import parse_insights
from backend.ranking.bias import bias_report, choose_beta
from backend.ranking.taste_lift import DEFAULT_BETA
from tests.helpers import fixture

PERSONAS = ["anime-indie", "space-pop", "dark-hiphop", "romance-pop", "rock-action"]


def test_k3_fixtures_support_the_default_beta():
    baseline = parse_insights(fixture("k3-baseline")["body"])
    personas = {p: parse_insights(fixture(f"k3-{p}")["body"]) for p in PERSONAS}
    report = bias_report(personas, baseline)

    assert [r.persona for r in report if r.top1_in_baseline_top3] == ["space-pop", "dark-hiphop"]
    assert report[1].top3[0] == "Lewis Hamilton"
    assert choose_beta(report) == DEFAULT_BETA == 0.15
