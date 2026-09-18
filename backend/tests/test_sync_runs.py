"""``sync_run`` bookkeeping (F03 close-out): the cursor to resume from is the latest
*successful* run's, a later failed run never advances it, and cursor timestamps are
ISO-8601 UTC strings."""

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import Engine

from app.core.db import tenant_session
from app.ingest.connections import get_or_create_connection
from app.ingest.sync_runs import (
    finish_sync_run,
    iso_utc,
    latest_successful_cursor,
    start_sync_run,
)
from tests.conftest import Seed


def test_iso_utc_formats_aware_datetimes_in_utc_and_refuses_naive() -> None:
    eastern = timezone(timedelta(hours=-4))
    assert iso_utc(datetime(2026, 9, 17, 10, 3, 9, 500, tzinfo=eastern)) == "2026-09-17T14:03:09Z"
    assert iso_utc(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="timezone-aware"):
        iso_utc(datetime(2026, 9, 17, 10, 0, 0))


def test_latest_successful_cursor_ignores_later_failed_runs(seed: Seed, rw_engine: Engine) -> None:
    with tenant_session(rw_engine, seed.tenant_a) as s:
        conn = get_or_create_connection(s, seed.tenant_a, "qbo")
        cid = conn.id
        assert latest_successful_cursor(s, cid, "cdc") is None
        r1 = start_sync_run(s, seed.tenant_a, cid, "cdc")
        finish_sync_run(
            s, r1, outcome="succeeded", cursor={"changed_since": "2026-09-01T00:00:00Z"}
        )
        r2 = start_sync_run(s, seed.tenant_a, cid, "cdc")
        finish_sync_run(
            s,
            r2,
            outcome="failed",
            cursor={"changed_since": "2026-09-17T00:00:00Z"},
            error="rate limited",
        )
        assert latest_successful_cursor(s, cid, "cdc") == {"changed_since": "2026-09-01T00:00:00Z"}
        # Another kind and another connection have their own cursors.
        assert latest_successful_cursor(s, cid, "backfill") is None
        other = get_or_create_connection(s, seed.tenant_a, "file")
        assert latest_successful_cursor(s, other.id, "cdc") is None
        r3 = start_sync_run(s, seed.tenant_a, cid, "cdc")
        finish_sync_run(
            s, r3, outcome="succeeded", cursor={"changed_since": "2026-09-18T00:00:00Z"}
        )
        assert latest_successful_cursor(s, cid, "cdc") == {"changed_since": "2026-09-18T00:00:00Z"}
        # A successful run with no cursor (nothing to resume from) is what it says.
        r4 = start_sync_run(s, seed.tenant_a, cid, "cdc")
        finish_sync_run(s, r4, outcome="succeeded", cursor=None)
        assert latest_successful_cursor(s, cid, "cdc") is None


def test_cursor_refuses_datetime_objects_and_bad_outcomes(seed: Seed, rw_engine: Engine) -> None:
    with tenant_session(rw_engine, seed.tenant_a) as s:
        conn = get_or_create_connection(s, seed.tenant_a, "qbo")
        run = start_sync_run(s, seed.tenant_a, conn.id, "cdc")
        with pytest.raises(TypeError, match=r"\$\.changed_since must be an ISO-8601 UTC string"):
            finish_sync_run(
                s, run, outcome="succeeded", cursor={"changed_since": datetime.now(UTC)}
            )
        with pytest.raises(TypeError, match=r"\$\.entities\[1\]\.since"):
            finish_sync_run(
                s,
                run,
                outcome="succeeded",
                cursor={
                    "entities": [{"since": "2026-09-01T00:00:00Z"}, {"since": datetime.now(UTC)}]
                },
            )
        with pytest.raises(ValueError, match="outcome"):
            finish_sync_run(s, run, outcome="done")
        finish_sync_run(
            s, run, outcome="succeeded", cursor={"changed_since": iso_utc(datetime.now(UTC))}
        )
        assert run.cursor["changed_since"].endswith("Z")


def test_sync_runs_are_tenant_scoped(seed: Seed, rw_engine: Engine) -> None:
    with tenant_session(rw_engine, seed.tenant_a) as s:
        conn = get_or_create_connection(s, seed.tenant_a, "qbo")
        run = start_sync_run(s, seed.tenant_a, conn.id, f"probe-{uuid.uuid4().hex[:6]}")
        finish_sync_run(s, run, outcome="succeeded", cursor={"n": 1})
        cid, kind = conn.id, run.kind
    with tenant_session(rw_engine, seed.tenant_b) as s:
        assert latest_successful_cursor(s, cid, kind) is None
