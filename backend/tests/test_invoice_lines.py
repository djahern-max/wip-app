"""F08.1 Part 1 (D-45): a person assigns earlier invoice lines to work areas, billed to
date per work area is computed from the tied lines, and billing on an unapproved change
order is flagged without changing any figure. Through the API on the reviewed 67 Elm
Street workbook, linked to a synthetic project, with constructed documents applied
through the normalizers (nothing here is a copy of a real invoice)."""

import uuid
from collections.abc import Callable
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from app.core.db import tenant_session
from app.domain.billing.models import BillingLineWorkArea
from app.domain.config.policy import CHANGE_ORDER_EVIDENCE
from app.tenancy.models import Membership, Role
from tests.billing_helpers import (
    DEPOSIT_ITEM,
    FUEL_ITEM,
    WORK_ITEM,
    apply_payloads,
    billing_policy,
    document_payload,
    line,
    voided_payload,
)
from tests.config_helpers import run_until_quiet
from tests.conftest import CSRF, Seed
from tests.estimate_helpers import ELM, build_workbook, fixture_rows, upload_template
from tests.job_helpers import ELM_ID, TURLEY_ID, Tenant, make_tenant, money_values, policy
from tests.test_zz_response_scan import _walk

D = Decimal
ELM_CUSTOMER = "201"  # the project row the F07 helpers seed for 67 Elm Street
CONFIRM_KINDS = "work-areas/kinds/confirm-suggested"
ELM_CONTRACT = "465469.59"
ELM_EAC = "327929.93"
LEDGE = [18, *range(22, 30)]  # the nine ledge removal change orders, 5,475.00 each
FOUR = [
    ("Mobilization", "5500.00"),
    ("Erosion Control & Site Prep", "10830.68"),
    ("Demo Existing Wall", "17142.92"),
    ("New Retaining Wall", "132820.88"),
]
PMT2 = "166294.48"
FLAG_NINE = (
    '49,275.00 has been billed on job "67 Elm Street | Parking Lot" on 9 change orders that '
    "are not approved: #18, #22, #23, #24, #25, #26, #27, #28, #29 (D-45)."
)


@pytest.fixture
def as_role(seed: Seed, owner_engine: Engine, login_as):
    """A seed client user given ``role`` in the fresh tenant, logged in there; the rows
    are removed afterwards (as the F07.4 tests do it)."""
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


@pytest.fixture
def t(seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant) -> Tenant:
    tenant = make_tenant(seed, rw_engine, login_as, fresh_tenant)
    billing_policy(rw_engine, seed, fresh_tenant)
    policy(rw_engine, seed, fresh_tenant, CHANGE_ORDER_EVIDENCE, "none")
    return tenant


def _elm(t: Tenant) -> dict:
    job = t.new_job(ELM_ID)
    t.link(job["id"], "elm", in_progress=True)
    return t.send("POST", f"/api/jobs/{job['id']}/{CONFIRM_KINDS}")


def sales_line(
    amount: str,
    description: str,
    *,
    qty: str = "1",
    rate: str | None = None,
    service_date: str | None = None,
    item: str = WORK_ITEM,
) -> dict:
    """A priced line with the raw detail the normalizer does not keep (quantity, rate,
    service date), read from raw for display."""
    ln = line(amount, item, description)
    detail = ln["SalesItemLineDetail"]
    detail["Qty"] = D(qty)
    detail["UnitPrice"] = D(rate if rate is not None else amount)
    if service_date:
        detail["ServiceDate"] = service_date
    return ln


def _pmt2(t: Tenant, lines: list[dict] | None = None) -> None:
    doc = document_payload(
        "5202",
        customer=ELM_CUSTOMER,
        date="2026-08-21",
        doc_number="EST6115758_PMT2",
        lines=lines or [sales_line(amount, name) for name, amount in FOUR],
    )
    apply_payloads(t.engine, t.id, [("Invoice", doc)])


