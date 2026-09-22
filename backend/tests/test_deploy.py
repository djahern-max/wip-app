"""F05.0 · First deployment: the repo side of the acceptance criteria. (a) the
production env templates declare the same names as ``.env.example``; (b) the three
static pages Intuit requires; (c) the nginx headers and site; (d) the systemd units;
(e) ``deploy.sh`` migrates before it restarts under a lock; (f) ``prod_check.py`` on
the test database, with the S3 HEAD exercised through botocore's Stubber. No test
opens a network connection to the droplet, the cluster or the Space."""

import importlib.util
import re
import subprocess
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber
from sqlalchemy import Engine

from app.core.storage import LocalObjectStore, S3ObjectStore
from app.tenancy import catalog
from tests.test_product_name import _NAME as BARE_PRODUCT_NAME
from tests.test_product_name import DOMAIN

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"
PUBLIC = REPO / "frontend" / "public"

# --- (a) environment files --------------------------------------------------------------------

_DECLARED = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=", re.M)

# Development-only names, absent from the production template on purpose.
DEV_ONLY = {
    "TEST_DATABASE_URL": "the test suite's database",
    "TEST_DATABASE_OWNER_URL": "the test suite's database",
    "POSTGRES_HOST_PORT": "the compose Postgres port",
    "LOCAL_OBJECT_STORE_DIR": "OBJECT_STORE=local only; production is the Space",
}
# Blank in the template: the owner fills them on the server, never in the repo.
SECRETS = {
    "DATABASE_URL",
    "CRYPTO_KEYS",
    "SPACES_ACCESS_KEY_ID",
    "SPACES_SECRET_ACCESS_KEY",
    "QBO_CLIENT_ID",
    "QBO_CLIENT_SECRET",
}
PRODUCTION_VALUES = {
    "ENV_FILE": "/etc/wip/app.env",
    "APP_BASE_URL": "https://jobcost.dev",
    "SESSION_COOKIE_SECURE": "true",
    "TRUSTED_PROXY_COUNT": "1",
    "OBJECT_STORE": "s3",
    "QBO_ENVIRONMENT": "sandbox",
    "QBO_REDIRECT_URI": "https://jobcost.dev/api/qbo/callback",
    "CRYPTO_ACTIVE_KEY_ID": "prod1",
    "MAX_UPLOAD_BYTES": "26214400",
}


def names(path: Path) -> list[str]:
    """Names *declared* in an env file: a line ``NAME=`` or ``# NAME=``."""
    return _DECLARED.findall(path.read_text())


def values(path: Path) -> dict[str, str]:
    """Uncommented assignments only."""
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def test_env_template_declares_the_same_names_as_env_example() -> None:
    example = names(REPO / ".env.example")
    template = names(DEPLOY / "env.template")
    assert len(set(example)) == len(example) and len(set(template)) == len(template)
    expected = [n for n in example if n not in DEV_ONLY and n != "DATABASE_OWNER_URL"]
    assert template == expected, (
        "deploy/env.template and .env.example declare different names (or a different "
        f"order): only in template {sorted(set(template) - set(expected))}, only in example "
        f"{sorted(set(expected) - set(template))}"
    )
    assert set(DEV_ONLY) <= set(example)  # the exclusions are real names
    assert "DATABASE_OWNER_URL" in example


def test_env_template_production_values_and_blank_secrets() -> None:
    assigned = values(DEPLOY / "env.template")
    for name in SECRETS:
        assert assigned.get(name) == "", f"{name} must be blank in the template"
    for name, value in PRODUCTION_VALUES.items():
        assert assigned.get(name) == value, name
    # The template is a working file, not a commented catalogue: every name assigned.
    assert set(names(DEPLOY / "env.template")) == set(assigned)
    # No key material, password or token shape anywhere in either template.
    for f in ("env.template", "migrate.env.template"):
        text = (DEPLOY / f).read_text()
        assert not re.search(r"[A-Za-z0-9+/]{43}=", text), f
        assert not re.search(r"://\w+:(?!PASSWORD@)[^@<\s]+@", text), (
            f"a URL with a password in {f}"
        )


