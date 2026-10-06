import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict[str, Any]:
    """A saved Day-1 response: {"status", "params", "body"}."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
