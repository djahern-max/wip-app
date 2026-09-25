"""F06: the shape of the estimates API (D-22): money as strings with cents, the
display fields beside the machine values, sentences rather than codes, filters."""

import re
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tests.config_helpers import run_until_quiet
from tests.conftest import Seed
from tests.estimate_helpers import ELM, TURLEY, configure_tenant, upload_template

MONEY = re.compile(r"^-?\d+\.\d\d$")
MONEY_FIELDS = ("price", "hours", "cost", "amount", "kept_total")


@pytest.fixture
def loaded(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant
) -> dict:
    configure_tenant(rw_engine, seed, fresh_tenant, basis=False)
    client = login_as("rotate_me", tenant=fresh_tenant)
    upload_template(client, ELM.read_bytes(), "elm.xlsx")
    upload_template(client, TURLEY.read_bytes(), "turley.xlsx")
    run_until_quiet(rw_engine)
    return {"client": client, "id": fresh_tenant}


def _money_values(obj, path="$") -> list[tuple[str, object]]:
    """Every value under a money-named key, anywhere in the body."""
    out: list[tuple[str, object]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            money_key = k in MONEY_FIELDS or k in ("omitted", "eac_in_basis")
            if money_key or (k.startswith("kept_") and not k.endswith("_label")):
                out.append((f"{path}.{k}", v))
            out.extend(_money_values(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_money_values(v, f"{path}[{i}]"))
    return out


def test_money_is_strings_with_cents_and_sentences_carry_no_code(loaded: dict) -> None:
    listing = loaded["client"].get("/api/estimates").json()
    assert listing["total"] == 2 and [e["external_id"] for e in listing["estimates"]] == [
        "EST6115758",
        "EST6120638",
    ]
    detail = loaded["client"].get(f"/api/estimates/{listing['estimates'][0]['id']}").json()
    for body in (listing, detail):
        for where, value in _money_values(body):
            if value is None:
                assert where.endswith((".eac_in_basis", ".hours", ".kept_total")), where
                continue
            assert isinstance(value, str) and MONEY.match(value), (where, value)
    # The WIP basis is not decided in this tenant: no EAC figure, the column says so.
    t = detail["totals"]
    assert (t["basis_decided"], t["eac_in_basis"]) == (False, None)
    assert {c["in_basis_label"] for c in t["by_category"]} == {"Not decided"}
    assert {c["in_basis"] for c in t["by_category"]} == {"not_decided"}
    for body in (listing, detail):
        text = str(body)
        for issue in re.findall(r"'message': '([^']*)'", text):
            assert "EST_" not in issue
    assert detail["status_label"] == "Sold" and detail["status_norm"] == "sold"
    assert detail["work_areas"][16]["kept_label"] == "Omitted"
    assert detail["versions_list"][0]["original_filename"] == "elm.xlsx"


def test_filters_by_status_and_estimator(loaded: dict) -> None:
    c = loaded["client"]
    assert c.get("/api/estimates?status=sold").json()["total"] == 2
    assert c.get("/api/estimates?status=pending").json()["total"] == 0
    assert c.get("/api/estimates?estimator=Stephanie%20Sanford").json()["total"] == 1
    assert c.get("/api/estimates").json()["estimators"] == ["Stephanie Sanford"]
    assert c.get("/api/estimates?status=won").status_code == 422
    assert c.get("/api/estimates/not-a-uuid").status_code == 422


def test_every_role_reads_and_only_uploaders_upload(
    loaded: dict, seed: Seed, login_as: Callable[..., TestClient], owner_engine: Engine
) -> None:
    from app.core.db import tenant_session
    from app.tenancy.models import Membership, Role

    with tenant_session(owner_engine, loaded["id"]) as s:
        s.add(
            Membership(
                tenant_id=loaded["id"],
                user_id=seed.users["client_viewer"].id,
                role=Role.client_viewer,
            )
        )
    viewer = login_as("client_viewer", tenant=loaded["id"])
    r = viewer.get("/api/estimates")
    assert r.status_code == 200 and r.json()["total"] == 2
    detail_id = r.json()["estimates"][0]["id"]
    assert viewer.get(f"/api/estimates/{detail_id}").status_code == 200
    r = viewer.post(
        "/api/imports",
        data={"source_kind": "estimate_template"},
        files={"file": ("x.xlsx", ELM.read_bytes(), "application/octet-stream")},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 403
