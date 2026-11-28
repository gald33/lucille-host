"""The conformance suite (``python -m lucille_host.conformance``): it passes against the host as
installed here, it cannot pass by running nothing, and a scenario whose expectation does not hold
makes it fail.

The end-to-end tests start a real hub (``switchboard serve``, from the ``conformance`` extra) and
the installed ``lucille-host``, as the suite does from a release. They are skipped when the suite
runs as root, where the host refuses to start, and when the hub's server is not installed; CI runs
them as an ordinary user, with the extra.
"""

from __future__ import annotations

import errno
import io
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import lucille_host.conformance as conformance
from lucille_host.conformance import _procfs, _restore, _scenarios, _spawn
from lucille_host.conformance._scenarios import Failed, Scenario, Skipped

AS_ROOT = os.geteuid() == 0
ROOT_REASON = "the host refuses to run as root; CI runs this test as an ordinary user"


def _needs_hub() -> None:
    pytest.importorskip("uvicorn", reason="switchboard serve needs the conformance extra")
    pytest.importorskip("fastapi", reason="switchboard serve needs the conformance extra")
    assert not _spawn.programs_missing(), _spawn.programs_missing()


# --- what decides the exit code -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("passed", "failed", "code"),
    [(0, 0, 1), (1, 0, 0), (21, 0, 0), (0, 1, 1), (20, 1, 1)],
)
def test_verdict(passed: int, failed: int, code: int) -> None:
    assert conformance.verdict(passed, failed) == code


def test_no_scenarios_is_not_a_pass(capsys: pytest.CaptureFixture[str]) -> None:
    assert conformance.main(scenarios=[]) == 1
    assert "no scenarios" in capsys.readouterr().out


def test_as_root_it_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("os.geteuid", lambda: 0)
    assert conformance.main() == 1
    assert "refuses to run as root" in capsys.readouterr().out


def test_without_the_programs_it_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_spawn, "LUCILLE_HOST_EXE", tmp_path / "lucille-host")
    assert conformance.main() == 1
    assert "not installed beside this interpreter" in capsys.readouterr().out


def test_each_scenario_gets_a_line_and_any_error_fails() -> None:
    def passes(ctx: object) -> None:
        pass

    def fails(ctx: object) -> None:
        raise Failed("expected refused not_enabled, got done\nmore detail")

    def breaks(ctx: object) -> None:
        raise KeyError("stdout")

    def cannot(ctx: object) -> None:
        raise Skipped("needs Linux's /proc")

    out = io.StringIO()
    chosen = [
        Scenario("passes", passes),
        Scenario("fails", fails),
        Scenario("breaks", breaks),
        Scenario("cannot", cannot),
    ]
    assert conformance.run_scenarios(chosen, object(), out) == (1, 2, 1)
    assert out.getvalue().splitlines() == [
        "PASS passes",
        "FAIL fails: expected refused not_enabled, got done [details on stderr]",
        "FAIL breaks: KeyError: 'stdout'",
        "SKIP cannot: needs Linux's /proc",
    ]


def test_skipped_alone_is_not_a_pass() -> None:
    def cannot(ctx: object) -> None:
        raise Skipped("needs Linux's /proc")

    out = io.StringIO()
    passed, failed, skipped = conformance.run_scenarios([Scenario("cannot", cannot)], object(), out)
    assert (passed, failed, skipped) == (0, 0, 1)
    assert conformance.verdict(passed, failed) == 1


def test_scenarios_are_registered_once_each_with_a_description() -> None:
    names = [found.name for found in _scenarios.REGISTRY]
    assert len(names) == len(set(names)) >= 20
    assert names[0] == "control-enabled-op-runs"
    assert names[-1] == "policy-unchanged"
    # The modules beside _scenarios.py register theirs when the package is imported, so this
    # test sees them however it is run.
    assert {"child-env-built-and-no-key", "upgrade-disables-changed-op"} <= set(names)
    assert all(found.doc for found in _scenarios.REGISTRY)
    with pytest.raises(ValueError, match="two scenarios"):
        _scenarios.scenario(lambda ctx: None, name="unknown-op")


def test_programs_are_the_ones_beside_this_interpreter() -> None:
    beside = Path(sys.executable).parent
    assert _spawn.LUCILLE_HOST_EXE == beside / "lucille-host"
    assert _spawn.SWITCHBOARD_EXE == beside / "switchboard"


def test_measure_is_the_protocols() -> None:
    assert _scenarios.measure("") == 2
    assert _scenarios.measure("é") == len('"\\u00e9"')
    assert _scenarios.measure({"a": [1, 2]}) == len('{"a":[1,2]}')


# --- against the installed host -----------------------------------------------------------------