def _ledge(t: Tenant) -> None:
    """The two constructed ledge invoices (the owner's export of 2026-10-07: one line per
    day, "Ledge Removal", quantity 1, rate 5,475.00, a service date per line, no "#n")."""
    july = document_payload(
        "5301",
        customer=ELM_CUSTOMER,
        date="2026-07-31",
        doc_number="3107",
        lines=[
            sales_line("5475.00", "Ledge Removal", service_date=d)
            for d in ("2026-07-07", "2026-07-17")
        ],
    )
    later = document_payload(
        "5302",
        customer=ELM_CUSTOMER,
        date="2026-09-30",
        doc_number="3142",
        lines=[
            sales_line("5475.00", "Ledge Removal", service_date=d)
            for d in (
                "2026-08-04",
                "2026-09-16",
                "2026-09-17",
                "2026-09-18",
                "2026-09-21",
                "2026-09-22",
                "2026-09-23",
            )
        ],
    )
    apply_payloads(t.engine, t.id, [("Invoice", july), ("Invoice", later)])


def _lines(job: dict) -> list[dict]:
    return job["invoice_lines"]["lines"]


def _areas(job: dict) -> dict[int, dict]:
    return {w["order_no"]: w for w in job["work_areas"]}


def _choice(job: dict, n: int, estimate: str = ELM_ID) -> str:
    return next(
        w["id"]
        for w in job["invoice_lines"]["work_areas"]
        if w["order_no"] == n and w["estimate_external_id"] == estimate
    )


def _assign(t: Tenant, job_id: str, line_id: str, area_id: str, status: int = 200, note=None):
    body = {"estimate_work_area_id": area_id}
    if note is not None:
        body["note"] = note
    return t.send("PUT", f"/api/jobs/{job_id}/invoice-lines/{line_id}/work-area", body, status)


def _suggested_pairs(job: dict) -> list[dict]:
    return [
        {
            "billing_line_id": ln["billing_line_id"],
            "estimate_work_area_id": ln["suggested_work_area_id"],
        }
        for ln in _lines(job)
        if ln["suggested_work_area_id"]
    ]


def _confirm_all(t: Tenant, job: dict, status: int = 200) -> dict:
    return t.send(
        "POST",
        f"/api/jobs/{job['id']}/invoice-lines/assign-suggested",
        {"assignments": _suggested_pairs(job)},
        status,
    )


def _figures(job: dict) -> tuple:
    b = job["billing"]
    return (
        job["revised_contract"],
        job["eac_in_basis"],
        b["billed_to_date"],
        b["collected_to_date"],
        b["open_ar"],
        b["remaining_to_bill"],
    )


def _flag(detail: dict) -> list[str]:
    return [i["message"] for i in detail["attention"] if i["code"] == "BILLING_UNAPPROVED_CO"]


def _rows(t: Tenant) -> int:
    with tenant_session(t.engine, t.id) as s:
        return s.execute(select(func.count()).select_from(BillingLineWorkArea)).scalar_one()


def _row_on_board(t: Tenant, job_id: str) -> dict:
    return next(j for j in t.get("/api/jobs")["jobs"] if j["id"] == job_id)


# --- criteria 2 and 3: the four lines, suggested by name, then confirmed --------------------


