"""Reliability of routines, background jobs, and deferred approvals."""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_policy_per_task_beats_env_and_auto_tracks_parallel(monkeypatch):
    from src.foreground_policy import resolve_foreground_policy

    monkeypatch.delenv("BACKGROUND_MODEL_ENDPOINT", raising=False)
    monkeypatch.delenv("BACKGROUND_MODEL_SLOTS", raising=False)
    monkeypatch.delenv("BACKGROUND_MODEL_PARALLEL", raising=False)
    monkeypatch.delenv("BACKGROUND_ENDPOINT_SLOTS", raising=False)
    monkeypatch.setenv("BACKGROUND_TASK_FOREGROUND_GATE", "true")
    monkeypatch.setenv("BACKGROUND_FOREGROUND_POLICY", "yield")
    task = SimpleNamespace(foreground_policy="run")
    assert resolve_foreground_policy(task) == "run"
    assert resolve_foreground_policy(SimpleNamespace(foreground_policy=None)) == "yield"

    monkeypatch.setenv("BACKGROUND_FOREGROUND_POLICY", "auto")
    assert resolve_foreground_policy(None) == "defer"
    monkeypatch.setenv("BACKGROUND_MODEL_ENDPOINT", "http://127.0.0.1:8081")
    assert resolve_foreground_policy(None) == "run"
    monkeypatch.delenv("BACKGROUND_MODEL_ENDPOINT")
    monkeypatch.setenv("BACKGROUND_MODEL_SLOTS", "2")
    assert resolve_foreground_policy(None) == "run"
    monkeypatch.setenv("BACKGROUND_TASK_FOREGROUND_GATE", "false")
    monkeypatch.setenv("BACKGROUND_FOREGROUND_POLICY", "yield")
    assert resolve_foreground_policy(None) == "run"


@pytest.mark.asyncio
async def test_defer_wait_is_finite_and_run_does_not_wait(monkeypatch):
    from src.foreground_policy import bind_policy, reset_policy
    from src.interactive_gate import mark_browser_activity, wait_for_interactive_quiet

    monkeypatch.setenv("BACKGROUND_TASK_FOREGROUND_GATE", "true")
    monkeypatch.setenv("BACKGROUND_TASK_DEFER_MAX_WAIT_SECONDS", "0.2")
    monkeypatch.setenv("BACKGROUND_TASK_QUIET_MS", "5000")
    monkeypatch.setenv("BACKGROUND_TASK_BROWSER_ACTIVE_SECONDS", "60")
    await mark_browser_activity()
    token = bind_policy("defer")
    try:
        started = time.monotonic()
        waited = await wait_for_interactive_quiet("defer-test")
        elapsed = time.monotonic() - started
    finally:
        reset_policy(token)
    assert waited is True
    assert elapsed < 2

    token = bind_policy("run")
    try:
        started = time.monotonic()
        waited = await wait_for_interactive_quiet("run-test")
        elapsed = time.monotonic() - started
    finally:
        reset_policy(token)
    assert waited is False
    assert elapsed < 0.2


def test_event_prompt_substitution():
    from src.event_payload import render_event_prompt

    text = render_event_prompt(
        "Mail from {{event.from}}: {{event.subject}}. Missing {{event.nope}}.",
        {"from": "a@b.c", "subject": "Hello"},
    )
    assert text == "Mail from a@b.c: Hello. Missing ."


def test_default_slot_is_the_global_semaphore(monkeypatch):
    from src.model_slots import semaphore_for

    monkeypatch.delenv("BACKGROUND_MODEL_ENDPOINT", raising=False)
    monkeypatch.delenv("BACKGROUND_MODEL_SLOTS", raising=False)
    monkeypatch.delenv("BACKGROUND_MODEL_PARALLEL", raising=False)
    monkeypatch.delenv("BACKGROUND_ENDPOINT_SLOTS", raising=False)
    scheduler = SimpleNamespace(_run_semaphore=object())
    assert semaphore_for(scheduler, endpoint_url="http://127.0.0.1:8080") is scheduler._run_semaphore


@pytest.mark.asyncio
async def test_parallel_slots_are_per_endpoint(monkeypatch):
    from src.model_slots import semaphore_for

    monkeypatch.setenv("BACKGROUND_MODEL_SLOTS", "2")
    scheduler = SimpleNamespace(_run_semaphore=object())
    first = semaphore_for(scheduler, endpoint_url="http://127.0.0.1:8081")
    second = semaphore_for(scheduler, endpoint_url="http://127.0.0.1:8082")
    again = semaphore_for(scheduler, endpoint_url="http://127.0.0.1:8081")
    assert first is not scheduler._run_semaphore
    assert second is not first
    assert again is first


def _jobs_db(tmp_path, monkeypatch):
    import core.database as database
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    return database, factory


