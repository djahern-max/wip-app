"""F05: backfill, change polling, normalize and the nightly drift check as worker
tasks, against the recorded sandbox company served by the stand-in. The worker is
the real one; nothing reaches the network."""

import uuid
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import Engine, select, text

from app.core.db import tenant_session
from app.domain.billing.models import Billing, Payment
from app.domain.billing.totals import month_totals
from app.ingest.models import Connection, RawRecord, SyncRun
from app.ingest.sync_runs import iso_utc
from app.integrations.qbo import schedule
from app.integrations.qbo.entities import ENTITIES
from app.integrations.qbo.schedule import (
    BACKFILL_PAGE_KIND,
    CDC_KIND,
    DRIFT_KIND,
    NORMALIZE_KIND,
    ensure_scheduled,
    start_backfill,
)
from app.worker.models import Task
from app.worker.runner import Worker, load_worker_modules
from tests.qbo_fixtures import fixture, records
from tests.qbo_helpers import FakeIntuit, FixtureCompany, connect_directly, installed

load_worker_modules()
ADMIN_KEY = "rotate_me"  # firm_admin with an entry row in every fresh tenant


@pytest.fixture
def company(fresh_tenant: uuid.UUID, rw_engine: Engine):
    """A connected tenant and a stand-in serving the fixtures. Yields (tenant, connection id,
    fake, company)."""
    fake = FakeIntuit()
    served = FixtureCompany().serve(fake)
    with installed(fake):
        cid = connect_directly(rw_engine, fresh_tenant, fake)
        yield fresh_tenant, cid, fake, served


def _drain(engine: Engine, tenant_id: uuid.UUID, *, limit: int = 400) -> int:
    """Run the worker until nothing of ``tenant_id`` is due. Pinned to the one tenant:
    the real worker sweeps every tenant, and a scheduled change poll seeded by an
    earlier test's tenant (next 15-minute wall-clock slot, ``next_cdc_slot``) comes due
    at some point in the run. Let in, it runs under *this* test's ``FakeIntuit``, whose
    401 → refresh → ``invalid_grant`` path counts a refresh this test never made
    (CI #24, 2026-09-22: ``refresh_calls`` 2 instead of 1)."""
    w = Worker(engine, name="w-qbo", listen=False, poll_seconds=0.01)
    w.tenant_ids = lambda: [tenant_id]  # type: ignore[method-assign]
    ran = 0
    for _ in range(limit):
        n = w.run_once()
        if n == 0:
            return ran
        ran += n
    raise AssertionError("the queue did not drain")


def _tasks(engine: Engine, tenant_id: uuid.UUID, kind: str | None = None) -> list[Task]:
    with tenant_session(engine, tenant_id) as db:
        stmt = select(Task).order_by(Task.created_at)
        if kind:
            stmt = stmt.where(Task.kind == kind)
        rows = list(db.execute(stmt).scalars())
        for r in rows:
            db.expunge(r)
        return rows


def _runs(engine: Engine, tenant_id: uuid.UUID, kind: str) -> list[SyncRun]:
    with tenant_session(engine, tenant_id) as db:
        rows = list(
            db.execute(
                select(SyncRun).where(SyncRun.kind == kind).order_by(SyncRun.started_at)
            ).scalars()
        )
        for r in rows:
            db.expunge(r)
        return rows


def _raw_count(engine: Engine, tenant_id: uuid.UUID) -> int:
    with tenant_session(engine, tenant_id) as db:
        return db.execute(text("SELECT count(*) FROM raw_record WHERE source = 'qbo'")).scalar_one()


# --- backfill --------------------------------------------------------------------------


