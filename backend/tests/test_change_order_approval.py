"""F07.4 (D-42, D-44): a change order is approved by the project manager and joins the
revised contract from then; a withdrawal takes it out again; both are history, never
edited, one audit row each. Rule C (the owner, 2026-10-06): any later version that
changes the work area's name or price ends the approval for good; nothing revives it.
On the two reviewed workbooks (67 Elm Street, EST6120638), through the API, computed on
read; the only rows written are approvals and withdrawals."""

import uuid
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from app.core.db import tenant_session
from app.domain.config.policy import CHANGE_ORDER_EVIDENCE
from app.domain.jobs.models import ChangeOrderApproval
from app.tenancy.models import Membership, Role
from tests.billing_helpers import WORK_ITEM, apply_payloads, billing_policy, document_payload, line
from tests.config_helpers import run_until_quiet
from tests.conftest import CSRF, Seed
from tests.estimate_helpers import ELM, build_workbook, fixture_rows, upload_template
from tests.job_helpers import ELM_ID, MONEY, TURLEY_ID, Tenant, make_tenant, money_values, policy

D = Decimal
CONFIRM_ALL = "work-areas/kinds/confirm-suggested"
AGREED = {"agreed_on": "2026-09-14"}
ELM_ORIGINAL = "465469.59"
ELM_CO = "53704.13"
ELM_TOTAL = "519173.72"
ELM_EAC = "327929.93"
TURLEY_ORIGINAL = "301553.22"
TURLEY_CO = "65336.58"
TURLEY_TOTAL = "366889.80"
# The change-order work areas of the two reviewed workbooks (re-read with the prefix rule).
ELM_CO_ORDERS = list(range(18, 30))  # 12; #20 at 0.00
TURLEY_CO_ORDERS = list(range(24, 33))  # 9; #26 to #30 at 0.00


@pytest.fixture
def t(seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant) -> Tenant:
    tenant = make_tenant(seed, rw_engine, login_as, fresh_tenant)
    policy(rw_engine, seed, fresh_tenant, CHANGE_ORDER_EVIDENCE, "none")
    return tenant


def _areas(job: dict) -> dict[int, dict]:
    return {w["order_no"]: w for w in job["work_areas"]}


def _co_areas(job: dict) -> dict[int, dict]:
    return {w["order_no"]: w for w in job["change_order_work_areas"]}


def _figures(job: dict) -> tuple[str, str, str, str, str]:
    return (
        job["original_contract"],
        job["approved_change_orders"],
        job["revised_contract"],
        job["unapproved_change_orders"],
        job["eac_in_basis"],
    )


def _elm(t: Tenant) -> dict:
    job = t.new_job(ELM_ID)
    return t.send("POST", f"/api/jobs/{job['id']}/{CONFIRM_ALL}")


def _turley(t: Tenant) -> dict:
    job = t.new_job(TURLEY_ID)
    return t.send("POST", f"/api/jobs/{job['id']}/{CONFIRM_ALL}")


def _approve(client: TestClient, job_id: str, area_id: str, body: dict | None = None, status=200):
    r = client.post(
        f"/api/jobs/{job_id}/work-areas/{area_id}/approval", json=body or AGREED, headers=CSRF
    )
    assert r.status_code == status, r.text
    return r.json()


def _withdraw(client: TestClient, job_id: str, area_id: str, reason: str | None, status=200):
    body = {} if reason is None else {"reason": reason}
    r = client.post(
        f"/api/jobs/{job_id}/work-areas/{area_id}/approval/withdraw", json=body, headers=CSRF
    )
    assert r.status_code == status, r.text
    return r.json()


@pytest.fixture
def as_role(seed: Seed, owner_engine: Engine, login_as):
    """A seed client user given ``role`` in a fresh tenant, logged in there; the rows are
    removed afterwards (the seed users are session-wide and other tests count theirs)."""
    added: list[tuple[uuid.UUID, uuid.UUID]] = []

    def _as(t: Tenant, key: str, role: Role) -> TestClient:
        with tenant_session(owner_engine, t.id) as s:
            s.add(Membership(tenant_id=t.id, user_id=seed.users[key].id, role=role))
        added.append((t.id, seed.users[key].id))
        return login_as(key, tenant=t.id)

    yield _as
    for tenant_id, user_id in added:
        with tenant_session(owner_engine, tenant_id) as s:
            for m in s.execute(
                select(Membership).where(
                    Membership.tenant_id == tenant_id, Membership.user_id == user_id
                )
            ).scalars():
                s.delete(m)


