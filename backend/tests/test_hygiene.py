"""Audit hygiene, settings, origin policy, engine configuration, the D-18 swap
helper, and the single effective-role call site (F02.1)."""

import base64
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

import app.core.db as app_db
from app.core.auth import read_as_user
from app.core.db import current_user_context, tenant_session, untenanted_session
from app.core.security import sha256_hex
from app.main import create_app
from app.tenancy.models import Membership, Role, User
from tests.conftest import APP_ORIGIN, CSRF, Seed, password_login
from tests.leaks import record_secret
from tests.test_auth import firm_events

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
APP_DIR = BACKEND / "app"


# --- unknown e-mail is stored hashed ----------------------------------------------------------


def test_unknown_email_failure_stores_the_sha256_not_the_address(
    client: TestClient, rw_engine: Engine
) -> None:
    email = f"Ghost-{uuid.uuid4().hex[:8]}@Example.test"
    record_secret("attempted_email", email)
    record_secret("attempted_email", email.lower())
    r = client.post(
        "/api/auth/login", json={"email": email, "password": "whatever-whatever-1"}, headers=CSRF
    )
    assert r.status_code == 401
    rows = [e for e in firm_events(rw_engine, None, "login_failure") if e.entity_id is None]
    row = rows[-1]
    assert row.detail == {
        "step": "password",
        "reason": "unknown_email",
        "email_sha256": sha256_hex(email.strip().lower()),
    }
    with rw_engine.connect() as conn:
        raw = conn.execute(
            text("SELECT detail::text FROM firm_audit_log WHERE id = :id"), {"id": row.id}
        ).scalar_one()
    assert email.lower() not in raw.lower()


# --- null-firm rows are for firm_admin only -----------------------------------------------------


def test_null_firm_audit_rows_are_visible_to_firm_admin_only(
    login_as: Callable[..., TestClient], client: TestClient
) -> None:
    client.post(
        "/api/auth/login",
        json={"email": f"nobody-{uuid.uuid4().hex[:6]}@example.test", "password": "xx-xx-xx-xx-xx"},
        headers=CSRF,
    )
    staff_rows = login_as("firm_staff").get("/api/firm-audit?limit=500").json()
    assert staff_rows and all(r["firm_id"] is not None for r in staff_rows)
    admin_rows = login_as("firm_admin").get("/api/firm-audit?limit=500").json()
    assert any(r["firm_id"] is None for r in admin_rows)


# --- settings ------------------------------------------------------------------------------------


def test_the_suite_never_reads_the_developers_env_file() -> None:
    """``tests._env`` runs before the application is imported (it refuses to load
    otherwise) and points ENV_FILE at a path that does not exist, so no ``Settings``
    built during the run loads the repo-root ``.env``."""
    from app.core import config

    assert not os.path.exists(os.environ["ENV_FILE"])
    assert config._env_file() is None
    # The guard: loading the application first is refused, not silently tolerated.
    proc = subprocess.run(
        [sys.executable, "-c", "import app.core.config, tests._env"],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode != 0
    assert "tests._env must be imported before the application" in proc.stderr


_START_KEY = base64.b64encode(os.urandom(32)).decode()  # generated per run, never written


def _start_app(
    env_overrides: dict[str, str | None], code: str = "import app.main"
) -> subprocess.CompletedProcess:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("DATABASE_", "CRYPTO_", "TEST_DATABASE_"))
    }
    env["ENV_FILE"] = "/nonexistent/.env"
    env["DATABASE_URL"] = "postgresql+psycopg://app_rw:secret-value-xyz@localhost:5433/wip_test"
    env["CRYPTO_KEYS"] = "k1:" + _START_KEY
    env["CRYPTO_ACTIVE_KEY_ID"] = "k1"
    for k, v in env_overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("missing", ["DATABASE_URL", "CRYPTO_KEYS", "CRYPTO_ACTIVE_KEY_ID"])
