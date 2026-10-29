"""The two cost review sentences (F10; BLUEPRINT §10; D-46, D-51). Pure generators
returning ``Issue`` as the estimate and billing generators do; F09's run persists them.

- ``COST_UNASSIGNED`` (warn): one per document and customer reference over the unassigned
  lines (job-cost, non-labor, no job), naming the amount and the account: the subject is the
  customer row when the line names one the copy holds, else the company ("no customer").
- ``COST_DIVISION_MISMATCH`` (warn): one per document and job where a job-cost line's
  account division is not the job's.

Neither fires on a labor-slot line or on an account outside job cost (``figures.part_of``).
"""

from collections.abc import Sequence

from app.domain.billing.figures import words
from app.domain.cost.figures import DivisionMismatch, UnassignedDoc
from app.domain.cost.models import TXN_TYPE_LABELS
from app.domain.estimates.exceptions import Issue

SENTENCES: dict[str, str] = {
    # §10 COST_UNASSIGNED: an in-job-cost line with no job
    "COST_UNASSIGNED": "{document}, {amount} to {accounts}, is coded to {where}.",
    # §10 COST_DIVISION_MISMATCH: the line's account division is not the job's
    "COST_DIVISION_MISMATCH": (
        "{document} posts {amount} to {account} ({account_division}) on a {job_division} job."
    ),
}


def sentence(code: str, /, **values) -> str:
    return SENTENCES[code].format(**values)


def document_words(txn_type: str, document: str) -> str:
    return f"{TXN_TYPE_LABELS.get(txn_type, txn_type)} {document}"


def _accounts(labels: Sequence[str]) -> str:
    if len(labels) <= 1:
        return labels[0] if labels else "no account"
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def unassigned_issue(doc: UnassignedDoc, customer_name: str | None, customer_known: bool) -> Issue:
    """``customer_name``: the row's name when the copy holds it; ``customer_known`` whether
    the line names a customer at all."""
    if not customer_known:
        where = "no customer"
    elif customer_name:
        where = f'"{customer_name}", which has no job'
    else:
        where = f"QuickBooks customer {doc.customer_external_id}, which has no job"
    return Issue(
        "COST_UNASSIGNED",
        sentence(
            "COST_UNASSIGNED",
            document=document_words(doc.txn_type, doc.document),
            amount=words(doc.amount),
            accounts=_accounts(doc.account_labels),
            where=where,
        ),
        {
            "amount": str(doc.amount),
            "account_no": ", ".join(doc.account_numbers),
            "txn_type": doc.txn_type,
            "txn_external_id": doc.txn_external_id,
            "document": doc.document,
            "customer_external_id": doc.customer_external_id,
            "lines": doc.lines,
        },
    )


def division_mismatch_issue(m: DivisionMismatch) -> Issue:
    return Issue(
        "COST_DIVISION_MISMATCH",
        sentence(
            "COST_DIVISION_MISMATCH",
            document=document_words(m.txn_type, m.document),
            amount=words(m.amount),
            account=m.account_label,
            account_division=m.account_division_name,
            job_division=m.job_division_name,
        ),
        {
            "amount": str(m.amount),
            "account_no": m.account_no or "",
            "txn_type": m.txn_type,
            "txn_external_id": m.txn_external_id,
            "document": m.document,
            "job_id": m.job_id,
        },
    )
