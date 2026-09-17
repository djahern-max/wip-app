from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy import Engine, text

from app.core.db import get_engine, untenanted_session
from app.core.product import PRODUCT_NAME

router = APIRouter()


@router.get("/health")
def health(engine: Annotated[Engine, Depends(get_engine)], response: Response) -> dict:
    """Liveness plus a database round-trip. No tenant context: touches no tenant table."""
    try:
        with untenanted_session(engine) as session:
            session.execute(text("SELECT 1"))
        db = "ok"
    except Exception:  # noqa: BLE001 - any DB failure is reported, not raised
        db = "unavailable"
        response.status_code = 503
    return {"status": "ok" if db == "ok" else "degraded", "product": PRODUCT_NAME, "db": db}
