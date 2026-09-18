"""F04 chart of accounts: the Rye Beach fixture against the oracle, revisions with
history, the owner's workbook layout, and the owner's amendments A, B and C."""

import io
import json
import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import Engine, select, text

from app.core.db import tenant_session
from app.domain.config.audit import Actor
from app.domain.config.chart import normalize_chart, suggest
from app.domain.config.models import AccountMap, AccountSuggestRule, GlAccount
from app.domain.config.rules import RuleError
from app.domain.config.service import confirm_account_map, load_suggest_rules
from app.ingest.raw import raw_history
from app.worker.models import Task
from app.worker.queue import PermanentTaskError
from tests.config_helpers import (
    CHART_CSV,
    account_state,
    batch_json,
    chart_batches,
    expected_rows,
    load_rye_beach_rules,
    rules_spec,
    run_until_quiet,
    upload_chart,
)
from tests.conftest import Seed


@pytest.fixture
def rye_beach(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant
) -> dict:
    """A fresh tenant with Rye Beach's rules loaded and the chart imported and suggested."""
    load_rye_beach_rules(rw_engine, fresh_tenant)
    client = login_as("rotate_me", tenant=fresh_tenant)
    batch = upload_chart(client, CHART_CSV.read_bytes())["batch"]
    run_until_quiet(rw_engine)
    return {"client": client, "batch_id": batch["id"], "tenant": fresh_tenant}


# --- the oracle ------------------------------------------------------------------------------


def test_rye_beach_chart_matches_the_oracle_row_for_row(
    rye_beach: dict, seed: Seed, rw_engine: Engine
) -> None:
    state = account_state(rw_engine, rye_beach["tenant"])
    expected = expected_rows()
    assert len(state) == 159 and set(state) == set(expected)
    mismatches = []
    for no, want in expected.items():
        got = state[no]
        if (got["division"], got["cost_category"], got["in_job_cost"]) != (
            want["division"],
            want["cost_category"],
            want["in_job_cost"],
        ):
            mismatches.append((no, got, want))
    assert mismatches == []
    assert all(v["status"] == "suggested" for v in state.values())
    assert sum(v["in_job_cost"] == "true" for v in state.values()) == 29
    assert all(v["active"] for v in state.values())
    # Unmapped = active accounts without a confirmed mapping: all of them, until a person confirms.
    r = rye_beach["client"].get("/api/config/accounts")
    body = r.json()
    assert (body["total_active"], body["unmapped_count"], body["confirmed_count"]) == (159, 159, 0)
    assert body["suggested_count"] == 159
    unmapped = rye_beach["client"].get("/api/config/accounts?filter=unmapped").json()
    assert len(unmapped["accounts"]) == 159
    # The batch reached Loaded and its follow-on finished (amendment C).
    b = batch_json(rye_beach["client"], rye_beach["batch_id"])
    assert (b["status"], b["rows_loaded"], b["rows_rejected"]) == ("loaded", 159, 0)
    assert (b["followup_status"], b["followup_label"]) == ("succeeded", "Updated")
    assert b["message"] == "Loaded. Accounts updated."


def test_same_chart_twice_is_one_batch_and_changes_nothing(
    rye_beach: dict, seed: Seed, rw_engine: Engine
) -> None:
    before = account_state(rw_engine, rye_beach["tenant"])
    with tenant_session(rw_engine, rye_beach["tenant"]) as c:
        audit_before = c.execute(text("SELECT count(*) FROM audit_log")).scalar_one()
    r = upload_chart(rye_beach["client"], CHART_CSV.read_bytes(), "again.csv")
    assert r["duplicate"] is True and r["batch"]["id"] == rye_beach["batch_id"]
    run_until_quiet(rw_engine)
    assert len(chart_batches(rw_engine, rye_beach["tenant"])) == 1
    assert account_state(rw_engine, rye_beach["tenant"]) == before
    with tenant_session(rw_engine, rye_beach["tenant"]) as s:
        audit_after = s.execute(text("SELECT count(*) FROM audit_log")).scalar_one()
    assert audit_after == audit_before + 1  # only import_duplicate (RLS: this tenant's rows)