def _approval_rows(t: Tenant) -> int:
    with tenant_session(t.engine, t.id) as s:
        return s.execute(select(func.count()).select_from(ChangeOrderApproval)).scalar_one()


def _unapproved(t: Tenant) -> dict:
    return t.get("/api/change-orders/unapproved")


# --- criterion 2: the figures before any approval -------------------------------------------------


def test_elm_street_before_any_approval(t: Tenant) -> None:
    job = _elm(t)
    assert _figures(job) == (ELM_ORIGINAL, "0.00", ELM_ORIGINAL, ELM_CO, ELM_EAC)
    assert job["unapproved_change_order_count"] == 12
    areas = _areas(job)
    assert areas[18]["approval_label"] == "Change order, not approved"
    assert areas[18]["approved"] is False and areas[18]["approval_id"] is None
    assert areas[1]["approval_label"] is None  # an original has no approval words
    assert areas[17]["approval_label"] is None  # omitted
    assert job["approval_history"] == [] and job["change_order_work_areas"] == []
    assert job["revised_contract_note"] is None


# --- criteria 3 and 4: approve #18 as client_pm, withdraw it --------------------------------------


def test_client_pm_approves_18_and_the_figures_audit_and_words_follow(
    t: Tenant, seed: Seed, as_role
) -> None:
    job = _elm(t)
    pm = as_role(t, "client_pm", Role.client_pm)
    seen = t.audit_rows()
    area = _areas(job)[18]
    assert area["price"] == "5475.00"
    out = _approve(
        pm,
        job["id"],
        area["id"],
        {**AGREED, "agreed_by": "Site owner", "evidence_ref": "e-mail of 2026-09-14", "note": "ok"},
    )
    assert _figures(out) == (ELM_ORIGINAL, "5475.00", "470944.59", "48229.13", ELM_EAC)
    assert out["unapproved_change_order_count"] == 11
    row = _areas(out)[18]
    assert row["approval_label"] == "Change order, approved 2026-09-14 by client_pm"
    assert row["approved"] is True and row["approval_id"]
    assert row["kind_label"] == "Change order, confirmed"  # F07.1's words stand
    rows = t.audit_actions(seen)
    assert [r.action for r in rows] == ["change_order_approved"]
    audit = rows[0]
    assert audit.actor_user_id == seed.users["client_pm"].id
    assert audit.entity_type == "job" and audit.entity_id == job["id"]
    assert audit.detail["rows"]["order_no"] == 18 and audit.detail["rows"]["estimate"] == ELM_ID
    assert audit.detail["rows"]["change_order_approval"] == row["approval_id"]
    assert audit.detail["before"] == {"approved": False, "price": None}
    assert audit.detail["after"]["approved"] is True and audit.detail["after"]["price"] == "5475.00"
    assert audit.detail["after"]["agreed_on"] == "2026-09-14"
    assert _approval_rows(t) == 1
    history = out["approval_history"]
    assert len(history) == 1 and history[0]["action"] == "approved"
    assert history[0]["action_label"] == "Approved"
    assert (history[0]["order_no"], history[0]["price"], history[0]["applies"]) == (
        18,
        "5475.00",
        True,
    )
    assert history[0]["recorded_by"] == "client_pm" and history[0]["agreed_by"] == "Site owner"
    assert history[0]["evidence_ref"] == "e-mail of 2026-09-14" and history[0]["note"] == "ok"
    # The board's two existing columns show the new figures; no column is added.
    board = next(j for j in t.get("/api/jobs")["jobs"] if j["id"] == job["id"])
    assert (board["revised_contract"], board["unapproved_change_orders"]) == (
        "470944.59",
        "48229.13",
    )
    assert board["approved_change_orders"] == "5475.00"


