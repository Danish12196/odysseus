"""Hand a long agent task to the background and report back into the origin chat."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

logger = logging.getLogger(__name__)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def enqueue_handoff(prompt: str, *, session_id: str, owner: str | None, max_steps: int | None = None) -> dict:
    """Seal the current request authority and queue the prompt.

    Refuses when there is no active user-turn authority to cap the handoff.
    """
    from src.agent_runtime.authority import active_request_authority, seal_task_authority
    from src.job_queue import enqueue

    parent = active_request_authority()
    if parent is None:
        return {
            "error": "handoff_background: no active user request to hand off from.",
            "exit_code": 1,
        }
    text = str(prompt or "").strip()
    if not text:
        return {"error": "handoff_background: prompt is required.", "exit_code": 1}
    text = text[:8000]
    snapshot = seal_task_authority(text, "llm", None, owner=owner, parent_authority=parent)
    job_id = enqueue(
        "handoff",
        owner=owner,
        session_id=session_id,
        payload={
            "prompt": text,
            "authority_json": snapshot,
            "max_steps": max_steps or 20,
        },
        max_attempts=3,
    )
    if not job_id:
        return {"error": "handoff_background: could not queue the task.", "exit_code": 1}
    return {
        "output": (
            f"Queued background task {job_id}. It keeps running if this chat is busy "
            "or Odysseus restarts, and the result is posted back into this chat."
        ),
        "exit_code": 0,
        "job_id": job_id,
    }


def _release_ready(job_id: str, attempts: int) -> None:
    from src.job_queue import update_job
    update_job(
        job_id,
        status="ready",
        attempts=max(0, int(attempts or 1) - 1),
        locked_by=None,
        locked_until=None,
        next_attempt_at=_utcnow() + timedelta(seconds=5),
    )


def _post_chat(session_id: str, owner: str | None, job_id: str, text: str) -> bool:
    from core.database import ChatMessage, Session, SessionLocal, utcnow_naive
    db = SessionLocal()
    try:
        session = db.get(Session, session_id)
        if session is None or (owner and session.owner != owner):
            return False
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"odysseus:handoff:{job_id}:{session_id}"))
        if db.get(ChatMessage, message_id):
            return True
        now = utcnow_naive()
        body = (text or "").strip() or "(background task finished with no output)"
        db.add(ChatMessage(
            id=message_id,
            session_id=session_id,
            role="assistant",
            content=body[:16000],
            meta_data=json.dumps({"background_handoff_id": job_id, "timestamp": now.isoformat() + "Z"}),
            timestamp=now,
        ))
        session.message_count = (session.message_count or 0) + 1
        session.last_message_at = now
        db.commit()
        return True
    finally:
        db.close()


async def execute_handoff(scheduler, job: dict) -> None:
    from src.agent_runs import is_active
    from src.job_queue import complete, update_job

    payload = job.get("payload") or {}
    session_id = job.get("session_id") or payload.get("session_id")
    owner = job.get("owner")
    prompt = payload.get("prompt") or ""
    if not session_id or not prompt:
        complete(job["id"], status="failed", error="Handoff is missing a chat or a prompt")
        return
    if is_active(session_id):
        _release_ready(job["id"], job.get("attempts") or 1)
        return
    if job.get("result"):
        if _post_chat(session_id, owner, job["id"], job["result"]):
            complete(job["id"], status="succeeded", result=job["result"])
        else:
            complete(job["id"], status="failed", error="Origin chat was not available for delivery")
        return

    from core.database import Session, SessionLocal
    endpoint = ""
    model = ""
    db = SessionLocal()
    try:
        session = db.get(Session, session_id)
        if session is None or (owner and session.owner != owner):
            complete(job["id"], status="failed", error="Origin chat was not available")
            return
        endpoint = session.endpoint_url or ""
        model = session.model or ""
    finally:
        db.close()

    task = SimpleNamespace(
        id=job["id"],
        name="Background handoff",
        prompt=prompt,
        owner=owner,
        task_type="llm",
        action=None,
        max_steps=payload.get("max_steps") or 20,
        request_authority_json=payload.get("authority_json"),
        trigger_type="schedule",
        endpoint_url=endpoint,
        model=model,
        session_id=session_id,
        notifications_enabled=False,
    )
    try:
        result = await scheduler._run_agent_loop(endpoint, model, task, session_id)
    except Exception as exc:
        logger.exception("Background handoff %s failed", job["id"])
        from src.job_queue import retry_later
        retry_later(job["id"], error=f"{type(exc).__name__}: {exc}", delay_seconds=30)
        return
    text = str(result or "")
    update_job(job["id"], result=text[:8000], status="running")
    if is_active(session_id):
        _release_ready(job["id"], job.get("attempts") or 1)
        update_job(job["id"], result=text[:8000])
        return
    if _post_chat(session_id, owner, job["id"], text):
        complete(job["id"], status="succeeded", result=text[:4000])
    else:
        complete(job["id"], status="failed", error="Origin chat was not available for delivery", result=text[:4000])


async def process_handoff_jobs(scheduler, *, limit: int = 2) -> None:
    from src.job_queue import claim_due
    for job in claim_due(kinds=("handoff",), limit=limit):
        try:
            await execute_handoff(scheduler, job)
        except Exception:
            logger.exception("Handoff job %s failed", job.get("id"))
            from src.job_queue import retry_later
            retry_later(job["id"], error="handoff worker failed", delay_seconds=30)
