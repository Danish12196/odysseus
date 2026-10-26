"""Park background approvals instead of auto-denying them.

The pending grant stays in the existing one-time approval store. Nothing runs
until the owner approves it through the same consume path the chat uses, or
the grant expires. A restart drops in-memory grants; parked jobs then expire
without executing the sealed action.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DECISIONS = {"approve", "approve_task", "deny"}


class TaskParked(Exception):
    """The run stopped so a person can approve one sealed action."""

    def __init__(self, message: str, approval_id: str | None = None, hold: bool = True):
        super().__init__(message)
        self.approval_id = approval_id
        self.hold = hold


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def park_scheduled_approval(scheduler, task, session_id: str, approval: dict) -> None:
    """Record a parked job and notify the owner. Does not consume the grant."""
    from src.job_queue import enqueue
    from src.tool_approvals import tool_approval_store

    approval_id = str((approval or {}).get("approval_id") or "")
    pending = tool_approval_store.peek(approval_id) if approval_id else None
    if pending is None:
        raise TaskParked(
            "Scheduled task paused safely: the approval was already gone, so nothing ran.",
            approval_id or None,
            hold=False,
        )
    tool = str((approval or {}).get("tool") or pending.tool_name or "tool")
    payload = {
        "approval_id": approval_id,
        "tool": tool,
        "session_id": session_id,
        "task_id": getattr(task, "id", None),
        "task_name": getattr(task, "name", None),
        "expires_at": pending.expires_at,
        "continuation_query": pending.continuation_query or getattr(task, "prompt", "") or "",
    }
    enqueue(
        "approval",
        owner=getattr(task, "owner", None),
        session_id=session_id,
        task_id=getattr(task, "id", None),
        payload=payload,
        dedupe_key=f"approval:{approval_id}",
        status="parked",
        max_attempts=1,
    )
    _notify(scheduler, task, tool, approval_id, session_id)
    _leave_chat_note(task, session_id, pending, tool)
    raise TaskParked(
        f"Waiting for approval before {tool} can run. Nothing was executed.",
        approval_id,
    )


def _notify(scheduler, task, tool: str, approval_id: str, session_id: str) -> None:
    if scheduler is None or not hasattr(scheduler, "add_notification"):
        return
    try:
        scheduler.add_notification(
            getattr(task, "name", None) or "Scheduled task",
            "approval",
            getattr(task, "id", None),
            owner=getattr(task, "owner", None),
            body=f"Approve {tool} ({approval_id}) in chat {session_id}. Nothing ran yet.",
        )
    except Exception:
        logger.debug("Could not notify parked approval", exc_info=True)


def _leave_chat_note(task, session_id: str, pending, tool: str) -> None:
    if not session_id:
        return
    try:
        from core.database import ChatMessage, Session, SessionLocal, utcnow_naive
        db = SessionLocal()
        try:
            session = db.get(Session, session_id)
            if session is None or (getattr(task, "owner", None) and session.owner != task.owner):
                return
            message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"odysseus:approval:{pending.approval_id}"))
            if db.get(ChatMessage, message_id):
                return
            now = utcnow_naive()
            note = (
                f"A background task needs approval before `{tool}` can run. "
                "Nothing was executed. Approve or deny it from the notification, "
                "or it will expire without running."
            )
            meta = {"pending_approval": pending.public_payload(), "task_id": getattr(task, "id", None)}
            db.add(ChatMessage(
                id=message_id,
                session_id=session_id,
                role="assistant",
                content=note,
                meta_data=json.dumps(meta),
                timestamp=now,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.debug("Could not leave approval note", exc_info=True)


def _job_for_approval(db, Job, approval_id: str):
    return db.query(Job).filter(Job.dedupe_key == f"approval:{approval_id}").first()


def decide(approval_id: str, *, decision: str, owner: str, session_id: str | None, scheduler) -> dict:
    """Apply the owner's decision. The grant is consumed once, or nothing runs."""
    from src.job_queue import append_event, complete, update_job
    from src.tool_approvals import tool_approval_store
    from core.database import BackgroundJob, SessionLocal

    normalized = str(decision or "").strip().lower()
    if normalized not in DECISIONS:
        raise ValueError("Unsupported approval decision")
    pending = tool_approval_store.peek(approval_id)
    if pending is None:
        raise LookupError("Approval is no longer pending")
    if str(pending.owner or "").casefold() != str(owner or "").strip().casefold():
        raise PermissionError("Approval belongs to another user")
    if session_id and pending.session_id != session_id:
        raise PermissionError("Approval belongs to another chat")

    db = SessionLocal()
    try:
        row = _job_for_approval(db, BackgroundJob, approval_id)
        if row is None or row.status not in {"parked", "resuming"}:
            raise LookupError("No parked background job for this approval")
        if str(row.owner or "").casefold() != str(owner or "").strip().casefold():
            raise PermissionError("Approval belongs to another user")
        job_id = row.id
        task_id = row.task_id
        payload = json.loads(row.payload_json or "{}")
        row.status = "resuming"
        row.updated_at = _utcnow()
        db.commit()
    finally:
        db.close()

    if normalized == "deny":
        # Deny pops the grant and returns None. Nothing is executed.
        tool_approval_store.consume(
            approval_id,
            decision="deny",
            owner=owner,
            session_id=pending.session_id,
            allow_continuation=False,
        )
        complete(job_id, status="denied", error="Owner denied the sealed action. Nothing ran.")
        _restore_schedule(task_id)
        append_event(job_id, "denied", "owner denied")
        return {"ok": True, "status": "denied", "approval_id": approval_id}

    exact = tool_approval_store.consume(
        approval_id,
        decision=normalized,
        owner=owner,
        session_id=pending.session_id,
        allow_continuation=True,
    )
    if exact is None:
        update_job(job_id, status="parked")
        raise LookupError("Approval could not be consumed")

    continuation = payload.get("continuation_query") or ""
    if scheduler is None or not hasattr(scheduler, "resume_parked_approval"):
        complete(job_id, status="failed", error="Scheduler cannot resume this approval")
        return {"ok": False, "status": "failed", "approval_id": approval_id}
    return {
        "ok": True,
        "status": "resuming",
        "approval_id": approval_id,
        "resume": (task_id, job_id, exact, continuation),
    }


