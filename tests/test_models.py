import pytest

from backend.qloo.models import parse_insights, parse_search
from tests.helpers import fixture


def test_search_fixture_parses_real_shape():
    results = parse_search(fixture("k1-max-verstappen")["body"])
    assert len(results) == 5
    top = results[0]
    assert top.name == "Max Verstappen"
    assert top.types == ["urn:entity:person"]
    assert top.disambiguation == "1997-09-30"  # a date of birth, not a description
    assert "Formula One" in top.sports
    assert top.short_description.startswith("Belgian-Dutch racing driver")
    # /search tags carry `tag_id`; the model normalises it to `id`
    assert any(t.id == "urn:tag:occupation:person:formula_one_driver" for t in top.tags)


def test_search_optional_fields_may_be_missing():
    namesake = parse_search(fixture("k1-max-verstappen")["body"])[1]
    assert namesake.popularity is None
    assert namesake.sports == []


def test_k2_insights_affinity_and_explainability():
    fx = fixture("k2-insights")
    entities = parse_insights(fx["body"])
    seeds = set(fx["params"]["signal.interests.entities"].split(","))

    assert len(entities) == 18
    assert all(e.affinity is not None and 0 <= e.affinity <= 1 for e in entities)
    first = entities[0]
    assert first.name == "Yuki Tsunoda"
    assert first.subtype == "urn:entity:person"
    assert first.affinity == pytest.approx(0.7556584135934045)
    assert first.explainability == pytest.approx({
        "9B33A620-68CA-49B3-BFCF-F09CCF8AD595": 0.3715101274076677,
        "38AE774E-F217-4C96-BE77-B3B40A6344E4": 0.3569726759074054,
        "924C8504-4BD8-4615-AB2D-1C16880A8A62": 0.27151719668492685,
    })
    assert first.tags and first.tags[0].id.startswith("urn:tag:")
    assert all(set(e.explainability) <= seeds for e in entities)


def test_k3_persona_has_affinity_but_no_explainability():
    entities = parse_insights(fixture("k3-anime-indie")["body"])
    assert all(e.affinity is not None for e in entities)
    assert all(e.explainability == {} for e in entities)  # K3 ran without feature.explainability


def test_k3_baseline_has_no_affinity_and_is_sorted_by_popularity():
    entities = parse_insights(fixture("k3-baseline")["body"])
    assert len(entities) == 18
    assert all(e.affinity is None for e in entities)
    pops = [e.popularity for e in entities]
    assert pops == sorted(pops, reverse=True)
