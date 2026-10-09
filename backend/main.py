"""Side Door API (FastAPI, stateless).

The main flow (/api/side-door) is deterministic and never touches the LLM. The ADK agent is only
used for /api/repair, and even there the picks come back if Gemini is down or slow.

    .venv\\Scripts\\python.exe -m uvicorn backend.main:app --reload
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.agent.agent import AGENT_TIMEOUT_S, Runner, adk_runner, run_repair
from backend.agent.tools import GeminiJson, SideDoorTools
from backend.api_models import (
    HealthResponse, InsightsResponse, PresetsResponse, RepairRequest, RepairResponse, ResolveRequest,
    ResolveResponse, SideDoorRequest, SideDoorResponse, WithoutQlooRequest, WithoutQlooResponse,
)
from backend.inventory.clips import load_media
from backend.inventory.events import EventStore
from backend.inventory.rosters import ROOT
from backend.pipeline import refresh_events, side_door
from backend.presets import cultural_doors, load_presets
from backend.qloo.client import BudgetExceeded, QlooClient, QlooError

PRECOMPUTED_DIR = ROOT / "data" / "precomputed"
DEV_ORIGIN = "http://localhost:5173"


class RateLimiter:
    """Sliding one-minute window per client IP."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute = per_minute
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self._clock()
        hits = self._hits[key]
        while hits and now - hits[0] >= 60:
            hits.popleft()
        if len(hits) >= self.per_minute:
            return False
        hits.append(now)
        return True


class Precomputed:
    """Preset payloads saved by scripts/precompute_presets.py: served with 0 live calls."""

    def __init__(self, directory: Path, presets: list[dict[str, Any]]) -> None:
        self.dir = Path(directory)
        self.by_ids = {frozenset(s["entity_id"] for s in p["seeds"]): p["id"] for p in presets}
        self.by_names = {frozenset(s["name"].lower() for s in p["seeds"]): p["id"] for p in presets}

    def _load(self, name: str) -> dict[str, Any] | None:
        path = self.dir / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def side_door(self, seed_ids: list[str], sport: str) -> dict[str, Any] | None:
        pid = self.by_ids.get(frozenset(seed_ids))
        return self._load(f"{pid}__{sport}") if pid else None

    def without_qloo(self, seed_names: list[str], sport: str) -> dict[str, Any] | None:
        pid = self.by_names.get(frozenset(n.lower() for n in seed_names))
        return self._load(f"{pid}__{sport}__without_qloo") if pid else None

    def count(self) -> int:
        return len(list(self.dir.glob("*.json"))) if self.dir.exists() else 0


def build_default_tools() -> SideDoorTools:
    llm = GeminiJson() if (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")) else None
    return SideDoorTools(QlooClient.dev(), EventStore(), load_media(), llm)


def create_app(tools: SideDoorTools | None = None, runner: Runner = adk_runner,
               precomputed_dir: Path = PRECOMPUTED_DIR, rate_limit: int | None = None,
               agent_timeout_s: float = AGENT_TIMEOUT_S) -> FastAPI:
    app = FastAPI(title="Side Door API", version="0.1.0")
    holder: dict[str, SideDoorTools | None] = {"tools": tools}

    presets = load_presets()
    preset_seed_names = {s["entity_id"]: s["name"] for p in presets for s in p["seeds"]}

    def get_tools() -> SideDoorTools:
        if holder["tools"] is None:
            holder["tools"] = build_default_tools()
        if not holder.get("seeded"):
            holder["tools"].remember_seeds(preset_seed_names)
            holder["seeded"] = True  # type: ignore[assignment]
        return holder["tools"]

    precomputed = Precomputed(precomputed_dir, presets)
    doors = cultural_doors()
    limiter = RateLimiter(rate_limit or int(os.environ.get("RATE_LIMIT_PER_MIN", "30")))

    origins = [DEV_ORIGIN, *filter(None, [os.environ.get("SIDE_DOOR_PROD_ORIGIN")])]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["*"])

    @app.middleware("http")
    async def rate_limit_mw(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path.startswith("/api/") and request.url.path != "/api/health":
            ip = request.client.host if request.client else "unknown"
            if not limiter.allow(ip):
                return JSONResponse({"detail": f"Too many requests: {limiter.per_minute} per minute per IP."},
                                    status_code=429)
        return await call_next(request)

    @app.exception_handler(BudgetExceeded)
    async def budget_exceeded(_: Request, exc: BudgetExceeded) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(QlooError)
    async def qloo_error(_: Request, exc: QlooError) -> JSONResponse:
        return JSONResponse({"detail": "Taste service unavailable, try a preset."}, status_code=502)

    @app.exception_handler(ValueError)
    async def bad_value(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.get("/api/health", response_model=HealthResponse)
    def health() -> dict[str, Any]:
        try:
            t: SideDoorTools | None = get_tools()  # cheap: no network calls
        except QlooError:
            t = None
        return {"status": "ok",
                "keys_present": {"qloo": bool(os.environ.get("QLOO_API_KEY")),
                                 "gemini": bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")),
                                 "football_data": bool(os.environ.get("FOOTBALL_DATA_API_KEY"))},
                "qloo_budget": {"used": t.qloo.budget.used, "limit": t.qloo.budget.limit} if t else {"used": 0, "limit": 0},
                "precomputed_payloads": precomputed.count()}

    @app.get("/api/presets", response_model=PresetsResponse)
    def get_presets() -> dict[str, Any]:
        return {"presets": presets}

    @app.post("/api/resolve", response_model=ResolveResponse)
    def resolve(req: ResolveRequest) -> dict[str, Any]:
        seeds = get_tools().resolve_tastes([i.model_dump() for i in req.items])
        return {"seeds": [s.__dict__ for s in seeds]}

    @app.post("/api/side-door", response_model=SideDoorResponse)
    def side_door_endpoint(req: SideDoorRequest) -> dict[str, Any]:
        tools = get_tools()
        saved = precomputed.side_door(req.seed_ids, req.sport)
        if saved:
            return {**refresh_events(tools, saved, req.window), "precomputed": True, "cached": True}
        return side_door(tools, req.seed_ids, req.sport, req.window)

    @app.post("/api/without-qloo", response_model=WithoutQlooResponse)
    def without_qloo(req: WithoutQlooRequest) -> dict[str, Any]:
        saved = precomputed.without_qloo(req.seed_names, req.sport)
        if saved:
            return {**saved, "precomputed": True, "cached": True}
        return get_tools().llm_only_rank(req.seed_names, req.sport)

    @app.post("/api/repair", response_model=RepairResponse)
    async def repair(req: RepairRequest) -> dict[str, Any]:
        return await run_repair(get_tools(), req.model_dump(), runner, agent_timeout_s)

    @app.get("/api/insights", response_model=InsightsResponse)
    def insights() -> dict[str, Any]:
        return doors

    return app


load_dotenv()  # keys enter the process environment only; they are never logged or returned
app = create_app()
