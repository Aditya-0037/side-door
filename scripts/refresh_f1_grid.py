"""Refresh data/f1_grid.json from Jolpica's latest race result and print what changed.

Meant to run as a nightly job later, followed by verify_rosters for F1:
    $env:PYTHONUTF8="1"; .venv\\Scripts\\python.exe scripts/refresh_f1_grid.py
    .venv\\Scripts\\python.exe scripts/verify_rosters.py --sport f1
No Qloo calls.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.inventory.rosters import F1_GRID_PATH, fetch_f1_grid  # noqa: E402


def main() -> None:
    old = json.loads(F1_GRID_PATH.read_text(encoding="utf-8")) if F1_GRID_PATH.exists() else None
    new = fetch_f1_grid()
    print(f"Latest race: {new['season']} R{new['round']} {new['race']} ({new['race_date']}), "
          f"{len(new['drivers'])} drivers")

    changed = old is None
    if old:
        before = {d["driver_id"]: d["team"] for d in old["drivers"]}
        after = {d["driver_id"]: d["team"] for d in new["drivers"]}
        for driver in sorted(after.keys() - before.keys()):
            print(f"  + {driver} ({after[driver]})")
        for driver in sorted(before.keys() - after.keys()):
            print(f"  - {driver} ({before[driver]})")
        for driver in sorted(after.keys() & before.keys()):
            if after[driver] != before[driver]:
                print(f"  ~ {driver}: {before[driver]} -> {after[driver]}")
        changed = after != before
    F1_GRID_PATH.parent.mkdir(parents=True, exist_ok=True)
    F1_GRID_PATH.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {F1_GRID_PATH.name}; "
          + ("grid changed -> run scripts/verify_rosters.py --sport f1" if changed else "no lineup changes"))


if __name__ == "__main__":
    main()
