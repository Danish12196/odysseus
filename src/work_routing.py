"""One routing policy for chat, utility, background, vision, coding, and escalation.

Odysseus already speaks to Ollama, llama.cpp, OpenAI, Anthropic, xAI, and
other OpenAI-compatible servers through ``ModelEndpoint`` rows. This module
does not replace that. It reads optional environment variables and builds an
ordered candidate list on top of whatever the caller already resolved:

* easy work stays on the local model
* a failed call falls through to the next candidate
* a request that is long, multi-step, or explicitly marked hard is sent to
  the escalation model first
* a daily USD cap can keep API calls from running once it is spent

An optional LiteLLM container is only a convenience if you want one
OpenAI-compatible URL in front of several keys. Point
``ESCALATION_MODEL_BASE_URL`` at it. Nothing here requires the proxy.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

WORK_KINDS = ("chat", "utility", "background", "vision", "coding", "escalation")

_LOCAL_ID = "work-routing-local"
_ESCALATION_ID = "work-routing-escalation"
_VISION_ID = "work-routing-vision"
_CODING_ID = "work-routing-coding"

_CODE_RE = re.compile(
    r"(```|\btraceback\b|\bstack trace\b|\brefactor\b|\bcompile\b|"
    r"\bunit test\b|\bpytest\b|\bdebug this\b)",
    re.IGNORECASE,
)
_ESCALATE_PHRASES = (
    "[[escalate]]",
    "use the stronger model",
    "escalate this",
)

_BUDGET_LOCK = threading.Lock()


@dataclass(frozen=True)
class ModelRoute:
    """A non-secret description plus the material needed to call a model."""

    endpoint_id: str
    label: str
    base_url: str
    model: str
    api_key: str = ""
    kind: str = "auto"  # local or api

    def candidate(self) -> tuple:
        from src.endpoint_resolver import build_chat_url, build_headers

        url = build_chat_url(self.base_url)
        headers = build_headers(self.api_key or None, self.base_url)
        return (url, self.model, headers)

    def descriptor(self) -> dict:
        from src.endpoint_resolver import endpoint_cost_tracked

        url = self.candidate()[0]
        return {
            "endpoint_id": self.endpoint_id,
            "endpoint_label": self.label,
            "endpoint_cost_tracked": endpoint_cost_tracked(url, self.kind),
        }


def _env(name: str) -> str:
    return str(os.getenv(name) or "").strip()


def _flag(name: str, default: str = "") -> str:
    return _env(name).lower() or default


def _disabled(value: str) -> bool:
    return value in {"0", "false", "no", "off"}


def _enabled(value: str) -> bool:
    return value in {"1", "true", "yes", "on"}


def _kind_for(base_url: str, *, budgeted: bool) -> str:
    """Local servers are free. Escalation is budgeted even via a private proxy."""

    if budgeted:
        return "api"
    try:
        from src.endpoint_resolver import endpoint_cost_tracked

        if endpoint_cost_tracked(base_url, "auto"):
            return "api"
    except Exception:
        return "api"
    return "local"


def _spec_from(
    prefix: str,
    endpoint_id: str,
    label: str,
    *,
    budgeted: bool,
) -> Optional[ModelRoute]:
    base = _env(f"{prefix}_BASE_URL")
    model = _env(f"{prefix}_NAME")
    if not base or not model:
        return None
    return ModelRoute(
        endpoint_id=endpoint_id,
        label=label,
        base_url=base,
        model=model,
        api_key=_env(f"{prefix}_API_KEY"),
        kind=_kind_for(base, budgeted=budgeted),
    )


def local_route() -> Optional[ModelRoute]:
    return _spec_from("LOCAL_MODEL", _LOCAL_ID, "Local model", budgeted=False)


def vision_route() -> Optional[ModelRoute]:
    return _spec_from("VISION_MODEL", _VISION_ID, "Vision model", budgeted=False)


def coding_route() -> Optional[ModelRoute]:
    return _spec_from("CODING_MODEL", _CODING_ID, "Coding model", budgeted=False)


def escalation_route() -> Optional[ModelRoute]:
    """The strong model, if one is configured.

    Explicit ``ESCALATION_MODEL_*`` wins. Otherwise the first of an xAI key,
    an Anthropic key, or (only when routing is explicitly enabled) an OpenAI
    key. OpenAI is not implicit: that variable is already used for other
    Odysseus features.
    """

    explicit_base = _env("ESCALATION_MODEL_BASE_URL")
    explicit_model = _env("ESCALATION_MODEL_NAME")
    if explicit_base and explicit_model:
        key = (
            _env("ESCALATION_MODEL_API_KEY")
            or _env("XAI_API_KEY")
            or _env("ANTHROPIC_API_KEY")
            or _env("OPENAI_API_KEY")
        )
        return ModelRoute(
            _ESCALATION_ID, "Escalation model", explicit_base, explicit_model, key, "api",
        )
    if _env("XAI_API_KEY"):
        return ModelRoute(
            _ESCALATION_ID,
            "xAI",
            "https://api.x.ai/v1",
            _env("XAI_MODEL") or "grok-4",
            _env("XAI_API_KEY"),
            "api",
        )
    if _env("ANTHROPIC_API_KEY"):
        return ModelRoute(
            _ESCALATION_ID,
            "Anthropic",
            "https://api.anthropic.com",
            _env("ANTHROPIC_MODEL") or "claude-sonnet-4-5",
            _env("ANTHROPIC_API_KEY"),
            "api",
        )
    if _enabled(_flag("WORK_ROUTING_ENABLED")) and _env("OPENAI_API_KEY"):
        return ModelRoute(
            _ESCALATION_ID,
            "OpenAI",
            "https://api.openai.com/v1",
            _env("OPENAI_MODEL") or "gpt-4.1",
            _env("OPENAI_API_KEY"),
            "api",
        )
    return None


def active() -> bool:
    """True when this policy should change candidate order.

    ``auto`` (the default) turns on when a local model or an xAI/Anthropic
    escalation target is configured. ``OPENAI_API_KEY`` alone does not.
    ``WORK_ROUTING_ENABLED=0`` forces the historical resolvers.
    """

    flag = _flag("WORK_ROUTING_ENABLED", "auto")
    if _disabled(flag):
        return False
    if _enabled(flag):
        return bool(local_route() or vision_route() or coding_route() or escalation_route())
    return bool(local_route() or vision_route() or coding_route() or (
        escalation_route() if (_env("ESCALATION_MODEL_BASE_URL") or _env("XAI_API_KEY") or _env("ANTHROPIC_API_KEY")) else None
    ))


def auto_escalate_enabled() -> bool:
    if not active():
        return False
    flag = _flag("WORK_ROUTING_AUTO_ESCALATE", "auto")
    if _disabled(flag):
        return False
    return escalation_route() is not None or coding_route() is not None or vision_route() is not None


def _int_env(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def user_text(messages: Any) -> str:
    """Last user message as plain text. Tool output is not included."""

    if not messages:
        return ""
    for message in reversed(list(messages)):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") in {None, "text"}:
                    parts.append(str(part.get("text") or ""))
                elif isinstance(part, str):
                    parts.append(part)
            return "\n".join(parts)
        if content is not None:
            return str(content)
    return ""


def looks_like_coding(text: str) -> bool:
    return bool(text) and len(text) >= 400 and bool(_CODE_RE.search(text))


def _is_api_url(url: str, kind: Optional[str] = None) -> bool:
    from src.endpoint_resolver import endpoint_cost_tracked

    return bool(url) and endpoint_cost_tracked(url, kind)


def complexity_reason(
    messages: Any = None,
    tool_events: Any = None,
    endpoint_url: str = "",
) -> Optional[str]:
    """Why this turn should start on the strong model, or None.

    A call that is already aimed at an API host is left alone. Length, tool
    rounds, and an explicit phrase are the only signals. They are deliberately
    coarse so a 9B model is not asked to grade its own difficulty.
    """

    if endpoint_url and _is_api_url(endpoint_url):
        return None
    text = user_text(messages)
    lowered = text.lower()
    for phrase in _ESCALATE_PHRASES:
        if phrase in lowered:
            return "explicit escalation request"
    limit = _int_env("WORK_ROUTING_ESCALATE_CHARS", 6000)
    if limit > 0 and len(text) >= limit:
        return f"request length {len(text)} exceeds {limit} characters"
    rounds = 0
    if isinstance(tool_events, (list, tuple)):
        rounds = len(tool_events)
    if isinstance(messages, (list, tuple)):
        rounds = max(
            rounds,
            sum(1 for message in messages if isinstance(message, dict) and message.get("role") == "tool"),
        )
    cap = _int_env("WORK_ROUTING_ESCALATE_ROUNDS", 4)
    if cap > 0 and rounds >= cap:
        return f"{rounds} tool rounds exceeds {cap}"
    return None


def _hard_route(kind: str, messages: Any) -> Optional[ModelRoute]:
    if kind == "vision":
        return vision_route() or escalation_route()
    text = user_text(messages)
    if kind == "coding" or (coding_route() and looks_like_coding(text)):
        return coding_route() or escalation_route()
    if kind == "escalation":
        return escalation_route()
    return escalation_route()


def _local_for(kind: str) -> Optional[ModelRoute]:
    if kind in {"vision", "escalation"}:
        return None
    if kind == "coding" and coding_route():
        return local_route()
    return local_route()


def budget_cap_usd() -> float:
    """Daily API cap. Zero or unset means no cap."""

    return max(0.0, _float_env("MODEL_API_DAILY_BUDGET_USD", 0.0))


def _budget_path() -> str:
    override = _env("MODEL_API_BUDGET_PATH")
    if override:
        return override
    from src.constants import DATA_DIR

    return os.path.join(DATA_DIR, "model_api_budget.json")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def budget_spent_usd() -> float:
    path = _budget_path()
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return 0.0
    if not isinstance(payload, dict) or payload.get("date") != _today():
        return 0.0
    try:
        return max(0.0, float(payload.get("spent_usd") or 0))
    except (TypeError, ValueError):
        return 0.0


def budget_allows_api() -> bool:
    cap = budget_cap_usd()
    if cap <= 0:
        return True
    return budget_spent_usd() < cap


def estimate_call_usd(model: str, url: str = "") -> float:
    """A conservative per-call figure used by the daily cap.

    Known priced models use a 2,000-in / 500-out estimate. Anything else uses
    ``MODEL_API_CALL_ESTIMATE_USD`` (default $0.02). Local calls are $0.
    """

    if url and not _is_api_url(url):
        return 0.0
    try:
        from src.model_pricing import estimate_cost_usd

        priced = estimate_cost_usd(model, 2000, 500, endpoint_url=url or "https://api.example.com")
    except Exception:
        priced = None
    if priced is not None and priced > 0:
        return float(priced)
    return max(0.0, _float_env("MODEL_API_CALL_ESTIMATE_USD", 0.02))


def note_successful_call(url: str, model: str) -> None:
    """Add one estimated call to today's ledger. No-op unless a cap is set."""

    if budget_cap_usd() <= 0:
        return
    if not url or not _is_api_url(url):
        return
    amount = estimate_call_usd(model, url)
    if amount <= 0:
        return
    path = _budget_path()
    with _BUDGET_LOCK:
        spent = budget_spent_usd() + amount
        payload = {"date": _today(), "spent_usd": round(spent, 6)}
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(temporary, path)
    if spent >= budget_cap_usd():
        logger.warning(
            "API model daily budget reached (spent about $%.4f, cap $%.4f). "
            "Later calls stay on the local model until tomorrow UTC.",
            spent,
            budget_cap_usd(),
        )


