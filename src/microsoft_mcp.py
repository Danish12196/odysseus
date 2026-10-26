"""Optional Softeria Microsoft 365 MCP server registration.

Mail send stays on the IMAP/SMTP approval path. This module hides Graph send
tools from the model and, when ``MS365_MCP_URL`` or ``MS365_MCP_SERVERS`` is
set, upserts one HTTP MCP row per account so Odysseus's existing OAuth client
can sign in. The container itself is a Compose profile and is not started by
a plain ``docker compose up``.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

_SERVER_RE = re.compile(r"[^a-z0-9_-]")
_SEND_FRAGMENTS = (
    "send-mail",
    "send-draft",
    "send-shared",
    "reply-mail",
    "reply-all",
    "forward-mail",
    "delete-mail",
    "delete-shared",
    "move-mail",
)
_READ_OR_DRAFT = re.compile(
    r"^(list|get|search|find|read|download)-|draft|login|logout|verify-login|search-tools|list-tools|load-tool",
    re.IGNORECASE,
)


def is_ms365_server(server_id: str) -> bool:
    text = str(server_id or "")
    return text == "ms365" or text.startswith("ms365-")


def normalize_server_id(raw: str) -> str:
    text = _SERVER_RE.sub("", str(raw or "").strip().lower())
    if not text:
        return ""
    if not is_ms365_server(text):
        text = "ms365-" + text
    return text[:64]


def graph_send_allowed() -> bool:
    return str(os.getenv("MS365_MCP_ALLOW_GRAPH_SEND") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def is_mail_send_tool(name: str) -> bool:
    """True for Graph tools that deliver, delete, or move mail.

    Draft tools stay available. ``send-draft-message`` is a send. Creating a
    calendar event is not a mail send.
    """

    lowered = str(name or "").strip().lower()
    if not lowered or ("draft" in lowered and "send" not in lowered):
        return False
    return any(fragment in lowered for fragment in _SEND_FRAGMENTS) or (
        lowered.startswith("send-") and ("mail" in lowered or "message" in lowered)
    )


def tool_mode() -> str:
    mode = str(os.getenv("MS365_MCP_TOOL_MODE") or "read-draft").strip().lower()
    if mode not in {"read-draft", "all"}:
        return "read-draft"
    return mode


def max_tools() -> int:
    try:
        return max(1, int(os.getenv("MS365_MCP_MAX_TOOLS") or "24"))
    except ValueError:
        return 24


def kept_for_small_model(name: str) -> bool:
    if is_mail_send_tool(name):
        return False
    return bool(_READ_OR_DRAFT.search(str(name or "")))


def extra_disabled_tools(server_id: str, names: Iterable[str]) -> set[str]:
    """Tool names to hide on a Microsoft MCP server.

    Sends are always hidden unless ``MS365_MCP_ALLOW_GRAPH_SEND`` is set.
    When the server still advertises more than ``MS365_MCP_MAX_TOOLS`` names,
    ``read-draft`` mode keeps list/get/search/read/draft tools and discovery
    helpers so a 9B model is not handed the full Graph surface.
    """

    if not is_ms365_server(server_id):
        return set()
    listed = [str(name) for name in names if name]
    disabled = set()
    if not graph_send_allowed():
        disabled.update(name for name in listed if is_mail_send_tool(name))
    if tool_mode() == "read-draft" and len(listed) > max_tools():
        disabled.update(name for name in listed if not kept_for_small_model(name))
    return disabled


def send_block_reason(server_id: str, tool_name: str) -> Optional[str]:
    """Runtime refusal for a Graph send, even if the model already knows the name."""

    if graph_send_allowed() or not is_ms365_server(server_id):
        return None
    if not is_mail_send_tool(tool_name):
        return None
    return (
        "Microsoft Graph send is disabled. Draft the message with draft_email "
        "or send it with send_email so it goes through approval on the "
        "connected Outlook mailbox. Nothing was sent."
    )


def configured_servers() -> list[tuple[str, str]]:
    """``(server id, mcp url)`` pairs from the environment.

    ``MS365_MCP_SERVERS=work=http://ms365-mcp:3000/mcp,personal=http://ms365-home:3000/mcp``
    registers one Odysseus MCP row per Microsoft login. A single
    ``MS365_MCP_URL`` is the one-account shortcut.
    """

    found: list[tuple[str, str]] = []
    raw = str(os.getenv("MS365_MCP_SERVERS") or "").strip()
    if raw:
        for part in raw.split(","):
            if "=" not in part:
                continue
            name, url = part.split("=", 1)
            server_id = normalize_server_id(name)
            url = url.strip()
            if server_id and url.startswith(("http://", "https://")):
                found.append((server_id, url))
    if found:
        return found
    single = str(os.getenv("MS365_MCP_URL") or "").strip()
    if single.startswith(("http://", "https://")):
        return [("ms365", single)]
    return []


def compose_command(*, org_mode: bool = True, preset: str = "outlook", discovery: bool = True, read_only: bool = False) -> list[str]:
    """Arguments for the Softeria image. HTTP mode, small preset, optional org mode.

    ``--read-only`` disables drafts as well as sends, so the default leaves
    writes to the server and Odysseus blocks send tools itself.
    """

    command = ["--http", "0.0.0.0:3000", "--preset", preset or "outlook"]
    if org_mode:
        command.append("--org-mode")
    if discovery:
        command.append("--discovery")
    if read_only:
        command.append("--read-only")
    return command


def sync_mcp_servers() -> list[str]:
    """Upsert HTTP MCP rows for the configured Microsoft servers.

    Tokens are not touched. A later connect uses the existing MCP OAuth
    client. Skipped under pytest unless explicitly requested.
    """

    if os.getenv("PYTEST_CURRENT_TEST") and str(os.getenv("MS365_MCP_SYNC_IN_TESTS") or "").lower() not in {
        "1", "true", "yes", "on",
    }:
        return []
    servers = configured_servers()
    if not servers:
        return []
    try:
        from core.database import McpServer, SessionLocal
    except Exception:
        logger.debug("Microsoft MCP sync unavailable", exc_info=True)
        return []
    db = SessionLocal()
    ids: list[str] = []
    try:
        for server_id, url in servers:
            row = db.get(McpServer, server_id)
            if row is None:
                row = McpServer(id=server_id, name=f"Microsoft 365 ({server_id})", transport="http")
                db.add(row)
            row.name = row.name or f"Microsoft 365 ({server_id})"
            row.transport = "http"
            row.url = url
            row.is_enabled = True
            ids.append(server_id)
        db.commit()
    except Exception:
        logger.warning("Could not register Microsoft MCP servers", exc_info=True)
        return []
    finally:
        db.close()
    return ids
