"""Supervising ``lucille-host run`` (spec/CLI.md section 13): its event stream, the lifeline on
standard input, its exit codes, the local policy while it runs, ``policy reset``, and the refusal
to start below the newest release that ran on the profile.

``run`` is started through the command, as a shell starts it, on a thread of its own, against
Switchboard's in-process test hub and a fake Lucille (``tests/_supervise.py``). Long-polls hold for
a fraction of a second here.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import errno
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from keyring.backends.fail import Keyring as FailKeyring
from keyring.errors import KeyringError
from switchboard import writekey
from switchboard.testing import hub

from lucille_host import (
    __version__,
    _switchboard,
    cli,
    envelope,
    gate,
    link,
    loop,
    paths,
    policy,
    rooms,
    spec,
)
from lucille_host.controls import Controls
from lucille_host.ops import OPERATIONS
from tests import _audit_hook
from tests._host import HostEnv, lock_held
from tests._keyring import MemoryKeyring
from tests._run import RunHost, body, counting, fast, nothing, until
from tests._supervise import Lines, Piped, Supervised, folder, strings
from tests.spec import _support

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)

# How long a test watches for something that must not happen.
QUIET = 1.0
# `run` as a shell starts it.
EVENTS = ("run", "--json-events", "--exit-on-stdin-eof")
CLI_MD = Path(__file__).resolve().parents[1] / "spec" / "CLI.md"


@pytest.fixture
def h() -> Iterator[Any]:
    with hub(start=time.time()) as handle:
        yield handle


@pytest.fixture(autouse=True)
def _runs_stopped(h: Any) -> Iterator[None]:
    """Every run a test started is stopped before its hub closes, even when the test fails."""
    yield
    Supervised.stop_all()
    RunHost.stop_all()


@pytest.fixture
def host(host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunHost:
    fast(monkeypatch)
    return RunHost(h, host_env, tmp_path)


@pytest.fixture
def supervise(host: RunHost, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Supervised]:
    def start(*argv: str, stdin: str = "pipe") -> Supervised:
        return Supervised(monkeypatch, argv, host=host, stdin=stdin)

    return start


def _stopped(reason: str, **fields: Any) -> dict[str, Any]:
    return {"v": 1, "type": "stopped", "reason": reason, **fields}


DRAINED = _stopped("drained", cause="stdin_end")


def _phases(found: list[dict[str, Any]]) -> list[str]:
    return [one["phase"] for one in found]


def _record(dirs: paths.Dirs, version: str) -> None:
    paths.ensure_dir(dirs.state)
    (dirs.state / "last_version.json").write_text(json.dumps({"v": 1, "version": version}))


def _recorded(dirs: paths.Dirs) -> Any:
    path = dirs.state / "last_version.json"
    return json.loads(path.read_text()) if path.exists() else None


class _Counted(MemoryKeyring):
    """A keyring in memory that counts every call."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def get_password(self, service: str, username: str) -> str | None:
        self.calls += 1
        return super().get_password(service, username)

    def set_password(self, service: str, username: str, password: str) -> None:
        self.calls += 1
        super().set_password(service, username, password)


# --- what the events carry ----------------------------------------------------------------------


def test_request_events_carry_no_absolute_path(
    host: RunHost, supervise: Callable[..., Supervised], tmp_path: Path
) -> None:
    docs = tmp_path / "Documents"
    docs.mkdir()
    (docs / "a.txt").write_text("the contents of a file\n")
    host.enable("files.sha256")
    folder(host.dirs, "docs", docs)
    run = supervise("--json", *EVENTS)
    run.ready()
    run.offer("work-1")
    asked = {
        "relative": body(op="files.sha256", args={"root": "docs", "path": "a.txt"}),
        "absolute": body(op="files.sha256", args={"root": "docs", "path": "/etc/hostname"}),
        "double": body(op="files.sha256", args={"root": "docs", "path": "//x"}),
        "control": body(op="files.sha256", args={"root": "docs", "path": "\u0001/etc/passwd"}),
        "describe": body(),
        "not_an_op": body(op="Not An Op"),
        "too_large": body(
            op="files.sha256",
            args={"root": "docs", "path": "a.txt"},
            pad="x" * envelope.REQUEST_LIMIT,
        ),
    }
    for sent in asked.values():
        host.request("work-1", sent)
    replies = {name: host.reply("work-1", sent["job_id"])["body"] for name, sent in asked.items()}
    run.close_stdin()
    assert run.wait() == 0
    objects = run.out.objects()
    assert objects[-1] == DRAINED
    described = json.loads(replies["describe"]["stdout"])
    record = host.record()
    private = [
        str(host.env.home),
        str(docs),
        str(host.dirs.config),
        str(host.dirs.state),
        _switchboard.LUCILLE_HUB,
        hashlib.sha256(b"the contents of a file\n").hexdigest(),
        replies["relative"]["stdout"],
        replies["describe"]["stdout"],
        described["policy_digest"],
        host.lucille.sas(),
        *(held.workspace for held in record.held.values()),
    ]
    for text in strings(objects):
        assert not text.lstrip().startswith("/"), text
        assert not text.startswith("swbp1_"), text
        for secret in private:
            assert secret not in text, (secret, text)
    found = {name: run.requests(sent["job_id"]) for name, sent in asked.items()}
    assert _phases(found["relative"]) == ["received", "decided", "started", "finished"]
    assert {k: found["relative"][0][k] for k in ("op", "root", "path")} == {
        "op": "files.sha256",
        "root": "docs",
        "path": "a.txt",
    }
    for name in ("absolute", "double"):
        assert _phases(found[name]) == ["received", "decided", "finished"], name
        assert (found[name][0]["root"], found[name][0]["path"]) == ("docs", None), name
        assert found[name][1]["code"] == "path_outside_root", name
    # A path with a character a terminal must not show is not written either, whatever it names.
    assert (found["control"][0]["root"], found["control"][0]["path"]) == ("docs", None)
    assert _phases(found["describe"]) == ["received", "decided", "finished"]
    assert {k: found["describe"][0][k] for k in ("op", "root", "path")} == {
        "op": "host.describe",
        "root": None,
        "path": None,
    }
    # An op without an operation name's form, and a request over the protocol's size, say nothing
    # of what they asked for.
    for name in ("not_an_op", "too_large"):
        assert _phases(found[name]) == ["received", "decided", "finished"], name
        assert {k: found[name][0][k] for k in ("op", "root", "path")} == dict.fromkeys(
            ("op", "root", "path")
        ), name
    for name, sent in asked.items():
        (sent_line,) = host.events("sent", job_id=sent["job_id"])
        assert found[name][-1]["reply_bytes"] == sent_line["size"], name


def test_run_events_are_one_valid_object_per_line(
    host: RunHost, supervise: Callable[..., Supervised]
) -> None:
    run = supervise(*EVENTS)
    run.ready()
    run.offer("work-1")
    host.reply("work-1", host.request("work-1"))
    run.close_stdin()
    assert run.wait() == 0
    objects = _support.cli_objects(run.out.text)
    assert run.out.text.isascii() and run.out.text.endswith("\n")
    assert objects[0]["type"] == "ready" and objects[1]["type"] == "state"
    assert objects[-1] == DRAINED
    assert [found["type"] for found in objects].count("ready") == 1
    assert not any(found["type"] == "error" for found in objects)
    assert objects[0] == {
        "v": 1,
        "type": "ready",
        "host_version": __version__,
        "pair": host.name,
    }


def test_host_too_old_event(host: RunHost, supervise: Callable[..., Supervised]) -> None:
    run = supervise(*EVENTS)
    run.ready()
    run.offer("work-1")
    sent = body(min_host_version="999.0.0")
    reply = host.reply("work-1", host.request("work-1", sent))["body"]
    assert (reply["status"], reply["code"]) == ("refused", "host_too_old")
    run.close_stdin()
    assert run.wait() == 0
    job = sent["job_id"]
    ordered = [
        (found["type"], found.get("phase"))
        for found in run.out.objects()
        if found.get("job_id") == job
    ]
    assert ordered == [
        ("request", "received"),
        ("request", "decided"),
        ("host_too_old", None),
        ("request", "finished"),
    ]
    (too_old,) = run.objects("host_too_old")
    assert too_old == {
        "v": 1,
        "type": "host_too_old",
        "job_id": job,
        "min_host_version": "999.0.0",
        "host_version": __version__,
    }
    decided = run.requests(job)[1]
    assert (decided["decision"], decided["code"]) == ("refused", "host_too_old")
    finished = run.requests(job)[-1]
    assert (finished["status"], finished["code"]) == ("refused", "host_too_old")