def test_app_refuses_to_start_without_a_required_variable(missing: str) -> None:
    proc = _start_app({missing: None})
    assert proc.returncode != 0
    assert missing in proc.stderr
    assert "secret-value-xyz" not in proc.stderr and _START_KEY not in proc.stderr


def test_app_starts_with_the_required_variables() -> None:
    proc = _start_app({})
    assert proc.returncode == 0, proc.stderr


def test_database_owner_url_is_not_required_by_the_api() -> None:
    proc = _start_app({"DATABASE_OWNER_URL": None})
    assert proc.returncode == 0, proc.stderr


def test_env_is_not_tracked_and_example_has_no_key_material() -> None:
    if subprocess.run(["git", "rev-parse"], cwd=REPO, capture_output=True).returncode != 0:
        pytest.skip("not a git checkout; CI enforces this")
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"], cwd=REPO, capture_output=True
    )
    assert tracked.returncode != 0, ".env is tracked"
    example = (REPO / ".env.example").read_text()
    for line in example.splitlines():
        if line.startswith("CRYPTO_KEYS="):
            assert not re.search(r"[A-Za-z0-9+/]{43}=", line), "key material in .env.example"
    assert ".env" in (REPO / ".gitignore").read_text().splitlines()


# --- the activation token never travels in a query string ------------------------------------


def test_no_backend_route_accepts_the_token_as_a_query_parameter(client: TestClient) -> None:
    from fastapi.routing import APIRoute

    offenders = []
    for route in create_app().routes:
        if not isinstance(route, APIRoute):
            continue
        for param in route.dependant.query_params:
            if "token" in param.name.lower():
                offenders.append(f"{route.path} ?{param.name}")
    assert offenders == []
    # Functionally: a token in the query string is ignored, the body is what counts.
    r = client.post(
        "/api/auth/activate?token=" + "x" * 40,
        json={"new_password": "whatever-whatever-1"},
        headers=CSRF,
    )
    assert r.status_code == 422  # body.token missing → nothing was read from the URL
    assert client.get("/activate?token=" + "x" * 40).status_code == 404  # no such backend route


# --- origin policy -------------------------------------------------------------------------------


def test_foreign_origin_preflight_and_requests_are_refused(client: TestClient, seed: Seed) -> None:
    r = client.options(
        "/api/auth/login",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-requested-with,content-type",
        },
    )
    assert r.status_code == 403
    assert not any(h.lower().startswith("access-control-") for h in r.headers)
    su = seed.users["client_viewer"]
    r = client.post(
        "/api/auth/login",
        json={"email": su.email, "password": su.password},
        headers={**CSRF, "Origin": "https://evil.example"},
    )
    assert (r.status_code, r.json()) == (403, {"detail": "cross-origin refused"})
    assert "set-cookie" not in r.headers
    # The app's own origin (what the Vite proxy forwards) and no Origin (CLI) both work.
    r = client.post(
        "/api/auth/login",
        json={"email": su.email, "password": su.password},
        headers={**CSRF, "Origin": APP_ORIGIN},
    )
    assert r.status_code == 200
    assert password_login(client, su).status_code == 200


# --- no pytest in the application; probes only in the suite ---------------------------------------


def test_application_package_never_imports_the_test_package() -> None:
    hits = [
        str(p.relative_to(BACKEND))
        for p in APP_DIR.rglob("*.py")
        if re.search(r"^\s*(from|import)\s+tests\b", p.read_text(), re.M)
    ]
    assert hits == []


def test_application_package_does_not_mention_pytest() -> None:
    hits = [str(p.relative_to(BACKEND)) for p in APP_DIR.rglob("*.py") if "pytest" in p.read_text()]
    assert hits == []


def test_probe_routes_do_not_exist_on_a_normally_started_app() -> None:
    with TestClient(create_app(), base_url="https://testserver") as c:
        assert c.get("/api/_probe/rows").status_code == 404
        assert c.get("/api/_probe/cap/view_reports").status_code == 404


