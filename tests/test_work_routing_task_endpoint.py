"""Background task calls pick up the work-routing candidate list."""

import pytest


@pytest.fixture
def routing_env(monkeypatch):
    for name in (
        "WORK_ROUTING_ENABLED",
        "XAI_API_KEY",
        "XAI_MODEL",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "LOCAL_MODEL_BASE_URL",
        "LOCAL_MODEL_NAME",
        "ESCALATION_MODEL_BASE_URL",
        "ESCALATION_MODEL_NAME",
        "MODEL_API_DAILY_BUDGET_USD",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.asyncio
async def test_background_call_appends_escalation(routing_env, monkeypatch):
    from src.task_endpoint import task_llm_call_async

    routing_env.setenv("XAI_API_KEY", "xai-secret")
    seen = {}

    async def fake_call(candidates, messages=None, **kwargs):
        seen["candidates"] = list(candidates)
        seen["workload"] = kwargs.get("workload")
        return "ok"

    async def quiet(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "src.task_endpoint.resolve_task_candidates",
        lambda **_kwargs: [("http://127.0.0.1:11434/v1/chat/completions", "qwen3.5:9b", {})],
    )
    monkeypatch.setattr("src.task_endpoint.llm_call_async_with_fallback", fake_call)
    monkeypatch.setattr("src.task_endpoint.wait_for_interactive_quiet", quiet)

    result = await task_llm_call_async([{"role": "user", "content": "status"}])
    assert result == "ok"
    assert seen["workload"] == "background"
    assert seen["candidates"][0][1] == "qwen3.5:9b"
    assert "api.x.ai" in seen["candidates"][1][0]


@pytest.mark.asyncio
async def test_background_call_is_unchanged_when_routing_is_off(routing_env, monkeypatch):
    from src.task_endpoint import task_llm_call_async

    seen = {}

    async def fake_call(candidates, messages=None, **kwargs):
        seen["candidates"] = list(candidates)
        return "ok"

    async def quiet(*_args, **_kwargs):
        return None

    original = [("http://127.0.0.1:11434/v1/chat/completions", "qwen3.5:9b", {})]
    monkeypatch.setattr("src.task_endpoint.resolve_task_candidates", lambda **_kwargs: list(original))
    monkeypatch.setattr("src.task_endpoint.llm_call_async_with_fallback", fake_call)
    monkeypatch.setattr("src.task_endpoint.wait_for_interactive_quiet", quiet)

    await task_llm_call_async([{"role": "user", "content": "status"}])
    assert seen["candidates"] == original