def test_withdrawing_restores_the_figures_and_needs_a_reason(t: Tenant, as_role) -> None:
    job = _elm(t)
    pm = as_role(t, "client_pm", Role.client_pm)
    area = _areas(job)[18]
    _approve(pm, job["id"], area["id"])
    seen = t.audit_rows()
    r = _withdraw(pm, job["id"], area["id"], None, status=422)
    assert r["detail"] == "Give the reason: an approval is withdrawn only with a reason."
    r = _withdraw(pm, job["id"], area["id"], "   ", status=422)
    assert r["detail"] == "Give the reason: an approval is withdrawn only with a reason."
    assert t.audit_actions(seen) == [] and _approval_rows(t) == 1
    out = _withdraw(pm, job["id"], area["id"], "Customer declined the price")
    assert _figures(out) == (ELM_ORIGINAL, "0.00", ELM_ORIGINAL, ELM_CO, ELM_EAC)
    assert _areas(out)[18]["approval_label"] == "Change order, not approved"
    rows = t.audit_actions(seen)
    assert [r.action for r in rows] == ["change_order_approval_withdrawn"]
    assert rows[0].detail["rows"]["order_no"] == 18
    assert (
        rows[0].detail["before"]["approved"] is True
        and rows[0].detail["after"]["approved"] is False
    )
    assert rows[0].detail["after"]["reason"] == "Customer declined the price"
    history = out["approval_history"]  # newest first
    assert [h["action"] for h in history] == ["withdrawn", "approved"]
    assert history[0]["action_label"] == "Approval withdrawn"
    assert history[0]["reason"] == "Customer declined the price"
    assert history[0]["withdraws_id"] == history[1]["id"]
    assert history[1]["applies"] is False
    assert _approval_rows(t) == 2
    # Approving again is a new row.
    again = _approve(pm, job["id"], area["id"])
    assert again["approved_change_orders"] == "5475.00" and _approval_rows(t) == 3
    assert [h["action"] for h in again["approval_history"]] == ["approved", "withdrawn", "approved"]


# --- criterion 5: every priced change order; EST6120638's job -------------------------------------


def test_approving_every_change_order_on_both_fixture_jobs(t: Tenant) -> None:
    elm = _elm(t)
    areas = _areas(elm)
    for o in ELM_CO_ORDERS:
        elm = _approve(t.client, elm["id"], areas[o]["id"])
    assert _figures(elm) == (ELM_ORIGINAL, ELM_CO, ELM_TOTAL, "0.00", ELM_EAC)
    assert elm["unapproved_change_order_count"] == 0
    assert _areas(elm)[20]["approved"] is True  # a change order priced 0.00 may be approved
    turley = _turley(t)
    assert _figures(turley)[:4] == (TURLEY_ORIGINAL, "0.00", TURLEY_ORIGINAL, TURLEY_CO)
    assert turley["unapproved_change_order_count"] == 9
    co = _co_areas(turley)
    assert co == {}
    areas = _areas(turley)
    for o in TURLEY_CO_ORDERS:
        turley = _approve(t.client, turley["id"], areas[o]["id"])
    assert _figures(turley)[:4] == (TURLEY_ORIGINAL, TURLEY_CO, TURLEY_TOTAL, "0.00")
    assert len(turley["approval_history"]) == 9


# --- criterion 6 and rule C: a later version ------------------------------------------------------


def _second_version(
    t: Tenant, *, price18: str | None = None, name18: str | None = None, extra=False
):
    rows = fixture_rows(ELM)
    header = D(ELM_TOTAL)
    for r in rows["Work areas"]:
        if r["order"] == 18:
            if price18 is not None:
                header += D(price18) - D("5475.00")
                r["price"] = price18
            if name18 is not None:
                r["name"] = name18
    if extra:
        header += D("100.00")
        rows["Work areas"].append(
            {
                "estimate_id": ELM_ID,
                "order": 30,
                "kept": "Y",
                "name": "Extra planting",
                "price": "100.00",
            }
        )
        rows["Estimate costs"].append(
            {"estimate_id": ELM_ID, "order": 30, "cost_code": "230", "amount": "60.00"}
        )
    rows["Estimates"][0]["price"] = str(header)
    upload_template(t.client, build_workbook(sheets=rows), f"elm_v{uuid.uuid4().hex[:4]}.xlsx")
    run_until_quiet(t.engine)