# --- engine configuration (owner answer to call 7) ---------------------------------------------


def test_production_engine_hides_parameters_and_does_not_echo() -> None:
    assert app_db.PRODUCTION_ENGINE_OPTIONS["hide_parameters"] is True
    assert app_db.PRODUCTION_ENGINE_OPTIONS["echo"] is False
    engine = app_db.create_app_engine(production=True)
    try:
        assert engine.hide_parameters is True
        assert engine.echo is False or not engine.echo
    finally:
        engine.dispose()


def test_integrity_error_through_the_production_engine_carries_no_bound_values(
    seed: Seed,
) -> None:
    """Bound parameters are hidden from the exception message. Postgres itself still
    echoes the conflicting key in its DETAIL line, so what the application logs about
    a database error is ``describe_db_error``: SQLSTATE and primary message only."""
    engine = app_db.create_app_engine(production=True)
    try:
        dup = seed.users["client_pm"].email
        with pytest.raises(IntegrityError) as excinfo:
            with untenanted_session(engine) as s:
                s.add(User(email=dup, display_name="Duplicate-Display-Name-Marker"))
                s.flush()
        message = str(excinfo.value)
        assert "Duplicate-Display-Name-Marker" not in message  # a bound value
        assert "hide_parameters" in message  # the parameter list is replaced by a notice
        described = app_db.describe_db_error(excinfo.value)
        assert described.startswith("IntegrityError [23505]: duplicate key value")
        assert dup not in described and "Duplicate-Display-Name-Marker" not in described
        assert "INSERT" not in described
    finally:
        engine.dispose()


# --- D-18: the app.user_id swap helper -----------------------------------------------------------


def test_read_as_user_restores_the_actor_after_normal_exit_and_after_an_exception(
    seed: Seed, rw_engine: Engine
) -> None:
    actor = seed.users["firm_admin"].id
    target = seed.users["client_pm"].id
    with untenanted_session(rw_engine) as s:
        app_db.set_user_context(s, actor)
        with read_as_user(s, target, actor_user_id=actor):
            assert current_user_context(s) == str(target)
            rows = s.execute(select(Membership).where(Membership.user_id == target)).scalars().all()
            assert {m.tenant_id for m in rows} == {seed.tenant_a}
        assert current_user_context(s) == str(actor)
        with pytest.raises(RuntimeError, match="boom"):
            with read_as_user(s, target, actor_user_id=actor):
                raise RuntimeError("boom")
        assert current_user_context(s) == str(actor)


def test_writes_on_membership_inside_read_as_user_still_fail_or_match_nothing(
    seed: Seed, rw_engine: Engine, owner_engine: Engine
) -> None:
    actor = seed.users["firm_admin"].id
    target = seed.users["client_pm"].id
    with pytest.raises(ProgrammingError, match="row-level security"):
        with untenanted_session(rw_engine) as s:
            with read_as_user(s, target, actor_user_id=actor):
                s.add(Membership(tenant_id=seed.tenant_b, user_id=target, role=Role.client_viewer))
                s.flush()
    with untenanted_session(rw_engine) as s:
        with read_as_user(s, target, actor_user_id=actor):
            updated = s.execute(
                text("UPDATE membership SET role = 'client_viewer' WHERE user_id = :u"),
                {"u": target},
            ).rowcount
            deleted = s.execute(
                text("DELETE FROM membership WHERE user_id = :u"), {"u": target}
            ).rowcount
    assert (updated, deleted) == (0, 0)
    with tenant_session(owner_engine, seed.tenant_a) as s:
        assert (
            s.execute(select(Membership.role).where(Membership.user_id == target)).scalar_one()
            is Role.client_pm
        )


