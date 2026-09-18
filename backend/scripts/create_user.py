#!/usr/bin/env python
"""Bootstrap the practice and its first firm_admin; create users; issue and re-issue
activation links; reset TOTP. The CLI never sets a password (D-16): every account
is activated through a one-time link, which this tool prints **once to stdout**
and never logs.

Runs as the application role (DATABASE_URL) and goes through the same admin
service as the API, so every action leaves a firm_audit_log / audit_log row
(actor NULL, detail.via = "cli"). No tenant has to exist to bootstrap.

Examples (from backend/, with .env in place):

  # fresh database: the firm and its first firm_admin; prints the activation link
  python scripts/create_user.py bootstrap --firm-name "Your CPA Practice" \\
      --email you@firm.test --display-name "You"

  # a client user with a role in one tenant
  python scripts/create_user.py create-user --email pm@client.test --display-name "PM" \\
      --membership rye-beach:client_pm

  # a firm staff member with entry rows in two tenants
  python scripts/create_user.py create-user --email staff@firm.test --display-name "Staff" \\
      --firm-role firm_staff --entry rye-beach --entry other-client

  # a new link (password reset), or a TOTP reset (clears TOTP, then a new link)
  python scripts/create_user.py issue-link --email you@firm.test
  python scripts/create_user.py reset-totp --email you@firm.test

  # a tenant, and an entry row for an existing firm user (F03 close-out; the same
  # audited service as POST /api/admin/tenants and /memberships, actor "cli")
  python scripts/create_user.py create-tenant --name "Rye Beach Landscaping" --slug rye-beach
  python scripts/create_user.py add-entry --email you@firm.test --tenant rye-beach
"""

import argparse
import sys

from sqlalchemy import select

from app.auth import admin
from app.auth.service import AuthError, issue_activation_link
from app.core.audit import RequestMeta
from app.core.db import create_app_engine, tenant_session, untenanted_session
from app.tenancy.models import CLIENT_ROLES, FIRM_ROLES, Firm, Role, Tenant, User

META = RequestMeta(ip=None, request_id="cli")
VIA = "cli"


def _print_link(url: str) -> None:
    """stdout only; the token in the URL must never reach a log."""
    print("Activation link (valid once; hand it over directly):")
    print(url)


def _user_by_email(db, email: str) -> User:
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        sys.exit("no such user")
    return user


def _parse_membership(spec: str) -> tuple[str, Role]:
    slug, sep, role = spec.partition(":")
    if not sep or role not in {r.value for r in CLIENT_ROLES}:
        sys.exit(
            "--membership must be SLUG:ROLE with ROLE one of "
            + ", ".join(sorted(r.value for r in CLIENT_ROLES))
        )
    return slug, Role(role)


def cmd_bootstrap(args: argparse.Namespace, engine) -> None:
    with untenanted_session(engine) as db:
        if db.execute(select(Firm.id)).first() is not None:
            sys.exit("a firm already exists; use create-user --firm-role firm_admin")
        firm = Firm(name=args.firm_name.strip())
        db.add(firm)
        db.flush()
        user, url = admin.create_user_with_link(
            db,
            email=args.email,
            display_name=args.display_name,
            actor=None,
            firm_id=firm.id,
            meta=META,
            via=VIA,
        )
        admin.create_firm_membership(
            db, None, firm_id=firm.id, user=user, role=Role.firm_admin, meta=META, via=VIA
        )
        print(f"created firm {firm.name!r} and firm_admin {user.email}")
    _print_link(url)
    print("The link sets the password, enrols TOTP and shows the recovery codes.")


def cmd_create_user(args: argparse.Namespace, engine) -> None:
    memberships = [_parse_membership(m) for m in args.membership]
    entries = list(args.entry)
    firm_role = Role(args.firm_role) if args.firm_role else None
    if firm_role is not None and memberships:
        sys.exit("a firm user holds entry rows (--entry), not client roles (--membership)")
    if firm_role is None and entries:
        sys.exit("--entry needs --firm-role")
    slugs = [s for s, _ in memberships] + entries
    with untenanted_session(engine) as db:
        firm_id = admin.only_firm_id(db)
        tenants = {
            t.slug: t for t in db.execute(select(Tenant).where(Tenant.slug.in_(slugs))).scalars()
        }
        missing = [s for s in slugs if s not in tenants]
        if missing:
            sys.exit(f"unknown tenant slug(s): {', '.join(missing)}")
        user, url = admin.create_user_with_link(
            db,
            email=args.email,
            display_name=args.display_name,
            actor=None,
            firm_id=firm_id,
            meta=META,
            via=VIA,
        )
        if firm_role is not None:
            admin.create_firm_membership(
                db, None, firm_id=firm_id, user=user, role=firm_role, meta=META, via=VIA
            )
            print(f"firm role: {firm_role.value}")
        user_id = user.id
        tenant_ids = {slug: t.id for slug, t in tenants.items()}
    for slug, role in [*memberships, *((s, None) for s in entries)]:
        with tenant_session(engine, tenant_ids[slug]) as db:
            tenant = db.get(Tenant, tenant_ids[slug])
            admin.create_membership(
                db, None, tenant=tenant, user=db.get(User, user_id), role=role, meta=META, via=VIA
            )
            print(f"{'entry' if role is None else role.value} in {slug}")
    print(f"created user {args.email.strip().lower()}")
    _print_link(url)


