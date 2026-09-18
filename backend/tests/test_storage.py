"""Object storage (D-21): the store builds the tenant prefix, refuses bad relative
keys, never writes outside its directory; the S3 store is proven with botocore's
Stubber (no network); starting with ``OBJECT_STORE=s3`` and a credential unset
exits naming the variable."""

import io
import uuid
from pathlib import Path

import boto3
import pytest
from botocore.stub import ANY, Stubber

from app.core.config import SPACES_SETTINGS
from app.core.storage import (
    LocalObjectStore,
    ObjectStoreError,
    S3ObjectStore,
    full_key,
    validate_relative_key,
)
from tests.test_hygiene import _start_app

TENANT = uuid.uuid4()


@pytest.mark.parametrize(
    "key",
    [
        "",
        "/abs",
        "a/../b",
        "..",
        "a/..",
        "back\\slash",
        "a//b",
        "trailing/",
        "-lead",
        "sp ace",
        "é",
    ],
)
def test_bad_relative_keys_are_refused(key: str) -> None:
    with pytest.raises(ObjectStoreError, match="invalid relative object key"):
        validate_relative_key(key)


def test_full_key_starts_with_the_tenant_prefix() -> None:
    assert full_key(TENANT, "imports/abc.csv") == f"tenant/{TENANT}/imports/abc.csv"
    assert validate_relative_key("imports/a.b-c_d/e.csv") == "imports/a.b-c_d/e.csv"


def test_local_store_round_trip_and_confinement(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "store")
    key = store.put(TENANT, "imports/x.bin", io.BytesIO(b"hello"))
    assert key == f"tenant/{TENANT}/imports/x.bin"
    assert store.exists(TENANT, "imports/x.bin")
    with store.open(TENANT, "imports/x.bin") as f:
        assert f.read() == b"hello"
    assert store.signed_url(TENANT, "imports/x.bin", 60) is None
    # Overwriting the same key (a retried content-addressed upload) leaves one object.
    store.put(TENANT, "imports/x.bin", io.BytesIO(b"hello"))
    written = [p for p in (tmp_path / "store").rglob("*") if p.is_file()]
    assert [p.relative_to(tmp_path / "store").as_posix() for p in written] == [key]
    # Another tenant's prefix is a different object; a missing object is an error.
    other = uuid.uuid4()
    assert not store.exists(other, "imports/x.bin")
    with pytest.raises(ObjectStoreError, match="not found"):
        with store.open(other, "imports/x.bin"):
            pass
    # Nothing can be written or read outside the store directory.
    for bad in ("../escape", "/etc/passwd", "a\\b"):
        with pytest.raises(ObjectStoreError):
            store.put(TENANT, bad, io.BytesIO(b"x"))
    assert not any(p.is_file() for p in tmp_path.iterdir() if p.name != "store")


def _stubbed_store() -> tuple[S3ObjectStore, Stubber]:
    client = boto3.client(
        "s3",
        region_name="nyc3",
        endpoint_url="https://nyc3.digitaloceanspaces.com",
        aws_access_key_id="stub-key-id",
        aws_secret_access_key="stub-secret",
    )
    return S3ObjectStore("wip-bucket", client), Stubber(client)


def test_s3_store_puts_privately_under_the_tenant_prefix() -> None:
    store, stub = _stubbed_store()
    expected_key = f"tenant/{TENANT}/imports/abc.csv"
    stub.add_response(
        "put_object",
        {},
        {"Bucket": "wip-bucket", "Key": expected_key, "Body": ANY, "ACL": "private"},
    )
    stub.add_response(
        "head_object", {"ContentLength": 3}, {"Bucket": "wip-bucket", "Key": expected_key}
    )
    stub.add_client_error(
        "head_object",
        service_error_code="404",
        expected_params={"Bucket": "wip-bucket", "Key": f"tenant/{TENANT}/imports/nope.csv"},
    )
    stub.add_response(
        "get_object",
        {"Body": io.BytesIO(b"a,b"), "ContentLength": 3},
        {"Bucket": "wip-bucket", "Key": expected_key},
    )
    stub.add_client_error(
        "get_object",
        service_error_code="NoSuchKey",
        expected_params={"Bucket": "wip-bucket", "Key": f"tenant/{TENANT}/imports/nope.csv"},
    )
    with stub:
        assert store.put(TENANT, "imports/abc.csv", io.BytesIO(b"a,b")) == expected_key
        assert store.exists(TENANT, "imports/abc.csv") is True
        assert store.exists(TENANT, "imports/nope.csv") is False
        with store.open(TENANT, "imports/abc.csv") as f:
            assert f.read() == b"a,b"
        with pytest.raises(ObjectStoreError, match="not found"):
            with store.open(TENANT, "imports/nope.csv"):
                pass
    stub.assert_no_pending_responses()


def test_s3_signed_url_names_bucket_key_and_expiry() -> None:
    store, _stub = _stubbed_store()
    url = store.signed_url(TENANT, "imports/abc.csv", 60)
    assert url.startswith("https://")
    assert "wip-bucket" in url and f"tenant/{TENANT}/imports/abc.csv" in url
    assert "X-Amz-Expires=60" in url
    assert "stub-secret" not in url


def test_s3_store_refuses_bad_keys_before_any_call() -> None:
    store, stub = _stubbed_store()
    with stub:  # no responses queued: any call would fail the stub
        with pytest.raises(ObjectStoreError):
            store.put(TENANT, "../x", io.BytesIO(b""))
    stub.assert_no_pending_responses()


S3_ENV = {
    "OBJECT_STORE": "s3",
    "SPACES_ENDPOINT_URL": "https://nyc3.digitaloceanspaces.com",
    "SPACES_REGION": "nyc3",
    "SPACES_BUCKET": "wip",
    "SPACES_ACCESS_KEY_ID": "stub-key-id",
    "SPACES_SECRET_ACCESS_KEY": "stub-secret-value",
}


@pytest.mark.parametrize("missing", [name.upper() for name in SPACES_SETTINGS])
def test_s3_start_without_a_credential_exits_naming_it(missing: str) -> None:
    proc = _start_app({**S3_ENV, missing: None})
    assert proc.returncode != 0
    assert missing in proc.stderr
    assert "stub-secret-value" not in proc.stderr


def test_s3_start_with_every_credential_succeeds() -> None:
    proc = _start_app(S3_ENV)
    assert proc.returncode == 0, proc.stderr


def test_unknown_store_name_is_refused() -> None:
    proc = _start_app({"OBJECT_STORE": "minio"})
    assert proc.returncode != 0 and "OBJECT_STORE" in proc.stderr
