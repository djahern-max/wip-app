"""``scripts/collected_tieout.py`` (F08.2 item 1): read-only, prints no customer name,
finds the month a credit applied through a payment throws off and says by how much;
``scripts/seed_board_load.py`` (item 2): refuses what it must and seeds what it says."""

import sys
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select

from app.core.db import tenant_session
from app.domain.billing.models import Billing, Payment
from app.tenancy.models import Tenant as TenantRow
from tests.billing_helpers import apply_payloads, document_payload, line, payment_payload
from tests.conftest import Seed
from tests.job_helpers import CUSTOMERS, Tenant, make_tenant

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import collected_tieout  # noqa: E402
import seed_board_load  # noqa: E402

UNTRACKED = "206"  # a sub-customer nobody picked (D-37)
WORK_ITEM = "903"


@pytest.fixture
def t(seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant) -> Tenant:
    return make_tenant(seed, rw_engine, login_as, fresh_tenant, load=False)


def _writes_during(fn) -> tuple[object, list[str]]:
    writes: list[str] = []

    def on_exec(conn, cursor, statement, parameters, context, executemany):
        head = statement.lstrip()[:6].upper()
        if head.startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement[:60])

    event.listen(Engine, "before_cursor_execute", on_exec)
    try:
        out = fn()
    finally:
        event.remove(Engine, "before_cursor_execute", on_exec)
    return out, writes


def test_finds_the_month_a_credit_through_a_payment_throws_off_and_names_nobody(
    t: Tenant, rw_engine: Engine, capsys
) -> None:
    # A zero payment that links an invoice and a journal-entry credit (the shape of
    # QuickBooks' "link credits to charges" payments, with a JournalEntry instead of a
    # CreditMemo): the copy holds no journal entry, the line is loose and positive.
    apply_payloads(
        rw_engine,
        t.id,
        [
            (
                "Invoice",
                document_payload(
                    "9001", customer=UNTRACKED, date="2026-09-10", lines=[line("100.00", WORK_ITEM)]
                ),
            ),
            (
                "Payment",
                payment_payload(
                    "9101",
                    customer=UNTRACKED,
                    date="2026-09-12",
                    total="0",
                    applied=[("100.00", "Invoice", "9001"), ("100.00", "JournalEntry", "JE7")],
                ),
            ),
        ],
    )
    (text, writes) = _writes_during(lambda: collected_tieout.run(rw_engine, t.id, None))
    assert writes == []
    assert text.startswith("1 of 2 months do not tie on the collected side")  # 2026-08 seed row
    assert "; 1 reported" in text.splitlines()[0]
    assert (
        "2026-09  board 0.00 + 200.00 = 200.00 (of which unapplied 0.00); "
        "QuickBooks 0.00; difference 200.00"
    ) in text
    assert (
        "payment 9101  2026-09-12  total 0.00  unapplied 0.00  applications 200.00  "
        "difference 200.00  lines: Invoice, JournalEntry"
    ) in text
    assert "payment 9101 line 2  JournalEntry JE7  100.00  (document not held)" in text
    assert "twice the month's non-CreditMemo credit lines: 200.00 = the difference" in text
    assert "JournalEntry: 1 lines, 100.00, twice 200.00" in text
    assert "In 1 of 1 months that do not tie, the difference equals twice the sum" in text
    for _ext, name, _parent, _project, _active in CUSTOMERS.values():
        assert name not in text, name
    # Limited to a month that ties: nothing is off there, and the sentence says so.
    only = collected_tieout.run(rw_engine, t.id, ["2026-08"])
    assert (
        only.startswith("1 of 2 months do not tie on the collected side") and "; 1 reported" in only
    )
    assert (
        "2026-08  board 0.00 + 0.00 = 0.00" in only and "In 0 of 0 months that do not tie" in only
    )
    # The command line: the slug, --counts, and an unknown slug.
    with tenant_session(rw_engine, t.id) as s:
        slug = s.execute(select(TenantRow.slug).where(TenantRow.id == t.id)).scalar_one()
    assert collected_tieout.main(["--tenant", slug]) == 0
    assert capsys.readouterr().out.strip() == text
    assert collected_tieout.main(["--tenant", slug, "--counts"]) == 0
    out = capsys.readouterr().out
    assert "documents: 2\n" in out and "payments: 1\n" in out and "payment applications: 2\n" in out
    assert collected_tieout.main(["--tenant", f"no-such-{uuid.uuid4().hex[:6]}"]) == 2


def test_a_tenant_that_ties_says_so(t: Tenant, rw_engine: Engine) -> None:
    assert collected_tieout.run(rw_engine, t.id, None) == (
        "Every month ties on the collected side (1 months)."
    )


def test_seed_board_load_refuses_and_seeds(rw_engine: Engine, seed: Seed, monkeypatch) -> None:
    assert seed_board_load.check_database_name("postgresql+psycopg://u:p@h/wip_test") == "wip_test"
    with pytest.raises(seed_board_load.Refused):
        seed_board_load.check_database_name("postgresql+psycopg://u:p@h/wip_prod")
    monkeypatch.setattr(seed_board_load, "protected_tenant_slug_set", lambda _s: {"rye-beach"})
    with pytest.raises(seed_board_load.Refused):
        seed_board_load.seed(rw_engine, "rye-beach", 1)
    slug = f"load-test-{uuid.uuid4().hex[:6]}"
    first = seed_board_load.seed(rw_engine, slug, 7, firm_id=seed.firm_id)
    again = seed_board_load.seed(rw_engine, slug, 3)  # the same tenant, more rows
    assert first.tenant_id == again.tenant_id and (first.documents, again.documents) == (7, 3)
    with tenant_session(rw_engine, first.tenant_id) as s:
        assert len(list(s.execute(select(Billing)).scalars())) == 10
        assert len(list(s.execute(select(Payment)).scalars())) == 10
        months = {b.txn_date.strftime("%Y-%m") for b in s.execute(select(Billing)).scalars()}
    assert months == {"2001-01", "2001-02", "2001-03", "2001-04", "2001-05", "2001-06", "2001-07"}