def test_a_price_change_ends_the_approval_and_says_so(t: Tenant) -> None:
    job = _elm(t)
    job = _approve(t.client, job["id"], _areas(job)[18]["id"])
    assert job["revised_contract"] == "470944.59"
    _second_version(t, price18="6000.00")
    job = t.job(job["id"])
    assert _figures(job)[:4] == (ELM_ORIGINAL, "0.00", ELM_ORIGINAL, str(D(ELM_CO) + D("525.00")))
    row = _areas(job)[18]
    assert row["approved"] is False and row["price"] == "6000.00"
    assert row["approval_label"] == "Change order, not approved"
    notes = [i for i in job["attention"] if i["code"] == "CO_APPROVAL_NOT_CARRIED"]
    assert len(notes) == 1
    assert "5,475.00" in notes[0]["message"] and "6,000.00" in notes[0]["message"]
    assert "version 2" in notes[0]["message"] and "(D-42)" in notes[0]["message"]
    assert notes[0]["message"].count(". ") == 0  # one sentence
    assert row["approval_note"] == notes[0]["message"]
    history = job["approval_history"]
    assert history[0]["applies"] is False and history[0]["ended"] == "version 2 priced it 6,000.00"
    # Rule C: a version 3 that restores the price does not revive the approval.
    _second_version(t, price18="5475.00")
    job = t.job(job["id"])
    assert _figures(job)[:4] == (ELM_ORIGINAL, "0.00", ELM_ORIGINAL, ELM_CO)
    assert _areas(job)[18]["approved"] is False
    assert [i["code"] for i in job["attention"]].count("CO_APPROVAL_NOT_CARRIED") == 1
    # Home carries the need (after the F07 and F08 rules: a sold, unlinked job reads as
    # backlog first, so the job is linked and in progress here); approving again is a new
    # row and clears the sentence.
    t.link(job["id"], "elm", in_progress=True)
    home = t.get("/api/home")
    line = next(j for j in home["jobs"] if j["id"] == job["id"])
    assert line["code"] == "co_approval_not_carried"
    assert line["message"] == (
        "1 change order approval no longer applies: the price or the name changed in a later "
        "version. Approve it again or withdraw it."
    )
    job = _approve(t.client, job["id"], _areas(job)[18]["id"])
    assert job["revised_contract"] == "470944.59"
    assert [i["code"] for i in job["attention"]].count("CO_APPROVAL_NOT_CARRIED") == 0
    assert [h["applies"] for h in job["approval_history"]] == [True, False]


def test_an_unchanged_second_version_carries_the_approval_and_a_renamed_one_does_not(
    t: Tenant,
) -> None:
    job = _elm(t)
    job = _approve(t.client, job["id"], _areas(job)[18]["id"])
    _second_version(t, extra=True)
    job = t.job(job["id"])
    assert len(_areas(job)) == 30
    assert _areas(job)[18]["approved"] is True
    assert (job["approved_change_orders"], job["revised_contract"]) == ("5475.00", "470944.59")
    assert [i["code"] for i in job["attention"]].count("CO_APPROVAL_NOT_CARRIED") == 0
    _second_version(t, name18="CO: Ledge removal, renamed", extra=True)
    job = t.job(job["id"])
    row = _areas(job)[18]
    assert row["approved"] is False and row["kind"] is None  # the kind did not carry either
    assert (job["approved_change_orders"], job["revised_contract"]) == ("0.00", ELM_ORIGINAL)
    notes = [i for i in job["attention"] if i["code"] == "CO_APPROVAL_NOT_CARRIED"]
    assert len(notes) == 1 and 'renamed it "CO: Ledge removal, renamed"' in notes[0]["message"]
    assert "version 3" in notes[0]["message"]
    # An ended approval can be withdrawn with a reason (the platform wrote no withdrawal).
    out = _withdraw(t.client, job["id"], row["id"], "Renamed in the estimate; re-approve later")
    assert [i["code"] for i in out["attention"]].count("CO_APPROVAL_NOT_CARRIED") == 0
    assert [h["action"] for h in out["approval_history"]] == ["withdrawn", "approved"]


