"""The executor, ``_exec.run``, and the path rules it is given (host-design "Execution rules")."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lucille_host import _exec, _output, spec
from lucille_host.containment import contain
from lucille_host.jobs import Context, Job, Ready, check, execute, process
from tests._host import MakeContext

PYTHON = sys.executable


def _python(code: str, *, timeout_s: float = 10, cwd: str = "/") -> _exec.Result:
    return _exec.run(_exec.Command(argv=(PYTHON, "-c", code), cwd=cwd, timeout_s=timeout_s))


def _alive(pid: int) -> bool:
    """Whether a process exists and is not a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if sys.platform.startswith("linux"):
        try:
            status = Path(f"/proc/{pid}/status").read_text()
        except FileNotFoundError:
            return False
        return "\nState:\tZ" not in status
    shown = subprocess.run(
        ["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return bool(shown.stdout.strip()) and not shown.stdout.strip().startswith("Z")


def test_exec_env_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUCILLE_HOST_CANARY", "must-not-reach-a-child")
    result = _exec.run(_exec.Command(argv=("/usr/bin/env",), cwd="/", timeout_s=10))
    assert result.ended == "exited" and result.exit_code == 0
    assert "LUCILLE_HOST_CANARY" not in result.stdout
    seen = dict(line.split("=", 1) for line in result.stdout.splitlines())
    # The design's allowlist, written out: a key added to CHILD_ENV must fail here.
    assert seen == dict(_exec.CHILD_ENV) == {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}


def test_exec_stdin_is_devnull_and_its_own_session() -> None:
    # The host's own stdin holds data while the child runs (pytest's capture would otherwise give
    # the host /dev/null, which a child inheriting it would read as empty too).
    saved = os.dup(0)
    reading, writing = os.pipe()
    os.write(writing, b"the host's stdin\n")
    os.close(writing)
    os.dup2(reading, 0)
    try:
        result = _python(
            "import os, sys; data = sys.stdin.read(); print(len(data), os.getsid(0), os.getpgid(0))"
        )
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(reading)
    length, sid, pgid = (int(x) for x in result.stdout.split())
    assert length == 0
    assert sid == pgid != os.getsid(0)


def test_exec_refuses_a_relative_executable() -> None:
    for argv in (("sleep", "1"), ("./sleep",), ()):
        with pytest.raises(ValueError):
            _exec.run(_exec.Command(argv=argv, cwd="/", timeout_s=5))
    with pytest.raises(ValueError):
        _exec.run(_exec.Command(argv=("/bin/sleep", "1"), cwd="relative", timeout_s=5))


def test_exec_symlink_escape(tmp_path: Path, make_context: MakeContext) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "inner").mkdir(parents=True)
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (root / "inner" / "ok.txt").write_text("ok")
    (root / "out").symlink_to(outside, target_is_directory=True)
    (root / "secret-link.txt").symlink_to(outside / "secret.txt")
    (root / "ok-link.txt").symlink_to(root / "inner" / "ok.txt")
    root_real = os.path.realpath(root)

    for escape in ("out/secret.txt", "secret-link.txt", "../outside/secret.txt", "/etc/hosts"):
        assert contain(str(root), escape) is None, escape
    for inside in ("inner/ok.txt", "ok-link.txt", "./inner/ok.txt", "missing.txt", "."):
        assert contain(str(root), inside) == root_real, inside
    # A root that is not an existing folder contains nothing.
    assert contain(str(tmp_path / "gone"), "a.txt") is None

    ctx = make_context(enabled=["files.sha256"], roots={"docs": root})
    job = Job(job_id="0" * 8, op="files.sha256", args={"root": "docs", "path": "out/secret.txt"})
    assert check(job, ctx) == "path_outside_root"
    reply = json.loads(process(job, ctx))
    assert (reply["status"], reply["code"]) == ("refused", "path_outside_root")
    assert "stdout" not in reply


def test_exec_dash_file(tmp_path: Path, make_context: MakeContext) -> None:
    root = tmp_path / "root"
    root.mkdir()
    content = b"not an option\n"
    (root / "--version").write_bytes(content)
    (root / "-").write_bytes(b"not standard input\n")
    ctx = make_context(enabled=["files.sha256"], roots={"docs": root})
    job = Job(job_id="1" * 8, op="files.sha256", args={"root": "docs", "path": "--version"})
    reply = json.loads(process(job, ctx))
    assert reply["status"] == "done" and reply["exit_code"] == 0, reply
    assert reply["stdout"].split() == [hashlib.sha256(content).hexdigest(), "--version"]
    # "-" alone means standard input to the program, even after "--": it is passed as "./-".
    job = Job(job_id="1" * 8, op="files.sha256", args={"root": "docs", "path": "-"})
    reply = json.loads(process(job, ctx))
    digest = hashlib.sha256(b"not standard input\n").hexdigest()
    assert reply["stdout"].split() == [digest, "./-"], reply


def test_files_sha256_runs_in_the_root(tmp_path: Path, make_context: MakeContext) -> None:
    root = tmp_path / "root"
    (root / "notes").mkdir(parents=True)
    content = "TODO: é\n".encode()
    (root / "notes" / "todo.txt").write_bytes(content)
    ctx = make_context(enabled=["files.sha256"], roots={"docs": root})
    job = Job(job_id="2" * 8, op="files.sha256", args={"root": "docs", "path": "notes/todo.txt"})
    ready = check(job, ctx)
    assert isinstance(ready, Ready) and ready.cwd == os.path.realpath(root)
    reply = json.loads(process(job, ctx))
    assert reply["stdout"].split() == [hashlib.sha256(content).hexdigest(), "notes/todo.txt"]
    # The root's real path never appears in a reply.
    assert str(root) not in json.dumps(reply) and os.path.realpath(root) not in json.dumps(reply)


def test_exec_caps() -> None:
    result = _python("import sys; sys.stdout.write('x' * 1048576)")
    assert result.ended == "exited"
    assert result.stdout_bytes == 1048576
    assert result.stdout_truncated
    assert _output.json_size(result.stdout) == _output.STREAM_CAP
    assert result.stdout == "x" * (_output.STREAM_CAP - 2)
    assert (result.stderr, result.stderr_truncated) == ("", False)


def _writing(ctx: Context, caps: dict[str, int]) -> Ready:
    """A ready job whose child writes 200000 bytes to stdout and 100 to stderr."""
    job = Job(job_id="3" * 8, op="host.sleep", args={"n": 1}, caps=caps)
    ready = check(job, ctx)
    assert isinstance(ready, Ready)
    code = "import sys; sys.stdout.write('x' * 200000); sys.stderr.write('y' * 100)"
    return dataclasses.replace(ready, argv=(PYTHON, "-c", code))


def test_exec_applies_a_lower_request_cap(make_context: MakeContext) -> None:
    ctx = make_context(enabled=["host.sleep"])
    outcome = execute(_writing(ctx, {"stdout_bytes": 50, "stderr_bytes": 10}), ctx)
    assert (outcome.stdout, outcome.stdout_truncated) == ("x" * 48, True)
    assert (outcome.stderr, outcome.stderr_truncated) == ("y" * 8, True)


def test_request_caps_never_raise_the_host_cap(make_context: MakeContext) -> None:
    ctx = dataclasses.replace(make_context(enabled=["host.sleep"]), stdout_cap=30, stderr_cap=1000)
    outcome = execute(_writing(ctx, {"stdout_bytes": 2147483647, "stderr_bytes": 2147483647}), ctx)
    assert (outcome.stdout, outcome.stdout_truncated) == ("x" * 28, True)
    assert (outcome.stderr, outcome.stderr_truncated) == ("y" * 100, False)


def test_exec_decodes_non_utf8_and_nul() -> None:
    for path in spec.fixture_paths("decode"):
        fixture = spec.load_fixture(path)
        raw = base64.b64decode(fixture["raw_base64"])
        code = f"import sys; sys.stdout.buffer.write({raw!r}); sys.stderr.buffer.write({raw!r})"
        result = _python(code)
        assert result.stdout == fixture["text"], path.stem
        assert result.stderr == fixture["text"], path.stem
        assert not result.stdout_truncated


def test_exec_raw_ceiling_drops_the_rest_and_flags_it() -> None:
    # Past RAW_CEILING bytes the host reads and drops output, so the tail here is never seen,
    # though the NUL before it would have been removed (rule raw-ceiling).
    size = _output.RAW_CEILING + 1024
    result = _python(f"import sys; sys.stdout.buffer.write(b'\\0' * {size} + b'tail')")
    assert result.ended == "exited" and result.exit_code == 0
    assert result.stdout_bytes == size + 4
    assert (result.stdout, result.stdout_truncated) == ("", True)


def test_exec_timeout_kills_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen(['/bin/sleep', '100'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(100)\n"
    )
    started = time.monotonic()
    result = _python(code, timeout_s=1)
    assert time.monotonic() - started < 10
    assert result.ended == "timeout"
    assert result.exit_code == -9
    assert 1000 <= result.duration_ms < 10000
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild)


def test_exec_kills_what_the_child_leaves_in_its_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "orphan.pid"
    code = (
        "import subprocess\n"
        "child = subprocess.Popen(['/bin/sleep', '100'], stdout=subprocess.DEVNULL,\n"
        "                         stderr=subprocess.DEVNULL)\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
    )
    result = _python(code)
    assert result.ended == "exited" and result.exit_code == 0
    orphan = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while _alive(orphan) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(orphan)


def _wait_for_pid(pid_file: Path) -> int:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if pid_file.exists() and pid_file.read_text():
            return int(pid_file.read_text())
        time.sleep(0.02)
    raise AssertionError("the child never wrote its pid")


SPAWNS_A_GRANDCHILD = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen(['/bin/sleep', '100'])\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
    "time.sleep(100)\n"
)