def test_revised_chart_renames_adds_deactivates_and_keeps_history(
    rye_beach: dict, seed: Seed, rw_engine: Engine
) -> None:
    # Confirm two untouched accounts and the one about to be renamed.
    with tenant_session(rw_engine, rye_beach["tenant"]) as s:
        ids = {
            a.account_no: a.id
            for a in s.execute(
                select(GlAccount).where(GlAccount.account_no.in_(["5130", "5230", "5140"]))
            ).scalars()
        }
        for no in ("5130", "5230", "5140"):
            confirm_account_map(
                s, rye_beach["tenant"], ids[no], Actor(user_id=seed.users["rotate_me"].id)
            )
    lines = CHART_CSV.read_text().splitlines()
    revised = []
    for line in lines:
        if line.startswith("5140,"):
            revised.append("5140,Subs - LS,Cost of Goods Sold")  # renamed
        elif line.startswith("5160,"):
            continue  # removed
        else:
            revised.append(line)
    revised.append("5190,Other - LS,Cost of Goods Sold")  # added (slot 90 = Other)
    upload_chart(rye_beach["client"], ("\n".join(revised) + "\n").encode(), "revised.csv")
    run_until_quiet(rw_engine)
    state = account_state(rw_engine, rye_beach["tenant"])
    assert state["5140"]["name"] == "Subs - LS"
    assert (state["5140"]["status"], state["5140"]["cost_category"]) == (
        "confirmed",
        "Subcontractors",
    )
    assert state["5160"]["active"] is False
    assert (state["5190"]["active"], state["5190"]["cost_category"], state["5190"]["status"]) == (
        True,
        "Other",
        "suggested",
    )
    assert state["5130"]["status"] == "confirmed" and state["5230"]["status"] == "confirmed"
    assert len(chart_batches(rw_engine, rye_beach["tenant"])) == 2
    with tenant_session(rw_engine, rye_beach["tenant"]) as s:
        versions = raw_history(s, rye_beach["tenant"], "chart", "account", "5140")
        assert [v.version for v in versions] == [1, 2]
        assert versions[0].payload["account_name"] == "Subcontractors - LS"
        assert versions[1].payload["account_name"] == "Subs - LS"
        assert len(raw_history(s, rye_beach["tenant"], "chart", "account", "5130")) == 1
        gone = s.execute(select(GlAccount).where(GlAccount.account_no == "5160")).scalar_one()
        assert gone.active is False  # deactivated, never deleted


def test_owner_workbook_layout_rejects_headings_and_legend_individually(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant
) -> None:
    load_rye_beach_rules(rw_engine, fresh_tenant)
    wb = Workbook()
    ws = wb.active
    ws.append(["Rye Beach Landscaping — Chart of Accounts", None, None])
    ws.append(["Account", "Name", "Type"])
    ws.append(["ASSETS (1000–1999)", None, None])
    ws.append([1010, "Bank account 01", "Bank"])
    ws.append([None, None, None])
    ws.append(["COST OF GOODS SOLD (5000–5999)", None, None])
    ws.append([5110, "Gross Payroll - LS", "Cost of Goods Sold"])
    ws.append([5410.0, "Gross Payroll - SNOW", "Cost of Goods Sold"])  # numeric cell
    ws.append(["Legend", None, None])
    ws.append(["5nXX: n = division, XX = cost slot", None, None])
    ws.append(["", "Notes", None])
    buf = io.BytesIO()
    wb.save(buf)
    client = login_as("recover_me", tenant=fresh_tenant)
    batch = upload_chart(client, buf.getvalue(), "chart.xlsx")["batch"]
    run_until_quiet(rw_engine)
    b = batch_json(client, batch["id"])
    # title, two section headings, "Legend", the legend line, the blank-number row: 6
    assert (b["status"], b["rows_loaded"], b["rows_rejected"]) == ("loaded_with_issues", 3, 6)
    state = account_state(rw_engine, fresh_tenant)
    assert set(state) == {"1010", "5110", "5410"}
    assert state["5410"] == {
        "name": "Gross Payroll - SNOW",
        "ledger_type": "Cost of Goods Sold",
        "active": True,
        "division": "SNOW",
        "cost_category": "Labor",
        "in_job_cost": "true",
        "status": "suggested",
        "rule": "division cost slot",
    }
    assert b["message"].startswith("6 rows could not be read and were skipped")
    assert b["message"].endswith("Accounts updated.")


