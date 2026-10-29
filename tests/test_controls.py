"""Pause, resume, kill and drain (spec/PROTOCOL.md section 16)."""

from __future__ import annotations

import dataclasses
import errno
import itertools
import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from lucille_host import paths
from lucille_host.cli import main
from lucille_host.controls import Controls, _present, _remove, _write_flag
from lucille_host.jobs import Context, Job, check, execute, process
from tests._host import MakeContext, lock_held


def _cli_context(make_context: MakeContext, enabled: list[str]) -> Context:
    """A context whose controls are the files the CLI writes for the default profile."""
    ctx = make_context(enabled=enabled)
    return dataclasses.replace(ctx, controls=Controls(paths.ensure_dir(paths.dirs().state)))


class _Background:
    """Runs process() in a thread and keeps its reply."""

    def __init__(self, job: Job, ctx: Context) -> None:
        self.reply: dict[str, Any] | None = None
        self.thread = threading.Thread(target=self._run, args=(job, ctx), daemon=True)
        self.thread.start()

    def _run(self, job: Job, ctx: Context) -> None:
        self.reply = json.loads(process(job, ctx))

    def wait(self, timeout: float = 15) -> dict[str, Any]:
        self.thread.join(timeout)
        assert self.reply is not None, "the operation did not finish"
        return self.reply