def test_a_deleted_change_order_keeps_its_history_and_counts_nowhere(t: Tenant) -> None:
    """Plan answer 7: version 2 has no row at #29 at all."""
    job = _elm(t)
    area29 = _areas(job)[29]
    job = _approve(t.client, job["id"], area29["id"])
    rows = fixture_rows(ELM)
    rows["Work areas"] = [r for r in rows["Work areas"] if r["order"] != 29]
    rows["Estimate costs"] = [r for r in rows["Estimate costs"] if r["order"] != 29]
    rows["Estimates"][0]["price"] = str(D(ELM_TOTAL) - D("5475.00"))
    upload_template(t.client, build_workbook(sheets=rows), "elm_v2_del.xlsx")
    run_until_quiet(t.engine)
    job = t.job(job["id"])
    assert 29 not in _areas(job)
    assert _figures(job)[:4] == (ELM_ORIGINAL, "0.00", ELM_ORIGINAL, str(D(ELM_CO) - D("5475.00")))
    notes = [i for i in job["attention"] if i["code"] == "CO_APPROVAL_NOT_CARRIED"]
    assert len(notes) == 1 and "version 2 does not carry it" in notes[0]["message"]
    history = job["approval_history"]
    assert len(history) == 1 and history[0]["applies"] is False and history[0]["order_no"] == 29
    assert all(r["order_no"] != 29 for r in _unapproved(t)["rows"])
    # The ended approval is withdrawn through the history's work-area id (no row is left).
    out = _withdraw(t.client, job["id"], area29["id"], "Removed from the estimate")
    assert [h["action"] for h in out["approval_history"]] == ["withdrawn", "approved"]
    assert [i["code"] for i in out["attention"]].count("CO_APPROVAL_NOT_CARRIED") == 0


# --- criterion 7: refusals, one sentence each, nothing written ------------------------------------


