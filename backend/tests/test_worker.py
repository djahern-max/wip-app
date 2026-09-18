"""The worker queue (D-19; plan call 1 with the owner's lease-lost addition):
explicit tenant context, one context per transaction, SKIP LOCKED, lease expiry,
conditional completion, backoff, dedupe, NOTIFY wake-up."""

import logging
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, event, select, text, update

from app.core.db import tenant_session
from app.ingest.models import ImportBatch
from app.tenancy.models import Tenant
from app.worker import queue
from app.worker.models import Task
from app.worker.registry import TASKS, TaskRegistrationError, task
from app.worker.runner import Worker
from tests.conftest import Seed

# --- test-only task kinds (registered here, never by the application) ----------------------

RESULTS: dict[str, list] = {}
_lock = threading.Lock()


def _record(key: str, value) -> None:
    with _lock:
        RESULTS.setdefault(key, []).append(value)


@task("_test.context")
def _context_task(tenant_id: uuid.UUID, *, engine: Engine, key: str) -> None:
    with tenant_session(engine, tenant_id) as s:
        setting = s.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar_one()
        batches = {b.tenant_id for b in s.execute(select(ImportBatch)).scalars()}
    _record(key, {"setting": setting, "batch_tenants": batches, "tenant_id": tenant_id})


@task("_test.record")
def _record_task(tenant_id: uuid.UUID, *, engine: Engine, key: str, sleep_ms: int = 0) -> None:
    if sleep_ms:  # an int: task payloads carry no float (the engine would refuse it)
        time.sleep(sleep_ms / 1000)
    _record(key, tenant_id)


@task("_test.fail")
def _fail_task(tenant_id: uuid.UUID, *, engine: Engine, key: str) -> None:
    _record(key, "attempt")
    raise RuntimeError("secret-file-content-in-message")


def test_registration_refuses_a_wrong_first_parameter() -> None:
    with pytest.raises(TaskRegistrationError, match="must be tenant_id"):

        @task("_test.bad")
        def bad(import_batch_id: str, tenant_id: uuid.UUID) -> None:  # noqa: ARG001
            pass

    with pytest.raises(TaskRegistrationError, match="must be tenant_id"):

        @task("_test.bad2")
        def bad2(*, engine: Engine) -> None:  # noqa: ARG001
            pass

    assert "_test.bad" not in TASKS and "_test.bad2" not in TASKS
    with pytest.raises(TaskRegistrationError, match="already registered"):

        @task("_test.record")
        def duplicate(tenant_id: uuid.UUID) -> None:  # noqa: ARG001
            pass


# --- helpers ---------------------------------------------------------------------------------


def _tenants(engine: Engine) -> list[uuid.UUID]:
    with engine.connect() as c:
        return list(c.execute(select(Tenant.id)).scalars())


@pytest.fixture
def clean_queue(seed: Seed, owner_engine: Engine) -> Iterator[None]:
    """No non-terminal task survives from another test, before or after."""

    def drain() -> None:
        for tid in _tenants(owner_engine):
            with tenant_session(owner_engine, tid) as s:
                s.execute(
                    update(Task)
                    .where(Task.status.in_(("queued", "running")))
                    .values(status="failed", last_error="drained by test", locked_by=None)
                )

    drain()
    yield
    drain()


def _enqueue(engine: Engine, tenant_id: uuid.UUID, kind: str, payload: dict, **kw) -> uuid.UUID:
    with tenant_session(engine, tenant_id) as s:
        row = queue.enqueue(s, tenant_id, kind, payload, **kw)
        assert row is not None
        return row.id


def _task(engine: Engine, tenant_id: uuid.UUID, task_id: uuid.UUID) -> Task:
    with tenant_session(engine, tenant_id) as s:
        row = s.get(Task, task_id)
        s.expunge(row)
        return row


def _set(engine: Engine, tenant_id: uuid.UUID, task_id: uuid.UUID, **values) -> None:
    with tenant_session(engine, tenant_id) as s:
        s.execute(update(Task).where(Task.id == task_id).values(**values))


def _worker(engine: Engine, name: str, **kw) -> Worker:
    return Worker(engine, name=name, listen=False, poll_seconds=0.05, **kw)


