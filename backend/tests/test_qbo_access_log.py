"""F05 (owner answer 1): the access log never holds the callback's ``code`` or
``state``. Proven against a real uvicorn server, whose access logger is the one that
prints request lines, and against the filter on its own."""

import logging
import socket
import threading
import time

import httpx
import uvicorn

from app.api.qbo import CALLBACK_PATH, CallbackQueryFilter, install_access_log_filter
from app.main import create_app
from tests.leaks import record_secret
from tests.logcapture import RECORDS


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_real_server_logs_the_callback_without_its_query_string(migrated_db: None) -> None:
    code, state = "code-accesslog-0123456789abcdef", "state-accesslog-0123456789abcdef"
    record_secret("oauth_code", code)
    record_secret("oauth_state", state)
    port = _free_port()
    # log_config=None: uvicorn leaves logging alone, so its records reach the capture.
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_config=None, lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    before = len(RECORDS)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        with httpx.Client(transport=httpx.HTTPTransport()) as http:  # a real socket, to localhost
            r = http.get(
                f"http://127.0.0.1:{port}{CALLBACK_PATH}",
                params={"code": code, "state": state, "realmId": "123"},
            )
            assert r.status_code == 303 and "result=state_invalid" in r.headers["location"]
            health = http.get(f"http://127.0.0.1:{port}/api/health", params={"probe": "kept"})
            assert health.status_code == 200
    finally:
        server.should_exit = True
        thread.join(10)
    access = [m for n, m in RECORDS[before:] if n == "uvicorn.access"]
    assert any(f'"GET {CALLBACK_PATH} HTTP/1.1" 303' in m for m in access), access
    assert any("probe=kept" in m for m in access)  # other routes keep their query string
    everything = "\n".join(m for _, m in RECORDS[before:])
    assert code not in everything and state not in everything


def test_the_filter_is_installed_once_and_only_touches_the_callback() -> None:
    install_access_log_filter()
    install_access_log_filter()
    filters = [
        f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, CallbackQueryFilter)
    ]
    assert len(filters) == 1

    def record(path: str) -> logging.LogRecord:
        return logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            "",
            0,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1", "GET", path, "1.1", 303),
            None,
        )

    hit = record(f"{CALLBACK_PATH}?code=abc&state=def")
    assert filters[0].filter(hit) and hit.getMessage().endswith(
        f'"GET {CALLBACK_PATH} HTTP/1.1" 303'
    )
    other = record("/api/imports?x=1")
    assert filters[0].filter(other) and "x=1" in other.getMessage()