def _dedupe_key(candidate: tuple) -> tuple:
    url = str(candidate[0] or "").rstrip("/")
    model = candidate[1] if len(candidate) > 1 else ""
    return (url, model)


def _append_unique(candidates: list, descriptors: list, route: Optional[ModelRoute]) -> None:
    if route is None:
        return
    candidate = route.candidate()
    if _is_api_url(candidate[0], route.kind) and not budget_allows_api():
        logger.info("Skipping API route %s because the daily budget is spent", route.label)
        return
    key = _dedupe_key(candidate)
    if any(_dedupe_key(existing) == key for existing in candidates):
        return
    candidates.append(candidate)
    descriptors.append(route.descriptor())


def align_foreground(
    candidates: list,
    descriptors: list,
    messages: Any = None,
    *,
    kind: str = "chat",
) -> tuple[list, list]:
    """Return candidates and the parallel route descriptors.

    When routing is off, the inputs are returned unchanged. When it is on,
    the local model leads easy work and the escalation model leads hard work.
    The other one stays in the list so a failure still has a fallback.
    """

    if not active():
        return list(candidates or []), list(descriptors or [])

    current = [tuple(item) for item in (candidates or []) if item and item[0] and item[1]]
    current_desc = [dict(item) if isinstance(item, dict) else {} for item in (descriptors or [])]
    while len(current_desc) < len(current):
        current_desc.append({
            "endpoint_id": None,
            "endpoint_label": "Selected route",
            "endpoint_cost_tracked": _is_api_url(current[len(current_desc)][0]),
        })
    current_desc = current_desc[: len(current)]

    primary_url = current[0][0] if current else ""
    if primary_url and _is_api_url(primary_url):
        return current, current_desc

    reason = complexity_reason(messages, endpoint_url=primary_url)
    hard = _hard_route(kind, messages)
    local = _local_for(kind)
    merged: list = []
    merged_desc: list = []

    def _take(candidate, descriptor):
        key = _dedupe_key(candidate)
        if any(_dedupe_key(existing) == key for existing in merged):
            return
        if _is_api_url(candidate[0]) and not budget_allows_api():
            return
        merged.append(tuple(candidate))
        merged_desc.append(dict(descriptor) if isinstance(descriptor, dict) else {})

    # Vision, coding, and escalation turns start on their specialist when one
    # is configured. Chat and background stay local until the turn is hard.
    specialist = kind in {"vision", "coding", "escalation"}
    if (reason or specialist) and hard is not None:
        _append_unique(merged, merged_desc, hard)
    for candidate, descriptor in zip(current, current_desc):
        _take(candidate, descriptor)
    if not reason:
        _append_unique(merged, merged_desc, local)
        _append_unique(merged, merged_desc, hard)
    else:
        _append_unique(merged, merged_desc, local)
        if hard is None:
            _append_unique(merged, merged_desc, escalation_route())
    if not merged:
        return current, current_desc
    return merged, merged_desc