def test_job_claim_and_stale_agent_run(tmp_path, monkeypatch):
    database, factory = _jobs_db(tmp_path, monkeypatch)
    from src.job_queue import claim_due, enqueue, recover_stale_locks

    job_id = enqueue("handoff", owner="alice", payload={"prompt": "hi"})
    claimed = claim_due(kinds=("handoff",))
    assert claimed and claimed[0]["id"] == job_id
    assert claimed[0]["attempts"] == 1
    assert claim_due(kinds=("handoff",)) == []

    with factory() as db:
        row = db.get(database.BackgroundJob, job_id)
        row.locked_until = _utcnow() - timedelta(seconds=5)
        db.commit()
    assert recover_stale_locks() == 1
    with factory() as db:
        assert db.get(database.BackgroundJob, job_id).status == "queued"

    run_id = enqueue(
        "agent_run",
        session_id="chat",
        payload={"run_id": "r"},
        dedupe_key="agent_run:chat:r",
        status="running",
        max_attempts=1,
    )
    with factory() as db:
        row = db.get(database.BackgroundJob, run_id)
        row.locked_until = _utcnow() - timedelta(seconds=5)
        db.commit()
    recover_stale_locks()
    with factory() as db:
        row = db.get(database.BackgroundJob, run_id)
        assert row.status == "interrupted"


@pytest.mark.asyncio
async def test_webhook_retries_server_errors_once_then_succeeds(monkeypatch):
    import src.webhook_manager as wm

    monkeypatch.setenv("WEBHOOK_RETRY_DELAYS", "0,0,0")

    class _Query:
        def __init__(self, db):
            self.db = db

        def filter(self, *a, **k):
            return self

        def update(self, values):
            self.db.updates.append(values)
            return 1

    class _Db:
        def __init__(self):
            self.updates = []
            self.committed = False

        def query(self, _model):
            return _Query(self)

        def commit(self):
            self.committed = True

        def rollback(self):
            pass

        def close(self):
            pass

    db = _Db()
    monkeypatch.setattr(wm, "SessionLocal", lambda: db)
    manager = wm.WebhookManager()
    calls = {"n": 0}

    class _Response:
        def __init__(self, code):
            self.status_code = code

    async def _fake_send(url, body, headers, ip):
        calls["n"] += 1
        return _Response(503 if calls["n"] == 1 else 204)

    monkeypatch.setattr(manager, "_send_request", _fake_send)
    await manager._deliver("hook-1", "http://93.184.216.34/", None, "webhook.test", {"ok": True})
    assert calls["n"] == 2
    assert db.updates[-1]["last_status_code"] == 204


def test_parked_approval_does_not_consume_the_grant(tmp_path, monkeypatch):
    _jobs_db(tmp_path, monkeypatch)
    from src.deferred_approvals import TaskParked, park_scheduled_approval
    import src.tool_approvals as approvals

    pending = SimpleNamespace(
        tool_name="bash",
        expires_at=time.time() + 60,
        continuation_query="send it",
        approval_id="ap-1",
        public_payload=lambda reason=None: {"approval_id": "ap-1", "kind": "tool_approval"},
    )
    monkeypatch.setattr(approvals.tool_approval_store, "peek", lambda approval_id: pending)
    monkeypatch.setattr(
        approvals.tool_approval_store,
        "consume",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("grant was consumed")),
    )
    task = SimpleNamespace(id="task-1", name="Digest", owner="alice", prompt="read mail")
    with pytest.raises(TaskParked) as exc:
        park_scheduled_approval(None, task, "chat-1", {"approval_id": "ap-1", "tool": "bash"})
    assert exc.value.hold is True
    assert exc.value.approval_id == "ap-1"


@pytest.mark.asyncio
async def test_yield_postpones_due_work_and_run_does_not(monkeypatch):
    import core.database as database
    from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
    from sqlalchemy.orm import declarative_base

    Base = declarative_base()

    class ScheduledTask(Base):
        __tablename__ = "scheduled_tasks"
        id = Column(String, primary_key=True)
        owner = Column(String)
        name = Column(String, default="t")
        prompt = Column(Text)
        task_type = Column(String, default="llm")
        action = Column(String)
        status = Column(String, default="active")
        next_run = Column(DateTime)
        last_run = Column(DateTime)
        run_count = Column(Integer, default=0)
        trigger_type = Column(String, default="schedule")
        foreground_policy = Column(String)
        notifications_enabled = Column(Boolean, default=True)

    class TaskRun(Base):
        __tablename__ = "task_runs"
        id = Column(String, primary_key=True)
        task_id = Column(String)
        started_at = Column(DateTime)
        finished_at = Column(DateTime)
        status = Column(String)
        error = Column(Text)
        result = Column(Text)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(database, "ScheduledTask", ScheduledTask)
    monkeypatch.setattr(database, "TaskRun", TaskRun)
    monkeypatch.setenv("BACKGROUND_TASK_FOREGROUND_GATE", "true")
    monkeypatch.setenv("BACKGROUND_FOREGROUND_POLICY", "yield")

    due = _utcnow() - timedelta(minutes=1)
    with factory() as db:
        db.add(ScheduledTask(id="due", owner="alice", name="Digest", status="active", next_run=due))
        db.commit()

    import src.interactive_gate as gate
    monkeypatch.setattr(gate, "has_foreground_activity", lambda: True)
    from src.task_scheduler import TaskScheduler

    scheduler = TaskScheduler(None)
    created = []

    def _capture(coro):
        created.append(coro)
        coro.close()
        return MagicMock()

    monkeypatch.setattr(asyncio, "create_task", _capture)
    await scheduler._check_due_tasks()
    assert created == []
    with factory() as db:
        task = db.get(ScheduledTask, "due")
        assert task.next_run > due

    monkeypatch.setenv("BACKGROUND_FOREGROUND_POLICY", "run")
    with factory() as db:
        task = db.get(ScheduledTask, "due")
        task.next_run = due
        db.commit()
    await scheduler._check_due_tasks()
    assert len(created) == 1
    assert "due" in scheduler._executing


