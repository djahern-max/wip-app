"""F05.1 (D-28): ``scripts/delete_tenant.py`` on a scratch tenant holding rows in
every tenant table and two objects in the local store. The owner role deletes it all
in one transaction with the append-only triggers suspended for that transaction
only; the protected slug is refused; the application role fails closed."""

import io
import json
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError

from app.audit.models import AuditLog
from app.core.config import get_settings
from app.core.db import tenant_session, untenanted_session
from app.core.storage import LocalObjectStore
from app.domain.config import burden, policy
from app.domain.config.audit import Actor
from app.domain.config.service import create_division
from app.tenancy import catalog
from app.tenancy.models import RlsProbe, Tenant, UserSession
from scripts import delete_tenant
from tests.config_helpers import load_rye_beach_rules, run_until_quiet, upload_chart
from tests.conftest import CSRF, Seed
from tests.qbo_helpers import FakeIntuit, FixtureCompany, connect_directly, installed
from tests.test_qbo_tasks import _drain

ADMIN = "rotate_me"


def _slug(engine: Engine, tenant_id: uuid.UUID) -> str:
    with untenanted_session(engine) as db:
        return db.execute(select(Tenant.slug).where(Tenant.id == tenant_id)).scalar_one()


def _counts(engine: Engine, tenant_id: uuid.UUID) -> dict[str, int]:
    with engine.connect() as conn:
        tables = catalog.tenant_tables(conn)
    return delete_tenant.row_counts(engine, tenant_id, tuple(tables))


def _fill_every_tenant_table(
    seed: Seed, tenant_id: uuid.UUID, admin: TestClient, rw_engine: Engine, owner_engine: Engine
) -> tuple[FakeIntuit, str]:
    """Rows in every tenant table: a backfilled QuickBooks company (connection,
    sync_run, raw_record, task, customer, billing, billing_line, payment,
    payment_application, audit_log), a chart and rules (gl_account, account_map,
    account_suggest_rule, import_batch, cost_category), a division, a policy, a burden
    rate, a probe row; membership rows come with the tenant."""
    fake = FakeIntuit()
    FixtureCompany().serve(fake)
    with installed(fake):
        cid = connect_directly(rw_engine, tenant_id, fake)
        with tenant_session(rw_engine, tenant_id) as db:
            from app.ingest.models import Connection
            from app.integrations.qbo.schedule import start_backfill

            start_backfill(db, tenant_id, db.get(Connection, cid))
        _drain(rw_engine, tenant_id)
    actor = Actor(user_id=seed.users[ADMIN].id)
    load_rye_beach_rules(rw_engine, tenant_id)
    chart = "account_no,name,type\n5100,Labor,Expense\n5410,Snow labor,Expense\n"
    upload_chart(admin, chart.encode(), "chart.csv")
    run_until_quiet(rw_engine)
    r = admin.post(
        "/api/imports",
        data={"source_kind": "unparsed_file"},
        files={"file": ("notes.bin", io.BytesIO(b"second object"), "application/octet-stream")},
        headers=CSRF,
    )
    assert r.status_code == 201, r.text
    with tenant_session(rw_engine, tenant_id) as db:
        create_division(
            db, tenant_id, code="ZZ", name="Scratch", code_digit="9", sort_order=99, actor=actor
        )
        policy.set_policy(
            db, tenant_id, "timezone", "America/New_York", decision_ref="t", actor=actor
        )
        burden.add_burden_rate(
            db,
            tenant_id,
            division_id=None,
            effective_from=date(2026, 1, 1),
            effective_to=None,
            rate=Decimal("0.3250"),
            basis_note="test",
            actor=actor,
        )
    with tenant_session(owner_engine, tenant_id) as db:
        db.add(RlsProbe(tenant_id=tenant_id, label="to-delete"))
    return fake, _slug(rw_engine, tenant_id)


