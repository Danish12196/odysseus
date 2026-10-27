"""Render event payloads into a task prompt without treating them as instructions."""

from __future__ import annotations

import json
import re
from typing import Any

_FIELD = re.compile(r"\{\{\s*event(?:\.([A-Za-z0-9_.-]+))?\s*\}\}")
_MAX_FIELD = 4000
_MAX_PAYLOAD = 16000


def dump_payload(payload: Any) -> str:
    if payload is None:
        return ""
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        text = str(payload)
    return text[:_MAX_PAYLOAD]


def _lookup(payload: Any, path: str) -> str:
    current = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return ""
    if isinstance(current, (dict, list)):
        try:
            return json.dumps(current, ensure_ascii=False, default=str)[:_MAX_FIELD]
        except Exception:
            return str(current)[:_MAX_FIELD]
    return str(current)[:_MAX_FIELD]


def render_event_prompt(prompt: str | None, payload: Any) -> str:
    """Substitute ``{{event}}`` and ``{{event.field}}`` placeholders.

    Missing fields become empty strings. The caller should also attach the
    raw payload as untrusted context so tool gates stay armed.
    """
    text = prompt or ""
    if payload is None or "{{" not in text:
        return text

    def replace(match: re.Match) -> str:
        path = match.group(1)
        if not path:
            return dump_payload(payload)[:_MAX_FIELD]
        return _lookup(payload, path)

    return _FIELD.sub(replace, text)


def payload_for_task(task) -> Any:
    """Load the last event/webhook payload stored on a task, if any."""
    if task is None:
        return None
    trigger = str(getattr(task, "trigger_type", "") or "")
    if trigger not in {"event", "webhook"}:
        return None
    raw = getattr(task, "event_payload_json", None)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": str(raw)[:_MAX_FIELD]}