def cmd_issue_link(args: argparse.Namespace, engine) -> None:
    with untenanted_session(engine) as db:
        user = _user_by_email(db, args.email.strip().lower())
        url = issue_activation_link(
            db, None, user, firm_id=admin.firm_id_for_target(db, None, user), meta=META, via=VIA
        )
    print("all sessions ended; any earlier link is void")
    _print_link(url)


def cmd_reset_totp(args: argparse.Namespace, engine) -> None:
    with untenanted_session(engine) as db:
        user = _user_by_email(db, args.email.strip().lower())
        url = admin.reset_totp_and_issue_link(db, None, user, meta=META, via=VIA)
    print("TOTP cleared; all sessions ended; the link enrols TOTP again")
    _print_link(url)


def cmd_create_tenant(args: argparse.Namespace, engine) -> None:
    with untenanted_session(engine) as db:
        firm_id = admin.only_firm_id(db)
    tenant = admin.create_tenant_and_seed(
        engine, None, name=args.name, slug=args.slug, meta=META, firm_id=firm_id, via=VIA
    )
    print(f"created tenant {tenant.name!r} ({tenant.slug}) id {tenant.id}; cost categories seeded")


def cmd_add_entry(args: argparse.Namespace, engine) -> None:
    """An entry row (D-15) for an existing firm user in one tenant."""
    with untenanted_session(engine) as db:
        user = _user_by_email(db, args.email.strip().lower())
        tenant = db.execute(select(Tenant).where(Tenant.slug == args.tenant)).scalar_one_or_none()
        if tenant is None:
            sys.exit(f"unknown tenant slug: {args.tenant}")
        user_id, tenant_id = user.id, tenant.id
    with tenant_session(engine, tenant_id) as db:
        admin.create_membership(
            db,
            None,
            tenant=db.get(Tenant, tenant_id),
            user=db.get(User, user_id),
            role=None,
            meta=META,
            via=VIA,
        )
    print(f"entry row for {args.email.strip().lower()} in {args.tenant}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="command", required=True)

    b = sub.add_parser("bootstrap", help="create the firm and its first firm_admin")
    b.add_argument("--firm-name", required=True)
    b.add_argument("--email", required=True)
    b.add_argument("--display-name", required=True)
    b.set_defaults(func=cmd_bootstrap)

    c = sub.add_parser("create-user", help="create a user and print their activation link")
    c.add_argument("--email", required=True)
    c.add_argument("--display-name", required=True)
    c.add_argument("--firm-role", choices=sorted(r.value for r in FIRM_ROLES))
    c.add_argument("--membership", action="append", default=[], metavar="SLUG:ROLE")
    c.add_argument("--entry", action="append", default=[], metavar="SLUG")
    c.set_defaults(func=cmd_create_user)

    i = sub.add_parser("issue-link", help="new activation link (password reset)")
    i.add_argument("--email", required=True)
    i.set_defaults(func=cmd_issue_link)

    r = sub.add_parser("reset-totp", help="clear TOTP and print a new activation link")
    r.add_argument("--email", required=True)
    r.set_defaults(func=cmd_reset_totp)

    t = sub.add_parser("create-tenant", help="create a bare tenant row in the firm")
    t.add_argument("--name", required=True)
    t.add_argument("--slug", required=True)
    t.set_defaults(func=cmd_create_tenant)

    e = sub.add_parser("add-entry", help="entry row for an existing firm user in a tenant")
    e.add_argument("--email", required=True)
    e.add_argument("--tenant", required=True, metavar="SLUG")
    e.set_defaults(func=cmd_add_entry)
    return ap


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    engine = create_app_engine()
    try:
        args.func(args, engine)
    except AuthError as exc:
        sys.exit(f"error: {exc.detail}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
