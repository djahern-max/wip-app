"""The exceptions queue (F09; D-22, D-46; the owner's answer C, 2026-10-08). Every role
reads the queue and adds notes (``can_view_exceptions``, ``can_note_exceptions``);
assign, dismiss and reopen follow the roles that manage jobs (``can_manage_exceptions``).
Refusals are one sentence. Money inside a sentence stays text; nothing here computes.

``refresh_after_write`` is the after-write hook (Plan answer 1): mounted on the routers
whose writes change a generator's input, it queues one run inside the request
transaction when a state-changing request returns without raising.
"""

from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.schemas import (
    ExceptionCountsOut,
    ExceptionDetailOut,
    ExceptionEventOut,
    ExceptionMemberOut,
    ExceptionOut,
    ExceptionsOut,
    ExceptionSubjectOut,
)
from app.core.audit import request_meta
from app.core.auth import Principal, TenantSession, require_tenant_id
from app.core.authz import (
    CAPABILITIES,
    can_manage_exceptions,
    can_note_exceptions,
    can_view_exceptions,
)
from app.core.db import get_request_session
from app.domain.config.audit import Actor
from app.domain.exceptions import service
from app.domain.exceptions.run import request_refresh
from app.tenancy.models import Tenant

router = APIRouter(prefix="/exceptions", tags=["exceptions"])

Viewer = Annotated[Principal, Depends(can_view_exceptions)]
Noter = Annotated[Principal, Depends(can_note_exceptions)]
Manager = Annotated[Principal, Depends(can_manage_exceptions)]

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def refresh_after_write(
    request: Request,
    db: Annotated[Session, Depends(get_request_session)],
    tenant_id: Annotated[UUID, Depends(require_tenant_id)],
) -> Iterator[None]:
    """Queue the exceptions run after a state-changing request that returned without
    raising; in the request transaction, so the notify goes with the commit. It depends
    on the request session, so it finishes before the session commits."""
    yield
    if request.method in WRITE_METHODS:
        request_refresh(db, tenant_id)


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssignIn(_In):
    user_id: UUID | None = None  # None: to nobody


class NoteIn(_In):
    text: str | None = Field(default=None, max_length=2000)


class DismissIn(_In):
    note: str | None = Field(default=None, max_length=2000)


def _actor(p: Principal, request: Request) -> Actor:
    return Actor(user_id=p.user.id, role=p.role, meta=request_meta(request))


def _raise(exc: service.ExceptionError) -> None:
    raise HTTPException(status_code=exc.status, detail=str(exc)) from None


def _out(v: service.ExceptionView) -> dict:
    return {
        "id": v.id,
        "code": v.code,
        "severity": v.severity,
        "severity_label": v.severity_label,
        "message": v.message,
        "subject": ExceptionSubjectOut(type=v.subject.type, id=v.subject.id, label=v.subject.label),
        "status": v.status,
        "status_label": v.status_label,
        "may_dismiss": v.may_dismiss,
        "assigned_to_id": v.assigned_to_id,
        "assigned_to": v.assigned_to,
        "first_raised_at": v.first_raised_at,
        "last_raised_at": v.last_raised_at,
        "resolved_at": v.resolved_at,
        "dismissed_at": v.dismissed_at,
        "dismissed_by": v.dismissed_by,
    }


def _detail(v: service.ExceptionView) -> ExceptionDetailOut:
    return ExceptionDetailOut(
        **_out(v),
        events=[
            ExceptionEventOut(
                id=e.id,
                kind=e.kind,
                kind_label=e.kind_label,
                occurred_at=e.occurred_at,
                actor=e.actor,
                assigned_to=e.assigned_to,
                text=e.text,
                detail=e.detail,
            )
            for e in v.events
        ],
    )


def _tenant_name(db: Session, tenant_id: UUID) -> str:
    row = db.get(Tenant, tenant_id)
    return row.name if row is not None else ""


Filter = Annotated[str | None, Query(max_length=20)]


@router.get("", response_model=ExceptionsOut)
def list_exceptions(
    viewer: Viewer,
    db: TenantSession,
    status: Filter = None,
    severity: Filter = None,
    mine: bool = False,
):
    if status and status not in service.STATUS_ORDER:
        raise HTTPException(status_code=422, detail="status is open, dismissed or resolved")
    if severity and severity not in service.SEVERITY_ORDER:
        raise HTTPException(status_code=422, detail="severity is block_close, warn or info")
    rows = service.list_exceptions(
        db,
        status=status or None,
        severity=severity or None,
        assigned_to=viewer.user.id if mine else None,
    )
    counts = service.open_counts_here(db)
    last = service.last_run_at(db)
    return ExceptionsOut(
        exceptions=[ExceptionOut(**_out(v)) for v in rows],
        total=len(rows),
        tenant_name=_tenant_name(db, viewer.active_tenant_id),
        as_of=last.isoformat() if last is not None else None,
        counts=ExceptionCountsOut(**counts),
        members=[
            ExceptionMemberOut(id=m.id, name=m.name)
            for m in service.members(db, viewer.active_tenant_id)
        ],
        can_manage=viewer.role in CAPABILITIES["manage_exceptions"][1],
    )


@router.get("/{exception_id}", response_model=ExceptionDetailOut)
def get_exception(viewer: Viewer, db: TenantSession, exception_id: UUID):
    try:
        return _detail(service.get_exception(db, exception_id))
    except service.ExceptionError as exc:
        _raise(exc)


@router.post("/{exception_id}/assign", response_model=ExceptionDetailOut)
def assign(request: Request, m: Manager, db: TenantSession, exception_id: UUID, body: AssignIn):
    try:
        return _detail(
            service.assign(db, m.active_tenant_id, exception_id, body.user_id, _actor(m, request))
        )
    except service.ExceptionError as exc:
        _raise(exc)


@router.post("/{exception_id}/notes", response_model=ExceptionDetailOut)
def add_note(request: Request, n: Noter, db: TenantSession, exception_id: UUID, body: NoteIn):
    try:
        return _detail(
            service.add_note(db, n.active_tenant_id, exception_id, body.text, _actor(n, request))
        )
    except service.ExceptionError as exc:
        _raise(exc)


@router.post("/{exception_id}/dismiss", response_model=ExceptionDetailOut)
def dismiss(request: Request, m: Manager, db: TenantSession, exception_id: UUID, body: DismissIn):
    try:
        return _detail(
            service.dismiss(db, m.active_tenant_id, exception_id, body.note, _actor(m, request))
        )
    except service.ExceptionError as exc:
        _raise(exc)


@router.post("/{exception_id}/reopen", response_model=ExceptionDetailOut)
def reopen(request: Request, m: Manager, db: TenantSession, exception_id: UUID):
    try:
        return _detail(service.reopen(db, m.active_tenant_id, exception_id, _actor(m, request)))
    except service.ExceptionError as exc:
        _raise(exc)
