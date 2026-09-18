"""firm_admin administration (F02; F02.1, D-15/D-16/D-18): users and activation
links, tenants, firm memberships, memberships and entry rows.

Callers are the ``/api/admin`` routers (behind ``can_manage_*`` guards) and the
CLI (``actor=None``). Every write is audited into the same transaction. Functions
that read another user's ``membership`` rows do so only through
``app.core.auth.read_as_user`` (D-18), after the firm-role check, and filter the
result to tenants of the actor's firm.

Invariants (D-15), enforced here and backed by CHECK constraints where the
database can:
- a firm role lives only in ``firm_membership``; ``membership.role`` is a client
  role or NULL (entry row);
- an entry row exists only for a user holding a ``firm_membership`` in the
  tenant's firm; a client-role row never does;
- the last ``firm_admin`` of a firm cannot be removed or demoted (the firm row is
  locked ``FOR UPDATE`` before counting);
- removing a ``firm_membership`` commits first, then entry rows are removed one
  tenant per transaction (owner answer A). A failure part-way is harmless: an
  orphan entry row grants no access, and repeating the request finishes the job.
"""

import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

from app.auth.service import AuthError, issue_activation_link, reset_totp
from app.core.audit import FirmEvent, RequestMeta, TenantEvent, write_firm_audit, write_tenant_audit
from app.core.auth import (
    Principal,
    delete_user_sessions,
    effective_role,
    load_firm_memberships,
    load_memberships,
    read_as_user,
)
from app.core.db import set_tenant_context, set_user_context, tenant_session, untenanted_session
from app.tenancy.models import (
    CLIENT_ROLES,
    FIRM_ROLES,
    Firm,
    FirmMembership,
    Membership,
    Role,
    Tenant,
    User,
    UserSession,
)

_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")


def _now() -> datetime:
    return datetime.now(UTC)


def _require_firm_admin(actor: Principal | None) -> None:
    """The routers' guard already checked; this keeps the service honest when called
    from anywhere else (D-18: the swap helper runs only after this check)."""
    if actor is not None and actor.firm_role is not Role.firm_admin:
        raise AuthError(403, "forbidden")


def _actor_ids(actor: Principal | None) -> tuple[UUID | None, Role | None]:
    return (actor.user.id, actor.firm_role) if actor else (None, None)


def only_firm_id(db: Session) -> UUID:
    """The deployment's single firm (D-17); for the CLI, which has no principal."""
    firms = db.execute(select(Firm.id)).scalars().all()
    if len(firms) != 1:
        raise AuthError(409, f"expected exactly one firm, found {len(firms)}")
    return firms[0]


def firm_id_for_target(db: Session, actor: Principal | None, target: User) -> UUID | None:
    """The firm to key an audit row on: the actor's firm, else the target's own firm,
    else the firm of the target's client tenants."""
    if actor is not None and actor.firm_id is not None:
        return actor.firm_id
    fms = load_firm_memberships(db, target.id)
    if fms:
        return fms[0].firm_id
    with read_as_user(db, target.id, actor_user_id=actor.user.id if actor else None):
        _, tenant_firms = load_memberships(db, target.id)
    firms = sorted(set(tenant_firms.values()), key=str)
    return firms[0] if firms else None


def _user(db: Session, user_id: UUID) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise AuthError(404, "user not found")
    return user


# --- users and links ------------------------------------------------------------------------------


def create_user(
    db: Session,
    *,
    email: str,
    display_name: str,
    actor: Principal | None,
    firm_id: UUID | None,
    meta: RequestMeta,
    via: str = "api",
) -> User:
    """A user starts with no password (D-16). The caller issues the activation link."""
    email = email.strip().lower()
    if "@" not in email or len(email) < 3:
        raise AuthError(422, "invalid e-mail address")
    if db.execute(select(User.id).where(User.email == email)).scalar_one_or_none():
        raise AuthError(409, "a user with that e-mail already exists")
    user = User(email=email, display_name=display_name.strip())
    db.add(user)
    db.flush()
    actor_id, actor_role = _actor_ids(actor)
    write_firm_audit(
        db,
        firm_id=firm_id,
        action=FirmEvent.user_created,
        entity_type="user",
        entity_id=user.id,
        actor_user_id=actor_id,
        actor_role=actor_role,
        detail={"via": via},
        meta=meta,
    )
    return user


