"""The run record (``runs.py``): one line per command, written before its work, in the profile's
state folder; never an argument, never a path outside an app; capped; never failing the command."""

from __future__ import annotations

import io
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from lucille_host import cli, paths, runs
from tests._host import HostEnv, lock_held

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
        # After `--` every word is plain, as the parser reads it.
        (["--", "status"], ("default", "status")),
        (["--", "s3cret-value"], None),
        # The parser takes an abbreviation of a long option: so does the record.
        (["--prof=mac-app", "status"], ("mac-app", "status")),
        (["--prof", "mac-app", "status"], ("mac-app", "status")),
        (["--vers", "s3cret-value"], ("default", "version")),
        (["--he"], None),
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


@pytest.fixture
def alarm() -> Iterator[None]:
    """Ends a test that blocks, rather than hanging the run, after 10 s."""

    def ring(signum: int, frame: object) -> None:
        raise TimeoutError("blocked for 10 s")

    before = signal.signal(signal.SIGALRM, ring)
    signal.alarm(10)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, before)


def test_a_fifo_in_the_records_place_neither_blocks_nor_is_written(
    host_env: HostEnv, alarm: None
) -> None:
    state = paths.ensure_dir(paths.dirs().state)
    fifo = state / runs.FILE
    os.mkfifo(fifo)
    # With no reader, opening a FIFO to write would block: the record never waits for one.
    started = time.monotonic()
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert time.monotonic() - started < 1
    # With a reader, it opens at once, and is left alone: it is no regular file.
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        started = time.monotonic()
        runs.record(["status"], VERBS)
        assert time.monotonic() - started < 1
        assert os.read(reader, 100) == b""  # the end, with no writer: nothing was written to it
    finally:
        os.close(reader)
    assert fifo.is_fifo()
    # Opened to read and write, a FIFO does not wait for a reader on Linux or macOS; O_NONBLOCK
    # makes sure where that is not so.
    assert runs._FLAGS & os.O_NONBLOCK and runs._FLAGS & os.O_NOFOLLOW


def test_a_file_linked_elsewhere_is_not_written(host_env: HostEnv, tmp_path: Path) -> None:
    state = paths.ensure_dir(paths.dirs().state)
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("untouched\n", encoding="utf-8")
    os.link(elsewhere, state / runs.FILE)
    runs.record(["status"], VERBS)
    assert elsewhere.read_text(encoding="utf-8") == "untouched\n"


