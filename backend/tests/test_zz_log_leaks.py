"""Runs last (file order): every log line captured during the suite, SQLAlchemy
statement/parameter logging included, is checked against every secret the suite
generated or received. Passwords, TOTP secrets, recovery codes, session tokens and
reset tokens are checked verbatim against all records; six-digit TOTP codes are
checked with a word boundary against application records only, since a six-digit
run occurs by chance inside timestamps and hex ids in database logging."""

import re

from tests.leaks import LEAKS
from tests.logcapture import RECORDS


def test_suite_produced_logs_and_secrets() -> None:
    assert len(RECORDS) > 100, "log capture did not run"
    assert any(name.startswith("sqlalchemy.engine") for name, _ in RECORDS)
    kinds = (
        "password",
        "totp_secret",
        "recovery_code",
        "session_token",
        "totp_code",
        "activation_token",
        "attempted_email",
        "connection_token",  # F03
    )
    for kind in kinds:
        assert LEAKS[kind], f"no {kind} was recorded by the suite"


def test_no_secret_appears_in_any_log_line() -> None:
    leaks: list[str] = []
    verbatim = [
        "password",
        "totp_secret",
        "recovery_code",
        "session_token",
        "activation_token",
        "connection_token",
    ]
    for kind in verbatim:
        for secret in LEAKS[kind]:
            for name, msg in RECORDS:
                if secret in msg:
                    leaks.append(f"{kind} in {name}: {msg[:120]}")
                    break
    app_records = [(n, m) for n, m in RECORDS if not n.startswith("sqlalchemy")]
    for code in LEAKS["totp_code"]:
        pattern = re.compile(rf"(?<![0-9A-Za-z]){re.escape(code)}(?![0-9A-Za-z])")
        for name, msg in app_records:
            if pattern.search(msg):
                leaks.append(f"totp_code in {name}: {msg[:120]}")
                break
    # An attempted e-mail address is looked up with a bound parameter, which the
    # harness logs on purpose (production hides parameters, see test_hygiene); the
    # application itself must never log it (F02.1).
    for email in LEAKS["attempted_email"]:
        for name, msg in app_records:
            if email.lower() in msg.lower():
                leaks.append(f"attempted_email in {name}: {msg[:120]}")
                break
    assert not leaks, "\n".join(leaks)
