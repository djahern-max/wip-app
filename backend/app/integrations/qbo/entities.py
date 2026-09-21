"""The QuickBooks entities this platform stores raw (BLUEPRINT §6.2), and how each
is read. Everything is stored; F05 normalizes only ``NORMALIZED``."""

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