def test_before_any_assignment_nothing_is_tied_and_each_line_is_suggested(t: Tenant) -> None:
    job = _elm(t)
    _pmt2(t)
    job = t.job(job["id"])
    areas = _areas(job)
    assert all(areas[n]["billed_to_date"] == "0.00" for n in range(1, 17))
    assert [areas[n]["left_to_bill"] for n in (1, 2, 3, 4)] == [p for _n, p in FOUR]
    assert areas[18]["left_to_bill"] is None  # an unapproved change order
    assert areas[17]["billed_to_date"] == "0.00" and areas[17]["left_to_bill"] is None  # omitted
    assert job["invoice_lines"]["not_assigned_to_work_area"] == PMT2
    assert job["work_area_totals"] == {
        "price": "519173.72",
        "billed_to_date": "0.00",
        "left_to_bill": ELM_CONTRACT,
    }
    lines = _lines(job)
    assert [
        (ln["description"], ln["amount"], ln["how"], ln["suggested_label"]) for ln in lines
    ] == [(name, amount, "none", f"#{n} {name}") for n, (name, amount) in enumerate(FOUR, 1)]
    assert all(ln["offered"] and ln["work_area_id"] is None for ln in lines)
    assert [(ln["quantity"], ln["rate"], ln["service_date"]) for ln in lines] == [
        ("1", amount, None) for _n, amount in FOUR
    ]
    assert lines[0]["doc_number"] == "EST6115758_PMT2" and lines[0]["line_no"] == 1
    assert job["invoice_lines"]["suggested"] == 4
    assert _rows(t) == 0
    assert t.get(f"/api/jobs/{job['id']}/invoice-lines")["lines"] == lines


def test_confirm_all_as_suggested_assigns_the_four_and_changes_no_job_figure(t: Tenant) -> None:
    job = _elm(t)
    _pmt2(t)
    job = t.job(job["id"])
    before = _figures(job)
    since = t.audit_rows()
    after = _confirm_all(t, job)
    assert (after["assigned"], after["message"]) == (4, "4 lines assigned as suggested.")
    areas = _areas(after)
    assert [(areas[n]["billed_to_date"], areas[n]["left_to_bill"]) for n in (1, 2, 3, 4)] == [
        (p, "0.00") for _n, p in FOUR
    ]
    assert after["invoice_lines"]["not_assigned_to_work_area"] == "0.00"
    assert after["work_area_totals"]["billed_to_date"] == PMT2
    assert after["work_area_totals"]["left_to_bill"] == str(D(ELM_CONTRACT) - D(PMT2))
    assert _figures(after) == before and before[2] == PMT2
    assert [a.action for a in t.audit_actions(since)] == ["billing_line_assigned"] * 4
    first = t.audit_actions(since)[0]
    assert first.detail["before"] == {"work_area": None}
    assert first.detail["after"]["work_area"]["order_no"] == 1
    assert first.detail["after"]["work_area"]["estimate"] == ELM_ID
    assert first.detail["rows"]["document"] == "EST6115758_PMT2"
    assert _rows(t) == 4
    lines = _lines(after)
    assert all(ln["how"] == "assigned" and ln["suggested_work_area_id"] is None for ln in lines)
    assert lines[0]["how_label"] == "Assigned by rotate_me" and lines[0]["assigned_at"]
    # Pressing it again has nothing to assign and writes nothing.
    again = _confirm_all(t, after)
    assert (again["assigned"], again["message"]) == (0, "Nothing was left to assign.")
    assert _rows(t) == 4


# --- criterion 4: "#n" ties by itself; position decides nothing ------------------------------


def test_a_hash_line_is_tied_with_no_row_and_order_does_not_matter(t: Tenant) -> None:
    job = _elm(t)
    lines = [sales_line(amount, name) for name, amount in FOUR]
    lines[2]["Description"] = "#3 Demo Existing Wall"
    _pmt2(t, [lines[3], lines[2], lines[0], lines[1]])  # another order
    job = t.job(job["id"])
    by_desc = {ln["description"]: ln for ln in _lines(job)}
    hashed = by_desc["#3 Demo Existing Wall"]
    assert (hashed["how"], hashed["how_label"], hashed["line_no"]) == (
        "number",
        "By its number (#n)",
        2,
    )
    assert hashed["work_area_label"] == "#3 Demo Existing Wall"
    assert hashed["suggested_work_area_id"] is None
    assert _areas(job)[3]["billed_to_date"] == "17142.92"
    assert _areas(job)[4]["billed_to_date"] == "0.00"  # first on the invoice, untied
    assert job["invoice_lines"]["not_assigned_to_work_area"] == str(D(PMT2) - D("17142.92"))
    assert _rows(t) == 0 and t.audit_actions(t.audit_rows()) == []
    # Neither an assignment nor a clear can change a line tied by its number.
    _assign(t, job["id"], hashed["billing_line_id"], _choice(job, 4), status=422)
    t.send(
        "DELETE",
        f"/api/jobs/{job['id']}/invoice-lines/{hashed['billing_line_id']}/work-area",
        status=422,
    )
    assert _rows(t) == 0


