"""Login, logout, TOTP, recovery codes, passwords (F02)."""

from typing import Annotated

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth import service
from app.auth.service import Outcome
from app.core.audit import request_meta
from app.core.auth import AnyPrincipal, Principal, clear_session_cookie, set_session_cookie
from app.core.config import get_settings
from app.core.db import RequestSession
from app.core.product import PRODUCT_NAME
from app.core.security import MIN_PASSWORD_LENGTH

router = APIRouter(prefix="/auth", tags=["auth"])

INVALID_CREDENTIALS = "invalid credentials"
INVALID_CODE = "invalid code"
LOCKED = "too many failed attempts; try again later"


class LoginIn(BaseModel):
    email: Annotated[str, Field(min_length=3, max_length=320)]
    password: Annotated[str, Field(min_length=1, max_length=1024)]


class CodeIn(BaseModel):
    code: Annotated[str, Field(min_length=1, max_length=32)]


class PasswordChangeIn(BaseModel):
    current_password: Annotated[str, Field(min_length=1, max_length=1024)]
    new_password: Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)]


class PasswordResetIn(BaseModel):
    token: Annotated[str, Field(min_length=16, max_length=128)]
    new_password: Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)]


def _error(status_code: int, detail: str) -> JSONResponse:
    """An error body returned (not raised) so the request transaction commits."""
    return JSONResponse(status_code=status_code, content={"detail": detail})


def session_payload(principal: Principal) -> dict:
    return {
        "user": {
            "id": str(principal.user.id),
            "email": principal.user.email,
            "display_name": principal.user.display_name,
        },
        "totp": principal.totp_state,
        "totp_enrolled": principal.totp_enrolled,
        "active_tenant_id": (
            str(principal.active_tenant_id) if principal.active_tenant_id else None
        ),
        "role": principal.role.value if principal.role else None,
        "firm_role": principal.firm_role.value if principal.firm_role else None,
    }


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: RequestSession) -> dict:
    result = service.login(
        db,
        email=body.email,
        password=body.password,
        old_token=request.cookies.get(get_settings().session_cookie_name),
        meta=request_meta(request),
    )
    if result.outcome is Outcome.locked:
        return _error(429, LOCKED)
    if result.outcome is Outcome.invalid:
        return _error(401, INVALID_CREDENTIALS)
    set_session_cookie(response, result.token)
    return {
        "user": {
            "id": str(result.user.id),
            "email": result.user.email,
            "display_name": result.user.display_name,
        },
        "totp": result.totp,
        "totp_enrolled": result.user.totp_enrolled_at is not None,
        "active_tenant_id": str(result.active_tenant_id) if result.active_tenant_id else None,
        "role": result.role.value if result.role else None,
    }


@router.post("/logout", status_code=204)
def logout(request: Request, principal: AnyPrincipal, db: RequestSession) -> Response:
    service.logout(db, principal, meta=request_meta(request))
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response


@router.post("/totp/enrol")
def totp_enrol_start(principal: AnyPrincipal, db: RequestSession) -> dict:
    secret, uri = service.start_totp_enrolment(db, principal, issuer=PRODUCT_NAME)
    return {"secret": secret, "otpauth_uri": uri}


@router.post("/totp/enrol/confirm")
def totp_enrol_confirm(
    body: CodeIn, request: Request, response: Response, principal: AnyPrincipal, db: RequestSession
) -> dict:
    result = service.confirm_totp_enrolment(db, principal, body.code, meta=request_meta(request))
    if result.outcome is Outcome.locked:
        return _error(429, LOCKED)
    if result.outcome is Outcome.invalid:
        return _error(400, INVALID_CODE)
    set_session_cookie(response, result.token)
    return {"recovery_codes": result.recovery_codes, **session_payload(principal)}


@router.post("/totp/verify")
def totp_verify(
    body: CodeIn, request: Request, response: Response, principal: AnyPrincipal, db: RequestSession
) -> dict:
    result = service.verify_totp(db, principal, body.code, meta=request_meta(request))
    if result.outcome is Outcome.locked:
        return _error(429, LOCKED)
    if result.outcome is Outcome.invalid:
        return _error(401, INVALID_CODE)
    set_session_cookie(response, result.token)
    return session_payload(principal)


@router.post("/totp/recover")
def totp_recover(
    body: CodeIn, request: Request, response: Response, principal: AnyPrincipal, db: RequestSession
) -> dict:
    result = service.use_recovery_code(db, principal, body.code, meta=request_meta(request))
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
    ok = service.change_password(
        db,
        principal,
        current_password=body.current_password,
        new_password=body.new_password,
        meta=request_meta(request),
    )
    if not ok:
        return _error(401, INVALID_CREDENTIALS)
    return Response(status_code=204)


@router.post("/password/reset", status_code=204)
def password_reset(body: PasswordResetIn, request: Request, db: RequestSession) -> Response:
    """Unauthenticated: the one-time link issued by a firm_admin is the proof."""
    service.complete_password_reset(
        db, token=body.token, new_password=body.new_password, meta=request_meta(request)
    )
    return Response(status_code=204)
