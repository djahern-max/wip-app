"""Re-encrypt stored ciphertexts under the active key after a rotation (F03).

    cd backend && .venv/bin/python scripts/reencrypt.py [--dry-run]

Covers ``user.totp_secret_enc`` (global table, one transaction) and the token
columns of ``connection`` (tenant table: one transaction per tenant, with tenant
context, exactly as the application does). Idempotent: rows already on the
active key are left alone, so a second run changes nothing. Prints counts by key
id before and after and never prints a value. Exits 1 if any blob could not be
decrypted (those rows are left as they are; add the missing key to CRYPTO_KEYS).

Runs as the application role with ``DATABASE_URL``, ``CRYPTO_KEYS`` (old and new
keys) and ``CRYPTO_ACTIVE_KEY_ID`` (the new key). The old key may be removed from
the ring only when the "after" counts show zero rows on it.
"""

import argparse
import sys
from collections import Counter
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.orm import undefer_group

from app.core.config import get_settings
from app.core.crypto import CryptoError, Keyring, aad_for, get_keyring
from app.core.db import create_app_engine, tenant_session, untenanted_session
from app.ingest.models import Connection
from app.tenancy.models import CREDENTIAL_GROUP, Tenant, User

TOKEN_FIELDS = ("access_token", "refresh_token")


def totp_counts(engine: Engine) -> Counter:
    with untenanted_session(engine) as s:
        rows = s.execute(
            select(User.totp_key_id).where(User.totp_secret_enc.is_not(None))
        ).scalars()
        return Counter(rows)


def connection_counts(engine: Engine, tenant_ids: list[UUID]) -> Counter:
    counts: Counter = Counter()
    for tid in tenant_ids:
        with tenant_session(engine, tid) as s:
            for access_kid, refresh_kid in s.execute(
                select(Connection.access_token_key_id, Connection.refresh_token_key_id)
            ):
                if access_kid:
                    counts[access_kid] += 1
                if refresh_kid:
                    counts[refresh_kid] += 1
    return counts


def reencrypt_totp(engine: Engine, ring: Keyring, *, dry_run: bool) -> tuple[int, int]:
    """(rows re-encrypted, rows that failed to decrypt)."""
    done = failed = 0
    with untenanted_session(engine) as s:
        users = s.execute(
            select(User)
            .options(undefer_group(CREDENTIAL_GROUP))
            .where(User.totp_secret_enc.is_not(None), User.totp_key_id != ring.active_key_id)
            .with_for_update()
        ).scalars()
        for u in users:
            try:
                plain = ring.decrypt(u.totp_key_id, u.totp_secret_enc, aad=u.id.bytes)
            except CryptoError:
                failed += 1
                continue
            if not dry_run:
                u.totp_key_id, u.totp_secret_enc = ring.encrypt(plain, aad=u.id.bytes)
            done += 1
    return done, failed


def reencrypt_connections(
    engine: Engine, ring: Keyring, tenant_ids: list[UUID], *, dry_run: bool
) -> tuple[int, int]:
    done = failed = 0
    for tid in tenant_ids:
        with tenant_session(engine, tid) as s:
            for c in s.execute(select(Connection).with_for_update()).scalars():
                for field in TOKEN_FIELDS:
                    kid = getattr(c, f"{field}_key_id")
                    blob = getattr(c, f"{field}_enc")
                    if blob is None or kid == ring.active_key_id:
                        continue
                    try:
                        plain = ring.decrypt(kid, blob, aad=aad_for(c.tenant_id, c.id, field))
                    except CryptoError:
                        failed += 1
                        continue
                    if not dry_run:
                        new_kid, new_blob = ring.encrypt(
                            plain, aad=aad_for(c.tenant_id, c.id, field)
                        )
                        setattr(c, f"{field}_key_id", new_kid)
                        setattr(c, f"{field}_enc", new_blob)
                    done += 1
    return done, failed


def _report(label: str, counts: Counter) -> None:
    body = ", ".join(f"{kid}={n}" for kid, n in sorted(counts.items())) or "none"
    print(f"{label}: {body}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="count only; write nothing")
    args = parser.parse_args(argv)

    settings = get_settings()
    ring = get_keyring()
    engine = create_app_engine(settings.database_url, production=True)
    try:
        with untenanted_session(engine) as s:
            tenant_ids = list(s.execute(select(Tenant.id).order_by(Tenant.created_at)).scalars())
        print(f"active key id: {ring.active_key_id}; tenants: {len(tenant_ids)}")
        _report("before user.totp_secret_enc", totp_counts(engine))
        _report("before connection tokens", connection_counts(engine, tenant_ids))
        t_done, t_failed = reencrypt_totp(engine, ring, dry_run=args.dry_run)
        c_done, c_failed = reencrypt_connections(engine, ring, tenant_ids, dry_run=args.dry_run)
        verb = "would re-encrypt" if args.dry_run else "re-encrypted"
        print(f"{verb}: user rows {t_done}, connection tokens {c_done}")
        _report("after user.totp_secret_enc", totp_counts(engine))
        _report("after connection tokens", connection_counts(engine, tenant_ids))
        if t_failed or c_failed:
            print(
                f"could not decrypt: user rows {t_failed}, connection tokens {c_failed} "
                "(missing key? add it to CRYPTO_KEYS and re-run)",
                file=sys.stderr,
            )
            return 1
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