def _finished(run: Supervised, job_id: str) -> dict[str, Any]:
    """A request's ``finished`` object, once it is written, without what every one carries."""
    found = until(
        lambda: [one for one in run.requests(job_id) if one["phase"] == "finished"],
        what=f"the finished object of {job_id}",
    )
    (finished,) = found
    return {k: v for k, v in finished.items() if k not in ("v", "type", "phase", "job_id")}


def _streams(reply: dict[str, Any]) -> dict[str, Any]:
    return {
        f"{name}_bytes": None if reply.get(name) is None else envelope.size(reply[name])
        for name in ("stdout", "stderr")
    }


def test_finished_says_what_the_reply_says(
    host: RunHost, supervise: Callable[..., Supervised], tmp_path: Path
) -> None:
    # A request that ran, one killed while it ran, and one whose room was given up while it ran:
    # each `finished` says what the reply that was posted says, and an abandoned one gets its own.
    docs = tmp_path / "Documents"
    docs.mkdir()
    (docs / "a.txt").write_text("the contents of a file\n")
    host.enable("files.sha256", "host.sleep")
    folder(host.dirs, "docs", docs)
    run = supervise(*EVENTS)
    run.ready()
    run.offer("work-1")
    for sent in (
        body(op="files.sha256", args={"root": "docs", "path": "a.txt"}),
        body(op="host.sleep", args={"n": 1}),
    ):
        reply = host.reply("work-1", host.request("work-1", sent))["body"]
        assert reply["status"] == "done"
        assert reply["stdout"] or reply["duration_ms"] > 0
        assert _finished(run, sent["job_id"]) == {
            "status": "done",
            "exit_code": reply["exit_code"],
            "duration_ms": reply["duration_ms"],
            **_streams(reply),
            "reply_bytes": envelope.size(reply),
        }
    killed = body(op="host.sleep", args={"n": 60})
    host.request("work-1", killed)
    until(lambda: "started" in _phases(run.requests(killed["job_id"])), what="the child started")
    Controls(host.dirs.state).kill()
    reply = host.reply("work-1", killed["job_id"])["body"]
    assert (reply["status"], reply["code"]) == ("failed", "killed_locally")
    assert reply["exit_code"] is not None
    assert _finished(run, killed["job_id"]) == {
        "status": "failed",
        "code": reply["code"],
        "exit_code": reply["exit_code"],
        "duration_ms": reply["duration_ms"],
        **_streams(reply),
        "reply_bytes": envelope.size(reply),
    }
    Controls(host.dirs.state).resume()
    left = body(op="host.sleep", args={"n": 60})
    host.request("work-1", left)
    until(lambda: "started" in _phases(run.requests(left["job_id"])), what="the child started")
    host.lucille.leave("work-1")
    assert _finished(run, left["job_id"]) == {"status": "abandoned"}
    run.close_stdin()
    assert run.wait() == 0
    assert host.events("sent", job_id=left["job_id"]) == []
    assert _support.cli_objects(run.out.text)[-1] == DRAINED


