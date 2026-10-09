import asyncio
import json

from backend.agent.agent import guard_narration, run_repair
from tests.fakes import make_tools

REQ = {"seed_ids": ["S1", "S2", "S3"], "sport": "f1", "window": "evenings", "rejected_ids": ["D1"],
       "reason": "not_feeling_it", "allow_sport_switch": False}


def runner_calling_swap(narration: str):
    async def runner(prompt, funcs, model):
        tool = next(f for f in funcs if f.__name__ == "swap_pick")
        tool(sport="f1", rejected_ids=["D1"], reason="not_feeling_it", window="evenings")
        return narration
    return runner


async def failing_runner(prompt, funcs, model):
    raise ConnectionError("gemini unreachable")


async def slow_runner(prompt, funcs, model):
    await asyncio.sleep(5)
    return "too late"


def featured_and_alternates(result):
    p = result["payload"]
    return [c["entity_id"] for c in [p["featured"], *p["alternates"]] if c]


def test_agent_narrates_from_the_tool_result(tmp_path):
    tools = make_tools(tmp_path)
    out = asyncio.run(run_repair(tools, REQ, runner_calling_swap("Try Driver Two instead: #1 of 3 for your taste.")))
    assert out["narration_available"] and out["narration"].startswith("Try Driver Two")
    assert [e["tool"] for e in out["trace"]] == ["swap_pick", "agent (gemini-2.5-flash)"]
    assert "D1" not in featured_and_alternates(out)
    assert out["payload"]["featured"]["entity_id"] == "D2"


def test_gemini_failure_falls_back_to_the_deterministic_swap(tmp_path):
    out = asyncio.run(run_repair(make_tools(tmp_path), REQ, failing_runner))
    assert not out["narration_available"] and out["narration"] is None
    assert out["narration_note"] == "ConnectionError"
    assert [e["tool"] for e in out["trace"]] == ["agent (gemini-2.5-flash)", "swap_pick (deterministic fallback)"]
    assert out["payload"]["featured"]["entity_id"] == "D2" and "D1" not in featured_and_alternates(out)


def test_agent_timeout_falls_back(tmp_path):
    out = asyncio.run(run_repair(make_tools(tmp_path), REQ, slow_runner, timeout_s=0.05))
    assert out["narration"] is None and out["narration_note"].startswith("timeout")
    assert out["payload"]["featured"] is not None


def test_guardrails_drop_scores_causes_and_unreturned_athletes():
    allowed, roster = {"Driver Two"}, {"Driver One", "Driver Two", "Driver Three"}
    assert guard_narration("Driver Two scores 0.742 for you.", allowed, roster) == (None, "contained a score")
    assert guard_narration("Valorant caused this pick: Driver Two.", allowed, roster)[1] == "causal claim"
    assert guard_narration("Driver Three is also great.", allowed, roster)[1].startswith("mentioned an athlete")
    long = guard_narration("Driver Two " + "word " * 80, allowed, roster)[0]
    assert len(long.split()) == 60 and long.endswith("…")


def test_trace_contains_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("QLOO_API_KEY", "fake-secret-qloo-123")
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-secret-google-456")
    out = asyncio.run(run_repair(make_tools(tmp_path), REQ, runner_calling_swap("Try Driver Two.")))
    dumped = json.dumps(out)
    assert "fake-secret" not in dumped
    assert all(set(e) == {"tool", "args", "ms", "ok", "error"} for e in out["trace"])
