"""Local-first work routing: order, fallback, complexity, and the daily cap."""

import json
from datetime import datetime, timedelta, timezone

import pytest


_ROUTING_ENV = (
    "WORK_ROUTING_ENABLED",
    "WORK_ROUTING_AUTO_ESCALATE",
    "WORK_ROUTING_ESCALATE_CHARS",
    "WORK_ROUTING_ESCALATE_ROUNDS",
    "LOCAL_MODEL_BASE_URL",
    "LOCAL_MODEL_NAME",
    "LOCAL_MODEL_API_KEY",
    "ESCALATION_MODEL_BASE_URL",
    "ESCALATION_MODEL_NAME",
    "ESCALATION_MODEL_API_KEY",
    "XAI_API_KEY",
    "XAI_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "VISION_MODEL_BASE_URL",
    "VISION_MODEL_NAME",
    "VISION_MODEL_API_KEY",
    "CODING_MODEL_BASE_URL",
    "CODING_MODEL_NAME",
    "CODING_MODEL_API_KEY",
    "MODEL_API_DAILY_BUDGET_USD",
    "MODEL_API_CALL_ESTIMATE_USD",
    "MODEL_API_BUDGET_PATH",
)


@pytest.fixture
def routing_env(monkeypatch):
    for name in _ROUTING_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _local_candidate():
    return ("http://127.0.0.1:11434/v1/chat/completions", "qwen3.5:9b", {})


def test_routing_is_inactive_without_configuration(routing_env):
    from src.work_routing import active, apply_work_routing, public_status

    assert active() is False
    candidates = apply_work_routing([_local_candidate()], [{"role": "user", "content": "hi"}])
    assert candidates == [_local_candidate()]
    status = public_status()
    assert status["active"] is False
    assert status["litellm"] == "optional"


def test_openai_key_alone_does_not_activate(routing_env):
    from src.work_routing import active, escalation_route

    routing_env.setenv("OPENAI_API_KEY", "sk-test")
    assert active() is False
    assert escalation_route() is None


def test_explicit_enable_uses_openai(routing_env):
    from src.work_routing import active, escalation_route

    routing_env.setenv("WORK_ROUTING_ENABLED", "1")
    routing_env.setenv("OPENAI_API_KEY", "sk-test")
    assert active() is True
    route = escalation_route()
    assert route is not None
    assert route.model == "gpt-4.1"
    url, model, headers = route.candidate()
    assert "api.openai.com" in url
    assert model == "gpt-4.1"
    assert headers["Authorization"] == "Bearer sk-test"
    blob = json.dumps(route.descriptor())
    assert "sk-test" not in blob


def test_xai_key_is_the_default_escalation(routing_env):
    from src.work_routing import active, apply_work_routing, escalation_route, public_status

    routing_env.setenv("XAI_API_KEY", "xai-secret")
    routing_env.setenv("LOCAL_MODEL_BASE_URL", "http://127.0.0.1:11434/v1")
    routing_env.setenv("LOCAL_MODEL_NAME", "qwen3.5:9b")
    assert active() is True
    route = escalation_route()
    assert route.label == "xAI"
    assert route.model == "grok-4"
    url, _model, headers = route.candidate()
    assert url.startswith("https://api.x.ai/")
    assert headers["Authorization"] == "Bearer xai-secret"
    ordered = apply_work_routing(
        [_local_candidate()],
        [{"role": "user", "content": "What is 2+2?"}],
        kind="chat",
    )
    assert ordered[0][1] == "qwen3.5:9b"
    assert "api.x.ai" in ordered[1][0]
    status = public_status()
    assert status["escalation_label"] == "xAI"
    assert "xai-secret" not in json.dumps(status)


def test_anthropic_key_uses_anthropic_headers(routing_env):
    from src.work_routing import escalation_route

    routing_env.setenv("ANTHROPIC_API_KEY", "ant-secret")
    route = escalation_route()
    assert route.model == "claude-sonnet-4-5"
    url, _model, headers = route.candidate()
    assert "api.anthropic.com" in url
    assert headers["x-api-key"] == "ant-secret"
    assert "Authorization" not in headers


def test_explicit_phrase_promotes_the_api_model(routing_env):
    from src.work_routing import apply_work_routing

    routing_env.setenv("XAI_API_KEY", "xai-secret")
    ordered = apply_work_routing(
        [_local_candidate()],
        [{"role": "user", "content": "Please escalate this and rewrite the plan."}],
        kind="background",
    )
    assert "api.x.ai" in ordered[0][0]
    assert ordered[1][1] == "qwen3.5:9b"


def test_long_prompt_promotes_escalation(routing_env):
    from src.work_routing import apply_work_routing, complexity_reason

    routing_env.setenv("WORK_ROUTING_ESCALATE_CHARS", "20")
    routing_env.setenv("XAI_API_KEY", "xai-secret")
    messages = [{"role": "user", "content": "x" * 25}]
    assert complexity_reason(messages) is not None
    ordered = apply_work_routing([_local_candidate()], messages, kind="chat")
    assert ordered[0][1] == "grok-4"


