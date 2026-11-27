"""The pure parts of the child-environment and upgrade conformance scenarios: the /proc reader,
what counts as a built environment, and the policy edit that stands in for an upgrade."""

from __future__ import annotations

import os

import pytest

from lucille_host.conformance import _child_env, _procfs, _upgrade
from lucille_host.conformance._scenarios import Failed


def test_parse_stat_ppid_reads_past_a_name_with_parentheses() -> None:
    assert _procfs.parse_stat_ppid("42 (a) b (c)) S 17 42 42 0 -1") == 17


def test_parse_environ_keeps_an_item_without_equals() -> None:
    raw = b"PATH=/usr/bin:/bin\0LANG=C\0ODD\0X=a=b\0"
    assert _procfs.parse_environ(raw) == {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "ODD": "",
        "X": "a=b",
    }


@pytest.mark.skipif(not _procfs.available(), reason="needs Linux's /proc")
def test_read_sees_this_process() -> None:
    proc = _procfs.read(os.getpid())
    assert proc is not None
    assert proc.argv
    assert 0 in proc.fds


def test_read_of_a_gone_process_is_none() -> None:
    assert _procfs.read(2**22 + 12345) is None


# --- the child-environment scenario's judgement -------------------------------------------------


def _proc(env: dict[str, str], fds: tuple[int, ...] = (0, 1, 2)) -> _procfs.Process:
    return _procfs.Process(pid=1, argv=("/bin/sleep", "3"), env=env, fds=fds)


def test_check_child_accepts_exactly_the_built_environment() -> None:
    assert _child_env.check_child(_proc(dict(_child_env.EXPECTED_ENV))) == []


@pytest.mark.parametrize(
    "env",
    [
        {**_child_env.EXPECTED_ENV, "GITHUB_TOKEN": _child_env.SENTINEL},
        {**_child_env.EXPECTED_ENV, "HOME": "/home/me"},
        {"PATH": "/usr/bin:/bin", "LANG": "C"},
        {**_child_env.EXPECTED_ENV, "PATH": "/usr/local/bin:/usr/bin:/bin"},
    ],
)
def test_check_child_refuses_any_other_environment(env: dict[str, str]) -> None:
    assert _child_env.check_child(_proc(env))


def test_check_child_refuses_an_inherited_descriptor() -> None:
    assert _child_env.check_child(_proc(dict(_child_env.EXPECTED_ENV), (0, 1, 2, 5)))


# --- the upgrade scenario's edit ----------------------------------------------------------------

POLICY = (
    b'version = 1\nlog_results = false\n\n[ops."files.sha256"]\ndefinition = "'
    + b"a" * 64
    + b'"\n\n[ops."host.describe"]\ndefinition = "'
    + b"b" * 64
    + b'"\n'
)


def test_rewrite_definition_changes_only_files_sha256() -> None:
    changed = _upgrade.rewrite_definition(POLICY, _upgrade.OTHER_HASH)
    assert changed == POLICY.replace(b"a" * 64, _upgrade.OTHER_HASH.encode())
    assert b"b" * 64 in changed


def test_rewrite_definition_without_the_entry_fails() -> None:
    with pytest.raises(Failed):
        _upgrade.rewrite_definition(POLICY.replace(b"files.sha256", b"files.other"), "c" * 64)