def test_migrate_env_template_holds_the_owner_url_and_the_guard_only() -> None:
    assert names(DEPLOY / "migrate.env.template") == [
        "DATABASE_OWNER_URL",
        "PROTECTED_DATABASE_NAMES",
    ]
    assigned = values(DEPLOY / "migrate.env.template")
    assert assigned["DATABASE_OWNER_URL"] == ""
    assert (
        assigned["PROTECTED_DATABASE_NAMES"]
        == values(DEPLOY / "env.template")["PROTECTED_DATABASE_NAMES"]
    )


def test_name_check_fails_when_a_name_is_added_to_one_file_only(tmp_path: Path) -> None:
    """Mutation check in-process (criterion 8): the comparison is sensitive to one
    extra name on either side, commented or not."""
    example = (REPO / ".env.example").read_text()
    template = (DEPLOY / "env.template").read_text()
    expected = [
        n for n in names(REPO / ".env.example") if n not in DEV_ONLY and n != "DATABASE_OWNER_URL"
    ]
    assert names(DEPLOY / "env.template") == expected
    (tmp_path / "a").write_text(template + "\nNEW_ONLY_HERE=1\n")
    assert names(tmp_path / "a") != expected
    (tmp_path / "b").write_text(example + "\n# NEW_ONLY_THERE=\n")
    assert [
        n for n in names(tmp_path / "b") if n not in DEV_ONLY and n != "DATABASE_OWNER_URL"
    ] != names(DEPLOY / "env.template")


# --- (b) the three static pages -------------------------------------------------------------

PAGES = ("privacy.html", "terms.html", "qbo-disconnected.html")
ENTITY_LINE = f"{DOMAIN} is operated by Ryze Group, Inc., a New Hampshire corporation."
CONTACT = f"admin@{DOMAIN}"

# One marker phrase per statement the brief requires on the privacy page.
PRIVACY_STATEMENTS = {
    "what is read from QuickBooks, read-only, own company": (
        "Access is read-only and covers only the client's own company"
    ),
    "what is read from uploaded files": "estimates, timesheets and payroll registers",
    "why": "job cost and work-in-progress reporting for that",
    "who can see it": "authorized users according to the role",
    "where it is stored": "DigitalOcean in the United States",
    "tokens encrypted; private object storage": "encrypted at the application layer",
    "not sold or shared": "not sold, not shared with third parties",
    "retention and deletion; tokens removed on disconnect": (
        "revoked and removed as soon as the connection is disconnected"
    ),
    "how to disconnect, and the security contact": "Apps, then Manage apps, then Disconnect",
}


@pytest.mark.parametrize("page", PAGES)
def test_static_page_is_plain_html_with_the_entity_line_address_and_date(page: str) -> None:
    html = " ".join((PUBLIC / page).read_text().split())
    assert "<script" not in html and "<style" not in html and "style=" not in html
    assert 'lang="en"' in html and 'name="viewport"' in html
    assert '<link rel="stylesheet" href="/pages.css" />' in html
    assert html.count("<main") == 1 and "<footer" in html
    assert ENTITY_LINE in html
    assert CONTACT in html
    assert re.search(r"Last updated \d{4}-\d{2}-\d{2}\.", html)
    # The domain is named once (the entity line); the address is not a second naming.
    assert len(re.findall(rf"(?<!@){re.escape(DOMAIN)}", html)) == 1
    # No bare product name: D-09's rename stays one edit per page (the entity line).
    assert BARE_PRODUCT_NAME.search(html) is None


def test_privacy_page_carries_every_required_statement() -> None:
    html = " ".join((PUBLIC / "privacy.html").read_text().split())
    missing = [k for k, phrase in PRIVACY_STATEMENTS.items() if phrase not in html]
    assert missing == []
    assert "Security contact" in html
    assert "/terms" in html


def test_terms_page_carries_the_required_statements() -> None:
    html = " ".join((PUBLIC / "terms.html").read_text().split())
    for phrase in (
        "provided by Ryze Group, Inc.",
        "remains the client's book of record",
        "writes nothing to it",
        "responsible for the accuracy and completeness of its own data",
        "No warranty beyond the data supplied",
        "Limitation of liability",
        "Termination and return of data",
        "laws of the State of New Hampshire",
    ):
        assert phrase in html, phrase
    assert "/privacy" in html