def test_read_as_user_is_called_only_from_the_admin_service() -> None:
    callers = sorted(
        str(p.relative_to(BACKEND))
        for p in APP_DIR.rglob("*.py")
        if re.search(r"\bread_as_user\(", p.read_text()) and p.name != "auth.py"
    )
    assert callers == ["app/auth/admin.py"]
    # ...and only after the firm-role check, on functions that filter to the firm.
    src = (APP_DIR / "auth" / "admin.py").read_text()
    for func in ("_rows_in_firm", "firm_id_for_target"):
        body = src.split(f"def {func}(")[1].split("\ndef ")[0]
        assert "read_as_user(" in body
    assert "Tenant.firm_id == firm_id" in src.split("def _rows_in_firm(")[1].split("\ndef ")[0]


# --- exactly one function computes the tenant role ----------------------------------------------

_ROLE_OWNERS = {"m", "membership", "row", "existing", "Membership"}


def _role_reads(source: str) -> list[int]:
    """Line numbers where ``<Membership instance or class>.role`` is *read* (an
    assignment ``x.role = …`` is a write). Tokenised, so docstrings and comments do
    not count."""
    import io
    import tokenize

    toks = [
        t
        for t in tokenize.generate_tokens(io.StringIO(source).readline)
        if t.type in (tokenize.NAME, tokenize.OP)
    ]
    hits = []
    for i in range(len(toks) - 2):
        a, dot, b = toks[i], toks[i + 1], toks[i + 2]
        if not (a.type == tokenize.NAME and a.string in _ROLE_OWNERS):
            continue
        if not (dot.string == "." and b.type == tokenize.NAME and b.string == "role"):
            continue
        if i > 0 and toks[i - 1].string == ".":
            continue  # attribute of something else, e.g. principal.m.role
        nxt = toks[i + 3].string if i + 3 < len(toks) else ""
        if nxt == "=":
            continue  # a write
        hits.append(a.start[0])
    return hits


def test_membership_role_is_read_only_inside_effective_role() -> None:
    offenders: list[str] = []
    for p in sorted(APP_DIR.rglob("*.py")):
        rel = str(p.relative_to(BACKEND))
        if rel == "app/tenancy/models.py":
            continue
        source = p.read_text()
        allowed: set[int] = set()
        if rel == "app/core/auth.py":
            lines = source.splitlines()
            start = next(i for i, ln in enumerate(lines, 1) if ln.startswith("def effective_role("))
            end = next(i for i, ln in enumerate(lines, 1) if i > start and ln.startswith("def "))
            allowed = set(range(start, end))
        for line_no in _role_reads(source):
            if line_no not in allowed:
                offenders.append(f"{rel}:{line_no}")
    assert offenders == []


# --- F03: money-safe JSON, worker isolation, local object store never tracked ------------------


def _code_only(source: str) -> str:
    """The source with strings and comments blanked (newlines kept): docstrings
    that *mention* a forbidden name do not count."""
    import io
    import tokenize

    out = []
    last = (1, 0)
    lines = source.splitlines(keepends=True)

    def pos_to_index(row: int, col: int) -> int:
        return sum(len(ln) for ln in lines[: row - 1]) + col

    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        start = pos_to_index(*tok.start)
        out.append(source[pos_to_index(*last) : start])
        if tok.type in (tokenize.STRING, tokenize.COMMENT):
            out.append(re.sub(r"[^\n]", " ", tok.string))
        else:
            out.append(tok.string)
        last = tok.end
    out.append(source[pos_to_index(*last) :])
    return "".join(out)


def _app_sources() -> list[tuple[str, str]]:
    return [
        (str(p.relative_to(BACKEND)), _code_only(p.read_text()))
        for p in sorted(APP_DIR.rglob("*.py"))
    ]


