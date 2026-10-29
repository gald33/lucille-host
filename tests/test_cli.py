"""The ``lucille-host`` command prints its version, and the version has one source."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

from lucille_host import __version__
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