def test_the_owner_role_deletes_everything_once_the_slug_is_typed_back(
    seed: Seed, fresh_tenant: uuid.UUID, rw_engine: Engine, owner_engine: Engine, login_as
) -> None:
    admin = login_as(ADMIN, tenant=fresh_tenant)
    fake, slug = _fill_every_tenant_table(seed, fresh_tenant, admin, rw_engine, owner_engine)
    settings = get_settings()
    store = LocalObjectStore(settings.local_object_store_dir)
    before = _counts(owner_engine, fresh_tenant)
    empty = sorted(t for t, n in before.items() if n == 0)
    assert empty == [], f"the scratch tenant must hold rows in every tenant table: {empty}"
    assert len(store.list_keys(fresh_tenant)) == 2
    # The admin's session points at the tenant; it must not block the delete.
    with rw_engine.connect() as conn:
        assert conn.execute(
            select(UserSession.id).where(UserSession.active_tenant_id == fresh_tenant)
        ).first()
    # Another tenant's rows and objects are untouched by all of this.
    other_before = _counts(owner_engine, seed.tenant_a)

    p = delete_tenant.plan(owner_engine, store, settings, slug)
    assert p.counts == before and len(p.object_keys) == 2
    assert p.order.index("raw_record") < p.order.index("import_batch")  # child first
    assert p.order.index("import_batch") < p.order.index("task")
    assert p.order.index("billing_line") < p.order.index("billing")
    with pytest.raises(delete_tenant.Refused, match="typed back does not match"):
        delete_tenant.run(
            owner_engine, store, settings, slug=slug, typed=slug + "x", operator="tester"
        )
    assert _counts(owner_engine, fresh_tenant) == before
    with installed(fake):
        out = delete_tenant.run(
            owner_engine, store, settings, slug=slug, typed=slug, operator="tester"
        )
    assert out.revoked is True and fake.revoked  # the refresh token went to Intuit
    assert out.objects_deleted == 2 and store.list_keys(fresh_tenant) == []
    assert out.rows_deleted == before
    assert all(n == 0 for n in _counts(owner_engine, fresh_tenant).values())
    with untenanted_session(owner_engine) as db:
        assert db.execute(select(Tenant.id).where(Tenant.id == fresh_tenant)).first() is None
        row = db.execute(
            text(
                "SELECT firm_id, detail::text FROM firm_audit_log "
                "WHERE action = 'tenant_deleted' AND entity_id = :t"
            ),
            {"t": str(fresh_tenant)},
        ).one()
    assert row[0] == seed.firm_id
    detail = json.loads(row[1])
    assert (detail["slug"], detail["operator"], detail["objects"]) == (slug, "tester", 2)
    assert detail["rows"] == before
    with owner_engine.connect() as conn:  # the triggers are back on, prod_check-style
        assert catalog.append_only_failures(conn) == []
    assert _counts(owner_engine, seed.tenant_a) == other_before
    # The application cannot do what the script did (a row of its own, then DELETE).
    with pytest.raises(DBAPIError, match="append-only"):
        with tenant_session(rw_engine, seed.tenant_a) as db:
            probe = AuditLog(
                tenant_id=seed.tenant_a, action="probe", entity_type="test", entity_id="x"
            )
            db.add(probe)
            db.flush()
            db.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": probe.id})


def test_a_protected_slug_is_refused_before_anything_runs(
    seed: Seed, fresh_tenant: uuid.UUID, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    slug = _slug(owner_engine, fresh_tenant)
    settings = get_settings()
    monkeypatch.setattr(settings, "protected_tenant_slugs", f"rye-beach, {slug}")
    store = LocalObjectStore(settings.local_object_store_dir)
    with pytest.raises(delete_tenant.Refused, match="PROTECTED_TENANT_SLUGS"):
        delete_tenant.plan(owner_engine, store, settings, slug)
    with pytest.raises(delete_tenant.Refused, match="PROTECTED_TENANT_SLUGS"):
        delete_tenant.run(owner_engine, store, settings, slug=slug, typed=slug, operator="t")
    with untenanted_session(owner_engine) as db:
        assert db.execute(select(Tenant.id).where(Tenant.id == fresh_tenant)).first()
    with pytest.raises(delete_tenant.Refused, match="no tenant with slug"):
        delete_tenant.plan(owner_engine, store, settings, "no-such-tenant")


def test_the_application_role_fails_closed(
    fresh_tenant: uuid.UUID, rw_engine: Engine, owner_engine: Engine
) -> None:
    slug = _slug(owner_engine, fresh_tenant)
    settings = get_settings()
    store = LocalObjectStore(settings.local_object_store_dir)
    before = _counts(owner_engine, fresh_tenant)
    with pytest.raises(delete_tenant.Refused, match="does not own the tenant tables"):
        delete_tenant.run(rw_engine, store, settings, slug=slug, typed=slug, operator="t")
    assert _counts(owner_engine, fresh_tenant) == before
    with untenanted_session(owner_engine) as db:
        assert db.execute(select(Tenant.id).where(Tenant.id == fresh_tenant)).first()


def test_without_the_owner_url_the_script_exits_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "database_owner_url", None)
    with pytest.raises(delete_tenant.Refused, match="DATABASE_OWNER_URL"):
        delete_tenant.owner_engine(settings)