def test_json_loads_appears_only_in_the_codec_and_always_with_parse_float() -> None:
    """CLAUDE.md Money: JSON is parsed as string → Decimal. The only ``json.loads``
    in ``app/`` is the codec's partial with ``parse_float=Decimal``; every other
    module reads JSON through the engine or ``app.core.jsoncodec.json_loads``."""
    offenders = []
    for rel, src in _app_sources():
        for m in re.finditer(r"\bjson\.loads?\b", src):
            line = src[src.rfind("\n", 0, m.start()) + 1 : src.find("\n", m.end())]
            if rel != "app/core/jsoncodec.py" or "parse_float=Decimal" not in line:
                offenders.append(f"{rel}: {line.strip()}")
    assert offenders == []
    codec = (APP_DIR / "core" / "jsoncodec.py").read_text()
    assert "partial(json.loads, parse_float=Decimal)" in codec


def test_no_float_conversion_on_ingestion_paths() -> None:
    """No ``float(`` call in the modules that carry payloads."""
    hits = []
    for rel, src in _app_sources():
        if rel.startswith(
            ("app/ingest/", "app/worker/", "app/core/jsoncodec.py", "app/integrations/")
        ):
            for m in re.finditer(r"(?<![\w.])float\(", src):
                hits.append(f"{rel}:{src.count(chr(10), 0, m.start()) + 1}")
    assert hits == []


_PRINCIPAL_WORDS = re.compile(r"\b(Principal|active_tenant_id|firm_ids)\b")


def _worker_and_task_sources() -> list[tuple[str, str]]:
    from app.worker.runner import TASK_MODULES

    files = sorted((APP_DIR / "worker").rglob("*.py"))
    files += [APP_DIR.parent / (m.replace(".", "/") + ".py") for m in TASK_MODULES]
    return [(str(p.relative_to(BACKEND)), _code_only(p.read_text())) for p in files]


def test_worker_and_task_modules_never_reference_the_request_principal() -> None:
    """D-19: a task knows its tenant only from its explicit ``tenant_id`` argument."""
    sources = _worker_and_task_sources()
    assert {rel for rel, _ in sources} >= {"app/worker/runner.py", "app/ingest/imports.py"}
    offenders = [
        f"{rel}:{src.count(chr(10), 0, m.start()) + 1}: {m.group(0)}"
        for rel, src in sources
        for m in _PRINCIPAL_WORDS.finditer(src)
    ]
    assert offenders == []


def test_worker_isolation_check_would_catch_a_reference() -> None:
    """Mutation check in-process: the pattern finds each forbidden name."""
    for word in ("Principal", "active_tenant_id", "firm_ids"):
        assert _PRINCIPAL_WORDS.search(f"x = actor.{word}") is not None
    assert _PRINCIPAL_WORDS.search("tenant_id = payload['tenant_id']") is None


def test_local_object_store_directory_is_ignored_and_untracked() -> None:
    assert ".object-store/" in (REPO / ".gitignore").read_text().splitlines()
    if subprocess.run(["git", "rev-parse"], cwd=REPO, capture_output=True).returncode != 0:
        pytest.skip("not a git checkout; CI enforces this")
    tracked = subprocess.run(
        ["git", "ls-files", ".object-store"], cwd=REPO, capture_output=True, text=True
    )
    assert tracked.stdout.strip() == "", "files under .object-store are tracked"


# --- F03 close-out: money on the frontend is text, never a binary float ------------------------


def test_frontend_never_parses_money_with_parsefloat_or_number() -> None:
    """One formatter (``frontend/src/money.js``) handles money as digits; no file
    under ``frontend/src`` calls ``parseFloat`` or ``Number(`` at all."""
    src = REPO / "frontend" / "src"
    hits = []
    for p in sorted(src.rglob("*")):
        if p.suffix in (".js", ".jsx") and not p.name.endswith(".test.js"):
            for i, line in enumerate(p.read_text().splitlines(), 1):
                code = line.split("//", 1)[0]  # comments may name the forbidden calls
                if re.search(r"\bparseFloat\s*\(|\bNumber\s*\(", code):
                    hits.append(f"{p.relative_to(src)}:{i}")
    assert hits == []
    money = (src / "money.js").read_text()
    assert "BigInt(" in money  # digits handled as integers, not as a binary float
