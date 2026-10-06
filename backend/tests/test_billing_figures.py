"""F08: the pure billing figures (``app/domain/billing/figures.py``) on the brief's
constructed cases: the D-39 surcharge invoice, the credit memo with a surcharge line,
the two-part deposit rule (owner's answer 2), 67 Elm Street's unapplied payment (D-02),
voided and deleted documents, remaining to bill, the month sums the tie-out uses, and
the undecided policy (no figure assumed)."""

from datetime import date
from decimal import Decimal

from app.domain.billing.figures import (
    ZERO,
    AppIn,
    BillingPolicy,
    DocIn,
    LineIn,
    PaymentIn,
    document_figures,
    job_figures,
    months_billed,
    months_collected,
    totals,
    words,
)

D = Decimal
TODAY = date(2026, 10, 6)
POLICY = BillingPolicy(deposit_items=frozenset({"DEP"}), surcharge_items=frozenset({"FUEL"}))
UNDECIDED = BillingPolicy(deposit_items=None, surcharge_items=None)
ELM = frozenset({"EST6115758"})


def sales(amount: str, item: str) -> LineIn:
    return LineIn("SalesItemLineDetail", item, D(amount))


def doc(
    ident: str,
    kind: str = "invoice",
    *,
    number: str | None = None,
    on: str = "2026-08-21",
    total: str,
    tax: str = "0.00",
    balance: str | None = None,
    lines: tuple[LineIn, ...] = (),
    voided: bool = False,
    deleted: bool = False,
) -> DocIn:
    return DocIn(
        ident,
        kind,
        ident,
        number,
        date.fromisoformat(on),
        D(total),
        D(tax),
        D(balance if balance is not None else total),
        voided,
        deleted,
        lines,
    )


def app(
    payment: str, billing: str | None, amount: str, *, on: str = "2026-09-01", kind: str = "Invoice"
) -> AppIn:
    return AppIn(
        payment,
        "payment",
        payment,
        date.fromisoformat(on),
        D(amount),
        False,
        kind,
        billing,
        D(amount),
    )


def test_the_d39_invoice_billed_to_date_leaves_out_the_surcharge_line() -> None:
    invoice = doc(
        "i1",
        total="73805.35",
        balance="0.00",
        lines=(sales("70290.81", "WORK"), sales("3514.54", "FUEL")),
    )
    paid = app("p1", "i1", "73805.35")
    f = job_figures([invoice], [], [paid], POLICY, revenue_method="fixed_price", today=TODAY)
    assert f.billed_to_date == D("70290.81")
    assert f.fuel_surcharge_billed == D("3514.54")
    assert f.collected_to_date == D("73805.35")
    assert f.open_ar == ZERO
    assert f.deposit_invoiced == ZERO and f.deposit_received == ZERO
    assert f.remaining_to_bill is None  # no revised contract given
    # The same line on an item that is not a surcharge item counts in billed to date.
    plain = doc("i2", total="73805.35", lines=(sales("70290.81", "WORK"), sales("3514.54", "WORK")))
    g = job_figures([plain], [], [], POLICY, today=TODAY)
    assert g.billed_to_date == D("73805.35") and g.fuel_surcharge_billed == ZERO


def test_a_credit_memo_with_a_surcharge_line_lowers_only_the_surcharge_by_it() -> None:
    invoice = doc("i1", total="1000.00", lines=(sales("900.00", "WORK"), sales("100.00", "FUEL")))
    memo = doc(
        "c1",
        "credit_memo",
        total="350.00",
        lines=(sales("250.00", "WORK"), sales("100.00", "FUEL")),
    )
    f = job_figures([invoice, memo], [], [], POLICY, today=TODAY)
    assert f.billed_to_date == D("650.00")  # 900 − 250: the memo's other lines only
    assert f.fuel_surcharge_billed == ZERO  # 100 − 100
    assert f.open_ar == D("650.00")  # 1000 − 350, signed balances
    # newest first; on the same day by id, descending ("i1" before "c1")
    assert [d.billed for d in f.documents] == [D("900.00"), D("-250.00")]


def test_sales_tax_is_out_of_billed_to_date_and_in_collected_and_the_tie_out_sum() -> None:
    invoice = doc(
        "i1", total="1070.00", tax="70.00", balance="0.00", lines=(sales("1000.00", "WORK"),)
    )
    f = job_figures([invoice], [], [app("p1", "i1", "1070.00")], POLICY, today=TODAY)
    assert f.billed_to_date == D("1000.00") and f.tax_billed == D("70.00")
    assert f.collected_to_date == D("1070.00")
    assert f.billed_by_month == {"2026-08": D("1070.00")}  # billed + surcharge + tax


