"""Home (F07.3): what to do next. One read for every role (``can_view_jobs``); the
set-up checklist is included only for roles that can view tenant configuration, and a
line carries a link only to a page the role can open. Nothing here writes."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.schemas import HomeItemOut, HomeJobOut, HomeLineOut, HomeLinkOut, HomeOut
from app.core.auth import Principal, TenantSession
from app.core.authz import can_view_jobs
from app.domain.home import service
from app.domain.home.checklist import Item, JobLine, Line, Link

router = APIRouter(prefix="/home", tags=["home"])

Viewer = Annotated[Principal, Depends(can_view_jobs)]


def _link(link: Link | None) -> HomeLinkOut | None:
    if link is None:
        return None
    return HomeLinkOut(page=link.page, section=link.section, job_id=link.job_id, review=link.review)


def _line(line: Line) -> HomeLineOut:
    return HomeLineOut(
        code=line.code,
        label=line.label,
        done=line.done,
        message=line.message,
        note=line.note,
        link=_link(line.link),
        count=line.count,
        total=line.total,
        primary=line.primary,
    )


def _item(item: Item) -> HomeItemOut:
    return HomeItemOut(
        code=item.code, message=item.message, link=_link(item.link), count=item.count
    )


def _job(j: JobLine) -> HomeJobOut:
    return HomeJobOut(
        id=j.job.id,
        name=j.job.name,
        status=j.job.status,
        status_label=j.job.status_label,
        code=j.need.code,
        message=j.need.message,
        link=_link(j.need.link),
        count=j.need.count,
    )


@router.get("", response_model=HomeOut)
def home(viewer: Viewer, db: TenantSession):
    view = service.home(db, viewer.active_tenant_id, viewer.role)
    return HomeOut(
        setup=None if view.setup is None else [_line(line) for line in view.setup],
        review=None if view.review is None else _item(view.review),
        jobs=[_job(j) for j in view.jobs],
        tracked_without_job=[_item(i) for i in view.tracked_without_job],
    )
