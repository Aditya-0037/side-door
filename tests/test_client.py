import logging
import traceback
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from backend.qloo.cache import DiskCache, MemoryCache, prime_from_fixtures
from backend.qloo.client import (
    BASE_URL, BudgetExceeded, BudgetGuard, QlooClient, QlooError, QlooHTTPError, QlooRateLimited, TokenBucket,
)

FAKE_KEY = "unit-test-secret-0123456789"
SEARCH_BODY = {"results": [{"name": "Max Verstappen", "entity_id": "E1", "types": ["urn:entity:person"]}]}
INSIGHTS_BODY = {"success": True, "results": {"entities": [
    {"name": "A", "entity_id": "A", "query": {"affinity": 0.9}}]}}


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def make_client(clock: FakeClock, retry_sleeps: list[float] | None = None, **kwargs) -> QlooClient:
    kwargs.setdefault("throttle", TokenBucket(clock=clock.now, sleep=clock.sleep))
    kwargs.setdefault("cache", MemoryCache(clock=clock.now))
    kwargs.setdefault("budget", BudgetGuard(limit=100))
    sleeps = retry_sleeps if retry_sleeps is not None else []
    return QlooClient(api_key=FAKE_KEY, sleep=sleeps.append, **kwargs)


def test_token_bucket_spaces_calls():
    clock = FakeClock()
    bucket = TokenBucket(rate=3.0, clock=clock.now, sleep=clock.sleep)
    times = []
    for _ in range(4):
        bucket.acquire()
        times.append(clock.now())
    assert [round(t - times[0], 6) for t in times] == [0.0, 0.333333, 0.666667, 1.0]


def test_throttle_never_exceeds_3_requests_per_second():
    clock = FakeClock()
    sent: list[float] = []
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/search").mock(side_effect=lambda req: (sent.append(clock.now()),
                                                          httpx.Response(200, json=SEARCH_BODY))[1])
        client = make_client(clock)
        for i in range(10):
            client.search(f"query {i}")
    assert len(sent) == 10
    for i in range(len(sent) - 3):  # any 4 consecutive requests span at least one second
        assert sent[i + 3] - sent[i] >= 1.0 - 1e-9


def test_retries_429_with_backoff_then_succeeds():
    clock, retry_sleeps = FakeClock(), []
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/search").mock(side_effect=[
            httpx.Response(429), httpx.Response(429), httpx.Response(200, json=SEARCH_BODY)])
        client = make_client(clock, retry_sleeps)
        results = client.search("Max Verstappen")
    assert results[0].entity_id == "E1"
    assert route.call_count == 3
    assert retry_sleeps == [0.5, 1.0]


def test_gives_up_after_three_retries():
    clock, retry_sleeps = FakeClock(), []
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/search").mock(return_value=httpx.Response(429))
        client = make_client(clock, retry_sleeps)
        with pytest.raises(QlooRateLimited):
            client.search("Max Verstappen")
    assert route.call_count == 4
    assert retry_sleeps == [0.5, 1.0, 2.0]
    assert client.budget.used == 4  # every HTTP attempt counts against the budget


def test_cache_hit_avoids_second_http_call_until_ttl_expires():
    clock = FakeClock()
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/search").mock(return_value=httpx.Response(200, json=SEARCH_BODY))
        client = make_client(clock, cache_ttl=60)
        client.search("Max Verstappen", types="urn:entity:person")
        client.search("Max Verstappen", types="urn:entity:person")
        assert route.call_count == 1
        assert client.budget.used == 1
        clock.t += 61
        client.search("Max Verstappen", types="urn:entity:person")
        assert route.call_count == 2


def test_disk_cache_survives_a_new_client(tmp_path):
    clock = FakeClock()
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/search").mock(return_value=httpx.Response(200, json=SEARCH_BODY))
        make_client(clock, cache=DiskCache(tmp_path, clock=clock.now)).search("Lando Norris")
        rerun = make_client(clock, cache=DiskCache(tmp_path, clock=clock.now))
        assert rerun.search("Lando Norris")[0].entity_id == "E1"
    assert route.call_count == 1
    for f in tmp_path.iterdir():
        assert FAKE_KEY not in f.read_text(encoding="utf-8")


