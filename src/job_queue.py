"""SQLite-backed background jobs.

Scheduled runs, chat handoffs, detached agent-run checkpoints and webhook
retries share one table. Claiming is a compare-and-set update so the in-process
scheduler and the optional ``python -m src.background_worker`` process can
share a database without a second broker. SQLite is the default; the same
table works on the project's other SQLAlchemy dialects.
"""

from __future__ import annotations

import json
import logging
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_WORKER_ID = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _dumps(payload: Any) -> str | None:
    if payload is None:
        return None
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)[:20000]
    except Exception:
        return json.dumps({"raw": str(payload)[:4000]})


def _loads(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw[:4000]}


def _models():
    from core.database import BackgroundJob, BackgroundJobEvent, SessionLocal
    return SessionLocal, BackgroundJob, BackgroundJobEvent


def append_event(job_id: str, status: str, message: str | None = None) -> None:
    try:
        SessionLocal, _Job, Event = _models()
        db = SessionLocal()
        try:
            db.add(Event(
                id=uuid.uuid4().hex,
                job_id=job_id,
                status=status,
                message=(message or "")[:2000] or None,
                created_at=_utcnow(),
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.debug("Could not record background job event", exc_info=True)


def enqueue(
    kind: str,
    *,
    owner: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    payload: Any = None,
    max_attempts: int = 3,
    dedupe_key: str | None = None,
    delay_seconds: float = 0,
    status: str = "queued",
) -> str | None:
    """Insert a job. A live queued/running row with the same dedupe key is reused."""
    try:
        SessionLocal, Job, _Event = _models()
        db = SessionLocal()
        try:
            if dedupe_key:
                existing = (
                    db.query(Job)
                    .filter(
                        Job.dedupe_key == dedupe_key,
                        Job.status.in_(("queued", "running", "parked", "ready", "resuming")),
                    )
                    .first()
                )
                if existing:
                    return existing.id
            job_id = uuid.uuid4().hex
            now = _utcnow()
            db.add(Job(
                id=job_id,
                kind=kind,
                status=status,
                owner=owner,
                session_id=session_id,
                task_id=task_id,
                dedupe_key=dedupe_key,
                payload_json=_dumps(payload),
                attempts=0,
                max_attempts=max(1, int(max_attempts)),
                next_attempt_at=now + timedelta(seconds=max(0, delay_seconds)),
                created_at=now,
                updated_at=now,
            ))
            db.commit()
        finally:
            db.close()
        append_event(job_id, status, f"enqueued {kind}")
        return job_id
    except Exception:
        logger.warning("Could not enqueue background job %s", kind, exc_info=True)
        return None


def get_job(job_id: str) -> dict | None:
    try:
        SessionLocal, Job, _Event = _models()
        db = SessionLocal()
        try:
            row = db.query(Job).filter(Job.id == job_id).first()
            return _row_dict(row) if row else None
        finally:
            db.close()
    except Exception:
        logger.debug("Could not load background job %s", job_id, exc_info=True)
        return None


def list_jobs(*, owner: str | None = None, limit: int = 50) -> list[dict]:
    try:
        SessionLocal, Job, Event = _models()
        db = SessionLocal()
        try:
            query = db.query(Job)
            if owner:
                query = query.filter(Job.owner == owner)
            rows = query.order_by(Job.created_at.desc()).limit(max(1, min(limit, 200))).all()
            jobs = []
            for row in rows:
                item = _row_dict(row)
                events = (
                    db.query(Event)
                    .filter(Event.job_id == row.id)
                    .order_by(Event.created_at.desc())
                    .limit(8)
                    .all()
                )
                item["history"] = [
                    {
                        "status": event.status,
                        "message": event.message,
                        "at": event.created_at.isoformat() + "Z" if event.created_at else None,
                    }
                    for event in events
                ]
                jobs.append(item)
            return jobs
        finally:
            db.close()
    except Exception:
        logger.debug("Could not list background jobs", exc_info=True)
        return []


def _row_dict(row) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "status": row.status,
        "owner": row.owner,
        "session_id": row.session_id,
        "task_id": row.task_id,
        "dedupe_key": row.dedupe_key,
        "payload": _loads(row.payload_json),
        "result": row.result,
        "error": row.error,
        "attempts": row.attempts or 0,
        "max_attempts": row.max_attempts or 1,
        "next_attempt_at": row.next_attempt_at.isoformat() + "Z" if row.next_attempt_at else None,
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
        "updated_at": row.updated_at.isoformat() + "Z" if row.updated_at else None,
        "finished_at": row.finished_at.isoformat() + "Z" if row.finished_at else None,
    }


def update_job(job_id: str, **fields) -> None:
    try:
        SessionLocal, Job, _Event = _models()
        db = SessionLocal()
        try:
            row = db.query(Job).filter(Job.id == job_id).first()
            if not row:
                return
            for key, value in fields.items():
                if hasattr(row, key):
                    setattr(row, key, value)
            row.updated_at = _utcnow()
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.debug("Could not update background job %s", job_id, exc_info=True)


def claim_due(*, limit: int = 4, kinds: tuple[str, ...] | None = None, lock_seconds: int = 900) -> list[dict]:
    """Claim up to ``limit`` jobs whose next attempt is due. Safe across processes."""
    claimed = []
    try:
        SessionLocal, Job, _Event = _models()
        db = SessionLocal()
        try:
            now = _utcnow()
            query = db.query(Job).filter(
                Job.status.in_(("queued", "ready")),
                Job.next_attempt_at <= now,
            )
            if kinds:
                query = query.filter(Job.kind.in_(kinds))
            rows = query.order_by(Job.next_attempt_at.asc()).limit(max(1, limit * 3)).all()
            for row in rows:
                if len(claimed) >= limit:
                    break
                attempts = row.attempts or 0
                updated = db.query(Job).filter(
                    Job.id == row.id,
                    Job.status == row.status,
                    Job.attempts == attempts,
                ).update({
                    "status": "running",
                    "attempts": attempts + 1,
                    "locked_by": _WORKER_ID,
                    "locked_until": now + timedelta(seconds=lock_seconds),
                    "updated_at": now,
                }, synchronize_session=False)
                if updated != 1:
                    db.rollback()
                    continue
                db.commit()
                fresh = db.query(Job).filter(Job.id == row.id).first()
                if fresh:
                    claimed.append(_row_dict(fresh))
        finally:
            db.close()
    except Exception:
        logger.debug("Background job claim failed", exc_info=True)
        return []
    for job in claimed:
        append_event(job["id"], "running", f"attempt {job['attempts']}")
    return claimed


def complete(job_id: str, *, status: str, result: str | None = None, error: str | None = None) -> None:
    now = _utcnow()
    update_job(
        job_id,
        status=status,
        result=(result or "")[:8000] or None,
        error=(error or "")[:2000] or None,
        finished_at=now if status not in {"queued", "running", "parked", "ready", "resuming"} else None,
        locked_by=None,
        locked_until=None,
    )
    append_event(job_id, status, error or result)


def retry_later(job_id: str, *, error: str, delay_seconds: float) -> bool:
    """Requeue when attempts remain. Returns False when the job is exhausted."""
    exhausted = True
    try:
        SessionLocal, Job, _Event = _models()
        db = SessionLocal()
        try:
            row = db.query(Job).filter(Job.id == job_id).first()
            if not row:
                return False
            if (row.attempts or 0) >= (row.max_attempts or 1):
                row.status = "failed"
                row.error = (error or "")[:2000]
                row.finished_at = _utcnow()
                row.locked_by = None
                row.locked_until = None
                exhausted = True
            else:
                row.status = "queued"
                row.error = (error or "")[:2000]
                row.next_attempt_at = _utcnow() + timedelta(seconds=max(1, delay_seconds))
                row.locked_by = None
                row.locked_until = None
                exhausted = False
            row.updated_at = _utcnow()
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.debug("Could not retry background job %s", job_id, exc_info=True)
        return False
    append_event(job_id, "failed" if exhausted else "queued", error)
    return not exhausted


def recover_stale_locks() -> int:
    """Requeue jobs whose worker lock expired, and close dead agent-run checkpoints."""
    recovered = 0
    try:
        SessionLocal, Job, _Event = _models()
        db = SessionLocal()
        try:
            now = _utcnow()
            stale = db.query(Job).filter(
                Job.status == "running",
                Job.locked_until.isnot(None),
                Job.locked_until < now,
            ).all()
            for row in stale:
                if row.kind == "agent_run":
                    row.status = "interrupted"
                    row.error = "Server restarted while this detached chat run was in progress"
                    row.finished_at = now
                    row.locked_by = None
                    row.locked_until = None
                elif (row.attempts or 0) >= (row.max_attempts or 1):
                    row.status = "failed"
                    row.error = row.error or "Worker lock expired and attempts were exhausted"
                    row.finished_at = now
                    row.locked_by = None
                    row.locked_until = None
                else:
                    row.status = "queued"
                    row.error = "Worker lock expired; queued for retry"
                    row.next_attempt_at = now + timedelta(seconds=5)
                    row.locked_by = None
                    row.locked_until = None
                row.updated_at = now
                recovered += 1
            if recovered:
                db.commit()
        finally:
            db.close()
    except Exception:
        logger.debug("Stale background-job recovery skipped", exc_info=True)
        return 0
    if recovered:
        logger.info("Recovered %d stale background job(s)", recovered)
    return recovered


def record_agent_run(session_id: str, run_id: str, owner: str | None = None) -> str | None:
    job_id = enqueue(
        "agent_run",
        owner=owner,
        session_id=session_id,
        payload={"run_id": run_id},
        dedupe_key=f"agent_run:{session_id}:{run_id}",
        max_attempts=1,
        status="running",
    )
    if not job_id:
        return None
    # A lease lets a later process tell a crashed run from one that is still
    # going. The web process also interrupts every running checkpoint on
    # startup, because the in-memory run cannot have survived.
    try:
        SessionLocal, Job, _Event = _models()
        db = SessionLocal()
        try:
            row = db.query(Job).filter(Job.id == job_id, Job.status == "running").first()
            if row:
                row.locked_by = _WORKER_ID
                row.locked_until = _utcnow() + timedelta(hours=24)
                row.updated_at = _utcnow()
                db.commit()
        finally:
            db.close()
    except Exception:
        logger.debug("Could not lease agent-run checkpoint %s", job_id, exc_info=True)
    return job_id


def interrupt_orphaned_agent_runs() -> int:
    """Close detached-run checkpoints left by a previous web process.

    Call this only from the process that owns chat runs. The optional worker
    must not: a live app may still hold those runs in memory.
    """
    count = 0
    try:
        SessionLocal, Job, _Event = _models()
        db = SessionLocal()
        try:
            now = _utcnow()
            rows = db.query(Job).filter(Job.kind == "agent_run", Job.status == "running").all()
            for row in rows:
                row.status = "interrupted"
                row.error = "Server restarted while this detached chat run was in progress"
                row.finished_at = now
                row.locked_by = None
                row.locked_until = None
                row.updated_at = now
                count += 1
            if count:
                db.commit()
        finally:
            db.close()
    except Exception:
        logger.debug("Orphaned agent-run sweep skipped", exc_info=True)
        return 0
    if count:
        logger.info("Marked %d detached agent run(s) interrupted after restart", count)
    return count


def finish_agent_run(session_id: str, run_id: str, status: str) -> None:
    try:
        SessionLocal, Job, _Event = _models()
        db = SessionLocal()
        try:
            key = f"agent_run:{session_id}:{run_id}"
            row = db.query(Job).filter(Job.dedupe_key == key).first()
            if not row:
                return
            terminal = status if status in {"done", "error", "stopped", "interrupted"} else "done"
            row.status = "succeeded" if terminal == "done" else terminal
            row.finished_at = _utcnow()
            row.updated_at = row.finished_at
            row.locked_by = None
            row.locked_until = None
            db.commit()
            job_id = row.id
        finally:
            db.close()
        append_event(job_id, status, "detached agent run finished")
    except Exception:
        logger.debug("Could not finish agent-run checkpoint", exc_info=True)