# --- suggestions ---------------------------------------------------------------------------------


def test_suggestions_never_overwrite_confirmed_and_are_idempotent(
    rye_beach: dict, seed: Seed, rw_engine: Engine
) -> None:
    with tenant_session(rw_engine, rye_beach["tenant"]) as s:
        acct = s.execute(select(GlAccount).where(GlAccount.account_no == "5355")).scalar_one()
        m = s.execute(select(AccountMap).where(AccountMap.gl_account_id == acct.id)).scalar_one()
        m.in_job_cost = False  # the owner's call: equipment maintenance out of the WIP basis
        confirm_account_map(
            s, rye_beach["tenant"], acct.id, Actor(user_id=seed.users["rotate_me"].id)
        )
    with tenant_session(rw_engine, rye_beach["tenant"]) as s:
        counts = suggest(s, rye_beach["tenant"])
        assert (counts.suggested, counts.removed) == (0, 0)
        assert counts.unchanged == 158
    state = account_state(rw_engine, rye_beach["tenant"])
    assert (state["5355"]["status"], state["5355"]["in_job_cost"]) == ("confirmed", "false")
    with tenant_session(rw_engine, rye_beach["tenant"]) as s:
        n_maps = s.execute(text("SELECT count(*) FROM account_map")).scalar_one()
        n_audit = s.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'account_map_suggested'")
        ).scalar_one()
    with tenant_session(rw_engine, rye_beach["tenant"]) as s:
        suggest(s, rye_beach["tenant"])
        assert s.execute(text("SELECT count(*) FROM account_map")).scalar_one() == n_maps
        assert (
            s.execute(
                text("SELECT count(*) FROM audit_log WHERE action = 'account_map_suggested'")
            ).scalar_one()
            == n_audit
        )


def test_amendment_a_unknown_slot_inside_5xxx_gets_no_suggestion(
    rye_beach: dict, seed: Seed, rw_engine: Engine
) -> None:
    content = CHART_CSV.read_text() + "5195,Misc - LS,Cost of Goods Sold\n"
    upload_chart(rye_beach["client"], content.encode(), "with-5195.csv")
    run_until_quiet(rw_engine)
    state = account_state(rw_engine, rye_beach["tenant"])
    assert state["5195"]["status"] is None  # no account_map row at all
    body = rye_beach["client"].get("/api/config/accounts?filter=unmapped").json()
    listed = {a["account_no"]: a for a in body["accounts"]}
    assert listed["5195"]["map"] is None and listed["5195"]["map_status_label"] == "Unmapped"
    assert body["unmapped_count"] == 160 and body["total_active"] == 160


def test_tenant_without_rules_gets_zero_suggestions(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant
) -> None:
    client = login_as("recover_me", tenant=fresh_tenant)
    upload_chart(client, CHART_CSV.read_bytes())
    run_until_quiet(rw_engine)
    body = client.get("/api/config/accounts").json()
    assert (body["total_active"], body["unmapped_count"], body["suggested_count"]) == (159, 159, 0)
    assert all(a["map"] is None for a in body["accounts"])