def test_the_deposit_needs_both_the_dep_number_and_the_deposit_item() -> None:
    deposit = doc(
        "d1",
        number="EST6115758_DEP",
        on="2026-06-30",
        total="149800.00",
        lines=(sales("149800.00", "DEP"),),
    )
    f = job_figures(
        [deposit],
        [],
        [app("p1", "d1", "149800.00", on="2026-07-01")],
        POLICY,
        estimate_numbers=ELM,
        today=TODAY,
    )
    assert f.deposit_invoiced == D("149800.00") and f.deposit_received == D("149800.00")
    assert f.billed_to_date == D("149800.00") and f.deposit_mismatches == ()
    assert f.documents[0].is_deposit
    # A surcharge line on the deposit invoice does not stop it being the deposit (D-39).
    with_fuel = doc(
        "d2",
        number="EST6115758_DEP",
        total="150000.00",
        lines=(sales("149800.00", "DEP"), sales("200.00", "FUEL")),
    )
    g = job_figures([with_fuel], [], [], POLICY, estimate_numbers=ELM, today=TODAY)
    assert g.deposit_invoiced == D("149800.00") and g.fuel_surcharge_billed == D("200.00")

    # One part without the other: not a deposit, still billed, reported once per document.
    number_only = doc(
        "n1", number="EST6115758_DEP", total="100.00", lines=(sales("100.00", "WORK"),)
    )
    item_only = doc("n2", number="EST6115758_PMT1", total="200.00", lines=(sales("200.00", "DEP"),))
    other_job = doc("n3", number="EST9999999_DEP", total="300.00", lines=(sales("300.00", "DEP"),))
    memo = doc(
        "n4", "credit_memo", number="EST6115758_DEP", total="50.00", lines=(sales("50.00", "DEP"),)
    )
    h = job_figures(
        [number_only, item_only, other_job, memo], [], [], POLICY, estimate_numbers=ELM, today=TODAY
    )
    assert h.deposit_invoiced == ZERO and h.billed_to_date == D("550.00")
    reasons = {d.doc.id: d.deposit_reason for d in h.deposit_mismatches}
    assert reasons == {
        "n1": "its number ends in _DEP but its lines are not all on a deposit item",
        "n2": "its lines are on a deposit item but its number is not <estimate number>_DEP",
        "n3": "its number names estimate EST9999999, which is not on this job",
        "n4": "it is a credit memo, and a deposit is an invoice",
    }
    # A document with neither part says nothing.
    assert (
        document_figures(
            doc("x", total="1.00", lines=(sales("1.00", "WORK"),)), POLICY, ELM
        ).deposit_reason
        is None
    )


def test_67_elm_street_unapplied_then_applied(today=TODAY) -> None:
    """D-02's case: 149,800.00 received 2026-07-01 with nothing to apply it to; applied
    to EST6115758_PMT2 (166,294.48) on 2026-08-21, leaving 16,494.48 open."""
    received = PaymentIn(
        "p1", "payment", "p1", date(2026, 7, 1), D("149800.00"), D("149800.00"), False, ()
    )
    before = job_figures(
        [],
        [received],
        [],
        POLICY,
        estimate_numbers=ELM,
        revenue_method="fixed_price",
        revised_contract=D("519173.72"),
        today=today,
    )
    assert before.unapplied_payments == D("149800.00") and before.unapplied_count == 1
    assert before.billed_to_date == ZERO and before.collected_to_date == ZERO
    assert before.remaining_to_bill == D("519173.72")
    assert (
        before.last_payment_date == date(2026, 7, 1)
        and before.days_since_activity == (today - date(2026, 7, 1)).days
    )

    pmt2 = doc(
        "i2",
        number="EST6115758_PMT2",
        total="166294.48",
        balance="16494.48",
        lines=(sales("166294.48", "WORK"),),
    )
    applied = AppIn(
        "p1",
        "payment",
        "p1",
        date(2026, 7, 1),
        D("149800.00"),
        False,
        "Invoice",
        "i2",
        D("149800.00"),
    )
    own = PaymentIn(
        "p1", "payment", "p1", date(2026, 7, 1), D("149800.00"), ZERO, False, (applied,)
    )
    after = job_figures(
        [pmt2],
        [own],
        [applied],
        POLICY,
        estimate_numbers=ELM,
        revenue_method="fixed_price",
        revised_contract=D("519173.72"),
        today=today,
    )
    assert after.unapplied_payments == ZERO and after.unapplied_count == 0
    assert after.collected_to_date == D("149800.00")
    assert after.open_ar == D("16494.48") and after.billed_to_date == D("166294.48")
    assert after.remaining_to_bill == D("352879.24")
    assert after.days_since_activity == (today - date(2026, 8, 21)).days
    row = after.payments[0]
    assert (row.applied, row.unapplied, row.on_this_job) == (
        (("EST6115758_PMT2", D("149800.00")),),
        ZERO,
        True,
    )


