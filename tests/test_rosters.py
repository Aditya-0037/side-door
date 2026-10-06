from backend.inventory.rosters import f1_query_names, name_tokens, names_match, pick_candidate, sport_evidence
from backend.qloo.models import SearchResult, parse_search
from tests.helpers import fixture


def person(eid: str, name: str, sport: list[str] | None = None, desc: str = "") -> SearchResult:
    return SearchResult(entity_id=eid, name=name, types=["urn:entity:person"],
                        properties={"sport": sport or [], "short_description": desc})


def test_name_tokens_are_accent_insensitive():
    assert name_tokens("Martin Ødegaard") == ("martin", "odegaard")
    assert name_tokens("Nico Hülkenberg") == ("nico", "hulkenberg")
    assert name_tokens("Carlos Sainz Jr.") == name_tokens("Carlos Sainz")


def test_names_match():
    assert names_match("Martin Odegaard", "Martin Ødegaard")
    assert names_match("Andrea Kimi Antonelli", "Kimi Antonelli")
    assert not names_match("Kimi Antonelli", "Giovanna Antonelli")
    assert not names_match("Rodri", "Rodrigo Hernández")


def test_sport_evidence_from_real_fixture():
    driver, puppeteer = parse_search(fixture("k1-max-verstappen")["body"])[:2]
    assert sport_evidence(driver, "f1") == (2, "properties.sport: Formula One")
    assert sport_evidence(puppeteer, "f1") is None
    assert sport_evidence(driver, "pl") is None


def test_pick_accepts_right_person_from_real_fixtures():
    verstappen = pick_candidate("Max Verstappen", "f1", parse_search(fixture("k1-max-verstappen")["body"]))
    assert verstappen.status == "auto"
    assert verstappen.result.disambiguation == "1997-09-30"

    saka = pick_candidate("Bukayo Saka", "pl", parse_search(fixture("k1-bukayo-saka")["body"]))
    assert saka.status == "auto" and "association football" in saka.evidence


def test_pick_rejects_the_day1_antonelli_actress():
    results = parse_search(fixture("k1-kimi-antonelli")["body"])
    assert results[0].name == "Giovanna Antonelli"
    assert pick_candidate(["Andrea Kimi Antonelli", "Kimi Antonelli"], "f1", results).status == "none"


def test_pick_never_guesses_between_two_strong_matches():
    results = [person("1", "Alex Driver", ["Formula One"]), person("2", "Alex Driver", ["Formula One"])]
    assert pick_candidate("Alex Driver", "f1", results).status == "ambiguous"


def test_strong_evidence_beats_generic_racing_driver():
    results = [person("SR", "Carlos Sainz", desc="Spanish rally and racing driver"),
               person("JR", "Carlos Sainz Jr.", ["Formula One"])]
    pick = pick_candidate("Carlos Sainz", "f1", results)
    assert pick.status == "auto" and pick.result.entity_id == "JR"


def test_f1_query_names():
    assert f1_query_names("Andrea Kimi", "Antonelli") == [
        "Andrea Kimi Antonelli", "Kimi Antonelli", "Andrea Antonelli"]
    assert f1_query_names("Carlos", "Sainz") == ["Carlos Sainz", "Carlos Sainz Jr."]