def apply_work_routing(candidates: list, messages: Any = None, *, kind: str = "background") -> list:
    """Candidate-only form of :func:`align_foreground`."""

    merged, _descriptors = align_foreground(candidates, [], messages, kind=kind)
    return merged


def extend_candidates(candidates: list, *, kind: str, messages: Any = None) -> list:
    return apply_work_routing(candidates, messages, kind=kind)


def public_status() -> dict:
    """Non-secret snapshot for /api/runtime and the setup notes."""

    def _host(route: Optional[ModelRoute]) -> str:
        if not route:
            return ""
        try:
            return urlparse(route.base_url).hostname or ""
        except Exception:
            return ""

    escalation = escalation_route() if active() or _env("ESCALATION_MODEL_BASE_URL") or _env("XAI_API_KEY") or _env("ANTHROPIC_API_KEY") else None
    if _enabled(_flag("WORK_ROUTING_ENABLED")):
        escalation = escalation_route()
    return {
        "active": active(),
        "auto_escalate": auto_escalate_enabled(),
        "kinds": list(WORK_KINDS),
        "local_configured": local_route() is not None,
        "local_host": _host(local_route()),
        "escalation_configured": escalation_route() is not None if active() else escalation is not None,
        "escalation_label": (escalation_route().label if escalation_route() else ""),
        "escalation_host": _host(escalation_route() if active() else escalation),
        "vision_configured": vision_route() is not None,
        "coding_configured": coding_route() is not None,
        "budget_cap_usd": budget_cap_usd(),
        "budget_spent_usd": round(budget_spent_usd(), 6) if budget_cap_usd() > 0 else 0.0,
        "litellm": "optional",
    }