def test_refusals_write_nothing(t: Tenant, seed: Seed, rw_engine: Engine) -> None:
    job = t.new_job(ELM_ID)  # nothing confirmed yet
    areas = _areas(job)
    r = _approve(t.client, job["id"], areas[18]["id"], status=422)
    assert r["detail"] == (
        "Work area #18 is not confirmed; confirm it as a change order before approving it."
    )
    assert _approval_rows(t) == 0
    job = t.send("POST", f"/api/jobs/{job['id']}/{CONFIRM_ALL}")
    areas = _areas(job)
    seen = t.audit_rows()  # from here only the policy changes below write (policy_set rows)
    r = _approve(t.client, job["id"], areas[1]["id"], status=422)
    assert r["detail"] == "Work area #1 is an original work area; only a change order is approved."
    r = _approve(t.client, job["id"], areas[17]["id"], status=422)
    assert r["detail"] == "Work area #17 is omitted; an omitted work area cannot be approved."
    r = _approve(t.client, job["id"], str(uuid.uuid4()), status=404)
    assert r["detail"] == "That work area is not on the latest version of an estimate on this job."
    tomorrow = (date.today() + timedelta(days=2)).isoformat()  # past today in any US zone
    r = _approve(t.client, job["id"], areas[18]["id"], {"agreed_on": tomorrow}, status=422)
    assert r["detail"].startswith("The date the customer agreed cannot be after today (")
    r = _withdraw(t.client, job["id"], areas[18]["id"], "no", status=409)
    assert r["detail"] == "Work area #18 is not approved; there is nothing to withdraw."
    # The evidence policy: a reference required; then undecided.
    policy(rw_engine, seed, t.id, CHANGE_ORDER_EVIDENCE, "reference")
    r = _approve(t.client, job["id"], areas[18]["id"], status=422)
    assert r["detail"] == (
        "A reference to the evidence is required by this company's policy "
        "(Change order evidence, D-42)."
    )
    r = _approve(t.client, job["id"], areas[18]["id"], {**AGREED, "evidence_ref": "  "}, status=422)
    assert "reference to the evidence is required" in r["detail"]
    with tenant_session(rw_engine, t.id) as s:
        from app.domain.config.models import TenantPolicy

        row = s.execute(
            select(TenantPolicy).where(TenantPolicy.key == CHANGE_ORDER_EVIDENCE)
        ).scalar_one()
        s.delete(row)
    r = _approve(t.client, job["id"], areas[18]["id"], status=409)
    assert r["detail"] == (
        "Decide the policy key Change order evidence before approving a change order (D-42)."
    )
    assert {a.action for a in t.audit_actions(seen)} == {"policy_set"} and _approval_rows(t) == 0
    policy(rw_engine, seed, t.id, CHANGE_ORDER_EVIDENCE, "none")
    job = _approve(t.client, job["id"], areas[18]["id"])
    seen = t.audit_rows()
    r = _approve(t.client, job["id"], areas[18]["id"], status=409)
    assert r["detail"] == (
        "Work area #18 is already approved (2026-09-14 by rotate_me); withdraw that approval first."
    )
    # An approved change order cannot be changed to original until the approval is withdrawn.
    r = t.client.post(
        f"/api/jobs/{job['id']}/work-areas/{areas[18]['id']}/kind",
        json={"kind": "original"},
        headers=CSRF,
    )
    assert r.status_code == 409
    assert r.json()["detail"] == (
        "Work area #18 is an approved change order; withdraw the approval before changing its kind."
    )
    assert t.audit_actions(seen) == [] and _approval_rows(t) == 1
    assert _areas(t.job(job["id"]))[18]["kind"] == "change_order"


# --- criterion 8: roles and isolation -------------------------------------------------------------


def test_roles_and_isolation(t: Tenant, seed: Seed, as_role, login_as) -> None:
    job = _elm(t)
    area = _areas(job)[18]["id"]
    viewer = as_role(t, "client_viewer", Role.client_viewer)
    assert viewer.get(f"/api/jobs/{job['id']}").status_code == 200
    assert viewer.get("/api/change-orders/unapproved").status_code == 200  # roles as the job pages
    _approve(viewer, job["id"], area, status=403)
    _withdraw(viewer, job["id"], area, "no", status=403)
    pm = as_role(t, "client_pm", Role.client_pm)
    r = pm.post(
        f"/api/jobs/{job['id']}/work-areas/{area}/kind", json={"kind": "original"}, headers=CSRF
    )
    assert r.status_code == 403  # F07.1's matrix unchanged for client_pm
    assert pm.post(f"/api/jobs/{job['id']}/{CONFIRM_ALL}", headers=CSRF).status_code == 403
    _approve(pm, job["id"], area)
    # A second tenant never sees or changes another's approvals.
    other = login_as("firm_admin", tenant=seed.tenant_b)
    assert other.get(f"/api/jobs/{job['id']}").status_code == 404
    _approve(other, job["id"], area, status=404)
    _withdraw(other, job["id"], area, "no", status=404)
    assert other.get("/api/change-orders/unapproved").json()["rows"] == []
    with tenant_session(t.engine, seed.tenant_b) as s:
        assert s.execute(select(func.count()).select_from(ChangeOrderApproval)).scalar_one() == 0
    assert _approval_rows(t) == 1


# --- criterion 9: the policy key ------------------------------------------------------------------


