"""The ``lucille-host`` command prints its version, the version has one source, and each verb runs
only its own handler."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

from lucille_host import __version__, cli, paths
from lucille_host.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED = f"lucille-host {__version__}\n"


def test_main_with_no_arguments_prints_the_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert capsys.readouterr().out == EXPECTED


def test_version_flag_prints_the_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == EXPECTED


def test_console_script_prints_the_version() -> None:
    script = Path(sys.executable).parent / "lucille-host"
    assert script.is_file(), f"no lucille-host console script next to {sys.executable}"
    for args in ([], ["--version"]):
        result = subprocess.run([str(script), *args], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        assert result.stdout == EXPECTED


def test_python_dash_m_prints_the_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "lucille_host", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == EXPECTED


def test_unknown_argument_is_a_usage_error() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "lucille_host", "--no-such-flag"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2


def test_version_has_a_single_source() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
    assert "version" not in project
    assert "version" in project["dynamic"]
    assert metadata.version("lucille-host") == __version__


class _ParsedAs:
    """A parser that reads any command line as one verb, with no handler of its own."""

    def __init__(self, verb: str) -> None:
        self.verb = verb

    def parse_args(self, argv: object) -> argparse.Namespace:
        return argparse.Namespace(verb=self.verb, profile="default")


def test_dispatch_is_explicit(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Every verb the parser takes has a handler of its own.
    verbs = next(action for action in cli.build_parser()._actions if action.dest == "verb")
    assert set(verbs.choices) == set(cli.VERBS)
    # A verb with no handler is refused, and is never run as another verb: the controls'
    # fallback was once `kill`.
    state = paths.dirs().state
    for verb in ("unpair", "begin", "start", "kill-everything"):
        monkeypatch.setattr("lucille_host.cli.build_parser", lambda verb=verb: _ParsedAs(verb))
        assert main([verb]) == 1
        assert "not a verb this release has" in capsys.readouterr().err
        assert not (state / "paused").exists()
        assert not (state / "kill-request").exists()
    with pytest.raises(Exception, match="not a control"):
        cli._controls("forget", paths.dirs())
    assert not (state / "kill-request").exists()
