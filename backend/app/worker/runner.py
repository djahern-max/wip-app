"""The worker loop (D-19). Reads the tenant list from the global ``tenant`` table,
then **for each tenant** opens ``tenant_session(engine, tenant_id)`` and claims at
most one due task. One transaction never holds two tenant contexts:

1. claim   — one short transaction per tenant (sweep + ``FOR UPDATE SKIP LOCKED``)
2. work    — the task function opens its own ``tenant_session``(s) for the same tenant
3. finish  — a third short transaction, conditional on still holding the lease

``LISTEN`` on ``NOTIFY_CHANNEL`` (payload: a tenant id) wakes an idle loop early;
polling every ``WORKER_POLL_SECONDS`` is the fallback. Log lines carry task id,
kind, tenant id, attempt and outcome; never a payload, a filename or a token.
"""

import logging
import os
import socket
import threading
import time
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.db import tenant_session, untenanted_session
from app.tenancy.models import Tenant
from app.worker import queue
from app.worker.registry import get_task

log = logging.getLogger("app.worker")

# Modules whose import registers the production task kinds.
TASK_MODULES: tuple[str, ...] = ("app.ingest.imports",)


def load_task_modules() -> None:
    import importlib

    for name in TASK_MODULES:
        importlib.import_module(name)


class Worker:
    def __init__(
        self,
        engine: Engine,
        *,
        name: str | None = None,
        poll_seconds: float | None = None,
        lease_seconds: int | None = None,
        listen: bool = True,
    ) -> None:
        s = get_settings()
        self.engine = engine
        self.name = name or f"{socket.gethostname()}:{os.getpid()}"
        self.poll_seconds = s.worker_poll_seconds if poll_seconds is None else poll_seconds
        self.lease_seconds = s.worker_lease_seconds if lease_seconds is None else lease_seconds
        self.listen = listen
        self.stop_event = threading.Event()
        self._listener = None

    # --- one pass over every tenant ------------------------------------------------

    def tenant_ids(self) -> list[UUID]:
        with untenanted_session(self.engine) as s:
            return list(s.execute(select(Tenant.id).order_by(Tenant.created_at)).scalars())

    def run_once(self) -> int:
        """Claim and run at most one task per tenant. Returns the number of tasks run."""
        ran = 0
        for tenant_id in self.tenant_ids():
            with tenant_session(self.engine, tenant_id) as s:
                queue.sweep_expired(s)
                task = queue.claim_one(s, worker=self.name, lease_seconds=self.lease_seconds)
                claimed = (
                    None
                    if task is None
                    else (task.id, task.kind, dict(task.payload), task.attempts, task.max_attempts)
                )
            if claimed is None:
                continue
            self._run(tenant_id, *claimed)
            ran += 1
        return ran

    def _run(
        self,
        tenant_id: UUID,
        task_id: UUID,
        kind: str,
        payload: dict,
        attempt: int,
        max_attempts: int,
    ) -> None:
        prefix = f"task={task_id} kind={kind} tenant={tenant_id} attempt={attempt}/{max_attempts}"
        log.info("%s start", prefix)
        started = time.monotonic()
        try:
            fn = get_task(kind)
            fn(tenant_id, engine=self.engine, **payload)
        except Exception as exc:  # noqa: BLE001 - every failure is recorded on the task
            error = queue.describe_error(exc)
            permanent = isinstance(exc, queue.PermanentTaskError)
            with tenant_session(self.engine, tenant_id) as s:
                held = queue.fail(s, task_id, worker=self.name, error=error, permanent=permanent)
            outcome = ("failed (no retry)" if permanent else "failed") if held else "lease lost"
            log.warning("%s outcome=%s error=%s", prefix, outcome, error)
        else:
            with tenant_session(self.engine, tenant_id) as s:
                held = queue.complete(s, task_id, worker=self.name)
            outcome = "succeeded" if held else "lease lost"
            log.info("%s outcome=%s", prefix, outcome)
        elapsed = time.monotonic() - started
        if elapsed > 0.8 * self.lease_seconds:
            log.warning(
                "%s ran %.1fs, over 80%% of the %ds lease: split this task's work",
                prefix,
                elapsed,
                self.lease_seconds,
            )

    # --- the loop --------------------------------------------------------------------

    def run_forever(self) -> None:
        load_task_modules()
        log.info(
            "worker %s started (poll %.1fs, lease %ds)",
            self.name,
            self.poll_seconds,
            self.lease_seconds,
        )
        try:
            while not self.stop_event.is_set():
                try:
                    ran = self.run_once()
                except Exception as exc:  # noqa: BLE001 - keep polling after a bad pass
                    log.error("worker pass failed: %s", queue.describe_error(exc))
                    ran = 0
                if ran == 0:
                    self._wait()
        finally:
            self._close_listener()
            log.info("worker %s stopped", self.name)

    def stop(self) -> None:
        self.stop_event.set()

    def _wait(self) -> None:
        deadline = time.monotonic() + self.poll_seconds
        listener = self._listener_conn() if self.listen else None
        while not self.stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            slice_ = min(remaining, 0.5)
            if listener is None:
                time.sleep(slice_)
                continue
            try:
                for _ in listener.notifies(timeout=slice_, stop_after=1):
                    return  # a tenant has work: run a pass now
            except Exception as exc:  # noqa: BLE001 - fall back to polling
                log.warning("listener failed (%s); polling only", type(exc).__name__)
                self._close_listener()
                listener = None

    def _listener_conn(self):
        if self._listener is None:
            import psycopg

            url = make_url(get_settings().database_url).set(drivername="postgresql")
            conn = psycopg.connect(url.render_as_string(hide_password=False), autocommit=True)
            conn.execute(f"LISTEN {queue.NOTIFY_CHANNEL}")
            self._listener = conn
        return self._listener

    def _close_listener(self) -> None:
        if self._listener is not None:
            try:
                self._listener.close()
            finally:
                self._listener = None