def test_backfill_is_one_task_per_page_stores_every_record_and_a_second_run_writes_nothing(
    company, rw_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, cid, fake, served = company
    from app.integrations.qbo import fetch

    monkeypatch.setattr(fetch, "PAGE_SIZE", 10)  # small pages: the paging is under test
    with tenant_session(rw_engine, tenant) as db:
        run = start_backfill(db, tenant, db.get(Connection, cid))
        assert run is not None and run.detail["realm_id"] == fake.realm_id
        assert start_backfill(db, tenant, db.get(Connection, cid)) is None  # one at a time
    first_pages = _tasks(rw_engine, tenant, BACKFILL_PAGE_KIND)
    assert len(first_pages) == len(ENTITIES) and {t.payload["after_id"] for t in first_pages} == {
        "0"
    }
    _drain(rw_engine, tenant)
    pages = _tasks(rw_engine, tenant, BACKFILL_PAGE_KIND)
    assert all(t.status == "succeeded" for t in pages), [t.last_error for t in pages]
    # Keyset paging: a full page is followed by one more fetch that comes back short or
    # empty, so an entity with 10·k rows takes k + 1 pages; a singleton takes one.
    expected_pages = sum(
        1 if e in ("Preferences", "CompanyInfo") else len(records(e)) // 10 + 1 for e in ENTITIES
    )
    assert len(pages) == expected_pages
    # Every record of every entity is held once, as version 1.
    with tenant_session(rw_engine, tenant) as db:
        for entity in ENTITIES:
            n = db.execute(
                text("SELECT count(*) FROM raw_record WHERE entity_type = :e AND version = 1"),
                {"e": entity},
            ).scalar_one()
            assert n == len(records(entity)), entity
        assert (
            db.execute(text("SELECT count(*) FROM raw_record WHERE version > 1")).scalar_one() == 0
        )
    (run,) = _runs(rw_engine, tenant, "backfill")
    assert run.outcome == "succeeded" and run.cursor["realm_id"] == fake.realm_id
    assert run.records_stored == sum(len(records(e)) for e in ENTITIES)
    assert all(v["done"] for v in run.detail["entities"].values())
    # The normalizers ran: rows exist and the totals tie to the oracle.
    with tenant_session(rw_engine, tenant) as db:
        assert db.execute(select(Billing)).scalars().first() is not None
        totals = {t.month: t for t in month_totals(db, tenant)}
        for o in fixture("oracle_month_totals"):
            assert f"{totals[o['month']].invoices:.2f}" == o["invoices"]
        assert db.get(Connection, cid).last_success_at is not None
    # The poll and drift chains were seeded when the backfill finished.
    assert _tasks(rw_engine, tenant, CDC_KIND) and _tasks(rw_engine, tenant, DRIFT_KIND)
    # A second backfill fetches everything again and stores nothing new (D-20).
    before = _raw_count(rw_engine, tenant)
    with tenant_session(rw_engine, tenant) as db:
        assert start_backfill(db, tenant, db.get(Connection, cid)) is not None
    _drain(rw_engine, tenant)
    assert _raw_count(rw_engine, tenant) == before
    second = _runs(rw_engine, tenant, "backfill")[1]
    assert second.outcome == "succeeded" and second.records_stored == 0
    assert second.records_fetched == run.records_fetched


def test_backfill_stops_without_retries_when_the_connection_needs_reconnecting(
    company, rw_engine: Engine
) -> None:
    tenant, cid, fake, served = company
    fake.refuse_refresh = True
    with tenant_session(rw_engine, tenant) as db:
        c = db.get(Connection, cid)
        c.token_expires_at = datetime.now(UTC)  # expired: the first call must refresh
        start_backfill(db, tenant, c)
    _drain(rw_engine, tenant)
    pages = _tasks(rw_engine, tenant, BACKFILL_PAGE_KIND)
    failed = [t for t in pages if t.status == "failed"]
    assert failed and {t.last_error for t in failed} == {"needs_reconnect"}
    assert {t.attempts for t in pages} == {1}  # no retries to max_attempts
    assert not [t for t in pages if t.status in ("queued", "running")]
    # The other pages found the run failed and ended without asking Intuit anything.
    assert fake.refresh_calls == 1 and not served.queries
    with tenant_session(rw_engine, tenant) as db:
        assert db.get(Connection, cid).status == "needs_reconnect"
    (run,) = _runs(rw_engine, tenant, "backfill")
    assert run.outcome == "failed" and run.error == "needs_reconnect"
    assert run.detail["intuit_tid"] == "tid-refused"  # F05.1: the refusal's trace id


# --- change polling --------------------------------------------------------------------


def _backfilled(company, rw_engine: Engine):
    tenant, cid, fake, served = company
    with tenant_session(rw_engine, tenant) as db:
        start_backfill(db, tenant, db.get(Connection, cid))
    _drain(rw_engine, tenant)
    return tenant, cid, fake, served


def test_a_poll_stores_the_changed_and_deleted_invoice_and_the_deleted_one_leaves_the_totals(
    company, rw_engine: Engine
) -> None:
    tenant, cid, fake, served = _backfilled(company, rw_engine)
    # Pretend the deleted invoice was held: a copy under the stub's id, as a version 1.
    stub = next(
        r
        for r in fixture("cdc_changed_and_deleted")["CDCResponse"][0]["QueryResponse"][0]["Invoice"]
        if r.get("status") == "Deleted"
    )
    held = dict(records("Invoice")[0]) | {"Id": stub["Id"], "DocNumber": "held"}
    served.rows["Invoice"] = [*records("Invoice"), held]
    served.cdc_body = {"CDCResponse": [{"QueryResponse": [{"Invoice": [held]}]}], "time": "x"}
    with tenant_session(rw_engine, tenant) as db:
        schedule.enqueue_cdc_poll(db, tenant, cid, slot=None)
    _drain(rw_engine, tenant)
    with tenant_session(rw_engine, tenant) as db:
        month = held["TxnDate"][:7]
        before = next(t for t in month_totals(db, tenant) if t.month == month).invoices
        row = db.execute(select(Billing).where(Billing.external_id == stub["Id"])).scalar_one()
        assert row.deleted_at is None
    served.cdc_body = fixture("cdc_changed_and_deleted")
    with tenant_session(rw_engine, tenant) as db:
        schedule.enqueue_cdc_poll(db, tenant, cid, slot=None)
    _drain(rw_engine, tenant)
    runs = _runs(rw_engine, tenant, "cdc")
    assert [r.outcome for r in runs] == ["succeeded", "succeeded"]
    assert runs[-1].detail["deleted"] == {"Invoice": 1} and runs[-1].detail["changed"] == {
        "Invoice": 1
    }
    assert runs[-1].cursor["realm_id"] == fake.realm_id
    with tenant_session(rw_engine, tenant) as db:
        versions = db.execute(
            select(RawRecord.external_id, RawRecord.version, RawRecord.is_deleted)
            .where(RawRecord.entity_type == "Invoice", RawRecord.version > 1)
            .order_by(RawRecord.external_id)
        ).all()
        assert sorted(versions) == sorted([(stub["Id"], 2, True), ("39", 2, False)])
        gone = db.execute(select(Billing).where(Billing.external_id == stub["Id"])).scalar_one()
        assert gone.deleted_at is not None
        # history still readable
        history = db.execute(
            select(RawRecord.version, RawRecord.payload["DocNumber"].as_string())
            .where(RawRecord.entity_type == "Invoice", RawRecord.external_id == stub["Id"])
            .order_by(RawRecord.version)
        ).all()
        assert history == [(1, "held"), (2, "held")]
        after = next(t for t in month_totals(db, tenant) if t.month == month).invoices
        assert before - after == held["TotalAmt"] - 10  # gone, and the changed one grew by 10.00
        changed = db.execute(select(Billing).where(Billing.external_id == "39")).scalar_one()
        assert (
            changed.total == records("Invoice")[0]["TotalAmt"] + 10
            if records("Invoice")[0]["Id"] == "39"
            else changed.total > 0
        )
    # The poll asked from the cursor, with an offset instant Intuit accepts.
    assert served.cdc_calls[-1]["entities"].split(",") == list(ENTITIES)
    assert served.cdc_calls[-1]["changedSince"].endswith("+00:00")


def test_a_poll_that_intuit_cannot_answer_fails_the_run_with_the_trace_id(
    company, rw_engine: Engine
) -> None:
    """F05.1: the failing response's ``intuit_tid`` is kept on ``sync_run.detail`` so a
    support case can name the call."""
    tenant, cid, fake, served = _backfilled(company, rw_engine)
    fake.on_api(
        "cdc", lambda request: httpx.Response(503, json={}, headers={"intuit_tid": "tid-cdc-503"})
    )
    with tenant_session(rw_engine, tenant) as db:
        schedule.enqueue_cdc_poll(db, tenant, cid, slot=None)
    _drain(rw_engine, tenant)
    (run,) = _runs(rw_engine, tenant, "cdc")
    assert run.outcome == "failed" and run.error == "unavailable"
    assert run.detail["intuit_tid"] == "tid-cdc-503"


def test_a_scheduled_poll_enqueues_its_successor_first_and_a_sync_now_is_deduped(
    company, rw_engine: Engine
) -> None:
    tenant, cid, fake, served = _backfilled(company, rw_engine)
    polls = _tasks(rw_engine, tenant, CDC_KIND)
    (seeded,) = [t for t in polls if t.status == "queued"]
    assert seeded.dedupe_key.startswith(f"cdc:{cid}:") and seeded.run_after > datetime.now(UTC)
    with tenant_session(rw_engine, tenant) as db:  # make it due
        db.execute(text("UPDATE task SET run_after = now() WHERE id = :id"), {"id": seeded.id})
    _drain(rw_engine, tenant)
    polls = _tasks(rw_engine, tenant, CDC_KIND)
    done = [t for t in polls if t.id == seeded.id]
    assert done[0].status == "succeeded"
    queued = [t for t in polls if t.status == "queued"]
    assert len(queued) == 1 and queued[0].dedupe_key != seeded.dedupe_key
    # The successor is the slot after this poll's own slot (the poll ran early here).
    assert queued[0].run_after == seeded.run_after + timedelta(minutes=15)
    assert queued[0].run_after.minute % 15 == 0
    # Sync now: one task, however often it is pressed.
    with tenant_session(rw_engine, tenant) as db:
        first = schedule.enqueue_cdc_poll(db, tenant, cid, slot=None)
        again = schedule.enqueue_cdc_poll(db, tenant, cid, slot=None)
    assert first is not None and again is None
    # ensure_scheduled adds nothing while a poll and a check are open.
    with tenant_session(rw_engine, tenant) as db:
        ensure_scheduled(db, tenant)
    assert len([t for t in _tasks(rw_engine, tenant, CDC_KIND) if t.status == "queued"]) == 2
    assert len([t for t in _tasks(rw_engine, tenant, DRIFT_KIND) if t.status == "queued"]) == 1


def test_the_chain_stops_when_disconnected_and_a_too_old_cursor_asks_for_a_backfill(
    company, rw_engine: Engine
) -> None:
    tenant, cid, fake, served = _backfilled(company, rw_engine)
    with tenant_session(rw_engine, tenant) as db:
        db.get(Connection, cid).status = "disconnected"
        seeded = [t for t in _tasks(rw_engine, tenant, CDC_KIND) if t.status == "queued"][0]
        db.execute(text("UPDATE task SET run_after = now() WHERE id = :id"), {"id": seeded.id})
    _drain(rw_engine, tenant)
    polls = _tasks(rw_engine, tenant, CDC_KIND)
    assert [t.status for t in polls if t.id == seeded.id] == ["succeeded"]
    assert not [t for t in polls if t.status == "queued"]  # no successor
    assert len(_runs(rw_engine, tenant, "cdc")) == 0  # nothing was asked of Intuit
    # Reconnect with a cursor older than 29 days: the poll refuses and stops.
    with tenant_session(rw_engine, tenant) as db:
        db.get(Connection, cid).status = "connected"
        old = iso_utc(datetime.now(UTC) - timedelta(days=30))
        db.execute(
            text(
                "UPDATE sync_run SET cursor = jsonb_set(cursor, '{changed_since}', "
                "to_jsonb(CAST(:old AS text))) WHERE kind = 'backfill'"
            ),
            {"old": old},
        )
        schedule.enqueue_cdc_poll(db, tenant, cid, slot=None)
    calls = len(served.cdc_calls)
    _drain(rw_engine, tenant)
    (run,) = _runs(rw_engine, tenant, "cdc")
    assert run.outcome == "failed" and run.error == "cursor_too_old"
    assert len(served.cdc_calls) == calls
    with tenant_session(rw_engine, tenant) as db:
        assert schedule.after_connect(db, tenant, db.get(Connection, cid)) == "backfill_needed"
        assert not [
            t for t in _tasks(rw_engine, tenant, BACKFILL_PAGE_KIND) if t.status == "queued"
        ]


def test_a_changed_payment_arriving_by_poll_replaces_its_applications(
    company, rw_engine: Engine
) -> None:
    tenant, cid, fake, served = _backfilled(company, rw_engine)
    reapplied = {k: v for k, v in fixture("payment_reapplied").items() if k != "_note"}
    served.cdc_body = {"CDCResponse": [{"QueryResponse": [{"Payment": [reapplied]}]}], "time": "x"}
    with tenant_session(rw_engine, tenant) as db:
        schedule.enqueue_cdc_poll(db, tenant, cid, slot=None)
    _drain(rw_engine, tenant)
    with tenant_session(rw_engine, tenant) as db:
        p = db.execute(select(Payment).where(Payment.external_id == "74")).scalar_one()
        assert p.unapplied_amount == p.total == 100
        n = db.execute(
            text("SELECT count(*) FROM payment_application WHERE payment_id = :p"), {"p": p.id}
        ).scalar_one()
        assert n == 0


# --- normalize ---------------------------------------------------------------------------


def test_normalize_ignores_a_version_that_is_no_longer_the_latest(
    company, rw_engine: Engine
) -> None:
    tenant, cid, fake, served = _backfilled(company, rw_engine)
    from app.worker.queue import enqueue

    with tenant_session(rw_engine, tenant) as db:
        old = db.execute(
            select(RawRecord).where(
                RawRecord.entity_type == "Invoice", RawRecord.external_id == "39"
            )
        ).scalar_one()
        old_id = old.id
        billing = db.execute(select(Billing).where(Billing.external_id == "39")).scalar_one()
        billing.total = billing.total + 1  # a marker that a re-apply would overwrite
        billing.subtotal = billing.subtotal + 1
        # a newer version exists
        from app.ingest.raw import RawOrigin, store_raw

        run = db.execute(select(SyncRun)).scalars().first()
        newer = store_raw(
            db,
            tenant,
            "qbo",
            "Invoice",
            "39",
            dict(old.payload) | {"SyncToken": "99"},
            RawOrigin(sync_run_id=run.id),
        )
        assert newer.record.version == 2
        enqueue(
            db,
            tenant,
            NORMALIZE_KIND,
            {"connection_id": str(cid), "entity": "Invoice", "raw_record_ids": [str(old_id)]},
        )
    _drain(rw_engine, tenant)
    with tenant_session(rw_engine, tenant) as db:
        billing = db.execute(select(Billing).where(Billing.external_id == "39")).scalar_one()
        assert billing.raw_record_id == old_id  # untouched: the old version is stale


# --- drift check -----------------------------------------------------------------------


def _due_drift(engine: Engine, tenant: uuid.UUID) -> Task:
    (t,) = [t for t in _tasks(engine, tenant, DRIFT_KIND) if t.status == "queued"]
    with tenant_session(engine, tenant) as db:
        db.execute(text("UPDATE task SET run_after = now() WHERE id = :id"), {"id": t.id})
    return t


def test_a_clean_drift_check_ends_succeeded_re_pulls_nothing_and_re_arms(
    company, rw_engine: Engine
) -> None:
    tenant, cid, fake, served = _backfilled(company, rw_engine)
    raw_before = _raw_count(rw_engine, tenant)
    queries_before = len(served.queries)
    task_row = _due_drift(rw_engine, tenant)
    _drain(rw_engine, tenant)
    (run,) = _runs(rw_engine, tenant, "drift")
    assert run.outcome == "succeeded" and run.detail["counts"] == {} and run.detail["months"] == {}
    assert _raw_count(rw_engine, tenant) == raw_before
    new_queries = served.queries[queries_before:]
    assert new_queries and all(q.startswith("SELECT COUNT(*)") for q in new_queries)
    assert any("Active IN (true, false)" in q for q in new_queries)  # inactive rows count too
    queued = [t for t in _tasks(rw_engine, tenant, DRIFT_KIND) if t.status == "queued"]
    assert len(queued) == 1 and queued[0].id != task_row.id
    assert queued[0].run_after.hour == schedule.DRIFT_HOUR_UTC


def test_a_missing_record_and_a_wrong_total_are_named_by_the_drift_check(
    company, rw_engine: Engine
) -> None:
    tenant, cid, fake, served = _backfilled(company, rw_engine)
    with tenant_session(rw_engine, tenant) as db:
        # one Vendor held out of the comparison set: the platform has one fewer
        served.rows["Vendor"] = [
            *served.rows["Vendor"],
            dict(served.rows["Vendor"][0]) | {"Id": "9999"},
        ]
        # one normalized total off by a cent (the raw copy is still right)
        billing = (
            db.execute(select(Billing).where(Billing.kind == "invoice", Billing.voided.is_(False)))
            .scalars()
            .first()
        )
        billing.total = billing.total + 1
        billing.subtotal = billing.subtotal + 1
        month = billing.txn_date.strftime("%Y-%m")
    _due_drift(rw_engine, tenant)
    _drain(rw_engine, tenant)
    (run,) = _runs(rw_engine, tenant, "drift")
    assert run.outcome == "drift"
    assert run.detail["counts"] == {
        "Vendor": {"quickbooks": len(served.rows["Vendor"]), "platform": len(records("Vendor"))}
    }
    assert list(run.detail["months"]) == [month]
    both = run.detail["months"][month]["invoices"]
    assert set(both) == {"normalized", "raw"} and both["normalized"] != both["raw"]
    assert "." in both["raw"] and both["raw"].split(".")[1].__len__() == 2  # strings with cents
    assert _raw_count(rw_engine, tenant) == sum(len(records(e)) for e in ENTITIES)  # no re-pull


# --- a chart loaded after the connection (the normal order for a new tenant) ------------


def test_a_chart_loaded_after_the_backfill_attaches_without_any_account_change(
    company, rw_engine: Engine, login_as, owner_engine: Engine
) -> None:
    """Owner, 2026-09-21: connect, backfill, then load the chart; expect 24 attached
    with no changed Account from QuickBooks, and the attachment visible on the page."""
    from app.domain.billing.sync import chart_match
    from app.domain.config.models import GlAccount
    from tests.config_helpers import upload_chart

    tenant, cid, fake, served = _backfilled(company, rw_engine)
    numbered = {r["AcctNum"]: r for r in records("Account") if r.get("AcctNum")}
    assert len(numbered) == 25
    lines = ["account_no,account_name,ledger_type"]
    for number, r in numbered.items():
        if number == "6150":
            continue
        name = "Operating checking (chart name)" if number == "1010" else r["Name"]
        lines.append(f"{number},{name},{r['AccountType']}")
    lines.append("1400,Inventory (chart only),Other Current Asset")
    client = login_as(ADMIN_KEY, tenant=tenant)
    polls_before = len(served.cdc_calls)
    with installed(fake):
        upload_chart(client, ("\n".join(lines) + "\n").encode(), "sandbox-chart.csv")
        _drain(rw_engine, tenant)  # the import, its follow-on, and nothing from QuickBooks
    assert len(served.cdc_calls) == polls_before
    with tenant_session(rw_engine, tenant) as db:
        match = chart_match(db, tenant)
        assert len(match.attached) == 24 and match.chart_total == 25
        assert match.unmatched == ["6150"] and match.chart_only == ["1400"]
        rows = {a.account_no: a for a in db.execute(select(GlAccount)).scalars()}
        assert rows["1010"].name == "Operating checking (chart name)"
        assert rows["1010"].external_id == numbered["1010"]["Id"]
        assert rows["1400"].external_id is None
        audits = db.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'gl_account_linked'")
        ).scalar_one()
        assert audits == 24
    with installed(fake):
        held = client.get("/api/qbo/status").json()["held"]
    items = {a["code"]: a["detail"] for a in held["attention"]}
    assert items["chart_attached"] == "24 of 25 chart accounts"
    assert items["numbered_not_in_chart"] == "6150" and items["chart_not_in_quickbooks"] == "1400"
    assert items["accounts_without_number"] == "64 of 89 active accounts"