def test_collected_follows_the_document_and_unapplied_follows_the_payment_row() -> None:
    """Owner's point 3 (2026-10-06): an application belongs to the job of the document it
    pays, dated by the payment; a payment's unapplied money and an application naming no
    held document belong to the payment's customer row."""
    invoice = doc("i1", total="500.00", balance="0.00", lines=(sales("500.00", "WORK"),))
    elsewhere = AppIn(
        "p9", "payment", "p9", date(2026, 9, 3), D("800.00"), False, "Invoice", "i1", D("500.00")
    )
    own_loose = AppIn(
        "p2", "payment", "p2", date(2026, 9, 5), D("60.00"), False, "Invoice", None, D("40.00")
    )
    own = PaymentIn(
        "p2", "payment", "p2", date(2026, 9, 5), D("60.00"), D("20.00"), False, (own_loose,)
    )
    f = job_figures([invoice], [own], [elsewhere], POLICY, today=TODAY)
    assert f.collected_to_date == D("540.00")
    assert f.unapplied_payments == D("20.00")
    assert f.collected_by_month == {
        "2026-09": D("560.00")
    }  # applications + unapplied, by payment month
    kinds = {r.payment_id: (r.on_this_job, r.unapplied, r.applied) for r in f.payments}
    assert kinds == {
        "p9": (False, None, (("i1", D("500.00")),)),
        "p2": (True, D("20.00"), (("Invoice", D("40.00")),)),
    }


def test_a_sales_receipt_is_billed_once_and_collected_once() -> None:
    receipt = doc(
        "s1", "sales_receipt", total="337.50", balance="0.00", lines=(sales("337.50", "WORK"),)
    )
    self_app = AppIn(
        "s1",
        "sales_receipt",
        "s1",
        date(2026, 8, 21),
        D("337.50"),
        False,
        "SalesReceipt",
        "s1",
        D("337.50"),
    )
    own = PaymentIn(
        "s1", "sales_receipt", "s1", date(2026, 8, 21), D("337.50"), ZERO, False, (self_app,)
    )
    f = job_figures([receipt], [own], [self_app], POLICY, today=TODAY)
    assert (
        f.billed_to_date == D("337.50") and f.collected_to_date == D("337.50") and f.open_ar == ZERO
    )
    assert f.billed_by_month == {"2026-08": D("337.50")} and f.collected_by_month == {
        "2026-08": D("337.50")
    }


def test_voided_and_deleted_documents_and_payments_are_in_no_figure_and_stay_marked() -> None:
    live = doc("i1", total="100.00", lines=(sales("100.00", "WORK"),))
    void = doc(
        "v1", number="EST6115758_DEP", total="0.00", voided=True, lines=(sales("0.00", "DEP"),)
    )
    gone = doc("g1", total="999.00", deleted=True, lines=(sales("999.00", "WORK"),))
    dead_pay = PaymentIn("p0", "payment", "p0", date(2026, 9, 9), D("55.00"), D("55.00"), True, ())
    dead_app = AppIn(
        "p0", "payment", "p0", date(2026, 9, 9), D("55.00"), True, "Invoice", "i1", D("55.00")
    )
    f = job_figures(
        [live, void, gone], [dead_pay], [dead_app], POLICY, estimate_numbers=ELM, today=TODAY
    )
    assert (
        f.billed_to_date,
        f.deposit_invoiced,
        f.collected_to_date,
        f.unapplied_payments,
        f.open_ar,
    ) == (
        D("100.00"),
        ZERO,
        ZERO,
        ZERO,
        D("100.00"),
    )
    assert f.deposit_mismatches == ()
    states = {d.doc.id: d.state_label for d in f.documents}
    assert states == {"i1": "", "v1": "Voided", "g1": "Deleted"}
    assert [r.deleted for r in f.payments] == [True]
    assert f.last_payment_date is None and f.last_billing_date == date(2026, 8, 21)


