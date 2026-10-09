"""Preset personas and the aggregate "cultural doors", both derived from the 20 synthetic eval
personas (data/personas_eval.json + the cached sweep responses in data/sweep_inputs.json)."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from backend.inventory.rosters import ROOT
from backend.qloo.models import InsightEntity
from backend.ranking.taste_lift import DEFAULT_ALPHA, DEFAULT_BETA, compute_taste_lift

PERSONAS_PATH = ROOT / "data" / "personas_eval.json"
SWEEP_INPUTS_PATH = ROOT / "data" / "sweep_inputs.json"
# Six diverse presets; the first two are Indian-taste personas.
PRESET_IDS = ("desi-anime-gamer", "punjabi-pop-bgmi", "cozy-romantic", "dark-hiphop", "space-dreamer", "horror-rock")


def load_presets(personas_path: Path = PERSONAS_PATH, inputs_path: Path = SWEEP_INPUTS_PATH) -> list[dict[str, Any]]:
    personas = {p["id"]: p for p in json.loads(personas_path.read_text(encoding="utf-8"))["personas"]}
    resolved = json.loads(inputs_path.read_text(encoding="utf-8"))["seeds"]
    presets = []
    for pid in PRESET_IDS:
        p = personas[pid]
        seeds = [{"kind": kind, "text": text, "entity_id": s["entity_id"], "name": s["name"]}
                 for (kind, text), s in zip(p["seeds"].items(), resolved[pid])]
        presets.append({"id": pid, "label": p["label"], "seeds": seeds})
    return presets


def cultural_doors(personas_path: Path = PERSONAS_PATH, inputs_path: Path = SWEEP_INPUTS_PATH) -> dict[str, Any]:
    """For every seed in the eval set: which F1 driver ends up featured for the personas using it."""
    personas = {p["id"]: p for p in json.loads(personas_path.read_text(encoding="utf-8"))["personas"]}
    inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
    teams = {r["entity_id"]: r["team"] for r in inputs["roster"]}
    baseline = [InsightEntity(**e) for e in inputs["baseline"]]
    doors: dict[tuple[str, str], Counter] = defaultdict(Counter)
    overall: Counter = Counter()
    for pid, entities in inputs["personal"].items():
        ranked = compute_taste_lift([InsightEntity(**e) for e in entities], baseline, teams).ranked
        if not ranked:
            continue
        overall[ranked[0].name] += 1
        for (kind, _), seed in zip(personas[pid]["seeds"].items(), inputs["seeds"][pid]):
            doors[(seed["name"], kind)][ranked[0].name] += 1
    return {
        "personas": len(inputs["personal"]), "sport": inputs["sport"],
        "params": {"alpha": DEFAULT_ALPHA, "beta": DEFAULT_BETA},
        "doors": [{"seed": seed, "kind": kind, "personas": sum(c.values()), "featured_counts": dict(c.most_common())}
                  for (seed, kind), c in sorted(doors.items(), key=lambda kv: (kv[0][1], kv[0][0]))],
        "featured_overall": dict(overall.most_common()),
        "note": "Aggregated from 20 synthetic evaluation personas; no user data.",
    }