def _upsert_route(db, route: ModelRoute) -> None:
    from core.database import ModelEndpoint

    row = db.get(ModelEndpoint, route.endpoint_id)
    if row is None:
        row = ModelEndpoint(id=route.endpoint_id, name=route.label, base_url=route.base_url)
        db.add(row)
    row.name = route.label
    row.base_url = route.base_url
    row.api_key = route.api_key or None
    row.is_enabled = True
    row.model_type = "llm"
    row.endpoint_kind = "local" if route.kind == "local" else "api"
    row.supports_tools = True
    row.cached_models = json.dumps([route.model])
    row.pinned_models = json.dumps([route.model])


def _fill_setting(settings: dict, key: str, value: str) -> bool:
    if not value:
        return False
    current = settings.get(key)
    if isinstance(current, str) and current.strip():
        return False
    if current not in (None, "", []):
        return False
    settings[key] = value
    return True


def sync_env_endpoints() -> None:
    """Create the env-owned endpoint rows and fill empty model settings.

    Existing default, utility, task, and vision choices are left in place.
    The rows with ids ``work-routing-*`` are rewritten from the environment
    on each startup so the Settings screen matches the env file. Skipped
    under pytest so a developer shell with API keys does not rewrite the
    test database.
    """

    if os.getenv("PYTEST_CURRENT_TEST") and not _enabled(_flag("WORK_ROUTING_SYNC_IN_TESTS")):
        return
    if not active():
        return
    routes = [route for route in (local_route(), escalation_route(), vision_route(), coding_route()) if route]
    if not routes:
        return
    try:
        from core.database import SessionLocal
        from src.settings import load_settings, save_settings
    except Exception:
        logger.debug("work routing endpoint sync unavailable", exc_info=True)
        return

    db = SessionLocal()
    try:
        for route in routes:
            _upsert_route(db, route)
        db.commit()
    except Exception:
        logger.warning("Could not store work-routing endpoints", exc_info=True)
        return
    finally:
        db.close()

    try:
        settings = load_settings()
    except Exception:
        logger.debug("work routing settings sync skipped", exc_info=True)
        return
    changed = False
    local = local_route()
    if local:
        changed = _fill_setting(settings, "default_endpoint_id", local.endpoint_id) or changed
        changed = _fill_setting(settings, "default_model", local.model) or changed
        changed = _fill_setting(settings, "utility_endpoint_id", local.endpoint_id) or changed
        changed = _fill_setting(settings, "utility_model", local.model) or changed
        changed = _fill_setting(settings, "task_endpoint_id", local.endpoint_id) or changed
        changed = _fill_setting(settings, "task_model", local.model) or changed
    vision = vision_route() or escalation_route()
    if vision and not str(settings.get("vision_model") or "").strip():
        settings["vision_model"] = f"{vision.model}@{vision.label}"
        changed = True
    if changed:
        try:
            save_settings(settings)
        except Exception:
            logger.warning("Could not save work-routing settings", exc_info=True)
