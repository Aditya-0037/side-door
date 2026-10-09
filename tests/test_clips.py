import httpx
import pytest

from backend.inventory.clips import (
    clips_for, match_athlete, uploads_embed_url, verify_uploads_playlist, wikimedia_user_agent,
)
from backend.inventory.rosters import RosterAthlete
from tests.fakes import MEDIA

CHANNEL = "UCB_qr75-ydFVKSF9Dmo6izg"


def test_uploads_embed_url_from_channel_id():
    assert uploads_embed_url(CHANNEL) == "https://www.youtube.com/embed/videoseries?list=UUB_qr75-ydFVKSF9Dmo6izg"
    assert uploads_embed_url("UUB_qr75-ydFVKSF9Dmo6izg") is None  # only channel ids are accepted
    assert uploads_embed_url("UCshort") is None
    assert uploads_embed_url("") is None


def test_clips_prefer_the_athlete_channel_then_the_league():
    own = clips_for("D1", "f1", MEDIA, "Driver One")
    assert [c.kind for c in own] == ["athlete_channel", "website"]
    assert own[0].embed_url.endswith("list=UU" + "a" * 22)
    assert own[1].url == "https://driver-one.example" and own[1].embed_url is None

    league = clips_for("D2", "f1", MEDIA, "Driver Two")
    assert [(c.kind, c.label) for c in league] == [("league_channel", "Formula 1 on YouTube")]
    assert clips_for("P1", "pl", MEDIA) == []  # no channel known anywhere: nothing invented


def test_wikimedia_requires_contact(monkeypatch):
    monkeypatch.delenv("WIKIMEDIA_CONTACT", raising=False)
    with pytest.raises(RuntimeError, match="WIKIMEDIA_CONTACT"):
        wikimedia_user_agent()
    monkeypatch.setenv("WIKIMEDIA_CONTACT", "https://example.org/side-door")
    assert "(https://example.org/side-door)" in wikimedia_user_agent()


def test_oembed_check_distinguishes_real_from_bogus_playlists():
    def fake_get(url, params=None, **kwargs):
        real = params["url"].endswith("list=UU" + CHANNEL[2:])
        req = httpx.Request("GET", url)
        return (httpx.Response(200, json={"title": "Uploads from FORMULA 1"}, request=req) if real
                else httpx.Response(404, text="Not Found", request=req))

    assert verify_uploads_playlist(CHANNEL, fake_get) == "Uploads from FORMULA 1"
    assert verify_uploads_playlist("UC" + "x" * 22, fake_get) is None


class FakeWikidata:
    def __init__(self, entities):
        self._entities = entities

    def search(self, name, limit=7):
        return list(self._entities)

    def entities(self, ids):
        return {i: self._entities[i] for i in ids}


def entity(dob, channel=None, site=None):
    def claim(value):
        return [{"mainsnak": {"datavalue": {"value": value}}}]
    claims = {"P569": claim({"time": f"+{dob}T00:00:00Z"})}
    if channel:
        claims["P2397"] = claim(channel)
    if site:
        claims["P856"] = claim(site)
    return {"claims": claims}


def test_wikidata_match_needs_the_exact_date_of_birth():
    athlete = RosterAthlete(sport="pl", name="Morgan Rogers", team="Chelsea", qloo_id="X", verified="manual",
                            dob="2002-07-26")
    wd = FakeWikidata({"Q1": entity("2002-07-16", channel="UC" + "w" * 22),  # the namesake
                       "Q2": entity("2002-07-26", site="https://example.org")})
    rec = match_athlete(wd, athlete)
    assert rec["wikidata_id"] == "Q2" and rec["youtube_channel_id"] is None
    assert rec["official_website"] == "https://example.org"

    twins = FakeWikidata({"Q1": entity("2002-07-26"), "Q2": entity("2002-07-26")})
    assert match_athlete(twins, athlete)["match"] == "ambiguous"
    no_dob = RosterAthlete(sport="pl", name="X", team="Y", qloo_id="Z", verified="auto")
    assert match_athlete(wd, no_dob)["match"] == "no_dob"