# --- F05.3 (D-48): the copy begins at the history start ------------------------------------

START = date(2026, 7, 1)  # inside the fixtures' range (2026-04-09 to 2026-09-18)


def _on_or_after(entity: str, day: date) -> list[dict]:
    return [r for r in records(entity) if str(r.get("TxnDate", "")) >= day.isoformat()]


@pytest.fixture
def company_from_july(fresh_tenant: uuid.UUID, rw_engine: Engine):
    """A connected tenant whose history start is inside the fixtures' range, backfilled."""
    fake = FakeIntuit()
    served = FixtureCompany().serve(fake)
    with installed(fake):
        cid = connect_directly(rw_engine, fresh_tenant, fake, history_start=START)
        with tenant_session(rw_engine, fresh_tenant) as db:
            run = start_backfill(db, fresh_tenant, db.get(Connection, cid))
            assert run is not None and run.detail["history_start"] == START.isoformat()
        _drain(rw_engine, fresh_tenant)
        yield fresh_tenant, cid, fake, served


def _entity_of(query: str) -> str:
    return query.split(" FROM ")[1].split(" ")[0]


def _poll(engine: Engine, tenant: uuid.UUID, cid: uuid.UUID, served: FixtureCompany, body: dict):
    served.cdc_body = {"CDCResponse": [{"QueryResponse": [body]}], "time": "x"}
    with tenant_session(engine, tenant) as db:
        schedule.enqueue_cdc_poll(db, tenant, cid, slot=None)
    _drain(engine, tenant)