def create_user_with_link(
    db: Session,
    *,
    email: str,
    display_name: str,
    actor: Principal | None,
    firm_id: UUID | None,
    meta: RequestMeta,
    via: str = "api",
) -> tuple[User, str]:
    user = create_user(
        db, email=email, display_name=display_name, actor=actor, firm_id=firm_id, meta=meta, via=via
    )
    url = issue_activation_link(db, actor, user, firm_id=firm_id, meta=meta, via=via)
    return user, url


def reset_totp_and_issue_link(
    db: Session, actor: Principal | None, target: User, *, meta: RequestMeta, via: str = "admin"
) -> str:
    """Admin TOTP reset (D-16): clear the enrolment, end sessions, hand out a new link."""
    firm_id = firm_id_for_target(db, actor, target)
    reset_totp(db, actor, target, firm_id=firm_id, meta=meta, via=via)
    return issue_activation_link(db, actor, target, firm_id=firm_id, meta=meta, via=via)


# --- tenants (owner answer C) ---------------------------------------------------------------------


def create_tenant(
    db: Session,
    actor: Principal | None,
    *,
    name: str,
    slug: str,
    meta: RequestMeta,
    firm_id: UUID | None = None,
    via: str = "api",
) -> Tenant:
    """Bare tenant row in the actor's firm (never a firm from the request). Tenant
    configuration is F04; the admin's own entry row is a separate audited step.
    ``actor=None`` is the CLI (``scripts/create_user.py``), which names the firm
    (``only_firm_id``) and is audited with ``via``."""
    _require_firm_admin(actor)
    firm_id = actor.firm_id if actor is not None else firm_id
    if firm_id is None:
        raise AuthError(403, "forbidden")
    slug = slug.strip().lower()
    if not _SLUG.match(slug):
        raise AuthError(422, "slug must be lowercase letters, digits and hyphens")
    if db.execute(select(Tenant.id).where(Tenant.slug == slug)).scalar_one_or_none():
        raise AuthError(409, "a tenant with that slug already exists")
    tenant = Tenant(firm_id=firm_id, name=name.strip(), slug=slug)
    db.add(tenant)
    db.flush()
    write_firm_audit(
        db,
        firm_id=firm_id,
        action=FirmEvent.tenant_created,
        entity_type="tenant",
        entity_id=tenant.id,
        actor_user_id=actor.user.id if actor else None,
        actor_role=actor.firm_role if actor else None,
        detail={"name": tenant.name, "slug": tenant.slug, "via": via},
        meta=meta,
    )
    return tenant


def create_tenant_and_seed(
    engine: Engine,
    actor: Principal | None,
    *,
    name: str,
    slug: str,
    meta: RequestMeta,
    firm_id: UUID | None = None,
    via: str = "api",
) -> Tenant:
    """Create the tenant in its own transaction (global tables), then, once that has
    committed, seed the D-23 cost categories in a second transaction under the new
    tenant's context (F04; the API and the CLI both come through here). The seed is
    attributed to the creator; if it ever fails, ``ensure_cost_categories`` covers the
    tenant on first read."""
    from app.domain.config.audit import Actor
    from app.domain.config.categories import seed_cost_categories_at_creation

    with untenanted_session(engine) as db:
        tenant = create_tenant(db, actor, name=name, slug=slug, meta=meta, firm_id=firm_id, via=via)
        db.expunge(tenant)
    actor_id, actor_role = _actor_ids(actor)
    with tenant_session(engine, tenant.id) as db:
        seed_cost_categories_at_creation(
            db, tenant.id, Actor(user_id=actor_id, role=actor_role, meta=meta)
        )
    return tenant


def tenant_in_firm(db: Session, firm_id: UUID | None, tenant_id: UUID) -> Tenant:
    """404 for a tenant outside the firm (never reveal that it exists)."""
    tenant = db.get(Tenant, tenant_id)
    if tenant is None or firm_id is None or tenant.firm_id != firm_id:
        raise AuthError(404, "tenant not found")
    return tenant


def enter_tenant_for_admin(db: Session, actor: Principal, tenant_id: UUID) -> Tenant:
    """Set ``app.tenant_id`` to a tenant of the actor's firm for an administrative
    write. The actor need not hold an entry row there."""
    tenant = tenant_in_firm(db, actor.firm_id, tenant_id)
    set_tenant_context(db, tenant_id)
    return tenant


# --- firm memberships (D-15) ----------------------------------------------------------------------


