"""Deciding a request (steps 5 to 9 of the refusal order) and the replies the host builds."""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from lucille_host import __version__, spec
from lucille_host.jobs import (
    Abandoned,
    Draining,
    Job,
    Outcome,
    Ready,
    check,
    describe_document,
    encode,
    execute,
    process,
    reply_body,
)
from lucille_host.ops import CATALOG_VERSION
from tests._host import MakeContext

JOB_ID = "00000000-0000-4000-8000-000000000001"


def _validate_reply(reply: dict[str, Any]) -> None:
    jsonschema.Draft202012Validator(spec.load_schema("reply")).validate(reply)


def test_host_describe_document_in_stdout(tmp_path: Path, make_context: MakeContext) -> None:
    root = tmp_path / "private-folder-name"
    root.mkdir()
    ctx = make_context(enabled=["host.describe", "host.sleep"], roots={"docs": root})
    # A request's caps do not apply to the document, however small.
    job = Job(
        job_id=JOB_ID, op="host.describe", args={}, caps={"stdout_bytes": 2, "stderr_bytes": 2}
    )
    data = process(job, ctx)
    reply = json.loads(data)
    _validate_reply(reply)
    assert reply["status"] == "done" and reply["exit_code"] == 0
    assert reply["stderr"] == ""
    assert reply["stdout_truncated"] is False and reply["stderr_truncated"] is False
    document = json.loads(reply["stdout"])
    jsonschema.Draft202012Validator(spec.load_schema("host.describe")).validate(document)
    assert document == {
        "host_version": __version__,
        "protocol": "lucille.exec/1",
        "os_family": ctx.os_family,
        "catalog_version": CATALOG_VERSION,
        "enabled_ops": ["host.describe", "host.sleep"],
        "root_aliases": ["docs"],
        "policy_digest": ctx.policy_digest,
    }
    # Never a path: not the root's, nor its real path, nor $HOME.
    text = data.decode()
    for path in (str(root), os.path.realpath(root), str(Path.home())):
        assert path not in text


def test_unheld_root_is_path_outside_root(make_context: MakeContext) -> None:
    ctx = make_context(enabled=["files.sha256"], roots=["docs"])
    job = Job(job_id=JOB_ID, op="files.sha256", args={"root": "photos", "path": "a.txt"})
    assert check(job, ctx) == "path_outside_root"
    reply = json.loads(process(job, ctx))
    _validate_reply(reply)
    assert (reply["status"], reply["code"]) == ("refused", "path_outside_root")


def test_a_root_whose_folder_is_gone_is_path_outside_root(
    tmp_path: Path, make_context: MakeContext
) -> None:
    ctx = make_context(enabled=["files.sha256"], roots={"docs": tmp_path / "gone"})
    job = Job(job_id=JOB_ID, op="files.sha256", args={"root": "docs", "path": "a.txt"})
    assert check(job, ctx) == "path_outside_root"


@pytest.mark.parametrize(
    ("paused", "op", "enabled", "args", "roots", "expected"),
    [
        # Each case has two faults; the earlier step wins.
        (True, "files.delete", [], {}, [], "paused"),
        (True, "files.sha256", [], {"root": "x", "path": "/a"}, [], "paused"),
        (False, "files.delete", [], {"bogus": 1}, [], "unknown_op"),
        (False, "files.search", [], {"bogus": 1}, [], "unknown_op"),
        (False, "files.sha256", [], {"root": "docs", "path": ""}, ["docs"], "not_enabled"),
        (False, "files.sha256", ["files.sha256"], {"root": "nope", "path": ""}, [], "bad_args"),
        (
            False,
            "files.sha256",
            ["files.sha256"],
            {"root": "nope", "path": "a"},
            [],
            "path_outside_root",
        ),
        (
            False,
            "files.sha256",
            ["files.sha256"],
            {"root": "docs", "path": "../a"},
            ["docs"],
            "path_outside_root",
        ),
        (False, "host.sleep", ["host.sleep"], {"n": 1.0}, [], "bad_args"),
        (False, "host.describe", ["host.describe"], [], [], "bad_args"),
    ],
)
def test_steps_5_to_9_in_order(
    make_context: MakeContext,
    paused: bool,
    op: str,
    enabled: list[str],
    args: Any,
    roots: list[str],
    expected: str,
) -> None:
    ctx = make_context(enabled=enabled, roots=roots)
    if paused:
        ctx.controls.pause()
    assert check(Job(job_id=JOB_ID, op=op, args=args), ctx) == expected