def test_amendment_b_five_digit_tenant_with_its_own_rules(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant
) -> None:
    """Account numbers are text of any length; positions and slots are not four-digit."""
    spec = {
        "divisions": [
            {"code": "N", "name": "North", "code_digit": "7"},
            {"code": "S", "name": "South", "code_digit": "8"},
        ],
        "rules": [
            {
                "order": 1,
                "name": "cost by 5-digit slot",
                "pattern": "^6[78][0-9]{3}$",
                "division_from_digit": 2,
                "cost_category_from_slot": True,
                "in_job_cost": True,
            },
            {
                "order": 2,
                "name": "header",
                "pattern": "^6[78]000$",
                "division_from_digit": 2,
                "cost_category": None,
                "in_job_cost": False,
            },
            {
                "order": 3,
                "name": "rest",
                "pattern": "^(?!6[78][0-9]{3}$).*$",
                "division": None,
                "cost_category": None,
                "in_job_cost": False,
            },
        ],
    }
    with tenant_session(rw_engine, fresh_tenant) as s:
        load_suggest_rules(s, fresh_tenant, spec, Actor())
    chart = "\n".join(
        [
            "account_no,account_name,ledger_type",
            "67010,North Labor,COGS",
            "68035,South Supplies,COGS",
            "67000,North header,COGS",
            "67099,North nothing,COGS",
            "10100,Cash,Bank",
            "",
        ]
    )
    # Straight through the domain layer, as the worker would.
    from app.ingest.models import ImportBatch
    from app.ingest.raw import RawOrigin, store_raw

    with tenant_session(rw_engine, fresh_tenant) as s:
        batch = ImportBatch(
            tenant_id=fresh_tenant,
            source_kind="chart_of_accounts",
            sha256=uuid.uuid4().hex * 2,
            byte_size=len(chart),
            original_filename="c.csv",
            object_key=f"tenant/{fresh_tenant}/imports/c.csv",
            status="loaded",
        )
        s.add(batch)
        s.flush()
        for line in chart.splitlines()[1:]:
            no, name, typ = line.split(",")
            store_raw(
                s,
                fresh_tenant,
                "chart",
                "account",
                no,
                {"account_no": no, "account_name": name, "ledger_type": typ},
                RawOrigin(import_batch_id=batch.id),
            )
        batch_id = batch.id
    with tenant_session(rw_engine, fresh_tenant) as s:
        normalize_chart(
            s, fresh_tenant, batch_id, present={"67010", "68035", "67000", "67099", "10100"}
        )
    with tenant_session(rw_engine, fresh_tenant) as s:
        suggest(s, fresh_tenant)
    state = account_state(rw_engine, fresh_tenant)
    assert (
        state["67010"]["division"],
        state["67010"]["cost_category"],
        state["67010"]["in_job_cost"],
    ) == ("N", "Labor", "true")
    assert (state["68035"]["division"], state["68035"]["cost_category"]) == ("S", "Supplies")
    assert (
        state["67000"]["division"],
        state["67000"]["cost_category"],
        state["67000"]["in_job_cost"],
    ) == ("N", "", "false")
    assert state["67099"]["status"] is None  # slot 99 does not exist → unmapped
    assert (state["10100"]["division"], state["10100"]["in_job_cost"]) == ("", "false")


def test_rules_are_validated_when_loaded_naming_the_rule(
    seed: Seed, rw_engine: Engine, fresh_tenant
) -> None:
    bad = rules_spec()
    bad["rules"][0]["pattern"] = "^4100($"
    with pytest.raises(RuleError, match="rule 'income LS': invalid pattern"):
        with tenant_session(rw_engine, fresh_tenant) as s:
            load_suggest_rules(s, fresh_tenant, bad, Actor())
    long = rules_spec()
    long["rules"][1]["pattern"] = "^" + "a" * 100 + "$"
    with pytest.raises(RuleError, match="rule 'income EX': pattern must be 1 to 100"):
        with tenant_session(rw_engine, fresh_tenant) as s:
            load_suggest_rules(s, fresh_tenant, long, Actor())
    with tenant_session(rw_engine, fresh_tenant) as s:
        assert s.execute(select(AccountSuggestRule)).first() is None  # nothing half-loaded


# --- amendment C: follow-on visibility ---------------------------------------------------