def _lock_firm(db: Session, firm_id: UUID) -> None:
    """Serialise last-admin checks for one firm (owner answer to call 4)."""
    db.execute(select(Firm.id).where(Firm.id == firm_id).with_for_update())


def _count_firm_admins(db: Session, firm_id: UUID) -> int:
    return db.execute(
        select(func.count())
        .select_from(FirmMembership)
        .where(FirmMembership.firm_id == firm_id, FirmMembership.role == Role.firm_admin)
    ).scalar_one()


def _firm_membership(db: Session, firm_id: UUID, user_id: UUID) -> FirmMembership | None:
    return db.execute(
        select(FirmMembership).where(
            FirmMembership.firm_id == firm_id, FirmMembership.user_id == user_id
        )
    ).scalar_one_or_none()


def client_rows_in_firm(
    db: Session, actor: Principal | None, target_user_id: UUID, firm_id: UUID
) -> list[tuple[Membership, Tenant]]:
    """The target's client-role rows in tenants of the firm (D-18 read)."""
    return [
        (m, t) for m, t in _rows_in_firm(db, actor, target_user_id, firm_id) if _is_client(m, t)
    ]


def entry_rows_in_firm(
    db: Session, actor: Principal | None, target_user_id: UUID, firm_id: UUID
) -> list[tuple[Membership, Tenant]]:
    """The target's entry rows in tenants of the firm (D-18 read)."""
    return [
        (m, t) for m, t in _rows_in_firm(db, actor, target_user_id, firm_id) if not _is_client(m, t)
    ]


def _rows_in_firm(
    db: Session, actor: Principal | None, target_user_id: UUID, firm_id: UUID
) -> list[tuple[Membership, Tenant]]:
    _require_firm_admin(actor)
    with read_as_user(db, target_user_id, actor_user_id=actor.user.id if actor else None):
        rows = db.execute(
            select(Membership, Tenant)
            .join(Tenant, Tenant.id == Membership.tenant_id)
            .where(Membership.user_id == target_user_id, Tenant.firm_id == firm_id)
            .order_by(Membership.created_at)
        ).all()
    return [(m, t) for m, t in rows]


def _is_client(m: Membership, tenant: Tenant) -> bool:
    """A row that grants access with *no* firm membership is a client-role row; an
    entry row resolves to None without one. Goes through the single role function."""
    return effective_role(m, tenant.firm_id, []) is not None


def create_firm_membership(
    db: Session,
    actor: Principal | None,
    *,
    firm_id: UUID,
    user: User,
    role: Role,
    meta: RequestMeta,
    via: str = "api",
) -> FirmMembership:
    _require_firm_admin(actor)
    if role not in FIRM_ROLES:
        raise AuthError(422, "firm_membership carries a firm role")
    if _firm_membership(db, firm_id, user.id) is not None:
        raise AuthError(409, "firm_membership already exists")
    if client_rows_in_firm(db, actor, user.id, firm_id):
        raise AuthError(409, "user holds a client role in a tenant of this firm")
    fm = FirmMembership(firm_id=firm_id, user_id=user.id, role=role)
    db.add(fm)
    db.flush()
    delete_user_sessions(db, user.id)
    actor_id, actor_role = _actor_ids(actor)
    write_firm_audit(
        db,
        firm_id=firm_id,
        action=FirmEvent.firm_membership_created,
        entity_type="firm_membership",
        entity_id=fm.id,
        actor_user_id=actor_id,
        actor_role=actor_role,
        detail={"user_id": str(user.id), "role": role.value, "via": via},
        meta=meta,
    )
    return fm


def change_firm_membership_role(
    db: Session,
    actor: Principal | None,
    *,
    firm_id: UUID,
    user_id: UUID,
    role: Role,
    meta: RequestMeta,
) -> FirmMembership:
    _require_firm_admin(actor)
    if role not in FIRM_ROLES:
        raise AuthError(422, "firm_membership carries a firm role")
    _lock_firm(db, firm_id)
    fm = _firm_membership(db, firm_id, user_id)
    if fm is None:
        raise AuthError(404, "firm_membership not found")
    if fm.role is Role.firm_admin and role is not Role.firm_admin:
        if _count_firm_admins(db, firm_id) <= 1:
            raise AuthError(409, "the last firm_admin cannot be demoted")
    old = fm.role
    fm.role = role
    db.flush()
    delete_user_sessions(db, user_id)
    actor_id, actor_role = _actor_ids(actor)
    write_firm_audit(
        db,
        firm_id=firm_id,
        action=FirmEvent.firm_membership_role_changed,
        entity_type="firm_membership",
        entity_id=fm.id,
        actor_user_id=actor_id,
        actor_role=actor_role,
        detail={"user_id": str(user_id), "old_role": old.value, "new_role": role.value},
        meta=meta,
    )
    return fm