def _held(client) -> dict:
    body = client.get("/api/qbo/status").json()["held"]
    return {e["entity"]: e for e in body["entities"]} | {"_attention": body["attention"]}


def test_backfill_holds_reference_records_whole_and_transactional_records_from_the_start(
    company_from_july, rw_engine: Engine
) -> None:
    from app.integrations.qbo.entities import NAME_LISTS, REFERENCE, SINGLETONS, TRANSACTIONAL
    from tests.qbo_helpers import HOLD_EVERYTHING

    tenant, cid, fake, served = company_from_july
    (run,) = _runs(rw_engine, tenant, "backfill")
    assert run.outcome == "succeeded"
    with tenant_session(rw_engine, tenant) as db:
        for entity in ENTITIES:
            expected = records(entity) if entity in REFERENCE else _on_or_after(entity, START)
            n = db.execute(
                text("SELECT count(*) FROM raw_record WHERE entity_type = :e"), {"e": entity}
            ).scalar_one()
            assert n == len(expected), entity
        assert run.records_stored == sum(
            len(records(e)) if e in REFERENCE else len(_on_or_after(e, START)) for e in ENTITIES
        )
        totals = {t.month: t for t in month_totals(db, tenant)}
    oracle = [o for o in fixture("oracle_month_totals") if o["month"] >= START.isoformat()[:7]]
    assert oracle and sorted(totals) == [o["month"] for o in oracle]  # no earlier month exists
    for o in oracle:
        got = totals[o["month"]]
        for column in ("invoices", "credit_memos", "sales_receipts", "payments"):
            assert f"{getattr(got, column):.2f}" == o[column], (o["month"], column)
    # Every page query for a transactional entity carries both clauses; a name list
    # carries its Active clause and no date; a singleton has no WHERE.
    pages = [q for q in served.queries if q.startswith("SELECT *")]
    assert pages
    for q in pages:
        entity = _entity_of(q)
        if entity in TRANSACTIONAL:
            assert f"WHERE TxnDate >= '{START.isoformat()}' AND Id > '" in q, q
            assert "ORDERBY Id MAXRESULTS" in q
        elif entity in NAME_LISTS:
            assert "Active IN (true, false) AND Id > '" in q and "TxnDate" not in q, q
        else:
            assert entity in SINGLETONS and " WHERE " not in q, q
    assert HOLD_EVERYTHING < START  # the ordinary fixture holds everything; this one does not


