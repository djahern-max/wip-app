"""Task registration (D-19). ``@task("kind")`` refuses any function whose first
parameter is not ``tenant_id`` at import time, so a task can never run without
being told which tenant it works for. The worker calls
``fn(tenant_id, **payload)`` with the context already set by ``tenant_session``."""

import inspect
from collections.abc import Callable
from typing import Any

TaskFn = Callable[..., Any]

TASKS: dict[str, TaskFn] = {}


class TaskRegistrationError(TypeError):
    pass


def task(kind: str) -> Callable[[TaskFn], TaskFn]:
    def register(fn: TaskFn) -> TaskFn:
        params = list(inspect.signature(fn).parameters)
        if not params or params[0] != "tenant_id":
            raise TaskRegistrationError(
                f"task {kind!r}: the first parameter of {fn.__qualname__} must be tenant_id"
            )
        if kind in TASKS and TASKS[kind] is not fn:
            raise TaskRegistrationError(f"task kind {kind!r} is already registered")
        TASKS[kind] = fn
        return fn

    return register


def get_task(kind: str) -> TaskFn:
    try:
        return TASKS[kind]
    except KeyError:
        raise LookupError(f"no task registered for kind {kind!r}") from None