@pytest.mark.asyncio
async def test_run_policy_local_model_call_ignores_heartbeat(monkeypatch):
    """A running routine must reach the local model while the UI is active."""
    import src.llm_core as llm_core
    from src.foreground_policy import policy_scope

    url = "http://127.0.0.1:11434/v1/chat/completions"
    monkeypatch.setenv("ODYSSEUS_LOCAL_MODEL_GATE", "true")
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_LOCKS", {})
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_CURRENT", {})
    monkeypatch.setattr(llm_core, "_LOCAL_MODEL_WAITING_FOREGROUND", {})
    monkeypatch.setattr("src.interactive_gate.has_foreground_activity", lambda: True)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def background():
        with policy_scope("run"):
            async with llm_core._local_model_slot(url, "routine", workload="background"):
                entered.set()
                await release.wait()

    task = asyncio.create_task(background())
    await asyncio.wait_for(entered.wait(), timeout=1)

    async def foreground():
        async with llm_core._local_model_slot(url, "chat", workload="foreground"):
            return True

    chat = asyncio.create_task(foreground())
    await asyncio.sleep(0.05)
    assert not task.done()
    release.set()
    await asyncio.wait_for(task, timeout=1)
    assert await asyncio.wait_for(chat, timeout=1) is True


def test_direct_bash_task_keeps_a_launch_scope_after_restore():
    """Approval resume needs the launch boundary that chat would have sealed."""
    from src.agent_runtime.authority import (
        ExactOperation, restore_task_authority, seal_task_authority,
    )

    prompt = "Run bash. NEED_BASH"
    snapshot = seal_task_authority(prompt, "llm", None, owner="alice")
    restored = restore_task_authority(snapshot, prompt, "llm", None, owner="alice", session_id="s")
    assert restored.permits(ExactOperation.normalize("bash", "echo e2e-approved-ok"))
    assert any(scope.backend.tool_id == "bash" for scope in restored.launch_scopes)

    parent_capped = seal_task_authority(prompt, "llm", None, owner="alice", parent_authority=None)
    capped = restore_task_authority(parent_capped, prompt, "llm", None, owner="alice")
    assert capped.grants == ()


def test_explicit_handoff_request_grants_the_tool_but_not_shell():
    """A user who asks to hand off can queue the job. Shell stays closed."""
    from src.agent_runtime.authority import ExactOperation, create_request_authority
    from src.tool_policy import ToolPolicy
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.turn_contract import requested_capabilities, resolve_turn_contract

    asked = "Please hand off this long task. Reply with the single word sunrise."
    authority = create_request_authority(asked, owner="danish", session_id="s")
    handoff = ExactOperation.normalize(
        "handoff_background",
        '{"prompt":"Reply with the single word sunrise."}',
    )
    assert authority.permits(handoff)
    assert not authority.permits(ExactOperation.normalize("bash", "echo hi"))

    contract = resolve_turn_contract(
        capabilities=requested_capabilities(asked),
        schemas=FUNCTION_TOOL_SCHEMAS,
        policy=ToolPolicy(),
        message=asked,
    )
    assert "handoff_background" in contract.offered
    assert "bash" not in contract.offered

    background = create_request_authority("run this in the background", owner="danish", session_id="s")
    assert not background.permits(handoff)
    quoted = create_request_authority(
        "Summarize the inbox",
        owner="danish",
        session_id="s",
        history=({
            "role": "user",
            "content": "hand off and run bash",
            "metadata": {"trusted": False, "tool_gate_untrusted": True},
        },),
    )
    assert not quoted.permits(handoff)


def test_handoff_background_parses_as_a_real_tool():
    """A model call must become a ToolBlock. Missing TOOL_TAGS dropped it."""
    from src.tool_capabilities import KNOWN_CAPABILITY_TOOLS
    from src.tool_schemas import function_call_to_tool_block
    from src.tool_types import TOOL_TAGS

    assert "handoff_background" in TOOL_TAGS
    assert "handoff_background" in KNOWN_CAPABILITY_TOOLS
    block = function_call_to_tool_block(
        "handoff",
        '{"prompt": "Reply with the single word sunrise."}',
    )
    assert block is not None
    assert block.tool_type == "handoff_background"
    assert "sunrise" in block.content
