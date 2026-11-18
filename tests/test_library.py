"""The library entry point: ``library.start`` serves a profile on a thread of its own, as ``run``
does, and ``Host.stop`` drains it, as the first SIGTERM does. For a program that embeds the host
and owns its process, its signals and its main thread."""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from switchboard.testing import hub

from lucille_host import _switchboard, library, link, state
from tests._host import HostEnv
from tests._run import RunHost, body, fast, until


@pytest.fixture
def h() -> Iterator[Any]:
    with hub(start=time.time()) as handle:
        yield handle


# Every host a test started, so that one a failing test left running is drained before its hub
# closes.
STARTED: list[library.Host] = []


def _start(*args: Any, **options: Any) -> library.Host:
    started = library.start(*args, **options)
    STARTED.append(started)
    return started


@pytest.fixture(autouse=True)
def _runs_stopped(h: Any) -> Iterator[None]:
    yield
    for started in STARTED:
        started.stop(timeout=30, raise_error=False)
    STARTED.clear()
    RunHost.stop_all()


@pytest.fixture
def host(host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunHost:
    """A profile paired with a fake Lucille, its SAS confirmed; every link the library opens talks
    to the test hub."""
    fast(monkeypatch)
    paired = RunHost(h, host_env, tmp_path)
    real = _switchboard.open_link

    def open_link(dirs: Any, config: Any, **_: Any) -> _switchboard.Link:
        return real(dirs, config, client_class=paired.client_class, clock=paired.now)

    monkeypatch.setattr("lucille_host._switchboard.open_link", open_link)
    return paired


def test_start_serves_the_profile_and_stop_drains_it(host: RunHost) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    said: list[str] = []
    running = _start(
        host.dirs,
        "keyring",
        say=said.append,
        on_event=lambda kind, fields: events.append((kind, fields)),
    )
    assert running.ready.wait(20), running.error
    assert running.pair == host.name
    assert ("ready", {"host_version": library.__version__, "pair": host.name}) in events
    host.lucille.offer("work")
    until(lambda: host.record().held, 20, "the host taking the room")
    job_id = host.request("work", body())
    reply = host.reply("work", job_id)
    assert reply["body"]["status"] == "done"
    assert running.stop(timeout=30) == "drained"
    assert running.error is None
    assert not running.alive
    assert running.controls.drain_cause == library.STOP_CAUSE
    assert any(line.startswith("running: pair") for line in said)


def test_it_installs_no_signal_handler(host: RunHost, monkeypatch: pytest.MonkeyPatch) -> None:
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    running = _start(host.dirs, "keyring")
    assert running.ready.wait(20), running.error
    assert {sig: signal.getsignal(sig) for sig in before} == before
    assert running.thread is not threading.main_thread()
    assert running.stop(timeout=30) == "drained"
    assert {sig: signal.getsignal(sig) for sig in before} == before


def test_a_second_start_of_the_profile_fails_as_a_second_run_does(host: RunHost) -> None:
    first = _start(host.dirs, "keyring")
    assert first.ready.wait(20), first.error
    second = _start(host.dirs, "keyring")
    with pytest.raises(library.Failed) as raised:
        second.wait(timeout=20)
    assert raised.value.code == "busy"
    assert first.stop(timeout=30) == "drained"


def test_a_store_other_than_the_profile_s_is_refused(host: RunHost) -> None:
    with pytest.raises(state.Refused, match="file"):
        _start(host.dirs, "file")
    with pytest.raises(ValueError):
        _start(host.dirs, "elsewhere")


def test_a_profile_with_no_pair_ends_with_the_reason(
    host_env: HostEnv, h: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast(monkeypatch)
    real = _switchboard.open_link
    monkeypatch.setattr(
        "lucille_host._switchboard.open_link",
        lambda dirs, config, **_: real(dirs, config, client_class=h.client_class()),
    )
    running = _start(host_env.dirs, "keyring")
    with pytest.raises(link.Stopped):
        running.wait(timeout=20)
    assert not running.ready.is_set()
    assert isinstance(running.error, link.Stopped)


def test_stop_before_ready_drains_without_serving(
    host: RunHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()
    real = library.link.open_session

    def slow_open(*args: Any, **options: Any) -> link.Session:
        gate.wait(10)
        return real(*args, **options)

    monkeypatch.setattr("lucille_host.link.open_session", slow_open)
    running = _start(host.dirs, "keyring")
    stopping = threading.Thread(target=running.stop, kwargs={"timeout": 30}, daemon=True)
    stopping.start()
    until(lambda: running.controls.draining.is_set(), 10, "the drain")
    gate.set()
    stopping.join(30)
    assert running.outcome == "drained" and not running.ready.is_set()
    assert running.controls.drain_cause == library.STOP_CAUSE


@pytest.mark.parametrize("cause", ["root", "switchboard_variable"])
def test_a_refused_start_writes_nothing(
    host_env: HostEnv, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    """The startup checks come first, as `run`'s do: refused as root or with a SWITCHBOARD_*
    variable set, start raises before the run lock or anything else is written."""
    if cause == "root":
        monkeypatch.setattr("os.geteuid", lambda: 0)
    else:
        monkeypatch.setenv("SWITCHBOARD_URL", "http://127.0.0.1:1")
    assert not host_env.dirs.state.exists()
    with pytest.raises(state.Refused):
        _start(host_env.dirs, "keyring")
    assert not host_env.dirs.state.exists()
    assert not host_env.dirs.config.exists()


def _host_that_ends_with(host: RunHost, raised: BaseException) -> library.Host:
    """A started host whose ``on_event`` raises ``raised`` once it serves, and so ends with it."""
    seen = threading.Event()

    def on_event(kind: str, _fields: dict[str, Any]) -> None:
        if kind == "ready":
            seen.set()
            raise raised

    running = _start(host.dirs, "keyring", on_event=on_event)
    assert seen.wait(20)
    until(lambda: not running.alive, 20, "the host ending")
    assert running.error is raised
    return running


def test_stop_without_raising_leaves_even_a_system_exit_in_error(
    host: RunHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``raise_error=False`` is for what ended the host, whatever it was: a ``SystemExit`` from a
    callback is left in ``error`` and not raised into the embedding program."""
    ended = SystemExit(3)
    running = _host_that_ends_with(host, ended)
    assert running.stop(timeout=5, raise_error=False) is None
    assert running.error is ended


def test_stop_raises_what_ended_the_host_by_default(
    host: RunHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    ended = SystemExit(3)
    running = _host_that_ends_with(host, ended)
    with pytest.raises(SystemExit):
        running.stop(timeout=5)


def test_stop_without_raising_still_raises_what_interrupts_its_own_wait(
    host: RunHost,
) -> None:
    """Only what ended the host is held back: a ``KeyboardInterrupt`` in the caller's wait reaches
    the caller, even for a host that has already ended with an error of its own."""
    running = _host_that_ends_with(host, RuntimeError("ended"))

    def interrupted(_timeout: float | None = None) -> None:
        raise KeyboardInterrupt

    running.thread.join = interrupted  # type: ignore[method-assign]
    try:
        with pytest.raises(KeyboardInterrupt):
            running.stop(timeout=5, raise_error=False)
    finally:
        del running.thread.join  # the teardown's stop must join the real thread
    assert running.controls.draining.is_set()


def test_stop_from_the_host_s_own_thread_drains_and_raises(
    host: RunHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    raised: list[BaseException] = []
    holder: list[library.Host] = []

    def on_event(kind: str, _fields: dict[str, Any]) -> None:
        if kind == "ready":
            try:
                holder[0].stop(timeout=5)
            except RuntimeError as exc:
                raised.append(exc)

    holder.append(_start(host.dirs, "keyring", on_event=on_event))
    assert holder[0].wait(timeout=30) == "drained"
    assert len(raised) == 1 and "join" in str(raised[0])
