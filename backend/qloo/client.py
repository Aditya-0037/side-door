"""Thin, typed Qloo client: throttled (3 req/s), retried on 429, TTL-cached and budget-guarded.

The API key is read from the environment, sent only as the `X-Api-Key` header, and never appears
in __repr__, cache keys, logs or exception messages.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from backend.qloo.cache import DEFAULT_TTL, Cache, DiskCache, MemoryCache, cache_key
from backend.qloo.models import InsightEntity, SearchResult, parse_insights, parse_search

ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "https://hackathon.api.qloo.com"
PERSON = "urn:entity:person"
MAX_TAKE = 50  # take > 50 is rejected by /v2/insights
MAX_CANDIDATES = 100  # filter.results.entities verified with 100 IDs
RETRY_DELAYS = (0.5, 1.0, 2.0)  # backoff for 429s, max 3 retries
BASELINE_TTL = 7 * 24 * 3600.0  # the no-signal ranking doesn't depend on the user, so keep it longer
DEFAULT_DAILY_BUDGET = 150

log = logging.getLogger(__name__)


class QlooError(Exception):
    pass


class QlooHTTPError(QlooError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class QlooRateLimited(QlooHTTPError):
    pass


class BudgetExceeded(QlooError):
    pass


class TokenBucket:
    """Blocks until a request may go out. capacity=1 means strict spacing: never more than `rate` per second."""

    def __init__(self, rate: float = 3.0, capacity: float = 1.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.rate = rate
        self.capacity = capacity
        self._clock = clock
        self._sleep = sleep
        self._tokens = capacity
        self._last = clock()

    def acquire(self) -> None:
        while True:
            now = self._clock()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens >= 1:
                self._tokens -= 1
                return
            self._sleep((1 - self._tokens) / self.rate)


class BudgetGuard:
    """Counts real HTTP calls per day; raises BudgetExceeded once `limit` is reached.

    With a `state_path` the count survives across processes (all scripts share one daily budget).
    """

    def __init__(self, limit: int = DEFAULT_DAILY_BUDGET, state_path: str | Path | None = None,
                 today: Callable[[], date] = date.today) -> None:
        self.limit = limit
        self.state_path = Path(state_path) if state_path else None
        self._today = today
        self._day = today().isoformat()
        self._used = 0
        if self.state_path and self.state_path.exists():
            try:
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
            if state.get("date") == self._day:
                self._used = int(state.get("calls", 0))

    @property
    def used(self) -> int:
        self._roll()
        return self._used

    def _roll(self) -> None:
        day = self._today().isoformat()
        if day != self._day:
            self._day, self._used = day, 0

    def spend(self) -> None:
        self._roll()
        if self._used >= self.limit:
            raise BudgetExceeded(
                f"Qloo daily call budget reached: {self._used}/{self.limit} calls used on {self._day}. "
                "Raise QLOO_DAILY_BUDGET or wait until tomorrow.")
        self._used += 1
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({"date": self._day, "calls": self._used}), encoding="utf-8")


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


def _unique(ids: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(i for i in ids if i))


class QlooClient:
    def __init__(self, api_key: str | None = None, *, base_url: str = BASE_URL,
                 cache: Cache | None = None, cache_ttl: float = DEFAULT_TTL,
                 budget: BudgetGuard | None = None, throttle: TokenBucket | None = None,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        key = api_key or os.environ.get("QLOO_API_KEY")
        if not key:
            raise QlooError("QLOO_API_KEY is not set. Put it in .env (never commit it).")
        self.__key = key
        self.base_url = base_url
        self.cache: Cache = cache if cache is not None else MemoryCache()
        self.cache_ttl = cache_ttl
        self.budget = budget if budget is not None else BudgetGuard()
        self.throttle = throttle if throttle is not None else TokenBucket()
        self._sleep = sleep
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    @classmethod
    def from_env(cls, **kwargs: Any) -> QlooClient:
        """Load .env (if python-dotenv finds one) and build a client from QLOO_API_KEY."""
        from dotenv import load_dotenv

        load_dotenv()
        return cls(**kwargs)

    def __repr__(self) -> str:
        return f"QlooClient(base_url={self.base_url!r}, api_key='***')"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> QlooClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _redact(self, text: str) -> str:
        return text.replace(self.__key, "***")

    def _get(self, path: str, params: dict[str, Any], ttl: float | None = None) -> dict[str, Any]:
        query = {k: _fmt(v) for k, v in params.items() if v is not None}
        key = cache_key(path, query)
        cached = self.cache.get(key)
        if cached is not None:
            log.debug("cache hit %s", path)
            return cached

        for attempt in range(len(RETRY_DELAYS) + 1):
            self.budget.spend()
            self.throttle.acquire()
            log.debug("GET %s (attempt %d)", path, attempt + 1)
            try:
                resp = self._http.get(path, params=query, headers={"X-Api-Key": self.__key})
            except httpx.HTTPError as exc:
                # `from None`: don't chain the httpx exception (it carries the request object).
                raise QlooError(self._redact(f"{type(exc).__name__} calling GET {path}: {exc}")) from None
            if resp.status_code == 429 and attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                log.warning("429 from GET %s; retry %d in %.1fs", path, attempt + 1, delay)
                self._sleep(delay)
                continue
            break

        if resp.status_code == 429:
            raise QlooRateLimited(429, f"GET {path} still rate-limited after {len(RETRY_DELAYS)} retries")
        if resp.status_code != 200:
            raise QlooHTTPError(resp.status_code,
                                self._redact(f"GET {path} -> {resp.status_code}: {resp.text[:300]}"))
        body = resp.json()
        self.cache.set(key, body, self.cache_ttl if ttl is None else ttl)
        return body

    # ------------------------------------------------------------------ endpoints

    def search(self, query: str, types: str | None = None, take: int = 5) -> list[SearchResult]:
        return parse_search(self._get("/search", {"query": query, "types": types, "take": take}))

    def insights_people(self, signal_ids: Sequence[str], candidate_ids: Sequence[str], explain: bool = True,
                        exclude_ids: Iterable[str] | None = None, take: int = MAX_TAKE) -> list[InsightEntity]:
        """Rank `candidate_ids` for a taste signal. Exclusions are dropped from the candidate list
        client-side (equivalent to a filter, and needs no extra API parameter)."""
        signals = _unique(signal_ids)
        if not signals:
            raise ValueError("insights_people needs at least one signal id; use baseline_people for no signal")
        params = self._people_params(candidate_ids, exclude_ids, take)
        params["signal.interests.entities"] = signals
        params["bias.trends"] = "off"
        if explain:
            params["feature.explainability"] = True
        return parse_insights(self._get("/v2/insights", params))

    def baseline_people(self, candidate_ids: Sequence[str], take: int = MAX_TAKE) -> list[InsightEntity]:
        """No-signal ranking over the same candidates (same params as the verified K3 baseline).
        Results carry no affinity; they come back ordered by popularity."""
        return parse_insights(self._get("/v2/insights", self._people_params(candidate_ids, None, take),
                                        ttl=BASELINE_TTL))

    def compare(self, a_ids: Sequence[str], b_ids: Sequence[str]) -> dict[str, Any]:
        """UNVERIFIED (one smoke call, 200): /v2/analysis/compare. Returns the raw body for now.
        Seen shape (tests/fixtures/compare-smoke.json): results.{tags, a, b, matchEntities}, where
        a/b are tag profiles [{tag_id, name, subtype, popularity, query: {count}}] and `tags` the
        shared ones. TODO: model it once we use it for "why you'd like them" copy."""
        return self._get("/v2/analysis/compare", {"a.signal.interests.entities": _unique(a_ids),
                                                  "b.signal.interests.entities": _unique(b_ids)})

    @staticmethod
    def dev(cache_ttl: float = DEFAULT_TTL) -> QlooClient:
        """Client for local scripts: disk cache in .cache/qloo/ and one daily budget shared by every
        script through .cache/qloo_budget.json (limit from QLOO_DAILY_BUDGET, default 150)."""
        from dotenv import load_dotenv

        load_dotenv()
        limit = int(os.environ.get("QLOO_DAILY_BUDGET", DEFAULT_DAILY_BUDGET))
        return QlooClient(cache=DiskCache(ROOT / ".cache" / "qloo"), cache_ttl=cache_ttl,
                          budget=BudgetGuard(limit, ROOT / ".cache" / "qloo_budget.json"))

    @staticmethod
    def _people_params(candidate_ids: Sequence[str], exclude_ids: Iterable[str] | None,
                       take: int) -> dict[str, Any]:
        if not 1 <= take <= MAX_TAKE:
            raise ValueError(f"take must be 1..{MAX_TAKE} (the API rejects more)")
        excluded = set(exclude_ids or ())
        # Sorted: the filter is a set, and a stable order gives stable cache keys.
        candidates = sorted(set(_unique(candidate_ids)) - excluded)
        if not candidates:
            raise ValueError("no candidate ids left to rank")
        if len(candidates) > MAX_CANDIDATES:
            raise ValueError(f"at most {MAX_CANDIDATES} candidate ids per query")
        return {"filter.type": PERSON, "filter.results.entities": candidates, "take": take}