def test_a_pre_start_record_arriving_by_poll_is_held_raw_skipped_and_counted_apart(
    company_from_july, rw_engine: Engine, login_as
) -> None:
    """Raw before normalized: the payload is stored (D-20), no canonical row is built,
    and the page counts it under "Before history start", not "Could not be read"; a
    later version dated on or after the start is normalized as usual."""
    tenant, cid, fake, served = company_from_july
    admin = login_as(ADMIN_KEY, tenant=tenant)
    old = next(r for r in records("Invoice") if str(r["TxnDate"]) < START.isoformat())
    with installed(fake):
        before = _held(admin)
        assert before["Invoice"]["before_history_start"] == 0
        assert before["Bill"]["before_history_start"] == 0
        assert before["Account"]["before_history_start"] is None  # a reference entity
        _poll(rw_engine, tenant, cid, served, {"Invoice": [old]})
        (run,) = _runs(rw_engine, tenant, "cdc")
        assert run.outcome == "succeeded" and run.records_stored == 1
        with tenant_session(rw_engine, tenant) as db:
            raw = db.execute(
                select(RawRecord).where(
                    RawRecord.entity_type == "Invoice", RawRecord.external_id == old["Id"]
                )
            ).scalar_one()
            assert raw.version == 1 and raw.payload["TxnDate"] == old["TxnDate"]
            assert (
                db.execute(select(Billing).where(Billing.external_id == old["Id"])).first() is None
            )
            month = old["TxnDate"][:7]
            assert month not in {t.month for t in month_totals(db, tenant)}
        after = _held(admin)
        assert after["Invoice"]["before_history_start"] == 1
        assert after["Invoice"]["current"] == before["Invoice"]["current"]
        assert after["Invoice"]["skipped"] == before["Invoice"]["skipped"] == 0
        assert "skipped" not in {a["code"] for a in after["_attention"]}
        # A later version dated on the start is normalized; the count falls to 0.
        moved = dict(old) | {"TxnDate": START.isoformat(), "SyncToken": "9"}
        _poll(rw_engine, tenant, cid, served, {"Invoice": [moved]})
        with tenant_session(rw_engine, tenant) as db:
            row = db.execute(select(Billing).where(Billing.external_id == old["Id"])).scalar_one()
            assert row.txn_date == START and row.deleted_at is None
            versions = db.execute(
                select(RawRecord.version)
                .where(RawRecord.entity_type == "Invoice", RawRecord.external_id == old["Id"])
                .order_by(RawRecord.version)
            ).scalars()
            assert list(versions) == [1, 2]
        latest = _held(admin)
        assert latest["Invoice"]["before_history_start"] == 0
        assert latest["Invoice"]["current"] == before["Invoice"]["current"] + 1