def test_the_evidence_key_has_no_default_and_gates_home(
    seed: Seed, rw_engine: Engine, login_as, fresh_tenant
) -> None:
    t = make_tenant(seed, rw_engine, login_as, fresh_tenant, load=False)  # time zone and basis set
    billing_policy(rw_engine, seed, fresh_tenant)  # the two F08 keys
    rows = {p["key"]: p for p in t.get("/api/config/policy")}
    key = rows[CHANGE_ORDER_EVIDENCE]
    assert key["decided"] is False and key["kind"] == "choice" and key["waiting"] is None
    assert key["description"] == "What an approval of a change order must carry (D-42)."
    assert [(o["value"], o["label"]) for o in key["options"]] == [
        ("none", "None required"),
        ("reference", "A reference is required"),
    ]
    home = t.get("/api/home")
    line = next(ln for ln in home["setup"] if ln["code"] == "policy")
    assert line["done"] is False
    assert line["message"] == "1 of 5 policy key needed now is not decided: Change order evidence."
    seen = t.audit_rows()
    r = t.client.put(
        f"/api/config/policy/{CHANGE_ORDER_EVIDENCE}", json={"value": "signed"}, headers=CSRF
    )
    assert r.status_code == 422 and "None required" in r.json()["detail"]
    r = t.client.put(
        f"/api/config/policy/{CHANGE_ORDER_EVIDENCE}",
        json={"value": "reference", "decision_ref": "D-42"},
        headers=CSRF,
    )
    assert r.status_code == 200
    assert (r.json()["value"], r.json()["value_label"]) == ("reference", "A reference is required")
    assert [a.action for a in t.audit_actions(seen)] == ["policy_set"]
    line = next(ln for ln in t.get("/api/home")["setup"] if ln["code"] == "policy")
    assert line["done"] is True


# --- criterion 10: the unapproved change orders list ----------------------------------------------


def test_the_unapproved_list_on_both_fixture_jobs(t: Tenant) -> None:
    elm = _elm(t)
    turley = _turley(t)
    body = _unapproved(t)
    assert body["tenant_name"] and body["as_of"]
    assert body["count"] == 21 and body["total"] == str(D(ELM_CO) + D(TURLEY_CO))
    by_job = {}
    for r in body["rows"]:
        by_job.setdefault(r["job_id"], []).append(r)
    elm_rows, turley_rows = by_job[elm["id"]], by_job[turley["id"]]
    assert [r["order_no"] for r in elm_rows] == ELM_CO_ORDERS
    assert sum(D(r["price"]) for r in elm_rows) == D(ELM_CO)
    assert sum(1 for r in elm_rows if D(r["price"]) == 0) == 1
    assert [r["order_no"] for r in turley_rows] == TURLEY_CO_ORDERS
    assert sum(D(r["price"]) for r in turley_rows) == D(TURLEY_CO)
    row = elm_rows[0]
    assert (row["job_name"], row["estimate_number"], row["estimator"]) == (
        elm["name"],
        ELM_ID,
        "Estimator A",
    )
    assert row["first_seen"] == date.today().isoformat() or row["days"] in (0, 1)
    assert row["approval_ended"] is False
    assert t.get("/api/jobs")["unapproved_change_order_count"] == 21
    elm = _approve(t.client, elm["id"], _areas(elm)[18]["id"])
    body = _unapproved(t)
    assert body["count"] == 20 and body["total"] == str(D(ELM_CO) + D(TURLEY_CO) - D("5475.00"))
    assert t.get("/api/jobs")["unapproved_change_order_count"] == 20
    # Closed and cancelled jobs are absent.
    t.send("PATCH", f"/api/jobs/{turley['id']}", {"status": "closed"})
    body = _unapproved(t)
    assert body["count"] == 11 and {r["job_id"] for r in body["rows"]} == {elm["id"]}
    # Home: one need per job with unapproved change orders, after the F07 and F08 entries
    # (a sold, unlinked job reads as backlog first).
    t.link(elm["id"], "elm", in_progress=True)
    line = next(j for j in t.get("/api/home")["jobs"] if j["id"] == elm["id"])
    assert (line["code"], line["message"], line["count"]) == (
        "change_orders_unapproved",
        "11 change orders, 48,229.13, not approved.",
        11,
    )


# --- criterion 11: F08's figures follow -----------------------------------------------------------


