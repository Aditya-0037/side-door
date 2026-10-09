import json

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.presets import load_presets
from tests.fakes import FakeLLM, make_tools
from tests.test_agent import failing_runner, runner_calling_swap

SEED_IDS = ["S1", "S2", "S3"]


@pytest.fixture
def client(tmp_path):
    app = create_app(tools=make_tools(tmp_path, FakeLLM()), runner=runner_calling_swap("Try Driver Two."),
                     precomputed_dir=tmp_path / "precomputed", rate_limit=1000)
    return TestClient(app)


def floats_outside_debug(node, path="") -> list[str]:
    """Every float in the payload except under `debug` (UI copy must never show raw scores)."""
    if isinstance(node, dict):
        return [p for k, v in node.items() if k != "debug" for p in floats_outside_debug(v, f"{path}.{k}")]
    if isinstance(node, list):
        return [p for i, v in enumerate(node) for p in floats_outside_debug(v, f"{path}[{i}]")]
    return [path] if isinstance(node, float) else []


def test_health_reports_key_presence_only(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok" and set(body["keys_present"]) == {"qloo", "gemini", "football_data"}
    assert all(isinstance(v, bool) for v in body["keys_present"].values())


def test_presets_are_six_with_indian_tastes(client):
    presets = client.get("/api/presets").json()["presets"]
    assert len(presets) == 6
    assert sum(p["id"] in {"desi-anime-gamer", "punjabi-pop-bgmi"} for p in presets) == 2
    assert all(len(p["seeds"]) == 3 and p["seeds"][0]["entity_id"] for p in presets)


def test_resolve(client):
    seeds = client.post("/api/resolve", json={"items": [{"text": "rocky", "type": "movie"}]}).json()["seeds"]
    assert seeds[0]["found"] and seeds[0]["matched_as"] == "Rocky — film, 2005"


def test_side_door_payload_has_no_raw_scores_in_ui_fields(client):
    body = client.post("/api/side-door", json={"seed_ids": SEED_IDS, "sport": "f1", "window": "late_night_ok"}).json()
    assert body["featured"]["name"] == "Driver One" and len(body["alternates"]) == 2
    assert body["featured"]["next_event"]["fits_window"]
    assert body["featured"]["clips"][0]["kind"] == "athlete_channel"
    assert body["unknown"]["athletes"] == [{"entity_id": "D5", "name": "Rookie Five", "team": "Team C"}]
    assert body["unknown"]["title"].startswith("New on the grid")
    assert floats_outside_debug(body) == []
    assert "0." not in json.dumps([body["featured"]["why_line"], *[a["why_line"] for a in body["alternates"]]])
    assert body["featured"]["debug"]["final"] > 0  # raw values exist, but only for debugging
    assert [s["step"] for s in body["trace"]][0].startswith("rank")


def test_side_door_serves_presets_from_precomputed_files(tmp_path):
    preset = load_presets()[0]
    seed_ids = [s["entity_id"] for s in preset["seeds"]]
    directory = tmp_path / "precomputed"
    directory.mkdir()
    tools = make_tools(tmp_path)
    live = TestClient(create_app(tools=tools, precomputed_dir=tmp_path / "none", rate_limit=1000)).post(
        "/api/side-door", json={"seed_ids": SEED_IDS, "sport": "f1"}).json()
    (directory / f"{preset['id']}__f1.json").write_text(json.dumps({**live, "seeds": []}), encoding="utf-8")

    calls_before = tools.qloo.budget.used
    app = create_app(tools=tools, precomputed_dir=directory, rate_limit=1000)
    body = TestClient(app).post("/api/side-door", json={"seed_ids": seed_ids, "sport": "f1",
                                                        "window": "weekends_only"}).json()
    assert body["precomputed"] and body["window"] == "weekends_only"
    assert body["featured"]["next_event"]["kind"] == "qualifying"  # events re-attached for this window
    assert tools.qloo.budget.used == calls_before  # 0 live calls


def test_without_qloo_drops_names_not_on_the_roster(client):
    body = client.post("/api/without-qloo", json={"seed_names": ["Spirited Away"], "sport": "f1"}).json()
    assert body["available"] and body["featured"] == "Driver Three" and body["dropped"] == ["Ayrton Senna"]


def test_llm_down_still_returns_picks(tmp_path):
    tools = make_tools(tmp_path, FakeLLM(error=TimeoutError()))
    c = TestClient(create_app(tools=tools, runner=failing_runner, precomputed_dir=tmp_path, rate_limit=1000))
    picks = c.post("/api/side-door", json={"seed_ids": SEED_IDS, "sport": "f1"}).json()
    assert picks["featured"]["name"] == "Driver One"
    assert c.post("/api/without-qloo", json={"seed_names": ["Rocky"], "sport": "f1"}).json()["available"] is False
    repair = c.post("/api/repair", json={"seed_ids": SEED_IDS, "sport": "f1", "rejected_ids": ["D1"],
                                         "reason": "not_feeling_it"}).json()
    assert repair["narration_available"] is False and repair["payload"]["featured"]["name"] == "Driver Two"


def test_repair_never_returns_rejected_ids(client):
    for reason, window in (("not_feeling_it", "evenings"), ("bad_time", "late_night_ok")):
        body = client.post("/api/repair", json={"seed_ids": SEED_IDS, "sport": "f1", "window": window,
                                                "rejected_ids": ["D1", "D3"], "reason": reason}).json()
        shown = [c["entity_id"] for c in [body["payload"]["featured"], *body["payload"]["alternates"]] if c]
        assert shown and not {"D1", "D3"} & set(shown)
        assert floats_outside_debug(body) == []


def test_insights_aggregate(client):
    body = client.get("/api/insights").json()
    assert body["personas"] == 20 and sum(body["featured_overall"].values()) == 20
    assert {d["kind"] for d in body["doors"]} == {"movie", "artist", "videogame"}


def test_rate_limit(tmp_path):
    c = TestClient(create_app(tools=make_tools(tmp_path), precomputed_dir=tmp_path, rate_limit=2))
    codes = [c.get("/api/presets").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert c.get("/api/health").status_code == 200  # health is never rate-limited


def test_bad_input_is_rejected(client):
    assert client.post("/api/side-door", json={"seed_ids": [], "sport": "f1"}).status_code == 422
    assert client.post("/api/side-door", json={"seed_ids": ["S1"], "sport": "nba"}).status_code == 422
    bad_reject = client.post("/api/repair", json={"seed_ids": SEED_IDS, "sport": "f1", "rejected_ids": ["XX"],
                                                  "reason": "not_feeling_it"})
    assert bad_reject.status_code == 422 and "not on the Formula 1 roster" in bad_reject.json()["detail"]
