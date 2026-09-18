"""One audit writer for the configuration services (D-12): the row lands in the
caller's transaction with the actor (user id and role, or ``None`` for a task) and
a ``before`` / ``after`` detail."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.audit import RequestMeta, TenantEvent, write_tenant_audit
from app.tenancy.models import Role


@dataclass(frozen=True)
class Actor:
    user_id: UUID | None = None
    role: Role | None = None
    meta: RequestMeta | None = None


SYSTEM = Actor()  # a worker task: no user, no role


def audit(
    db: Session,
    tenant_id: UUID,
    event: TenantEvent,
    entity_type: str,
    entity_id: UUID | str | None,
    actor: Actor,
    *,
    before: dict | None = None,
    after: dict | None = None,
    **extra,
) -> None:
    detail: dict = {}
    if before is not None:
        detail["before"] = before
    if after is not None:
        detail["after"] = after
    detail.update(extra)
    write_tenant_audit(
        db,
        tenant_id=tenant_id,
        action=event,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_user_id=actor.user_id,
        actor_role=actor.role,
        detail=detail or None,
        meta=actor.meta,
    )
