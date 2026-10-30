"""Running ``lucille-host run`` in-process, against Switchboard's test hub and a fake Lucille.

A :class:`RunHost` is a profile paired with a :class:`FakeLucille` through the host's own
:class:`link.Session` (its gate open unless the test says otherwise), and a :class:`loop.Runner`
started on a thread of its own, with no signal handlers: a test drains it by setting the drain
flag, as the first Ctrl-C would. The host's clock is the wall clock, plus an offset a test may
move; the hub's clock stands at the time the hub was made. Long-polls hold for a fraction of a
second rather than 25 s, and the main thread looks around every few hundredths of a second.

Each run must end within its timeout, so that a loop that never stops fails the test instead of
hanging the suite.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from lucille_host import _switchboard, gate, link, loop, paths, policy, rooms, state
from lucille_host.controls import Controls
from lucille_host.journal import Journal
from lucille_host.ops import OPERATIONS
from tests._host import HostEnv
from tests._lucille import FakeLucille

PROTOCOL = "lucille.exec/1"
# How long a long-poll holds, and how often main looks around, in these tests.
LONG_POLL = 0.3
TICK = 0.02


def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop's waits, made short enough for tests."""
    monkeypatch.setattr("lucille_host.link.LONG_POLL", LONG_POLL)
    monkeypatch.setattr("lucille_host.link.BACKOFF_FIRST", 0.05)
    monkeypatch.setattr("lucille_host.link.BACKOFF_MOST", 0.2)
    monkeypatch.setattr("lucille_host.loop.TICK", TICK)
    monkeypatch.setattr("lucille_host.loop.FLUSH", 5.0)
    # Wait for each reader to end, so that no thread outlives its test.
    monkeypatch.setattr("lucille_host.loop.READER_GRACE", 10.0)


def counting(monkeypatch: pytest.MonkeyPatch, owner: type, *names: str) -> collections.Counter[str]:
    """Count the calls of each method ``names`` of ``owner``, from any thread."""
    calls: collections.Counter[str] = collections.Counter()
    lock = threading.Lock()
    for name in names:
        real = getattr(owner, name)

        def counted(*args: Any, _real: Any = real, _name: str = name, **options: Any) -> Any:
            with lock:
                calls[_name] += 1
            return _real(*args, **options)

        monkeypatch.setattr(owner, name, counted)
    return calls


def until(condition: Callable[[], Any], timeout: float = 20.0, what: str = "the condition") -> Any:
    """Wait until ``condition()`` is true; returns what it returned."""
    deadline = time.monotonic() + timeout
    while True:
        found = condition()
        if found:
            return found
        assert time.monotonic() < deadline, f"{what} never held"
        time.sleep(0.02)