def test_state_follows_what_run_sees(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast(monkeypatch)
    host = RunHost(h, host_env, tmp_path, open_gate=False)
    run = Supervised(monkeypatch, EVENTS, host=host)
    run.ready()
    first = until(run.state, what="the first state")
    assert first["pair"] == host.name
    assert (first["confirmed"], first["paused"], first["rooms"]) == (False, False, [])
    assert first["policy"] in ("ok", "checking")
    host.confirm()
    until(lambda: (run.state() or {}).get("confirmed"), what="a state saying confirmed")
    until(lambda: (run.state() or {}).get("policy") == "ok", what="the policy checked")
    room = run.offer("work-1")
    assert run.state()["rooms"] == [{"name": room, "state": "reading"}]
    Controls(host.dirs.state).pause()
    until(lambda: run.state()["paused"], what="a state saying paused")
    Controls(host.dirs.state).resume()
    until(lambda: not run.state()["paused"], what="a state saying not paused")
    run.close_stdin()
    assert run.wait() == 0
    states = run.objects("state")
    # A state is written when what it says changes, and not again until then, within a minute:
    # a new contact time or skew alone waits.

    def said(found: dict[str, Any]) -> Any:
        kept = {k: v for k, v in found.items() if k not in ("last_hub_contact", "clock")}
        return kept, found["last_hub_contact"] is None, found["clock"]["state"]

    seen = [said(found) for found in states]
    assert all(one != two for one, two in zip(seen, seen[1:], strict=False)), seen


def test_state_comes_at_least_every_status_period(
    host: RunHost, supervise: Callable[..., Supervised], monkeypatch: pytest.MonkeyPatch
) -> None:
    # No state for twice the period means `run` is not stepping: a shell can tell a host that has
    # wedged from one with nothing new to say.
    monkeypatch.setattr("lucille_host.loop.STATUS_EVERY", 0.3)
    run = supervise(*EVENTS)
    run.ready()
    until(run.state, what="the first state")
    before = len(run.objects("state"))
    time.sleep(1.5)
    assert len(run.objects("state")) >= before + 3


def _slow_resolves(monkeypatch: pytest.MonkeyPatch, hold: float) -> None:
    """Main's resolves wait ``hold`` seconds and fail, as against a hub that takes the connection
    and never answers, which the client gives up on."""

    def resolve(_link: Any, _name: str) -> Any:
        time.sleep(hold)
        raise _switchboard.HubError("the hub did not answer (ReadTimeout)")

    monkeypatch.setattr(_switchboard.Link, "resolve", resolve)


def _gaps(run: Supervised, dirs: paths.Dirs, seconds: float) -> tuple[list[float], set[Any]]:
    """Watch ``run`` for ``seconds``: the longest wait between two ``state`` objects at each
    step, and every ``written_at`` status.json said."""
    gaps: list[float] = []
    written: set[Any] = set()
    count, last = len(run.objects("state")), time.monotonic()
    ends = last + seconds
    while time.monotonic() < ends:
        now, found = time.monotonic(), len(run.objects("state"))
        if found != count:
            gaps.append(now - last)
            count, last = found, now
        with contextlib.suppress(OSError, ValueError):
            written.add(json.loads(dirs.status_file.read_text())["written_at"])
        time.sleep(0.02)
    gaps.append(time.monotonic() - last)
    return gaps, written


def test_state_goes_on_while_main_waits_on_the_hub(
    host: RunHost, supervise: Callable[..., Supervised], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hub that takes the connection and never answers holds main in each call until the client
    # gives up. `state` and status.json still come at least every period, as main last wrote
    # them, from the first one on: `run` is waiting on the hub, not stuck, and a shell reads that
    # from `last_hub_contact`.
    monkeypatch.setattr("lucille_host.loop.STATUS_EVERY", 0.3)
    _slow_resolves(monkeypatch, 1.5)
    run = supervise(*EVENTS)
    run.ready()
    until(run.state, 1.0, what="a state before the first hub call ends")
    gaps, written = _gaps(run, host.dirs, 4.0)
    assert max(gaps) < 1.0, gaps
    assert len(written) >= 6, written
    assert run.state()["last_hub_contact"] is None
    run.close_stdin()
    assert run.wait() == 0


def test_a_step_held_too_long_goes_quiet(
    host: RunHost, supervise: Callable[..., Supervised], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Past WAIT_MOST in one step, nothing is written for main any more: it counts as not
    # stepping.
    monkeypatch.setattr("lucille_host.loop.STATUS_EVERY", 0.3)
    monkeypatch.setattr("lucille_host.loop.WAIT_MOST", 0.5)
    _slow_resolves(monkeypatch, 2.5)
    run = supervise(*EVENTS)
    run.ready()
    until(run.state, what="the first state")
    gaps, _ = _gaps(run, host.dirs, 5.0)
    assert max(gaps) > 1.5, gaps
    run.close_stdin()
    assert run.wait() == 0


def test_state_says_when_the_hub_was_reached(
    host: RunHost, supervise: Callable[..., Supervised]
) -> None:
    # A shell reads the hub as out of reach from `last_hub_contact`: once a reply is posted, it is
    # a time of now, and the clock is measured.
    run = supervise(*EVENTS)
    run.ready()
    run.offer("work-1")
    host.reply("work-1", host.request("work-1"))
    latest = until(
        lambda: [
            found
            for found in run.objects("state")
            if found["last_hub_contact"] is not None and found["clock"]["state"] == "ok"
        ],
        what="a state with a hub contact and a measured clock",
    )[-1]
    contact = latest["last_hub_contact"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", contact), contact
    assert abs(_parse_time(contact) - time.time()) < 30
    skew = latest["clock"]["skew_s"]
    assert isinstance(skew, int | float) and not isinstance(skew, bool)
    assert abs(skew) < _switchboard.SKEW_WINDOW
    run.close_stdin()
    assert run.wait() == 0


def _parse_time(text: str) -> float:
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z").timestamp()


def test_state_says_the_clock_is_skewed(
    host: RunHost, supervise: Callable[..., Supervised], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The hub refuses the host's reply for its clock: `state` says the clock is skewed.
    skewing = threading.local()
    sign = writekey.RoomWriteKey.sign_request
    post = _switchboard.RoomClient.post

    def signed(key: Any, *args: Any, **options: Any) -> Any:
        if getattr(skewing, "on", False):
            options["now"] = time.time() + 2 * _switchboard.SKEW_WINDOW
        return sign(key, *args, **options)

    def posted(client: Any, *args: Any, **options: Any) -> Any:
        skewing.on = True
        try:
            return post(client, *args, **options)
        finally:
            skewing.on = False

    monkeypatch.setattr(writekey.RoomWriteKey, "sign_request", signed)
    monkeypatch.setattr(_switchboard.RoomClient, "post", posted)
    run = supervise(*EVENTS)
    run.ready()
    run.offer("work-1")
    assert until(run.state)["clock"]["state"] != "skewed"
    host.request("work-1")
    until(lambda: run.state()["clock"]["state"] == "skewed", what="a state saying skewed")
    run.close_stdin()
    assert run.wait() == 0


def test_state_says_why_the_host_left_its_room(
    host: RunHost, supervise: Callable[..., Supervised]
) -> None:
    # A room offered carrying a hub token fails check 4: the host leaves it on its own, holds no
    # room, and `state` says which check it failed.
    run = supervise(*EVENTS)
    run.ready()
    until(lambda: (run.state() or {}).get("policy") == "ok", what="the policy checked")
    assert run.state()["last_left_because"] is None
    host.lucille.offer("work-1", token="sb_room_token")
    until(lambda: run.state()["last_left_because"] == "check_4", what="a state saying check_4")
    assert run.state()["rooms"] == []
    assert host.record().held == {}
    run.close_stdin()
    assert run.wait() == 0


def test_state_rooms_bound_is_the_room_cap() -> None:
    schema = spec.load_cli_schema("state")
    assert schema["properties"]["rooms"]["maxItems"] == rooms.CAP


# --- the lifeline ---------------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["closed", "killed"])
def test_stdin_eof_drains(host: RunHost, supervise: Callable[..., Supervised], how: str) -> None:
    host.enable("host.sleep")
    run = supervise(*EVENTS)
    run.ready()
    run.offer("work-1")
    sleeping = body(op="host.sleep", args={"n": 2})
    host.request("work-1", sleeping)
    until(lambda: "started" in _phases(run.requests(sleeping["job_id"])), what="the child started")
    if how == "closed":
        run.close_stdin()
    else:
        # The only write end is held by another process, which is then killed.
        holder = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            pass_fds=(run.stdin_write_end,),
        )
        try:
            run.close_stdin()
            time.sleep(0.3)
            assert run.running and host.events("lifeline") == []
        finally:
            holder.kill()
            holder.wait(10)
    until(lambda: host.events("lifeline"), what="the end of standard input seen")
    later = host.request("work-1", body())
    assert run.wait() == 0
    reply = host.lucille.reply("work-1", sleeping["job_id"])
    assert reply is not None and reply["body"]["status"] == "done"
    assert host.lucille.reply("work-1", later) is None
    assert host.journal().get(later) is None
    assert host.events("received", job_id=later) == []
    assert run.out.objects()[-1] == DRAINED
    assert [line["event"] for line in host.events("lifeline")] == ["stdin_end"]


def test_run_without_the_lifeline_never_reads_stdin(
    host: RunHost, supervise: Callable[..., Supervised]
) -> None:
    # Standard input ended at once, and no --exit-on-stdin-eof: `run` keeps running, as under
    # nohup.
    run = supervise("run", "--json-events")
    run.close_stdin()
    run.ready()
    run.offer("work-1")
    host.reply("work-1", host.request("work-1"))
    assert run.running
    run.drain()
    assert run.wait() == 0
    assert run.out.objects()[-1] == _stopped("drained", cause="signal")
    assert host.events("lifeline") == []


def test_no_stdin_at_all_drains_at_once(
    host: RunHost, supervise: Callable[..., Supervised]
) -> None:
    run = supervise(*EVENTS, stdin="closed")
    assert run.wait(20) == 0
    # It ended before `run` started serving: no ready, no hub call, nothing written down.
    assert run.out.objects() == [DRAINED]
    assert not host.dirs.status_file.exists()


def test_lifeline_on_a_non_blocking_pipe(host: RunHost, monkeypatch: pytest.MonkeyPatch) -> None:
    # A read end handed over non-blocking reads EAGAIN, which is not the end; it is made blocking,
    # so the lifeline waits in its read rather than spinning.
    reads: list[int] = []
    real = os.read

    def read(fd: int, size: int) -> bytes:
        reads.append(fd)
        return real(fd, size)

    monkeypatch.setattr("os.read", read)
    run = Supervised(monkeypatch, EVENTS, host=host, nonblocking=True)
    fd = run.stdin.fileno()
    run.ready()
    run.offer("work-1")
    host.reply("work-1", host.request("work-1"))
    time.sleep(QUIET)
    assert run.running and host.events("lifeline") == []
    assert reads.count(fd) <= 3, reads.count(fd)
    run.close_stdin()
    assert run.wait() == 0
    assert run.out.objects()[-1] == DRAINED


def test_lifeline_ending_during_the_start_checks_drains(
    host: RunHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The shell quits while `run` makes its last start checks, before `run` listens for the
    # lifeline's end: `run` still drains for it, saying why, and calls no hub.
    lifelines: list[loop.Lifeline] = []
    started: list[str] = []
    made: list[Supervised] = []
    real_start = loop.Lifeline.start
    real_check = link.Session.check_ready

    def start(lifeline: loop.Lifeline) -> loop.Lifeline:
        lifelines.append(lifeline)
        return real_start(lifeline)

    def check_ready(session: link.Session) -> str:
        made[0].close_stdin()
        assert lifelines[0].ended.wait(10)
        return real_check(session)

    monkeypatch.setattr(loop.Lifeline, "start", start)
    monkeypatch.setattr(link.Session, "check_ready", check_ready)
    monkeypatch.setattr(link.Session, "start", lambda _session: started.append("start"))
    made.append(Supervised(monkeypatch, EVENTS, host=host))
    run = made[0]
    assert run.wait(30) == 0
    assert run.out.objects() == [DRAINED]
    assert started == []
    assert [line["event"] for line in host.events("lifeline")] == ["stdin_end"]


def test_ctrl_c_after_stdin_eof_kills(host: RunHost, monkeypatch: pytest.MonkeyPatch) -> None:
    # `run` on the main thread, with its own handlers, as `lucille-host run` runs: standard input
    # ends while an operation runs, then Ctrl-C kills it, as a second Ctrl-C does.
    assert threading.current_thread() is threading.main_thread()
    host.enable("host.sleep")
    real = _switchboard.open_link
    monkeypatch.setattr(
        "lucille_host._switchboard.open_link",
        lambda dirs, config, **_: real(dirs, config, client_class=host.client_class),
    )
    read, write = os.pipe()
    stdin = os.fdopen(read, "rb")
    out, err = Lines(), Lines()
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)
    sent = body(op="host.sleep", args={"n": 60})
    main = threading.main_thread().ident
    assert main is not None
    stops = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    inherited = {stop: signal.signal(stop, nothing) for stop in stops}
    trouble: list[BaseException] = []

    def driver() -> None:
        try:
            until(lambda: [o for o in out.objects() if o["type"] == "ready"], what="ready")
            host.lucille.offer("work-1")
            host.request("work-1", sent)
            until(lambda: host.events("start", job_id=sent["job_id"]), what="the child started")
            os.close(write)
            until(lambda: host.events("lifeline"), what="the end of standard input seen")
            signal.pthread_kill(main, signal.SIGINT)
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            trouble.append(exc)
            with contextlib.suppress(OSError):
                os.close(write)

    thread = threading.Thread(target=driver, daemon=True)
    thread.start()
    try:
        rc = cli.main(list(EVENTS))
    finally:
        thread.join(30)
        for stop, handler in inherited.items():
            signal.signal(stop, handler)
        stdin.close()
    assert trouble == []
    assert rc == 0
    reply = host.lucille.reply("work-1", sent["job_id"])
    assert reply is not None
    assert (reply["body"]["status"], reply["body"]["code"]) == ("failed", "killed_locally")
    assert Controls(host.dirs.state).paused()
    assert out.objects()[-1] == DRAINED


class _Gone(Lines):
    """A standard output whose reader is gone: every write fails with EPIPE."""

    def write(self, text: str) -> int:
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")


def test_unread_stdout_keeps_the_exit_code(host: RunHost, monkeypatch: pytest.MonkeyPatch) -> None:
    # A standard output nobody reads any more. Without the lifeline, `run` stops writing events
    # and goes on answering; with it, `run` drains. Either way it exits with the code it chose.
    with monkeypatch.context() as patch:
        run = Supervised(patch, ("run", "--json-events"), host=host, out=_Gone())
        host.lucille.offer("work-1")
        until(lambda: host.record().held, what="the room taken")
        assert host.reply("work-1", host.request("work-1"))["body"]["status"] == "done"
        assert run.running
        run.drain()
        assert run.wait() == 0
        assert "Traceback" not in run.err.text
    assert host.events("lifeline") == []
    with monkeypatch.context() as patch:
        run = Supervised(patch, EVENTS, host=host, out=_Gone())
        assert run.wait(20) == 0
        assert "Traceback" not in run.err.text
    assert [line["event"] for line in host.events("lifeline")] == ["stdout_gone"]


@pytest.mark.parametrize("closed", ["stdout", "stderr"])
def test_a_refusal_to_a_closed_stream_keeps_its_exit_code(tmp_path: Path, closed: str) -> None:
    # In a real process: standard output's, or standard error's, read end closed before `run`
    # starts, and a refusal at start. The exit code is still the refusal's, with no traceback,
    # and the stream still read says so.
    read, write = os.pipe()
    os.close(read)
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""), "SWITCHBOARD_URL": "x"}
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-m", "lucille_host", *EVENTS],
            stdin=subprocess.DEVNULL,
            stdout=write if closed == "stdout" else subprocess.PIPE,
            stderr=write if closed == "stderr" else subprocess.PIPE,
            env=env,
            timeout=60,
            check=False,
        )
    finally:
        os.close(write)
    if closed == "stdout":
        assert done.returncode == 24, done.stderr
        assert b"Traceback" not in done.stderr and done.stderr
    else:
        assert done.returncode == 24, done.stdout
        assert _support.cli_objects(done.stdout.decode("ascii")) == [_stopped("startup_refused")]


# --- one run per profile ------------------------------------------------------------------------


def test_second_run_exits_21(
    host: RunHost, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("lucille_host.loop.__version__", "0.2.0")
    counted = _Counted()
    counted.items = dict(host.env.keyring.items)
    monkeypatch.setattr("keyring.core._keyring_backend", counted)
    lock = host.dirs.state / loop.RUN_LOCK
    # Another process holds the lock: the second `run` changes nothing at all.
    lines = len(host.audit())
    with lock_held(lock), monkeypatch.context() as patch:
        refused = Supervised(patch, ("run", "--json-events"), host=host)
        assert refused.wait(20) == 21
    assert refused.out.objects() == [_stopped("already_running")]
    assert refused.err.text == f"lucille-host: {loop.ALREADY_RUNNING}\n"
    assert not host.dirs.status_file.exists()
    assert len(host.audit()) == lines
    assert _recorded(host.dirs) is None
    assert counted.calls == 0
    # A `run` of the profile going in this process: the second refuses too, and the first goes on
    # answering.
    first = Supervised(monkeypatch, EVENTS, host=host)
    first.ready()
    first.offer("work-1")
    with monkeypatch.context() as patch:
        second = Supervised(patch, ("run", "--json-events"), host=host)
        assert second.wait(20) == 21
    assert second.out.objects() == [_stopped("already_running")]
    assert host.reply("work-1", host.request("work-1"))["body"]["status"] == "done"
    first.close_stdin()
    assert first.wait() == 0


# --- exit codes -----------------------------------------------------------------------------------


def _world(tmp_path: Path, name: str, patch: pytest.MonkeyPatch) -> HostEnv:
    home = tmp_path / name / "home"
    home.mkdir(parents=True)
    patch.setenv("HOME", str(home))
    keyring = MemoryKeyring()
    patch.setattr("keyring.core._keyring_backend", keyring)
    return HostEnv(home=home, dirs=paths.dirs(), keyring=keyring)


class _Case:
    """How one exit of `run` is made: a set-up, and what the test does once `run` is ready.
    ``before`` are the words before ``run`` on the command line."""

    def __init__(
        self,
        reason: str,
        setup: Callable[..., RunHost | None],
        then: Callable[[Supervised, RunHost | None, pytest.MonkeyPatch], None] | None = None,
        *,
        lifeline: bool = False,
        before: tuple[str, ...] = (),
        **fields: Any,
    ) -> None:
        self.reason = reason
        self.setup = setup
        self.then = then
        self.lifeline = lifeline
        self.before = before
        self.fields = fields


def _paired(env: HostEnv, h: Any, where: Path, patch: pytest.MonkeyPatch, stack: Any) -> RunHost:
    return RunHost(h, env, where)


def _unpaired(env: HostEnv, h: Any, where: Path, patch: pytest.MonkeyPatch, stack: Any) -> None:
    return None


def _no_gate(env: HostEnv, h: Any, where: Path, patch: pytest.MonkeyPatch, stack: Any) -> RunHost:
    made = RunHost(h, env, where)
    gate.remove(env.dirs.gate_file)
    return made


def _two_pairs(env: HostEnv, h: Any, where: Path, patch: pytest.MonkeyPatch, stack: Any) -> None:
    patch.setattr(link.Session, "held", lambda _session: ["pair-a", "pair-b"])


def _refusing(env: HostEnv, h: Any, where: Path, patch: pytest.MonkeyPatch, stack: Any) -> RunHost:
    made = RunHost(h, env, where)

    def failing(_link: Any, _name: str) -> Any:
        raise _switchboard.PairFailed("aborted_here", "the library refused the pair")

    patch.setattr(_switchboard.Link, "resolve", failing)
    return made


def _deadline_passed(
    env: HostEnv, h: Any, where: Path, patch: pytest.MonkeyPatch, stack: Any
) -> RunHost:
    made = RunHost(h, env, where, open_gate=False)
    session = made.session()
    shown = session.show(made.name)
    assert shown is not None
    gate.save(env.dirs.gate_file, dataclasses.replace(shown[1], deadline=time.time() - 1))
    return made


def _locked(env: HostEnv, h: Any, where: Path, patch: pytest.MonkeyPatch, stack: Any) -> None:
    stack.enter_context(lock_held(paths.ensure_dir(env.dirs.state) / loop.RUN_LOCK))


def _insecure(env: HostEnv, h: Any, where: Path, patch: pytest.MonkeyPatch, stack: Any) -> None:
    patch.setattr("keyring.core._keyring_backend", FailKeyring())


class _Unreadable(MemoryKeyring):
    def get_password(self, service: str, username: str) -> str | None:
        raise KeyringError("locked")


class _Unusable(_Unreadable):
    def set_password(self, service: str, username: str, password: str) -> None:
        raise KeyringError("locked")


def _unreadable(env: HostEnv, h: Any, where: Path, patch: pytest.MonkeyPatch, stack: Any) -> None:
    patch.setattr("keyring.core._keyring_backend", _Unreadable())


def _switchboard_set(
    env: HostEnv, h: Any, where: Path, patch: pytest.MonkeyPatch, stack: Any
) -> None:
    patch.setenv("SWITCHBOARD_URL", "https://hub.example")


def _as_root(env: HostEnv, h: Any, where: Path, patch: pytest.MonkeyPatch, stack: Any) -> None:
    patch.setattr("os.geteuid", lambda: 0)


def _newer_recorded(
    env: HostEnv, h: Any, where: Path, patch: pytest.MonkeyPatch, stack: Any
) -> None:
    _record(env.dirs, "999.0.0")


def _raising(raised: BaseException) -> Callable[..., None]:
    def setup(env: HostEnv, h: Any, where: Path, patch: pytest.MonkeyPatch, stack: Any) -> None:
        def failing(*_: Any, **__: Any) -> None:
            raise raised

        patch.setattr("lucille_host.loop.check_version", failing)

    return setup


def _leaking(env: HostEnv, h: Any, where: Path, patch: pytest.MonkeyPatch, stack: Any) -> RunHost:
    made = RunHost(h, env, where)

    def failing(_link: Any, _name: str) -> Any:
        raise _switchboard.PairFailed("aborted", "the pairing ended early")

    patch.setattr(_switchboard.Link, "resolve", failing)
    return made


def _forget_here(run: Supervised, made: RunHost | None, patch: pytest.MonkeyPatch) -> None:
    assert made is not None
    made.session().forget()


def _lucille_forgets(run: Supervised, made: RunHost | None, patch: pytest.MonkeyPatch) -> None:
    assert made is not None
    made.lucille.forget()


def _store_fails(run: Supervised, made: RunHost | None, patch: pytest.MonkeyPatch) -> None:
    # After `ready`: the keyring cannot be read or written any more, and Lucille offers a room,
    # whose key the host must keep.
    assert made is not None
    patch.setattr("keyring.core._keyring_backend", _Unusable())
    made.lucille.offer("work-1")


CASES = {
    "drained": _Case("drained", _paired, lambda run, _, __: run.drain(), cause="signal"),
    "drained-stdin": _Case(
        "drained",
        _paired,
        lambda run, _, __: run.close_stdin(),
        lifeline=True,
        cause="stdin_end",
    ),
    "forgotten": _Case("forgotten", _paired, _forget_here),
    "ended": _Case("ended", _paired, _lucille_forgets),
    "not_paired": _Case("not_paired", _unpaired),
    "no_gate": _Case("no_gate", _no_gate),
    "several_pairs": _Case("several_pairs", _two_pairs),
    "pair_unusable": _Case("pair_unusable", _refusing),
    "leaked": _Case("leaked", _leaking),
    "expired": _Case("expired", _deadline_passed),
    "already_running": _Case("already_running", _locked),
    "insecure_keyring": _Case("insecure_keyring", _insecure),
    "store_error": _Case("store_error", _unreadable),
    "startup_refused-switchboard": _Case("startup_refused", _switchboard_set),
    "startup_refused-root": _Case("startup_refused", _as_root),
    "downgrade": _Case("downgrade", _newer_recorded, last_version="999.0.0"),
    "failed-os_error": _Case(
        "failed", _raising(PermissionError(errno.EACCES, "Permission denied")), code="os_error"
    ),
    "failed-internal": _Case("failed", _raising(ZeroDivisionError("a bug")), code="internal"),
    "failed-store_error": _Case("failed", _paired, _store_fails, code="store_error"),
    "failed-bad_profile": _Case(
        "failed", _unpaired, before=("--profile", "../escape"), code="bad_profile"
    ),
}


def test_every_reason_has_a_case() -> None:
    assert {made.reason for made in CASES.values()} == set(cli.RUN_EXITS)
    schema = spec.load_cli_schema("stopped")
    codes = schema["$defs"]["stopped.failed"]["properties"]["code"]["enum"]
    assert set(codes) == cli.RUN_FAILURES and len(codes) == len(cli.RUN_FAILURES)


@pytest.mark.parametrize("case", CASES)
def test_run_exit_codes(
    case: str,
    host_env: HostEnv,
    h: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast(monkeypatch)
    made = CASES[case]
    seen: dict[str, tuple[Any, str, list[dict[str, Any]]]] = {}
    for mode in ("events", "text"):
        argv = [*made.before, "run"]
        if mode == "events":
            argv.append("--json-events")
        if made.lifeline:
            argv.append("--exit-on-stdin-eof")
        with monkeypatch.context() as patch, contextlib.ExitStack() as stack:
            env = _world(tmp_path, mode, patch)
            paired = made.setup(env, h, tmp_path / mode, patch, stack)
            run = Supervised(patch, argv, host=paired, client_class=h.client_class())
            if made.then is not None:
                if mode == "events":
                    until(run.state, what="the first state")
                else:
                    status = env.dirs.status_file
                    until(status.exists, what="run started")
                made.then(run, paired, patch)
            outcome: Any
            try:
                outcome = run.wait(30)
            except ZeroDivisionError as exc:
                outcome = exc
            name = paired.name if paired is not None else "pair-a"
            objects = run.out.objects() if mode == "events" else []
            seen[mode] = (outcome, run.err.text.replace(name, "<pair>"), objects)
    events, text = seen["events"], seen["text"]
    expected = cli.RUN_EXITS[made.reason]
    if made.reason == "failed" and made.fields.get("code") == "internal":
        assert isinstance(events[0], ZeroDivisionError) and isinstance(text[0], ZeroDivisionError)
    else:
        assert events[0] == expected, events[1]
        assert text[0] == expected, text[1]
        assert events[1] == text[1]
    last = events[2][-1]
    assert last == _stopped(made.reason, **made.fields)
    assert _support.cli_objects(json.dumps(last, separators=(",", ":")) + "\n") == [last]
    assert not any(found["type"] == "stopped" for found in events[2][:-1])


def test_run_exits_are_the_spec_s_table() -> None:
    text = CLI_MD.read_text(encoding="utf-8")
    section = text[text.index("### 13.3") :]
    section = section[: section.index("\n### ", 1)]
    table: dict[str, int] = {}
    for row in re.findall(r"^\| (\d+) \|([^|]*)\|", section, re.MULTILINE):
        exit_code, reasons = row
        for reason in re.findall(r"`([a-z_]+)`", reasons):
            table[reason] = int(exit_code)
    assert table == dict(cli.RUN_EXITS)
    schema = spec.load_cli_schema("stopped")
    reasons: set[str] = set()
    for branch in schema["$defs"].values():
        given = branch.get("properties", {}).get("reason", {})
        reasons |= set(given.get("enum", [])) | ({given["const"]} if "const" in given else set())
    assert reasons == set(cli.RUN_EXITS)
    assert 22 not in cli.RUN_EXITS.values()


# --- the local policy while run runs ----------------------------------------------------------


def test_removed_root_refused_on_next_request(
    host: RunHost, supervise: Callable[..., Supervised], tmp_path: Path
) -> None:
    docs = tmp_path / "Documents"
    docs.mkdir()
    (docs / "a.txt").write_text("a\n")
    host.enable("files.sha256")
    folder(host.dirs, "docs", docs)
    run = supervise(*EVENTS)
    run.ready()
    run.offer("work-1")
    asked = {"args": {"root": "docs", "path": "a.txt"}, "op": "files.sha256"}

    def ask() -> dict[str, Any]:
        return host.reply("work-1", host.request("work-1", body(**asked)))["body"]

    assert ask()["status"] == "done"
    # `roots remove docs`, the verb, while `run` runs: the next request is refused, no restart.
    assert cli.main(["roots", "remove", "docs"]) == 0
    reply = ask()
    assert (reply["status"], reply["code"]) == ("refused", "path_outside_root")
    # The folder back; then the file rewritten in place without it, to the same size, with its
    # modification time put back: only its change time says so.
    folder(host.dirs, "docs", docs)
    assert ask()["status"] == "done"
    path = host.dirs.policy_file
    with_docs = path.read_bytes()
    stat = path.stat()
    without = policy.dumps(policy.Policy(enabled=policy.load(path).enabled))
    padded = without.encode() + b"#" * (len(with_docs) - len(without.encode()) - 1) + b"\n"
    assert len(padded) == len(with_docs)
    with open(path, "r+b") as handle:
        handle.write(padded)
        handle.truncate()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = path.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
    )
    reply = ask()
    assert (reply["status"], reply["code"]) == ("refused", "path_outside_root")
    run.close_stdin()
    assert run.wait() == 0
    assert len(run.objects("ready")) == 1


@pytest.mark.parametrize(
    "invalid", ["this is not toml\n", "version = 1\nnot_a_key = 1\n"], ids=["toml", "key"]
)
def test_an_invalid_file_holds_offers_while_an_operation_runs(
    host: RunHost,
    supervise: Callable[..., Supervised],
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
) -> None:
    # The worker is busy for seconds; main still sees the file go invalid at its next look, by
    # what it holds (not TOML, or a key the policy does not have), and accepts no room from then
    # on.
    host.enable("host.sleep")
    calls = counting(monkeypatch, _switchboard.Link, "accept_room")
    run = supervise(*EVENTS)
    run.ready()
    run.offer("work-1")
    assert calls["accept_room"] == 1
    sleeping = body(op="host.sleep", args={"n": 4})
    host.request("work-1", sleeping)
    until(lambda: "started" in _phases(run.requests(sleeping["job_id"])), what="the child started")
    host.dirs.policy_file.write_text(invalid)
    until(lambda: run.state()["policy"] == "invalid", 3, what="a state saying invalid")
    host.lucille.offer("work-2")
    time.sleep(QUIET)
    assert "finished" not in _phases(run.requests(sleeping["job_id"]))
    assert calls["accept_room"] == 1
    assert host.events("offer", event="refused", check=rooms.CHECK_POLICY)
    reply = host.reply("work-1", sleeping["job_id"])
    assert reply["body"]["status"] == "done"


def _invalid(host: RunHost) -> None:
    paths.ensure_dir(host.dirs.config)
    host.dirs.policy_file.write_text("this is not toml\n")


def _refused_for_policy(host: RunHost) -> list[dict[str, Any]]:
    return host.events("offer", event="refused", check=rooms.CHECK_POLICY)


def test_an_offer_waiting_at_start_waits_for_a_valid_policy(
    host: RunHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Lucille offered a room before `run` started. While the worker's first load of the policy is
    # held up (`checking`), and then while the file is invalid, the offer waits, from the first
    # step on.
    calls = counting(monkeypatch, _switchboard.Link, "accept_room")
    host.lucille.offer("work-1")
    release = threading.Event()
    real = policy.load

    def load(*args: Any, **options: Any) -> Any:
        release.wait(30)
        return real(*args, **options)

    monkeypatch.setattr("lucille_host.policy.load", load)
    try:
        run = Supervised(monkeypatch, EVENTS, host=host)
        run.ready()
        until(lambda: _refused_for_policy(host), what="the offer held")
        time.sleep(QUIET)
        assert run.state()["policy"] == "checking"
        assert run.state()["offers_waiting"] == 1
        assert calls["accept_room"] == 0 and host.record().held == {}
        _invalid(host)
    finally:
        release.set()
    until(lambda: run.state()["policy"] == "invalid", what="a state saying invalid")
    time.sleep(QUIET)
    assert calls["accept_room"] == 0
    assert host.record().offers and host.record().held == {}
    assert all(line["offers"] == 1 for line in _refused_for_policy(host))
    assert cli.main(["policy", "reset"]) == 0
    until(lambda: host.record().held, what="the room taken")
    assert calls["accept_room"] == 1
    run.close_stdin()
    assert run.wait() == 0


def test_an_interrupted_accept_waits_for_a_valid_policy(
    host: RunHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stop interrupted an accept after the hub took it, and `run` starts on an invalid file: the
    # accept is not finished until the file is valid again.
    session = host.session()
    session.start()
    real = _switchboard.Link.accept_room

    def interrupted(opened: _switchboard.Link, *args: Any) -> Any:
        real(opened, *args)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(_switchboard.Link, "accept_room", interrupted)
        host.lucille.offer("work-1")
        session.note(_switchboard.TAG_SHARED)
        with pytest.raises(KeyboardInterrupt):
            session.step()
    accepting = host.record().accepting
    assert accepting is not None and host.record().held == {}
    _invalid(host)
    run = Supervised(monkeypatch, EVENTS, host=host)
    run.ready()
    until(lambda: (run.state() or {}).get("policy") == "invalid", what="a state saying invalid")
    time.sleep(QUIET)
    record = host.record()
    assert record.accepting == accepting and record.held == {}
    assert run.state()["rooms"] == []
    assert cli.main(["policy", "reset"]) == 0

    def finished() -> bool:
        record = host.record()
        return record.accepting is None and accepting.room in record.held

    until(finished, what="the accept finished")
    run.close_stdin()
    assert run.wait() == 0


def test_no_room_is_read_while_the_policy_is_invalid_at_start(
    host: RunHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A room held from an earlier `run`, and a new one started on an invalid file: it reads no room
    # and answers nothing until the file is valid again.
    with monkeypatch.context() as patch:
        first = Supervised(patch, EVENTS, host=host)
        first.ready()
        first.offer("work-1")
        host.reply("work-1", host.request("work-1"))
        first.close_stdin()
        assert first.wait() == 0
    assert host.record().held
    _invalid(host)
    run = Supervised(monkeypatch, EVENTS, host=host)
    run.ready()
    until(lambda: (run.state() or {}).get("policy") == "invalid", what="a state saying invalid")
    job_id = host.request("work-1")
    time.sleep(QUIET)
    assert [found["rooms"] for found in run.objects("state")] == [[]] * len(run.objects("state"))
    assert host.events("received", job_id=job_id) == []
    assert cli.main(["policy", "reset"]) == 0
    assert host.reply("work-1", job_id)["body"]["status"] == "done"
    assert run.state()["rooms"] != []
    run.close_stdin()
    assert run.wait() == 0


def test_a_verdict_on_an_earlier_file_counts_for_nothing(
    host: RunHost, supervise: Callable[..., Supervised], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The worker loads a valid file and is held there; the file then turns invalid by what it
    # holds, and main holds the offers. The worker's verdict on the earlier file comes after: it
    # says nothing of the file now, and lets no offer through.
    calls = counting(monkeypatch, _switchboard.Link, "accept_room")
    run = supervise(*EVENTS)
    run.ready()
    run.offer("work-1")
    assert calls["accept_room"] == 1
    real = policy.load
    holding = threading.Event()
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    held: list[int] = []

    def load(*args: Any, **options: Any) -> Any:
        if not holding.is_set() or threading.current_thread().name != loop.WORKER:
            return real(*args, **options)
        index = len(held)
        if index >= 2:
            return real(*args, **options)
        held.append(index)
        try:
            return real(*args, **options)
        finally:
            entered[index].set()
            release[index].wait(30)

    monkeypatch.setattr("lucille_host.policy.load", load)
    try:
        holding.set()
        host.enable("host.sleep")  # a valid change: main asks the worker to load it
        assert entered[0].wait(10)
        host.dirs.policy_file.write_text("this is not toml\n")
        until(lambda: run.state()["policy"] == "invalid", 5, what="a state saying invalid")
        host.lucille.offer("work-2")
        until(lambda: _refused_for_policy(host), what="the offer held")
        release[0].set()
        time.sleep(QUIET)
        states = [found["policy"] for found in run.objects("state")]
        assert calls["accept_room"] == 1, states
        assert states[states.index("invalid") :] == ["invalid"] * (
            len(states) - states.index("invalid")
        )
    finally:
        release[0].set()
        release[1].set()
    run.close_stdin()
    assert run.wait() == 0


def test_a_policy_file_that_cannot_be_read_idles(
    host: RunHost, supervise: Callable[..., Supervised], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A policy file that cannot be looked at (its folder's permissions) is an invalid policy:
    # `run` acts on nothing, and does not stop.
    run = supervise(*EVENTS)
    run.ready()
    until(lambda: (run.state() or {}).get("policy") == "ok", what="the policy checked")
    target = str(host.dirs.policy_file)
    real_stat = os.stat
    real_read = Path.read_bytes
    blocked = threading.Event()
    blocked.set()

    def stat(path: Any, *args: Any, **options: Any) -> Any:
        if blocked.is_set() and os.fspath(path) == target:
            raise PermissionError(errno.EACCES, "Permission denied", target)
        return real_stat(path, *args, **options)

    def read_bytes(self: Path) -> bytes:
        if blocked.is_set() and str(self) == target:
            raise PermissionError(errno.EACCES, "Permission denied", target)
        return real_read(self)

    monkeypatch.setattr("os.stat", stat)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    until(lambda: run.state()["policy"] == "invalid", what="a state saying invalid")
    time.sleep(QUIET)
    assert run.running
    blocked.clear()
    until(lambda: run.state()["policy"] == "ok", what="the policy valid again")
    run.close_stdin()
    assert run.wait() == 0
    assert run.out.objects()[-1] == DRAINED


def test_policy_signature_sees_every_change(home: Path) -> None:
    dirs = paths.dirs()
    path = dirs.policy_file
    assert policy.signature(path) is None
    paths.ensure_dir(dirs.config)
    policy.save(path, policy.fresh())
    first = policy.signature(path)
    policy.save(path, policy.fresh())
    second = policy.signature(path)
    assert second != first  # a new inode
    stat = path.stat()
    text = path.read_bytes()
    with open(path, "r+b") as handle:
        handle.write(text)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    third = policy.signature(path)
    assert third != second  # the change time
    os.chmod(path, 0o400)
    assert policy.signature(path) != third
    path.unlink()
    assert policy.signature(path) is None


def test_policy_is_loaded_only_by_the_worker(
    host: RunHost, supervise: Callable[..., Supervised], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Loading the policy looks at each of its folders, which may wait on a stalled mount: only the
    # thread that handles requests does it, never the thread that drains.
    threads: list[str] = []
    real = policy.load
    current = policy.load(host.dirs.policy_file)
    sleep_enabled = dataclasses.replace(
        current,
        enabled=MappingProxyType(
            {**current.enabled, "host.sleep": OPERATIONS["host.sleep"].definition_hash}
        ),
    )

    def load(*args: Any, **options: Any) -> Any:
        threads.append(threading.current_thread().name)
        return real(*args, **options)

    monkeypatch.setattr("lucille_host.policy.load", load)
    run = supervise(*EVENTS)
    run.ready()
    run.offer("work-1")
    host.reply("work-1", host.request("work-1"))
    paths.ensure_dir(host.dirs.config)
    policy.save(host.dirs.policy_file, sleep_enabled)
    until(lambda: len(threads) >= 3, what="the change checked")
    host.reply("work-1", host.request("work-1"))
    run.close_stdin()
    assert run.wait() == 0
    assert threads and set(threads) == {loop.WORKER}


def test_a_stalled_policy_load_does_not_hold_up_the_drain(
    host: RunHost, supervise: Callable[..., Supervised], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A folder on a stalled mount holds the worker inside a policy load, with a request it has
    # not written down. The end of standard input still ends `run`, at once, with nothing decided.
    run = supervise(*EVENTS)
    run.ready()
    run.offer("work-1")
    stalled = threading.Event()
    release = threading.Event()
    real = policy.load

    def load(*args: Any, **options: Any) -> Any:
        stalled.set()
        release.wait(60)
        return real(*args, **options)

    monkeypatch.setattr("lucille_host.policy.load", load)
    monkeypatch.setattr("lucille_host.loop.KILL_WAIT", 1.0)
    try:
        job = host.request("work-1")
        assert stalled.wait(10)
        run.close_stdin()
        assert run.wait(15) == 0
    finally:
        release.set()
    assert run.out.objects()[-1] == DRAINED
    assert host.journal().get(job) is None
    assert host.events("received", job_id=job) == []


def test_a_failing_audit_write_still_stops_run(
    host: RunHost, supervise: Callable[..., Supervised], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The end of standard input sets the drain before anything else, so a lifeline whose own
    # audit line cannot be written still stops `run`: no host outlives its shell.
    from lucille_host.audit import AuditLog

    real = AuditLog.write

    def write(self: AuditLog, kind: str, **fields: Any) -> None:
        if kind == "lifeline":
            raise OSError(errno.ENOSPC, "No space left on device")
        real(self, kind, **fields)

    run = supervise(*EVENTS)
    run.ready()
    monkeypatch.setattr(AuditLog, "write", write)
    run.close_stdin()
    # It ends either way: drained, when main finished draining before the lifeline came to its
    # audit line, or failed, when it saw the failure first.
    rc = run.wait(20)
    last = run.out.objects()[-1]
    assert (rc, last) in (
        (0, DRAINED),
        (1, _stopped("failed", code="os_error")),
    )


def test_drain_keeps_writing_status(
    host: RunHost, supervise: Callable[..., Supervised], monkeypatch: pytest.MonkeyPatch
) -> None:
    # While `run` drains, status still says it runs: a shell that finds the run lock held can tell
    # a `run` finishing its operation from one that stopped without saying so.
    monkeypatch.setattr("lucille_host.loop.STATUS_EVERY", 0.2)
    host.enable("host.sleep")
    run = supervise(*EVENTS)
    run.ready()
    run.offer("work-1")
    sleeping = body(op="host.sleep", args={"n": 3})
    host.request("work-1", sleeping)
    until(lambda: "started" in _phases(run.requests(sleeping["job_id"])), what="the child started")
    run.close_stdin()
    until(lambda: host.events("lifeline"), what="the drain")
    seen = set()
    for _ in range(10):
        record = json.loads(host.dirs.status_file.read_text())
        if record["running"]:
            seen.add(record["written_at"])
        time.sleep(0.15)
    assert run.running
    assert len(seen) >= 3
    assert run.wait() == 0


def test_a_blocked_stdout_holds_up_nothing(host: RunHost, monkeypatch: pytest.MonkeyPatch) -> None:
    # A shell that stops reading without dying: the events wait, and nothing else does. Requests
    # are answered, a pause still refuses before anything starts, and the drain ends `run`.
    release = threading.Event()

    class Stuck(Lines):
        def write(self, text: str) -> int:
            release.wait(120)
            return super().write(text)

    monkeypatch.setattr("lucille_host.cli.EVENTS_FLUSH", 0.5)
    host.enable("host.sleep")
    try:
        run = Supervised(monkeypatch, EVENTS, host=host, out=Stuck())
        with _audit_hook.recording() as events:
            host.lucille.offer("work-1")
            until(lambda: host.record().held, what="the room taken")
            assert host.reply("work-1", host.request("work-1"))["body"]["status"] == "done"
            Controls(host.dirs.state).pause()
            paused = host.request("work-1", body(op="host.sleep", args={"n": 1}))
            assert host.reply("work-1", paused)["body"]["code"] == "paused"
        assert _audit_hook.spawns(events) == []
        run.close_stdin()
        assert run.wait(20) == 0
    finally:
        release.set()


def test_a_damaged_digest_key_stops_run(
    host: RunHost, supervise: Callable[..., Supervised]
) -> None:
    (host.dirs.state / policy.KEY_FILE).write_bytes(b"abc")
    run = supervise(*EVENTS)
    assert run.wait(20) == 1
    assert run.out.objects() == [_stopped("failed", code="state_invalid")]


def test_a_shut_gate_waits_and_expires_with_20(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast(monkeypatch)
    host = RunHost(h, host_env, tmp_path, open_gate=False)
    shown = host.session().show(host.name)
    assert shown is not None
    gate.save(host.dirs.gate_file, dataclasses.replace(shown[1], deadline=time.time() + 2))
    run = Supervised(monkeypatch, EVENTS, host=host)
    run.ready()
    first = until(run.state, what="the first state")
    assert first["confirmed"] is False
    assert run.wait(20) == 20
    assert run.out.objects()[-1] == _stopped("expired")
    assert host.opened.names() == []


def test_supervision_spawns_nothing(host: RunHost, supervise: Callable[..., Supervised]) -> None:
    host.enable("host.sleep")
    with _audit_hook.recording() as events:
        run = supervise(*EVENTS)
        run.ready()
        run.offer("work-1")
        host.reply("work-1", host.request("work-1"))
        sleeping = body(op="host.sleep", args={"n": 1})
        host.reply("work-1", host.request("work-1", sleeping))
        run.close_stdin()
        assert run.wait() == 0
    spawned = _audit_hook.spawns(events)
    (start,) = host.events("start", job_id=sleeping["job_id"])
    assert [argv for _, argv in spawned] == [start["argv"]]


# --- the writer -----------------------------------------------------------------------------------


def test_events_on_a_pipe_are_ascii(
    host: RunHost, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Standard output a real pipe, as a shell gives it: the events go out on its descriptor, and a
    # path that is not ASCII is written escaped, the stream going on.
    docs = tmp_path / "Documents"
    docs.mkdir()
    (docs / "R\u00e9sum\u00e9.txt").write_text("x\n")
    host.enable("files.sha256")
    folder(host.dirs, "docs", docs)
    out = Piped()
    try:
        run = Supervised(monkeypatch, EVENTS, host=host, out=out)
        run.ready()
        run.offer("work-1")
        sent = body(op="files.sha256", args={"root": "docs", "path": "R\u00e9sum\u00e9.txt"})
        assert host.reply("work-1", host.request("work-1", sent))["body"]["status"] == "done"
        until(lambda: "finished" in _phases(run.requests(sent["job_id"])), what="finished")
        assert run.running
        run.close_stdin()
        assert run.wait() == 0
    finally:
        out.end()
    assert out.raw.isascii() and out.raw.endswith(b"\n")
    objects = _support.cli_objects(out.text)
    assert objects[-1] == DRAINED
    found = run.requests(sent["job_id"])
    assert _phases(found) == ["received", "decided", "started", "finished"]
    assert found[0]["path"] == "R\u00e9sum\u00e9.txt"


def test_every_event_line_is_within_the_bound() -> None:
    # The longest object is a `received` whose path holds 1024 characters outside the BMP, each
    # written as two escapes.
    path = "\U0001f600" * 1024
    line = cli._line(
        "request",
        {
            "phase": "received",
            "job_id": "00000000-0000-4000-8000-000000000000",
            "op": "o" + ".p" * 31,
            "root": "a" * 32,
            "path": path,
        },
    )
    assert len(line) + 1 <= 16384
    assert "16384 bytes" in CLI_MD.read_text(encoding="utf-8")


def test_event_lines_stay_whole_across_threads() -> None:
    class Slow(Lines):
        def write(self, text: str) -> int:
            for char in text:
                super().write(char)
                time.sleep(0)
            return len(text)

    stream = Slow()
    count, each = 8, 200
    events = cli._Events(stream, most=count * each + 10)

    def write(index: int) -> None:
        for n in range(each):
            events.emit("log_line", text=f"{index}:{n}")

    threads = [threading.Thread(target=write, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    events.close("stopped", reason="drained", cause="signal")
    assert events.finish(30)
    objects = stream.objects()
    assert len(objects) == count * each + 1
    for index in range(count):
        mine = [o["text"] for o in objects if o.get("text", "").startswith(f"{index}:")]
        assert mine == [f"{index}:{n}" for n in range(each)]


def test_nothing_follows_stopped() -> None:
    # The writer is held, so that nothing it writes is behind it when the late object comes.
    release = threading.Event()

    class Held(Lines):
        def write(self, text: str) -> int:
            release.wait(10)
            return super().write(text)

    stream = Held()
    events = cli._Events(stream)
    events.emit("ready", host_version="0.1.0", pair="pair-a")
    events.close("stopped", reason="drained", cause="signal")
    # A thread still finishing an operation writes after `run` stopped: it reaches nobody.
    late = threading.Thread(target=lambda: events.emit("log_line", text="late"))
    late.start()
    late.join(10)
    release.set()
    assert events.finish(10)
    assert [o["type"] for o in stream.objects()] == ["ready", "stopped"]


def test_a_full_queue_counts_as_a_stdout_nobody_reads() -> None:
    release = threading.Event()

    class Stuck(Lines):
        def write(self, text: str) -> int:
            release.wait(30)
            return super().write(text)

    broken: list[bool] = []
    events = cli._Events(Stuck(), most=5)
    events.on_broken = lambda: broken.append(True)
    try:
        for n in range(20):
            events.emit("log_line", text=str(n))
        until(lambda: broken, 5, what="the stream given up")
        assert events.broken
    finally:
        release.set()


# --- policy reset ---------------------------------------------------------------------------------


def test_policy_reset(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dirs = paths.dirs()
    paths.ensure_dir(dirs.config)
    previous = b"version = 1\n"
    dirs.previous_policy_file.write_bytes(previous)

    def ran(*argv: str) -> tuple[int, str, str]:
        capsys.readouterr()
        rc = cli.main(list(argv))
        out, err = capsys.readouterr()
        return rc, out, err

    # An invalid file: set aside, and the fresh policy in its place.
    dirs.policy_file.write_text("this is not toml\n")
    rc, out, _ = ran("policy", "reset", "--json")
    assert rc == 0
    assert [json.loads(line) for line in out.splitlines()] == [{"v": 1, "type": "policy_reset"}]
    assert dirs.invalid_policy_file.read_text() == "this is not toml\n"
    assert dirs.policy_file.read_text() == policy.dumps(policy.fresh())
    assert dirs.previous_policy_file.read_bytes() == previous
    # And in text.
    dirs.policy_file.write_text("[still not]\nversion = 2\n")
    rc, out, _ = ran("policy", "reset")
    assert rc == 0 and out.startswith("reset: ")
    assert dirs.invalid_policy_file.read_text() == "[still not]\nversion = 2\n"
    # A valid file, and no file: nothing changes, and it says the policy is not invalid.
    for valid in (True, False):
        if not valid:
            dirs.policy_file.unlink()
        before = dirs.policy_file.read_bytes() if valid else None
        rc, out, err = ran("--json", "policy", "reset")
        assert rc == 1
        assert json.loads(out.splitlines()[-1])["code"] == "not_invalid"
        assert err.startswith("lucille-host: ")
        assert (dirs.policy_file.read_bytes() if dirs.policy_file.exists() else None) == before
    assert dirs.invalid_policy_file.read_text() == "[still not]\nversion = 2\n"
    # Where the policy lock cannot be taken.
    dirs.policy_file.write_text("broken\n")
    with pytest.MonkeyPatch.context() as patch:

        def cannot(_descriptor: int) -> None:
            raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

        patch.setattr("lucille_host.state.wait_for_lock", cannot)
        rc, out, _ = ran("--json", "policy", "reset")
    assert rc == 1 and json.loads(out.splitlines()[-1])["code"] == "policy_lock_failed"
    assert dirs.policy_file.read_text() == "broken\n"
    # A link as the policy file: the link is moved, and what it points to is left as it is.
    target = home / "elsewhere.toml"
    target.write_text("not a policy\n")
    dirs.policy_file.unlink()
    dirs.policy_file.symlink_to(target)
    rc, _, _ = ran("policy", "reset")
    assert rc == 0
    assert dirs.invalid_policy_file.is_symlink()
    assert target.read_text() == "not a policy\n"
    assert not dirs.policy_file.is_symlink()
    assert dirs.policy_file.read_text() == policy.dumps(policy.fresh())


# --- downgrades -----------------------------------------------------------------------------------


@pytest.fixture
def unpaired(
    host_env: HostEnv, h: Any, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., Supervised]:
    def start(*argv: str, version: str = "0.2.0") -> Supervised:
        monkeypatch.setattr("lucille_host.loop.__version__", version)
        return Supervised(monkeypatch, argv, client_class=h.client_class())

    return start


def test_last_version_is_per_profile(
    host_env: HostEnv, unpaired: Callable[..., Supervised]
) -> None:
    mac = paths.dirs("mac-app")
    assert unpaired("--profile", "mac-app", "run", version="0.3.0").wait(20) == 20
    assert _recorded(mac) == {"v": 1, "version": "0.3.0"}
    assert unpaired("run", version="0.2.0").wait(20) == 20
    assert _recorded(host_env.dirs) == {"v": 1, "version": "0.2.0"}
    refused = unpaired("--profile", "mac-app", "run", "--json-events", version="0.2.0")
    assert refused.wait(20) == 25
    assert refused.out.objects() == [_stopped("downgrade", last_version="0.3.0")]


def test_downgrade_refused(
    host_env: HostEnv, unpaired: Callable[..., Supervised], monkeypatch: pytest.MonkeyPatch
) -> None:
    counted = _Counted()
    monkeypatch.setattr("keyring.core._keyring_backend", counted)
    dirs = host_env.dirs
    _record(dirs, "0.2.0")
    refused = unpaired("run", "--json-events", version="0.1.0")
    assert refused.wait(20) == 25
    assert refused.out.objects() == [_stopped("downgrade", last_version="0.2.0")]
    assert refused.err.text.startswith("lucille-host: ") and "0.2.0" in refused.err.text
    # Before anything else: no keyring call, no status, no audit line, and the record kept.
    assert counted.calls == 0
    assert not dirs.status_file.exists()
    assert not (dirs.state / "audit.log").exists()
    assert _recorded(dirs) == {"v": 1, "version": "0.2.0"}
    # The order is min_host_version's: a version with anything after the triple is older than
    # the triple, and one with no triple is older than every release.
    for version, recorded, exit_code, after in (
        ("0.2.0.dev1", "0.2.0", 25, "0.2.0"),
        ("1.2", "0.0.1", 25, "0.0.1"),
        ("0.2.0", "0.2.0", 20, "0.2.0"),
        ("0.3.0", "0.2.0", 20, "0.3.0"),
        # A build with anything after the triple is never recorded: it is not a release.
        ("0.2.1.dev3", "0.2.0", 20, "0.2.0"),
    ):
        _record(dirs, recorded)
        ran = unpaired("run", version=version)
        assert ran.wait(20) == exit_code, (version, recorded, ran.err.text)
        assert _recorded(dirs) == {"v": 1, "version": after}, (version, recorded)


@pytest.mark.parametrize(
    "held",
    [
        "not json",
        json.dumps({"v": True, "version": "0.2.0"}),
        json.dumps({"v": 1, "version": "0.2.0\n"}),
        json.dumps({"v": 1, "version": "0.2.0.dev1"}),
        json.dumps({"v": 1, "version": "0.2.0", "more": 1}),
        json.dumps({"v": 1, "version": 2}),
    ],
)
def test_last_version_record_must_be_a_version(
    host_env: HostEnv,
    unpaired: Callable[..., Supervised],
    monkeypatch: pytest.MonkeyPatch,
    held: str,
) -> None:
    counted = _Counted()
    monkeypatch.setattr("keyring.core._keyring_backend", counted)
    paths.ensure_dir(host_env.dirs.state)
    (host_env.dirs.state / "last_version.json").write_text(held)
    ran = unpaired("run", "--json-events")
    assert ran.wait(20) == 1
    assert ran.out.objects() == [_stopped("failed", code="state_invalid")]
    assert counted.calls == 0


def test_allow_downgrade_overrides(host_env: HostEnv, unpaired: Callable[..., Supervised]) -> None:
    _record(host_env.dirs, "0.2.0")
    allowed = unpaired("run", "--json-events", "--allow-downgrade", version="0.1.0")
    assert allowed.wait(20) == 20
    assert allowed.out.objects() == [_stopped("not_paired")]
    assert "--allow-downgrade" in allowed.err.text and "0.2.0" in allowed.err.text
    assert _recorded(host_env.dirs) == {"v": 1, "version": "0.2.0"}
    assert unpaired("run", version="0.1.0").wait(20) == 25


def test_release_versions_are_plain_triples() -> None:
    # A release is MAJOR.MINOR.PATCH and nothing else, and a fix is a new PATCH: the downgrade
    # record orders versions as min_host_version does, which puts 0.2.0.post1 below 0.2.0. The
    # version this repository holds is a release's, or a development one's (.devN).
    triple = r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})"
    assert re.fullmatch(triple + r"(\.dev[0-9]+)?", __version__), __version__