# --- explicit tenant context ------------------------------------------------------------------


def test_task_runs_with_the_tenant_context_it_was_queued_in(
    seed: Seed, rw_engine: Engine, owner_engine: Engine, clean_queue: None
) -> None:
    with tenant_session(owner_engine, seed.tenant_b) as s:
        s.add(
            ImportBatch(
                tenant_id=seed.tenant_b,
                source_kind="unparsed_file",
                sha256=uuid.uuid4().hex * 2,
                byte_size=1,
                original_filename="b.bin",
                object_key=f"tenant/{seed.tenant_b}/imports/b.bin",
            )
        )
    key = uuid.uuid4().hex
    task_id = _enqueue(rw_engine, seed.tenant_a, "_test.context", {"key": key})
    assert _worker(rw_engine, "w-ctx").run_once() == 1
    (result,) = RESULTS[key]
    assert result["setting"] == str(seed.tenant_a) == str(result["tenant_id"])
    assert seed.tenant_b not in result["batch_tenants"]  # tenant A cannot read B's batch
    assert _task(rw_engine, seed.tenant_a, task_id).status == "succeeded"


def test_each_claim_and_task_is_one_transaction_with_one_tenant_context(
    seed: Seed, rw_engine: Engine, clean_queue: None
) -> None:
    """Across a pass with tasks in two tenants, no transaction sets app.tenant_id twice."""
    per_connection: dict[int, int] = {}
    maxima: list[int] = []

    def on_begin(conn):
        per_connection[id(conn)] = 0

    def on_execute(conn, cursor, statement, parameters, context, executemany):
        if "set_config('app.tenant_id'" in statement:
            per_connection[id(conn)] = per_connection.get(id(conn), 0) + 1

    def on_end(conn):
        maxima.append(per_connection.pop(id(conn), 0))

    listeners = [
        ("begin", on_begin),
        ("before_cursor_execute", on_execute),
        ("commit", on_end),
        ("rollback", on_end),
    ]
    for name, fn in listeners:
        event.listen(rw_engine, name, fn)
    try:
        ka, kb = uuid.uuid4().hex, uuid.uuid4().hex
        _enqueue(rw_engine, seed.tenant_a, "_test.context", {"key": ka})
        _enqueue(rw_engine, seed.tenant_b, "_test.context", {"key": kb})
        maxima.clear()
        assert _worker(rw_engine, "w-iso").run_once() == 2
    finally:
        for name, fn in listeners:
            event.remove(rw_engine, name, fn)
    assert maxima and max(maxima) == 1
    assert maxima.count(1) >= 6  # claim, work and finish for each of two tenants
    assert RESULTS[ka][0]["setting"] == str(seed.tenant_a)
    assert RESULTS[kb][0]["setting"] == str(seed.tenant_b)


# --- queue behaviour ------------------------------------------------------------------------------