def _gone(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _alive(pid)


def test_a_user_kill_takes_the_whole_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"

    def kill_once_started() -> bool:
        return pid_file.exists() and bool(pid_file.read_text())

    command = _exec.Command(
        argv=(PYTHON, "-c", SPAWNS_A_GRANDCHILD, str(pid_file)), cwd="/", timeout_s=30
    )
    result = _exec.run(command, kill_requested=kill_once_started)
    assert (result.ended, result.exit_code) == ("killed", -9)
    assert _gone(int(pid_file.read_text()))


@pytest.mark.parametrize("raised", [PermissionError(13, "denied"), KeyboardInterrupt()])
def test_no_child_outlives_an_exception_in_the_wait(tmp_path: Path, raised: BaseException) -> None:
    pid_file = tmp_path / "grandchild.pid"

    def fails_once_started() -> bool:
        if pid_file.exists() and pid_file.read_text():
            raise raised
        return False

    command = _exec.Command(
        argv=(PYTHON, "-c", SPAWNS_A_GRANDCHILD, str(pid_file)), cwd="/", timeout_s=30
    )
    with pytest.raises(type(raised)):
        _exec.run(command, kill_requested=fails_once_started)
    assert _gone(int(pid_file.read_text()))


def test_exec_records_the_executable_hash() -> None:
    result = _exec.run(_exec.Command(argv=("/bin/sleep", "0"), cwd="/", timeout_s=5))
    expected = hashlib.sha256(Path("/bin/sleep").read_bytes()).hexdigest()
    assert result.executable_sha256 == expected


def test_exec_reports_a_signal_the_host_did_not_send_as_exited() -> None:
    result = _python("import os, signal; os.kill(os.getpid(), signal.SIGTERM)")
    assert result.ended == "exited" and result.exit_code == -15


def test_a_start_that_fails_raises_not_started(tmp_path: Path) -> None:
    with pytest.raises(_exec.NotStarted):
        _exec.run(_exec.Command(argv=("/bin/sleep", "1"), cwd=str(tmp_path / "gone"), timeout_s=5))
    with pytest.raises(_exec.NotStarted):
        _exec.run(_exec.Command(argv=(str(tmp_path / "no-such-program"),), cwd="/", timeout_s=5))


def test_an_error_after_the_start_is_not_reported_as_not_started(
    tmp_path: Path, make_context: MakeContext
) -> None:
    ctx = make_context(enabled=["host.sleep"])
    job = Job(job_id="7" * 8, op="host.sleep", args={"n": 5})
    ready = check(job, ctx)
    assert isinstance(ready, Ready)
    calls = {"n": 0}

    def fails_after_a_while() -> bool:
        calls["n"] += 1
        if calls["n"] > 3:
            raise PermissionError(13, "denied")
        return False

    original = ctx.controls.kill_requested
    try:
        ctx.controls.kill_requested = fails_after_a_while  # type: ignore[method-assign]
        with pytest.raises(PermissionError):
            execute(ready, ctx)
    finally:
        ctx.controls.kill_requested = original  # type: ignore[method-assign]
    kinds = [json.loads(line)["kind"] for line in ctx.audit.path.read_text().splitlines()]
    # The start is recorded and no "never started" execution line follows it.
    assert kinds[-1] == "start"


def test_a_stream_held_open_past_the_group_is_cut_short_once(tmp_path: Path) -> None:
    """A process that leaves the child's group and keeps both streams open: the host stops reading
    them STREAM_GRACE_S after the group is gone (one wait for both, not one each), keeps what came
    before, and flags both as cut short (rule raw-ceiling)."""
    pid_file = tmp_path / "left-the-group"
    code = (
        "import os, sys, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        f"    open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "    time.sleep(30)\n"
        "    os._exit(0)\n"
        "while not os.path.exists(" + repr(str(pid_file)) + "): time.sleep(0.01)\n"
        "print('hi')\n"
    )
    started = time.monotonic()
    try:
        result = _python(code, timeout_s=20)
        elapsed = time.monotonic() - started
    finally:
        if pid_file.exists():
            os.kill(int(pid_file.read_text()), 9)
    assert (result.ended, result.exit_code, result.stdout) == ("exited", 0, "hi\n")
    assert result.stdout_truncated is True and result.stderr_truncated is True
    assert _exec.STREAM_GRACE_S <= elapsed < 2 * _exec.STREAM_GRACE_S
