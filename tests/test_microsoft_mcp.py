"""Microsoft 365 MCP: small tool surface, blocked sends, several accounts."""

import pytest


@pytest.fixture
def mcp_env(monkeypatch):
    for name in (
        "MS365_MCP_URL",
        "MS365_MCP_SERVERS",
        "MS365_MCP_ALLOW_GRAPH_SEND",
        "MS365_MCP_MAX_TOOLS",
        "MS365_MCP_TOOL_MODE",
        "MS365_MCP_SYNC_IN_TESTS",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_compose_command_is_outlook_discovery_org_mode():
    from src.microsoft_mcp import compose_command

    command = compose_command()
    assert command[:4] == ["--http", "0.0.0.0:3000", "--preset", "outlook"]
    assert "--org-mode" in command
    assert "--discovery" in command
    assert "--read-only" not in command
    assert "--read-only" in compose_command(read_only=True)


def test_send_is_blocked_and_drafts_stay(mcp_env):
    from src.microsoft_mcp import extra_disabled_tools, is_mail_send_tool, send_block_reason

    names = [
        "list-mail-messages",
        "get-mail-message",
        "search-mail",
        "create-draft-message",
        "send-mail",
        "send-draft-message",
        "reply-mail",
        "delete-mail",
        "list-calendar-events",
        "create-calendar-event",
        "search-contacts",
        "search-tools",
    ]
    assert is_mail_send_tool("send-mail")
    assert is_mail_send_tool("send-draft-message")
    assert not is_mail_send_tool("create-draft-message")
    assert not is_mail_send_tool("create-calendar-event")
    disabled = extra_disabled_tools("ms365", names)
    assert "send-mail" in disabled
    assert "create-draft-message" not in disabled
    assert "list-calendar-events" not in disabled
    reason = send_block_reason("ms365", "send-mail")
    assert reason and "approval" in reason
    assert send_block_reason("other", "send-mail") is None
    mcp_env.setenv("MS365_MCP_ALLOW_GRAPH_SEND", "1")
    assert send_block_reason("ms365", "send-mail") is None


def test_tool_cap_hides_the_long_tail(mcp_env):
    from src.microsoft_mcp import extra_disabled_tools

    mcp_env.setenv("MS365_MCP_MAX_TOOLS", "3")
    names = [
        "list-mail-messages",
        "create-draft-message",
        "search-tools",
        "update-user-profile",
        "create-team",
    ]
    disabled = extra_disabled_tools("ms365-work", names)
    assert "update-user-profile" in disabled
    assert "create-team" in disabled
    assert "list-mail-messages" not in disabled
    assert "create-draft-message" not in disabled


def test_several_server_urls(mcp_env):
    from src.microsoft_mcp import configured_servers

    mcp_env.setenv(
        "MS365_MCP_SERVERS",
        "work=http://ms365-mcp:3000/mcp, personal=http://ms365-home:3000/mcp",
    )
    assert configured_servers() == [
        ("ms365-work", "http://ms365-mcp:3000/mcp"),
        ("ms365-personal", "http://ms365-home:3000/mcp"),
    ]
    mcp_env.delenv("MS365_MCP_SERVERS")
    mcp_env.setenv("MS365_MCP_URL", "http://ms365-mcp:3000/mcp")
    assert configured_servers() == [("ms365", "http://ms365-mcp:3000/mcp")]


@pytest.mark.asyncio
async def test_call_tool_refuses_graph_send_without_calling_the_session(mcp_env):
    from src.mcp_manager import McpManager

    manager = McpManager()
    session = type("Session", (), {"call_tool": lambda *a, **k: (_ for _ in ()).throw(AssertionError("session called"))})()
    manager._sessions["ms365"] = session
    result = await manager.call_tool("mcp__ms365__send-mail", {"body": "hi"})
    assert result["approval_required"] is True
    assert result["exit_code"] == 1
    assert "send_email" in result["error"]


def test_send_tool_is_an_external_side_effect():
    from src.tool_capabilities import ToolEffect, capabilities_for_tool

    caps = capabilities_for_tool("mcp__ms365__send-mail")
    assert ToolEffect.EXTERNAL_SIDE_EFFECT in caps.effects
    assert caps.known is True
    draft = capabilities_for_tool("mcp__ms365__create-draft-message")
    assert draft.known is False
