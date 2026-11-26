"""Driving the ``lucille-host`` command in-process against Switchboard's test hub.

The command builds its stores and its clients itself (``_switchboard.open_link``); here that is
replaced by the same function given the test hub's client class and clock, so that every pairing
call the command makes reaches the in-process hub, on the hub's clock. The wait for Lucille to
collect the answer resolves every 0.2 s rather than every 5 s, and a Connect request's drop room is
read every 0.05 s.

Every pairing is a Connect (decisions/0002 §11.4): ``pair begin`` keeps a request, and the fake
Lucille delivers a pairing code into its drop room, signed by a throwaway key that stands in for
Lucille's (:attr:`HostCli.signer`, pinned beside Lucille's own).

Each run is made on a thread of its own and must end within its timeout, so that a wait that
never ends fails the test instead of hanging the run: a run that is still waiting at its timeout,
or when the test ends, is stopped as Ctrl-C would stop it, the next time it listens. A fake
Lucille that collects in the background stands in for Lucille's backend collecting the answer.
"""

from __future__ import annotations

import contextlib
import io
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lucille_host import _switchboard, connect, connecting, gate, paths, policy, state
from lucille_host.cli import main
from tests._host import HostEnv
from tests._lucille import FakeLucille, Signer

# How often the command resolves while it waits, in these tests.
RESOLVE_EVERY = 0.2
# How often ``pair begin`` reads the drop room, in these tests.
POLL_EVERY = 0.05
# What ``pair begin`` reads on standard input: the chat service, then the user's username.
STDIN = "telegram\nada\n"


@dataclass(frozen=True)
class Ran:
    rc: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return self.out + self.err


class Running:
    """A run of the command on a thread of its own."""

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def __init__(self, cli: HostCli, argv: tuple[str, ...]) -> None:
        self._cli = cli
        self._argv = argv
        self._result: dict[str, Any] = {}
        self._thread = threading.Thread(target=self._main, daemon=True)
        self._thread.start()

    def _main(self) -> None:
        try:
            self._result["rc"] = main(list(self._argv))
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            self._result["exc"] = exc

    def wait(self, timeout: float = 60.0) -> Ran:
        self._thread.join(timeout)
        if self._thread.is_alive():
            self._cli.stopping.set()
            self._thread.join(10)
            raise AssertionError(f"lucille-host {self._argv} did not end in {timeout} s")
        if "exc" in self._result:
            raise self._result["exc"]
        out, err = self._cli.capsys.readouterr()
        return Ran(self._result["rc"], out, err)


