"""tools/guards/_reexports.py covers the interpreter running the tests, and was written for the
runtime's current import allowlist.

The AST lint reads that table to know which module an attribute of an allowed standard-library
module is (``tempfile._os`` is ``os``). The table is the union over the interpreters it was written
with; CI runs this test on each Python version and operating system it tests, so a module attribute
one of them has and the table lacks fails here.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tools.guards import _reexports, reexport_probe

REGENERATE = (
    "write the table again: python tools/guards/reexport_probe.py <python> [<python> ...], "
    "naming every interpreter CI tests"
)
ADD = (
    "the table does not cover this interpreter. Add what it lacks: save this log and run python "
    "tools/guards/reexport_probe.py --add <log>, which reads the line below, or write the table "
    "again with every interpreter CI tests"
)


def test_the_table_scans_the_runtime_allowlist() -> None:
    assert _reexports.SCANNED == set(reexport_probe.scanned_modules()), REGENERATE


def test_the_table_covers_this_interpreter() -> None:
    # Every entry it lacks, on one line, so that a CI log of an interpreter not at hand is enough
    # to add them.
    lacking = reexport_probe.lacking(sys.executable)
    record = reexport_probe.RECORD + json.dumps(lacking, sort_keys=True)
    assert lacking["reexports"] == {} and lacking["submodules"] == [], f"{ADD}\n{record}"


def test_the_table_is_what_the_probe_writes() -> None:
    # Adding nothing writes the table as it is: it was written by the probe, not by hand.
    assert reexport_probe.added([]) == reexport_probe.TABLE.read_text()


def _values(source: str) -> dict[str, Any]:
    """The table's constants, read from its text: each a literal, a frozenset's one inside."""
    values: dict[str, Any] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            value = node.value
            if isinstance(value, ast.Call):
                values[node.target.id] = frozenset(ast.literal_eval(value.args[0]))
            elif value is not None:
                values[node.target.id] = ast.literal_eval(value)
    return values


def test_a_record_in_a_log_adds_what_it_lacked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    record = {
        "python": "CPython 3.99.0 on linux",
        "reexports": {"tempfile._os": "posix", "zz_new._re": "re"},
        "submodules": ["zz_new.sub"],
    }
    log = tmp_path / "log.txt"
    log.write_text(
        "2026-11-19T12:42:26.1Z E       AssertionError: the table does not cover\n"
        f"2026-11-19T12:42:26.2Z E       {reexport_probe.RECORD}{json.dumps(record)}\n"
    )
    table = tmp_path / "_reexports.py"
    monkeypatch.setattr(reexport_probe, "TABLE", table)
    assert reexport_probe.main(["--add", str(log)]) == 0
    written = _values(table.read_text())
    assert written["REEXPORTS"]["tempfile._os"] == ("os", "posix")
    assert written["REEXPORTS"]["zz_new._re"] == ("re",)
    assert "zz_new.sub" in written["SUBMODULES"]
    assert written["PROBED"] == tuple(sorted({*_reexports.PROBED, "CPython 3.99.0 on linux"}))
    # Everything the table held is kept.
    for name, targets in _reexports.REEXPORTS.items():
        assert set(targets) <= set(written["REEXPORTS"][name])
    assert _reexports.SUBMODULES <= written["SUBMODULES"]
    # A log with no record writes nothing.
    table.unlink()
    log.write_text("nothing here\n")
    assert reexport_probe.main(["--add", str(log)]) == 2
    assert not table.exists()
    assert "no REEXPORT-PROBE-RECORD: line" in capsys.readouterr().err


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
