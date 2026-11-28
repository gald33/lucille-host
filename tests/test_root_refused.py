"""Running as root (decisions/0004-running-as-root.md, 4.2): every verb but ``--version`` and
``--help`` is refused at uid 0, before the profile's paths are resolved, so that a refused root
run creates nothing: no folder, no lock file, no run record.

CI is not root, so uid 0 is injected the way the other tests do it, by patching ``os.geteuid``.
Each case runs in a private home, and the tree under it is compared before and after.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

from lucille_host.cli import ROOT_REFUSED, VERBS, main
from tests._host import HostEnv
from tests.spec import _support

# One command line for every verb and action the parser knows, each as it would be typed.
LINES: dict[str, list[str]] = {
    "ops list": ["ops", "list"],
    "ops enable": ["ops", "enable", "files.sha256"],
    "ops enable unknown": ["ops", "enable", "nosuch.op"],
    "ops disable": ["ops", "disable", "files.sha256"],
    "roots list": ["roots", "list"],
    "roots add": ["roots", "add", "docs", "."],
    "roots add suggest": ["roots", "add", "--suggest-alias", "."],
    "roots remove": ["roots", "remove", "docs"],
    "roots check": ["roots", "check"],
    "pause": ["pause"],
    "resume": ["resume"],
    "kill": ["kill"],
    "status": ["status"],
    "log": ["log"],
    "forget": ["forget"],
    "uninstall-data": ["uninstall-data", "--yes"],
    "pair": ["pair"],
    "pair code": ["pair", "ABC-DEF"],
    "pair store-file": ["pair", "--store-file", "ABC-DEF"],
    "pair begin": ["pair", "begin"],
    "pair begin store-file": ["pair", "begin", "--store-file"],
    "pair begin hub": [
        *("pair", "begin", "--hub", "http://127.0.0.1:8765", f"--test-key={'A' * 43}")
    ],
    "pair resume": ["pair", "resume"],
    "pair cancel": ["pair", "cancel"],
    "pair status": ["pair", "status"],
    "pair confirm": ["pair", "confirm"],
    "pair reject": ["pair", "reject"],
    "policy previous": ["policy", "previous"],
    "policy reset": ["policy", "reset"],
    "profile check": ["profile", "check"],
    "credential list": ["credential", "list"],
    "credential set": [
        *("credential", "set", "github", "work", "--host", "api.github.com", "--value-stdin")
    ],
    "credential remove": ["credential", "remove", "github", "work"],
    "credential test": ["credential", "test", "github", "work"],
    "verify": ["verify"],
}
RUNS: dict[str, list[str]] = {
    "run": ["run"],
    "run stdin-eof": ["run", "--exit-on-stdin-eof"],
}


def snapshot(top: Path) -> dict[str, tuple[object, ...]]:
    """Every file and folder under ``top``: its kind, mode and size, and the time it changed."""
    found: dict[str, tuple[object, ...]] = {}
    for where, folders, files in os.walk(top):
        for name in [*folders, *files]:
            path = Path(where, name)
            info = path.lstat()
            found[str(path.relative_to(top))] = (
                oct(info.st_mode),
                info.st_size,
                info.st_mtime_ns,
            )
    return found


@pytest.fixture
def as_root(host_env: HostEnv, monkeypatch: pytest.MonkeyPatch) -> HostEnv:
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    return host_env


def _json(line: list[str]) -> list[str]:
    return ["--json", *line] if line[0] != "run" else [*line, "--json-events"]


def test_the_lines_cover_every_verb() -> None:
    assert {line[0] for line in [*LINES.values(), *RUNS.values()]} == set(VERBS)


@pytest.mark.parametrize("line", LINES.values(), ids=LINES.keys())
def test_a_verb_is_refused_as_root_and_touches_nothing(
    as_root: HostEnv, line: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    before = snapshot(as_root.home.parent)
    assert main(line) == 1
    said = capsys.readouterr()
    assert said.out == ""
    assert said.err == f"lucille-host: {ROOT_REFUSED}\n"
    assert snapshot(as_root.home.parent) == before
    assert not (as_root.dirs.config / ".policy.lock").exists()
    if sys.platform.startswith("linux"):
        # The folders a verb would make above the profile, which the default layout names.
        assert not (as_root.home / ".config").exists() and not (as_root.home / ".local").exists()


@pytest.mark.parametrize("line", LINES.values(), ids=LINES.keys())
def test_a_verb_is_refused_as_root_with_json(
    as_root: HostEnv, line: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    before = snapshot(as_root.home.parent)
    assert main(_json(line)) == 1
    said = capsys.readouterr()
    assert _support.cli_objects(said.out) == [
        {"v": 1, "type": "error", "code": "startup_refused", "message": ROOT_REFUSED}
    ]
    assert said.err == f"lucille-host: {ROOT_REFUSED}\n"
    assert snapshot(as_root.home.parent) == before


@pytest.mark.parametrize("line", RUNS.values(), ids=RUNS.keys())
def test_run_is_refused_as_root_with_exit_24_and_touches_nothing(
    as_root: HostEnv, line: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    before = snapshot(as_root.home.parent)
    assert main(line) == 24
    said = capsys.readouterr()
    assert said.out == ""
    assert said.err == f"lucille-host: {ROOT_REFUSED}\n"
    assert snapshot(as_root.home.parent) == before


@pytest.mark.parametrize("line", RUNS.values(), ids=RUNS.keys())
def test_run_json_events_is_refused_as_root_with_a_stopped_object(
    as_root: HostEnv, line: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    before = snapshot(as_root.home.parent)
    assert main([*line, "--json-events"]) == 24
    said = capsys.readouterr()
    assert _support.cli_objects(said.out) == [
        {"v": 1, "type": "stopped", "reason": "startup_refused"}
    ]
    assert said.err == f"lucille-host: {ROOT_REFUSED}\n"
    assert snapshot(as_root.home.parent) == before


def test_a_refused_root_run_leaves_a_profile_that_exists_as_it_was(
    as_root: HostEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    assert main(["ops", "enable", "files.sha256"]) == 0
    assert main(["pause"]) == 0
    monkeypatch.setattr("os.geteuid", lambda: 0)
    before = snapshot(as_root.home.parent)
    assert before
    for line in [*LINES.values(), *RUNS.values()]:
        main(line)
        assert snapshot(as_root.home.parent) == before


def test_the_check_comes_before_the_profile_is_looked_at(
    as_root: HostEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    # A profile name the host would refuse (`bad_profile`) is not looked at as root.
    assert main(["--profile", "../escape", "pause"]) == 1
    assert capsys.readouterr().err == f"lucille-host: {ROOT_REFUSED}\n"


@pytest.mark.parametrize("flag", ["--version", "--help"])
def test_version_and_help_work_as_root(
    as_root: HostEnv, flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    before = snapshot(as_root.home.parent)
    with pytest.raises(SystemExit) as ended:
        main([flag])
    assert ended.value.code == 0
    said = capsys.readouterr()
    assert said.out and ROOT_REFUSED not in said.out + said.err
    assert snapshot(as_root.home.parent) == before


def test_no_verb_shows_the_version_as_root(
    as_root: HostEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([]) == 0
    assert capsys.readouterr().out.startswith("lucille-host ")


def test_a_user_sees_no_change(host_env: HostEnv, capsys: pytest.CaptureFixture[str]) -> None:
    assert os.geteuid() != 0
    assert main(["pause"]) == 0
    assert main(["resume"]) == 0
    assert main(["roots", "list"]) == 0
    assert main(["ops", "enable", "nosuch.op"]) == 1
    said = capsys.readouterr()
    assert ROOT_REFUSED not in said.out + said.err
    assert "not in this release's catalog" in said.err
    assert (host_env.dirs.config / ".policy.lock").exists()
    assert main(["--json", "status"]) == 0
    assert "startup_refused" not in capsys.readouterr().out


def test_run_as_root_starts_no_lifeline(as_root: HostEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    def started(*_: object, **__: object) -> None:
        raise AssertionError("a lifeline was made for a run that is refused")

    monkeypatch.setattr("lucille_host.loop.Lifeline", started)
    assert main(["run", "--exit-on-stdin-eof"]) == 24


def test_the_message_names_the_repair() -> None:
    assert "does not run as root (uid 0)" in ROOT_REFUSED
    assert "`lucille-host profile check`" in ROOT_REFUSED