def test_a_refusal_from_the_read_loop_is_passed_through(make_context: MakeContext) -> None:
    ctx = make_context(enabled=["host.describe"])
    job = Job(job_id=JOB_ID, op="host.describe", args={})
    reply = json.loads(process(job, ctx, earlier_code="expired"))
    _validate_reply(reply)
    assert (reply["status"], reply["code"]) == ("refused", "expired")


def test_replies_of_each_shape_hold_to_the_schema(make_context: MakeContext) -> None:
    digest = make_context().policy_digest
    shapes = [
        Outcome(status="refused", code="bad_args"),
        Outcome(
            status="done",
            exit_code=1,
            stdout="",
            stdout_truncated=False,
            stderr="x",
            stderr_truncated=False,
            duration_ms=3,
        ),
        Outcome(status="failed", code="timeout", duration_ms=30000),
        Outcome(
            status="failed",
            code="killed_locally",
            exit_code=-9,
            stdout="partial",
            stdout_truncated=False,
            stderr="",
            stderr_truncated=False,
            duration_ms=10,
        ),
    ]
    for outcome in shapes:
        body = reply_body(JOB_ID, outcome, digest)
        _validate_reply(body)
        assert json.loads(encode(body)) == body
        assert encode(body).isascii()
        assert body["host_version"] == __version__ and body["policy_digest"] == digest


def test_draining_starts_nothing_and_records_nothing(make_context: MakeContext) -> None:
    ctx = make_context(enabled=["host.sleep"])
    ctx.controls.draining.set()
    with pytest.raises(Draining):
        process(Job(job_id=JOB_ID, op="host.sleep", args={"n": 1}), ctx)
    assert not ctx.audit.path.exists()