def _restore_schedule(task_id: str | None) -> None:
    if not task_id:
        return
    try:
        from core.database import ScheduledTask, SessionLocal
        from src.task_scheduler import compute_next_run, _resolve_task_timezone, _utcnow as task_now
        db = SessionLocal()
        try:
            task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
            if not task or task.status != "active":
                return
            if (task.trigger_type or "schedule") == "schedule":
                task.next_run = compute_next_run(
                    task.schedule, task.scheduled_time,
                    task.scheduled_day, task.scheduled_date,
                    after=task_now(),
                    cron_expression=task.cron_expression,
                    tz_name=_resolve_task_timezone(db, task),
                )
            else:
                task.next_run = None
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.debug("Could not restore schedule for %s", task_id, exc_info=True)


def expire_due(scheduler=None, *, restart: bool = False) -> int:
    """Expire parked approvals. Never executes the sealed action."""
    expired = 0
    try:
        from core.database import BackgroundJob, SessionLocal
        db = SessionLocal()
        try:
            rows = db.query(BackgroundJob).filter(
                BackgroundJob.kind == "approval",
                BackgroundJob.status == "parked",
            ).all()
            now = time.time()
            for row in rows:
                try:
                    payload = json.loads(row.payload_json or "{}")
                except Exception:
                    payload = {}
                approval_id = str(payload.get("approval_id") or "")
                expires_at = float(payload.get("expires_at") or 0)
                from src.tool_approvals import tool_approval_store
                pending = tool_approval_store.peek(approval_id) if approval_id else None
                if restart or pending is None or (expires_at and expires_at <= now):
                    if not restart and pending is None and expires_at and expires_at > now:
                        row.status = "resolved"
                        row.error = "Approval was decided outside this parked job. The sealed action was not re-run."
                    else:
                        row.status = "expired"
                        row.error = (
                            "Approval expired after a restart; the sealed action was not executed."
                            if restart else
                            "Approval expired before it was granted. Nothing ran."
                        )
                        if approval_id and pending is not None:
                            tool_approval_store.consume(
                                approval_id,
                                decision="deny",
                                owner=row.owner,
                                session_id=payload.get("session_id"),
                                allow_continuation=False,
                            )
                    row.finished_at = _utcnow()
                    row.updated_at = row.finished_at
                    row.locked_by = None
                    row.locked_until = None
                    expired += 1
                    _restore_schedule(row.task_id)
                    if scheduler is not None and hasattr(scheduler, "add_notification") and row.status == "expired":
                        try:
                            scheduler.add_notification(
                                payload.get("task_name") or "Scheduled task",
                                "expired",
                                row.task_id,
                                owner=row.owner,
                                body=row.error,
                            )
                        except Exception:
                            pass
            if expired:
                db.commit()
        finally:
            db.close()
    except Exception:
        logger.debug("Parked approval expiry skipped", exc_info=True)
        return 0
    return expired


def sweep_restart(scheduler=None) -> int:
    """Fail closed after a process restart. In-memory grants are gone."""
    return expire_due(scheduler, restart=True)
