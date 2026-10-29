"""The QuickBooks entities this platform stores raw (BLUEPRINT §6.2), and how each
is read. Everything is stored; F05 normalizes only ``NORMALIZED``. F05.3 (D-48):
``REFERENCE`` entities are pulled whole; ``TRANSACTIONAL`` entities are pulled, counted
and held from the company's history start, by ``TxnDate``."""

ENTITIES: tuple[str, ...] = (
    "Account",
    "Customer",
    "Vendor",
    "Item",
    "Invoice",
    "Payment",
    "CreditMemo",
    "SalesReceipt",
    "Deposit",
    "Bill",
    "VendorCredit",
    "Purchase",
    "JournalEntry",
    "TimeActivity",
    "Preferences",
    "CompanyInfo",
)
# Name lists hide inactive rows unless asked for them (S-01).
NAME_LISTS: frozenset[str] = frozenset({"Account", "Customer", "Vendor", "Item"})
# One record per company; no Id-based paging.
SINGLETONS: frozenset[str] = frozenset({"Preferences", "CompanyInfo"})
# Normalized in F05 (Account: id attached to gl_account by account number only).
NORMALIZED: tuple[str, ...] = ("Customer", "Invoice", "CreditMemo", "SalesReceipt", "Payment")
# F10 (D-51): the cost side, normalized to ``ledger_line`` (app/domain/cost/sync.py).
COST_NORMALIZED: tuple[str, ...] = ("Bill", "VendorCredit", "Purchase", "JournalEntry", "Deposit")
# F05.3 (D-48): reference records are held whole; transactional records from the
# history start. Every entity is one or the other.
REFERENCE: frozenset[str] = frozenset(
    {"Account", "Customer", "Vendor", "Item", "Preferences", "CompanyInfo"}
)
TRANSACTIONAL: tuple[str, ...] = tuple(e for e in ENTITIES if e not in REFERENCE)
