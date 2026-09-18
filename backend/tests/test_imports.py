"""Upload and import pipeline (F03): one batch per distinct file per tenant and
source kind, raw before normalized, versions with history, bounded failure,
limits, keys, and the audit rows that go with each action."""

import io
import os
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select, text
from sqlalchemy.exc import OperationalError

from app.audit.models import AuditLog
from app.core.config import get_settings
from app.core.db import tenant_session
from app.core.storage import LocalObjectStore
from app.ingest.imports import PROCESS_BATCH, process_batch_now, receive_upload
from app.ingest.models import ImportBatch, RawRecord
from app.ingest.raw import raw_history
from app.tenancy.models import Role
from app.worker.models import Task
from app.worker.queue import PermanentTaskError
from app.worker.runner import Worker
from tests._env import OBJECT_STORE_DIR
from tests.conftest import CSRF, Seed

STORE = Path(OBJECT_STORE_DIR)


def upload(client: TestClient, kind: str, filename: str, content: bytes, ctype: str | None = None):
    return client.post(
        "/api/imports",
        data={"source_kind": kind},
        files={"file": (filename, io.BytesIO(content), ctype or "application/octet-stream")},
        headers=CSRF,
    )


def audit_actions(engine: Engine, tenant_id: uuid.UUID, entity_id: str) -> list[str]:
    with tenant_session(engine, tenant_id) as s:
        return list(
            s.execute(
                select(AuditLog.action)
                .where(AuditLog.entity_type == "import_batch", AuditLog.entity_id == entity_id)
                .order_by(AuditLog.occurred_at, AuditLog.id)
            ).scalars()
        )


def objects_under(tenant_id: uuid.UUID) -> list[Path]:
    root = STORE / "tenant" / str(tenant_id)
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def tasks_for(engine: Engine, tenant_id: uuid.UUID, batch_id: str) -> list[Task]:
    with tenant_session(engine, tenant_id) as s:
        rows = s.execute(select(Task).where(Task.kind == PROCESS_BATCH)).scalars().all()
        out = [t for t in rows if t.payload.get("import_batch_id") == batch_id]
        for t in out:
            s.expunge(t)
        return out


def run_worker(engine: Engine) -> None:
    w = Worker(engine, name="w-imports", listen=False, poll_seconds=0.01)
    while w.run_once():
        pass


def batch_row(engine: Engine, tenant_id: uuid.UUID, batch_id: str) -> ImportBatch:
    with tenant_session(engine, tenant_id) as s:
        row = s.get(ImportBatch, uuid.UUID(batch_id))
        s.expunge(row)
        return row


# --- upload and duplicates ------------------------------------------------------------------


