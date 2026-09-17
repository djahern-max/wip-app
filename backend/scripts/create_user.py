#!/usr/bin/env python
"""Bootstrap the first firm_admin, and the last-resort password / TOTP reset path.

Runs as the application role (DATABASE_URL) and goes through the same service
functions as the API, so every action leaves a firm_audit_log / audit_log row
(actor NULL, detail.via = "cli"). Nothing secret is printed or logged.

Examples (from backend/, with .env in place):

  # first admin on a fresh database: creates the firm and the tenant if missing
  python scripts/create_user.py --email you@firm.test --display-name "You" \\
      --create-tenant rye-beach:"Rye Beach Landscaping" --firm-name "Your CPA Practice" \\
      --membership rye-beach:firm_admin --password-stdin < pw.txt

  # add a client user to an existing tenant
  python scripts/create_user.py --email pm@client.test --display-name "PM" \\
      --membership rye-beach:client_pm            # prompts for the password

  # last resort: a firm_admin locked out of TOTP or password
  python scripts/create_user.py --email you@firm.test --reset-totp
  python scripts/create_user.py --email you@firm.test --reset-password
"""

import argparse
import getpass
import sys

from sqlalchemy import select

from app.auth import service
from app.auth.service import AuthError
from app.core.audit import RequestMeta
from app.core.db import create_app_engine, set_user_context, tenant_session, untenanted_session
from app.core.security import MIN_PASSWORD_LENGTH
from app.tenancy.models import Firm, Role, Tenant, User

META = RequestMeta(ip=None, request_id="cli")


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        pw = sys.stdin.readline().rstrip("\r\n")
    else:
        pw = getpass.getpass(f"Password ({MIN_PASSWORD_LENGTH}+ characters): ")
        if pw != getpass.getpass("Again: "):
            sys.exit("passwords differ")
    if len(pw) < MIN_PASSWORD_LENGTH:
        sys.exit(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return pw


def _parse_membership(spec: str) -> tuple[str, Role]:
    slug, sep, role = spec.partition(":")
    if not sep or role not in Role.__members__:
        sys.exit(f"--membership must be SLUG:ROLE with ROLE one of {', '.join(Role.__members__)}")
    return slug, Role(role)


def _ensure_tenant(db, spec: str, firm_name: str | None) -> Tenant:
    slug, sep, name = spec.partition(":")
    if not sep:
        sys.exit("--create-tenant must be SLUG:NAME")
    tenant = db.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one_or_none()
    if tenant is not None:
        return tenant
    firms = db.execute(select(Firm)).scalars().all()
    if len(firms) == 1:
        firm = firms[0]
    elif not firms:
        firm = Firm(name=firm_name or "Firm")
        db.add(firm)
        db.flush()
        print(f"created firm {firm.name!r}")
    else:
        sys.exit("several firms exist; create the tenant by hand")
    tenant = Tenant(firm_id=firm.id, name=name, slug=slug)
    db.add(tenant)
    db.flush()
    print(f"created tenant {slug!r}")
    return tenant


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--email", required=True)
    ap.add_argument("--display-name")
    ap.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    ap.add_argument("--membership", action="append", default=[], metavar="SLUG:ROLE")
    ap.add_argument(
        "--create-tenant", metavar="SLUG:NAME", help="create the tenant (and firm) if missing"
    )
    ap.add_argument("--firm-name", help="name for the firm when it has to be created")
    ap.add_argument("--reset-password", action="store_true")
    ap.add_argument("--reset-totp", action="store_true")
    args = ap.parse_args(argv)

    engine = create_app_engine()
    email = args.email.strip().lower()
    try:
        if args.reset_password or args.reset_totp:
            with untenanted_session(engine) as db:
                user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
                if user is None:
                    sys.exit("no such user")
                set_user_context(db, user.id)  # own memberships → firm for the audit row
                if args.reset_totp:
                    service.reset_totp(db, None, user, meta=META, via="cli")
                    print("TOTP cleared; the user enrols again at next login")
                if args.reset_password:
                    service.set_password_directly(
                        db, user, _read_password(args), meta=META, via="cli"
                    )
                    print("password set; all sessions ended")
            return

        if not args.display_name:
            sys.exit("--display-name is required to create a user")
        memberships = [_parse_membership(m) for m in args.membership]
        password = _read_password(args)
        with untenanted_session(engine) as db:
            if args.create_tenant:
                _ensure_tenant(db, args.create_tenant, args.firm_name)
            tenants = {
                t.slug: t
                for t in db.execute(
                    select(Tenant).where(Tenant.slug.in_([s for s, _ in memberships]))
                ).scalars()
            }
            missing = [s for s, _ in memberships if s not in tenants]
            if missing:
                sys.exit(f"unknown tenant slug(s): {', '.join(missing)}")
            firm_id = tenants[memberships[0][0]].firm_id if memberships else None
            user = service.create_user(
                db,
                email=email,
                display_name=args.display_name,
                password=password,
                actor=None,
                firm_id=firm_id,
                meta=META,
                via="cli",
            )
            user_id = user.id
            tenant_ids = {slug: t.id for slug, t in tenants.items()}
        for slug, role in memberships:
            with tenant_session(engine, tenant_ids[slug]) as db:
                user = db.get(User, user_id)
                service.create_membership(
                    db, None, tenant_id=tenant_ids[slug], user=user, role=role, meta=META
                )
                print(f"membership {slug}: {role.value}")
        print(f"created user {email}")
        if any(r in (Role.firm_admin, Role.firm_staff) for _, r in memberships):
            print("firm role: TOTP enrolment is required at first login")
    except AuthError as exc:
        sys.exit(f"error: {exc.detail}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
