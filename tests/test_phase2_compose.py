"""Compose and docs for the optional Microsoft and routing pieces."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATHS = (
    ROOT / "docker-compose.yml",
    ROOT / "docker-compose.gpu-nvidia.yml",
    ROOT / "docker-compose.gpu-amd.yml",
)
ENV_EXAMPLE = ROOT / ".env.example"

FORWARDED = (
    "MICROSOFT_OAUTH_CLIENT_ID=${MICROSOFT_OAUTH_CLIENT_ID:-}",
    "MICROSOFT_OAUTH_CLIENT_SECRET=${MICROSOFT_OAUTH_CLIENT_SECRET:-}",
    "MICROSOFT_OAUTH_TENANT=${MICROSOFT_OAUTH_TENANT:-common}",
    "MICROSOFT_OAUTH_REDIRECT_URI=${MICROSOFT_OAUTH_REDIRECT_URI:-}",
    "WORK_ROUTING_ENABLED=${WORK_ROUTING_ENABLED:-auto}",
    "LOCAL_MODEL_BASE_URL=${LOCAL_MODEL_BASE_URL:-}",
    "LOCAL_MODEL_NAME=${LOCAL_MODEL_NAME:-}",
    "XAI_API_KEY=${XAI_API_KEY:-}",
    "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}",
    "MODEL_API_DAILY_BUDGET_USD=${MODEL_API_DAILY_BUDGET_USD:-0}",
    "MS365_MCP_URL=${MS365_MCP_URL:-}",
    "MS365_MCP_ALLOW_GRAPH_SEND=${MS365_MCP_ALLOW_GRAPH_SEND:-0}",
    "MS365_MCP_TOOL_MODE=${MS365_MCP_TOOL_MODE:-read-draft}",
)


def test_routing_and_microsoft_env_is_forwarded_on_every_compose_file():
    for path in COMPOSE_PATHS:
        environment = set(yaml.safe_load(path.read_text(encoding="utf-8"))["services"]["odysseus"]["environment"])
        missing = [item for item in FORWARDED if item not in environment]
        assert not missing, f"{path.name} missing {missing}"


def test_optional_profiles_are_not_in_the_default_stack():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert services["ms365-mcp"]["profiles"] == ["ms365"]
    assert services["litellm"]["profiles"] == ["litellm"]
    assert "softeria/ms-365-mcp-server" in services["ms365-mcp"]["image"]
    assert "--preset" in services["ms365-mcp"]["command"]
    assert "outlook" in " ".join(str(part) for part in services["ms365-mcp"]["command"])
    for name, service in services.items():
        profiles = service.get("profiles") or []
        if name in {"ms365-mcp", "litellm", "background-worker"}:
            continue
        assert "ms365" not in profiles
        assert "litellm" not in profiles


def test_phase2_docs_and_env_example_exist():
    setup = (ROOT / "docs" / "PHASE2_SETUP.md").read_text(encoding="utf-8")
    assert "MICROSOFT_OAUTH_CLIENT_ID" in setup
    assert "admin consent" in setup.lower() or "admin-consent" in setup.lower()
    assert "consumers" in setup
    assert "MODEL_API_DAILY_BUDGET_USD" in setup
    assert "LiteLLM" in setup
    fork = (ROOT / "docs" / "FORK_SETUP.md").read_text(encoding="utf-8")
    assert "PHASE2_SETUP.md" in fork
    example = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "# MICROSOFT_OAUTH_CLIENT_ID=" in example
    assert "# LOCAL_MODEL_BASE_URL=" in example
    assert "# XAI_API_KEY=" in example
    outlook = (ROOT / "website" / "email-outlook.md").read_text(encoding="utf-8")
    assert "does not support Microsoft" not in outlook
    assert "Connect with Microsoft" in outlook
