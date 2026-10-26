"""Foreground policy for scheduled and background work.

Policies:
  run    — start immediately and do not cancel when the UI is active
  defer  — wait for a quiet window, but only up to a finite max, then run
  yield  — the historical gate: postpone while the UI is active and cancel
           a running task if foreground activity appears

``auto`` (the default) resolves to ``run`` when a separate background model
endpoint or a parallel-capable endpoint is configured, and to ``defer``
otherwise. A per-task ``foreground_policy`` overrides the global setting.
Disabling ``BACKGROUND_TASK_FOREGROUND_GATE`` still means ``run``.
"""

from __future__ import annotations

import contextvars
import os

POLICIES = ("run", "defer", "yield")
_CURRENT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "odysseus_foreground_policy",
    default=None,
)


def gate_enabled() -> bool:
    return os.getenv("BACKGROUND_TASK_FOREGROUND_GATE", "true").lower() not in {
        "0", "false", "no", "off",
    }


def defer_max_wait_seconds() -> float:
    """Finite wait used by the defer policy. ``0`` means do not wait."""
    raw = os.getenv("BACKGROUND_TASK_DEFER_MAX_WAIT_SECONDS")
    if raw is None or str(raw).strip() == "":
        legacy = os.getenv("BACKGROUND_TASK_MAX_WAIT_SECONDS")
        if legacy not in (None, "", "0"):
            try:
                return max(0.0, float(legacy))
            except (TypeError, ValueError):
                pass
        return 180.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 180.0


def _explicit_policy(value: str | None) -> str | None:
    text = str(value or "").strip().lower()
    if text in POLICIES:
        return text
    return None


def resolve_foreground_policy(task=None) -> str:
    """Resolve the policy for one task. Per-task wins, then the env, then auto."""
    if not gate_enabled():
        return "run"
    per_task = _explicit_policy(getattr(task, "foreground_policy", None) if task is not None else None)
    if per_task:
        return per_task
    env = _explicit_policy(os.getenv("BACKGROUND_FOREGROUND_POLICY", "auto"))
    if env:
        return env
    if background_endpoint_is_parallel():
        return "run"
    return "defer"


def background_endpoint_is_parallel() -> bool:
    """True when background work has its own endpoint or more than one slot."""
    from src.model_slots import parallel_or_separate
    return parallel_or_separate()


def current_policy() -> str | None:
    """Policy bound for the task currently executing, if any."""
    return _CURRENT.get()


def bind_policy(policy: str | None):
    return _CURRENT.set(policy if policy in POLICIES else None)


def reset_policy(token) -> None:
    _CURRENT.reset(token)


class policy_scope:
    """Bind a foreground policy for the duration of a scheduled run."""

    def __init__(self, policy: str | None):
        self.policy = policy
        self.token = None

    def __enter__(self):
        self.token = bind_policy(self.policy)
        return self.policy

    def __exit__(self, *exc) -> None:
        if self.token is not None:
            reset_policy(self.token)


def should_postpone_for_foreground(policy: str, foreground_active: bool) -> bool:
    return bool(foreground_active) and policy == "yield"


def should_cancel_for_foreground(policy: str) -> bool:
    return policy == "yield"
