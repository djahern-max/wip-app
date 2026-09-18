"""Postgres-backed worker queue (F03, D-19; BLUEPRINT §12). No Redis, no queue
library. ``task`` is a tenant table; the worker polls one tenant at a time and
every task function takes ``tenant_id`` first. Nothing in this package may
reference the request principal (static test in ``tests/test_hygiene.py``)."""
