"""Pure functions from a QuickBooks payload (as stored in ``raw_record``) to
canonical row values (F05). No database, no I/O, no clock. A payload that cannot
be read raises ``Unreadable`` with a short reason; the caller counts it and moves
on (importers never fail a whole batch for one bad row)."""


class Unreadable(ValueError):
    """``reason`` is a short code, never a value from the payload."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