@pytest.mark.skipif(AS_ROOT, reason=ROOT_REASON)
def test_suite_passes_against_the_installed_host() -> None:
    _needs_hub()
    done = subprocess.run(
        [sys.executable, "-m", "lucille_host.conformance"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    shown = f"stdout:\n{done.stdout}\nstderr:\n{done.stderr}"
    assert done.returncode == 0, shown
    lines = [
        line for line in done.stdout.splitlines() if line.startswith(("PASS ", "FAIL ", "SKIP "))
    ]
    names = [found.name for found in _scenarios.REGISTRY]
    assert [_verdict(line) for line in lines] == expected_verdicts(names, needs_met()), shown


def needs_met() -> dict[str, bool]:
    """Each scenario that may skip, and whether what it needs is here: Linux's ``/proc`` for the
    child's environment, and ripgrep at the catalog's path, looked for on disk by the suite
    itself rather than asked of the host, for the scenarios that search."""
    search = _restore.ripgrep_here() is not None
    return {
        "child-env-built-and-no-key": _procfs.available(),
        "output-bounded": search,
        "replies-path-free": search,
        "output-decodable": search,
    }


def expected_verdicts(names: list[str], needs: dict[str, bool]) -> list[tuple[str, str]]:
    """What the suite must print for each scenario, in order: SKIP only for a scenario whose
    need is not met here, and PASS for every other, so that a scenario that skips where it could
    check, or one that may never skip, fails this test."""
    return [(name, "SKIP" if needs.get(name, True) is False else "PASS") for name in names]


def _verdict(line: str) -> tuple[str, str]:
    verdict, rest = line.split(" ", 1)
    return rest.split(":", 1)[0].strip(), verdict


def test_expected_verdicts_allow_a_skip_only_where_its_need_is_missing() -> None:
    names = ["control-enabled-op-runs", "child-env-built-and-no-key", "output-bounded"]
    met = {"child-env-built-and-no-key": True, "output-bounded": True}
    assert expected_verdicts(names, met) == [(name, "PASS") for name in names]
    missing = {"child-env-built-and-no-key": False, "output-bounded": True}
    assert expected_verdicts(names, missing) == [
        ("control-enabled-op-runs", "PASS"),
        ("child-env-built-and-no-key", "SKIP"),
        ("output-bounded", "PASS"),
    ]


def test_every_scenario_that_may_skip_is_registered() -> None:
    assert set(needs_met()) <= {found.name for found in _scenarios.REGISTRY}


def _refused_as_unknown_op(ctx: _scenarios.Context) -> None:
    # host.sleep is in the catalog and off: the host refuses it not_enabled.
    _scenarios.expect_refused(ctx, ctx.body("host.sleep", {"n": 1}), "unknown_op")


def _control_refused(ctx: _scenarios.Context) -> None:
    # The control's request runs, so a scenario that expects it refused must fail.
    body = ctx.body("files.sha256", {"root": _scenarios.ROOT, "path": "hello.txt"})
    _scenarios.expect_refused(ctx, body, "not_enabled")


@pytest.mark.skipif(AS_ROOT, reason=ROOT_REASON)
def test_an_expectation_that_does_not_hold_fails_the_suite(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _needs_hub()
    chosen = [
        Scenario("control-enabled-op-runs", _scenarios.REGISTRY[0].run),
        Scenario("wrong-code", _refused_as_unknown_op),
        Scenario("ran-not-refused", _control_refused),
    ]
    assert conformance.main(scenarios=chosen) == 1
    out = capsys.readouterr().out
    assert "PASS control-enabled-op-runs" in out
    assert "FAIL wrong-code: expected refused unknown_op, got " in out
    assert '"code": "not_enabled"' in out
    assert "FAIL ran-not-refused: expected refused not_enabled, got " in out
    assert '"status": "done"' in out
    assert "1 passed, 2 failed" in out


def test_the_output_scenarios_run_before_policy_unchanged() -> None:
    names = [found.name for found in _scenarios.REGISTRY]
    mine = ["output-bounded", "replies-path-free", "failures-codes-only", "output-decodable"]
    assert all(name in names for name in mine)
    assert all(names.index(name) < names.index("policy-unchanged") for name in mine)


def test_a_name_the_filesystem_refuses_is_not_planted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # APFS refuses a file name that is not UTF-8 with EILSEQ; the other files are still planted.
    from lucille_host.conformance import scenarios_output

    real_open = open

    def refusing(path: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(path, bytes) and path.endswith(scenarios_output.ODD_NAME):
            raise OSError(errno.EILSEQ, "Illegal byte sequence", path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", refusing)
    files = {scenarios_output.ODD_NAME: b"x\n", scenarios_output.LATIN: b"y\n"}
    folder = tmp_path / "planted"
    with scenarios_output._planted_bytes(folder, files) as planted:
        assert planted == {scenarios_output.LATIN}
        assert (folder / "latin.txt").read_bytes() == b"y\n"
    assert not folder.exists()