def _wait_for_child(ctx: Context) -> None:
    """Until the decision to run is on disk and the child has had a moment to start."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if ctx.audit.path.exists() and '"decision":"run"' in ctx.audit.path.read_text():
            time.sleep(0.3)
            return
        time.sleep(0.02)
    raise AssertionError("the child never started")


def test_pause_refuses_and_resume_clears(home: Path, make_context: MakeContext) -> None:
    ctx = _cli_context(make_context, ["host.describe"])
    job = Job(job_id="e" * 8, op="host.describe", args={})
    assert main(["pause"]) == 0
    assert ctx.controls.paused() and not ctx.controls.may_start()
    reply = json.loads(process(job, ctx))
    assert (reply["status"], reply["code"]) == ("refused", "paused")
    assert main(["resume"]) == 0
    assert not ctx.controls.paused() and ctx.controls.may_start()
    assert json.loads(process(job, ctx))["status"] == "done"


def test_kill_kills_the_running_group_and_pauses(home: Path, make_context: MakeContext) -> None:
    ctx = _cli_context(make_context, ["host.sleep"])
    started = time.monotonic()
    running = _Background(Job(job_id="f" * 8, op="host.sleep", args={"n": 60}), ctx)
    _wait_for_child(ctx)
    assert main(["kill"]) == 0
    reply = running.wait()
    assert time.monotonic() - started < 15
    assert (reply["status"], reply["code"]) == ("failed", "killed_locally")
    assert reply["exit_code"] == -9
    # The kill is consumed; the pause stays until the user resumes.
    assert ctx.controls.paused() and not ctx.controls.kill_requested()
    assert check(Job(job_id="0" * 8, op="host.sleep", args={"n": 1}), ctx) == "paused"


def test_a_kill_sent_while_idle_reaches_no_later_operation(
    home: Path, make_context: MakeContext
) -> None:
    ctx = _cli_context(make_context, ["host.sleep"])
    assert main(["kill"]) == 0
    assert main(["resume"]) == 0
    reply = json.loads(process(Job(job_id="1" * 8, op="host.sleep", args={"n": 1}), ctx))
    assert reply["status"] == "done"


def test_timeout_replies_failed_timeout(make_context: MakeContext) -> None:
    ctx = make_context(enabled=["host.sleep"])
    job = Job(job_id="2" * 8, op="host.sleep", args={"n": 5})
    ready = check(job, ctx)
    assert not isinstance(ready, str)
    short = dataclasses.replace(ready, timeout_s=1)
    outcome = execute(short, ctx)
    assert (outcome.status, outcome.code, outcome.exit_code) == ("failed", "timeout", -9)


_STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


@pytest.fixture
def handlers(state_dir: Path) -> Any:
    # A signal the test process inherited ignored (under nohup, or as a background job) would stay
    # ignored, so each starts from its default here, and goes back to what it was after.
    inherited = {signum: signal.signal(signum, signal.SIG_DFL) for signum in _STOP_SIGNALS}
    controls = Controls(state_dir)
    restore = controls.install_handlers()
    try:
        yield controls
    finally:
        restore()
        for signum, handler in inherited.items():
            signal.signal(signum, handler)


def _signal_self(signum: int) -> None:
    os.kill(os.getpid(), signum)
    time.sleep(0.05)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_first_sigterm_or_sighup_drains(handlers: Controls, signum: int) -> None:
    # SIGHUP is the terminal closing: the host drains rather than dying with a child running.
    assert handlers.may_start()
    _signal_self(signum)
    assert handlers.draining.is_set() and not handlers.may_start()
    assert not handlers.paused() and not handlers.kill_requested()
    # A second one still only drains.
    _signal_self(signum)
    assert not handlers.kill_requested()


def test_drain_lets_the_running_operation_finish(
    handlers: Controls, make_context: MakeContext
) -> None:
    ctx = dataclasses.replace(make_context(enabled=["host.sleep"]), controls=handlers)
    running = _Background(Job(job_id="3" * 8, op="host.sleep", args={"n": 1}), ctx)
    _wait_for_child(ctx)
    _signal_self(signal.SIGTERM)
    reply = running.wait()
    assert (reply["status"], reply["exit_code"]) == ("done", 0)
    assert handlers.draining.is_set()


def test_second_ctrl_c_kills(handlers: Controls, make_context: MakeContext) -> None:
    ctx = dataclasses.replace(make_context(enabled=["host.sleep"]), controls=handlers)
    running = _Background(Job(job_id="4" * 8, op="host.sleep", args={"n": 60}), ctx)
    _wait_for_child(ctx)
    _signal_self(signal.SIGINT)
    assert handlers.draining.is_set() and not handlers.kill_requested()
    _signal_self(signal.SIGINT)
    reply = running.wait()
    assert (reply["status"], reply["code"]) == ("failed", "killed_locally")
    assert handlers.paused()


@pytest.mark.parametrize("verb", ["pause", "kill", "resume"])
def test_each_control_waits_for_the_controls_lock(state_dir: Path, verb: str) -> None:
    """kill writes two files and resume removes them: run at once, each waits for the other, so
    a kill request never stands without the pause flag for a later operation to meet."""
    controls = Controls(state_dir)
    if verb == "resume":
        controls.kill()
    before = (controls.paused(), controls.kill_requested())
    acting = threading.Thread(target=getattr(controls, verb))
    with lock_held(state_dir / ".controls.lock"):
        acting.start()
        acting.join(0.5)
        assert acting.is_alive() and (controls.paused(), controls.kill_requested()) == before
    acting.join(10)
    after = {"pause": (True, False), "kill": (True, True), "resume": (False, False)}[verb]
    assert (controls.paused(), controls.kill_requested()) == after


def _no_locks(*args: Any) -> None:
    raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))


def test_the_controls_work_where_the_filesystem_cannot_lock(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """On a network mount with no lock service, pause, kill and resume go ahead without the lock
    and say so; a policy change refuses, and the file can be edited by hand."""
    monkeypatch.setattr("os.lockf", _no_locks)
    controls = Controls(paths.ensure_dir(paths.dirs().state))
    assert main(["kill"]) == 0
    assert controls.paused() and controls.kill_requested()
    assert "without the controls' lock" in capsys.readouterr().err
    assert main(["resume"]) == 0
    assert not controls.paused() and not controls.kill_requested()
    assert main(["ops", "enable", "host.sleep"]) == 1
    assert "the policy was not changed" in capsys.readouterr().err


class _Turns:
    """Lets two threads take their file steps in a set order. A thread that has finished gives
    up the turns left to it."""

    def __init__(self, order: tuple[str, ...]) -> None:
        self._order = order
        self._next = 0
        self._finished: set[str] = set()
        self._changed = threading.Condition()

    def _skip_finished(self) -> None:
        while self._next < len(self._order) and self._order[self._next] in self._finished:
            self._next += 1

    def take(self, who: str) -> None:
        with self._changed:
            self._skip_finished()
            while self._next < len(self._order) and self._order[self._next] != who:
                assert self._changed.wait(10), "a step never came"
                self._skip_finished()

    def done_one(self) -> None:
        with self._changed:
            self._next += 1
            self._changed.notify_all()

    def finish(self, who: str) -> None:
        with self._changed:
            self._finished.add(who)
            self._changed.notify_all()


def test_no_order_of_a_kill_and_a_resume_leaves_a_kill_request_alone(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the lock, every way the file steps of a kill (three writes) and a resume (two
    removals, a look, perhaps a write) can interleave ends with no kill request, or with one beside
    the pause flag: never one alone, which would kill the next operation."""
    monkeypatch.setattr("os.lockf", _no_locks)
    real = {"_write_flag": _write_flag, "_remove": _remove, "_present": _present}
    turns = _Turns(())

    def stepping(function: Any) -> Any:
        def step(path: Path) -> Any:
            turns.take(threading.current_thread().name)
            try:
                return function(path)
            finally:
                turns.done_one()

        return step

    for name, function in real.items():
        monkeypatch.setattr(f"lucille_host.controls.{name}", stepping(function))
    orders = 0
    for kill_turns in itertools.combinations(range(7), 3):
        order = tuple("kill" if index in kill_turns else "resume" for index in range(7))
        turns = _Turns(order)
        controls = Controls(state_dir)
        for flag in (state_dir / "paused", state_dir / "kill-request"):
            flag.unlink(missing_ok=True)

        def run(verb: str, controls: Controls = controls, turns: _Turns = turns) -> None:
            try:
                getattr(controls, verb)()
            finally:
                turns.finish(verb)

        threads = [threading.Thread(target=run, args=(v,), name=v) for v in ("kill", "resume")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
            assert not thread.is_alive(), order
        assert not controls.kill_requested() or controls.paused(), order
        orders += 1
    assert orders == 35


def test_an_ignored_sighup_stays_ignored(state_dir: Path) -> None:
    """A host started with nohup keeps running when its terminal closes."""
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    controls = Controls(state_dir)
    restore = controls.install_handlers()
    try:
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
        _signal_self(signal.SIGHUP)
        assert not controls.draining.is_set()
    finally:
        restore()
        signal.signal(signal.SIGHUP, previous)


class _WatchedLock:
    """A lock that counts how often it is taken while ``watching`` is set."""

    taken = 0
    watching = False

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        if _WatchedLock.watching:
            _WatchedLock.taken += 1
        return bool(self._inner.acquire(*args, **kwargs))

    def release(self) -> None:
        self._inner.release()

    def locked(self) -> bool:
        return bool(self._inner.locked())

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


def test_the_signal_handler_takes_no_lock(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A handler runs between two steps of the main thread, which may be inside the very lock it
    would take, and would then wait forever. A threading.Event's set() and clear() take one, for
    instance. Every lock made through threading.Lock or threading.RLock while the controls are
    built is watched while the handler runs. A lock made another way (when controls.py is first
    imported, say) is not, and controls.py makes none."""
    plain, reentrant = threading.Lock, threading.RLock
    monkeypatch.setattr("threading.Lock", lambda: _WatchedLock(plain()))
    monkeypatch.setattr("threading.RLock", lambda: _WatchedLock(reentrant()))
    controls = Controls(state_dir)
    monkeypatch.undo()
    controls.draining.set()  # So a SIGINT takes the longest path: it also kills.
    _WatchedLock.taken, _WatchedLock.watching = 0, True
    try:
        controls.on_signal(signal.SIGINT, None)
    finally:
        _WatchedLock.watching = False
    assert _WatchedLock.taken == 0
    assert controls.kill_requested() and controls.paused()