def test_a_hash_number_on_more_than_one_attached_estimate_ties_nothing(t: Tenant) -> None:
    """The owner's answer 1: EST6120638 attached as a change order carries a #1 too."""
    job = _elm(t)
    t.send(
        "POST",
        f"/api/jobs/{job['id']}/estimates",
        {"estimate_id": t.estimate_id(TURLEY_ID), "role": "change_order"},
    )
    lines = [sales_line("5500.00", "#1 Mobilization"), sales_line("10830.68", "#2 Erosion")]
    _pmt2(t, lines)
    job = t.job(job["id"])
    one, two = _lines(job)
    assert _areas(job)[1]["billed_to_date"] == "0.00" and _areas(job)[2]["billed_to_date"] == "0.00"
    assert one["how"] == "none" and one["work_area_id"] is None and one["offered"]
    assert "more than one estimate" in one["note"] and ELM_ID in one["note"]
    assert two["how"] == "none" and "more than one estimate" in two["note"]  # #2 is on both
    # The pick-list labels the other estimate's work areas; an assignment settles it.
    choice = _choice(job, 1, TURLEY_ID)
    label = next(w["label"] for w in job["invoice_lines"]["work_areas"] if w["id"] == choice)
    assert label == "#1 MOBILIZATION (EST6120638)"
    job = _assign(t, job["id"], one["billing_line_id"], choice)
    turley_areas = {w["order_no"]: w for w in job["change_order_work_areas"]}
    assert (
        turley_areas[1]["billed_to_date"] == "5500.00" and turley_areas[1]["left_to_bill"] is None
    )
    assert _flag(job) == [
        '5,500.00 has been billed on job "67 Elm Street | Parking Lot" on 1 change order that '
        "is not approved: #1 of EST6120638 (D-45)."
    ]


# --- criteria 5 to 7: the ledge invoices and the flag ---------------------------------------


