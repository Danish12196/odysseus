"""Env escalation becomes the teacher when Settings has no teacher model."""

import json

import pytest


@pytest.fixture
def quiet_env(monkeypatch):
    for name in (
        "WORK_ROUTING_ENABLED",
        "WORK_ROUTING_AUTO_ESCALATE",
        "XAI_API_KEY",
        "XAI_MODEL",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "LOCAL_MODEL_BASE_URL",
        "ESCALATION_MODEL_BASE_URL",
        "ESCALATION_MODEL_NAME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: default,
    )
    return monkeypatch


async def _collect(monkeypatch, **kwargs):
    from src.teacher_escalation import run_teacher_inline

    async def fake_stream(*_args, **_kwargs):
        yield 'data: {"type": "text", "delta": "Teacher reply"}\n\n'
        yield "data: [DONE]\n\n"

    async def no_skill(*_args, **_kwargs):
        return "NO_SKILL"

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", fake_stream)
    monkeypatch.setattr("src.teacher_escalation._call_teacher", no_skill)
    events = []
    async for event in run_teacher_inline(
        student_endpoint_url="http://127.0.0.1:8081/v1",
        student_messages=kwargs.get("messages") or [{"role": "user", "content": "status please"}],
        student_tool_events=[],
        student_reply=kwargs.get("reply", "done"),
        owner="alice",
    ):
        events.append(event)
    return events


@pytest.mark.asyncio
async def test_give_up_uses_the_env_model_when_teacher_settings_are_off(quiet_env, monkeypatch):
    quiet_env.setenv("XAI_API_KEY", "xai-secret")
    events = await _collect(monkeypatch, reply="I can't do this request")
    takeover = next(json.loads(event[6:]) for event in events if "teacher_takeover" in event)
    assert takeover["teacher_model"] == "grok-4"
    assert takeover["type"] == "teacher_takeover"


@pytest.mark.asyncio
async def test_no_takeover_without_an_escalation_route(quiet_env, monkeypatch):
    events = await _collect(monkeypatch, reply="I can't do this request")
    assert events == []


@pytest.mark.asyncio
async def test_complexity_triggers_takeover(quiet_env, monkeypatch):
    quiet_env.setenv("XAI_API_KEY", "xai-secret")
    quiet_env.setenv("WORK_ROUTING_ESCALATE_CHARS", "10")
    events = await _collect(
        monkeypatch,
        messages=[{"role": "user", "content": "x" * 40}],
        reply="Here is a normal answer.",
    )
    assert any("teacher_takeover" in event for event in events)


@pytest.mark.asyncio
async def test_auto_escalate_off_skips_complexity(quiet_env, monkeypatch):
    quiet_env.setenv("XAI_API_KEY", "xai-secret")
    quiet_env.setenv("WORK_ROUTING_AUTO_ESCALATE", "0")
    quiet_env.setenv("WORK_ROUTING_ESCALATE_CHARS", "10")
    events = await _collect(
        monkeypatch,
        messages=[{"role": "user", "content": "x" * 40}],
        reply="Here is a normal answer.",
    )
    assert events == []