def test_tool_rounds_promote_escalation(routing_env):
    from src.work_routing import complexity_reason

    routing_env.setenv("WORK_ROUTING_ESCALATE_ROUNDS", "2")
    messages = [
        {"role": "user", "content": "go"},
        {"role": "tool", "content": "a"},
        {"role": "tool", "content": "b"},
    ]
    assert "tool rounds" in (complexity_reason(messages) or "")


def test_selected_api_model_is_left_alone(routing_env):
    from src.work_routing import align_foreground

    routing_env.setenv("XAI_API_KEY", "xai-secret")
    routing_env.setenv("LOCAL_MODEL_BASE_URL", "http://127.0.0.1:11434/v1")
    routing_env.setenv("LOCAL_MODEL_NAME", "qwen3.5:9b")
    current = [("https://api.openai.com/v1/chat/completions", "gpt-4.1", {})]
    merged, _descriptors = align_foreground(
        current,
        [],
        [{"role": "user", "content": "[[escalate]] " + ("x" * 8000)}],
    )
    assert merged == [tuple(current[0])]


def test_coding_prompt_uses_the_coding_model_when_hard(routing_env):
    from src.work_routing import apply_work_routing

    routing_env.setenv("XAI_API_KEY", "xai-secret")
    routing_env.setenv("CODING_MODEL_BASE_URL", "https://api.openai.com/v1")
    routing_env.setenv("CODING_MODEL_NAME", "gpt-4.1")
    routing_env.setenv("CODING_MODEL_API_KEY", "sk-code")
    text = "Please debug this pytest failure.\n```\n" + ("def f():\n    pass\n" * 40)
    ordered = apply_work_routing(
        [_local_candidate()],
        [{"role": "user", "content": "[[escalate]]\n" + text}],
        kind="chat",
    )
    assert ordered[0][1] == "gpt-4.1"
    assert ordered[0][2]["Authorization"] == "Bearer sk-code"


def test_vision_kind_leads_with_the_vision_model(routing_env):
    from src.work_routing import extend_candidates

    routing_env.setenv("VISION_MODEL_BASE_URL", "http://127.0.0.1:11435/v1")
    routing_env.setenv("VISION_MODEL_NAME", "qwen-vl")
    routing_env.setenv("LOCAL_MODEL_BASE_URL", "http://127.0.0.1:11434/v1")
    routing_env.setenv("LOCAL_MODEL_NAME", "qwen3.5:9b")
    ordered = extend_candidates(
        [_local_candidate()],
        kind="vision",
        messages=[{"role": "user", "content": "describe"}],
    )
    assert ordered[0][1] == "qwen-vl"
    assert "11434" in ordered[1][0]


def test_budget_blocks_api_and_resets_the_next_utc_day(routing_env, tmp_path):
    from src.work_routing import apply_work_routing, budget_allows_api, budget_spent_usd, note_successful_call

    ledger = tmp_path / "budget.json"
    routing_env.setenv("MODEL_API_BUDGET_PATH", str(ledger))
    routing_env.setenv("MODEL_API_DAILY_BUDGET_USD", "0.01")
    routing_env.setenv("XAI_API_KEY", "xai-secret")
    routing_env.setenv("LOCAL_MODEL_BASE_URL", "http://127.0.0.1:11434/v1")
    routing_env.setenv("LOCAL_MODEL_NAME", "qwen3.5:9b")

    note_successful_call("https://api.x.ai/v1/chat/completions", "grok-4")
    assert budget_spent_usd() > 0
    assert budget_allows_api() is False
    ordered = apply_work_routing(
        [_local_candidate()],
        [{"role": "user", "content": "[[escalate]]"}],
        kind="chat",
    )
    assert [item[1] for item in ordered] == ["qwen3.5:9b"]

    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    ledger.write_text(json.dumps({"date": yesterday, "spent_usd": 50}), encoding="utf-8")
    assert budget_spent_usd() == 0
    assert budget_allows_api() is True


def test_zero_budget_does_not_record_spend(routing_env, tmp_path):
    from src.work_routing import note_successful_call

    ledger = tmp_path / "budget.json"
    routing_env.setenv("MODEL_API_BUDGET_PATH", str(ledger))
    routing_env.setenv("MODEL_API_DAILY_BUDGET_USD", "0")
    note_successful_call("https://api.x.ai/v1/chat/completions", "grok-4")
    assert not ledger.exists()


def test_litellm_is_not_required(routing_env):
    from src.work_routing import active

    routing_env.setenv("LOCAL_MODEL_BASE_URL", "http://host.docker.internal:11434/v1")
    routing_env.setenv("LOCAL_MODEL_NAME", "qwen3.5:9b")
    routing_env.setenv("XAI_API_KEY", "xai-secret")
    assert active() is True
