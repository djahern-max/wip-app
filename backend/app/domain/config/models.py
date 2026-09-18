"""Configuration tables (F04). All tenant-scoped: ``tenant_id`` NOT NULL, an index or
unique constraint leading with it, RLS enabled and forced in migration 0005.

- ``division``: a line of business; ``code_digit`` (one character, unique per tenant
  when set) is the first character of a cost code (D-23).
- ``cost_category``: the D-23 list per tenant, keyed by two-digit ``slot``; deactivated,
  never deleted.
- ``gl_account``: the tenant's chart of accounts. ``account_no`` is text of any length
  (owner amendment B); unique per tenant. ``source`` is ``file`` now, ``qbo`` in F05.
- ``account_map``: one row per mapped account; only ``confirmed`` rows are used by any
  later feature. ``suggested_by_rule`` names the rule that proposed it.
- ``account_suggest_rule``: the numbering scheme as data (plan call 2): ordered rules;
  first match wins; each output is an explicit id or derived from the account number.
- ``tenant_policy``: typed keys, one row per key; no accounting-policy key has a default.
- ``burden_rate``: effective-dated, ``rate`` a fraction as ``NUMERIC(7,4)``.
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.tenancy.models import Base

MAP_STATUSES: tuple[str, ...] = ("suggested", "confirmed")
MAP_STATUS_CHECK = "ck_account_map_status"
MAP_STATUS_CHECK_SQL = "status IN ('suggested','confirmed')"
RULE_DIVISION_CHECK = "ck_account_suggest_rule_division_one_way"
RULE_DIVISION_CHECK_SQL = "NOT (division_id IS NOT NULL AND division_from_digit IS NOT NULL)"
RULE_CATEGORY_CHECK = "ck_account_suggest_rule_category_one_way"
RULE_CATEGORY_CHECK_SQL = "NOT (cost_category_id IS NOT NULL AND cost_category_from_slot)"
BURDEN_PERIOD_CHECK = "ck_burden_rate_period"
BURDEN_PERIOD_CHECK_SQL = "effective_to IS NULL OR effective_to > effective_from"
PATTERN_MAX_LENGTH = 100


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _tenant_id() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Division(Base):
    __tablename__ = "division"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_division_tenant_code"),
        Index(
            "uq_division_tenant_code_digit",
            "tenant_id",
            "code_digit",
            unique=True,
            postgresql_where=text("code_digit IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    code_digit: Mapped[str | None] = mapped_column(String(1))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _now()


class CostCategory(Base):
    __tablename__ = "cost_category"
    __table_args__ = (UniqueConstraint("tenant_id", "slot", name="uq_cost_category_tenant_slot"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    slot: Mapped[str] = mapped_column(String(2), nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _now()


class GlAccount(Base):
    __tablename__ = "gl_account"
    __table_args__ = (UniqueConstraint("tenant_id", "account_no", name="uq_gl_account_tenant_no"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    account_no: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    ledger_type: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="file")
    external_id: Mapped[str | None] = mapped_column(String(80))
    raw_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_record.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AccountMap(Base):
    __tablename__ = "account_map"
    __table_args__ = (
        UniqueConstraint("tenant_id", "gl_account_id", name="uq_account_map_tenant_account"),
        CheckConstraint(MAP_STATUS_CHECK_SQL, name=MAP_STATUS_CHECK),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    gl_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gl_account.id", ondelete="RESTRICT"), nullable=False
    )
    division_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("division.id", ondelete="RESTRICT")
    )
    cost_category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cost_category.id", ondelete="RESTRICT")
    )
    in_job_cost: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="suggested")
    suggested_by_rule: Mapped[str | None] = mapped_column(String(100))
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="RESTRICT")
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AccountSuggestRule(Base):
    __tablename__ = "account_suggest_rule"
    __table_args__ = (
        UniqueConstraint("tenant_id", "sort_order", name="uq_account_suggest_rule_tenant_order"),
        CheckConstraint(RULE_DIVISION_CHECK_SQL, name=RULE_DIVISION_CHECK),
        CheckConstraint(RULE_CATEGORY_CHECK_SQL, name=RULE_CATEGORY_CHECK),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # A regular expression matched against the whole account number string.
    pattern: Mapped[str] = mapped_column(String(PATTERN_MAX_LENGTH), nullable=False)
    division_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("division.id", ondelete="RESTRICT")
    )
    # 1-based position in the account number whose character is looked up in
    # division.code_digit (owner amendment B: any position, any length).
    division_from_digit: Mapped[int | None] = mapped_column(Integer)
    cost_category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cost_category.id", ondelete="RESTRICT")
    )
    # The last two characters are the cost category slot.
    cost_category_from_slot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    in_job_cost: Mapped[bool] = mapped_column(Boolean, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _now()


class TenantPolicy(Base):
    __tablename__ = "tenant_policy"
    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_tenant_policy_tenant_key"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    key: Mapped[str] = mapped_column(String(60), nullable=False)
    value: Mapped[object] = mapped_column(JSONB, nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="RESTRICT")
    )
    decided_at: Mapped[datetime] = _now()
    decision_ref: Mapped[str] = mapped_column(String(200), nullable=False)


class BurdenRate(Base):
    __tablename__ = "burden_rate"
    __table_args__ = (
        Index("ix_burden_rate_tenant_id_effective_from", "tenant_id", "effective_from"),
        CheckConstraint(BURDEN_PERIOD_CHECK_SQL, name=BURDEN_PERIOD_CHECK),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    division_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("division.id", ondelete="RESTRICT")
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)  # exclusive; NULL = open-ended
    rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    basis_note: Mapped[str | None] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _now()
