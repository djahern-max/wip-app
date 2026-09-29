"""Job spine tables (F07; BLUEPRINT §5; D-01, D-03, D-24, D-30, D-31). All
tenant-scoped: ``tenant_id`` NOT NULL, an index or unique constraint leading with it,
RLS enabled and forced in migration 0011.

- ``job``: the unit everything after F07 reports on. No contract amount is stored:
  the revised contract is computed from the attached estimates (§5, D-01).
  ``revenue_method`` is the D-31 field; only ``fixed_price`` reaches the WIP schedule.
- ``job_estimate``: an estimate belongs to at most one job, with a role. A job has at
  most one ``original`` (partial unique index); a pool has none (the service).
- ``job_alias``: the crosswalk. One outside id maps to one job; a job may carry
  several ``qbo_customer`` aliases (owner's answer 15). Matching is by alias, never
  by name.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.tenancy.models import Base

REVENUE_METHODS: tuple[str, ...] = (
    "fixed_price",
    "time_and_materials",
    "recurring_service",
    "none",
    "pool",
)
REVENUE_METHOD_LABELS: dict[str, str] = {
    "fixed_price": "Fixed price",
    "time_and_materials": "Time and materials",
    "recurring_service": "Recurring service",
    "none": "None",
    "pool": "Pool",
}
JOB_STATUSES: tuple[str, ...] = (
    "sold",
    "in_progress",
    "substantially_complete",
    "closed",
    "cancelled",
)
JOB_STATUS_LABELS: dict[str, str] = {
    "sold": "Sold",
    "in_progress": "In progress",
    "substantially_complete": "Substantially complete",
    "closed": "Closed",
    "cancelled": "Cancelled",
}
OPEN_STATUSES = frozenset({"sold", "in_progress", "substantially_complete"})
ROLES: tuple[str, ...] = ("original", "change_order", "ignored")
ROLE_LABELS: dict[str, str] = {
    "original": "Original",
    "change_order": "Change order",
    "ignored": "Ignored",
}
ALIAS_SYSTEMS: tuple[str, ...] = ("lmn_estimate", "qbo_customer")
ALIAS_SYSTEM_LABELS: dict[str, str] = {
    "lmn_estimate": "Estimate",
    "qbo_customer": "QuickBooks",
}

REVENUE_METHOD_CHECK_SQL = (
    "revenue_method IN ('fixed_price','time_and_materials','recurring_service','none','pool')"
)
STATUS_CHECK_SQL = "status IN ('sold','in_progress','substantially_complete','closed','cancelled')"
ROLE_CHECK_SQL = "role IN ('original','change_order','ignored')"
IGNORED_NOTE_CHECK_SQL = "role <> 'ignored' OR (note IS NOT NULL AND btrim(note) <> '')"
SYSTEM_CHECK_SQL = "system IN ('lmn_estimate','qbo_customer')"
ORIGINAL_INDEX = "uq_job_estimate_one_original"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _tenant_id() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )


def _fk(target: str, *, nullable: bool = True):
    return mapped_column(
        UUID(as_uuid=True), ForeignKey(target, ondelete="RESTRICT"), nullable=nullable
    )


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (
        Index("ix_job_tenant_id_status", "tenant_id", "status"),
        Index("ix_job_tenant_id_customer_id", "tenant_id", "customer_id"),
        CheckConstraint(REVENUE_METHOD_CHECK_SQL, name="ck_job_revenue_method"),
        CheckConstraint(STATUS_CHECK_SQL, name="ck_job_status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    customer_id: Mapped[uuid.UUID | None] = _fk("customer.id")
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    # Required by the API; nullable so data made another way is reported
    # (JOB_DIVISION_UNSET) rather than refused.
    division_id: Mapped[uuid.UUID | None] = _fk("division.id")
    revenue_method: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    sold_on: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(4000))
    created_by: Mapped[uuid.UUID | None] = _fk("user.id")
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class JobEstimate(Base):
    __tablename__ = "job_estimate"
    __table_args__ = (
        UniqueConstraint("tenant_id", "estimate_id", name="uq_job_estimate_estimate"),
        Index("ix_job_estimate_tenant_id_job_id", "tenant_id", "job_id"),
        Index(
            ORIGINAL_INDEX,
            "tenant_id",
            "job_id",
            unique=True,
            postgresql_where=text("role = 'original'"),
        ),
        CheckConstraint(ROLE_CHECK_SQL, name="ck_job_estimate_role"),
        CheckConstraint(IGNORED_NOTE_CHECK_SQL, name="ck_job_estimate_ignored_note"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    job_id: Mapped[uuid.UUID] = _fk("job.id", nullable=False)
    estimate_id: Mapped[uuid.UUID] = _fk("estimate.id", nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    attached_by: Mapped[uuid.UUID | None] = _fk("user.id")
    attached_at: Mapped[datetime] = _now()
    note: Mapped[str | None] = mapped_column(String(2000))


class JobAlias(Base):
    __tablename__ = "job_alias"
    __table_args__ = (
        UniqueConstraint("tenant_id", "system", "external_id", name="uq_job_alias_system_id"),
        Index("ix_job_alias_tenant_id_job_id", "tenant_id", "job_id"),
        CheckConstraint(SYSTEM_CHECK_SQL, name="ck_job_alias_system"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    job_id: Mapped[uuid.UUID] = _fk("job.id", nullable=False)
    system: Mapped[str] = mapped_column(String(30), nullable=False)
    external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    linked_by: Mapped[uuid.UUID | None] = _fk("user.id")
    linked_at: Mapped[datetime] = _now()
