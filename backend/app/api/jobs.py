"""Jobs and the crosswalk (F07). Every role reads jobs (``can_view_jobs``); creating,
attaching, detaching, linking, unlinking, confirming a work-area kind and editing are
for ``firm_admin``, ``firm_staff`` and ``client_admin`` (``can_manage_jobs``), each one
audit row. The customer duplicates list is read-only, for the same three roles.

Money is strings with cents (D-22). Suggestions carry their reason; nothing is
attached or linked except by a person's request naming an id.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import (
    AttachCandidateOut,
    ChoiceOut,
    DivisionChoiceOut,
    DuplicatePairOut,
    DuplicateSideOut,
    DuplicatesOut,
    EstimateIssueOut,
    JobAliasOut,
    JobDetailOut,
    JobEstimateOut,
    JobRowOut,
    JobsOut,
    JobWorkAreaOut,
    QboRowOut,
    QboRowsOut,
    ReviewEntryOut,
    ReviewOut,
)
from app.core.audit import request_meta
from app.core.auth import Principal, TenantSession
from app.core.authz import can_manage_jobs, can_view_customer_duplicates, can_view_jobs
from app.domain.config.audit import Actor
from app.domain.config.models import Division
from app.domain.estimates.exceptions import Issue
from app.domain.estimates.models import STATUS_LABELS
from app.domain.estimates.service import money
from app.domain.jobs import service
from app.domain.jobs.issues import sentence
from app.domain.jobs.models import (
    ALIAS_SYSTEM_LABELS,
    JOB_STATUS_LABELS,
    JOB_STATUSES,
    REVENUE_METHOD_LABELS,
    REVENUE_METHODS,
    ROLE_LABELS,
    ROLES,
)
from app.domain.jobs.suggest import CustomerRow, suggested_kind

router = APIRouter(prefix="/jobs", tags=["jobs"])
customers_router = APIRouter(prefix="/customers", tags=["customers"])

Viewer = Annotated[Principal, Depends(can_view_jobs)]
Manager = Annotated[Principal, Depends(can_manage_jobs)]
DuplicatesViewer = Annotated[Principal, Depends(can_view_customer_duplicates)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobCreateIn(_In):
    estimate_id: UUID
    division_id: UUID | None = None
    name: str | None = Field(default=None, max_length=500)


class PoolJobIn(_In):
    name: str = Field(max_length=500)
    division_id: UUID | None = None


class JobPatchIn(_In):
    """Only the fields sent are changed (``model_fields_set``)."""

    name: str | None = Field(default=None, max_length=500)
    division_id: UUID | None = None
    revenue_method: str | None = Field(default=None, max_length=30)
    status: str | None = Field(default=None, max_length=30)
    notes: str | None = Field(default=None, max_length=4000)


class AttachIn(_In):
    estimate_id: UUID
    role: str = Field(max_length=20)
    note: str | None = Field(default=None, max_length=2000)


class AliasIn(_In):
    system: str = Field(max_length=30)
    external_id: str = Field(min_length=1, max_length=80)


class KindIn(_In):
    kind: str = Field(max_length=20)


def _actor(p: Principal, request: Request) -> Actor:
    return Actor(user_id=p.user.id, role=p.role, meta=request_meta(request))


def _raise(exc: service.JobError) -> None:
    raise HTTPException(status_code=exc.status, detail=str(exc)) from None


def _issues(items: list[Issue]) -> list[EstimateIssueOut]:
    return [EstimateIssueOut(code=i.code, message=i.message) for i in items]


def _divisions(db: Session) -> list[DivisionChoiceOut]:
    return [
        DivisionChoiceOut(id=str(d.id), code=d.code, name=d.name)
        for d in db.execute(
            select(Division).where(Division.active).order_by(Division.code)
        ).scalars()
    ]


def _choices(values: tuple[str, ...], labels: dict[str, str]) -> list[ChoiceOut]:
    return [ChoiceOut(value=v, label=labels[v]) for v in values]


def _contract_note(v: service.JobView) -> str | None:
    c = v.contract
    method = v.job.revenue_method
    if method == "pool":
        return "A pool has no contract (D-30)."
    if method == "time_and_materials":
        return "Time and materials: no revised contract; revenue is what is billed (D-24)."
    if not c.has_original:
        return "No original estimate on this job."
    if c.from_header_price:
        return "No work areas loaded: the original estimate's price."
    if c.to_confirm:
        return f"{c.to_confirm} work area{'s' if c.to_confirm != 1 else ''} to confirm"
    return None


def _eac_note(v: service.JobView) -> str | None:
    if v.job.revenue_method == "pool":
        return "A pool has no EAC (D-30)."
    if v.contract.eac_not_computed:
        return (
            "Not computed: an estimate on this job has no EAC in the WIP basis; see its attention."
        )
    if v.contract.eac_in_basis is None:
        return "Not computed"
    return None


def _row(v: service.JobView) -> dict:
    job = v.job
    return {
        "id": str(job.id),
        "name": job.name,
        "customer_name": v.customer.display_name if v.customer else None,
        "division_id": str(job.division_id) if job.division_id else None,
        "division_code": v.division.code if v.division else None,
        "revenue_method": job.revenue_method,
        "revenue_method_label": REVENUE_METHOD_LABELS[job.revenue_method],
        "status": job.status,
        "status_label": JOB_STATUS_LABELS[job.status],
        "sold_on": job.sold_on.isoformat(),
        "sold_on_set_when_created": v.sold_on_set_when_created,
        "revised_contract": money(v.contract.revised_contract),
        "revised_contract_note": _contract_note(v),
        "unapproved_change_orders": money(v.contract.unapproved_change_orders),
        "eac_in_basis": money(v.contract.eac_in_basis),
        "eac_note": _eac_note(v),
        "to_confirm": v.contract.to_confirm,
        "qbo_linked": bool(v.qbo_aliases),
        "qbo_names": [
            v.alias_rows[a.external_id].display_name
            if a.external_id in v.alias_rows
            else a.external_id
            for a in v.qbo_aliases
        ],
        "attention": _issues(v.issues),
    }


def _kind_label(kind: str | None, suggested: str | None, kept: bool) -> str:
    if not kept:
        return "Omitted"
    if kind is not None:
        return "Original" if kind == "original" else "Change order"
    return "Original (suggested)" if suggested == "original" else "Change order (suggested)"


def _detail(db: Session, v: service.JobView) -> JobDetailOut:
    original = v.original
    work_areas: list[JobWorkAreaOut] = []
    if original is not None:
        for w in original.view.work_areas or ():
            row = w.row
            suggestion = suggested_kind(row.change_order_suggested) if row.kept else None
            work_areas.append(
                JobWorkAreaOut(
                    id=str(row.id),
                    order_no=row.order_no,
                    name=row.name,
                    kept=row.kept,
                    kept_label="Kept" if row.kept else "Omitted",
                    price=money(row.price),
                    kind=row.kind,
                    suggested_kind=suggestion,
                    kind_label=_kind_label(row.kind, suggestion, row.kept),
                    confirmed=row.kind is not None,
                )
            )
    return JobDetailOut(
        **_row(v),
        notes=v.job.notes,
        created_at=v.job.created_at.isoformat(),
        created_by=v.users.get(v.job.created_by) if v.job.created_by else None,
        estimates=[
            JobEstimateOut(
                estimate_id=str(a.estimate.id),
                external_id=a.estimate.external_id,
                name=a.estimate.name,
                status_label=STATUS_LABELS.get(a.estimate.status_norm or "", a.estimate.status),
                role=a.link.role,
                role_label=ROLE_LABELS[a.link.role],
                price=money(a.estimate.price),
                note=a.link.note,
                attached_at=a.link.attached_at.isoformat(),
                attached_by=v.users.get(a.link.attached_by) if a.link.attached_by else None,
                work_areas_loaded=a.view.work_areas is not None,
                eac_in_basis=money(a.eac),
            )
            for a in v.attached
        ],
        aliases=[
            JobAliasOut(
                id=str(a.id),
                system=a.system,
                system_label=ALIAS_SYSTEM_LABELS[a.system],
                external_id=a.external_id,
                display_name=v.alias_rows[a.external_id].display_name
                if a.external_id in v.alias_rows
                else None,
                kind_label=v.alias_rows[a.external_id].kind_label
                if a.external_id in v.alias_rows
                else None,
                linked_at=a.linked_at.isoformat(),
                linked_by=v.users.get(a.linked_by) if a.linked_by else None,
            )
            for a in v.aliases
        ],
        original_external_id=original.estimate.external_id if original else None,
        work_areas=work_areas,
        work_areas_loaded=bool(original and original.view.work_areas is not None),
        divisions=_divisions(db),
        revenue_methods=_choices(REVENUE_METHODS, REVENUE_METHOD_LABELS),
        statuses=_choices(JOB_STATUSES, JOB_STATUS_LABELS),
    )


def _fresh_detail(db: Session, p: Principal, job_id: UUID) -> JobDetailOut:
    db.flush()
    db.expire_all()
    return _detail(db, service.job_detail(db, p.active_tenant_id, job_id))


def _qbo_row(r: CustomerRow, reason: str | None) -> QboRowOut:
    return QboRowOut(
        customer_id=str(r.id),
        external_id=r.external_id,
        display_name=r.display_name,
        parent_name=r.parent_name,
        kind_label=r.kind_label,
        reason=reason,
    )


# --- reads ---------------------------------------------------------------------------


@router.get("", response_model=JobsOut)
def list_jobs(
    viewer: Viewer,
    db: TenantSession,
    status: Annotated[str | None, Query(max_length=30)] = None,
    division_id: UUID | None = None,
    revenue_method: Annotated[str | None, Query(max_length=30)] = None,
    no_link: bool = False,
):
    views = service.list_jobs(
        db,
        viewer.active_tenant_id,
        status=status or None,
        division_id=division_id,
        revenue_method=revenue_method or None,
        no_link=no_link,
    )
    return JobsOut(
        jobs=[JobRowOut(**_row(v)) for v in views],
        total=len(views),
        to_review=len(service.review_queue(db, viewer.active_tenant_id)),
        divisions=_divisions(db),
        revenue_methods=_choices(REVENUE_METHODS, REVENUE_METHOD_LABELS),
        statuses=_choices(JOB_STATUSES, JOB_STATUS_LABELS),
        ledger_items=_issues(service.ledger_items(db)),
    )


@router.get("/review", response_model=ReviewOut)
def review(viewer: Viewer, db: TenantSession):
    entries = service.review_queue(db, viewer.active_tenant_id)
    divisions = _divisions(db)
    codes = {d.id: d.code for d in divisions}
    out = []
    for q in entries:
        e = q.view.estimate
        suggestion = str(q.division_suggestion) if q.division_suggestion else None
        out.append(
            ReviewEntryOut(
                estimate_id=str(e.id),
                external_id=e.external_id,
                estimator=e.estimator,
                client_name=e.client_name,
                jobsite=e.jobsite,
                name=e.name,
                price=money(e.price),
                estimate_date=e.estimate_date.isoformat() if e.estimate_date else None,
                division_suggestion_id=suggestion if suggestion in codes else None,
                division_suggestion_code=codes.get(suggestion) if suggestion else None,
                candidates=[
                    AttachCandidateOut(
                        job_id=str(c.job.id),
                        job_name=c.job.name,
                        reasons=list(c.reasons),
                        same_customer=c.same_customer,
                    )
                    for c in q.candidates
                ],
                attention=_issues(q.issues),
            )
        )
    return ReviewOut(
        entries=out,
        total=len(out),
        divisions=divisions,
        roles=_choices(ROLES, ROLE_LABELS),
    )


@router.get("/{job_id}", response_model=JobDetailOut)
def get_job(viewer: Viewer, db: TenantSession, job_id: UUID):
    try:
        return _detail(db, service.job_detail(db, viewer.active_tenant_id, job_id))
    except service.JobError as exc:
        _raise(exc)


@router.get("/{job_id}/qbo-candidates", response_model=QboRowsOut)
def qbo_candidates(viewer: Viewer, db: TenantSession, job_id: UUID):
    try:
        found = service.job_qbo_candidates(db, job_id)
    except service.JobError as exc:
        _raise(exc)
    return QboRowsOut(rows=[_qbo_row(c.row, c.reason) for c in found])


@router.get("/{job_id}/qbo-search", response_model=QboRowsOut)
def qbo_search(
    viewer: Viewer, db: TenantSession, job_id: UUID, q: Annotated[str, Query(max_length=200)] = ""
):
    try:
        service.job_detail(db, viewer.active_tenant_id, job_id)
    except service.JobError as exc:
        _raise(exc)
    return QboRowsOut(rows=[_qbo_row(r, None) for r in service.qbo_search(db, q)])


@customers_router.get("/duplicates", response_model=DuplicatesOut)
def duplicates(viewer: DuplicatesViewer, db: TenantSession):
    def side(s: service.DuplicateSide) -> DuplicateSideOut:
        return DuplicateSideOut(
            customer_id=str(s.row.id),
            external_id=s.row.external_id,
            display_name=s.row.display_name,
            billing_count=s.billing_count,
            payment_count=s.payment_count,
        )

    return DuplicatesOut(
        pairs=[
            DuplicatePairOut(
                code="CUSTOMER_FUZZY",
                message=sentence("CUSTOMER_FUZZY", a=a.row.display_name, b=b.row.display_name),
                a=side(a),
                b=side(b),
            )
            for a, b in service.customer_duplicates(db)
        ]
    )


# --- writes (one audit row each) -------------------------------------------------------


@router.post("", response_model=JobDetailOut, status_code=201)
def create_job(request: Request, m: Manager, db: TenantSession, body: JobCreateIn):
    try:
        job = service.create_job_from_estimate(
            db,
            m.active_tenant_id,
            estimate_id=body.estimate_id,
            division_id=body.division_id,
            name=body.name,
            actor=_actor(m, request),
        )
    except service.JobError as exc:
        _raise(exc)
    return _fresh_detail(db, m, job.id)


@router.post("/pool", response_model=JobDetailOut, status_code=201)
def create_pool(request: Request, m: Manager, db: TenantSession, body: PoolJobIn):
    try:
        job = service.create_pool_job(
            db,
            m.active_tenant_id,
            name=body.name,
            division_id=body.division_id,
            actor=_actor(m, request),
        )
    except service.JobError as exc:
        _raise(exc)
    return _fresh_detail(db, m, job.id)


@router.patch("/{job_id}", response_model=JobDetailOut)
def patch_job(request: Request, m: Manager, db: TenantSession, job_id: UUID, body: JobPatchIn):
    changes = {k: getattr(body, k) for k in body.model_fields_set}
    try:
        service.update_job(db, m.active_tenant_id, job_id, changes, _actor(m, request))
    except service.JobError as exc:
        _raise(exc)
    return _fresh_detail(db, m, job_id)


@router.post("/{job_id}/estimates", response_model=JobDetailOut)
def attach(request: Request, m: Manager, db: TenantSession, job_id: UUID, body: AttachIn):
    try:
        service.attach_estimate(
            db,
            m.active_tenant_id,
            job_id,
            estimate_id=body.estimate_id,
            role=body.role,
            note=body.note,
            actor=_actor(m, request),
        )
    except service.JobError as exc:
        _raise(exc)
    return _fresh_detail(db, m, job_id)


@router.delete("/{job_id}/estimates/{estimate_id}", response_model=JobDetailOut)
def detach(request: Request, m: Manager, db: TenantSession, job_id: UUID, estimate_id: UUID):
    try:
        service.detach_estimate(db, m.active_tenant_id, job_id, estimate_id, _actor(m, request))
    except service.JobError as exc:
        _raise(exc)
    return _fresh_detail(db, m, job_id)


@router.post("/{job_id}/aliases", response_model=JobDetailOut)
def link(request: Request, m: Manager, db: TenantSession, job_id: UUID, body: AliasIn):
    try:
        service.link_alias(
            db,
            m.active_tenant_id,
            job_id,
            system=body.system,
            external_id=body.external_id,
            actor=_actor(m, request),
        )
    except service.JobError as exc:
        _raise(exc)
    return _fresh_detail(db, m, job_id)


@router.delete("/{job_id}/aliases/{alias_id}", response_model=JobDetailOut)
def unlink(request: Request, m: Manager, db: TenantSession, job_id: UUID, alias_id: UUID):
    try:
        service.unlink_alias(db, m.active_tenant_id, job_id, alias_id, _actor(m, request))
    except service.JobError as exc:
        _raise(exc)
    return _fresh_detail(db, m, job_id)


@router.post("/{job_id}/work-areas/{work_area_id}/kind", response_model=JobDetailOut)
def confirm_kind(
    request: Request, m: Manager, db: TenantSession, job_id: UUID, work_area_id: UUID, body: KindIn
):
    try:
        service.confirm_kind(
            db, m.active_tenant_id, job_id, work_area_id, body.kind, _actor(m, request)
        )
    except service.JobError as exc:
        _raise(exc)
    return _fresh_detail(db, m, job_id)
