"""F05: the OAuth ``state`` value and the authorization URL (no I/O)."""

import base64
import uuid
from urllib.parse import parse_qs, urlparse

from app.integrations.qbo.constants import AUTHORIZATION_URL
from app.integrations.qbo.oauth import (
    authorization_url,
    new_state,
    state_sha256,
    tenant_id_from_state,
)


def test_state_carries_the_tenant_and_is_different_every_time() -> None:
    tenant = uuid.uuid4()
    a, b = new_state(tenant), new_state(tenant)
    assert a != b
    assert tenant_id_from_state(a) == tenant == tenant_id_from_state(b)
    assert len(base64.urlsafe_b64decode(a)) == 48  # 16 of tenant id + 32 random
    assert state_sha256(a) != state_sha256(b) and len(state_sha256(a)) == 64


def test_anything_that_is_not_one_of_our_states_names_no_tenant() -> None:
    for bad in ("", "x", "not base64 !!", base64.urlsafe_b64encode(b"short").decode(), "é" * 10):
        assert tenant_id_from_state(bad) is None
    too_long = base64.urlsafe_b64encode(b"\x00" * 49).decode()
    assert tenant_id_from_state(too_long) is None


def test_authorization_url_asks_for_the_accounting_scope_only() -> None:
    url = authorization_url(client_id="cid", redirect_uri="https://x.test/cb", state="s t")
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == AUTHORIZATION_URL
    assert parse_qs(parsed.query) == {
        "client_id": ["cid"],
        "response_type": ["code"],
        "scope": ["com.intuit.quickbooks.accounting"],
        "redirect_uri": ["https://x.test/cb"],
        "state": ["s t"],
    }
