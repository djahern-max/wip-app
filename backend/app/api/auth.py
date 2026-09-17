"""Login, logout, TOTP, recovery codes, password change, activation (F02, F02.1).

Unauthenticated credential routes (login, TOTP verify and recover, activation)
run the per-IP throttle first, before any argon2 verification."""

from typing import Annotated

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.schemas import ActivateOut, EnrolConfirmOut, EnrolStartOut, SessionOut, UserOut
from app.auth import service
from app.auth.service import Outcome
from app.core.audit import request_meta
from app.core.auth import AnyPrincipal, Principal, clear_session_cookie, set_session_cookie, utcnow
from app.core.config import get_settings
from app.core.db import RequestSession
from app.core.product import PRODUCT_NAME
from app.core.security import MIN_PASSWORD_LENGTH
from app.core.throttle import is_throttled

router = APIRouter(prefix="/auth", tags=["auth"])

INVALID_CREDENTIALS = "invalid credentials"
INVALID_CODE = "invalid code"
INVALID_LINK = "invalid or expired link"
LOCKED = "too many failed attempts; try again later"


class LoginIn(BaseModel):
    email: Annotated[str, Field(min_length=3, max_length=320)]
    password: Annotated[str, Field(min_length=1, max_length=1024)]


class CodeIn(BaseModel):
    code: Annotated[str, Field(min_length=1, max_length=32)]


class PasswordChangeIn(BaseModel):
    current_password: Annotated[str, Field(min_length=1, max_length=1024)]
    new_password: Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)]


class ActivateIn(BaseModel):
    token: Annotated[str, Field(min_length=16, max_length=128)]
    new_password: Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)]


def _error(status_code: int, detail: str) -> JSONResponse:
    """An error body returned (not raised) so the request transaction commits."""
    return JSONResponse(status_code=status_code, content={"detail": detail})


def session_payload(principal: Principal) -> SessionOut:
    return SessionOut(
        user=UserOut(
            id=str(principal.user.id),
            email=principal.user.email,
            display_name=principal.user.display_name,
        ),
        totp=principal.totp_state,
        totp_enrolled=principal.totp_enrolled,
        active_tenant_id=(str(principal.active_tenant_id) if principal.active_tenant_id else None),
        role=principal.role.value if principal.role else None,
        firm_role=principal.firm_role.value if principal.firm_role else None,
    )


@router.post("/login", response_model=SessionOut)
def login(body: LoginIn, request: Request, response: Response, db: RequestSession):
    meta = request_meta(request)
    if is_throttled(db, meta, utcnow()):
        return _error(429, LOCKED)
    result = service.login(
        db,
        email=body.email,
        password=body.password,
        old_token=request.cookies.get(get_settings().session_cookie_name),
        meta=meta,
    )
    if result.outcome is Outcome.locked:
        return _error(429, LOCKED)
    if result.outcome is Outcome.invalid:
        return _error(401, INVALID_CREDENTIALS)
    set_session_cookie(response, result.token)
    return session_payload(result.principal)


@router.post("/logout", status_code=204)
def logout(request: Request, principal: AnyPrincipal, db: RequestSession) -> Response:
    service.logout(db, principal, meta=request_meta(request))
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response


@router.post("/totp/enrol", response_model=EnrolStartOut)
def totp_enrol_start(principal: AnyPrincipal, db: RequestSession):
    secret, uri = service.start_totp_enrolment(db, principal, issuer=PRODUCT_NAME)
    return EnrolStartOut(secret=secret, otpauth_uri=uri)


@router.post("/totp/enrol/confirm", response_model=EnrolConfirmOut)
def totp_enrol_confirm(
    body: CodeIn, request: Request, response: Response, principal: AnyPrincipal, db: RequestSession
):
    result = service.confirm_totp_enrolment(db, principal, body.code, meta=request_meta(request))
    if result.outcome is Outcome.locked:
        return _error(429, LOCKED)
    if result.outcome is Outcome.invalid:
        return _error(400, INVALID_CODE)
    set_session_cookie(response, result.token)
    return EnrolConfirmOut(
        recovery_codes=result.recovery_codes, **session_payload(principal).model_dump()
    )


@router.post("/totp/verify", response_model=SessionOut)
def totp_verify(
    body: CodeIn, request: Request, response: Response, principal: AnyPrincipal, db: RequestSession
):
    meta = request_meta(request)
    if is_throttled(db, meta, utcnow()):
        return _error(429, LOCKED)
    result = service.verify_totp(db, principal, body.code, meta=meta)
    if result.outcome is Outcome.locked:
        return _error(429, LOCKED)
    if result.outcome is Outcome.invalid:
        return _error(401, INVALID_CODE)
    set_session_cookie(response, result.token)
    return session_payload(principal)


@router.post("/totp/recover", response_model=SessionOut)
def totp_recover(
    body: CodeIn, request: Request, response: Response, principal: AnyPrincipal, db: RequestSession
):
    meta = request_meta(request)
    if is_throttled(db, meta, utcnow()):
        return _error(429, LOCKED)
    result = service.use_recovery_code(db, principal, body.code, meta=meta)
    if result.outcome is Outcome.locked:
        return _error(429, LOCKED)
    if result.outcome is Outcome.invalid:
        return _error(401, INVALID_CODE)
    set_session_cookie(response, result.token)
    return session_payload(principal)


@router.post("/password/change", status_code=204)
def password_change(
    body: PasswordChangeIn, request: Request, principal: AnyPrincipal, db: RequestSession
) -> Response:
    """On success every session of the user ends, this one included (F02.1)."""
    ok = service.change_password(
        db,
        principal,
        current_password=body.current_password,
        new_password=body.new_password,
        meta=request_meta(request),
    )
    if not ok:
        return _error(401, INVALID_CREDENTIALS)
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response


@router.post("/activate", response_model=ActivateOut)
def activate(body: ActivateIn, request: Request, response: Response, db: RequestSession):
    """Unauthenticated: the one-time link issued by a firm_admin is the proof (D-16).
    Sets the password; for a firm user without TOTP, opens an enrolment-only
    session (cookie) and answers ``next: totp_enrol``; otherwise ``next: login``."""
    meta = request_meta(request)
    if is_throttled(db, meta, utcnow()):
        return _error(429, LOCKED)
    result = service.redeem_activation_link(
        db, token=body.token, new_password=body.new_password, meta=meta
    )
    if result.outcome is not Outcome.ok:
        return _error(400, INVALID_LINK)
    if result.token is not None:
        set_session_cookie(response, result.token)
    return ActivateOut(next=result.next)
