"""tools/guards/_reexports.py covers the interpreter running the tests, and was written for the
runtime's current import allowlist.

The AST lint reads that table to know which module an attribute of an allowed standard-library
module is (``tempfile._os`` is ``os``). The table is the union over the interpreters it was written
with; CI runs this test on each Python version and operating system it tests, so a module attribute
one of them has and the table lacks fails here.
"""

from __future__ import annotations

import sys

from tools.guards import _reexports, reexport_probe

REGENERATE = (
    "write the table again: python tools/guards/reexport_probe.py <python> [<python> ...], "
    "naming every interpreter CI tests"
)


def test_the_table_scans_the_runtime_allowlist() -> None:
    assert _reexports.SCANNED == set(reexport_probe.scanned_modules()), REGENERATE


def test_the_table_covers_this_interpreter() -> None:
    found, submodules = reexport_probe.probe(sys.executable)
    missing = sorted(
        f"{name} -> {target}"
        for name, target in found.items()
        if target not in _reexports.REEXPORTS.get(name, ())
    )
    assert missing == [], REGENERATE
    assert sorted(submodules - _reexports.SUBMODULES) == [], REGENERATE


def test_the_table_holds_the_cases_that_matter() -> None:
    assert _reexports.REEXPORTS["tempfile._os"] == ("os",)
    assert _reexports.REEXPORTS["argparse._os"] == ("os",)
    assert _reexports.REEXPORTS["threading._os"] == ("os",)
    assert _reexports.REEXPORTS["enum.bltns"] == ("builtins",)
    assert _reexports.REEXPORTS["logging.handlers.socket"] == ("socket",)
    assert _reexports.REEXPORTS["tomllib._parser.string"] == ("string",)
    # An import path is not a re-export: os.path is reached as os.path.
    assert "os.path" not in _reexports.REEXPORTS
    assert "os.path" in _reexports.SUBMODULES