def test_a_poll_that_re_dates_a_held_invoice_to_before_the_start_removes_it_from_its_month(
    company_from_july, rw_engine: Engine, login_as
) -> None:
    tenant, cid, fake, served = company_from_july
    admin = login_as(ADMIN_KEY, tenant=tenant)
    held = next(r for r in records("Invoice") if str(r["TxnDate"]) >= START.isoformat())
    month = held["TxnDate"][:7]
    with installed(fake):
        with tenant_session(rw_engine, tenant) as db:
            before = next(t for t in month_totals(db, tenant) if t.month == month).invoices
            assert (
                db.execute(select(Billing).where(Billing.external_id == held["Id"]))
                .scalar_one()
                .deleted_at
                is None
            )
        moved = dict(held) | {"TxnDate": (START - timedelta(days=1)).isoformat(), "SyncToken": "9"}
        _poll(rw_engine, tenant, cid, served, {"Invoice": [moved]})
        with tenant_session(rw_engine, tenant) as db:
            row = db.execute(select(Billing).where(Billing.external_id == held["Id"])).scalar_one()
            assert row.deleted_at is not None
            after = next((t.invoices for t in month_totals(db, tenant) if t.month == month), 0)
            assert before - after == held["TotalAmt"]
            versions = db.execute(
                select(RawRecord.version, RawRecord.is_deleted)
                .where(RawRecord.entity_type == "Invoice", RawRecord.external_id == held["Id"])
                .order_by(RawRecord.version)
            ).all()
            assert versions == [(1, False), (2, False)]  # kept; not a source delete
        status = _held(admin)
        assert status["Invoice"]["before_history_start"] == 1 and status["Invoice"]["skipped"] == 0