def _remove_entry_row(
    engine: Engine,
    *,
    tenant_id: UUID,
    user_id: UUID,
    actor_id: UUID | None,
    actor_role: Role | None,
    meta: RequestMeta,
) -> None:
    """One tenant, one transaction (owner answer A). Module-level so a test can make
    it fail part-way."""
    with tenant_session(engine, tenant_id) as db:
        m = db.execute(
            select(Membership).where(
                Membership.tenant_id == tenant_id, Membership.user_id == user_id
            )
        ).scalar_one_or_none()
        if m is None:
            return
        write_tenant_audit(
            db,
            tenant_id=tenant_id,
            action=TenantEvent.membership_removed,
            entity_type="membership",
            entity_id=m.id,
            actor_user_id=actor_id,
            actor_role=actor_role,
            detail={"user_id": str(user_id), "role": "entry", "via": "firm_membership_removed"},
            meta=meta,
        )
        db.delete(m)
        db.flush()
        _clear_active_tenant(db, user_id, tenant_id)


def remove_firm_membership(
    engine: Engine,
    actor: Principal | None,
    *,
    firm_id: UUID,
    user_id: UUID,
    meta: RequestMeta,
) -> bool:
    """Owner answer A. Transaction 1: lock the firm, refuse the last firm_admin,
    remove the row, write ``firm_membership_removed``, delete the user's sessions,
    commit. Then one transaction per tenant removes the entry rows. Returns False
    when there was nothing to do (no row, no orphan entry rows) → 404.

    Runs in its own transactions, not the request's: the request transaction only
    carries the principal. The actor cannot remove their own row (their session row
    is locked by the request transaction; and a firm should not lose its admin by
    accident)."""
    _require_firm_admin(actor)
    if actor is not None and actor.user.id == user_id:
        raise AuthError(409, "you cannot remove your own firm_membership")
    actor_id, actor_role = _actor_ids(actor)
    with untenanted_session(engine) as db:
        if actor_id is not None:
            set_user_context(db, actor_id)
        _lock_firm(db, firm_id)
        fm = _firm_membership(db, firm_id, user_id)
        if fm is not None and fm.role is Role.firm_admin and _count_firm_admins(db, firm_id) <= 1:
            raise AuthError(409, "the last firm_admin cannot be removed")
        entry_tenants = [t.id for _, t in entry_rows_in_firm(db, actor, user_id, firm_id)]
        if fm is None and not entry_tenants:
            return False
        if fm is not None:
            db.delete(fm)
            db.flush()
            delete_user_sessions(db, user_id)
            write_firm_audit(
                db,
                firm_id=firm_id,
                action=FirmEvent.firm_membership_removed,
                entity_type="firm_membership",
                entity_id=fm.id,
                actor_user_id=actor_id,
                actor_role=actor_role,
                detail={
                    "user_id": str(user_id),
                    "role": fm.role.value,
                    "entry_rows_to_remove": len(entry_tenants),
                },
                meta=meta,
            )
    for tenant_id in entry_tenants:
        _remove_entry_row(
            engine,
            tenant_id=tenant_id,
            user_id=user_id,
            actor_id=actor_id,
            actor_role=actor_role,
            meta=meta,
        )
    return True


def list_firm_memberships(db: Session, firm_id: UUID) -> list[tuple[FirmMembership, User]]:
    return [
        (fm, u)
        for fm, u in db.execute(
            select(FirmMembership, User)
            .join(User, User.id == FirmMembership.user_id)
            .where(FirmMembership.firm_id == firm_id)
            .order_by(User.email)
        ).all()
    ]


# --- memberships and entry rows -------------------------------------------------------------------


def _target_is_firm_user_here(db: Session, user_id: UUID, tenant: Tenant) -> bool:
    return any(fm.firm_id == tenant.firm_id for fm in load_firm_memberships(db, user_id))


