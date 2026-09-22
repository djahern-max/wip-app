"""Allow-list response schemas (F02.1). Every response that includes a user, a
membership, a tenant, or an audit row is built from one of these; FastAPI's
``response_model`` drops anything not declared, so a credential column can never
ride along by accident. ``tests/test_zz_response_scan.py`` checks every JSON body
the suite produces for the credential column names and values."""

from pydantic import BaseModel, ConfigDict


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserOut(_Out):
    id: str
    email: str
    display_name: str


class SessionOut(_Out):
    user: UserOut
    totp: str
    totp_enrolled: bool
    active_tenant_id: str | None
    role: str | None
    firm_role: str | None


class EnrolStartOut(_Out):
    """The only time the TOTP secret leaves the server (show once)."""

    secret: str
    otpauth_uri: str


class EnrolConfirmOut(SessionOut):
    """Recovery codes are shown exactly once, here."""

    recovery_codes: list[str]


class TenantOut(_Out):
    tenant_id: str
    name: str
    slug: str
    role: str


class TenantEnteredOut(_Out):
    active_tenant_id: str
    role: str


class ActivateOut(_Out):
    """``next`` is ``totp_enrol`` (cookie set, enrolment-only session) or ``login``."""

    next: str


class ActivationLinkOut(_Out):
    """Handed to the admin to pass on out of band; no e-mail is sent (D-16)."""

    activation_url: str


class UserCreatedOut(UserOut):
    activation_url: str


class TenantUserOut(_Out):
    user_id: str
    email: str
    display_name: str
    role: str | None
    totp_enrolled: bool


class MembershipOut(_Out):
    membership_id: str
    tenant_id: str
    user_id: str
    role: str | None  # None = entry row for a firm user; the effective role is theirs


class FirmMembershipOut(_Out):
    firm_membership_id: str
    firm_id: str
    user_id: str
    email: str
    display_name: str
    role: str
    totp_enrolled: bool


class TenantCreatedOut(_Out):
    tenant_id: str
    firm_id: str
    name: str
    slug: str


class ImportBatchOut(_Out):
    """An uploaded file (F03). ``object_key`` is deliberately absent. ``source_label``,
    ``status_label`` and ``message`` are what a person sees (D-22); ``source_kind``,
    ``status`` and ``error_detail`` are the machine values for OPERATIONS."""

    id: str
    source_kind: str
    source_label: str
    sha256: str
    byte_size: int
    original_filename: str
    content_type: str | None
    uploaded_by: str | None
    uploaded_by_email: str | None  # from the global user table; never a credential
    uploaded_at: str
    status: str
    status_label: str
    rows_loaded: int
    rows_rejected: int
    message: str | None
    error_detail: str | None
    processed_at: str | None
    # F04 (owner amendment C): the after_load task's state; machine status and label.
    followup_status: str | None
    followup_label: str | None


class ImportUploadOut(_Out):
    batch: ImportBatchOut
    duplicate: bool


class SourceKindOut(_Out):
    name: str
    label: str
    description: str
    extensions: list[str]


class AuditRowOut(_Out):
    id: str
    occurred_at: str
    actor_user_id: str | None
    actor_role: str | None
    action: str
    entity_type: str
    entity_id: str | None
    detail: dict | None
    ip: str | None
    request_id: str | None
    tenant_id: str | None = None
    firm_id: str | None = None


# --- F04 · tenant configuration ---------------------------------------------------------------


class DivisionOut(_Out):
    id: str
    code: str
    name: str
    code_digit: str | None
    active: bool
    sort_order: int


class CostCategoryOut(_Out):
    id: str
    slot: str
    name: str
    active: bool
    sort_order: int


class AccountMapOut(_Out):
    division_id: str | None
    division_code: str | None
    cost_category_id: str | None
    cost_category_name: str | None
    in_job_cost: bool
    status: str
    status_label: str
    suggested_by_rule: str | None
    confirmed_by_email: str | None
    confirmed_at: str | None


class GlAccountOut(_Out):
    id: str
    account_no: str
    name: str
    ledger_type: str
    active: bool
    cost_code: str | None
    map: AccountMapOut | None
    map_status_label: str  # "Unmapped", "Suggested", "Confirmed"


class AccountsOut(_Out):
    total_active: int
    unmapped_count: int
    suggested_count: int
    confirmed_count: int
    accounts: list[GlAccountOut]


class ConfirmAllOut(_Out):
    confirmed: int
    unmapped_count: int


class CostCodeCellOut(_Out):
    cost_category_id: str
    slot: str
    code: str | None
    accounts: list[dict]


class CostCodeRowOut(_Out):
    division_id: str
    code: str
    name: str
    code_digit: str | None
    cells: list[CostCodeCellOut]


class CostCodesOut(_Out):
    categories: list[CostCategoryOut]
    rows: list[CostCodeRowOut]


class BurdenRateOut(_Out):
    id: str
    division_id: str | None
    division_code: str | None
    effective_from: str
    effective_to: str | None
    rate: str  # a fraction as a string, Decimal end to end
    basis_note: str | None
    active: bool


class PolicyOut(_Out):
    key: str
    label: str
    kind: str
    description: str
    decided: bool
    value: object | None  # money as a string
    decided_by_email: str | None
    decided_at: str | None
    decision_ref: str | None


class SuggestRuleOut(_Out):
    id: str
    sort_order: int
    name: str
    pattern: str
    division_code: str | None
    division_from_digit: int | None
    cost_category_name: str | None
    cost_category_from_slot: bool
    in_job_cost: bool
    active: bool


class QboConnectOut(_Out):
    """Where to send the browser. The URL carries the single-use ``state``."""

    authorization_url: str


class QboEntityCountOut(_Out):
    entity: str
    current: int  # current, non-deleted raw records
    skipped: int | None  # payloads the normalizer could not read (normalized entities only)


class QboMonthTotalsOut(_Out):
    """Money as strings with cents (D-22); the page never parses a number."""

    month: str
    invoices: str
    credit_memos: str
    sales_receipts: str
    payments: str


class QboSyncRunOut(_Out):
    kind: str
    kind_label: str
    outcome: str | None
    outcome_label: str
    started_at: str
    finished_at: str | None
    error_detail: str | None
    message: str | None


class QboAttentionOut(_Out):
    """One item needing attention, words first: ``label`` says what, ``detail`` how much."""

    code: str
    label: str
    detail: str | None


class QboCopyOut(_Out):
    """What the platform holds for the connected company."""

    entities: list[QboEntityCountOut]
    month_totals: list[QboMonthTotalsOut]
    attention: list[QboAttentionOut]
    last_sync: QboSyncRunOut | None
    backfill: QboSyncRunOut | None
    backfill_needed: bool


class QboStatusOut(_Out):
    """The QuickBooks connection of the active tenant (F05). Words first (D-22):
    ``status_label`` and ``message`` are what a person sees; ``status`` and
    ``error_detail`` are the machine values. No token, key id or ``state`` field
    exists here. ``can_manage`` says whether this user may connect or disconnect."""

    status: str
    status_label: str
    message: str | None
    error_detail: str | None
    company_name: str | None
    environment: str | None
    connected_company: bool
    last_success_at: str | None
    can_manage: bool
    result: str | None
    result_message: str | None
    held: QboCopyOut | None
    # F05.1: when a webhook delivery last named this company, and how many arrived in
    # the last 24 hours (0 when none, or when no company is connected).
    last_webhook_at: str | None = None
    webhooks_24h: int = 0


class QboSyncRequestOut(_Out):
    """``created`` is false when the same request was already queued or running."""

    created: bool
    message: str