def test_the_nightly_count_carries_the_date_clause_for_transactional_entities_only_and_stays_clean(
    company_from_july, rw_engine: Engine
) -> None:
    from app.integrations.qbo.entities import NAME_LISTS, TRANSACTIONAL

    tenant, cid, fake, served = company_from_july
    old = next(r for r in records("Invoice") if str(r["TxnDate"]) < START.isoformat())
    with installed(fake):
        _poll(rw_engine, tenant, cid, served, {"Invoice": [old]})  # held raw, before the start
        queries_before = len(served.queries)
        _due_drift(rw_engine, tenant)
        _drain(rw_engine, tenant)
    (run,) = _runs(rw_engine, tenant, "drift")
    assert run.outcome == "succeeded", run.detail
    assert run.detail["counts"] == {} and run.detail["months"] == {}
    counts = [q for q in served.queries[queries_before:] if q.startswith("SELECT COUNT(*)")]
    counted = {e for e in ENTITIES if e not in ("Preferences", "CompanyInfo")}
    assert {_entity_of(q) for q in counts} == counted
    for q in counts:
        entity = _entity_of(q)
        if entity in TRANSACTIONAL:
            assert q == f"SELECT COUNT(*) FROM {entity} WHERE TxnDate >= '{START.isoformat()}'", q
        elif entity in NAME_LISTS:
            assert q == f"SELECT COUNT(*) FROM {entity} WHERE Active IN (true, false)", q
        else:
            assert "TxnDate" not in q, q


