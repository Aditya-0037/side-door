from datetime import date

from backend.inventory.rosters import (
    Expected, club_evidence, club_tokens, dob_evidence, f1_query_names, name_tokens, names_match, pick_candidate,
    sport_evidence,
)
from backend.qloo.models import SearchResult, parse_search
from tests.helpers import fixture

TODAY = date(2026, 10, 6)


def results(name: str) -> list[SearchResult]:
    return parse_search(fixture(name)["body"])


def person(eid: str, name: str, **props) -> SearchResult:
    return SearchResult(entity_id=eid, name=name, types=["urn:entity:person"], properties=props)


def test_name_tokens_are_accent_insensitive():
    assert name_tokens("Martin Ødegaard") == ("martin", "odegaard")
    assert name_tokens("Nico Hülkenberg") == ("nico", "hulkenberg")
    assert name_tokens("Carlos Sainz Jr.") == name_tokens("Carlos Sainz")


def test_names_match():
    assert names_match("Martin Odegaard", "Martin Ødegaard")
    assert names_match("Andrea Kimi Antonelli", "Kimi Antonelli")
    assert not names_match("Kimi Antonelli", "Giovanna Antonelli")
    assert not names_match("Rodri", "Rodrigo Hernández")


def test_club_tokens_and_match():
    assert club_tokens("Manchester United F.C.") == {"manchester", "united"}
    assert club_tokens("Scuderia Ferrari") == {"ferrari"}
    assert club_tokens("Haas F1 Team") == {"haas"}
    united = person("1", "X", member_of_sports_team=["Manchester United F.C."])
    assert club_evidence(united, "Manchester United") == "member_of_sports_team: Manchester United F.C."
    assert club_evidence(united, "Manchester City") is None
    brighton = person("2", "Y", member_of_sports_team=["Brighton & Hove Albion F.C."])
    assert club_evidence(brighton, "Brighton & Hove Albion")


def test_sport_evidence_from_real_fixture():
    driver, puppeteer = results("k1-max-verstappen")[:2]
    assert sport_evidence(driver, "f1") == "properties.sport: Formula One"
    assert sport_evidence(puppeteer, "f1") is None
    assert sport_evidence(driver, "pl") is None


def test_dob_evidence_exact_or_plausible_age():
    r = person("1", "X", date_of_birth="2001-01-17")
    assert dob_evidence(r, "2001-01-17", TODAY)
    assert dob_evidence(r, "2001-01-18", TODAY) is None  # the source has an exact DOB: must match
    assert dob_evidence(r, None, TODAY) == "date_of_birth: 2001-01-17 (age ~25)"
    assert dob_evidence(person("2", "Y", date_of_birth="1916-04-09"), None, TODAY) is None


def test_tier1_current_club_in_team_list():
    pick = pick_candidate(Expected(names=["Bukayo Saka"], team="Arsenal"), "pl", results("k1-bukayo-saka"), TODAY)
    assert (pick.status, pick.tier, pick.evidence) == ("auto", 1, "member_of_sports_team: Arsenal F.C.")


def test_tier2_sport_nationality_and_exact_dob():
    # Qloo's team list for Hamilton has no Ferrari yet, so tier 1 fails and tier 2 must carry it.
    expected = Expected(names=["Lewis Hamilton"], team="Ferrari", nationality="British", dob="1985-01-07")
    pick = pick_candidate(expected, "f1", results("k1-lewis-hamilton"), TODAY)
    assert (pick.status, pick.tier) == ("auto", 2)
    assert "citizenship: United Kingdom" in pick.evidence and "(= source)" in pick.evidence

    wrong_dob = Expected(names=["Lewis Hamilton"], team="Ferrari", nationality="British", dob="1985-01-08")
    assert pick_candidate(wrong_dob, "f1", results("k1-lewis-hamilton"), TODAY).status == "none"


def test_exact_dob_separates_sainz_from_his_father():
    expected = Expected(names=f1_query_names("Carlos", "Sainz"), team="Williams", nationality="Spanish",
                        dob="1994-09-01")
    pick = pick_candidate(expected, "f1", results("search-carlos-sainz"), TODAY)
    assert pick.status == "auto" and pick.result.name == "Carlos Sainz Jr."


def test_enzo_resolved_by_nationality_never_by_popularity():
    candidates = results("search-enzo-fernandez")
    pick = pick_candidate(Expected(names=["Enzo Fernández"], team="Manchester City", nationality="Argentine"),
                          "pl", candidates, TODAY)
    assert (pick.status, pick.tier) == ("auto", 2)
    assert pick.result.entity_id == "5592F57C-1E99-42D0-BB2F-DD452736A212"
    most_popular = max(candidates, key=lambda r: r.popularity or 0)
    assert most_popular.entity_id != pick.result.entity_id  # the popular namesake is the wrong man


def test_salah_resolved_by_club():
    pick = pick_candidate(Expected(names=["Mohamed Salah"], team="Liverpool", nationality="Egyptian"),
                          "pl", results("search-mohamed-salah"), TODAY)
    assert (pick.status, pick.tier) == ("auto", 1)
    assert pick.result.entity_id == "C5E59A12-DB26-438F-90D8-565F45D02596"


def test_real_namesakes_at_tier2_go_to_review():
    # Two footballers called Morgan Rogers, both born July 2002 with UK citizenship: one English
    # (the Chelsea player), one a Welsh women's international. Tier 2 can't separate them.
    expected = Expected(names=["Morgan Rogers"], team="Chelsea", nationality="English")
    assert pick_candidate(expected, "pl", results("search-morgan-rogers"), TODAY).status == "ambiguous"


def test_two_candidates_at_the_best_tier_is_ambiguous():
    twins = [person(i, "Alex Driver", sport=["Formula One"], citizenship=["Italy"], date_of_birth="2000-01-01")
             for i in ("1", "2")]
    expected = Expected(names=["Alex Driver"], team="Ferrari", nationality="Italian")
    assert pick_candidate(expected, "f1", twins, TODAY).status == "ambiguous"


def test_without_nationality_tier2_cannot_pass():
    expected = Expected(names=["Max Verstappen"], team="Ferrari")
    assert pick_candidate(expected, "f1", results("k1-max-verstappen"), TODAY).status == "none"


def test_day1_antonelli_actress_is_rejected():
    expected = Expected(names=["Andrea Kimi Antonelli", "Kimi Antonelli"], team="Mercedes",
                        nationality="Italian", dob="2006-08-25")
    candidates = results("k1-kimi-antonelli")
    assert candidates[0].name == "Giovanna Antonelli"
    assert pick_candidate(expected, "f1", candidates, TODAY).status == "none"


def test_f1_query_names():
    assert f1_query_names("Andrea Kimi", "Antonelli") == [
        "Andrea Kimi Antonelli", "Kimi Antonelli", "Andrea Antonelli"]
    assert f1_query_names("Carlos", "Sainz") == ["Carlos Sainz", "Carlos Sainz Jr."]
