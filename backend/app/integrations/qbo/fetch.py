"""Reads with a shape (F05): one page of an entity by keyset, the two singletons,
a count, and one Change Data Capture request. Nothing here stores anything.

F05.3 (D-48): a transactional entity is paged and counted from the company's history
start, ``TxnDate >= 'YYYY-MM-DD'``, which the caller passes in (read once per run, never
here). The grammar was accepted by QuickBooks on the real company on 2026-10-15 for all
ten transactional entities, with the count equal to the page total each time. CDC and
its overflow fallback are not filtered: a pre-start record that arrives by poll is
stored raw and skipped by its normalizer."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from app.integrations.qbo.entities import NAME_LISTS, SINGLETONS, TRANSACTIONAL
from app.integrations.qbo.reader import CompanyReader

# MAXRESULTS caps at 1,000 (query) and CDC returns at most 1,000 objects per entity.
# https://developer.intuit.com/app/developer/qbo/docs/learn/explore-the-quickbooks-online-api/data-queries
PAGE_SIZE = 1000
CDC_MAX_OBJECTS = 1000
DELETED = "Deleted"


def _active_clause(entity: str) -> str:
    return "Active IN (true, false) AND " if entity in NAME_LISTS else ""


def _txn_date_clause(entity: str, history_start: date) -> str:
    """D-48: transactional entities from the history start; nothing else is dated."""
    return f"TxnDate >= '{history_start.isoformat()}' AND " if entity in TRANSACTIONAL else ""


def page_after(
    reader: CompanyReader, entity: str, after_id: str, *, history_start: date
) -> list[dict]:
    """The next page by Id (compared as a number by QuickBooks, S-01 extra 3): a
    delete during the backfill cannot shift a record past a page boundary."""
    statement = (
        f"SELECT * FROM {entity} WHERE {_active_clause(entity)}"
        f"{_txn_date_clause(entity, history_start)}Id > '{after_id}' "
        f"ORDERBY Id MAXRESULTS {PAGE_SIZE}"
    )
    rows = reader.query(statement, operation=entity).get(entity, [])
    return [r for r in rows if isinstance(r, dict)]


def singleton(reader: CompanyReader, entity: str, realm_id: str) -> list[dict]:
    if entity == "CompanyInfo":
        body = reader.get(f"companyinfo/{realm_id}", operation=entity)
        info = body.get("CompanyInfo") if isinstance(body, dict) else None
        return [info] if isinstance(info, dict) else []
    rows = reader.query(f"SELECT * FROM {entity}", operation=entity).get(entity, [])
    return [r for r in rows if isinstance(r, dict)]


def count(reader: CompanyReader, entity: str, *, history_start: date) -> int | None:
    """QuickBooks' own count of what the copy should hold: a name list with its
    inactive rows, a transactional entity from the history start."""
    if entity in SINGLETONS:
        return None
    clauses = _active_clause(entity) + _txn_date_clause(entity, history_start)
    where = f" WHERE {clauses.removesuffix(' AND ')}" if clauses else ""
    body = reader.query(f"SELECT COUNT(*) FROM {entity}{where}", operation=f"{entity} count")
    total = body.get("totalCount")
    return total if isinstance(total, int) and not isinstance(total, bool) else None


def changed_since_param(moment: datetime) -> str:
    """CDC accepts an ISO-8601 instant with an offset (S-01)."""
    return moment.astimezone(UTC).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class CdcResult:
    changed: dict[str, list[dict]]  # entity → full payloads
    deleted: dict[str, list[dict]]  # entity → stubs (status = Deleted)
    overflowed: tuple[str, ...]  # entities that hit the 1,000-object cap


def cdc(reader: CompanyReader, entities: tuple[str, ...], since: datetime) -> CdcResult:
    body: Any = reader.get(
        "cdc",
        {"entities": ",".join(entities), "changedSince": changed_since_param(since)},
        operation="CDC",
    )
    changed: dict[str, list[dict]] = {e: [] for e in entities}
    deleted: dict[str, list[dict]] = {e: [] for e in entities}
    overflowed: list[str] = []
    blocks = body.get("CDCResponse", []) if isinstance(body, dict) else []
    for block in blocks:
        for response in block.get("QueryResponse", []) if isinstance(block, dict) else []:
            if not isinstance(response, dict):
                continue
            for entity in entities:
                rows = response.get(entity)
                if not isinstance(rows, list):
                    continue
                if len(rows) >= CDC_MAX_OBJECTS:
                    overflowed.append(entity)
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    (deleted if row.get("status") == DELETED else changed)[entity].append(row)
    return CdcResult(changed, deleted, tuple(overflowed))


def changed_after(reader: CompanyReader, entity: str, since: datetime) -> list[dict]:
    """Every record changed since ``since`` by keyset query: the fallback when CDC
    overflows for an entity. Deletes are not visible here; the nightly count catches
    them."""
    rows: list[dict] = []
    after = "0"
    stamp = changed_since_param(since)
    while True:
        statement = (
            f"SELECT * FROM {entity} WHERE {_active_clause(entity)}"
            f"MetaData.LastUpdatedTime >= '{stamp}' AND Id > '{after}' "
            f"ORDERBY Id MAXRESULTS {PAGE_SIZE}"
        )
        page = reader.query(statement, operation=entity).get(entity, [])
        page = [r for r in page if isinstance(r, dict)]
        rows += page
        if len(page) < PAGE_SIZE:
            return rows
        after = str(page[-1].get("Id"))
