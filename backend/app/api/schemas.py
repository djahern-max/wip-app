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
    # F06: rows the file could not load and file-level facts, one sentence each.
    issues: list["ImportIssueOut"]


class ImportIssueOut(_Out):
    code: str | None  # an EST_* code when one applies; the machine value
    message: str  # the sentence a person reads (D-22)
    row_number: int | None


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
    inactive_count: int  # F06.1: rows returned only with include_inactive=true
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


# --- F06 estimates: money as strings with cents (D-22) ------------------------------------------


class EstimateIssueOut(_Out):
    code: str  # machine value; never shown without its sentence
    message: str


class EstimateRowOut(_Out):
    id: str
    external_id: str
    estimator: str | None
    client_name: str | None
    jobsite: str | None
    name: str
    status: str  # as received
    status_norm: str | None  # machine value
    status_label: str  # what a person sees
    price: str
    estimate_date: str | None
    versions: int
    attention: list[EstimateIssueOut]


class EstimatesOut(_Out):
    estimates: list[EstimateRowOut]
    estimators: list[str]
    total: int


class EstimateCostLineOut(_Out):
    cost_code: str
    category_name: str | None  # None: the code is not on the grid
    division_code: str | None
    hours: str | None
    amount: str
    notes: str | None


class EstimateWorkAreaOut(_Out):
    order_no: int
    name: str
    kept: bool
    kept_label: str
    change_order_suggested: bool
    hours: str
    cost: str
    price: str
    notes: str | None
    burden: str | None  # F06.1: computed on read; 0.00 when none applies; None = not computed
    lines: list[EstimateCostLineOut]


class EstimateVersionOut(_Out):
    id: str
    version_no: int
    received_at: str
    is_baseline: bool
    has_work_areas: bool
    kept_total: str | None
    status_label: str
    import_batch_id: str
    original_filename: str | None


class EstimateCategoryTotalOut(_Out):
    slot: str | None  # None: the "Unknown cost code" row
    name: str
    amount: str  # as estimated: the cost lines exactly as loaded
    amount_with_burden: str | None  # F06.1; differs only on Labor Burden; None = not computed
    hours: str
    in_basis: str  # yes | no | not_decided (machine value)
    in_basis_label: str


class EstimateBurdenDivisionOut(_Out):
    """F06.1: one division's labor and burden, with the rate row that produced it."""

    division_code: str
    labor_amount: str
    rate: str | None  # the stored fraction, e.g. "0.1959"; None = no rate in force
    rate_percent: str | None  # "19.59"
    rate_effective_from: str | None
    basis_note: str | None
    burden: str | None


class EstimateTotalsOut(_Out):
    kept_original: str
    kept_change_orders: str
    kept_total: str
    omitted: str
    kept_hours: str
    kept_cost: str
    eac_in_basis: str | None  # with burden (F06.1); None: basis not decided, or not computed
    basis_decided: bool
    by_category: list[EstimateCategoryTotalOut]
    # F06.1 (D-05, D-34): burden is computed on read and never stored.
    eac_in_basis_as_estimated: str | None
    eac_not_computed: bool  # Labor Burden is in the basis and burden is not computed
    cost_total_as_estimated: str
    cost_total_with_burden: str | None
    burden_total: str | None
    burden_computed: bool
    burden_date: str | None  # the date the rates were read at
    burden_date_source: str  # estimate_date | received | none
    burden_by_division: list[EstimateBurdenDivisionOut]


class EstimateJobOut(_Out):
    """F07: the job an estimate is on, for the detail's "Job:" line."""

    id: str
    name: str
    role: str
    role_label: str


class EstimateDetailOut(EstimateRowOut):
    versions_list: list[EstimateVersionOut]
    baseline_version_no: int | None
    work_areas: list[EstimateWorkAreaOut]
    totals: EstimateTotalsOut
    job: EstimateJobOut | None = None  # F07
    to_review: bool = False  # F07: sold and on no job


# --- F07 jobs and the crosswalk: money as strings with cents (D-22) --------------------------


class DivisionChoiceOut(_Out):
    id: str
    code: str
    name: str


class ChoiceOut(_Out):
    value: str  # machine value
    label: str


class JobEstimateOut(_Out):
    estimate_id: str
    external_id: str
    name: str
    status_label: str
    role: str  # machine value
    role_label: str
    price: str
    note: str | None
    attached_at: str
    attached_by: str | None
    work_areas_loaded: bool
    eac_in_basis: str | None  # None: not computed (or the estimate is ignored)


