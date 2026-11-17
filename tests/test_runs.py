"""The run record (``runs.py``): one line per command, written before its work, in the profile's
state folder; never an argument, never a path outside an app; capped; never failing the command."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from lucille_host import cli, paths, runs
from tests._host import HostEnv

VERBS = tuple(cli.VERBS)
RUNTIME = "/Applications/Capsule.app" + runs.RUNTIME_IN_APP


def _lines(profile: str = paths.DEFAULT_PROFILE) -> list[dict[str, object]]:
    path = paths.dirs(profile).state / runs.FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize(
    ("words", "named"),
    [
        (["status"], ("default", "status")),
        (["--json", "status"], ("default", "status")),
        (["--profile", "mac-app", "--json", "status"], ("mac-app", "status")),
        (["--profile=mac-app", "run", "--json-events"], ("mac-app", "run")),
        (["roots", "add", "--", "docs", "/home/me/docs"], ("default", "roots")),
        (["credential", "set", "gmail", "x", "--host", "a.example.com"], ("default", "credential")),
        (["--json", "--version"], ("default", "version")),
        ([], ("default", "version")),
        # A word that is not a verb is never recorded: it may be a value typed by mistake.
        (["s3cret-value"], None),
        (["--version", "s3cret-value"], ("default", "version")),
        (["--help"], None),
        (["credential", "-h"], ("default", "credential")),
        # The profile's value is not taken for the verb.
        (["--profile", "status"], ("status", "version")),
        (["--", "status"], ("default", "version")),
        # Only the first plain word can be the verb.
        (["s3cret-value", "status"], None),
    ],
)
def test_the_command_a_line_names(words: list[str], named: tuple[str, str] | None) -> None:
    assert runs.command_of(words, VERBS) == named


def test_a_record_line_holds_the_command_its_process_and_when_and_nothing_else(
    host_env: HostEnv,
) -> None:
    runs.record(["--json", "status"], VERBS, clock=lambda: 1234.5, executable=RUNTIME)
    [line] = _lines()
    assert line == {
        "v": 1,
        "command": "status",
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "executable": RUNTIME,
        "started": 1234.5,
    }
    path = paths.dirs().state / runs.FILE
    assert path.stat().st_mode & 0o777 == 0o600


def test_an_executable_outside_an_app_is_not_kept(host_env: HostEnv) -> None:
    # A path outside an app can name the user (a home folder's virtual environment): not kept.
    runs.record(["status"], VERBS, executable="/home/someone/.venv/bin/python3")
    assert [line["executable"] for line in _lines()] == [None]


def test_the_executable_is_kept_by_its_real_path(host_env: HostEnv, tmp_path: Path) -> None:
    app = tmp_path / "Capsule.app"
    runtime = Path(str(app) + runs.RUNTIME_IN_APP)
    runtime.parent.mkdir(parents=True)
    runtime.write_text("", encoding="utf-8")
    link = tmp_path / "link-to-runtime"
    link.symlink_to(runtime)
    runs.record(["status"], VERBS, executable=str(link))
    assert [line["executable"] for line in _lines()] == [os.path.realpath(runtime)]


def test_no_record_for_a_line_that_names_no_command_or_a_bad_profile(host_env: HostEnv) -> None:
    runs.record(["s3cret-value"], VERBS)
    runs.record(["--help"], VERBS)
    runs.record(["--profile", "Not A Profile", "status"], VERBS)
    assert not (paths.dirs().state / runs.FILE).exists()


def test_no_record_as_root(host_env: HostEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.geteuid", lambda: 0)
    runs.record(["status"], VERBS)
    assert not (paths.dirs().state / runs.FILE).exists()


def test_a_record_that_cannot_be_written_is_skipped_silently(
    host_env: HostEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    state = paths.dirs().state
    paths.ensure_dir(state)
    (state / runs.FILE).mkdir()  # a folder where the file goes: the open fails
    runs.record(["status"], VERBS)
    said = capsys.readouterr()
    assert said.out == "" and said.err == ""


def test_a_failing_write_never_fails_the_command(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(*args: object) -> int:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runs, "_append", broken)
    assert cli.main(["pause"]) == 0
    assert "paused" in capsys.readouterr().out


def test_a_symbolic_link_in_the_records_place_is_not_followed(
    host_env: HostEnv, tmp_path: Path
) -> None:
    state = paths.ensure_dir(paths.dirs().state)
    target = tmp_path / "elsewhere.txt"
    target.write_text("untouched\n", encoding="utf-8")
    (state / runs.FILE).symlink_to(target)
    runs.record(["status"], VERBS)
    assert target.read_text(encoding="utf-8") == "untouched\n"


def test_the_record_is_cut_back_to_its_last_lines_once_too_large(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runs, "MAX_BYTES", 2000)
    monkeypatch.setattr(runs, "KEEP", 5)
    for n in range(40):
        runs.record(["status"], VERBS, clock=lambda n=n: float(n))
    lines = _lines()
    path = paths.dirs().state / runs.FILE
    # Cut back to 5 lines each time it passed 2000 bytes: never more than that and one line, and
    # far fewer lines than were written.
    longest = max(len(line) for line in path.read_text(encoding="utf-8").splitlines()) + 1
    assert path.stat().st_size <= 2000 + longest
    assert len(lines) < 40
    # The newest are the ones kept, in order, each whole.
    assert [line["started"] for line in lines][-1] == 39.0
    starts = [line["started"] for line in lines]
    assert starts == sorted(starts)


def test_a_cut_while_another_holds_the_lock_waits_for_none(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The lock is only tried for: held elsewhere, the line is still written, and the cut left to
    # a later command.
    monkeypatch.setattr(runs, "MAX_BYTES", 10)

    def held(descriptor: int, command: int, length: int) -> None:
        raise BlockingIOError(11, "Resource temporarily unavailable")

    monkeypatch.setattr("os.lockf", held)
    runs.record(["status"], VERBS)
    runs.record(["pause"], VERBS)
    assert [line["command"] for line in _lines()] == ["status", "pause"]


def test_every_command_records_itself_before_its_work(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # status, a verb refused for a bad option, the version, and a credential line: each is
    # recorded by its command alone, before it runs, whatever it did then.
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["--json", "status"]) == 0
    assert cli.main(["log", "--limit", "0"]) == 1
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert cli.main(["credential", "remove", "gmail", "x"]) == 1
    with pytest.raises(SystemExit):
        cli.main(["credential", "set", "gmail", "x", "s3cret-value", "--host", "a.example.com"])
    capsys.readouterr()
    lines = _lines()
    assert [line["command"] for line in lines] == [
        "status",
        "log",
        "version",
        "credential",
        "credential",
    ]
    text = (paths.dirs().state / runs.FILE).read_text(encoding="utf-8")
    assert "s3cret-value" not in text and "gmail" not in text and "a.example.com" not in text
    assert all(line["pid"] == os.getpid() for line in lines)
