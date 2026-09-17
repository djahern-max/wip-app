"""No non-idempotent request from a React effect (F02.1 enrolment idempotency fix).

React 18 StrictMode runs every mount effect twice in dev, and a refresh or a remount
does the same in production: a ``useEffect`` body that sends a POST/PUT/PATCH/DELETE
sends it twice. That is how ``TotpEnrol.jsx`` came to show one secret while the server
held another. This static check fails when an effect in ``frontend/src`` calls
``api(`` with a method other than GET, directly or through a function defined in the
same file, unless the file is listed in ``GUARDED`` with the reason its effect is safe.

Limits, by design (no JS parser, no new dependency): it reads one file at a time, so
a mutating helper imported from another module is not followed; ``api(`` with a
method that is not a string literal, and ``fetch(`` with a ``method`` option, are
refused outright inside an effect rather than interpreted.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FRONTEND_SRC = REPO / "frontend/src"

# file (relative to frontend/src) → why its non-GET effect cannot send twice.
GUARDED: dict[str, str] = {
    "pages/TotpEnrol.jsx": "one request per mount, held in a ref; the server is idempotent too",
}

_CLOSERS = {"(": ")", "{": "}", "[": "]"}
_API_CALL = re.compile(r"\bapi\(\s*(?:([\"'])(\w+)\1)?")
_FETCH_WITH_METHOD = re.compile(r"\bfetch\([^;]*?\bmethod\s*:", re.S)
_FUNCTION = re.compile(r"\bfunction\s+(\w+)\s*\(")
_CONST = re.compile(r"\b(?:const|let)\s+(\w+)\s*=\s*(?:async\s*)?")


def _skip_literal(src: str, i: int) -> int | None:
    """If a string, template literal or comment starts at ``i``, the index after it."""
    ch = src[i]
    if ch in "\"'`":
        j = i + 1
        while j < len(src) and src[j] != ch:
            j += 2 if src[j] == "\\" else 1
        return j + 1
    if src.startswith("//", i):
        end = src.find("\n", i)
        return len(src) if end == -1 else end
    if src.startswith("/*", i):
        end = src.find("*/", i)
        return len(src) if end == -1 else end + 2
    return None


def _without_comments(src: str) -> str:
    """Comments blanked out (newlines kept, so line numbers hold); strings untouched."""
    out: list[str] = []
    i = 0
    while i < len(src):
        after = _skip_literal(src, i)
        if after is None:
            out.append(src[i])
            i += 1
            continue
        chunk = src[i:after]
        out.append(chunk if src[i] in "\"'`" else re.sub(r"[^\n]", " ", chunk))
        i = after
    return "".join(out)


def _balanced_end(src: str, open_idx: int) -> int:
    """Index of the bracket that closes the one at ``open_idx``."""
    stack = [_CLOSERS[src[open_idx]]]
    i = open_idx + 1
    while i < len(src):
        after = _skip_literal(src, i)
        if after is not None:
            i = after
            continue
        ch = src[i]
        if ch in _CLOSERS:
            stack.append(_CLOSERS[ch])
        elif ch == stack[-1]:
            stack.pop()
            if not stack:
                return i
        i += 1
    raise ValueError("unbalanced source")


def _mutating_calls(code: str) -> list[str]:
    found = []
    for m in _API_CALL.finditer(code):
        method = m.group(2)
        if method is None:
            found.append("api( with a method that is not a string literal")
        elif method.upper() != "GET":
            found.append(f'api("{method}")')
    if _FETCH_WITH_METHOD.search(code):
        found.append("fetch( with a method option")
    return found


def _function_bodies(src: str) -> dict[str, str]:
    """Name → source of each function declared in the file (declarations, and
    ``const f = (…) => {…}`` / ``const f = useCallback(…)`` forms)."""
    bodies: dict[str, str] = {}
    for m in _FUNCTION.finditer(src):
        params_end = _balanced_end(src, m.end() - 1)
        brace = src.find("{", params_end)
        if brace != -1:
            bodies[m.group(1)] = src[brace : _balanced_end(src, brace) + 1]
    for m in _CONST.finditer(src):
        paren = m.end()
        while paren < len(src) and (src[paren].isalnum() or src[paren] in "_."):
            paren += 1  # a wrapper such as useCallback
        if paren >= len(src) or src[paren] != "(":
            continue
        end = _balanced_end(src, paren)
        body = src[paren : end + 1]
        arrow = re.match(r"\s*=>\s*\{", src[end + 1 :])
        if arrow:
            brace = end + 1 + arrow.end() - 1
            body += src[brace : _balanced_end(src, brace) + 1]
        bodies[m.group(1)] = body
    return bodies


def effect_findings(src: str) -> tuple[int, list[str]]:
    """``(number of effects, findings)`` for one source file."""
    src = _without_comments(src)
    mutating = {
        name: calls
        for name, body in _function_bodies(src).items()
        if (calls := _mutating_calls(body))
    }
    findings: list[str] = []
    effects = 0
    for m in re.finditer(r"\buse(?:Layout)?Effect\(", src):
        effects += 1
        body = src[m.end() - 1 : _balanced_end(src, m.end() - 1) + 1]
        line = src.count("\n", 0, m.start()) + 1
        for call in _mutating_calls(body):
            findings.append(f"line {line}: {call}")
        for name, calls in mutating.items():
            if re.search(rf"\b{name}\b", body):
                findings.append(f"line {line}: {name}() → {calls[0]}")
    return effects, findings


def _scan_tree() -> tuple[int, dict[str, list[str]]]:
    total = 0
    offenders: dict[str, list[str]] = {}
    for path in sorted(FRONTEND_SRC.rglob("*")):
        if path.suffix not in (".js", ".jsx"):
            continue
        effects, findings = effect_findings(path.read_text())
        total += effects
        if findings:
            offenders[path.relative_to(FRONTEND_SRC).as_posix()] = findings
    return total, offenders


# --- the scanner itself -----------------------------------------------------------------------


def test_scanner_flags_the_original_enrolment_effect() -> None:
    src = """
      useEffect(() => {
        let cancelled = false;
        api("POST", "/api/auth/totp/enrol").then((s) => { if (!cancelled) setSetup(s); });
        return () => { cancelled = true; };
      }, []);
    """
    assert effect_findings(src) == (1, ['line 2: api("POST")'])


def test_scanner_passes_reads_and_requests_outside_effects() -> None:
    src = """
      useEffect(() => {
        api("GET", "/api/session/tenants").then(setTenants);
        fetch("/api/health").then((r) => r.json()).then(setHealth);
        // api("POST", "/commented/out")
      }, [me.active_tenant_id]);
      async function submit(e) { await api("POST", "/api/auth/login", { email }); }
    """
    assert effect_findings(src) == (1, [])


def test_scanner_follows_a_function_defined_in_the_same_file() -> None:
    src = """
      const save = useCallback(() => { api("PUT", "/api/thing", body); }, [body]);
      async function leave() { await api("DELETE", "/api/thing"); }
      useEffect(save, [save]);
      useEffect(() => { leave(); }, []);
    """
    effects, findings = effect_findings(src)
    assert effects == 2
    assert findings == ['line 4: save() → api("PUT")', 'line 5: leave() → api("DELETE")']


def test_scanner_refuses_what_it_cannot_read() -> None:
    src = """
      useEffect(() => { api(method, path); }, []);
      useEffect(() => { fetch("/api/x", { method: "POST" }); }, []);
    """
    _, findings = effect_findings(src)
    assert findings == [
        "line 2: api( with a method that is not a string literal",
        "line 3: fetch( with a method option",
    ]


# --- the tree ---------------------------------------------------------------------------------


def test_no_effect_sends_a_non_get_request_outside_the_guarded_list() -> None:
    total, offenders = _scan_tree()
    # App.jsx, Shell.jsx, TotpEnrol.jsx: if the scanner stops seeing effects, it has rotted.
    assert total >= 3, "the scanner found fewer effects than the frontend is known to have"
    unguarded = {f: found for f, found in offenders.items() if f not in GUARDED}
    assert not unguarded, (
        "a useEffect sends a non-idempotent request (StrictMode, a refresh or a remount "
        f"sends it twice): {unguarded}. Move it to an event handler, or guard it and add "
        "the file to GUARDED with the reason."
    )


def test_guarded_list_has_no_stale_entries() -> None:
    _, offenders = _scan_tree()
    stale = sorted(set(GUARDED) - set(offenders))
    assert not stale, f"GUARDED names files with no non-GET effect left: {stale}"


def test_guarded_enrolment_effect_reuses_one_request() -> None:
    """The guard the allow-list entry stands for: the request lives in a ref and is
    created only when the ref is empty."""
    src = (FRONTEND_SRC / "pages/TotpEnrol.jsx").read_text()
    assert re.search(r"const enrolRequest = useRef\(null\)", src)
    assert re.search(
        r"if \(!enrolRequest\.current\) enrolRequest\.current = "
        r'api\("POST", "/api/auth/totp/enrol"\)',
        src,
    )
    assert "<React.StrictMode>" in (FRONTEND_SRC / "main.jsx").read_text()
