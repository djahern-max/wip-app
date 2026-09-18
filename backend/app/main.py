import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError

from app.api.admin import router as admin_router
from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.imports import router as imports_router
from app.api.session import router as session_router
from app.auth.service import AuthError
from app.core.auth import CSRF_HEADER, CSRF_METHODS
from app.core.config import get_settings, require_object_store_settings
from app.core.crypto import CryptoError
from app.core.db import create_app_engine, describe_db_error
from app.core.storage import build_object_store

log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.engine = create_app_engine()
    app.state.object_store = build_object_store(get_settings())
    try:
        yield
    finally:
        app.state.engine.dispose()


async def _auth_error(_: Request, exc: AuthError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


async def _crypto_error(_: Request, exc: CryptoError) -> JSONResponse:
    # Configuration problem; the message carries no key material.
    return JSONResponse(status_code=500, content={"detail": f"encryption: {exc}"})


async def _db_error(request: Request, exc: DBAPIError) -> JSONResponse:
    """An unexpected database error: log the SQLSTATE and primary message only (no
    statement, parameters or DETAIL), answer generically."""
    log.error("%s request_id=%s", describe_db_error(exc), request.state.request_id)
    return JSONResponse(status_code=500, content={"detail": "database error"})


def _origin_allowed(request: Request, app_origin: str) -> bool:
    """F02.1: no cross-origin request with credentials. A browser request carries
    ``Origin`` on every state-changing call; it must equal the app's own origin.
    Requests without ``Origin`` (the CLI, curl) pass and still need the CSRF header."""
    origin = request.headers.get("origin")
    return origin is None or origin.rstrip("/") == app_origin


def create_app() -> FastAPI:
    # Refuse to start without the required environment (names only in the message).
    settings = get_settings()
    require_object_store_settings(settings)  # F03: OBJECT_STORE=s3 needs its credentials
    app_origin = settings.app_base_url.rstrip("/")

    app = FastAPI(title="WIP API", lifespan=lifespan)
    app.add_exception_handler(AuthError, _auth_error)
    app.add_exception_handler(CryptoError, _crypto_error)
    app.add_exception_handler(DBAPIError, _db_error)

    @app.middleware("http")
    async def request_id_csrf_and_origin(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Server-generated id, echoed back and written to audit rows.
        request.state.request_id = uuid.uuid4().hex
        is_preflight = request.method == "OPTIONS" and (
            "origin" in request.headers and "access-control-request-method" in request.headers
        )
        response: Response
        if is_preflight or (
            request.method in CSRF_METHODS and not _origin_allowed(request, app_origin)
        ):
            # No Access-Control-* headers are ever emitted: the browser refuses.
            response = JSONResponse(status_code=403, content={"detail": "cross-origin refused"})
        elif (
            request.method in CSRF_METHODS
            and request.url.path.startswith("/api/")
            and not request.headers.get(CSRF_HEADER)
        ):
            response = JSONResponse(
                status_code=403, content={"detail": f"missing {CSRF_HEADER} header"}
            )
        else:
            response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    for router in (
        health_router,
        auth_router,
        session_router,
        admin_router,
        audit_router,
        imports_router,
    ):
        app.include_router(router, prefix="/api")
    return app


app = create_app()
