"""Estimate tables (F06; BLUEPRINT §7 Spine; D-01, D-32). All tenant-scoped:
``tenant_id`` NOT NULL, an index or unique constraint leading with it, RLS enabled
and forced in migration 0010. Money is ``NUMERIC(14,2)``, hours ``NUMERIC(9,2)``.

- ``estimate``: one row per estimate per tenant and source, matched by
  ``external_id`` (never by name). ``status`` as received; ``status_norm`` is
  pending / sold / lost or NULL for a word the template does not know. ``price``
  is the control total the kept work areas must sum to.
- ``estimate_version``: one row per accepted upload that touched the estimate. Every
  version is a full snapshot of the work areas and cost lines the platform held at
  that moment; the three ``*_raw_record_id`` columns say which raw versions it was
  built from. ``is_baseline`` marks the D-01 baseline (owner's answer to plan
  question 11): the first version with work areas received while the estimate is
  sold, or the latest such version when a later upload marks it sold.
- ``estimate_work_area``: per version, identity ``order_no`` (the "#n" of D-26).
  Its cost is the sum of its cost lines, computed, never stored.
- ``estimate_cost``: one row per cost code under a work area (same-code lines are
  summed at parse). The code resolves to ``(division, cost_category)`` through the
  F04 grid; an unknown code keeps its text with both ids NULL.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.tenancy.models import Base

STATUS_NORMS: tuple[str, ...] = ("pending", "sold", "lost")
STATUS_NORM_CHECK = "ck_estimate_status_norm"
STATUS_NORM_CHECK_SQL = "status_norm IS NULL OR status_norm IN ('pending','sold','lost')"
# The one status → label mapping (D-22). An unknown word is shown as received.
STATUS_LABELS: dict[str, str] = {"pending": "Pending", "sold": "Sold", "lost": "Lost"}
BASELINE_INDEX = "uq_estimate_version_baseline"
MONEY = Numeric(14, 2)
HOURS = Numeric(9, 2)


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


class Estimate(Base):
    __tablename__ = "estimate"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source", "external_id", name="uq_estimate_tenant_source_id"),
        Index("ix_estimate_tenant_id_status_norm", "tenant_id", "status_norm"),
        Index("ix_estimate_tenant_id_estimator", "tenant_id", "estimator"),
        CheckConstraint(STATUS_NORM_CHECK_SQL, name=STATUS_NORM_CHECK),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    estimator: Mapped[str | None] = mapped_column(String(200))
    client_name: Mapped[str | None] = mapped_column(String(500))
    jobsite: Mapped[str | None] = mapped_column(String(500))
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    status_norm: Mapped[str | None] = mapped_column(String(20))
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    estimate_date: Mapped[date | None] = mapped_column(Date)
    raw_record_id: Mapped[uuid.UUID | None] = _fk("raw_record.id")
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class EstimateVersion(Base):
    __tablename__ = "estimate_version"
    __table_args__ = (
        UniqueConstraint("tenant_id", "estimate_id", "version_no", name="uq_estimate_version_no"),
        Index(
            BASELINE_INDEX,
            "tenant_id",
            "estimate_id",
            unique=True,
            postgresql_where=text("is_baseline"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    estimate_id: Mapped[uuid.UUID] = _fk("estimate.id", nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    import_batch_id: Mapped[uuid.UUID] = _fk("import_batch.id", nullable=False)
    header_raw_record_id: Mapped[uuid.UUID | None] = _fk("raw_record.id")
    work_areas_raw_record_id: Mapped[uuid.UUID | None] = _fk("raw_record.id")
    cost_lines_raw_record_id: Mapped[uuid.UUID | None] = _fk("raw_record.id")
    received_at: Mapped[datetime] = _now()
    status_norm: Mapped[str | None] = mapped_column(String(20))
    kept_total: Mapped[Decimal | None] = mapped_column(MONEY)
    is_baseline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class EstimateWorkArea(Base):
    __tablename__ = "estimate_work_area"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "estimate_version_id", "order_no", name="uq_estimate_work_area_order"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    estimate_version_id: Mapped[uuid.UUID] = _fk("estimate_version.id", nullable=False)
    order_no: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    kept: Mapped[bool] = mapped_column(Boolean, nullable=False)
    change_order_suggested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(2000))


class EstimateCost(Base):
    __tablename__ = "estimate_cost"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "estimate_work_area_id", "cost_code", name="uq_estimate_cost_code"
        ),
        Index("ix_estimate_cost_tenant_id_division_id", "tenant_id", "division_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    estimate_work_area_id: Mapped[uuid.UUID] = _fk("estimate_work_area.id", nullable=False)
    cost_category_id: Mapped[uuid.UUID | None] = _fk("cost_category.id")
    division_id: Mapped[uuid.UUID | None] = _fk("division.id")
    cost_code: Mapped[str] = mapped_column(String(20), nullable=False)
    hours: Mapped[Decimal | None] = mapped_column(HOURS)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(2000))