def test_remaining_to_bill_and_over_contract() -> None:
    invoice = doc("i1", total="600.00", lines=(sales("600.00", "WORK"),))
    fixed = job_figures(
        [invoice],
        [],
        [],
        POLICY,
        revenue_method="fixed_price",
        revised_contract=D("500.00"),
        today=TODAY,
    )
    assert fixed.remaining_to_bill == D("-100.00") and fixed.over_contract == D("100.00")
    under = job_figures(
        [invoice],
        [],
        [],
        POLICY,
        revenue_method="fixed_price",
        revised_contract=D("900.00"),
        today=TODAY,
    )
    assert under.remaining_to_bill == D("300.00") and under.over_contract is None
    for method in ("time_and_materials", "pool", "recurring_service", "none"):
        assert (
            job_figures(
                [invoice],
                [],
                [],
                POLICY,
                revenue_method=method,
                revised_contract=D("900.00"),
                today=TODAY,
            ).remaining_to_bill
            is None
        )
    assert (
        job_figures(
            [invoice],
            [],
            [],
            POLICY,
            revenue_method="fixed_price",
            revised_contract=None,
            today=TODAY,
        ).remaining_to_bill
        is None
    )


def test_an_undecided_key_leaves_the_figures_that_need_it_unset_and_the_others_whole() -> None:
    invoice = doc(
        "i1", total="1070.00", tax="70.00", balance="70.00", lines=(sales("1000.00", "WORK"),)
    )
    f = job_figures(
        [invoice],
        [],
        [app("p1", "i1", "1000.00")],
        UNDECIDED,
        revenue_method="fixed_price",
        revised_contract=D("5000.00"),
        today=TODAY,
    )
    assert (
        f.billed_to_date,
        f.fuel_surcharge_billed,
        f.deposit_invoiced,
        f.deposit_received,
        f.remaining_to_bill,
    ) == (None,) * 5
    assert (f.collected_to_date, f.open_ar, f.unapplied_payments) == (
        D("1000.00"),
        D("70.00"),
        ZERO,
    )
    assert f.deposit_mismatches == () and f.documents[0].billed is None
    assert f.billed_by_month == {"2026-08": D("1070.00")}  # the tie-out needs no policy
    half = BillingPolicy(deposit_items=frozenset({"DEP"}), surcharge_items=None)
    assert job_figures([invoice], [], [], half, today=TODAY).billed_to_date is None


def test_totals_and_month_sums_add_across_jobs() -> None:
    a = job_figures(
        [
            doc(
                "i1",
                total="100.00",
                on="2026-07-02",
                balance="0.00",
                lines=(sales("100.00", "WORK"),),
            )
        ],
        [],
        [app("p1", "i1", "100.00", on="2026-07-30")],
        POLICY,
        revenue_method="fixed_price",
        revised_contract=D("150.00"),
        today=TODAY,
    )
    b = job_figures(
        [doc("i2", total="40.00", on="2026-08-02", lines=(sales("40.00", "WORK"),))],
        [],
        [],
        POLICY,
        revenue_method="time_and_materials",
        today=TODAY,
    )
    t = totals([a, b])
    assert (t.billed_to_date, t.collected_to_date, t.open_ar, t.remaining_to_bill) == (
        D("140.00"),
        D("100.00"),
        D("40.00"),
        D("50.00"),
    )
    assert months_billed([a, b]) == {"2026-07": D("100.00"), "2026-08": D("40.00")}
    assert months_collected([a, b]) == {"2026-07": D("100.00")}
    assert totals([]).remaining_to_bill is None and totals([]).collected_to_date == ZERO


def test_money_words() -> None:
    assert words(D("1234.5")) == "1,234.50"
    assert words(D("-16494.48")) == "(16,494.48)"
    assert words(ZERO) == "0.00" and words(D("-0.00")) == "0.00"


def test_the_credit_types_are_one_constant_read_by_both_sign_functions() -> None:
    """F08.2: which payment-line types count against the invoice lines is decided in
    one place; today CreditMemo only (item 1 waits on the diagnostic). The ORM sign
    (``amounts.py``) and the pure sign (``figures.py``) read the same set."""
    import inspect

    from app.domain.billing import amounts
    from app.domain.billing.figures import CREDIT_TXN_TYPES, signed_application

    assert CREDIT_TXN_TYPES == frozenset({"CreditMemo"})
    assert "CREDIT_TXN_TYPES" in inspect.getsource(amounts.signed_application)
    assert "CREDIT_TXN_TYPES" in inspect.getsource(signed_application)
    assert '"CreditMemo"' not in inspect.getsource(amounts) + inspect.getsource(signed_application)
    app = AppIn("p", "payment", "1", TODAY, D("0"), False, "JournalEntry", None, D("100.00"))
    credit = AppIn("p", "payment", "1", TODAY, D("0"), False, "CreditMemo", None, D("100.00"))
    assert signed_application(app) == D("100.00") and signed_application(credit) == D("-100.00")
