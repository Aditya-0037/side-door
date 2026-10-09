from datetime import UTC, datetime

import httpx

import backend.inventory.events as events
from backend.inventory.events import (
    HIGHLIGHTS, EventStore, in_window, ist_label, next_f1_event, next_pl_event, parse_f1_schedule, parse_pl_matches,
    to_ist,
)
from tests.fakes import F1_WEEKENDS, NOW, PL_MATCHES


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_ist_conversion_rolls_over_midnight():
    race = utc(2026, 10, 25, 20, 0)  # Sunday 20:00 UTC
    t = to_ist(race)
    assert (t.day, t.hour, t.minute, t.strftime("%a")) == (26, 1, 30, "Mon")
    assert ist_label(race) == "Mon 26 Oct, 01:30 IST"
    assert ist_label(utc(2026, 10, 11, 12, 0)) == "Sun 11 Oct, 17:30 IST"  # Singapore GP race


def test_window_presets():
    def ist(day: int, hour: int, minute: int) -> datetime:  # takes UTC; comments give the IST time
        return utc(2026, 10, day, hour, minute)
    # evenings: weekdays 19:00-23:59 IST, weekends 12:00-23:59 IST
    assert in_window(ist(26, 13, 30), "evenings")          # Mon 19:00 IST
    assert not in_window(ist(26, 13, 29), "evenings")      # Mon 18:59 IST
    assert in_window(ist(25, 6, 30), "evenings")           # Sun 12:00 IST
    assert not in_window(ist(25, 6, 29), "evenings")       # Sun 11:59 IST
    assert not in_window(ist(25, 20, 0), "evenings")       # Mon 01:30 IST
    # late_night_ok: any day 17:00-02:59 IST
    assert in_window(ist(25, 20, 0), "late_night_ok")      # 01:30 IST
    assert in_window(ist(25, 21, 29), "late_night_ok")     # 02:59 IST
    assert not in_window(ist(25, 21, 30), "late_night_ok")  # 03:00 IST
    assert in_window(ist(26, 11, 30), "late_night_ok")     # 17:00 IST
    assert not in_window(ist(26, 11, 29), "late_night_ok")  # 16:59 IST
    # weekends_only: Saturday/Sunday IST, any time
    assert in_window(ist(24, 21, 0), "weekends_only")      # Sun 02:30 IST
    assert not in_window(ist(25, 20, 0), "weekends_only")  # Mon 01:30 IST


def test_parse_jolpica_schedule_keeps_race_qualifying_sprint():
    data = {"MRData": {"RaceTable": {"Races": [{
        "raceName": "Singapore Grand Prix", "date": "2026-10-11", "time": "12:00:00Z",
        "FirstPractice": {"date": "2026-10-09", "time": "08:30:00Z"},
        "Qualifying": {"date": "2026-10-10", "time": "13:00:00Z"},
        "Sprint": {"date": "2026-10-10", "time": "09:00:00Z"}}]}}}
    (weekend,) = parse_f1_schedule(data)
    assert [s.kind for s in weekend] == ["sprint", "qualifying", "race"]  # practice is not watchable
    assert weekend[-1].title == "Singapore Grand Prix — Race"


def test_f1_prefers_the_race_when_it_fits():
    data = {"MRData": {"RaceTable": {"Races": [{
        "raceName": "Singapore Grand Prix", "date": "2026-10-11", "time": "12:00:00Z",
        "Qualifying": {"date": "2026-10-10", "time": "13:00:00Z"},
        "Sprint": {"date": "2026-10-10", "time": "09:00:00Z"}}]}}}
    pick = next_f1_event(parse_f1_schedule(data), "evenings", utc(2026, 10, 9))
    assert pick.fits_window and pick.event.kind == "race"
    assert pick.to_dict()["label_ist"] == "Sun 11 Oct, 17:30 IST"