def test_a_scheduled_poll_and_check_on_a_company_without_a_history_start_pull_nothing_and_say_so(
    fresh_tenant: uuid.UUID, rw_engine: Engine
) -> None:
    """A company connected before F05.3: the start-up hook still seeds the chains, a
    seeded poll or check ends its run ``failed`` with the reason (never silently), asks
    Intuit nothing and enqueues no successor."""
    fake = FakeIntuit()
    served = FixtureCompany().serve(fake)
    with installed(fake):
        connect_directly(rw_engine, fresh_tenant, fake, history_start=None)
        with tenant_session(rw_engine, fresh_tenant) as db:
            ensure_scheduled(db, fresh_tenant)  # the start-up hook, as the worker runs it
        with tenant_session(rw_engine, fresh_tenant) as db:
            assert len(_tasks(rw_engine, fresh_tenant)) == 2  # one poll, one check
            db.execute(text("UPDATE task SET run_after = now()"))
        _drain(rw_engine, fresh_tenant)
    assert not served.cdc_calls and not served.queries
    runs = {r.kind: r for r in _runs(rw_engine, fresh_tenant, "cdc")} | {
        r.kind: r for r in _runs(rw_engine, fresh_tenant, "drift")
    }
    assert {k: (r.outcome, r.error) for k, r in runs.items()} == {
        "cdc": ("failed", "history_start_not_set"),
        "drift": ("failed", "history_start_not_set"),
    }
    assert not [t for t in _tasks(rw_engine, fresh_tenant) if t.status in ("queued", "running")]
    assert not _tasks(rw_engine, fresh_tenant, BACKFILL_PAGE_KIND)
