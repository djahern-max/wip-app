"""The cost run (F10; D-19): one worker task per tenant that applies every cost document's
latest raw version and fills ``job_id`` from the aliases (``sync.apply_everything``), or
only the job ids after a link or an unlink.

Triggers (``request_cost_run``): a job link or unlink (``app/domain/jobs/service.py``; job
ids only), a backfill that finished and a change poll that stored an Item (every document:
an item's expense account may have changed), and once per tenant at worker start-up, so a
deploy that adds this feature fills the table without waiting for a poll. The QuickBooks
``normalize`` task applies each changed document itself; this run is the catch-all. Dedupe
keys collapse a burst into one queued run. The run writes no audit row.
"""

import logging
from uuid import UUID

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.domain.config.policy import history_start
from app.domain.cost import sync
from app.worker.queue import enqueue
from app.worker.registry import task
from app.worker.runner import STARTUP_HOOKS

log = logging.getLogger("app.cost")

COST_RUN_KIND = "cost.run"
DEDUPE_FULL = "cost:full"
DEDUPE_JOBS = "cost:jobs"


@task(COST_RUN_KIND)
def cost_run(tenant_id: UUID, *, engine: Engine, full: bool) -> None:
    from app.core.db import tenant_session
    from app.domain.exceptions.run import request_refresh

    with tenant_session(engine, tenant_id) as db:
        if full:
            counts = sync.apply_everything(db, tenant_id, history_start=history_start(db))
            changed = counts["changed"]
        else:
            changed = sync.refill_job_ids(db, tenant_id)
            counts = {"changed": changed}
        if changed:
            request_refresh(db, tenant_id)  # F09: the sentences follow the lines
    log.info("cost run full=%s %s tenant=%s", full, counts, tenant_id)


def request_cost_run(db: Session, tenant_id: UUID, *, full: bool) -> None:
    """Queue one run in the caller's transaction (the notify goes with the commit)."""
    enqueue(
        db,
        tenant_id,
        COST_RUN_KIND,
        {"full": full},
        dedupe_key=DEDUPE_FULL if full else DEDUPE_JOBS,
    )


def ensure_cost_lines(db: Session, tenant_id: UUID) -> None:
    """Worker start-up: one full run per tenant, deduped."""
    request_cost_run(db, tenant_id, full=True)


STARTUP_HOOKS.append(ensure_cost_lines)
