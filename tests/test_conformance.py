"""The conformance suite's harness: the registry, the runner's verdict, and the suite itself, run
as CI runs it (``python -m lucille_host.conformance``).

The run of the whole suite starts a real hub and the installed host as processes of their own. It
is skipped when the tests run as root, where the host refuses to start; CI runs them as an ordinary
user. As root, the suite itself must still fail, never pass.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from typing import Any

import pytest

from lucille_host.conformance import _scenarios, _spawn
from lucille_host.conformance._scenarios import Context, Failed, Scenario, run_all

ROOT = os.geteuid() == 0


class FakeSession:
    """A session that starts nothing."""

    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.closed = False

    def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("no hub")

    def close(self) -> None:
        self.closed = True


def _run(registry: list[Scenario], **options: Any) -> tuple[int, str, FakeSession]:
    session = FakeSession(**options)
    out, err = io.StringIO(), io.StringIO()
    code = run_all(out, err, registry=registry, session_factory=lambda: session)
    return code, out.getvalue(), session


def _passes(ctx: Context) -> None:
    pass


def _fails(ctx: Context) -> None:
    raise Failed("widened")


def _errors(ctx: Context) -> None:
    raise KeyError("boom")


def test_an_empty_registry_exits_1() -> None:
    code, out, _ = _run([])
    assert code == 1
    assert out.splitlines()[-1] == "summary: 0 scenarios, 0 passed, 0 failed: FAIL"


def test_every_scenario_passing_exits_0_with_one_line_each() -> None:
    code, out, session = _run([Scenario("a", _passes), Scenario("b", _passes)])
    assert code == 0 and session.closed
    lines = out.splitlines()
    assert [line.split()[:2] for line in lines[:2]] == [["PASS", "a"], ["PASS", "b"]]
    assert lines[2] == "summary: 2 scenarios, 2 passed, 0 failed: PASS"


@pytest.mark.parametrize("bad", [_fails, _errors])
def test_one_failing_scenario_fails_the_run(bad: Any) -> None:
    code, out, session = _run([Scenario("a", _passes), Scenario("b", bad), Scenario("c", _passes)])
    assert code == 1 and session.closed
    lines = out.splitlines()
    assert lines[1].startswith("FAIL b: ")
    assert lines[2].startswith("PASS c")
    assert lines[3] == "summary: 3 scenarios, 2 passed, 1 failed: FAIL"


def test_a_setup_that_fails_fails_every_scenario() -> None:
    code, out, session = _run([Scenario("a", _passes)], fail_start=True)
    assert code == 1 and session.closed
    assert out.splitlines() == [
        "FAIL a: not run: the setup failed",
        "summary: 1 scenarios, 0 passed, 1 failed: FAIL",
    ]


def test_scenario_names_are_unique() -> None:
    saved = list(_scenarios.REGISTRY)
    try:
        _scenarios.scenario("duplicate-name")(_passes)
        with pytest.raises(ValueError, match="registered already"):
            _scenarios.scenario("duplicate-name")(_passes)
    finally:
        _scenarios.REGISTRY[:] = saved


def test_the_suite_starts_only_programs_installed_beside_its_interpreter() -> None:
    beside = os.path.dirname(sys.executable)
    assert str(_spawn.SWITCHBOARD_EXE) == os.path.join(beside, "switchboard")
    assert str(_spawn.LUCILLE_HOST_EXE) == os.path.join(beside, "lucille-host")
    assert _spawn.SWITCHBOARD_EXE.is_file(), "install the conformance extra"
    assert _spawn.LUCILLE_HOST_EXE.is_file()


def _suite() -> subprocess.CompletedProcess[str]:
    env = {name: value for name, value in os.environ.items() if not name.startswith("SWITCHBOARD_")}
    return subprocess.run(
        [sys.executable, "-m", "lucille_host.conformance"],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )


@pytest.mark.skipif(
    ROOT, reason="the host refuses to run as root; CI runs this as an ordinary user"
)
def test_the_suite_passes_against_the_installed_host() -> None:
    result = _suite()
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert any(line.startswith("PASS allowed-op-runs") for line in lines), result.stdout
    assert lines[-1].endswith(" failed: PASS") and " 0 failed" in lines[-1]


@pytest.mark.skipif(not ROOT, reason="only as root does the host refuse to start")
def test_as_root_the_suite_fails() -> None:
    result = _suite()
    assert result.returncode == 1
    assert "refuses to run as root" in result.stderr
    assert result.stdout.splitlines()[-1].endswith(": FAIL")