def test_followup_states_are_shown_in_words(
    seed: Seed,
    rw_engine: Engine,
    login_as: Callable[..., TestClient],
    monkeypatch: pytest.MonkeyPatch,
    fresh_tenant,
) -> None:
    load_rye_beach_rules(rw_engine, fresh_tenant)
    client = login_as("rotate_me", tenant=fresh_tenant)
    from app.worker.runner import Worker

    w = Worker(rw_engine, name="w-follow", listen=False, poll_seconds=0.01)
    # 1. queued: the import task ran; the follow-on is waiting.
    batch = upload_chart(client, CHART_CSV.read_bytes())["batch"]
    assert w.run_once() >= 1
    b = batch_json(client, batch["id"])
    assert (b["status"], b["followup_status"], b["followup_label"]) == (
        "loaded",
        "queued",
        "Updating",
    )
    assert b["message"] == "Loaded. Updating accounts…"
    # 2. succeeded
    run_until_quiet(rw_engine)
    b = batch_json(client, batch["id"])
    assert (b["followup_status"], b["message"]) == ("succeeded", "Loaded. Accounts updated.")
    assert b["error_detail"] is None
    # 3. failed: the follow-on fails permanently; the person sees a sentence, the
    # machine detail stays in error_detail and the task's last_error.
    content = CHART_CSV.read_text() + "5195,Misc - LS,Cost of Goods Sold\n"
    batch2 = upload_chart(client, content.encode(), "second.csv")["batch"]
    assert w.run_once() >= 1

    def boom(*_a, **_k):
        raise PermanentTaskError("normalize failed", ValueError("secret detail"))

    monkeypatch.setattr("app.domain.config.chart.normalize_chart", boom)
    run_until_quiet(rw_engine)
    b = batch_json(client, batch2["id"])
    assert (b["followup_status"], b["followup_label"]) == ("failed", "Not updated")
    assert b["message"] == (
        "Loaded, but the accounts could not be updated. Try uploading again, or contact support."
    )
    assert b["error_detail"] == "follow-up task failed: normalize failed: ValueError"
    assert "ValueError" not in b["message"] and "secret detail" not in b["message"]
    with tenant_session(rw_engine, fresh_tenant) as s:
        t = s.execute(select(Task).where(Task.kind == "config.normalize_chart")).scalars().all()
        assert any(
            x.status == "failed" and x.last_error == "normalize failed: ValueError" for x in t
        )


def test_followup_is_enqueued_once_per_batch(
    rye_beach: dict, seed: Seed, rw_engine: Engine
) -> None:
    """Re-running the import task for a loaded batch does not enqueue a second follow-on
    while one is open, and the batch keeps pointing at its task."""
    from app.core.storage import LocalObjectStore
    from app.ingest.imports import process_batch_now
    from tests._env import OBJECT_STORE_DIR

    with tenant_session(rw_engine, rye_beach["tenant"]) as s:
        before = (
            s.execute(select(Task.id).where(Task.kind == "config.normalize_chart")).scalars().all()
        )
    process_batch_now(
        rw_engine,
        LocalObjectStore(OBJECT_STORE_DIR),
        rye_beach["tenant"],
        uuid.UUID(rye_beach["batch_id"]),
    )
    with tenant_session(rw_engine, rye_beach["tenant"]) as s:
        after = (
            s.execute(select(Task.id).where(Task.kind == "config.normalize_chart")).scalars().all()
        )
    # The first follow-on had finished (terminal), so a re-run may enqueue a fresh one
    # (dedupe covers open tasks only); never two open ones.
    assert len(after) <= len(before) + 1
    with tenant_session(rw_engine, rye_beach["tenant"]) as s:
        open_count = (
            s.execute(
                select(Task).where(
                    Task.kind == "config.normalize_chart", Task.status.in_(("queued", "running"))
                )
            )
            .scalars()
            .all()
        )
        assert len(open_count) <= 1
    run_until_quiet(rw_engine)
    b = batch_json(rye_beach["client"], rye_beach["batch_id"])
    assert b["followup_status"] == "succeeded"


def test_load_suggest_rules_script(
    seed: Seed, rw_engine: Engine, capsys, monkeypatch, fresh_tenant
) -> None:
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "load_suggest_rules",
        Path(__file__).resolve().parents[1] / "scripts" / "load_suggest_rules.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "create_app_engine", lambda *_a, **_k: rw_engine)
    from app.core.db import untenanted_session
    from app.tenancy.models import Tenant

    with untenanted_session(rw_engine) as s:
        slug = s.get(Tenant, fresh_tenant).slug
    from tests.config_helpers import RULES_JSON

    assert mod.main(["--tenant", slug, "--file", str(RULES_JSON)]) == 0
    assert f"loaded 13 rule(s) for {slug}" in capsys.readouterr().out
    assert mod.main(["--tenant", "nope", "--file", str(RULES_JSON)]) == 1
    bad = json.loads(RULES_JSON.read_text())
    bad["rules"][0]["pattern"] = "("
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(bad, fh)
    assert mod.main(["--tenant", slug, "--file", fh.name]) == 1
    assert "rule 'income LS'" in capsys.readouterr().err
