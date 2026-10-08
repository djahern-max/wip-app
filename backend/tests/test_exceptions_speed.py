"""F09 criteria 12 and 13: the queue's and the company picker's statement counts do not
grow with the number of exceptions, and the run on a tenant with 1,000 sold estimates
(work areas and cost lines loaded) and 200 jobs finishes inside the Plan's budget. The
rows of criterion 12 are inserted directly (what is under test is the count)."""

import time
import uuid
from datetime import UTC, datetime

from sqlalchemy import Engine, select

from app.core.db import tenant_session
from app.domain.config.audit import Actor
from app.domain.exceptions.models import ReviewException
from app.domain.exceptions.run import refresh
from app.domain.jobs import service as jobs
from app.domain.jobs.models import Job
from tests.billing_helpers import billing_policy
from tests.config_helpers import run_until_quiet
from tests.conftest import Seed
from tests.estimate_helpers import build_workbook, upload_template
from tests.job_helpers import ELM_ID, Tenant, make_tenant
from tests.test_board_speed import _statements

BUDGET_SECONDS = 20.0  # the Plan's budget (criterion 13); the observed time is in the build notes


def _seed_exceptions(engine: Engine, tenant_id: uuid.UUID, job_id: str, n: int, start: int) -> None:
    now = datetime.now(UTC)
    with tenant_session(engine, tenant_id) as s:
        for i in range(start, start + n):
            s.add(
                ReviewException(
                    tenant_id=tenant_id,
                    code="DEPOSIT_NOT_IDENTIFIED",
                    severity="warn",
                    subject_type="job",
                    subject_id=uuid.UUID(job_id),
                    item_key=f"speed-{i}",
                    message=f"Speed row {i}.",
                    detail={"billing_id": f"speed-{i}", "reason": "test"},
                    status="open" if i % 3 else "dismissed",
                    first_raised_at=now,
                    last_raised_at=now,
                    dismissed_at=None if i % 3 else now,
                    dismissed_by=None if i % 3 else _any_user(s),
                )
            )


def _any_user(s) -> uuid.UUID:
    from app.tenancy.models import User

    return s.execute(select(User.id)).scalars().first()


def test_statement_counts_are_the_same_at_10_and_at_1000_exceptions(
    seed: Seed, rw_engine: Engine, login_as, fresh_tenant: uuid.UUID
) -> None:
    t: Tenant = make_tenant(seed, rw_engine, login_as, fresh_tenant)
    billing_policy(rw_engine, seed, fresh_tenant)
    job = t.new_job(ELM_ID)
    t.link(job["id"], "elm", in_progress=True)
    run_until_quiet(t.engine)
    paths = ("/api/exceptions", "/api/session/tenants", "/api/jobs", f"/api/jobs/{job['id']}")
    _seed_exceptions(rw_engine, fresh_tenant, job["id"], 10, 0)
    at_10 = {p: _statements(t.client, p) for p in paths}
    assert t.get("/api/exceptions")["total"] >= 10
    _seed_exceptions(rw_engine, fresh_tenant, job["id"], 990, 10)
    at_1000 = {p: _statements(t.client, p) for p in paths}
    assert t.get("/api/exceptions")["total"] >= 1000
    assert at_1000 == at_10, (at_10, at_1000)


def _thousand_estimates() -> bytes:
    estimates, areas, costs = [], [], []
    for i in range(1000):
        eid = f"EST9{i:05d}"
        estimates.append(
            {
                "estimate_id": eid,
                "estimator": "Speed",
                "client": f"Client {i % 50}",
                "jobsite": f"{i} Speed Road",
                "name": f"Speed job {i}",
                "status": "Sold",
                "price": "3000.00",
                "estimate_date": "2026-03-01",
            }
        )
        for n in (1, 2, 3):
            areas.append(
                {
                    "estimate_id": eid,
                    "order": n,
                    "kept": "Y",
                    "name": f"Area {n}",
                    "price": "1000.00",
                }
            )
            costs.append(
                {
                    "estimate_id": eid,
                    "order": n,
                    "cost_code": "110",
                    "hours": "10",
                    "amount": "600.00",
                }
            )
    return build_workbook(estimates=estimates, work_areas=areas, costs=costs)


def test_the_run_on_a_thousand_estimates_and_two_hundred_jobs_is_inside_the_budget(
    seed: Seed, rw_engine: Engine, login_as, fresh_tenant: uuid.UUID
) -> None:
    t: Tenant = make_tenant(seed, rw_engine, login_as, fresh_tenant, load=False)
    billing_policy(rw_engine, seed, fresh_tenant)
    upload_template(t.client, _thousand_estimates(), "speed.xlsx")
    run_until_quiet(t.engine)
    assert t.get("/api/estimates")["total"] == 1000
    actor = Actor(user_id=seed.users["rotate_me"].id)
    with tenant_session(rw_engine, fresh_tenant) as s:
        sold = list(
            s.execute(select(jobs.Estimate.id).order_by(jobs.Estimate.external_id)).scalars()
        )
        division = t.divisions["LS"]
        for estimate_id in sold[:200]:
            jobs.create_job_from_estimate(
                s,
                fresh_tenant,
                estimate_id=estimate_id,
                division_id=division,
                name=None,
                actor=actor,
            )
    with tenant_session(rw_engine, fresh_tenant) as s:
        assert s.execute(select(Job.id)).scalars().all().__len__() == 200
    started = time.perf_counter()
    with tenant_session(rw_engine, fresh_tenant) as s:
        result = refresh(s, fresh_tenant)
    first = time.perf_counter() - started
    started = time.perf_counter()
    with tenant_session(rw_engine, fresh_tenant) as s:
        second = refresh(s, fresh_tenant)
    again = time.perf_counter() - started
    print(f"\nF09 run: first {first:.2f}s ({result.raised} raised), second {again:.2f}s")
    assert result.raised >= 800  # EST_UNATTACHED on the 800 unattached sold estimates
    assert second.writes == 0
    assert first < BUDGET_SECONDS and again < BUDGET_SECONDS, (first, again)
