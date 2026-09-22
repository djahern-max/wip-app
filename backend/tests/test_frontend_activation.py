"""F05.1 small fix: the activation page survives a reload. The token stays in the
address fragment until the link has been used (a reload before "Save password" still
has it), and is dropped right after the activation request. Static checks, like
``test_frontend_effects``; the server side of a reload mid-enrolment is in
``test_activation.py``."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "frontend/src/App.jsx"
ACTIVATE = REPO / "frontend/src/pages/Activate.jsx"


def test_app_reads_the_token_from_the_fragment_without_clearing_it() -> None:
    src = APP.read_text()
    assert "window.location.hash" in src
    assert "replaceState" not in src, "App.jsx must not drop the token before it is used"
    assert "__activationToken" not in src


def test_activate_drops_the_token_only_after_the_request() -> None:
    src = ACTIVATE.read_text()
    post = src.index('api("POST", "/api/auth/activate"')
    clear = src.index("window.history.replaceState")
    assert post < clear, "the address is cleared after the activation request, not before"
    # The clear sits inside the try block that awaits the request, before any branch.
    between = src[post:clear]
    assert "await" in src[post - 40 : post] and not re.search(r"\bif\s*\(", between)
