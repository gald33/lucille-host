"""What this skeleton release promises: no operations, no processes, no conformance pass."""

from __future__ import annotations

import subprocess
import sys

import pytest

from lucille_host import _exec, _switchboard
from lucille_host.ops import OPERATIONS


def test_the_operation_set_is_empty_and_read_only() -> None:
    assert dict(OPERATIONS) == {}
    with pytest.raises(TypeError):
        OPERATIONS["host.describe"] = object()  # type: ignore[index]


def test_exec_starts_nothing() -> None:
    with pytest.raises(NotImplementedError):
        _exec.run(["/bin/true"])


def test_switchboard_surface_names_exist_in_the_pinned_library() -> None:
    assert _switchboard.LUCILLE_HUB == "https://switchboard.lucille-ai.com"
    for function in (*_switchboard.PAIRING_CALLS, *_switchboard.SHARING_CALLS):
        assert callable(function)
    assert isinstance(_switchboard.CLIENT_CLASS, type)


def test_conformance_suite_without_scenarios_fails() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "lucille_host.conformance"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "no conformance scenarios" in result.stderr