def test_same_file_twice_is_one_batch_and_another_tenant_gets_its_own(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    content = f"bytes-{uuid.uuid4().hex}".encode()
    c = login_as("firm_admin")
    r1 = upload(c, "unparsed_file", "report.bin", content)
    assert r1.status_code == 201, r1.text
    body = r1.json()
    assert body["duplicate"] is False
    b = body["batch"]
    assert (b["status"], b["byte_size"], b["original_filename"]) == (
        "received",
        len(content),
        "report.bin",
    )
    assert (b["status_label"], b["source_label"], b["message"], b["error_detail"]) == (
        "Received",
        "Unparsed file",
        None,
        None,
    )
    assert "object_key" not in b and "error" not in b
    assert b["uploaded_by"] == str(seed.users["firm_admin"].id)
    before = objects_under(seed.tenant_a)

    r2 = upload(c, "unparsed_file", "renamed.bin", content)
    assert r2.status_code == 200, r2.text
    assert r2.json()["duplicate"] is True and r2.json()["batch"]["id"] == b["id"]
    assert r2.json()["batch"]["original_filename"] == "report.bin"  # the first upload's name

    with tenant_session(rw_engine, seed.tenant_a) as s:
        n = (
            s.execute(
                select(ImportBatch).where(
                    ImportBatch.sha256 == b["sha256"], ImportBatch.source_kind == "unparsed_file"
                )
            )
            .scalars()
            .all()
        )
        assert (
            len(n) == 1 and n[0].object_key == f"tenant/{seed.tenant_a}/imports/{b['sha256']}.bin"
        )
    assert objects_under(seed.tenant_a) == before  # one object, unchanged by the retry
    assert len(tasks_for(rw_engine, seed.tenant_a, b["id"])) == 1
    assert audit_actions(rw_engine, seed.tenant_a, b["id"]) == [
        "import_uploaded",
        "import_duplicate",
    ]

    # The same bytes in tenant B: a separate batch and a separate object under B's prefix.
    cb = login_as("client_admin_b", tenant=seed.tenant_b)
    r3 = upload(cb, "unparsed_file", "report.bin", content)
    assert r3.status_code == 201 and r3.json()["duplicate"] is False
    assert r3.json()["batch"]["id"] != b["id"]
    assert r3.json()["batch"]["sha256"] == b["sha256"]
    b_objects = [p.relative_to(STORE).as_posix() for p in objects_under(seed.tenant_b)]
    assert f"tenant/{seed.tenant_b}/imports/{b['sha256']}.bin" in b_objects
    assert all(p.startswith(f"tenant/{seed.tenant_b}/") for p in b_objects)


def test_every_object_key_written_starts_with_the_tenant_prefix_and_stays_inside(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    c = login_as("firm_staff")
    upload(c, "unparsed_file", "x.bin", uuid.uuid4().bytes)
    with tenant_session(rw_engine, seed.tenant_a) as s:
        keys = list(s.execute(select(ImportBatch.object_key)).scalars())
    assert keys and all(k.startswith(f"tenant/{seed.tenant_a}/imports/") for k in keys)
    for p in STORE.rglob("*"):
        if p.is_file():
            rel = p.relative_to(STORE).as_posix()
            assert rel.startswith("tenant/"), rel


def test_list_get_download_and_cross_tenant_404(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    content = f"dl-{uuid.uuid4().hex}\n".encode()
    c = login_as("client_admin")
    bid = upload(c, "unparsed_file", "Rye Beach jobs.bin", content).json()["batch"]["id"]
    listed = c.get("/api/imports").json()
    assert listed[0]["id"] == bid  # newest first
    assert c.get(f"/api/imports/{bid}").json()["original_filename"] == "Rye Beach jobs.bin"
    r = c.get(f"/api/imports/{bid}/download")
    assert r.status_code == 200 and r.content == content
    assert "attachment" in r.headers["content-disposition"]
    assert audit_actions(rw_engine, seed.tenant_a, bid)[-1] == "import_downloaded"
    # Another tenant: 404 for detail and download, not 403 (RLS never returns the row).
    cb = login_as("client_admin_b", tenant=seed.tenant_b)
    assert cb.get(f"/api/imports/{bid}").status_code == 404
    assert cb.get(f"/api/imports/{bid}/download").status_code == 404
    assert bid not in {b["id"] for b in cb.get("/api/imports").json()}
    assert c.get(f"/api/imports/{uuid.uuid4()}").status_code == 404


def test_upload_requires_the_csrf_header(login_as: Callable[..., TestClient]) -> None:
    c = login_as("firm_admin")
    r = c.post(
        "/api/imports",
        data={"source_kind": "unparsed_file"},
        files={"file": ("x.bin", io.BytesIO(b"x"), "application/octet-stream")},
    )
    assert r.status_code == 403 and "X-Requested-With" in r.json()["detail"]


def test_source_kinds_are_listed(login_as: Callable[..., TestClient]) -> None:
    kinds = {k["name"]: k for k in login_as("firm_admin").get("/api/imports/source-kinds").json()}
    assert kinds["unparsed_file"]["extensions"] == []
    assert kinds["unparsed_file"]["label"] == "Unparsed file"
    assert kinds["test_csv"]["extensions"] == ["csv"] and kinds["test_csv"]["label"] == "Test CSV"


# --- limits and filenames ---------------------------------------------------------------------


def test_upload_limits_refuse_before_anything_is_stored(
    login_as: Callable[..., TestClient],
    seed: Seed,
    rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = login_as("firm_admin")
    before_objects = objects_under(seed.tenant_a)
    with tenant_session(rw_engine, seed.tenant_a) as s:
        before_rows = s.execute(select(ImportBatch.id)).scalars().all()
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "64")
    get_settings.cache_clear()
    try:
        r = upload(c, "unparsed_file", "big.bin", os.urandom(65))
        assert r.status_code == 413
        assert r.json()["detail"] == "This file is larger than 64 bytes. Upload a smaller file."
        assert upload(c, "unparsed_file", "ok.bin", os.urandom(64)).status_code == 201
    finally:
        monkeypatch.delenv("MAX_UPLOAD_BYTES")
        get_settings.cache_clear()
    r = upload(c, "test_csv", "notes.txt", b"external_id\n1\n")
    assert r.status_code == 415
    assert r.json()["detail"] == (
        "Test CSV files must end in .csv. Choose a .csv file and upload it again."
    )
    r = upload(c, "no_such_kind", "x.csv", b"x")
    assert (r.status_code, r.json()["detail"]) == (422, "Choose a source from the list.")
    # Messages never name a setting, an exception type or a machine identifier (D-22).
    for body in (r.text,):
        assert "MAX_UPLOAD_BYTES" not in body and "source kind" not in body
    with tenant_session(rw_engine, seed.tenant_a) as s:
        after_rows = s.execute(select(ImportBatch.id)).scalars().all()
    assert len(after_rows) == len(before_rows) + 1  # only ok.bin
    assert len(objects_under(seed.tenant_a)) == len(before_objects) + 1


def test_filename_with_path_separators_is_text_only(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    c = login_as("firm_admin")
    content = uuid.uuid4().bytes
    name = "../../etc/passwd\\..\\evil.csv"
    r = upload(c, "test_csv", name, b"external_id\n" + content.hex().encode() + b"\n")
    assert r.status_code == 201, r.text
    b = r.json()["batch"]
    assert b["original_filename"] == name
    with tenant_session(rw_engine, seed.tenant_a) as s:
        key = s.execute(
            select(ImportBatch.object_key).where(ImportBatch.id == uuid.UUID(b["id"]))
        ).scalar_one()
    assert key == f"tenant/{seed.tenant_a}/imports/{b['sha256']}.csv"
    assert ".." not in key and "\\" not in key and "passwd" not in key
    assert not (STORE.parent / "etc").exists()


# --- processing: versions, history, failure modes --------------------------------------------


def _csv(rows: list[tuple[str, str, str]]) -> bytes:
    return ("external_id,name,amount\n" + "".join(f"{e},{n},{a}\n" for e, n, a in rows)).encode()


def _raw_versions(engine: Engine, tenant_id: uuid.UUID, ext: str) -> list[RawRecord]:
    with tenant_session(engine, tenant_id) as s:
        rows = raw_history(s, tenant_id, "test", "row", ext)
        for r in rows:
            s.expunge(r)
        return rows


def test_modified_file_creates_new_versions_and_leaves_history(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    salt = uuid.uuid4().hex[:6]
    a, b, c_, d = (f"{k}{salt}" for k in "abcd")
    v1 = _csv([(a, "Alpha", "10.00"), (b, "Beta", "20.50"), (c_, "Gamma", "0.10")])
    v2 = _csv([(a, "Alpha", "10.00"), (b, "Beta changed", "21.00"), (d, "Delta", "4.00")])
    client = login_as("firm_admin")

    b1 = upload(client, "test_csv", "jobs.csv", v1).json()["batch"]["id"]
    run_worker(rw_engine)
    row = batch_row(rw_engine, seed.tenant_a, b1)
    assert (row.status, row.rows_loaded, row.rows_rejected, row.error) == ("loaded", 3, 0, None)
    assert row.processed_at is not None
    first_b = _raw_versions(rw_engine, seed.tenant_a, b)[0]

    b2 = upload(client, "test_csv", "jobs.csv", v2).json()["batch"]["id"]
    assert b2 != b1
    run_worker(rw_engine)
    assert batch_row(rw_engine, seed.tenant_a, b2).status == "loaded"

    assert [r.version for r in _raw_versions(rw_engine, seed.tenant_a, a)] == [1]
    b_versions = _raw_versions(rw_engine, seed.tenant_a, b)
    assert [r.version for r in b_versions] == [1, 2]
    assert b_versions[0].payload == first_b.payload
    assert b_versions[0].payload_sha256 == first_b.payload_sha256  # byte-identical to before
    assert b_versions[0].import_batch_id == uuid.UUID(b1)
    assert b_versions[1].import_batch_id == uuid.UUID(b2)
    assert b_versions[1].payload["name"] == "Beta changed"
    assert [r.version for r in _raw_versions(rw_engine, seed.tenant_a, d)] == [1]
    c_versions = _raw_versions(rw_engine, seed.tenant_a, c_)
    assert [(r.version, r.is_deleted) for r in c_versions] == [(1, False)]  # no inferred delete

    # Processing v2 a second time writes zero raw rows.
    with tenant_session(rw_engine, seed.tenant_a) as s:
        count_before = s.execute(text("SELECT count(*) FROM raw_record")).scalar_one()
    store = LocalObjectStore(OBJECT_STORE_DIR)
    process_batch_now(rw_engine, store, seed.tenant_a, uuid.UUID(b2))
    with tenant_session(rw_engine, seed.tenant_a) as s:
        assert s.execute(text("SELECT count(*) FROM raw_record")).scalar_one() == count_before
    assert batch_row(rw_engine, seed.tenant_a, b2).rows_loaded == 3


def test_parse_that_raises_fails_the_batch_and_the_task_with_no_retry(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    secret = f"SECRET-{uuid.uuid4().hex}"
    bid = upload(login_as("firm_admin"), "test_csv", "bad.csv", f"#raise {secret}\n".encode())
    bid = bid.json()["batch"]["id"]
    w = Worker(rw_engine, name="w-fail", listen=False, poll_seconds=0.01)
    assert w.run_once() >= 1
    (t,) = tasks_for(rw_engine, seed.tenant_a, bid)
    assert (t.status, t.attempts, t.last_error) == ("failed", 1, "parse failed: ValueError")
    row = batch_row(rw_engine, seed.tenant_a, bid)
    assert (row.status, row.error) == ("failed", "parse failed: ValueError")
    assert secret not in (row.error or "") and secret not in (t.last_error or "")
    assert w.run_once() == 0  # nothing left to retry
    # On screen: a sentence that says what happened and what to do next; the exception
    # type stays in error_detail for OPERATIONS.
    out = login_as("firm_admin").get(f"/api/imports/{bid}").json()
    assert out["status_label"] == "Failed"
    assert out["message"] == (
        "This file could not be read. Check that it is the right export and upload it again."
    )
    assert out["error_detail"] == "parse failed: ValueError"
    assert "ValueError" not in out["message"] and secret not in out["message"]


def test_a_failed_batch_never_goes_back_to_processing(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    bid = upload(login_as("firm_admin"), "test_csv", "bad.csv", b"#raise x\n").json()["batch"]["id"]
    run_worker(rw_engine)
    assert batch_row(rw_engine, seed.tenant_a, bid).status == "failed"
    statuses: list[str] = []

    def on_execute(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE import_batch") and isinstance(parameters, dict):
            if "status" in parameters:
                statuses.append(parameters["status"])

    event.listen(rw_engine, "before_cursor_execute", on_execute)
    try:
        # A requeued task (the OPERATIONS runbook) and a direct call both refuse.
        (t,) = tasks_for(rw_engine, seed.tenant_a, bid)
        with tenant_session(rw_engine, seed.tenant_a) as s:
            s.execute(
                text(
                    "UPDATE task SET status = 'queued', attempts = 0, run_after = now(), "
                    "last_error = NULL WHERE id = :id"
                ),
                {"id": t.id},
            )
        run_worker(rw_engine)
        (t,) = tasks_for(rw_engine, seed.tenant_a, bid)
        assert (t.status, t.attempts, t.last_error) == ("failed", 1, "batch already failed")
        with pytest.raises(PermanentTaskError, match="batch already failed"):
            process_batch_now(
                rw_engine, LocalObjectStore(OBJECT_STORE_DIR), seed.tenant_a, uuid.UUID(bid)
            )
    finally:
        event.remove(rw_engine, "before_cursor_execute", on_execute)
    assert "processing" not in statuses
    row = batch_row(rw_engine, seed.tenant_a, bid)
    assert (row.status, row.error) == ("failed", "parse failed: ValueError")


class _UnopenableStore(LocalObjectStore):
    @contextmanager
    def open(self, tenant_id, relative_key):  # noqa: ARG002
        raise OSError("spaces unreachable")
        yield  # pragma: no cover


def test_object_open_failure_retries_with_backoff_and_does_not_fail_the_batch(
    login_as: Callable[..., TestClient],
    seed: Seed,
    rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bid = upload(
        login_as("firm_admin"), "test_csv", "ok.csv", _csv([(uuid.uuid4().hex, "a", "1.00")])
    )
    bid = bid.json()["batch"]["id"]
    monkeypatch.setattr(
        "app.core.storage.build_object_store", lambda _s: _UnopenableStore(OBJECT_STORE_DIR)
    )
    w = Worker(rw_engine, name="w-open", listen=False, poll_seconds=0.01)
    assert w.run_once() >= 1
    (t,) = tasks_for(rw_engine, seed.tenant_a, bid)
    assert (t.status, t.attempts, t.last_error) == ("queued", 1, "OSError")
    assert t.run_after > datetime.now(UTC) + timedelta(seconds=20)
    row = batch_row(rw_engine, seed.tenant_a, bid)
    assert (row.status, row.error) == ("received", "OSError (will retry)")
    out = login_as("firm_admin").get(f"/api/imports/{bid}").json()
    assert (
        out["message"].startswith("Processing did not finish") and "OSError" not in out["message"]
    )
    assert out["error_detail"] == "OSError (will retry)"
    monkeypatch.undo()
    with tenant_session(rw_engine, seed.tenant_a) as s:
        s.execute(text("UPDATE task SET run_after = now() WHERE id = :id"), {"id": t.id})
    assert w.run_once() >= 1
    (t,) = tasks_for(rw_engine, seed.tenant_a, bid)
    row = batch_row(rw_engine, seed.tenant_a, bid)
    assert (t.status, t.attempts) == ("succeeded", 2)
    assert (row.status, row.rows_loaded, row.error) == ("loaded", 1, None)


def test_database_failure_while_storing_retries_and_does_not_fail_the_batch(
    login_as: Callable[..., TestClient],
    seed: Seed,
    rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bid = upload(
        login_as("firm_admin"), "test_csv", "db.csv", _csv([(uuid.uuid4().hex, "a", "1.00")])
    )
    bid = bid.json()["batch"]["id"]

    def lost_connection(*_a, **_k):
        raise OperationalError("INSERT", {}, ConnectionError("server closed the connection"))

    monkeypatch.setattr("app.ingest.imports.store_raw", lost_connection)
    w = Worker(rw_engine, name="w-db", listen=False, poll_seconds=0.01)
    assert w.run_once() >= 1
    (t,) = tasks_for(rw_engine, seed.tenant_a, bid)
    assert (t.status, t.attempts) == ("queued", 1)
    assert t.last_error.startswith("OperationalError")
    row = batch_row(rw_engine, seed.tenant_a, bid)
    assert row.status == "received" and row.error.endswith("(will retry)")
    monkeypatch.undo()
    with tenant_session(rw_engine, seed.tenant_a) as s:
        s.execute(text("UPDATE task SET run_after = now() WHERE id = :id"), {"id": t.id})
    assert w.run_once() >= 1
    assert batch_row(rw_engine, seed.tenant_a, bid).status == "loaded"
    assert tasks_for(rw_engine, seed.tenant_a, bid)[0].status == "succeeded"


def test_one_bad_item_of_five_ends_loaded_with_issues(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    salt = uuid.uuid4().hex[:6]
    lines = [f"{k}{salt},{k},1.00" for k in "abcde"]
    lines[2] = f",{salt},1.00"  # no external_id: rejected, the rest still loads
    content = ("external_id,name,amount\n" + "\n".join(lines) + "\n").encode()
    bid = upload(login_as("firm_admin"), "test_csv", "mixed.csv", content).json()["batch"]["id"]
    run_worker(rw_engine)
    row = batch_row(rw_engine, seed.tenant_a, bid)
    assert (row.status, row.rows_loaded, row.rows_rejected) == ("loaded_with_issues", 4, 1)
    (t,) = tasks_for(rw_engine, seed.tenant_a, bid)
    assert t.status == "succeeded"
    out = login_as("firm_admin").get(f"/api/imports/{bid}").json()
    assert (out["status_label"], out["message"]) == (
        "Loaded with issues",
        "1 row could not be read and was skipped; the rest were loaded.",
    )


def test_an_item_the_store_refuses_is_rejected_not_fatal(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    """An external_id longer than the column is one rejected row; the file still loads."""
    salt = uuid.uuid4().hex[:6]
    content = f"external_id,name\n{salt}-ok,fine\n{'x' * 300},too-long\n".encode()
    bid = upload(login_as("firm_admin"), "test_csv", "long.csv", content).json()["batch"]["id"]
    run_worker(rw_engine)
    row = batch_row(rw_engine, seed.tenant_a, bid)
    assert (row.status, row.rows_loaded, row.rows_rejected) == ("loaded_with_issues", 1, 1)


# --- raw before normalized -------------------------------------------------------------------


class _BrokenStore(LocalObjectStore):
    def put(self, tenant_id, relative_key, stream):  # noqa: ARG002
        raise OSError("disk full")


def test_object_write_failure_leaves_no_batch_row(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    c = login_as("firm_admin")
    app = c.app  # type: ignore[attr-defined]
    good = app.state.object_store
    app.state.object_store = _BrokenStore(OBJECT_STORE_DIR)
    content = uuid.uuid4().bytes
    with tenant_session(rw_engine, seed.tenant_a) as s:
        queued_before = s.execute(
            text("SELECT count(*) FROM task WHERE status = 'queued'")
        ).scalar_one()
    try:
        r = upload(c, "unparsed_file", "x.bin", content)
    finally:
        app.state.object_store = good
    assert r.status_code == 503
    assert r.json()["detail"] == "The file could not be stored. Try again in a minute."
    assert "disk full" not in r.text and "OSError" not in r.text
    with tenant_session(rw_engine, seed.tenant_a) as s:
        assert (
            s.execute(select(ImportBatch).where(ImportBatch.sha256 == _sha(content))).first()
            is None
        )
        queued_after = s.execute(
            text("SELECT count(*) FROM task WHERE status = 'queued'")
        ).scalar_one()
        assert queued_after == queued_before
    # Then a normal upload of the same bytes works and creates exactly one batch.
    assert upload(c, "unparsed_file", "x.bin", content).status_code == 201
    with tenant_session(rw_engine, seed.tenant_a) as s:
        rows = s.execute(select(ImportBatch).where(ImportBatch.sha256 == _sha(content))).all()
        assert len(rows) == 1


def _sha(b: bytes) -> str:
    import hashlib

    return hashlib.sha256(b).hexdigest()


def test_commit_failure_after_the_object_write_is_retried_to_one_object(
    seed: Seed, rw_engine: Engine
) -> None:
    store = LocalObjectStore(OBJECT_STORE_DIR)
    content = uuid.uuid4().bytes
    sha = _sha(content)
    common = dict(
        tenant_id=seed.tenant_a,
        source_kind="unparsed_file",
        filename="retry.bin",
        content_type=None,
        actor_user_id=seed.users["firm_admin"].id,
        actor_role=Role.firm_admin,
    )
    with pytest.raises(RuntimeError, match="commit failed"):
        with tenant_session(rw_engine, seed.tenant_a) as s:
            batch, dup = receive_upload(s, store, stream=io.BytesIO(content), **common)
            assert not dup and store.exists(seed.tenant_a, f"imports/{sha}.bin")
            raise RuntimeError("commit failed")  # the transaction rolls back
    with tenant_session(rw_engine, seed.tenant_a) as s:
        assert s.execute(select(ImportBatch).where(ImportBatch.sha256 == sha)).first() is None
    with tenant_session(rw_engine, seed.tenant_a) as s:
        batch, dup = receive_upload(s, store, stream=io.BytesIO(content), **common)
        assert not dup and batch.sha256 == sha
    objects = [p for p in objects_under(seed.tenant_a) if p.name == f"{sha}.bin"]
    assert len(objects) == 1
    with tenant_session(rw_engine, seed.tenant_a) as s:
        rows = s.execute(select(ImportBatch).where(ImportBatch.sha256 == sha)).scalars().all()
        assert len(rows) == 1
        assert audit_actions(rw_engine, seed.tenant_a, str(rows[0].id)) == ["import_uploaded"]


def test_task_payload_holds_ids_only(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    bid = upload(login_as("firm_admin"), "unparsed_file", "p.bin", uuid.uuid4().bytes)
    bid = bid.json()["batch"]["id"]
    (t,) = tasks_for(rw_engine, seed.tenant_a, bid)
    assert t.payload == {"import_batch_id": bid} and t.dedupe_key == bid
    run_worker(rw_engine)
    row = batch_row(rw_engine, seed.tenant_a, bid)
    assert (row.status, row.rows_loaded) == ("loaded", 0)  # unparsed_file yields no records


def test_the_test_csv_kind_never_exists_in_a_normally_started_app() -> None:
    """``tests/csv_source.py`` is registered by the harness only. A fresh process that
    imports the application sees exactly the production kinds."""
    from tests.test_hygiene import _start_app

    proc = _start_app(
        {},
        code=(
            "import app.main; from app.integrations.base import SOURCE_KINDS; "
            "print(sorted(SOURCE_KINDS))"
        ),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "['unparsed_file']"
