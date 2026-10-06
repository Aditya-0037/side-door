"""Popularity-bias check (Task 5): does a persona's personal top-1 just echo the crowd (no-signal baseline)?"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from backend.qloo.models import InsightEntity
from backend.ranking.taste_lift import ranks

BIAS_PERSONA_THRESHOLD = 2  # personas whose top-1 sits in the crowd top-3
BIAS_BETA = 0.15


@dataclass(frozen=True)
class PersonaBias:
    persona: str
    top3: list[str]
    top1_in_crowd_top3: bool


def _top(entities: Sequence[InsightEntity], k: int) -> list[InsightEntity]:
    order = ranks(entities)
    return sorted(entities, key=lambda e: order[e.entity_id])[:k]


def bias_report(personas: Mapping[str, Sequence[InsightEntity]],
                baseline: Sequence[InsightEntity]) -> list[PersonaBias]:
    crowd_top3 = {e.entity_id for e in _top(baseline, 3)}
    report = []
    for persona, entities in personas.items():
        top3 = _top(entities, 3)
        report.append(PersonaBias(persona=persona, top3=[e.name for e in top3],
                                  top1_in_crowd_top3=bool(top3) and top3[0].entity_id in crowd_top3))
    return report


def choose_beta(report: Sequence[PersonaBias]) -> float:
    hits = sum(r.top1_in_crowd_top3 for r in report)
    return BIAS_BETA if hits >= BIAS_PERSONA_THRESHOLD else 0.0
