"""Precompute the 6 preset personas x 2 sports for reliability: the full /api/side-door payload
(window "evenings"; the API re-attaches events for other windows) and the /api/without-qloo
result. The API then serves presets from data/precomputed/ with 0 live calls.

    $env:PYTHONUTF8="1"; .venv\\Scripts\\python.exe scripts/precompute_presets.py [--max-qloo-calls 15]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from backend.main import PRECOMPUTED_DIR, build_default_tools  # noqa: E402
from backend.pipeline import side_door  # noqa: E402
from backend.presets import load_presets  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-qloo-calls", type=int, default=15)
    args = parser.parse_args()

    load_dotenv()
    tools = build_default_tools()
    start = tools.qloo.budget.used
    tools.qloo.budget.limit = min(tools.qloo.budget.limit, start + args.max_qloo_calls)
    PRECOMPUTED_DIR.mkdir(parents=True, exist_ok=True)

    presets = load_presets()
    tools.remember_seeds({s["entity_id"]: s["name"] for p in presets for s in p["seeds"]})
    llm_ok = 0
    for p in presets:
        seed_ids = [s["entity_id"] for s in p["seeds"]]
        names = [s["name"] for s in p["seeds"]]
        for sport in ("f1", "pl"):
            payload = side_door(tools, seed_ids, sport, "evenings")
            (PRECOMPUTED_DIR / f"{p['id']}__{sport}.json").write_text(
                json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            llm = tools.llm_only_rank(names, sport)
            if not llm["available"] and tools.llm is not None and not llm.get("cached"):
                tools.llm = None  # circuit breaker: don't repeat a failing Gemini call 11 more times
            if llm["available"]:
                (PRECOMPUTED_DIR / f"{p['id']}__{sport}__without_qloo.json").write_text(
                    json.dumps(llm, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
                llm_ok += 1
            featured = payload["featured"]["name"] if payload["featured"] else "-"
            print(f"  {p['id']:18s} {sport}: side door -> {featured:18s} | without qloo -> "
                  f"{llm['featured'] if llm['available'] else 'unavailable (' + str(llm['error']) + ')'}")
    print(f"\nQloo calls: {tools.qloo.budget.used - start}; without-qloo saved: {llm_ok}/{len(presets) * 2}")


if __name__ == "__main__":
    main()