def test_budget_guard_blocks_calls_when_exhausted():
    clock = FakeClock()
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/search").mock(return_value=httpx.Response(200, json=SEARCH_BODY))
        client = make_client(clock, budget=BudgetGuard(limit=2))
        client.search("one")
        client.search("two")
        with pytest.raises(BudgetExceeded, match="budget"):
            client.search("three")
        client.search("one")  # cached: still allowed
    assert route.call_count == 2


def test_budget_guard_persists_per_day(tmp_path):
    state = tmp_path / "budget.json"
    first = BudgetGuard(limit=5, state_path=state, today=lambda: date(2026, 10, 6))
    first.spend()
    first.spend()
    assert BudgetGuard(limit=5, state_path=state, today=lambda: date(2026, 10, 6)).used == 2
    assert BudgetGuard(limit=5, state_path=state, today=lambda: date(2026, 10, 7)).used == 0


def test_insights_params_and_limits():
    clock = FakeClock()
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/v2/insights").mock(return_value=httpx.Response(200, json=INSIGHTS_BODY))
        client = make_client(clock)
        client.insights_people(["S1", "S2"], ["C3", "C1", "C2", "C1"], exclude_ids=["C2"])
        params = route.calls.last.request.url.params
        assert params["filter.type"] == "urn:entity:person"
        assert params["signal.interests.entities"] == "S1,S2"
        assert params["filter.results.entities"] == "C1,C3"  # deduped, sorted, exclusion removed
        assert params["feature.explainability"] == "true"
        assert params["bias.trends"] == "off"
        assert params["take"] == "50"

        client.baseline_people(["C1", "C2"])
        params = route.calls.last.request.url.params
        assert "signal.interests.entities" not in params
        assert params["filter.results.entities"] == "C1,C2"

    with pytest.raises(ValueError):
        client.insights_people(["S1"], ["C1"], take=51)
    with pytest.raises(ValueError):
        client.baseline_people([f"C{i}" for i in range(101)])
    with pytest.raises(ValueError):
        client.insights_people([], ["C1"])


def test_api_key_never_leaks(caplog):
    caplog.set_level(logging.DEBUG)
    clock = FakeClock()
    with respx.mock(base_url=BASE_URL) as mock:
        # A server that echoes the key back in its error body.
        route = mock.get("/search").mock(side_effect=lambda req: httpx.Response(
            500, text=f"invalid key {req.headers['X-Api-Key']}"))
        client = make_client(clock)
        with pytest.raises(QlooHTTPError) as http_err:
            client.search("Max Verstappen")
        assert route.calls.last.request.headers["X-Api-Key"] == FAKE_KEY  # it IS sent...

        mock.get("/v2/insights").mock(side_effect=httpx.ConnectError("connection refused"))
        with pytest.raises(QlooError) as net_err:
            client.baseline_people(["C1"])

    for exc in (http_err.value, net_err.value):  # ...but never shown
        assert FAKE_KEY not in str(exc)
        assert FAKE_KEY not in repr(exc)
        assert FAKE_KEY not in "".join(traceback.format_exception(exc))
    assert http_err.value.status_code == 500
    assert "***" in str(http_err.value)
    assert FAKE_KEY not in repr(client)
    assert FAKE_KEY not in caplog.text
    assert all(FAKE_KEY not in k for k in client.cache.keys())


def test_fixtures_prime_the_cache_so_no_call_is_made():
    clock = FakeClock()
    fixtures = Path(__file__).parent / "fixtures"
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/search").mock(return_value=httpx.Response(500))
        client = make_client(clock)
        assert prime_from_fixtures(client.cache, [fixtures / "k1-max-verstappen.json"], "/search", 60) == 1
        hits = client.search("Max Verstappen", types="urn:entity:person", take=5)
    assert hits[0].disambiguation == "1997-09-30"
    assert route.call_count == 0


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("QLOO_API_KEY", raising=False)
    with pytest.raises(QlooError, match="QLOO_API_KEY is not set"):
        QlooClient()