def test_the_ledge_lines_assigned_raise_the_flag_and_change_no_figure(t: Tenant, as_role) -> None:
    job = _elm(t)
    _pmt2(t)
    _ledge(t)
    job = t.job(job["id"])
    ledge = [ln for ln in _lines(job) if ln["description"] == "Ledge Removal"]
    assert len(ledge) == 9
    assert all(ln["suggested_work_area_id"] is None for ln in ledge)  # change b: no suggestion
    assert [(ln["quantity"], ln["rate"]) for ln in ledge] == [("1", "5475.00")] * 9
    assert ledge[0]["service_date"] == "2026-07-07" and ledge[-1]["service_date"] == "2026-09-23"
    assert _flag(job) == [] and _flag(_row_on_board(t, job["id"])) == []
    confirmed = _confirm_all(t, job)
    assert confirmed["assigned"] == 4  # the four named lines; none of the nine
    assert all(
        ln["how"] == "none" for ln in _lines(confirmed) if ln["description"] == "Ledge Removal"
    )
    before = _figures(confirmed)
    assert before[:2] == (ELM_CONTRACT, ELM_EAC)
    assert before[2] == str(D(PMT2) + D("49275.00"))
    since = t.audit_rows()
    for ln, n in zip(ledge, LEDGE, strict=True):
        job = _assign(t, job["id"], ln["billing_line_id"], _choice(job, n))
    assert [a.action for a in t.audit_actions(since)] == ["billing_line_assigned"] * 9
    assert _figures(job) == before
    assert _flag(job) == [FLAG_NINE]
    assert _flag(_row_on_board(t, job["id"])) == [FLAG_NINE]
    home = next(j for j in t.get("/api/home")["jobs"] if j["id"] == job["id"])
    assert (home["code"], home["message"]) == (
        "billing_unapproved_co",
        "49,275.00 billed on 9 change orders that are not approved: #18, #22, #23, #24, #25, "
        "#26, #27, #28, #29 (D-45).",
    )
    assert (
        _areas(job)[18]["billed_to_date"] == "5475.00" and _areas(job)[18]["left_to_bill"] is None
    )
    assert job["invoice_lines"]["not_assigned_to_work_area"] == "0.00"
    # Approving one of the nine: eight remain; withdrawing restores nine. Never "payment".
    pm = as_role(t, "client_pm", Role.client_pm)
    area18 = _areas(job)[18]["id"]
    r = pm.post(
        f"/api/jobs/{job['id']}/work-areas/{area18}/approval",
        json={"agreed_on": "2026-09-14"},
        headers=CSRF,
    )
    assert r.status_code == 200, r.text
    approved = r.json()
    assert _flag(approved) == [
        '43,800.00 has been billed on job "67 Elm Street | Parking Lot" on 8 change orders '
        "that are not approved: #22, #23, #24, #25, #26, #27, #28, #29 (D-45)."
    ]
    assert _areas(approved)[18]["left_to_bill"] == "0.00"
    assert approved["revised_contract"] == "470944.59" and approved["eac_in_basis"] == ELM_EAC
    r = pm.post(
        f"/api/jobs/{job['id']}/work-areas/{area18}/approval/withdraw",
        json={"reason": "owner decides with the project manager"},
        headers=CSRF,
    )
    assert r.status_code == 200, r.text
    assert _flag(r.json()) == [FLAG_NINE] and _figures(r.json()) == before
    for text in _flag(r.json()) + _flag(approved):
        assert not any(w in text.lower() for w in ("paid", "payment", "collected"))


# --- criterion 8: what is not offered; roles; isolation -------------------------------------


