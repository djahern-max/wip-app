"""OAuth helpers with no I/O: the authorization URL and the ``state`` value.

``state`` = base64url(16 bytes of tenant id ‖ 32 random bytes). ``connection`` is
under forced RLS, so a callback that arrives without a session cannot find the
pending row unless the value itself says which tenant to look in; the tenant id is
an opaque identifier, not a secret, and a forged one finds no matching hash. Only
the SHA-256 of the whole value is stored.
"""

import base64
import binascii
import hashlib
import secrets
from urllib.parse import urlencode
from uuid import UUID

from app.integrations.qbo.constants import AUTHORIZATION_URL, SCOPE

_RANDOM_BYTES = 32
_STATE_BYTES = 16 + _RANDOM_BYTES


def new_state(tenant_id: UUID) -> str:
    raw = tenant_id.bytes + secrets.token_bytes(_RANDOM_BYTES)
    return base64.urlsafe_b64encode(raw).decode()


def state_sha256(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def tenant_id_from_state(state: str) -> UUID | None:
    """The tenant to look in, or ``None`` for anything that is not one of our values."""
    try:
        raw = base64.b64decode(state.encode(), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return None
    if len(raw) != _STATE_BYTES:
        return None
    return UUID(bytes=raw[:16])


def authorization_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": SCOPE,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{AUTHORIZATION_URL}?{query}"
