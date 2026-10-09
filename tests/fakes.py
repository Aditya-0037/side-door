"""Test doubles: a fake Qloo client, small rosters, a fixed event schedule and a fake LLM.
Nothing here touches the network."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from backend.agent.tools import SideDoorTools
from backend.inventory.events import Event, next_f1_event, next_pl_event
from backend.inventory.rosters import RosterAthlete
from backend.qloo.models import InsightEntity, SearchResult

NOW = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)  # Tuesday
SEEDS = {"S1": "Spirited Away", "S2": "Arijit Singh", "S3": "Valorant"}


def _athlete(sport: str, eid: str, name: str, team: str) -> RosterAthlete:
    return RosterAthlete(sport=sport, name=name, team=team, qloo_id=eid, verified="auto", tier=2)


ROSTERS = {
    "f1": [_athlete("f1", "D1", "Driver One", "Team A"), _athlete("f1", "D2", "Driver Two", "Team A"),
           _athlete("f1", "D3", "Driver Three", "Team B"), _athlete("f1", "D4", "Driver Four", "Team C"),
           _athlete("f1", "D5", "Rookie Five", "Team C")],  # D5: no Qloo data
    "pl": [_athlete("pl", "P1", "Player One", "Liverpool"), _athlete("pl", "P2", "Player Two", "Arsenal"),
           _athlete("pl", "P3", "Player Three", "Chelsea"), _athlete("pl", "P4", "Player Four", "Everton")],
}
NAMES = {a.qloo_id: a.name for athletes in ROSTERS.values() for a in athletes}
PERSONAL = {"D1": 0.80, "D2": 0.78, "D3": 0.75, "D4": 0.70, "P1": 0.90, "P2": 0.80, "P3": 0.70, "P4": 0.60}
CROWD = {"D1": 0.99, "D2": 0.50, "D3": 0.90, "D4": 0.60, "P1": 0.90, "P2": 0.80, "P3": 0.70, "P4": 0.95}
EXPL = {"S1": 0.35, "S2": 0.34, "S3": 0.31}

# US GP weekend: qualifying Sun 02:30 IST, race Mon 01:30 IST; Mexico the week after, same pattern.
F1_WEEKENDS = [
    [Event("f1", "qualifying", "United States Grand Prix — Qualifying", datetime(2026, 10, 24, 21, 0, tzinfo=UTC)),
     Event("f1", "race", "United States Grand Prix — Race", datetime(2026, 10, 25, 20, 0, tzinfo=UTC))],
    [Event("f1", "qualifying", "Mexico City Grand Prix — Qualifying", datetime(2026, 10, 31, 21, 0, tzinfo=UTC)),
     Event("f1", "race", "Mexico City Grand Prix — Race", datetime(2026, 11, 1, 20, 0, tzinfo=UTC))],
]
PL_MATCHES = [
    Event("pl", "match", "Arsenal vs Chelsea", datetime(2026, 10, 24, 14, 0, tzinfo=UTC),  # Sat 19:30 IST
          ("Arsenal FC", "Chelsea FC")),
    Event("pl", "match", "Liverpool vs Everton", datetime(2026, 10, 27, 5, 0, tzinfo=UTC),  # Tue 10:30 IST
          ("Liverpool FC", "Everton FC")),
]


class FakeBudget:
    def __init__(self) -> None:
        self.used = 0
        self.limit = 100


class FakeQloo:
    def __init__(self) -> None:
        self.budget = FakeBudget()
        self.calls: list[str] = []

    def _call(self, name: str) -> None:
        self.calls.append(name)
        self.budget.used += 1

    def search(self, query: str, types: str | None = None, take: int = 5) -> list[SearchResult]:
        self._call("search")
        if query == "nothing at all":
            return []
        kind = types or "urn:entity:movie"
        return [SearchResult(entity_id=f"E-{query[:6]}", name=query.title(), types=[kind], disambiguation="2005")]

    def insights_people(self, signal_ids, candidate_ids, explain=True, exclude_ids=None, take=50):
        self._call("insights")
        keep = set(candidate_ids) - set(exclude_ids or ())
        return [InsightEntity(entity_id=i, name=NAMES[i], affinity=PERSONAL[i],
                              explainability={s: EXPL.get(s, 0.3) for s in signal_ids})
                for i in candidate_ids if i in keep and i in PERSONAL]

    def baseline_people(self, candidate_ids, take=50):
        self._call("baseline")
        return [InsightEntity(entity_id=i, name=NAMES[i], popularity=CROWD[i]) for i in candidate_ids if i in CROWD]


class FakeEvents:
    def __init__(self, now: datetime = NOW, pl_available: bool = True) -> None:
        self.now = now
        self.pl_available = pl_available

    def next_watchable_event(self, sport: str, team: str | None, window: str):
        if sport == "f1":
            return next_f1_event(F1_WEEKENDS, window, self.now)
        if not self.pl_available or not team:
            return None
        return next_pl_event(PL_MATCHES, team, window, self.now)


class FakeLLM:
    model = "fake-flash"

    def __init__(self, reply: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.reply = reply or {"featured": "Driver Three", "alternates": ["Ayrton Senna", "driver one"],
                               "reason": "Feels like a match."}
        self.error = error
        self.calls = 0

    def generate_json(self, prompt: str) -> dict[str, Any]:
        self.calls += 1
        if self.error:
            raise self.error
        return self.reply


MEDIA = {"athletes": {"D1": {"name": "Driver One", "youtube_channel_id": "UC" + "a" * 22,
                             "official_website": "https://driver-one.example"}},
         "leagues": {"f1": {"label": "Formula 1", "youtube_channel_id": "UC" + "b" * 22},
                     "pl": {"label": "Premier League", "youtube_channel_id": None}}}


def make_tools(tmp_path, llm: FakeLLM | None = None, pl_available: bool = True) -> SideDoorTools:
    tools = SideDoorTools(FakeQloo(), FakeEvents(pl_available=pl_available), MEDIA, llm, ROSTERS,
                          seed_names_path=None, llm_cache_dir=tmp_path / "llm")
    tools.remember_seeds(SEEDS)
    return tools