def test_f1_falls_back_to_qualifying_then_to_highlights():
    weekends_only = next_f1_event(F1_WEEKENDS, "weekends_only", NOW)
    assert weekends_only.fits_window and weekends_only.event.kind == "qualifying"  # race is Monday IST
    assert in_window(weekends_only.event.start_utc, "weekends_only")

    late = next_f1_event(F1_WEEKENDS, "late_night_ok", NOW)
    assert late.fits_window and late.event.kind == "race"

    evenings = next_f1_event(F1_WEEKENDS, "evenings", NOW)  # nothing fits in 14 days
    assert not evenings.fits_window and evenings.suggestion == HIGHLIGHTS
    assert evenings.event.title == "United States Grand Prix — Race"  # the real next race, not a fake time


def test_pl_next_fixture_for_the_club_in_the_window():
    arsenal = next_pl_event(PL_MATCHES, "Arsenal", "evenings", NOW)
    assert arsenal.fits_window and arsenal.event.title == "Arsenal vs Chelsea"
    liverpool = next_pl_event(PL_MATCHES, "Liverpool", "evenings", NOW)  # Tue 10:30 IST
    assert not liverpool.fits_window and liverpool.suggestion == HIGHLIGHTS
    assert next_pl_event(PL_MATCHES, "Manchester City", "evenings", NOW) is None
    assert next_pl_event(PL_MATCHES, "Manchester United", "evenings", NOW) is None


def test_parse_football_data_keeps_only_upcoming():
    data = {"matches": [
        {"status": "FINISHED", "utcDate": "2026-10-01T14:00:00Z",
         "homeTeam": {"name": "Arsenal FC", "shortName": "Arsenal"}, "awayTeam": {"name": "Chelsea FC", "shortName": "Chelsea"}},
        {"status": "TIMED", "utcDate": "2026-10-24T14:00:00Z",
         "homeTeam": {"name": "Arsenal FC", "shortName": "Arsenal"}, "awayTeam": {"name": "Chelsea FC", "shortName": "Chelsea"}},
        {"status": "SCHEDULED", "utcDate": "2026-12-26T15:00:00Z",
         "homeTeam": {"name": "Everton FC", "shortName": "Everton"}, "awayTeam": {"name": "Liverpool FC", "shortName": "Liverpool"}},
    ]}
    matches = parse_pl_matches(data)
    assert [m.title for m in matches] == ["Arsenal vs Chelsea", "Everton vs Liverpool"]
    assert matches[0].teams == ("Arsenal FC", "Chelsea FC")


def must_not_call(*args, **kwargs):
    raise AssertionError("no HTTP call expected")


def test_event_store_without_football_data_key_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
    store = EventStore(tmp_path, now=lambda: NOW, http_get=must_not_call)
    assert store.pl_matches() is None
    assert store.next_watchable_event("pl", "Arsenal", "evenings") is None


def test_event_store_caches_and_survives_a_failed_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "fake-football-key")
    calls = []
    data = {"matches": [{"status": "TIMED", "utcDate": "2026-10-24T14:00:00Z",
                         "homeTeam": {"name": "Arsenal FC"}, "awayTeam": {"name": "Chelsea FC"}}]}

    def fake_get(url, **kwargs):
        calls.append(kwargs.get("headers", {}).get("X-Auth-Token"))
        return httpx.Response(200, json=data, request=httpx.Request("GET", url))

    monkeypatch.setattr(events, "FOOTBALL_DATA_MIN_GAP_S", 0.0)
    store = EventStore(tmp_path, now=lambda: NOW, http_get=fake_get)
    assert len(store.pl_matches()) == 1
    assert len(store.pl_matches()) == 1
    assert calls == ["fake-football-key"]  # second read came from the cache

    def failing_get(url, **kwargs):
        raise httpx.ConnectError("down")

    stale = EventStore(tmp_path, now=lambda: datetime(2026, 11, 30, tzinfo=UTC), http_get=failing_get)
    assert len(stale.pl_matches()) == 1  # stale copy beats nothing
