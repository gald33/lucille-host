"""The host as a library: serve a profile from a program that owns its own process.

``lucille-host run`` (``cli.py``) owns the process it runs in: its main thread, its signal handlers,
standard input and output. A program that embeds the host owns those itself, so :func:`start` takes
none of them. It runs ``run``'s start checks, in ``run``'s order (spec/CLI.md section 13.3), and
then the same runner (``loop.Runner``), all on a thread of its own. The runner installs signal
handlers only on the main thread (``loop.Runner.serve``), so it installs none here, and the
embedding program's stay as they were. :meth:`Host.stop` drains, as the first SIGTERM does: nothing
new starts, a running operation finishes (within its time limit), and the rooms are left as a
drained ``run`` leaves them. The drain's cause is :data:`STOP_CAUSE`.

What it does not change, said plainly: the host is the same host, with the same files under
``dirs``, the same lock (one ``run`` of a profile at a time: a second start of a profile that is
being served, by ``run`` or by this, fails with :class:`Failed` ``busy``), and the same start
checks, the refusal to run as root and with any ``SWITCHBOARD_*`` variable set among them
(``state.startup_checks``). Pairing is not here: it is the ``link.Session`` the ``pair`` verb uses.

Nothing here knows which program embeds it. ``say``, ``warn`` and ``on_event`` are how that
program hears what ``run`` prints, warns of, and writes as ``run --json-events`` objects; each is
called on one of the host's threads, and anything one of them raises stops the host (it fails
closed), as a broken standard output stops ``run``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Final

from lucille_host import __version__, link, loop, paths
from lucille_host import state as host_state
from lucille_host.controls import Controls

__all__ = [
    "STOP_CAUSE",
    "Failed",
    "Host",
    "__version__",
    "start",
]

# The drain cause :meth:`Host.stop` gives, as a signal gives ``signal``.
STOP_CAUSE: Final = "stop"

Failed = loop.Failed


def _quiet(_line: str) -> None:
    """Nobody is told."""


class Host:
    """A profile the host serves on a thread of its own, from :func:`start` until it ends."""

    def __init__(
        self,
        dirs: paths.Dirs,
        config: host_state.HostConfig,
        *,
        say: Callable[[str], None],
        warn: Callable[[str], None],
        on_event: Callable[[str, dict[str, Any]], None] | None,
        allow_downgrade: bool,
    ) -> None:
        self.dirs = dirs
        self.config = config
        self.controls = Controls(dirs.state)
        # Set once the host serves its pair (``run``'s ``ready``); ``pair`` is then its name.
        self.ready = threading.Event()
        self.pair: str | None = None
        # How it ended, once it did: ``drained``, ``forgotten`` or ``ended`` (as ``run``'s
        # ``stopped`` reasons), or None with ``error`` set; and the line ``run`` would print.
        self.outcome: str | None = None
        self.said: str | None = None
        self.error: BaseException | None = None
        self._say = say
        self._warn = warn
        self._on_event = on_event
        self._allow_downgrade = allow_downgrade
        self.thread = threading.Thread(target=self._main, name="lucille-host", daemon=True)

    @property
    def alive(self) -> bool:
        return self.thread.is_alive()

    def _tell(self, kind: str, fields: dict[str, Any]) -> None:
        if kind == "ready":
            self.pair = fields.get("pair")
            self.ready.set()
        if self._on_event is not None:
            self._on_event(kind, fields)

    def _main(self) -> None:
        try:
            with loop.one_run(self.dirs.state):
                if self.controls.draining.is_set():
                    self.outcome, self.said = "drained", "drained: run stopped"
                    return
                loop.check_version(self.dirs, allow=self._allow_downgrade, warn=self._warn)
                session = link.open_session(self.dirs, self.config, say=self._say, warn=self._warn)
                runner = loop.Runner(
                    self.dirs, session, controls=self.controls, say=self._say, tell=self._tell
                )
                self.said = runner.serve()
                self.outcome = runner.outcome or "drained"
        except BaseException as exc:  # noqa: BLE001 - kept for the caller, who owns the process
            self.error = exc

    def wait(self, timeout: float | None = None) -> str:
        """Wait for the host to end; returns how it ended, or raises what ended it. Raises
        ``TimeoutError`` when it is still running after ``timeout`` seconds."""
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise TimeoutError(f"the host still runs after {timeout} s")
        if self.error is not None:
            raise self.error
        assert self.outcome is not None
        return self.outcome

    def stop(self, timeout: float | None = None, *, raise_error: bool = True) -> str | None:
        """Drain, as the first SIGTERM does, and wait for the host to end (see :meth:`wait`).

        A running operation is let finish: the wait can last as long as its time limit. With
        ``raise_error`` false, what ended the host is left in ``error`` and None is returned for
        it."""
        self.controls.drain(STOP_CAUSE)
        try:
            return self.wait(timeout)
        except Exception:
            if raise_error or self.thread.is_alive():
                raise
            return None


def start(
    dirs: paths.Dirs,
    store: str,
    *,
    say: Callable[[str], None] = _quiet,
    warn: Callable[[str], None] = _quiet,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    allow_downgrade: bool = False,
) -> Host:
    """Serve the profile at ``dirs`` on a thread of its own, and return it running.

    ``store`` is where the profile keeps its pair, ``keyring`` or ``file`` (``state.STORE_KINDS``);
    it must be the one the profile's ``config/host.json`` names, which pairing writes, and a
    profile with none names ``keyring``. A store that is not one of those raises ``ValueError``,
    and one other than the profile's raises ``state.Refused``, both before anything starts.
    Everything after that, the start checks included, happens on the host's thread: their
    failures end it, and :meth:`Host.wait` or :meth:`Host.stop` raises them, as ``run`` reports
    them in its ``stopped`` object.
    """
    if store not in host_state.STORE_KINDS:
        raise ValueError(f"a store is one of {', '.join(host_state.STORE_KINDS)}")
    config = host_state.load_host_config(dirs)
    if config.store != store:
        raise host_state.Refused(
            f"this profile keeps its pair in its {config.store} store, not its {store} store; "
            "pair it again with that store to use it"
        )
    host = Host(
        dirs,
        config,
        say=say,
        warn=warn,
        on_event=on_event,
        allow_downgrade=allow_downgrade,
    )
    host.thread.start()
    return host
