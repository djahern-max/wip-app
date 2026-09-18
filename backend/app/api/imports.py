"""File imports (F03): upload, list, detail, download. Capability
``can_manage_imports`` (firm_admin, firm_staff, client_admin). The tenant is the
session's active tenant; a batch in another tenant is 404 because RLS never
returns it. Uploads need the CSRF header like every state-changing route."""

import logging
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import select

from app.api.schemas import ImportBatchOut, ImportUploadOut, SourceKindOut
from app.core.audit import request_meta
from app.core.auth import Principal, TenantSession
from app.core.authz import can_manage_imports
from app.core.config import get_settings
from app.core.storage import ObjectStore, ObjectStoreError
from app.ingest.imports import (
    UploadRefused,
    audit_download,
    batch_message,
    open_batch_object,
    receive_upload,
    relative_key,
)
from app.ingest.models import IMPORT_STATUS_LABELS, ImportBatch
from app.integrations.base import SOURCE_KINDS
from app.tenancy.models import User

log = logging.getLogger("app.ingest")

router = APIRouter(prefix="/imports", tags=["imports"])

Actor = Annotated[Principal, Depends(can_manage_imports)]


def get_object_store(request: Request) -> ObjectStore:
    return request.app.state.object_store


Store = Annotated[ObjectStore, Depends(get_object_store)]


def _out(b: ImportBatch, email: str | None = None) -> ImportBatchOut:
    kind = SOURCE_KINDS.get(b.source_kind)
    return ImportBatchOut(
        id=str(b.id),
        source_kind=b.source_kind,
        source_label=kind.label if kind else b.source_kind.replace("_", " ").capitalize(),
        sha256=b.sha256,
        byte_size=b.byte_size,
        original_filename=b.original_filename,
        content_type=b.content_type,
        uploaded_by=str(b.uploaded_by) if b.uploaded_by else None,
        uploaded_by_email=email,
        uploaded_at=b.uploaded_at.isoformat(),
        status=b.status,
        status_label=IMPORT_STATUS_LABELS[b.status],
        rows_loaded=b.rows_loaded,
        rows_rejected=b.rows_rejected,
        message=batch_message(b),
        error_detail=b.error,
        processed_at=b.processed_at.isoformat() if b.processed_at else None,
    )


def _batch_or_404(db: TenantSession, batch_id: UUID) -> ImportBatch:
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="import batch not found")
    return batch


@router.get("/source-kinds", response_model=list[SourceKindOut])
def source_kinds(_actor: Actor):
    return [
        SourceKindOut(
            name=k.name, label=k.label, description=k.description, extensions=sorted(k.extensions)
        )
        for k in SOURCE_KINDS.values()
    ]


@router.post("", response_model=ImportUploadOut, status_code=201)
def upload(
    request: Request,
    response: Response,
    actor: Actor,
    db: TenantSession,
    store: Store,
    source_kind: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
):
    try:
        batch, duplicate = receive_upload(
            db,
            store,
            tenant_id=actor.active_tenant_id,
            source_kind=source_kind,
            filename=file.filename or "",
            content_type=file.content_type,
            stream=file.file,
            actor_user_id=actor.user.id,
            actor_role=actor.role,
            meta=request_meta(request),
        )
    except UploadRefused as exc:
        # The person sees the sentence; the machine detail goes to the log with the
        # request id (echoed as X-Request-Id) so OPERATIONS can match them up.
        log.info("upload refused request_id=%s: %s", request.state.request_id, exc.reason)
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    if duplicate:
        response.status_code = 200  # nothing was created: the existing batch is returned
    return ImportUploadOut(batch=_out(batch, _email(db, batch)), duplicate=duplicate)


def _email(db: TenantSession, batch: ImportBatch) -> str | None:
    if batch.uploaded_by is None:
        return None
    return db.execute(select(User.email).where(User.id == batch.uploaded_by)).scalar_one_or_none()


@router.get("", response_model=list[ImportBatchOut])
def list_batches(_actor: Actor, db: TenantSession):
    stmt = (
        select(ImportBatch, User.email)
        .outerjoin(User, User.id == ImportBatch.uploaded_by)
        .order_by(ImportBatch.uploaded_at.desc(), ImportBatch.id)
        .limit(500)
    )
    return [_out(b, email) for b, email in db.execute(stmt)]


@router.get("/{batch_id}", response_model=ImportBatchOut)
def get_batch(_actor: Actor, db: TenantSession, batch_id: UUID):
    batch = _batch_or_404(db, batch_id)
    return _out(batch, _email(db, batch))


@router.get("/{batch_id}/download")
def download(request: Request, actor: Actor, db: TenantSession, store: Store, batch_id: UUID):
    """Streams the object (local store) or redirects to a short-lived signed URL
    (S3). Audited as ``import_downloaded`` in the request transaction."""
    batch = _batch_or_404(db, batch_id)
    audit_download(
        db,
        batch,
        actor_user_id=actor.user.id,
        actor_role=actor.role,
        meta=request_meta(request),
    )
    url = store.signed_url(
        batch.tenant_id, relative_key(batch), get_settings().signed_url_ttl_seconds
    )
    if url is not None:
        return RedirectResponse(url, status_code=307)
    try:
        with open_batch_object(store, batch) as f:
            data = f.read()
    except ObjectStoreError:
        raise HTTPException(status_code=404, detail="object not found") from None
    # The original name is data: offered to the browser (RFC 5987, percent-encoded),
    # never used as a key. Ascii fallback is the content hash.
    headers = {
        "Content-Disposition": f'attachment; filename="{batch.sha256}.bin"; '
        f"filename*=UTF-8''{quote(batch.original_filename)}"
    }
    return StreamingResponse(
        iter([data]), media_type=batch.content_type or "application/octet-stream", headers=headers
    )
