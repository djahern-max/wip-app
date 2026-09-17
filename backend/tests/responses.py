"""Every response body the suite receives, for the end-of-run scan
(``test_zz_response_scan.py``): no credential column name or stored value may
appear in any JSON body."""

from dataclasses import dataclass

RESPONSES: list["Captured"] = []


@dataclass(frozen=True)
class Captured:
    method: str
    path: str
    status: int
    body: str


def record_response(method: str, path: str, status: int, body: bytes | str) -> None:
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    RESPONSES.append(Captured(method, path, status, text))
