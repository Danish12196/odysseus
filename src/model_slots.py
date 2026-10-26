"""Per-endpoint model slots for background work.

The historical scheduler held one global ``Semaphore(1)``. That lock remains
the fallback when nothing asks for more than one slot, so existing callers
that swap ``TaskScheduler._run_semaphore`` keep working. When
``BACKGROUND_MODEL_SLOTS`` (or a per-URL map) is greater than 1, or a
separate background endpoint is configured, each model endpoint gets its own
semaphore and they no longer share one lock.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_POOLS: dict[int, dict] = {}
_POOL_LOCK = threading.Lock()


def _clean_url(url: str | None) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        if parts.scheme and parts.netloc:
            path = parts.path.rstrip("/")
            if path.endswith("/chat/completions"):
                path = path[: -len("/chat/completions")]
            return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    except Exception:
        pass
    return text.rstrip("/")


def separate_background_endpoint() -> str:
    """Configured background endpoint, or empty when unset."""
    env = str(os.getenv("BACKGROUND_MODEL_ENDPOINT") or "").strip()
    if env:
        return _clean_url(env)
    try:
        from src.settings import get_setting
        return _clean_url(get_setting("background_model_endpoint", "") or "")
    except Exception:
        return ""


def _parse_slot_map(raw: Any) -> dict[str, int]:
    if isinstance(raw, dict):
        out = {}
        for key, value in raw.items():
            try:
                slots = int(value)
            except (TypeError, ValueError):
                continue
            if slots > 0:
                out[_clean_url(str(key)) or "*"] = slots
        return out
    text = str(raw or "").strip()
    if not text or text.isdigit():
        return {}
    out = {}
    for part in text.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        try:
            slots = int(value.strip())
        except ValueError:
            continue
        if slots > 0:
            out[_clean_url(key) or "*"] = slots
    return out


def _settings_slot_map() -> dict[str, int]:
    try:
        from src.settings import get_setting
        raw = get_setting("background_endpoint_slots", None)
    except Exception:
        raw = None
    return _parse_slot_map(raw)


def _env_slot_map() -> dict[str, int]:
    return _parse_slot_map(os.getenv("BACKGROUND_ENDPOINT_SLOTS", ""))


def default_slots() -> int:
    raw = os.getenv("BACKGROUND_MODEL_SLOTS")
    if raw is None or str(raw).strip() == "":
        try:
            from src.settings import get_setting
            raw = get_setting("background_model_slots", 1)
        except Exception:
            raw = 1
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def parallel_or_separate() -> bool:
    """True when background calls can overlap a foreground chat."""
    flag = str(os.getenv("BACKGROUND_MODEL_PARALLEL") or "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    if separate_background_endpoint():
        return True
    if default_slots() > 1:
        return True
    mapping = {**_settings_slot_map(), **_env_slot_map()}
    return bool(mapping)


def slots_for(endpoint_url: str | None) -> int:
    key = _clean_url(endpoint_url)
    mapping = {**_settings_slot_map(), **_env_slot_map()}
    if key and key in mapping:
        return mapping[key]
    if "*" in mapping:
        return mapping["*"]
    return default_slots()


def per_endpoint_pool_enabled() -> bool:
    if default_slots() > 1:
        return True
    return bool(_env_slot_map() or _settings_slot_map() or separate_background_endpoint())


def _running_loop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def semaphore_for(scheduler, task_id: str | None = None, endpoint_url: str | None = None):
    """Return the semaphore a task must acquire before a model call.

    With the default single slot this is ``scheduler._run_semaphore`` so tests
    and older callers that hold that lock still queue the task.
    """
    fallback = getattr(scheduler, "_run_semaphore", None)
    if not per_endpoint_pool_enabled():
        return fallback
    if endpoint_url is None and task_id:
        endpoint_url = _endpoint_for_task(task_id)
    key = _clean_url(endpoint_url) or "*"
    slots = max(1, slots_for(key if key != "*" else None))
    loop = _running_loop()
    loop_id = id(loop) if loop is not None else 0
    with _POOL_LOCK:
        pool = _POOLS.setdefault(id(scheduler), {})
        sem = pool.get((loop_id, key))
        if sem is None:
            sem = asyncio.Semaphore(slots)
            pool[(loop_id, key)] = sem
        return sem


def _endpoint_for_task(task_id: str) -> str:
    try:
        from core.database import ScheduledTask, SessionLocal
        db = SessionLocal()
        try:
            task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
            if not task:
                return ""
            if getattr(task, "endpoint_url", None):
                return task.endpoint_url
            separate = separate_background_endpoint()
            return separate or ""
        finally:
            db.close()
    except Exception:
        return separate_background_endpoint()