def create_membership(
    db: Session,
    actor: Principal | None,
    *,
    tenant: Tenant,
    user: User,
    role: Role | None,
    meta: RequestMeta,
    via: str = "api",
) -> Membership:
    """Requires ``app.tenant_id`` = ``tenant.id`` (``enter_tenant_for_admin`` or
    ``tenant_session``). ``role=None`` writes an entry row for a firm user."""
    _require_firm_admin(actor)
    if role is not None and role in FIRM_ROLES:
        raise AuthError(422, "firm roles are held in firm_membership, not membership")
    firm_user = _target_is_firm_user_here(db, user.id, tenant)
    if firm_user and role is not None:
        raise AuthError(409, "user holds a firm role; an entry row carries no role")
    if not firm_user and role is None:
        raise AuthError(422, "a client user needs a client role")
    existing = db.execute(
        select(Membership.id).where(
            Membership.tenant_id == tenant.id, Membership.user_id == user.id
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise AuthError(409, "membership already exists")
    m = Membership(tenant_id=tenant.id, user_id=user.id, role=role)
    db.add(m)
    db.flush()
    actor_id, actor_role = _actor_ids(actor)
    write_tenant_audit(
        db,
        tenant_id=tenant.id,
        action=TenantEvent.membership_created,
        entity_type="membership",
        entity_id=m.id,
        actor_user_id=actor_id,
        actor_role=actor_role,
        detail={"user_id": str(user.id), "role": role.value if role else "entry", "via": via},
        meta=meta,
    )
    return m


def change_membership_role(
    db: Session,
    actor: Principal | None,
    *,
    tenant: Tenant,
    user_id: UUID,
    role: Role,
    meta: RequestMeta,
) -> Membership:
    """Client-role rows only. An entry row carries no role (409)."""
    _require_firm_admin(actor)
    if role not in CLIENT_ROLES:
        raise AuthError(422, "membership carries a client role")
    if _target_is_firm_user_here(db, user_id, tenant):
        raise AuthError(409, "user holds a firm role; an entry row carries no role")
    m = db.execute(
        select(Membership).where(Membership.tenant_id == tenant.id, Membership.user_id == user_id)
    ).scalar_one_or_none()
    if m is None:
        raise AuthError(404, "membership not found")
    old = effective_role(m, tenant.firm_id, [])
    m.role = role
    db.flush()
    actor_id, actor_role = _actor_ids(actor)
    write_tenant_audit(
        db,
        tenant_id=tenant.id,
        action=TenantEvent.membership_role_changed,
        entity_type="membership",
        entity_id=m.id,
        actor_user_id=actor_id,
        actor_role=actor_role,
        detail={
            "user_id": str(user_id),
            "old_role": old.value if old else "entry",
            "new_role": role.value,
        },
        meta=meta,
    )
    return m


def _clear_active_tenant(db: Session, user_id: UUID, tenant_id: UUID) -> None:
    """Removing a membership leaves the user's sessions valid with no active tenant."""
    db.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.active_tenant_id == tenant_id)
        .values(active_tenant_id=None)
    )


def remove_membership(
    db: Session,
    actor: Principal | None,
    *,
    tenant: Tenant,
    user_id: UUID,
    meta: RequestMeta,
) -> None:
    _require_firm_admin(actor)
    m = db.execute(
        select(Membership).where(Membership.tenant_id == tenant.id, Membership.user_id == user_id)
    ).scalar_one_or_none()
    if m is None:
        raise AuthError(404, "membership not found")
    role = effective_role(m, tenant.firm_id, load_firm_memberships(db, user_id))
    actor_id, actor_role = _actor_ids(actor)
    write_tenant_audit(
        db,
        tenant_id=tenant.id,
        action=TenantEvent.membership_removed,
        entity_type="membership",
        entity_id=m.id,
        actor_user_id=actor_id,
        actor_role=actor_role,
        detail={"user_id": str(user_id), "role": role.value if role else "entry"},
        meta=meta,
    )
    db.delete(m)
    db.flush()
    _clear_active_tenant(db, user_id, tenant.id)


def list_tenant_users(db: Session, tenant: Tenant) -> list[tuple[Membership, User, Role | None]]:
    """Users of one tenant with their effective role. Requires ``app.tenant_id``."""
    rows = db.execute(
        select(Membership, User).join(User, User.id == Membership.user_id).order_by(User.email)
    ).all()
    out = []
    for m, u in rows:
        out.append((m, u, effective_role(m, tenant.firm_id, load_firm_memberships(db, u.id))))
    return out