def body(
    op: str = "host.describe",
    args: Any = None,
    *,
    job_id: str | None = None,
    not_after: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    """A request body: a fresh job id and ten minutes to run, unless the test says otherwise."""
    return {
        "v": PROTOCOL,
        "dir": "request",
        "job_id": job_id or str(uuid.uuid4()),
        "op": op,
        "args": {} if args is None else args,
        "not_after": int(time.time()) + 600 if not_after is None else not_after,
        **extra,
    }


class RunHost:
    """A profile paired with a fake Lucille, and ``run`` on a thread when started."""

    # Every one made, so that a test that fails mid-run still stops its run (:func:`stop_all`)
    # before the hub it talks to is closed.
    made: list[RunHost] = []

    def __init__(
        self,
        h: Any,
        env: HostEnv,
        folder: Path,
        *,
        open_gate: bool = True,
        client_class: type | None = None,
        config: state.HostConfig | None = None,
    ) -> None:
        self.h = h
        self.env = env
        self.dirs = env.dirs
        self.folder = folder
        self.offset = 0.0
        self.config = config or state.HostConfig()
        self.client_class = client_class or h.client_class()
        self.opened = _switchboard.open_link(
            self.dirs, self.config, client_class=self.client_class, clock=self.now
        )
        self.lucille = FakeLucille(h, folder / "lucille", url=_switchboard.LUCILLE_HUB)
        self.said: list[str] = []
        session = self.session()
        code = self.lucille.start(name="lucille")
        self.name = session.accept(session.check_code(code, self.config))
        self.lucille.collect()
        assert session.resolve(self.name).sas_known
        if open_gate:
            self.confirm()
        self.runner: loop.Runner | None = None
        self.controls = Controls(self.dirs.state)
        self._thread: threading.Thread | None = None
        self._result: dict[str, Any] = {}
        RunHost.made.append(self)

    def now(self) -> float:
        return time.time() + self.offset

    def session(self) -> link.Session:
        return link.Session(self.dirs, self.opened, say=self.said.append)

    def confirm(self) -> None:
        """The SAS shown, as ``pair status`` shows it, and confirmed, as ``y`` or ``pair confirm``
        confirms it; here with no hub call, as the session does both."""
        session = self.session()
        shown = session.show(self.name)
        assert shown is not None and shown[0] == self.lucille.sas()
        assert session.open_gate(self.name) == f"pair {self.name} is confirmed on this computer"

    def enable(self, *ops: str) -> None:
        """``ops enable`` for each of ``ops``."""
        current = policy.load(self.dirs.policy_file)
        enabled = dict(current.enabled)
        for name in ops:
            enabled[name] = OPERATIONS[name].definition_hash
        paths.ensure_dir(self.dirs.config)
        policy.save(
            self.dirs.policy_file, dataclasses.replace(current, enabled=MappingProxyType(enabled))
        )

    # --- the run --------------------------------------------------------------------------

    def start(self) -> loop.Runner:
        self.controls = Controls(self.dirs.state)
        self.runner = loop.Runner(
            self.dirs, self.session(), controls=self.controls, say=self.said.append
        )
        self._result = {}
        runner = self.runner

        def main() -> None:
            try:
                self._result["said"] = runner.run()
            except BaseException as exc:  # noqa: BLE001 - handed to the test thread
                self._result["exc"] = exc

        self._thread = threading.Thread(target=main, daemon=True)
        self._thread.start()
        return runner

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait(self, timeout: float = 30.0) -> str:
        """Wait for the run to end by itself; returns what it said, or raises what it raised."""
        assert self._thread is not None
        self._thread.join(timeout)
        assert not self._thread.is_alive(), f"run did not end within {timeout} s"
        if "exc" in self._result:
            raise self._result["exc"]
        return self._result["said"]

    def stop(self, timeout: float = 30.0) -> str:
        """Drain the run, as the first Ctrl-C does, and wait for it to end."""
        self.controls.draining.set()
        return self.wait(timeout)

    @classmethod
    def stop_all(cls) -> None:
        """Drain every run still going, whatever it raises."""
        for made in cls.made:
            if made.running:
                made.controls.draining.set()
                assert made._thread is not None
                made._thread.join(30)
        cls.made.clear()

    # --- what the host holds and did ------------------------------------------------------

    def record(self) -> rooms.Record:
        return rooms.load(self.dirs.rooms_file)

    def gate(self) -> gate.Gate | None:
        return gate.load(self.dirs.gate_file)

    def journal(self) -> Journal:
        return Journal(self.dirs.journal_file, clock=self.now)

    def audit(self) -> list[dict[str, Any]]:
        path = self.dirs.state / "audit.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def events(self, kind: str, **match: Any) -> list[dict[str, Any]]:
        return [
            line
            for line in self.audit()
            if line["kind"] == kind and all(line.get(k) == v for k, v in match.items())
        ]

    def reading(self) -> list[str]:
        """The rooms the run reads now."""
        runner = self.runner
        if runner is None:
            return []
        return sorted(
            name
            for name, room in runner._rooms.items()
            if room.thread is not None and room.thread.is_alive() and not room.stop.is_set()
        )

    # --- Lucille ---------------------------------------------------------------------------

    def offer(self, room: str, timeout: float = 20.0) -> str:
        """Lucille offers ``room``; returns the host's name for it once the run reads it."""
        before = set(self.record().held)
        self.lucille.offer(room)

        def taken() -> str | None:
            new = set(self.record().held) - before
            if len(new) == 1:
                (name,) = new
                if name in self.reading():
                    return name
            return None

        return until(taken, timeout, f"the host reading {room}")

    def request(self, room: str, sent: dict[str, Any] | None = None, **options: Any) -> str:
        """Lucille posts a request (``sent``, or a fresh ``host.describe``); its job id."""
        sent = body() if sent is None else sent
        self.lucille.request(room, sent, **options)
        return sent["job_id"]

    def reply(self, room: str, job_id: str, timeout: float = 20.0) -> dict[str, Any]:
        """The host's reply for ``job_id`` in Lucille's ``room``, as the message carrying it."""
        return until(lambda: self.lucille.reply(room, job_id), timeout, f"a reply for {job_id}")
