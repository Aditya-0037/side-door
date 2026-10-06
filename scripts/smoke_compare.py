"""ONE smoke call to the UNVERIFIED /v2/analysis/compare endpoint, saved as tests/fixtures/compare-smoke.json.

Compares the Day-1 "anime-indie" seeds (Spirited Away, Arijit Singh, Valorant) with Lewis Hamilton,
reusing IDs from the fixtures so no search calls are needed. A successful response is cached, so a
re-run costs nothing; a failed one is saved with its error message.

Usage:
    $env:PYTHONUTF8="1"; .venv\\Scripts\\python.exe scripts/smoke_compare.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.qloo.client import QlooClient, QlooError, QlooHTTPError  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def main() -> None:
    k2 = json.loads((FIXTURES / "k2-insights.json").read_text(encoding="utf-8"))
    hamilton = json.loads((FIXTURES / "k1-lewis-hamilton.json").read_text(encoding="utf-8"))
    a_ids = k2["params"]["signal.interests.entities"].split(",")
    b_ids = [hamilton["body"]["results"][0]["entity_id"]]
    params = {"a.signal.interests.entities": ",".join(a_ids), "b.signal.interests.entities": ",".join(b_ids)}

    client = QlooClient.dev()
    out: dict[str, object] = {"endpoint": "/v2/analysis/compare", "params": params}
    try:
        out.update(status=200, body=client.compare(a_ids, b_ids))
    except QlooHTTPError as exc:
        out.update(status=exc.status_code, error=str(exc))
    except QlooError as exc:
        out.update(status=None, error=str(exc))
    (FIXTURES / "compare-smoke.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"status: {out['status']}")
    if "body" in out:
        print(json.dumps(out["body"], ensure_ascii=False)[:1500])
    else:
        print(out["error"])


if __name__ == "__main__":
    main()