def test_disconnect_page_says_what_happened_and_what_to_do() -> None:
    html = (PUBLIC / "qbo-disconnected.html").read_text()
    assert "was ended from QuickBooks" in html
    assert '<a href="/">Sign in</a> to reconnect' in html


def test_pages_stylesheet_is_plain() -> None:
    css = re.sub(r"/\*.*?\*/", "", (PUBLIC / "pages.css").read_text(), flags=re.S)
    assert re.search(r"font-family:\s*system-ui", css)
    assert css.count("--accent:") == 1
    for forbidden in (
        "gradient(",
        "box-shadow",
        "animation",
        "transition",
        "@keyframes",
        "url(",
        "@import",
    ):
        assert forbidden not in css, forbidden


def test_nvmrc_pins_the_node_major_ci_uses() -> None:
    major = (REPO / "frontend" / ".nvmrc").read_text().strip()
    assert major == "24"
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "node-version-file: frontend/.nvmrc" in ci
    assert "dist/index.html" in ci and "qbo-disconnected.html" in ci


# --- (c) nginx ------------------------------------------------------------------------------

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)


def _no_comments(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def test_security_headers_snippet_carries_the_exact_csp_and_the_other_headers() -> None:
    conf = _no_comments((DEPLOY / "nginx" / "security-headers.conf").read_text())
    assert f'add_header Content-Security-Policy "{CSP}" always;' in conf
    assert (
        'add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;' in conf
    )
    assert "preload" not in conf  # no HSTS preload without a decision
    assert 'add_header X-Content-Type-Options "nosniff" always;' in conf
    assert 'add_header Referrer-Policy "strict-origin-when-cross-origin" always;' in conf
    assert 'add_header X-Frame-Options "DENY" always;' in conf
    assert conf.count("add_header") == 5


def test_site_config_serves_the_pages_proxies_the_api_and_sets_no_header_outside_the_include() -> (
    None
):
    conf = _no_comments((DEPLOY / "nginx" / "jobcost.dev.conf").read_text())
    assert "client_max_body_size 25m;" in conf
    assert conf.count("server_tokens off;") == 3
    assert "location = /privacy {\n        try_files /privacy.html =404;" in conf
    assert "location = /terms {\n        try_files /terms.html =404;" in conf
    assert "location = /qbo/disconnected {\n        try_files /qbo-disconnected.html =404;" in conf
    assert "proxy_pass http://127.0.0.1:8000;" in conf
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in conf
    assert "proxy_set_header X-Forwarded-Proto https;" in conf
    assert "$proxy_add_x_forwarded_for" not in conf  # set, never appended
    assert "return 301 https://jobcost.dev$request_uri;" in conf
    assert "try_files $uri /index.html;" in conf
    assert "root /opt/wip/frontend/dist;" in conf
    assert "/etc/letsencrypt/live/jobcost.dev/" in conf
    # The only add_header lines outside the snippet are Cache-Control, and every
    # location that has one includes the snippet again.
    extra = [ln.strip() for ln in conf.splitlines() if "add_header" in ln]
    assert all(ln.startswith("add_header Cache-Control") for ln in extra), extra
    for block in re.findall(r"location[^{]*\{([^}]*)\}", conf):
        if "add_header" in block:
            assert "include snippets/wip-security-headers.conf;" in block
    assert conf.count("include snippets/wip-security-headers.conf;") == 2 + len(extra)
    # The bootstrap site answers the challenge only.
    boot = _no_comments((DEPLOY / "nginx" / "bootstrap-http.conf").read_text())
    assert "listen 80;" in boot and "ssl" not in boot and "/.well-known/acme-challenge/" in boot
    tls = _no_comments((DEPLOY / "nginx" / "tls.conf").read_text())
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in tls and "DHE-RSA" not in tls.replace("ECDHE", "")


# --- (d) systemd ----------------------------------------------------------------------------


@pytest.mark.parametrize("unit", ("wip-api.service", "wip-worker.service"))
def test_units_run_as_wip_with_the_app_env_and_restart(unit: str) -> None:
    text = _no_comments((DEPLOY / "systemd" / unit).read_text())
    for line in (
        "User=wip",
        "EnvironmentFile=/etc/wip/app.env",
        "Restart=always",
        "After=network-online.target",
        "WorkingDirectory=/opt/wip/backend",
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "PrivateTmp=true",
        "WantedBy=multi-user.target",
    ):
        assert line in text, f"{unit}: {line}"
    assert "migrate.env" not in text
    assert "DATABASE_OWNER_URL" not in text


def test_api_unit_binds_loopback_with_two_workers_and_worker_unit_waits_a_lease() -> None:
    api = (DEPLOY / "systemd" / "wip-api.service").read_text()
    assert (
        "ExecStart=/opt/wip/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 "
        "--port 8000 --workers 2 --proxy-headers --forwarded-allow-ips 127.0.0.1" in api
    )
    worker = (DEPLOY / "systemd" / "wip-worker.service").read_text()
    assert "ExecStart=/opt/wip/backend/.venv/bin/python -m app.worker" in worker
    assert "TimeoutStopSec=330" in worker and "KillSignal=SIGTERM" in worker


# --- (e) the scripts ------------------------------------------------------------------------


@pytest.mark.parametrize("script", ("setup.sh", "deploy.sh"))
def test_scripts_parse_and_are_strict(script: str) -> None:
    path = DEPLOY / script
    proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    text = path.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert path.stat().st_mode & 0o111, f"{script} is not executable"
    # No secret and no hostname other than jobcost.dev (and github.com / NodeSource).
    for host in re.findall(r"\b[a-z0-9-]+\.(?:com|net|org|io|dev)\b", text):
        assert host in {DOMAIN, "github.com", "nodesource.com"}, host


def test_deploy_script_locks_then_migrates_before_it_restarts() -> None:
    lines = _no_comments((DEPLOY / "deploy.sh").read_text()).splitlines()

    def first(pattern: str) -> int:
        return next(i for i, ln in enumerate(lines) if re.search(pattern, ln))

    lock = first(r"\bflock\b")
    build = first(r"vite build --outDir dist.new")
    migrate = first(r"alembic\" upgrade head|alembic upgrade head")
    swap = first(r"mv dist.new dist")
    restart = first(r"systemctl restart wip-api wip-worker")
    assert lock < build < migrate < swap < restart < first(r"\bcurl -sS")
    # Every git command runs as wip (root refuses a checkout owned by another user).
    bare_git = [ln for ln in lines if re.search(r"\bgit\b", ln) and "git_wip()" not in ln]
    assert bare_git == [], bare_git  # only the git_wip definition names git itself
    assert (
        sum(1 for ln in lines if re.search(r"\bgit_wip ", ln)) >= 4
    )  # rev-parse ×2, fetch, checkout
    # The health step tells a 503 with a body apart from no answer.
    text_after_restart = "\n".join(lines[restart:])
    assert '"db":"unavailable"' in text_after_restart and "no answer from" in text_after_restart
    assert lines.count("systemctl restart wip-api wip-worker") == 1
    text = "\n".join(lines)
    assert 'MIGRATE_ENV="${MIGRATE_ENV_FILE:-/etc/wip/migrate.env}"' in text
    assert 'ENV_FILE="$MIGRATE_ENV"' in text and "timeout 600" in text
    assert "set -a" in text and '. "$MIGRATE_ENV"' in text  # exported around alembic only
    assert "trap on_error ERR" in text
    assert "--detach" in text  # never a branch on the server


def test_setup_script_reports_every_step_and_never_overwrites_the_env_files() -> None:
    text = (DEPLOY / "setup.sh").read_text()
    assert text.count("skip ") >= 15 and text.count("done_ ") >= 15
    assert 'env.template" "$ETC_DIR/app.env"' in text
    assert 'if [ -f "$ETC_DIR/app.env" ]; then' in text
    assert 'if [ -f "$ETC_DIR/migrate.env" ]; then' in text
    assert '-o root -g root "$APP_DIR/deploy/migrate.env.template"' in text
    assert '-m 600 -o "$APP_USER" -g "$APP_USER" "$APP_DIR/deploy/env.template"' in text
    assert "certbot certonly --webroot" in text and '--deploy-hook "systemctl reload nginx"' in text
    assert "alembic" not in text and "systemctl start" not in text  # deploy.sh does those
    assert "frontend/.nvmrc" in text and "deb.nodesource.com/node_${NODE_MAJOR}.x" in text
    assert (
        "curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor"
        in text
    )
    assert "| bash" not in text and "| sh" not in text


# --- (f) prod_check.py ----------------------------------------------------------------------


def _prod_check():
    spec = importlib.util.spec_from_file_location(
        "prod_check", REPO / "backend" / "scripts" / "prod_check.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _stubbed_store() -> tuple[S3ObjectStore, Stubber]:
    client = boto3.client(
        "s3",
        region_name="nyc3",
        endpoint_url="https://nyc3.digitaloceanspaces.com",
        aws_access_key_id="stub-key-id",
        aws_secret_access_key="stub-secret",
    )
    return S3ObjectStore("stub-bucket", client), Stubber(client)


def test_prod_check_passes_on_the_test_database(
    migrated_db: None, rw_engine: Engine, tmp_path: Path
) -> None:
    """As ``app_rw``: every check passes except SSL when the local Postgres has none
    (the compose and CI containers do not; production must)."""
    pc = _prod_check()
    results = pc.run_checks(rw_engine, LocalObjectStore(tmp_path))
    failed = {name for name, failures in results if failures}
    assert failed <= {"connection over SSL"}, results
    assert {name for name, _ in results} >= {
        "role is app_rw, NOSUPERUSER, NOBYPASSRLS",
        "role owns no table",
        "connection over SSL",
        "tenant tables enumerated",
        "RLS enabled and forced with tenant_isolation on every tenant table",
        "no policy outside the D-11 allow-list",
        "append-only triggers present and enabled",
        "append-only register matches the catalog",
        "object store is local (no bucket to check)",
    }
    lines, code = pc.report(results)
    assert code == (1 if failed else 0)
    assert any(ln.startswith("ok: role is app_rw") for ln in lines)
    joined = "\n".join(lines)
    for secret in ("app_rw_dev", "localhost", "5433", "wip_test", "postgresql"):
        assert secret not in joined


def test_prod_check_fails_as_the_owner_role(
    migrated_db: None, owner_engine: Engine, tmp_path: Path
) -> None:
    pc = _prod_check()
    results = dict(pc.run_checks(owner_engine, LocalObjectStore(tmp_path)))
    assert results["role is app_rw, NOSUPERUSER, NOBYPASSRLS"] == [
        "connected as app_owner, expected app_rw"
    ]
    assert results["role owns no table"]  # app_owner owns every table
    assert all(
        f.startswith("owned by the application role: ") for f in results["role owns no table"]
    )


def test_prod_check_names_membership_when_the_allow_list_is_empty(
    migrated_db: None, rw_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pc = _prod_check()
    monkeypatch.setattr(catalog, "EXTRA_POLICIES", frozenset())
    results = dict(pc.run_checks(rw_engine, LocalObjectStore(tmp_path)))
    (failure,) = results["no policy outside the D-11 allow-list"]
    assert failure.startswith("unlisted policy on membership: own_membership_read FOR SELECT")
    lines, code = pc.report(list(results.items()))
    assert code == 1 and any(ln.startswith("FAIL: no policy outside") for ln in lines)


def test_prod_check_bucket_head_success_and_refusal_through_the_stubber() -> None:
    pc = _prod_check()
    store, stub = _stubbed_store()
    stub.add_response("head_bucket", {}, {"Bucket": "stub-bucket"})
    stub.add_client_error(
        "head_bucket", service_error_code="403", expected_params={"Bucket": "stub-bucket"}
    )
    with stub:
        assert pc.bucket_check(store) == []
        assert pc.bucket_check(store) == ["HEAD on the bucket failed (403)"]
    stub.assert_no_pending_responses()


def test_prod_check_output_never_carries_a_url_key_or_host() -> None:
    src = (REPO / "backend" / "scripts" / "prod_check.py").read_text()
    # Nothing formats a settings value or a row into a message.
    assert "database_url" not in src and "spaces_" not in src and "settings." not in src
    assert "print(" in src and src.count("print(") == 1