def test_surcharge_deposit_discount_and_voided_lines_are_listed_and_not_offered(t: Tenant) -> None:
    job = _elm(t)
    dep = document_payload(
        "5201",
        customer=ELM_CUSTOMER,
        date="2026-06-30",
        doc_number="EST6115758_DEP",
        lines=[sales_line("149800.00", "Mobilization", item=DEPOSIT_ITEM)],
    )
    mixed = document_payload(
        "5401",
        customer=ELM_CUSTOMER,
        date="2026-09-15",
        lines=[
            sales_line("70290.81", "Mobilization"),
            sales_line("3514.54", "Mobilization", item=FUEL_ITEM),
        ],
    )
    # A 10.00 discount line: subtotal 73,805.35 − 10.00 = 73,795.35 (the F05 identity).
    mixed["Line"].insert(
        2, {"DetailType": "DiscountLineDetail", "Amount": D("10.00"), "Description": "Mobilization"}
    )
    mixed["TotalAmt"] = mixed["Balance"] = D("73795.35")
    gone = document_payload(
        "5402",
        customer=ELM_CUSTOMER,
        date="2026-09-16",
        lines=[sales_line("999.00", "Mobilization")],
    )
    receipt = document_payload(
        "5403",
        customer=ELM_CUSTOMER,
        date="2026-09-17",
        lines=[sales_line("250.00", "Mobilization")],
    )
    memo = document_payload(
        "5404",
        customer=ELM_CUSTOMER,
        date="2026-09-18",
        lines=[sales_line("100.00", "#1 Mobilization")],
    )
    apply_payloads(
        t.engine,
        t.id,
        [
            ("Invoice", dep),
            ("Invoice", mixed),
            ("Invoice", gone),
            ("SalesReceipt", receipt),
            ("CreditMemo", memo),
        ],
    )
    apply_payloads(t.engine, t.id, [("Invoice", voided_payload(gone))])
    job = t.job(job["id"])
    by = {(ln["doc_number"] or ln["external_id"], ln["line_no"]): ln for ln in _lines(job)}
    assert by[("EST6115758_DEP", 1)]["offered"] is False
    assert "deposit" in by[("EST6115758_DEP", 1)]["not_offered"]
    assert (
        by[("5401", 1)]["offered"] is True
        and by[("5401", 1)]["suggested_label"] == "#1 Mobilization"
    )
    assert (
        by[("5401", 2)]["offered"] is False and "Fuel surcharge" in by[("5401", 2)]["not_offered"]
    )
    assert by[("5401", 3)]["offered"] is False and by[("5401", 3)]["not_offered"].startswith(
        "Not a priced"
    )
    assert by[("5402", 1)]["offered"] is False and by[("5402", 1)]["state_label"] == "Voided"
    assert by[("5402", 1)]["amount"] == "0.00"
    # A sales receipt line is offered like an invoice line (the owner's answer 2); a credit
    # memo's "#1" line is tied by its number and counts negative.
    assert by[("5403", 1)]["offered"] is True and by[("5403", 1)]["kind_label"] == "Sales receipt"
    assert by[("5404", 1)]["how"] == "number" and by[("5404", 1)]["amount"] == "-100.00"
    assert _areas(job)[1]["billed_to_date"] == "-100.00"
    assert job["billing"]["billed_to_date"] == str(
        D("149800.00") + D("73795.35") - D("3514.54") + D("250.00") - D("100.00")
    )
    assert by[("5401", 3)]["amount"] == "10.00"  # listed; a discount is never tied
    assert job["invoice_lines"]["not_assigned_to_work_area"] == str(
        D(job["billing"]["billed_to_date"]) + D("100.00")
    )
    # A PUT on a line that is not offered is refused in one sentence and writes nothing.
    for key in (("EST6115758_DEP", 1), ("5401", 2), ("5401", 3), ("5402", 1)):
        r = t.client.put(
            f"/api/jobs/{job['id']}/invoice-lines/{by[key]['billing_line_id']}/work-area",
            json={"estimate_work_area_id": _choice(job, 1)},
            headers=CSRF,
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"].count(". ") == 0 or r.json()["detail"].endswith(".")
    assert _rows(t) == 0
    # The deposit sits in "Not assigned" by construction; nothing else moves.
    assert by[("EST6115758_DEP", 1)]["suggested_work_area_id"] is None


def test_assign_reassign_clear_and_the_refusals(t: Tenant) -> None:
    job = _elm(t)
    _pmt2(t)
    job = t.job(job["id"])
    ln = _lines(job)[0]
    since = t.audit_rows()
    job = _assign(
        t, job["id"], ln["billing_line_id"], _choice(job, 2), note="keyed on the wrong line"
    )
    assert _areas(job)[2]["billed_to_date"] == "5500.00"
    job = _assign(t, job["id"], ln["billing_line_id"], _choice(job, 1))
    assert (
        _areas(job)[2]["billed_to_date"] == "0.00" and _areas(job)[1]["billed_to_date"] == "5500.00"
    )
    _assign(t, job["id"], ln["billing_line_id"], _choice(job, 1), status=409)
    job = t.send("DELETE", f"/api/jobs/{job['id']}/invoice-lines/{ln['billing_line_id']}/work-area")
    assert _areas(job)[1]["billed_to_date"] == "0.00"
    assert (
        _lines(job)[0]["how"] == "none" and _lines(job)[0]["suggested_label"] == "#1 Mobilization"
    )
    t.send(
        "DELETE",
        f"/api/jobs/{job['id']}/invoice-lines/{ln['billing_line_id']}/work-area",
        status=409,
    )
    actions = [a.action for a in t.audit_actions(since)]
    assert actions == ["billing_line_assigned", "billing_line_reassigned", "billing_line_cleared"]
    reassigned = t.audit_actions(since)[1].detail
    assert (
        reassigned["before"]["work_area"]["order_no"],
        reassigned["after"]["work_area"]["order_no"],
    ) == (2, 1)
    assert t.audit_actions(since)[0].detail["after"]["note"] == "keyed on the wrong line"
    assert _rows(t) == 3  # every event is a row; nothing was updated
    # An omitted work area, an unknown one, an unknown line, a line of another job.
    omitted = _areas(job)[17]["id"]
    _assign(t, job["id"], ln["billing_line_id"], omitted, status=422)
    _assign(t, job["id"], ln["billing_line_id"], str(uuid.uuid4()), status=404)
    _assign(t, job["id"], str(uuid.uuid4()), _choice(job, 1), status=404)
    other = t.new_job(TURLEY_ID)
    _assign(t, other["id"], ln["billing_line_id"], _choice(job, 1), status=404)
    # The pairs route refuses a bad pair whole: a line named twice, a tied line.
    pairs = _suggested_pairs(job)
    r = t.client.post(
        f"/api/jobs/{job['id']}/invoice-lines/assign-suggested",
        json={"assignments": pairs + pairs[:1]},
        headers=CSRF,
    )
    assert r.status_code == 422 and _rows(t) == 3
    job = _assign(t, job["id"], ln["billing_line_id"], _choice(job, 1))
    r = t.client.post(
        f"/api/jobs/{job['id']}/invoice-lines/assign-suggested",
        json={
            "assignments": [
                {"billing_line_id": ln["billing_line_id"], "estimate_work_area_id": _choice(job, 2)}
            ]
        },
        headers=CSRF,
    )
    assert r.status_code == 409 and _rows(t) == 4


def test_roles_and_isolation(t: Tenant, seed: Seed, as_role, login_as) -> None:
    job = _elm(t)
    _pmt2(t)
    job = t.job(job["id"])
    ln = _lines(job)[0]["billing_line_id"]
    path = f"/api/jobs/{job['id']}/invoice-lines"
    for role_name, role in (("client_pm", Role.client_pm), ("client_viewer", Role.client_viewer)):
        c = as_role(t, role_name, role)
        assert c.get(path).status_code == 200
        assert c.get(f"/api/jobs/{job['id']}").json()["invoice_lines"]["suggested"] == 4
        assert (
            c.put(
                f"{path}/{ln}/work-area",
                json={"estimate_work_area_id": _choice(job, 1)},
                headers=CSRF,
            ).status_code
            == 403
        )
        assert c.delete(f"{path}/{ln}/work-area", headers=CSRF).status_code == 403
        assert (
            c.post(f"{path}/assign-suggested", json={"assignments": []}, headers=CSRF).status_code
            == 403
        )
    other = login_as("firm_admin", tenant=seed.tenant_b)
    assert other.get(path).status_code == 404
    assert (
        other.put(
            f"{path}/{ln}/work-area", json={"estimate_work_area_id": _choice(job, 1)}, headers=CSRF
        ).status_code
        == 404
    )
    _confirm_all(t, job)
    with tenant_session(t.engine, seed.tenant_b) as s:
        assert s.execute(select(func.count()).select_from(BillingLineWorkArea)).scalar_one() == 0
    assert _rows(t) == 4


# --- the owner's answer 5: a later version ------------------------------------------------


def _version(t: Tenant, *, name18: str | None = None, drop: int | None = None) -> None:
    rows = fixture_rows(ELM)
    header = D("519173.72")
    areas = []
    for r in rows["Work areas"]:
        if r["order"] == drop:
            header -= D(str(r["price"]))
            continue
        if r["order"] == 18 and name18 is not None:
            r["name"] = name18
        areas.append(r)
    rows["Work areas"] = areas
    rows["Estimate costs"] = [c for c in rows["Estimate costs"] if c["order"] != drop]
    rows["Estimates"][0]["price"] = str(header)
    upload_template(t.client, build_workbook(sheets=rows), f"elm_v{uuid.uuid4().hex[:4]}.xlsx")
    run_until_quiet(t.engine)


def test_an_assignment_follows_the_number_through_a_rename_and_says_so(t: Tenant) -> None:
    job = _elm(t)
    _ledge(t)
    job = t.job(job["id"])
    ledge = [ln for ln in _lines(job) if ln["description"] == "Ledge Removal"]
    job = _assign(t, job["id"], ledge[0]["billing_line_id"], _choice(job, 18))
    job = _assign(t, job["id"], ledge[-1]["billing_line_id"], _choice(job, 29))
    _version(t, name18="CO: Ledge Removal day one")
    job = t.job(job["id"])
    first = next(ln for ln in _lines(job) if ln["billing_line_id"] == ledge[0]["billing_line_id"])
    assert (
        first["how"] == "assigned" and first["work_area_label"] == "#18 CO: Ledge Removal day one"
    )
    assert first["note"] == (
        'Work area #18 was "CO: Ledge Removal per Day (07/07/26)" in the baseline and is now '
        '"CO: Ledge Removal day one". Work areas keep their numbers; check the order column '
        "and upload again."
    )
    assert _areas(job)[18]["billed_to_date"] == "5475.00"
    # A version that drops #29: the line counts as not assigned and says so; no row written.
    rows = _rows(t)
    _version(t, drop=29)
    job = t.job(job["id"])
    last = next(ln for ln in _lines(job) if ln["billing_line_id"] == ledge[-1]["billing_line_id"])
    assert last["how"] == "none" and last["work_area_id"] is None and last["offered"]
    assert last["note"] == (
        'Assigned to work area #29 "CO: Ledge Removal per Day (09/23/26)", which is not on the '
        "latest version of estimate EST6115758; assign the line again or clear it."
    )
    assert 29 not in _areas(job)
    assert job["invoice_lines"]["not_assigned_to_work_area"] == str(D("49275.00") - D("5475.00"))
    assert _rows(t) == rows
    # Clearing it is allowed and writes a cleared row.
    job = t.send(
        "DELETE", f"/api/jobs/{job['id']}/invoice-lines/{last['billing_line_id']}/work-area"
    )
    assert _rows(t) == rows + 1


# --- criterion 10: money as strings; nothing but assignment rows stored --------------------


def test_money_is_strings_and_only_assignment_rows_are_written(
    t: Tenant, rw_engine: Engine
) -> None:
    from sqlalchemy import inspect

    from app.tenancy.models import Base

    job = _elm(t)
    _pmt2(t)
    _ledge(t)
    job = t.job(job["id"])

    def counts() -> dict[str, int]:
        with tenant_session(rw_engine, t.id) as s:
            return {
                name: s.execute(select(func.count()).select_from(table)).scalar_one()
                for name, table in Base.metadata.tables.items()
                if "tenant_id" in table.c
            }

    before = counts()
    t.job(job["id"])
    t.get(f"/api/jobs/{job['id']}/invoice-lines")
    t.get("/api/jobs")
    t.get("/api/home")
    assert counts() == before  # a read stores nothing
    after = _confirm_all(t, job)
    changed = {k: v - before[k] for k, v in counts().items() if v != before[k]}
    assert changed == {"billing_line_work_area": 4, "audit_log": 4}
    problems: list[str] = []
    _walk(after, "detail", problems)
    assert problems == []
    for path, value in money_values(after):
        assert value is None or isinstance(value, str), path
    assert inspect(rw_engine).get_columns("job") and not any(
        c["name"] == "retainage_pct" for c in inspect(rw_engine).get_columns("job")
    )
