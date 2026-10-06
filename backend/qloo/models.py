"""Pydantic models for the Qloo responses we use.

Shapes confirmed against the Day-1 fixtures in tests/fixtures/:
- /search            -> body.results[]           (tags carry `tag_id`; `disambiguation` is a date of birth for people)
- /v2/insights       -> body.results.entities[]  (tags carry `id`; score lives in `query.affinity`)
- no-signal insights -> same list, but `query` has NO `affinity`; results come back sorted by `popularity`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class Tag(BaseModel):
    id: str
    name: str = ""
    type: str = ""

    @model_validator(mode="before")
    @classmethod
    def _accept_tag_id(cls, data: Any) -> Any:
        # /search calls it `tag_id`, /v2/insights calls it `id`.
        if isinstance(data, dict) and "id" not in data and "tag_id" in data:
            return {**data, "id": data["tag_id"]}
        return data


class SearchResult(BaseModel):
    entity_id: str
    name: str
    types: list[str] = Field(default_factory=list)
    popularity: float | None = None
    disambiguation: str | None = None
    tags: list[Tag] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)

    @property
    def short_description(self) -> str | None:
        return self.properties.get("short_description")

    @property
    def sports(self) -> list[str]:
        sport = self.properties.get("sport") or []
        return sport if isinstance(sport, list) else [sport]


class InsightEntity(BaseModel):
    entity_id: str
    name: str
    subtype: str | None = None
    affinity: float | None = None  # None in no-signal (baseline) calls
    explainability: dict[str, float] = Field(default_factory=dict)  # seed entity_id -> score
    popularity: float | None = None
    tags: list[Tag] = Field(default_factory=list)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> InsightEntity:
        query = raw.get("query") or {}
        contributions = (query.get("explainability") or {}).get("signal.interests.entities") or []
        return cls(
            entity_id=raw["entity_id"],
            name=raw.get("name", ""),
            subtype=raw.get("subtype"),
            affinity=query.get("affinity"),
            explainability={c["entity_id"]: float(c["score"]) for c in contributions
                            if "entity_id" in c and "score" in c},
            popularity=raw.get("popularity"),
            tags=raw.get("tags") or [],
        )


def parse_search(body: dict[str, Any]) -> list[SearchResult]:
    return [SearchResult.model_validate(r) for r in body.get("results") or []]


def parse_insights(body: dict[str, Any]) -> list[InsightEntity]:
    results = body.get("results") or {}
    return [InsightEntity.from_api(e) for e in results.get("entities") or []]
