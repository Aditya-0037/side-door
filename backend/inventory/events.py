"""Watchable events: the next F1 race weekend (Jolpica) and Premier League fixtures
(football-data.org), shown in IST and filtered by the viewer's watch window.

Times come only from the sources; when nothing fits the window we say "catch the highlights"
instead of inventing a time. Schedules are cached in .cache/events/ and refreshed at most daily.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from backend.inventory.rosters import JOLPICA, ROOT, club_tokens

IST = timezone(timedelta(hours=5, minutes=30), "IST")  # Asia/Kolkata: fixed offset, no DST
WINDOWS = ("evenings", "late_night_ok", "weekends_only")
LOOKAHEAD = timedelta(days=14)
F1_PREFERENCE = ("race", "qualifying", "sprint")
HIGHLIGHTS = "Catch the highlights"
JOLPICA_SCHEDULE = f"{JOLPICA}/current.json?limit=100"
FOOTBALL_DATA_PL = "https://api.football-data.org/v4/competitions/PL/matches"
UPCOMING_STATUSES = {"SCHEDULED", "TIMED"}  # TIMED = kick-off time confirmed
CACHE_DIR = ROOT / ".cache" / "events"
MAX_AGE = timedelta(hours=24)  # nightly refresh
FOOTBALL_DATA_MIN_GAP_S = 6.0  # free tier: 10 calls/min


@dataclass(frozen=True)
class Event:
    sport: str  # "f1" | "pl"
    kind: str  # "race" | "qualifying" | "sprint" | "match"
    title: str
    start_utc: datetime
    teams: tuple[str, ...] = ()  # PL: home, away


@dataclass(frozen=True)
class WatchableEvent:
    event: Event
    window: str
    fits_window: bool
    suggestion: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"sport": self.event.sport, "kind": self.event.kind, "title": self.event.title,
                "start_utc": self.event.start_utc.isoformat(), "start_ist": to_ist(self.event.start_utc).isoformat(),
                "label_ist": ist_label(self.event.start_utc), "window": self.window,
                "fits_window": self.fits_window, "suggestion": self.suggestion}


def to_ist(dt: datetime) -> datetime:
    return dt.astimezone(IST)


def ist_label(dt: datetime) -> str:
    """'Sun 11 Oct, 17:30 IST'."""
    t = to_ist(dt)
    return f"{t:%a} {t.day} {t:%b}, {t:%H:%M} IST"


def in_window(dt: datetime, window: str) -> bool:
    """Watch-window presets, judged on the IST start time.
    evenings: weekdays 19:00-23:59, weekends 12:00-23:59; late_night_ok: any day 17:00-02:59;
    weekends_only: Saturday or Sunday, any time."""
    t = to_ist(dt)
    weekend = t.weekday() >= 5
    if window == "evenings":
        return t.hour >= (12 if weekend else 19)
    if window == "late_night_ok":
        return t.hour >= 17 or t.hour < 3
    if window == "weekends_only":
        return weekend
    raise ValueError(f"unknown window {window!r}; use one of {WINDOWS}")


def _utc(date: str, clock: str | None) -> datetime:
    return datetime.fromisoformat(f"{date}T{(clock or '00:00:00Z').replace('Z', '+00:00')}")


def parse_f1_schedule(data: dict[str, Any]) -> list[list[Event]]:
    """Jolpica season schedule -> one list of watchable sessions per race weekend."""
    weekends = []
    for race in data["MRData"]["RaceTable"]["Races"]:
        name = race["raceName"]
        sessions = [Event("f1", "race", f"{name} — Race", _utc(race["date"], race.get("time")))]
        for key, kind, label in (("Qualifying", "qualifying", "Qualifying"), ("Sprint", "sprint", "Sprint")):
            if key in race:
                sessions.append(Event("f1", kind, f"{name} — {label}", _utc(race[key]["date"], race[key].get("time"))))
        weekends.append(sorted(sessions, key=lambda s: s.start_utc))
    return weekends


def parse_pl_matches(data: dict[str, Any]) -> list[Event]:
    events = []
    for m in data.get("matches", []):
        if m.get("status") not in UPCOMING_STATUSES:
            continue
        home, away = m["homeTeam"]["name"], m["awayTeam"]["name"]
        title = f"{m['homeTeam'].get('shortName') or home} vs {m['awayTeam'].get('shortName') or away}"
        start = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
        events.append(Event("pl", "match", title, start, (home, away)))
    return sorted(events, key=lambda e: e.start_utc)


def next_f1_event(weekends: list[list[Event]], window: str, now: datetime) -> WatchableEvent | None:
    """The next session that fits the window, weekend by weekend within 14 days (race preferred,
    then qualifying, then sprint). Otherwise the next race, with fits_window=False."""
    horizon = now + LOOKAHEAD
    upcoming = [[s for s in w if s.start_utc > now] for w in weekends]
    upcoming = [w for w in upcoming if w]
    for sessions in upcoming:
        if sessions[0].start_utc > horizon:
            break
        fitting = [s for s in sessions if s.start_utc <= horizon and in_window(s.start_utc, window)]
        if fitting:
            best = min(fitting, key=lambda s: (F1_PREFERENCE.index(s.kind), s.start_utc))
            return WatchableEvent(best, window, True)
    if not upcoming:
        return None
    first = upcoming[0]
    main = next((s for s in first if s.kind == "race"), first[0])
    return WatchableEvent(main, window, False, HIGHLIGHTS)


def next_pl_event(matches: list[Event], club: str, window: str, now: datetime) -> WatchableEvent | None:
    wanted = club_tokens(club)
    mine = [m for m in matches if m.start_utc > now and any(wanted <= club_tokens(t) for t in m.teams)]
    for m in mine:
        if m.start_utc > now + LOOKAHEAD:
            break
        if in_window(m.start_utc, window):
            return WatchableEvent(m, window, True)
    return WatchableEvent(mine[0], window, False, HIGHLIGHTS) if mine else None


class EventStore:
    """Cached schedules. A failed refresh falls back to the stale cache; PL needs FOOTBALL_DATA_API_KEY."""

    def __init__(self, cache_dir: Path = CACHE_DIR, now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 http_get: Callable[..., httpx.Response] = httpx.get, max_age: timedelta = MAX_AGE) -> None:
        self.cache_dir = Path(cache_dir)
        self.now = now
        self._get = http_get
        self.max_age = max_age
        self._last_football_data_call = 0.0

    def _cached(self, name: str, fetch: Callable[[], dict[str, Any]] | None, refresh: bool = False) -> dict[str, Any] | None:
        path = self.cache_dir / f"{name}.json"
        stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        fresh = stored and self.now() - datetime.fromisoformat(stored["fetched_at"]) < self.max_age
        if fetch and (refresh or not fresh):
            try:
                stored = {"fetched_at": self.now().isoformat(), "data": fetch()}
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
            except (httpx.HTTPError, ValueError, KeyError):
                pass  # keep the stale copy, if any
        return stored["data"] if stored else None

    def _fetch_jolpica(self) -> dict[str, Any]:
        resp = self._get(JOLPICA_SCHEDULE, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _fetch_football_data(self) -> dict[str, Any]:
        key = os.environ.get("FOOTBALL_DATA_API_KEY")
        if not key:
            raise KeyError("FOOTBALL_DATA_API_KEY")
        wait = FOOTBALL_DATA_MIN_GAP_S - (time.monotonic() - self._last_football_data_call)
        if wait > 0:
            time.sleep(wait)
        self._last_football_data_call = time.monotonic()
        resp = self._get(FOOTBALL_DATA_PL, headers={"X-Auth-Token": key}, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def f1_weekends(self, refresh: bool = False) -> list[list[Event]]:
        data = self._cached("jolpica_schedule", self._fetch_jolpica, refresh)
        return parse_f1_schedule(data) if data else []

    def pl_matches(self, refresh: bool = False) -> list[Event] | None:
        """None when no fixtures are available (no key and nothing cached)."""
        data = self._cached("football_data_pl", self._fetch_football_data, refresh)
        return parse_pl_matches(data) if data else None

    def next_watchable_event(self, sport: str, team: str | None, window: str) -> WatchableEvent | None:
        if sport == "f1":
            return next_f1_event(self.f1_weekends(), window, self.now())
        matches = self.pl_matches()
        if not matches or not team:
            return None
        return next_pl_event(matches, team, window, self.now())
