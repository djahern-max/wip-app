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
    """An uploaded file (F03). ``object_key`` is deliberately absent."""

    id: str
    source_kind: str
    sha256: str
    byte_size: int
    original_filename: str
    content_type: str | None
    uploaded_by: str | None
    uploaded_by_email: str | None  # from the global user table; never a credential
    uploaded_at: str
    status: str
    rows_loaded: int
    rows_rejected: int
    error: str | None
    processed_at: str | None


class ImportUploadOut(_Out):
    batch: ImportBatchOut
    duplicate: bool


class SourceKindOut(_Out):
    name: str
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