class JobAliasOut(_Out):
    id: str
    system: str  # machine value
    system_label: str
    external_id: str
    display_name: str | None  # the QuickBooks row's name; None for an estimate id
    kind_label: str | None  # Project | Sub-customer | Customer
    linked_at: str
    linked_by: str | None


class JobWorkAreaOut(_Out):
    id: str
    order_no: int
    name: str
    kept: bool
    kept_label: str
    price: str
    kind: str | None  # confirmed kind (machine value); None = not confirmed
    suggested_kind: str | None  # None for an omitted work area
    kind_label: str  # "Original", "Change order (suggested)", "Omitted"…
    confirmed: bool


class JobRowOut(_Out):
    id: str
    name: str
    customer_name: str | None
    division_id: str | None
    division_code: str | None
    revenue_method: str  # machine value
    revenue_method_label: str
    status: str  # machine value
    status_label: str
    sold_on: str
    sold_on_set_when_created: bool
    revised_contract: str | None  # None: not shown (T&M, pool, no original)
    revised_contract_note: str | None
    unapproved_change_orders: str | None
    eac_in_basis: str | None
    eac_note: str | None
    to_confirm: int
    qbo_linked: bool
    qbo_names: list[str]
    attention: list[EstimateIssueOut]


class JobsOut(_Out):
    jobs: list[JobRowOut]
    total: int
    to_review: int
    divisions: list[DivisionChoiceOut]
    revenue_methods: list[ChoiceOut]
    statuses: list[ChoiceOut]
    ledger_items: list[EstimateIssueOut]


class JobDetailOut(JobRowOut):
    notes: str | None
    created_at: str
    created_by: str | None
    estimates: list[JobEstimateOut]
    aliases: list[JobAliasOut]
    original_external_id: str | None
    work_areas: list[JobWorkAreaOut]
    work_areas_loaded: bool
    divisions: list[DivisionChoiceOut]
    revenue_methods: list[ChoiceOut]
    statuses: list[ChoiceOut]


class ConfirmSuggestedOut(JobDetailOut):
    """F07.1: the job detail after "Confirm all as suggested", with what the press did."""

    confirmed: int
    skipped: int  # kept work areas with no suggestion, left to confirm
    message: str  # one sentence


class AttachCandidateOut(_Out):
    job_id: str
    job_name: str
    reasons: list[str]
    same_customer: bool


class ReviewEntryOut(_Out):
    estimate_id: str
    external_id: str
    estimator: str | None
    client_name: str | None
    jobsite: str | None
    name: str
    price: str
    estimate_date: str | None
    division_suggestion_id: str | None
    division_suggestion_code: str | None
    candidates: list[AttachCandidateOut]
    attention: list[EstimateIssueOut]


class ReviewOut(_Out):
    entries: list[ReviewEntryOut]
    total: int
    divisions: list[DivisionChoiceOut]
    roles: list[ChoiceOut]


class QboRowOut(_Out):
    customer_id: str
    external_id: str
    display_name: str
    parent_name: str | None
    kind_label: str
    reason: str | None  # None in search results: a person chose it from the list


class QboRowsOut(_Out):
    rows: list[QboRowOut]


class JobRefOut(_Out):
    id: str
    name: str


class CustomerPickOut(_Out):
    """One row of the picker (F07.2, D-37): the search, and the Tracked list."""

    customer_id: str
    external_id: str
    display_name: str
    parent_name: str | None
    kind_label: str  # Project | Sub-customer | Customer
    active: bool
    billing_count: int
    payment_count: int
    tracked: bool
    job: JobRefOut | None
    needs_job: str | None  # the LEDGER_PROJECT_NO_JOB sentence, when this row raises it


class CustomersPageOut(_Out):
    rows: list[CustomerPickOut]
    page: int
    pages: int  # 0 when nothing matched or nothing was asked
    total: int
    page_size: int


class TrackedOut(_Out):
    rows: list[CustomerPickOut]


class DuplicateSideOut(_Out):
    customer_id: str
    external_id: str
    display_name: str
    billing_count: int
    payment_count: int


class DuplicatePairOut(_Out):
    code: str
    message: str
    a: DuplicateSideOut
    b: DuplicateSideOut


class DuplicatesOut(_Out):
    pairs: list[DuplicatePairOut]