class HostCli:
    def __init__(
        self,
        h: Any,
        env: HostEnv,
        folder: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.h = h
        self.env = env
        self.dirs = env.dirs
        self.folder = folder
        self.capsys = capsys
        self.monkeypatch = monkeypatch
        # The client class every command builds its clients from; a test may change it.
        self.client_class: type = h.client_class()
        real = _switchboard.open_link

        def open_link(dirs: Any, config: Any, **_: Any) -> _switchboard.Link:
            return real(dirs, config, client_class=self.client_class, clock=lambda: h.now)

        monkeypatch.setattr("lucille_host._switchboard.open_link", open_link)
        monkeypatch.setattr("lucille_host.link.RESOLVE_EVERY", RESOLVE_EVERY)
        # How many times a command has listened on the pair channel: a wait that has listened
        # has resolved at least once before.
        self.listens = 0
        # Set when a run outlived its test or its timeout: it stops at its next listen.
        self.stopping = threading.Event()
        listen = _switchboard.PairNotes.wait

        def counted(notes: _switchboard.PairNotes, **options: Any) -> Any:
            if self.stopping.is_set():
                raise KeyboardInterrupt
            self.listens += 1
            return listen(notes, **options)

        monkeypatch.setattr("lucille_host._switchboard.PairNotes.wait", counted)
        self._lucilles = 0
        # Lucille's signing key, for these tests: a throwaway one, pinned beside Lucille's own.
        self.signer = Signer()
        monkeypatch.setattr(
            "lucille_host._switchboard.LUCILLE_SIGNING_KEYS",
            {**_switchboard.LUCILLE_SIGNING_KEYS, **self.signer.keys},
        )
        monkeypatch.setattr("lucille_host.connecting.POLL_EVERY", POLL_EVERY)
        poll = connecting.poll

        def stoppable(*args: Any, **kwargs: Any) -> Any:
            # A wait the test stopped ends as Ctrl-C ends it.
            if self.stopping.is_set():
                raise KeyboardInterrupt
            return poll(*args, **kwargs)

        monkeypatch.setattr("lucille_host.connecting.poll", stoppable)

    def start(self, *argv: str, stdin: Any = "") -> Running:
        """Run the command on a thread; ``stdin`` is its input, or an object to read it from."""
        source = io.StringIO(stdin) if isinstance(stdin, str) else stdin
        self.monkeypatch.setattr("sys.stdin", source)
        return Running(self, argv)

    def run(self, *argv: str, stdin: Any = "", timeout: float = 60.0) -> Ran:
        return self.start(*argv, stdin=stdin).wait(timeout)

    def lucille(self, **options: Any) -> FakeLucille:
        """A fake Lucille whose codes name Lucille's hub, unless ``url`` names another."""
        self._lucilles += 1
        options.setdefault("url", _switchboard.LUCILLE_HUB)
        return FakeLucille(self.h, self.folder / f"lucille-{self._lucilles}", **options)

    def gate(self) -> gate.Gate | None:
        return gate.load(self.dirs.gate_file)

    def held(self) -> list[str]:
        """The pairs the profile's store holds, read without the command."""
        link = _switchboard.open_link(self.dirs, state.load_host_config(self.dirs))
        return link.names()

    def audit(self) -> str:
        path = self.dirs.state / "audit.log"
        return path.read_text() if path.exists() else ""

    def policy(self) -> policy.Policy:
        return policy.load(self.dirs.policy_file)

    def _connect_file(self, profile: str | None) -> Path:
        return self.dirs.connect_file if profile is None else paths.dirs(profile).connect_file

    def request(self, profile: str | None = None) -> connecting.Request | None:
        """The Connect request the profile keeps, read without the command."""
        return connecting.load(self._connect_file(profile))

    def waiting(self, running: Running, profile: str | None = None) -> connecting.Request | None:
        """The request ``running`` keeps, once it keeps one; none if it ended before it did."""
        until(lambda: self._connect_file(profile).exists() or not running.alive)
        return self.request(profile)

    def begin(
        self,
        lucille: FakeLucille,
        *argv: str,
        stdin: Any = STDIN,
        profile: str | None = None,
        **delivery: Any,
    ) -> Running:
        """``pair begin *argv``, with Lucille delivering a pairing code into the request's drop
        room once the request is kept (``delivery`` goes to :meth:`FakeLucille.deliver`). Lucille
        does not collect the answer here: a test wraps the wait in :func:`collecting` for that.
        ``stdin`` is what the command reads, the service and the username first; ``profile``
        names a profile other than the default. A run that ends before it keeps a request is left
        to end, with nothing delivered."""
        before = () if profile is None else ("--profile", profile)
        running = self.start(*before, "pair", "begin", *argv, stdin=stdin)
        request = self.waiting(running, profile)
        if request is not None:
            lucille.deliver(connect.shown(request.code), self.signer, **delivery)
        return running

    def connect(
        self,
        lucille: FakeLucille,
        *argv: str,
        stdin: Any = STDIN,
        profile: str | None = None,
        timeout: float = 60.0,
        **delivery: Any,
    ) -> Ran:
        """:meth:`begin`, with Lucille collecting the answer, run to its end."""
        running = self.begin(lucille, *argv, stdin=stdin, profile=profile, **delivery)
        with collecting(lucille):
            return running.wait(timeout)

    def paired(
        self, lucille: FakeLucille, *options: str, profile: str | None = None, **delivery: Any
    ) -> Ran:
        """``pair begin`` with Lucille answering and collecting: the SAS is shown, and waits to be
        confirmed."""
        ran = self.connect(lucille, *options, profile=profile, **delivery)
        assert ran.rc == 0, ran.text
        return ran

    def interrupted(self, lucille: FakeLucille, *options: str, **delivery: Any) -> Ran:
        """``pair begin``, with Lucille's code delivered and answered, stopped by Ctrl-C while it
        waits for Lucille to collect."""

        def interrupt(*_: Any, **__: Any) -> Any:
            raise KeyboardInterrupt

        with self.monkeypatch.context() as patch:
            patch.setattr("lucille_host._switchboard.PairNotes.wait", interrupt)
            ran = self.begin(lucille, *options, **delivery).wait()
        assert ran.rc == 1 and "interrupted:" in ran.err, ran.text
        return ran


@contextlib.contextmanager
def collecting(lucille: FakeLucille, *, every: float = 0.05) -> Iterator[None]:
    """Lucille collects the answer, in the background, as soon as there is one: once it has
    started a pairing, which with ``pair begin`` is once it has delivered its code."""
    stop = threading.Event()

    def collect() -> None:
        while not stop.is_set():
            if lucille.name is not None and lucille.collect().state == "paired":
                return
            stop.wait(every)

    thread = threading.Thread(target=collect, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(30)


def until(condition: Callable[[], bool], timeout: float = 30.0) -> None:
    """Wait until ``condition()`` holds."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "the condition never held"
        time.sleep(0.02)
