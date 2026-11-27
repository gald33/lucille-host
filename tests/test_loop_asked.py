"""What a request's ``received`` object says it asked for (``loop._asked``), for each kind of
operation: one with a path, one that works in a whole folder with no path, and one with no
folder. A valid request must never make it raise: ``run --json-events`` calls it on every
request, and an exception there stops ``run``."""

from __future__ import annotations

from typing import Any

import pytest

from lucille_host import loop


def _body(op: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "v": "lucille.exec/1",
        "dir": "request",
        "job_id": "00000000-0000-4000-8000-000000000001",
        "op": op,
        "args": args,
        "not_after": 1767229200,
    }


@pytest.mark.parametrize(
    ("op", "args", "asked"),
    [
        ("files.sha256", {"root": "docs", "path": "a.txt"}, ("files.sha256", "docs", "a.txt")),
        ("files.sha256", {"root": "docs", "path": "/etc/passwd"}, ("files.sha256", "docs", None)),
        (
            "files.search",
            {"root": "docs", "pattern": "x", "fixed_strings": True, "max_count": 1},
            ("files.search", "docs", None),
        ),
        ("host.sleep", {"n": 1}, ("host.sleep", None, None)),
        ("host.describe", {}, ("host.describe", None, None)),
    ],
)
def test_asked(op: str, args: dict[str, Any], asked: tuple[str | None, ...]) -> None:
    assert loop._asked(_body(op, args)) == asked