def test_two_workers_never_run_the_same_task(
    seed: Seed, rw_engine: Engine, clean_queue: None
) -> None:
    key = uuid.uuid4().hex
    ids = [
        _enqueue(rw_engine, seed.tenant_a, "_test.record", {"key": key, "sleep_ms": 20})
        for _ in range(16)
    ]
    workers = [_worker(rw_engine, "w-1"), _worker(rw_engine, "w-2")]
    barrier = threading.Barrier(2)

    def loop(w: Worker) -> None:
        barrier.wait()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if w.run_once() == 0:
                with tenant_session(rw_engine, seed.tenant_a) as s:
                    left = s.execute(
                        select(Task.id).where(Task.id.in_(ids), Task.status != "succeeded")
                    ).first()
                if left is None:
                    return
                time.sleep(0.01)

    threads = [threading.Thread(target=loop, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert len(RESULTS[key]) == 16
    with tenant_session(rw_engine, seed.tenant_a) as s:
        rows = s.execute(
            select(Task.status, Task.attempts, Task.locked_by).where(Task.id.in_(ids))
        ).all()
    assert len(rows) == 16
    assert all(r == ("succeeded", 1, None) for r in rows)


def test_expired_lease_is_reclaimed_and_attempts_increment(
    seed: Seed, rw_engine: Engine, clean_queue: None
) -> None:
    task_id = _enqueue(rw_engine, seed.tenant_a, "_test.record", {"key": "x"})
    with tenant_session(rw_engine, seed.tenant_a) as s:
        first = queue.claim_one(s, worker="w-a", lease_seconds=300)
        assert first is not None and first.id == task_id and first.attempts == 1
    with tenant_session(rw_engine, seed.tenant_a) as s:
        assert queue.claim_one(s, worker="w-b", lease_seconds=300) is None  # still leased
    _set(rw_engine, seed.tenant_a, task_id, locked_until=datetime.now(UTC) - timedelta(seconds=1))
    with tenant_session(rw_engine, seed.tenant_a) as s:
        second = queue.claim_one(s, worker="w-b", lease_seconds=300)
        assert second is not None and second.id == task_id
        assert (second.attempts, second.locked_by, second.status) == (2, "w-b", "running")


def test_late_completion_after_a_lost_lease_changes_nothing(
    seed: Seed, rw_engine: Engine, clean_queue: None
) -> None:
    """Owner addition to plan call 1: A outlives its lease, B reclaims and completes;
    A's late completion or failure writes nothing; succeeded exactly once, attempts 2."""
    task_id = _enqueue(rw_engine, seed.tenant_a, "_test.record", {"key": "x"})
    with tenant_session(rw_engine, seed.tenant_a) as s:
        assert queue.claim_one(s, worker="w-a", lease_seconds=300).id == task_id
    _set(rw_engine, seed.tenant_a, task_id, locked_until=datetime.now(UTC) - timedelta(seconds=1))
    with tenant_session(rw_engine, seed.tenant_a) as s:
        assert queue.claim_one(s, worker="w-b", lease_seconds=300).locked_by == "w-b"
    with tenant_session(rw_engine, seed.tenant_a) as s:
        assert queue.complete(s, task_id, worker="w-b") is True
    with tenant_session(rw_engine, seed.tenant_a) as s:
        assert queue.complete(s, task_id, worker="w-a") is False
        assert queue.fail(s, task_id, worker="w-a", error="late") is False
    row = _task(rw_engine, seed.tenant_a, task_id)
    assert (row.status, row.attempts, row.locked_by, row.last_error) == ("succeeded", 2, None, None)
    assert row.finished_at is not None


def test_worker_logs_lease_lost_when_reclaimed_mid_run(
    seed: Seed, rw_engine: Engine, clean_queue: None, caplog: pytest.LogCaptureFixture
) -> None:
    key = uuid.uuid4().hex
    task_id = _enqueue(rw_engine, seed.tenant_a, "_test.record", {"key": key, "sleep_ms": 1200})
    slow = _worker(rw_engine, "w-slow", lease_seconds=1)
    fast = _worker(rw_engine, "w-fast", lease_seconds=300)
    caplog.set_level(logging.INFO, logger="app.worker")
    t = threading.Thread(target=slow.run_once)
    t.start()
    time.sleep(1.05)  # the 1 s lease has expired while w-slow still sleeps
    assert fast.run_once() == 1
    t.join(10)
    row = _task(rw_engine, seed.tenant_a, task_id)
    assert (row.status, row.attempts) == ("succeeded", 2)
    assert len(RESULTS[key]) == 2  # the reclaimed run executed too: tasks are idempotent
    messages = [r.getMessage() for r in caplog.records if r.name == "app.worker"]
    assert any("w-slow" not in m and "outcome=succeeded" in m for m in messages)
    assert any("outcome=lease lost" in m for m in messages)
    assert any("over 80%" in m for m in messages)  # 1.2 s run against a 1 s lease


def test_failure_backs_off_then_fails_at_max_attempts_with_a_type_only_error(
    seed: Seed, rw_engine: Engine, clean_queue: None
) -> None:
    key = uuid.uuid4().hex
    task_id = _enqueue(rw_engine, seed.tenant_a, "_test.fail", {"key": key}, max_attempts=3)
    w = _worker(rw_engine, "w-fail")
    for attempt, delay in ((1, 30), (2, 60)):
        assert w.run_once() == 1
        row = _task(rw_engine, seed.tenant_a, task_id)
        assert (row.status, row.attempts, row.last_error) == ("queued", attempt, "RuntimeError")
        assert "secret-file-content" not in (row.last_error or "")
        wait = (row.run_after - datetime.now(UTC)).total_seconds()
        assert delay - 5 < wait <= delay
        assert w.run_once() == 0  # not due yet
        _set(rw_engine, seed.tenant_a, task_id, run_after=datetime.now(UTC))
    assert w.run_once() == 1
    row = _task(rw_engine, seed.tenant_a, task_id)
    assert (row.status, row.attempts) == ("failed", 3) and row.finished_at is not None
    assert w.run_once() == 0
    assert len(RESULTS[key]) == 3


def test_backoff_curve_is_capped() -> None:
    assert [queue.backoff_delay(n).total_seconds() for n in (1, 2, 3, 4, 5, 6)] == [
        30,
        60,
        120,
        240,
        480,
        900,
    ]


def test_expired_lease_at_max_attempts_is_swept_to_failed(
    seed: Seed, rw_engine: Engine, clean_queue: None
) -> None:
    task_id = _enqueue(rw_engine, seed.tenant_a, "_test.record", {"key": "x"}, max_attempts=1)
    with tenant_session(rw_engine, seed.tenant_a) as s:
        queue.claim_one(s, worker="w-a", lease_seconds=300)
    _set(rw_engine, seed.tenant_a, task_id, locked_until=datetime.now(UTC) - timedelta(seconds=1))
    assert _worker(rw_engine, "w-sweep").run_once() == 0
    row = _task(rw_engine, seed.tenant_a, task_id)
    assert (row.status, row.last_error) == ("failed", "lease expired at max_attempts")


def test_dedupe_key_blocks_a_second_enqueue_while_the_first_is_open(
    seed: Seed, rw_engine: Engine, clean_queue: None
) -> None:
    dk = uuid.uuid4().hex
    with tenant_session(rw_engine, seed.tenant_a) as s:
        first = queue.enqueue(s, seed.tenant_a, "_test.record", {"key": "x"}, dedupe_key=dk)
        assert first is not None
        assert queue.enqueue(s, seed.tenant_a, "_test.record", {"key": "x"}, dedupe_key=dk) is None
        # Another kind, or another tenant, is not a duplicate.
        assert queue.enqueue(s, seed.tenant_a, "_test.context", {"key": "x"}, dedupe_key=dk)
        first_id = first.id
    with tenant_session(rw_engine, seed.tenant_b) as s:
        assert queue.enqueue(s, seed.tenant_b, "_test.record", {"key": "x"}, dedupe_key=dk)
    with tenant_session(rw_engine, seed.tenant_a) as s:
        queue.claim_one(s, worker="w-d", lease_seconds=300)  # running still blocks
        assert queue.enqueue(s, seed.tenant_a, "_test.record", {"key": "x"}, dedupe_key=dk) is None
    _set(rw_engine, seed.tenant_a, first_id, status="succeeded", locked_by=None)
    with tenant_session(rw_engine, seed.tenant_a) as s:
        again = queue.enqueue(s, seed.tenant_a, "_test.record", {"key": "x"}, dedupe_key=dk)
        assert again is not None and again.id != first_id


def test_notify_wakes_an_idle_worker_in_under_a_second(
    seed: Seed, rw_engine: Engine, clean_queue: None
) -> None:
    w = Worker(rw_engine, name="w-listen", listen=True, poll_seconds=30)
    t = threading.Thread(target=w.run_forever, daemon=True)
    t.start()
    try:
        time.sleep(0.8)  # the first pass found nothing; the loop is now waiting on LISTEN
        key = uuid.uuid4().hex
        started = time.monotonic()
        _enqueue(rw_engine, seed.tenant_a, "_test.record", {"key": key})
        while key not in RESULTS and time.monotonic() - started < 5:
            time.sleep(0.02)
        elapsed = time.monotonic() - started
    finally:
        w.stop()
        t.join(5)
    assert key in RESULTS and elapsed < 1.0, elapsed
    assert not t.is_alive()