def test_remaining_to_bill_reads_the_new_revised_contract(t: Tenant) -> None:
    billing_policy(t.engine, t.seed, t.id)
    job = _elm(t)
    t.link(job["id"], "elm", in_progress=True)
    invoice = document_payload(
        "5301", customer="201", date="2026-09-01", lines=[line("470000.00", WORK_ITEM)]
    )
    apply_payloads(t.engine, t.id, [("Invoice", invoice)])
    job = t.job(job["id"])
    assert job["billing"]["remaining_to_bill"] == str(D(ELM_ORIGINAL) - D("470000.00"))
    assert [i["code"] for i in job["attention"]].count("BILLED_OVER_CONTRACT") == 1
    job = _approve(t.client, job["id"], _areas(job)[18]["id"])
    assert job["billing"]["remaining_to_bill"] == str(D("470944.59") - D("470000.00"))
    assert [i["code"] for i in job["attention"]].count("BILLED_OVER_CONTRACT") == 0


# --- criterion 12: money as strings; nothing but history stored -----------------------------------


def test_money_is_strings_and_a_read_stores_nothing(t: Tenant) -> None:
    job = _elm(t)
    job = _approve(t.client, job["id"], _areas(job)[18]["id"])
    for body in (job, t.get("/api/jobs"), _unapproved(t)):
        values = money_values(body)
        assert values
        for path, v in values:
            assert v is None or MONEY.match(v), (path, v)
    for key in (
        "original_contract",
        "approved_change_orders",
        "revised_contract",
        "unapproved_change_orders",
    ):
        assert MONEY.match(job[key])
    for r in _unapproved(t)["rows"]:
        assert MONEY.match(r["price"])
    assert MONEY.match(_unapproved(t)["total"])
    for h in job["approval_history"]:
        assert MONEY.match(h["price"])
    seen = t.audit_rows()
    rows = _approval_rows(t)
    t.job(job["id"])
    t.get("/api/jobs")
    _unapproved(t)
    t.get("/api/home")
    assert t.audit_rows() == seen and _approval_rows(t) == rows == 1


# --- criterion 14 (answer B): an estimate attached as a change order ------------------------------


def test_work_areas_of_an_estimate_attached_as_a_change_order_are_approvable(t: Tenant) -> None:
    """Fixture: EST6120638 (the reviewed Turley workbook) attached to the 67 Elm Street
    job as a change order, the D-03 attach path."""
    job = _elm(t)
    turley_id = t.estimate_id(TURLEY_ID)
    job = t.send(
        "POST",
        f"/api/jobs/{job['id']}/estimates",
        {"estimate_id": turley_id, "role": "change_order", "note": "one contract"},
    )
    assert _figures(job)[:4] == (
        ELM_ORIGINAL,
        "0.00",
        ELM_ORIGINAL,
        str(D(ELM_CO) + D(TURLEY_TOTAL)),
    )
    assert job["unapproved_change_order_count"] == 12 + 25
    co = _co_areas(job)
    assert len(co) == 32 and co[1]["estimate_external_id"] == TURLEY_ID
    assert co[1]["approval_label"] == "Change order, not approved"
    assert co[6]["approval_label"] is None  # omitted on the attached estimate
    price1 = D(co[1]["price"])
    out = _approve(t.client, job["id"], co[1]["id"])
    assert (out["approved_change_orders"], out["revised_contract"]) == (
        str(price1),
        str(D(ELM_ORIGINAL) + price1),
    )
    assert out["unapproved_change_orders"] == str(D(ELM_CO) + D(TURLEY_TOTAL) - price1)
    assert _co_areas(out)[1]["approval_label"] == "Change order, approved 2026-09-14 by rotate_me"
    assert out["approval_history"][0]["estimate_external_id"] == TURLEY_ID
    rows = [r for r in _unapproved(t)["rows"] if r["estimate_number"] == TURLEY_ID]
    assert len(rows) == 24 and {r["job_id"] for r in rows} == {job["id"]}
    # An original work area of the attached estimate cannot be made original on this job;
    # an estimate attached with no work areas loaded has nothing to approve.
    r = t.client.post(
        f"/api/jobs/{job['id']}/work-areas/{co[2]['id']}/kind",
        json={"kind": "original"},
        headers=CSRF,
    )
    assert r.status_code == 404
