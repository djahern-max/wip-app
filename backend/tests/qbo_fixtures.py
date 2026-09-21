"""Loaders for the recorded sandbox fixtures (tests/fixtures/qbo_sandbox)."""

from pathlib import Path

from app.core.jsoncodec import json_loads

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "qbo_sandbox"


def fixture(name: str):
    return json_loads((FIXTURE_DIR / f"{name}.json").read_text())


def records(entity: str) -> list[dict]:
    return fixture(entity)


def record(entity: str, external_id: str) -> dict:
    return next(r for r in records(entity) if r["Id"] == external_id)


def cdc_records(body: dict, entity: str) -> list[dict]:
    return [
        r
        for block in body.get("CDCResponse", [])
        for q in block.get("QueryResponse", [])
        for r in q.get(entity, [])
    ]