def test_an_operation_this_computer_cannot_run_now_is_not_enabled(
    make_context: MakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_context(enabled=["host.describe", "host.sleep"])
    assert "host.sleep" in ctx.policy.enabled_ops
    monkeypatch.setattr(
        "lucille_host.ops.Operation.available", lambda self, family: self.name == "host.describe"
    )
    assert check(Job(job_id=JOB_ID, op="host.sleep", args={"n": 1}), ctx) == "not_enabled"
    # host.describe says the same: it lists what would run now, not what the policy loaded with.
    assert describe_document(ctx)["enabled_ops"] == ["host.describe"]


def test_a_start_that_fails_after_the_decision_is_failed_interrupted(
    tmp_path: Path, make_context: MakeContext
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("a")
    ctx = make_context(enabled=["files.sha256"], roots={"docs": root})
    job = Job(job_id=JOB_ID, op="files.sha256", args={"root": "docs", "path": "a.txt"})
    ready = check(job, ctx)
    assert isinstance(ready, Ready)
    # The folder goes away between the decision and the start.
    (root / "a.txt").unlink()
    root.rmdir()
    outcome = execute(ready, ctx)
    assert (outcome.status, outcome.code, outcome.duration_ms) == ("failed", "interrupted", 0)
    reply = reply_body(JOB_ID, outcome, ctx.policy_digest)
    _validate_reply(reply)
    execution = json.loads(ctx.audit.path.read_text().splitlines()[-1])
    assert (execution["kind"], execution["started"]) == ("execution", False)


def test_a_pause_after_the_decision_stops_the_start(
    tmp_path: Path, make_context: MakeContext
) -> None:
    marker = tmp_path / "the-child-ran"
    ctx = make_context(enabled=["host.sleep"])
    ready = check(Job(job_id=JOB_ID, op="host.sleep", args={"n": 1}), ctx)
    assert isinstance(ready, Ready)
    ready = dataclasses.replace(
        ready, argv=(sys.executable, "-c", f"open({str(marker)!r}, 'w').close()")
    )
    ctx.controls.pause()
    outcome = execute(ready, ctx)
    assert (outcome.status, outcome.code, outcome.duration_ms) == ("failed", "interrupted", 0)
    _validate_reply(reply_body(JOB_ID, outcome, ctx.policy_digest))
    assert not marker.exists()
    lines = [json.loads(line) for line in ctx.audit.path.read_text().splitlines()]
    assert [line["kind"] for line in lines] == ["execution"]
    assert (lines[0]["started"], lines[0]["stopped"]) == (False, "paused")


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_a_drain_signal_after_the_decision_stops_the_start(
    tmp_path: Path, make_context: MakeContext, monkeypatch: pytest.MonkeyPatch, signum: int
) -> None:
    """The signal lands once the decision to run is written, before the child starts: nothing new
    starts while the host drains (spec section 16), so the decision is not carried out."""
    marker = tmp_path / "the-child-ran"
    ctx = make_context(enabled=["host.sleep"])
    real_check = check

    def deciding_to_run_a_marker(job: Job, context: Any) -> Any:
        ready = real_check(job, context)
        assert isinstance(ready, Ready)
        return dataclasses.replace(
            ready, argv=(sys.executable, "-c", f"open({str(marker)!r}, 'w').close()")
        )

    real_decision = ctx.audit.decision

    def decision(*args: Any, **kwargs: Any) -> None:
        real_decision(*args, **kwargs)
        ctx.controls.on_signal(signum, None)

    monkeypatch.setattr("lucille_host.jobs.check", deciding_to_run_a_marker)
    monkeypatch.setattr(ctx.audit, "decision", decision)
    reply = json.loads(process(Job(job_id=JOB_ID, op="host.sleep", args={"n": 1}), ctx))
    assert (reply["status"], reply["code"]) == ("failed", "interrupted")
    assert not marker.exists()
    lines = [json.loads(line) for line in ctx.audit.path.read_text().splitlines()]
    assert [line["kind"] for line in lines] == ["received", "decision", "execution", "sent"]
    assert (lines[2]["started"], lines[2]["stopped"]) == (False, "draining")


def test_an_operation_with_no_root_runs_in_the_filesystem_root(make_context: MakeContext) -> None:
    ready = check(Job(job_id=JOB_ID, op="host.sleep", args={"n": 1}), make_context(["host.sleep"]))
    assert isinstance(ready, Ready) and ready.cwd == "/"


def test_received_is_written_before_deciding(
    make_context: MakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_context(enabled=["host.sleep"])
    seen: list[list[str]] = []
    real_check = check

    def spy(job: Job, context: Any) -> Any:
        seen.append([json.loads(line)["kind"] for line in ctx.audit.path.read_text().splitlines()])
        return real_check(job, context)

    monkeypatch.setattr("lucille_host.jobs.check", spy)
    process(Job(job_id=JOB_ID, op="host.sleep", args={"n": 1}), ctx)
    assert seen == [["received"]]


def _kinds(ctx: Any) -> list[str]:
    return [json.loads(line)["kind"] for line in ctx.audit.path.read_text().splitlines()]


def test_decided_is_called_after_the_decision_line_and_before_anything_runs(
    make_context: MakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_context(enabled=["host.sleep"])
    seen: list[tuple[str, str | None, bytes | None, list[str]]] = []

    def decided(decision: str, code: str | None, data: bytes | None) -> None:
        seen.append((decision, code, data, _kinds(ctx)))

    data = process(Job(job_id=JOB_ID, op="host.sleep", args={"n": 1}), ctx, decided=decided)
    assert seen == [("run", None, None, ["received", "decision"])]
    assert json.loads(data)["status"] == "done"
    # A refusal hands over the exact bytes of its reply, before the sent line.
    seen.clear()
    refused = process(Job(job_id=JOB_ID, op="host.nothing", args={}), ctx, decided=decided)
    assert len(seen) == 1
    decision, code, sent, kinds = seen[0]
    assert (decision, code, sent) == ("refused", "unknown_op", refused)
    assert kinds[-2:] == ["received", "decision"]
    # And one the read loop decided passes through it too.
    seen.clear()
    early = process(
        Job(job_id=JOB_ID, op="x", args=None), ctx, earlier_code="bad_request", decided=decided
    )
    assert seen[0][:3] == ("refused", "bad_request", early)


def test_abandoned_before_the_start_starts_nothing(
    tmp_path: Path, make_context: MakeContext
) -> None:
    ctx = dataclasses.replace(make_context(enabled=["host.sleep"]), stop=lambda: True)
    with pytest.raises(Abandoned):
        process(Job(job_id=JOB_ID, op="host.sleep", args={"n": 30}), ctx)
    lines = [json.loads(line) for line in ctx.audit.path.read_text().splitlines()]
    assert [line["kind"] for line in lines] == ["received", "decision", "execution"]
    assert (lines[2]["started"], lines[2]["stopped"]) == (False, "abandoned")


def test_abandoned_while_running_kills_with_no_reply(
    make_context: MakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = threading.Event()
    ctx = dataclasses.replace(make_context(enabled=["host.sleep"]), stop=stop.is_set)
    done: list[Any] = []
    kills: list[bool] = []
    monkeypatch.setattr(ctx.controls, "kill_done", lambda: kills.append(True))

    def run() -> None:
        try:
            done.append(process(Job(job_id=JOB_ID, op="host.sleep", args={"n": 30}), ctx))
        except Abandoned as exc:
            done.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    thread.start()
    deadline = time.monotonic() + 10
    while '"kind":"start"' not in (ctx.audit.path.read_text() if ctx.audit.path.exists() else ""):
        assert time.monotonic() < deadline, "the child never started"
        time.sleep(0.02)
    stop.set()
    thread.join(15)
    assert time.monotonic() - started < 15
    assert len(done) == 1 and isinstance(done[0], Abandoned)
    lines = [json.loads(line) for line in ctx.audit.path.read_text().splitlines()]
    assert [line["kind"] for line in lines] == ["received", "decision", "start", "execution"]
    assert (lines[3]["ended"], lines[3]["abandoned"]) == ("killed", True)
    # A user's kill request, if any, is left as it was: the room's abandonment is not one.
    assert kills == []


# --- github.read: what the host fills, the child's environment, the HTTP status ---------------

GITHUB_ARGS = {"credential": "personal", "path": "/user"}


@pytest.fixture(autouse=True)
def _no_keyring_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # keyring's keyringrc.cfg is looked for under XDG_CONFIG_HOME: point it at an empty folder, so
    # a setting of the computer running the tests cannot turn github.read off here.
    empty = tmp_path / "no-keyring-config"
    empty.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty))


def _stand_in_interpreter(tmp_path: Path, stderr_line: str, code: int = 0) -> str:
    """An executable that stands in for the host's interpreter: it prints its environment and
    its arguments, one per line, and writes ``stderr_line`` on standard error."""
    script = tmp_path / "python-stand-in"
    script.write_text(
        "#!/bin/sh\n"
        "/usr/bin/env\n"
        'for a in "$@"; do echo "arg=$a"; done\n'
        f"echo '{stderr_line}' >&2\n"
        f"exit {code}\n"
    )
    script.chmod(0o755)
    return str(script)


def test_github_read_is_not_enabled_where_the_host_knows_no_interpreter(
    make_context: MakeContext,
) -> None:
    # A program that embeds the host (library.py) gives none: the entry is unavailable there.
    ctx = make_context(enabled=["host.describe", "github.read"], interpreter=None)
    assert "github.read" in ctx.policy.enabled_ops
    job = Job(job_id=JOB_ID, op="github.read", args=GITHUB_ARGS)
    assert check(job, ctx) == "not_enabled"
    assert describe_document(ctx)["enabled_ops"] == ["host.describe"]


def test_github_read_runs_the_hosts_interpreter_with_the_values_the_host_fills(
    tmp_path: Path, make_context: MakeContext, state_dir: Path
) -> None:
    interpreter = _stand_in_interpreter(tmp_path, "HTTP 200")
    environ = {
        "HOME": "/home/someone",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "HTTPS_PROXY": "http://proxy.example:3128",
        "SSL_CERT_FILE": "/tmp/evil.pem",
        "PYTHONPATH": "/tmp/evil",
        "LD_PRELOAD": "/tmp/evil.so",
    }
    ctx = make_context(enabled=["github.read"], interpreter=interpreter, environ=environ)
    assert describe_document(ctx)["enabled_ops"] == ["github.read"]
    ready = check(Job(job_id=JOB_ID, op="github.read", args=GITHUB_ARGS), ctx)
    assert isinstance(ready, Ready)
    assert ready.cwd == "/"
    assert ready.argv == (
        interpreter,
        *("-I", "-B", "-W", "ignore", "-X", "utf8", "-m", "lucille_host.proxy"),
        *("--state", str(state_dir), "--profile", "default", "--", "personal", "/user"),
    )
    outcome = execute(ready, ctx)
    assert (outcome.status, outcome.exit_code, outcome.stderr) == ("done", 0, "HTTP 200\n")
    assert outcome.stdout is not None
    lines = outcome.stdout.splitlines()
    env = dict(line.split("=", 1) for line in lines if not line.startswith("arg="))
    copied = (
        {"HOME": "/home/someone"}
        if sys.platform == "darwin"
        else {
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        }
    )
    # macOS's /bin/sh adds a few of its own (PWD, SHLVL, _): only what the host passed counts.
    passed = {k: v for k, v in env.items() if k not in ("PWD", "SHLVL", "_", "OLDPWD")}
    assert passed == {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", **copied}
    assert [line[4:] for line in lines if line.startswith("arg=")] == list(ready.argv[1:])
    execution = json.loads(ctx.audit.path.read_text().splitlines()[-1])
    assert execution["kind"] == "execution" and execution["http_status"] == 200
    assert execution["executable"] == interpreter


def test_a_github_read_reply_with_an_unterminated_key_block_is_flagged_truncated(
    tmp_path: Path, make_context: MakeContext
) -> None:
    # GitHub's body is passed on as it came: a private-key block that never ends takes the rest of
    # the stream, which the flag says, as it does for a body cut at the cap.
    script = tmp_path / "python-stand-in"
    script.write_text(
        "#!/bin/sh\n"
        'printf \'%s\' \'{"content":"-----BEGIN PRIVATE KEY-----\\nMIIE","name":"k"}\'\n'
        "echo 'HTTP 200' >&2\n"
    )
    script.chmod(0o755)
    ctx = make_context(enabled=["github.read"], interpreter=str(script))
    reply = json.loads(process(Job(job_id=JOB_ID, op="github.read", args=GITHUB_ARGS), ctx))
    assert reply["status"] == "done"
    assert reply["stdout"] == '{"content":"[redacted private key]'
    assert reply["stdout_truncated"] is True and reply["stderr_truncated"] is False


@pytest.mark.parametrize(
    "stderr_line",
    [
        "lucille-host proxy: no such credential",
        "HTTP 2000",
        "HTTP 099",
        "HTTP 600",
        "HTTP ٢٠٠",
    ],
)
def test_no_http_status_is_recorded_when_the_child_reported_none(
    tmp_path: Path, make_context: MakeContext, stderr_line: str
) -> None:
    interpreter = _stand_in_interpreter(tmp_path, stderr_line, code=2)
    ctx = make_context(enabled=["github.read"], interpreter=interpreter)
    ready = check(Job(job_id=JOB_ID, op="github.read", args=GITHUB_ARGS), ctx)
    assert isinstance(ready, Ready)
    outcome = execute(ready, ctx)
    # The child's exit codes are data under done, never a refusal of the protocol.
    assert (outcome.status, outcome.exit_code) == ("done", 2)
    execution = json.loads(ctx.audit.path.read_text().splitlines()[-1])
    assert "http_status" not in execution


def test_other_children_get_the_fixed_environment_only(
    tmp_path: Path, make_context: MakeContext
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("a")
    ctx = make_context(
        enabled=["files.sha256"],
        roots={"docs": root},
        environ={"HOME": "/h", "XDG_RUNTIME_DIR": "/r"},
    )
    ready = check(
        Job(job_id=JOB_ID, op="files.sha256", args={"root": "docs", "path": "a.txt"}), ctx
    )
    assert isinstance(ready, Ready)
    assert ready.operation.child_env(ctx.os_family, {"HOME": "/h"}) == {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
    }
    assert execute(ready, ctx).status == "done"


def _keyring_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    """Keyring's own settings, in the host's environment: none, or one that turns it off."""
    monkeypatch.delenv("PYTHON_KEYRING_BACKEND", raising=False)
    config = tmp_path / "custom-config"
    config.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    if how == "null":
        monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    elif how == "fail":
        monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    elif how == "keyringrc":
        folder = config / "python_keyring"
        folder.mkdir()
        (folder / "keyringrc.cfg").write_text(
            "[backend]\ndefault-keyring = keyring.backends.null.Keyring\n"
        )


@pytest.mark.parametrize("how", ["null", "fail", "keyringrc"])
def test_github_read_is_refused_before_its_child_starts_while_keyring_is_off(
    tmp_path: Path, make_context: MakeContext, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """The child's environment carries neither PYTHON_KEYRING_BACKEND nor XDG_CONFIG_HOME, so it
    cannot see keyring's off switch: the host refuses the request itself, and starts nothing."""
    started = tmp_path / "started"
    script = tmp_path / "python-stand-in"
    script.write_text(f"#!/bin/sh\ntouch '{started}'\necho 'HTTP 200' >&2\n")
    script.chmod(0o755)
    _keyring_settings(tmp_path, monkeypatch, how)
    ctx = make_context(enabled=["host.describe", "github.read"], interpreter=str(script))
    job = Job(job_id=JOB_ID, op="github.read", args=GITHUB_ARGS)
    assert check(job, ctx) == "not_enabled"
    reply = json.loads(process(job, ctx))
    assert (reply["status"], reply["code"]) == ("refused", "not_enabled")
    assert not started.exists()
    kinds = [json.loads(line)["kind"] for line in ctx.audit.path.read_text().splitlines()]
    assert "start" not in kinds
    assert describe_document(ctx)["enabled_ops"] == ["host.describe"]


def test_github_read_runs_with_keyring_on_and_the_off_switch_spares_other_operations(
    tmp_path: Path, make_context: MakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _stand_in_interpreter(tmp_path, "HTTP 200")
    _keyring_settings(tmp_path, monkeypatch, "none")
    ctx = make_context(enabled=["github.read", "host.sleep"], interpreter=script)
    assert isinstance(check(Job(job_id=JOB_ID, op="github.read", args=GITHUB_ARGS), ctx), Ready)
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    assert isinstance(check(Job(job_id=JOB_ID, op="host.sleep", args={"n": 1}), ctx), Ready)


# --- the gmail. operations: the args element, the same interpreter and keyring rules ----------

GMAIL_OPS = ("gmail.check", "gmail.mailboxes", "gmail.search", "gmail.read")
GMAIL_ARGS = {
    "gmail.check": {"credential": "default"},
    "gmail.mailboxes": {"credential": "default"},
    "gmail.search": {"credential": "default", "subject": "שלום", "unseen": True, "limit": 5},
    "gmail.read": {"credential": "default", "id": "1795581384121139226"},
}


@pytest.mark.parametrize("op", GMAIL_OPS)
def test_gmail_runs_the_hosts_interpreter_with_its_args_as_one_json_element(
    tmp_path: Path, make_context: MakeContext, state_dir: Path, op: str
) -> None:
    interpreter = _stand_in_interpreter(tmp_path, "")
    environ = {"HOME": "/h", "XDG_RUNTIME_DIR": "/r", "DBUS_SESSION_BUS_ADDRESS": "unix:x"}
    ctx = make_context(enabled=[op], interpreter=interpreter, environ=environ)
    ready = check(Job(job_id=JOB_ID, op=op, args=GMAIL_ARGS[op]), ctx)
    assert isinstance(ready, Ready)
    assert ready.argv == (
        interpreter,
        *("-I", "-B", "-W", "ignore", "-X", "utf8", "-m", "lucille_host.gmail"),
        *("--state", str(state_dir), "--profile", "default", "--", op),
        json.dumps(GMAIL_ARGS[op], sort_keys=True, separators=(",", ":"), ensure_ascii=True),
    )
    outcome = execute(ready, ctx)
    assert (outcome.status, outcome.exit_code) == ("done", 0)
    assert outcome.stdout is not None
    lines = outcome.stdout.splitlines()
    env = dict(line.split("=", 1) for line in lines if not line.startswith("arg="))
    passed = {k: v for k, v in env.items() if k not in ("PWD", "SHLVL", "_", "OLDPWD")}
    copied = (
        {"HOME": "/h"}
        if sys.platform == "darwin"
        else {"DBUS_SESSION_BUS_ADDRESS": "unix:x", "XDG_RUNTIME_DIR": "/r"}
    )
    assert passed == {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", **copied}


@pytest.mark.parametrize("op", GMAIL_OPS)
def test_gmail_is_not_enabled_where_the_host_knows_no_interpreter(
    make_context: MakeContext, op: str
) -> None:
    ctx = make_context(enabled=["host.describe", op], interpreter=None)
    assert check(Job(job_id=JOB_ID, op=op, args=GMAIL_ARGS[op]), ctx) == "not_enabled"
    assert describe_document(ctx)["enabled_ops"] == ["host.describe"]


@pytest.mark.parametrize("op", GMAIL_OPS)
def test_gmail_is_refused_before_its_child_starts_while_keyring_is_off(
    tmp_path: Path, make_context: MakeContext, monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    started = tmp_path / "started"
    script = tmp_path / "python-stand-in"
    script.write_text(f"#!/bin/sh\ntouch '{started}'\n")
    script.chmod(0o755)
    _keyring_settings(tmp_path, monkeypatch, "keyringrc")
    ctx = make_context(enabled=["host.describe", op], interpreter=str(script))
    job = Job(job_id=JOB_ID, op=op, args=GMAIL_ARGS[op])
    reply = json.loads(process(job, ctx))
    assert (reply["status"], reply["code"]) == ("refused", "not_enabled")
    assert not started.exists()
    assert describe_document(ctx)["enabled_ops"] == ["host.describe"]


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("gmail.search", {"credential": "default", "subject": "a\r\nb"}),
        ("gmail.search", {"credential": "default", "since": "2026-02-31"}),
        ("gmail.read", {"credential": "default", "id": "18446744073709551616"}),
    ],
)
def test_gmail_args_the_prose_rule_refuses_start_nothing(
    tmp_path: Path, make_context: MakeContext, op: str, args: dict[str, object]
) -> None:
    started = tmp_path / "started"
    script = tmp_path / "python-stand-in"
    script.write_text(f"#!/bin/sh\ntouch '{started}'\n")
    script.chmod(0o755)
    ctx = make_context(enabled=[op], interpreter=str(script))
    reply = json.loads(process(Job(job_id=JOB_ID, op=op, args=args), ctx))
    assert (reply["status"], reply["code"]) == ("refused", "bad_args")
    assert not started.exists()