def test_anything_but_a_regular_file_is_not_written(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A device at the record's place (made here by reporting the file opened as one, since a test
    # cannot make a device) is left alone, whatever writing to it would do.
    state = paths.ensure_dir(paths.dirs().state)
    real = os.fstat

    def as_a_device(descriptor: int) -> os.stat_result:
        found = real(descriptor)
        return os.stat_result((stat.S_IFCHR | 0o600, *tuple(found)[1:]))

    monkeypatch.setattr("os.fstat", as_a_device)
    runs.record(["status"], VERBS)
    monkeypatch.undo()
    assert (state / runs.FILE).read_bytes() == b""


def test_the_record_is_cut_back_by_bytes_once_too_large(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runs, "MAX_BYTES", 2000)
    monkeypatch.setattr(runs, "KEEP_BYTES", 1000)
    path = paths.dirs().state / runs.FILE
    sizes = []
    for n in range(100):
        runs.record(["status"], VERBS, clock=lambda n=n: float(n))
        sizes.append(path.stat().st_size)
    lines = _lines()
    # Never larger than MAX_BYTES and one line; cut to KEEP_BYTES at most, whole lines only.
    longest = max(len(line) for line in path.read_bytes().splitlines(keepends=True))
    assert max(sizes) <= 2000 and min(sizes[20:]) <= 1000 + longest
    assert any(after < before for before, after in zip(sizes, sizes[1:], strict=False))
    # The newest are the ones kept, in order, each whole, with none missing after the first.
    starts = [line["started"] for line in lines]
    assert starts == [float(n) for n in range(int(starts[0]), 100)]
    assert path.stat().st_mode & 0o777 == 0o600
    # The cut leaves nothing beside the record.
    assert sorted(entry.name for entry in path.parent.iterdir()) == [runs.FILE]


@pytest.mark.parametrize(
    "planted",
    [
        pytest.param(b"x" * 5_000_000 + b"\n", id="one 5 MB line"),
        pytest.param((b"y" * 1_000_000 + b"\n") * 20, id="20 lines of 1 MB"),
        pytest.param(b"z" * 3_000_000, id="3 MB with no newline"),
    ],
)
def test_a_planted_large_record_costs_one_cut_of_its_tail(
    host_env: HostEnv, planted: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[int] = []
    pread = os.pread

    def counted(descriptor: int, length: int, offset: int) -> bytes:
        if length > 1:  # not the one byte the append reads to see that the record ends a line
            asked.append(length)
        return pread(descriptor, length, offset)

    monkeypatch.setattr("os.pread", counted)
    state = paths.ensure_dir(paths.dirs().state)
    path = state / runs.FILE
    path.write_bytes(planted)
    path.chmod(0o600)
    started = time.monotonic()
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    first = time.monotonic() - started
    # Only the tail is read: the cut asks for KEEP_BYTES at most, wherever the record ends.
    assert max(asked) <= runs.KEEP_BYTES and len(asked) >= 1, asked
    assert path.stat().st_size <= runs.KEEP_BYTES
    assert [line["command"] for line in _lines()][-1:] == ["version"]
    started = time.monotonic()
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert max(first, time.monotonic() - started) < 0.5
    assert [line["command"] for line in _lines()][-2:] == ["version", "version"]


def _app_path(length: int) -> str:
    """A path of ``length`` characters to an app's runtime, in folders that are not there, each
    name short enough for any system: the path itself, not a folder, is what is long."""
    prefix, suffix = "/no-such-folder/", ".app" + runs.RUNTIME_IN_APP
    filler = length - len(prefix) - len(suffix)
    names = ["a" * 99] * (filler // 100) + ["a" * (filler % 100)]
    path = prefix + "/".join(names).strip("/") + suffix
    assert len(path) == length
    return path


def test_a_line_is_never_longer_than_its_cap(host_env: HostEnv) -> None:
    # Only an app at a very long path makes a line this long: it is kept without the path. The
    # line's other fields are this process's, so its length for a given path is known here.
    fields = {"v": 1, "command": "status", "pid": os.getpid(), "ppid": os.getppid()}
    base = len(runs._line({**fields, "executable": "", "started": 1.5}))
    longest = runs.MAX_LINE - base
    runs.record(["status"], VERBS, clock=lambda: 1.5, executable=_app_path(longest))
    runs.record(["status"], VERBS, clock=lambda: 1.5, executable=_app_path(longest + 1))
    runs.record(["status"], VERBS, clock=lambda: 1.5, executable=_app_path(5000))
    lines = (paths.dirs().state / runs.FILE).read_bytes().splitlines(keepends=True)
    assert [len(line) for line in lines][:1] == [runs.MAX_LINE]
    assert [json.loads(line)["executable"] for line in lines] == [_app_path(longest), None, None]
    assert all(json.loads(line)["command"] == "status" for line in lines)


def test_a_path_that_cannot_be_resolved_is_left_out_and_the_line_kept(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    def too_long(path: str) -> str:
        raise OSError(63, "File name too long")

    monkeypatch.setattr("os.path.realpath", too_long)
    runs.record(["status"], VERBS, executable=RUNTIME)
    assert [(line["command"], line["executable"]) for line in _lines()] == [("status", None)]


def test_a_start_time_that_is_not_a_number_writes_no_line(host_env: HostEnv) -> None:
    runs.record(["status"], VERBS, clock=lambda: float("nan"))
    runs.record(["status"], VERBS, clock=lambda: float("inf"))
    assert _lines() == []


def test_a_lock_held_elsewhere_is_waited_for_briefly_then_the_line_is_skipped(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The wait is 0.2 s, as spec/CLI.md section 16 says, and no longer: a lock held for ever by
    # another process costs each command that and no more.
    assert runs.LOCK_WAIT == 0.2
    state = paths.ensure_dir(paths.dirs().state)
    (state / runs.FILE).touch(mode=0o600)
    with lock_held(state / runs.FILE):
        started = time.monotonic()
        assert cli.main(["pause"]) == 0
        waited = time.monotonic() - started
    assert 0.2 <= waited < 1.5, waited
    assert "paused" in capsys.readouterr().out
    assert _lines() == []


_WRITER = """
import json, os, sys
from pathlib import Path
from lucille_host import runs
runs.MAX_BYTES, runs.KEEP_BYTES, runs.LOCK_WAIT = 400, 200, 30.0
path, archive = Path(sys.argv[1]), Path(sys.argv[2])
worker, count = int(sys.argv[3]), int(sys.argv[4])
cut = runs._cut


def archived(path, descriptor):
    # Each record a cut replaces is kept whole in the archive first, read through the cut's own
    # descriptor (closing another would let go of the lock), so that every line ever written is
    # either there or in the record.
    with open(archive, "ab") as kept:
        kept.write(os.pread(descriptor, os.fstat(descriptor).st_size, 0))
    cut(path, descriptor)


runs._cut = archived
print("ready", flush=True)
sys.stdin.readline()  # all start together
for index in range(count):
    runs._append(path, (json.dumps({"w": worker, "i": index}) + "\\n").encode())
"""


def test_concurrent_commands_lose_no_line_after_a_cut_and_a_reader_never_finds_it_empty(
    tmp_path: Path,
) -> None:
    path = tmp_path / runs.FILE
    path.write_bytes(b'{"w": -1, "i": 0}\n')
    path.chmod(0o600)
    archive = tmp_path / "archive"
    empty: list[int] = []
    reads: list[int] = []
    done = threading.Event()

    def read() -> None:
        while not done.is_set():
            data = path.read_bytes()
            reads.append(1)
            if not data or not data.endswith(b"\n"):
                empty.append(len(data))

    reader = threading.Thread(target=read)
    reader.start()
    workers, count = 8, 1500
    try:
        writers = [
            subprocess.Popen(
                [sys.executable, "-c", _WRITER, str(path), str(archive), str(w), str(count)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            for w in range(workers)
        ]
        for writer in writers:
            assert writer.stdout is not None and writer.stdout.readline() == "ready\n"
        for writer in writers:
            assert writer.stdin is not None
            writer.stdin.write("go\n")
            writer.stdin.flush()  # communicate closes it (on 3.11 it fails on one closed already)
        for writer in writers:
            writer.communicate(timeout=120)
            assert writer.returncode == 0
    finally:
        done.set()
        reader.join()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    # Every line written is in the record or in a record a cut replaced: none was lost.
    seen = {
        (line["w"], line["i"])
        for line in map(json.loads, (archive.read_bytes() + path.read_bytes()).splitlines())
    }
    assert {(w, i) for w in range(workers) for i in range(count)} <= seen
    assert len(path.read_bytes()) <= 400 + 100
    # The record was cut many times; what it keeps is, for each writer, every line it wrote from
    # its oldest kept one to its last.
    for w in range(workers):
        kept = [line["i"] for line in lines if line["w"] == w]
        assert kept == list(range(count - len(kept), count)), w
    assert empty == [] and len(reads) > 10
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["archive", runs.FILE]


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
