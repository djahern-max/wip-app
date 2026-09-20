"""Runs last (file order, before test_zz_log_leaks): every JSON response body the
suite received is scanned for the credential column names and for the stored
values of those columns, and the audit tables for activation tokens (F02.1)."""

import base64
import json

from sqlalchemy import Engine, text

from app.core.security import sha256_hex
from app.tenancy.models import CREDENTIAL_COLUMNS
from tests.leaks import LEAKS
from tests.responses import RESPONSES

FORBIDDEN_NAMES = (
    *CREDENTIAL_COLUMNS,
    "password_reset_token_hash",
    "password_reset_expires_at",
    "token_hash",
)


def _json_bodies() -> list[tuple[str, str]]:
    out = []
    for r in RESPONSES:
        body = r.body.strip()
        if body.startswith(("{", "[")):
            out.append((f"{r.method} {r.path} -> {r.status}", body))
    return out


def test_suite_produced_json_responses() -> None:
    assert len(_json_bodies()) > 200


def test_no_credential_column_name_in_any_json_body() -> None:
    hits = []
    for where, body in _json_bodies():
        for name in FORBIDDEN_NAMES:
            if f'"{name}"' in body:
                hits.append(f"{name} in {where}: {body[:120]}")
    assert hits == []


def _stored_credential_values(engine: Engine) -> dict[str, set[str]]:
    values: dict[str, set[str]] = {name: set() for name in CREDENTIAL_COLUMNS}
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT password_hash, totp_secret_enc, totp_key_id, recovery_code_hashes, "
                'activation_token_hash FROM "user"'
            )
        ).all()
    for pw, enc, kid, hashes, act in rows:
        if pw:
            values["password_hash"].add(pw)
        if enc:
            values["totp_secret_enc"].add(base64.b64encode(enc).decode())
            values["totp_secret_enc"].add(bytes(enc).hex())
        if kid:
            values["totp_key_id"].add(f'"{kid}"')
        for h in hashes or []:
            values["recovery_code_hashes"].add(h)
        if act:
            values["activation_token_hash"].add(act)
    for token in LEAKS["activation_token"]:
        values["activation_token_hash"].add(sha256_hex(token))
    for token in LEAKS["session_token"]:
        values.setdefault("session_token_hash", set()).add(sha256_hex(token))
    # F03: connection tokens in clear (the ciphertext never leaves a service function).
    values["connection_token"] = set(LEAKS["connection_token"])
    # F05: an authorization code and the client secret never appear in a body at all.
    values["oauth_code"] = set(LEAKS["oauth_code"])
    values["client_secret"] = set(LEAKS["client_secret"])
    return values


def test_no_stored_credential_value_in_any_json_body(seed, owner_engine: Engine) -> None:
    values = _stored_credential_values(owner_engine)
    assert values["password_hash"] and values["recovery_code_hashes"]
    assert values["connection_token"], "no connection token was recorded by the suite"
    hits = []
    for where, body in _json_bodies():
        for kind, vals in values.items():
            for v in vals:
                if v and v in body:
                    hits.append(f"{kind} in {where}: {body[:120]}")
                    break
    assert hits == []


def test_activation_tokens_never_reach_the_audit_tables(seed, owner_engine: Engine) -> None:
    tokens = LEAKS["activation_token"]
    assert tokens, "no activation token was recorded by the suite"
    with owner_engine.connect() as conn:
        details = (
            conn.execute(text("SELECT detail::text FROM firm_audit_log WHERE detail IS NOT NULL"))
            .scalars()
            .all()
        )
    blob = "\n".join(details)
    for token in tokens:
        assert token not in blob
        assert sha256_hex(token) not in blob
    # Tokens appear in responses only as the show-once activation_url.
    for where, body in _json_bodies():
        for token in tokens:
            if token in body:
                assert '"activation_url"' in body, f"token outside activation_url in {where}"
                parsed = json.loads(body)
                assert isinstance(parsed, dict) and parsed["activation_url"].endswith(token)


def test_oauth_states_appear_only_in_the_authorization_url_and_never_in_audit(
    seed, owner_engine: Engine
) -> None:
    """F05: the ``state`` leaves the server once, inside the URL the browser is sent
    to. Its hash is stored on the connection; neither reaches an audit row."""
    states = LEAKS["oauth_state"]
    assert states, "no OAuth state was recorded by the suite"
    for where, body in _json_bodies():
        for state in states:
            if state in body:
                parsed = json.loads(body)
                assert set(parsed) == {"authorization_url"}, f"state outside the URL in {where}"
    with owner_engine.connect() as conn:
        tenants = conn.execute(text("SELECT id FROM tenant")).scalars().all()
        details: list[str] = []
        for tenant_id in tenants:
            conn.execute(
                text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(tenant_id)}
            )
            details += (
                conn.execute(text("SELECT detail::text FROM audit_log WHERE detail IS NOT NULL"))
                .scalars()
                .all()
            )
    blob = "\n".join(details)
    assert "connection_started" not in blob and len(details) > 10
    for secret in (*states, *LEAKS["oauth_code"], *LEAKS["connection_token"]):
        assert secret not in blob
    for state in states:
        assert sha256_hex(state) not in blob
