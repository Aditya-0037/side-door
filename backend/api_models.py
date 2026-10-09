"""Request/response models for the Side Door API -- the stable schema the frontend codes against.

UI fields carry names, ranks and labels only; raw scores live in `debug` (never for UI copy).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Sport = Literal["f1", "pl"]
Window = Literal["evenings", "late_night_ok", "weekends_only"]
SeedType = Literal["movie", "artist", "videogame"]


class SeedIn(BaseModel):
    text: str = Field(min_length=1, max_length=120)
    type: SeedType | None = None


class ResolveRequest(BaseModel):
    items: list[SeedIn] = Field(min_length=1, max_length=5)


class ResolvedSeedOut(BaseModel):
    input_text: str
    type: SeedType | None
    found: bool
    entity_id: str | None = None
    name: str | None = None
    matched_as: str | None = None


class ResolveResponse(BaseModel):
    seeds: list[ResolvedSeedOut]


class EventOut(BaseModel):
    sport: Sport
    kind: Literal["race", "qualifying", "sprint", "match"]
    title: str
    start_utc: str
    start_ist: str
    label_ist: str
    window: Window
    fits_window: bool
    suggestion: str | None = None


class ClipOut(BaseModel):
    kind: Literal["athlete_channel", "league_channel", "website"]
    label: str
    url: str
    embed_url: str | None = None


class SeedShare(BaseModel):
    seed: str
    pct: int


class CardDebug(BaseModel):
    final: float
    pct_personal: float
    crowd_pct: float
    distinctive_ratio: float | None = None


class AthleteCard(BaseModel):
    entity_id: str
    name: str
    team: str | None
    crowd_rank: int
    personal_rank: int
    lift: int
    n_scored: int
    why_line: str
    distinctive_seed: str | None = None
    seed_shares_pct: list[SeedShare]
    next_event: EventOut | None = None
    clips: list[ClipOut]
    debug: CardDebug


class UnknownAthleteOut(BaseModel):
    entity_id: str
    name: str | None
    team: str | None


class UnknownSection(BaseModel):
    title: str
    athletes: list[UnknownAthleteOut]


class TraceStep(BaseModel):
    step: str
    detail: str = ""
    ms: int
    ok: bool
    error: str | None = None


class SeedRef(BaseModel):
    entity_id: str
    name: str


class SideDoorRequest(BaseModel):
    seed_ids: list[str] = Field(min_length=1, max_length=5)
    sport: Sport
    window: Window = "evenings"


class SideDoorResponse(BaseModel):
    sport: Sport
    window: Window
    seeds: list[SeedRef]
    featured: AthleteCard | None
    alternates: list[AthleteCard]
    unknown: UnknownSection
    n_scored: int
    events_available: bool
    trace: list[TraceStep]
    cached: bool
    precomputed: bool


class WithoutQlooRequest(BaseModel):
    seed_names: list[str] = Field(min_length=1, max_length=5)
    sport: Sport


class WithoutQlooResponse(BaseModel):
    sport: Sport
    seed_names: list[str]
    model: str
    available: bool
    error: str | None = None
    featured: str | None
    alternates: list[str]
    reason: str
    dropped: list[str]
    cached: bool
    precomputed: bool = False


class RepairRequest(BaseModel):
    seed_ids: list[str] = Field(min_length=1, max_length=5)
    sport: Sport
    window: Window = "evenings"
    rejected_ids: list[str] = Field(min_length=1, max_length=10)
    reason: Literal["not_feeling_it", "bad_time"]
    allow_sport_switch: bool = False


class AgentTraceEntry(BaseModel):
    tool: str
    args: str
    ms: int
    ok: bool
    error: str | None = None


class RepairResponse(BaseModel):
    payload: SideDoorResponse
    what_changed: str
    narration: str | None
    narration_available: bool
    narration_note: str | None = None
    trace: list[AgentTraceEntry]


class PresetSeed(BaseModel):
    kind: SeedType
    text: str
    entity_id: str
    name: str


class Preset(BaseModel):
    id: str
    label: str
    seeds: list[PresetSeed]


class PresetsResponse(BaseModel):
    presets: list[Preset]


class Door(BaseModel):
    seed: str
    kind: SeedType
    personas: int
    featured_counts: dict[str, int]


class InsightsResponse(BaseModel):
    personas: int
    sport: Sport
    params: dict[str, float]
    doors: list[Door]
    featured_overall: dict[str, int]
    note: str


class HealthResponse(BaseModel):
    status: Literal["ok"]
    keys_present: dict[str, bool]
    qloo_budget: dict[str, int]
    precomputed_payloads: int
