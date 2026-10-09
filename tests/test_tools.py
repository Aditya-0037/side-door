from datetime import datetime

import pytest

from backend.inventory.events import in_window
from tests.fakes import FakeLLM, make_tools

SEED_IDS = ["S1", "S2", "S3"]


def ids(ranking):
    return [a.entity_id for a in ranking.ranked]


def test_resolve_tastes_reports_what_each_text_matched(tmp_path):
    tools = make_tools(tmp_path)
    seeds = tools.resolve_tastes([{"text": "pride and prejudice", "type": "movie"},
                                  {"text": "nothing at all", "type": "artist"}])
    assert seeds[0].found and seeds[0].matched_as == "Pride And Prejudice — film, 2005"
    assert not seeds[1].found and seeds[1].entity_id is None
    assert tools.seed_name(seeds[0].entity_id) == "Pride And Prejudice"  # remembered for why-lines
    with pytest.raises(ValueError):
        tools.resolve_tastes([{"text": "x", "type": "book"}])


def test_rank_candidates_featured_alternates_and_unknown(tmp_path):
    tools = make_tools(tmp_path)
    ranking = tools.rank_candidates(SEED_IDS, "f1")
    assert ids(ranking)[:3] == ["D1", "D3", "D4"]  # D2 shares D1's team, so it's pushed below the top 3
    assert [u.entity_id for u in ranking.result.unknown] == ["D5"]
    assert ranking.qloo_calls == 2 and ranking.seeds == {"S1": "Spirited Away", "S2": "Arijit Singh", "S3": "Valorant"}


def test_explain_pick_is_factual_and_omits_weak_distinctive_seed(tmp_path):
    tools = make_tools(tmp_path)
    ranking = tools.rank_candidates(SEED_IDS, "f1")
    out = tools.explain_pick(ranking.ranked[0], ranking.result.n, ranking.seeds)
    assert out == {"why_line": "People whose taste overlaps yours (Spirited Away, Arijit Singh, Valorant) "
                               "rank Driver One #1 of 4; the crowd ranks them #1."}


def test_not_feeling_it_never_returns_the_rejected_athlete(tmp_path):
    tools = make_tools(tmp_path)
    swap = tools.swap_pick(SEED_IDS, "f1", ["D1"], "not_feeling_it")
    assert "D1" not in ids(swap.ranking) and swap.featured_id == "D2"
    assert swap.what_changed.startswith("Swapped out Driver One. Next for your taste: Driver Two, #1 of 3")


def test_unknown_rejected_ids_are_an_error_not_a_silent_no_op(tmp_path):
    with pytest.raises(ValueError, match="not on the Formula 1 roster"):
        make_tools(tmp_path).swap_pick(SEED_IDS, "f1", ["NOT-A-DRIVER"], "not_feeling_it")


def test_bad_time_without_sport_switch_says_nothing_fits(tmp_path):
    tools = make_tools(tmp_path)  # F1 sessions are Sun 02:30 / Mon 01:30 IST: never "evenings"
    swap = tools.swap_pick(SEED_IDS, "f1", ["D1"], "bad_time", window="evenings")
    assert swap.sport == "f1" and swap.featured_id != "D1"
    assert "catch the highlights" in swap.what_changed


def test_bad_time_picks_an_event_inside_the_window(tmp_path):
    tools = make_tools(tmp_path)
    late = tools.swap_pick(SEED_IDS, "f1", ["D1"], "bad_time", window="late_night_ok")
    event = tools.next_watchable_event(late.featured_id, "f1", "late_night_ok")
    assert event["fits_window"] and in_window(datetime.fromisoformat(event["start_utc"]), "late_night_ok")

    switched = tools.swap_pick(SEED_IDS, "f1", ["D1"], "bad_time", window="evenings", allow_sport_switch=True)
    assert switched.sport == "pl"
    event = tools.next_watchable_event(switched.featured_id, "pl", "evenings")
    assert event["fits_window"] and event["title"] == "Arsenal vs Chelsea"
    assert "Premier League pick" in switched.what_changed

    pl = tools.swap_pick(SEED_IDS, "pl", ["P2"], "bad_time", window="evenings")
    assert pl.featured_id == "P3" and "P2" not in ids(pl.ranking)  # Chelsea plays in the Sat 19:30 IST match


def test_llm_only_rank_validates_against_the_roster_and_caches(tmp_path):
    llm = FakeLLM()
    tools = make_tools(tmp_path, llm)
    out = tools.llm_only_rank(["Spirited Away", "Arijit Singh", "Valorant"], "f1")
    assert out["available"] and out["featured"] == "Driver Three"
    assert out["alternates"] == ["Driver One"]  # matched case-insensitively
    assert out["dropped"] == ["Ayrton Senna"]  # not on the roster
    again = tools.llm_only_rank(["Spirited Away", "Arijit Singh", "Valorant"], "f1")
    assert again["cached"] and llm.calls == 1


def test_llm_failure_is_reported_not_raised(tmp_path):
    class ApiError(Exception):
        code = 401

    tools = make_tools(tmp_path, FakeLLM(error=ApiError("bad credentials for key sk-abc")))
    out = tools.llm_only_rank(["Rocky"], "pl")
    assert not out["available"] and out["error"] == "ApiError 401"  # no message text echoed
    assert not make_tools(tmp_path / "x").llm_only_rank(["Rocky"], "pl")["available"]  # no LLM configured
