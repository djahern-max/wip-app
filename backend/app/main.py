import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.api.admin import router as admin_router
from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.session import router as session_router
from app.auth.service import AuthError
from app.core.auth import CSRF_HEADER, CSRF_METHODS
from app.core.crypto import CryptoError
from app.core.db import create_app_engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.engine = create_app_engine()
    try:
        yield
    finally:
        app.state.engine.dispose()


async def _auth_error(_: Request, exc: AuthError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


async def _crypto_error(_: Request, exc: CryptoError) -> JSONResponse:
    # Configuration problem; the message carries no key material.
    return JSONResponse(status_code=500, content={"detail": f"encryption: {exc}"})


def create_app() -> FastAPI:
    app = FastAPI(title="WIP API", lifespan=lifespan)
    app.add_exception_handler(AuthError, _auth_error)
    app.add_exception_handler(CryptoError, _crypto_error)

    @app.middleware("http")
    async def request_id_and_csrf(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Server-generated id, echoed back and written to audit rows.
        request.state.request_id = uuid.uuid4().hex
        if (
            request.method in CSRF_METHODS
            and request.url.path.startswith("/api/")
            and not request.headers.get(CSRF_HEADER)
        ):
            response: Response = JSONResponse(
                status_code=403, content={"detail": f"missing {CSRF_HEADER} header"}
            )
        else:
            response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    for router in (health_router, auth_router, session_router, admin_router, audit_router):
        app.include_router(router, prefix="/api")
    if "pytest" in sys.modules:  # test-only probe routes (F02 brief)
        from app.api.probes import build_probe_router

        app.include_router(build_probe_router(), prefix="/api")
    return app


app = create_app()
