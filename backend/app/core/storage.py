"""Object storage behind a protocol (F03, D-21; BLUEPRINT §11, §12).

The store, not the caller, builds the ``tenant/{tenant_id}/`` prefix: a caller
passes a *relative* key and a tenant id, so a cross-tenant key cannot be built
by mistake. Relative keys containing ``..``, a leading ``/``, or a backslash are
refused. Objects are private; downloads use signed URLs with a short expiry.

``LocalObjectStore`` (development, tests, CI) keeps objects under a directory and
never writes outside it. ``S3ObjectStore`` talks to DigitalOcean Spaces through
``boto3`` with a private ACL. No S3 emulator: the S3 store is tested with
botocore's Stubber.

``open`` yields a *seekable* stream from every store. boto3's streaming body is
not seekable, so the S3 store spools the object to a temporary file first (an
object is at most ``MAX_UPLOAD_BYTES``); a parser that sniffs the first bytes and
rewinds then behaves the same in production as it does against the local store.
"""

import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Protocol
from uuid import UUID

from app.core.config import Settings

_RELATIVE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]*$")


class ObjectStoreError(Exception):
    """A refused key or a missing object. Carries no file content."""


def validate_relative_key(relative_key: str) -> str:
    if (
        not relative_key
        or relative_key.startswith("/")
        or "\\" in relative_key
        or ".." in relative_key.split("/")
        or "//" in relative_key
        or relative_key.endswith("/")
        or not _RELATIVE_KEY.match(relative_key)
    ):
        raise ObjectStoreError("invalid relative object key")
    return relative_key


def full_key(tenant_id: UUID, relative_key: str) -> str:
    return f"tenant/{tenant_id}/{validate_relative_key(relative_key)}"


class ObjectStore(Protocol):
    def put(self, tenant_id: UUID, relative_key: str, stream: BinaryIO) -> str:
        """Write the object; returns the full key. Overwrites an identical key."""

    @contextmanager
    def open(self, tenant_id: UUID, relative_key: str) -> Iterator[BinaryIO]:
        """A readable, seekable binary stream positioned at 0; ``ObjectStoreError``
        when the object is missing."""

    def signed_url(self, tenant_id: UUID, relative_key: str, ttl: int) -> str | None:
        """A short-lived download URL, or ``None`` when the store streams instead."""

    def exists(self, tenant_id: UUID, relative_key: str) -> bool: ...


class LocalObjectStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, tenant_id: UUID, relative_key: str) -> Path:
        path = (self.root / full_key(tenant_id, relative_key)).resolve()
        if self.root not in path.parents:
            raise ObjectStoreError("invalid relative object key")
        return path

    def put(self, tenant_id: UUID, relative_key: str, stream: BinaryIO) -> str:
        path = self._path(tenant_id, relative_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        with tmp.open("wb") as out:
            shutil.copyfileobj(stream, out)
        tmp.replace(path)  # atomic: a reader never sees a half-written object
        return full_key(tenant_id, relative_key)

    @contextmanager
    def open(self, tenant_id: UUID, relative_key: str) -> Iterator[BinaryIO]:
        path = self._path(tenant_id, relative_key)
        if not path.is_file():
            raise ObjectStoreError("object not found")
        with path.open("rb") as f:
            yield f

    def signed_url(self, tenant_id: UUID, relative_key: str, ttl: int) -> str | None:
        return None

    def exists(self, tenant_id: UUID, relative_key: str) -> bool:
        return self._path(tenant_id, relative_key).is_file()


class S3ObjectStore:
    """DigitalOcean Spaces (S3-compatible). ``client`` is injectable for the Stubber."""

    def __init__(self, bucket: str, client) -> None:
        self.bucket = bucket
        self.client = client

    @classmethod
    def from_settings(cls, settings: Settings) -> "S3ObjectStore":
        import boto3

        client = boto3.client(
            "s3",
            endpoint_url=settings.spaces_endpoint_url,
            region_name=settings.spaces_region,
            aws_access_key_id=settings.spaces_access_key_id,
            aws_secret_access_key=settings.spaces_secret_access_key,
        )
        return cls(settings.spaces_bucket or "", client)

    def put(self, tenant_id: UUID, relative_key: str, stream: BinaryIO) -> str:
        key = full_key(tenant_id, relative_key)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=stream, ACL="private")
        return key

    @contextmanager
    def open(self, tenant_id: UUID, relative_key: str) -> Iterator[BinaryIO]:
        from botocore.exceptions import ClientError

        key = full_key(tenant_id, relative_key)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise ObjectStoreError("object not found") from None
            raise
        body = response["Body"]
        tmp = tempfile.TemporaryFile()
        try:
            shutil.copyfileobj(body, tmp)
        finally:
            body.close()
        try:
            tmp.seek(0)
            yield tmp
        finally:
            tmp.close()

    def signed_url(self, tenant_id: UUID, relative_key: str, ttl: int) -> str | None:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": full_key(tenant_id, relative_key)},
            ExpiresIn=ttl,
        )

    def exists(self, tenant_id: UUID, relative_key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=full_key(tenant_id, relative_key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True


def build_object_store(settings: Settings) -> ObjectStore:
    if settings.object_store == "s3":
        return S3ObjectStore.from_settings(settings)
    return LocalObjectStore(settings.local_object_store_dir)
