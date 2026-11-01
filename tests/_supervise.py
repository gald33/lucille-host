"""Supervising ``lucille-host run`` through the command, in-process, as a shell would.

A :class:`Supervised` is ``lucille-host run`` started by :func:`lucille_host.cli.main` on a thread
of its own, beside a :class:`tests._run.RunHost` that paired the profile with a fake Lucille. Its
standard output and standard error are :class:`Lines`, which keep what is written, from any thread,
and have no file descriptor. Its standard input is a pipe whose write end the test holds, as a
shell holds it, unless the test asks for none. The command's stores reach the test hub, on the wall
clock. Each run must end within its timeout; :meth:`Supervised.stop_all` ends the ones a failing
test left going, by closing their standard input and draining them.
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from lucille_host import _switchboard, cli, paths, policy
from lucille_host.controls import Controls
from tests._run import RunHost, until


class Lines(io.TextIOBase):
    """A text stream that keeps what any thread writes to it, and has no file descriptor."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._parts: list[str] = []

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        with self._lock:
            self._parts.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    @property
    def text(self) -> str:
        with self._lock:
            return "".join(self._parts)

    def objects(self) -> list[dict[str, Any]]:
        """Every whole line, parsed: a line still being written is left out."""
        whole = self.text.rpartition("\n")[0]
        return [json.loads(line) for line in whole.splitlines()]


class Piped(Lines):
    """A standard output with a real pipe behind it, as a shell gives one: what is written on its
    descriptor is read back, on a thread, as bytes. :meth:`end` closes the write end once the
    command is done, and waits for the reader."""

    def __init__(self) -> None:
        super().__init__()
        self._read, self._write = os.pipe()
        self._data = bytearray()
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def fileno(self) -> int:
        return self._write

    def _drain(self) -> None:
        try:
            while data := os.read(self._read, 65536):
                with self._lock:
                    self._data += data
        finally:
            os.close(self._read)

    @property
    def raw(self) -> bytes:
        with self._lock:
            return bytes(self._data)

    @property
    def text(self) -> str:
        return self.raw.decode("utf-8", "replace") + super().text

    def end(self) -> None:
        if self._write >= 0:
            os.close(self._write)
            self._write = -1
        self._reader.join(10)


class Supervised:
    """``lucille-host <argv>`` on a thread, with its own standard streams."""

    made: list[Supervised] = []

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        argv: Sequence[str],
        *,
        host: RunHost | None = None,
        client_class: type | None = None,
        stdin: str = "pipe",
        out: Lines | None = None,
        nonblocking: bool = False,
    ) -> None:
        self._host = host
        self.argv = list(argv)
        self.out = Lines() if out is None else out
        self.err = Lines()
        self.controls: list[Controls] = []
        real = _switchboard.open_link
        if host is not None and client_class is None:
            client_class = host.client_class
        if client_class is not None:
            chosen = client_class
            monkeypatch.setattr(
                "lucille_host._switchboard.open_link",
                lambda dirs, config, **_: real(dirs, config, client_class=chosen),
            )
        made = self.controls

        def controls(state_dir: Path) -> Controls:
            made.append(Controls(state_dir))
            return made[-1]

        monkeypatch.setattr("lucille_host.cli.Controls", controls)
        self._write: int | None = None
        source: Any
        if stdin == "pipe":
            read, self._write = os.pipe()
            if nonblocking:
                os.set_blocking(read, False)
            source = os.fdopen(read, "rb")
        elif stdin == "closed":
            # No file descriptor behind it: a standard input that ended before `run` started.
            source = io.StringIO("")
        else:
            raise ValueError(stdin)
        self.stdin = source
        monkeypatch.setattr("sys.stdin", source)
        monkeypatch.setattr("sys.stdout", self.out)
        monkeypatch.setattr("sys.stderr", self.err)
        self._result: dict[str, Any] = {}
        self._thread = threading.Thread(target=self._main, daemon=True)
        Supervised.made.append(self)
        self._thread.start()

    def _main(self) -> None:
        try:
            self._result["rc"] = cli.main(self.argv)
        except SystemExit as exc:
            self._result["rc"] = exc.code
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            self._result["exc"] = exc

    @property
    def host(self) -> RunHost:
        assert self._host is not None, "this run has no paired host beside it"
        return self._host

    # --- what it wrote --------------------------------------------------------------------

    def objects(self, kind: str | None = None, **match: Any) -> list[dict[str, Any]]:
        return [
            found
            for found in self.out.objects()
            if (kind is None or found["type"] == kind)
            and all(found.get(key) == value for key, value in match.items())
        ]

    def requests(self, job_id: str) -> list[dict[str, Any]]:
        """The ``request`` objects of one job, in the order they were written."""
        return self.objects("request", job_id=job_id)

    def state(self) -> dict[str, Any] | None:
        """The latest ``state`` object."""
        found = self.objects("state")
        return found[-1] if found else None

    def ready(self, timeout: float = 20.0) -> dict[str, Any]:
        found = until(lambda: self.objects("ready"), timeout, "ready")
        return found[0]

    def offer(self, room: str, timeout: float = 20.0) -> str:
        """Lucille offers ``room``; returns the host's name for it once a ``state`` says it is
        read."""
        before = {found["name"] for found in (self.state() or {}).get("rooms", [])}
        self.host.lucille.offer(room)

        def reading() -> str | None:
            latest = self.state()
            names = {
                found["name"]
                for found in (latest or {}).get("rooms", [])
                if found["state"] == "reading"
            }
            new = names - before
            return next(iter(new)) if len(new) == 1 else None

        return until(reading, timeout, f"a state reading {room}")

    # --- standard input and the end ---------------------------------------------------------

    @property
    def stdin_write_end(self) -> int:
        assert self._write is not None
        return self._write

    def close_stdin(self) -> None:
        """Close the test's write end of standard input, as a shell that quits closes it."""
        if self._write is not None:
            os.close(self._write)
            self._write = None

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def wait(self, timeout: float = 60.0) -> int:
        """Wait for the command to end; its exit code, or what it raised."""
        self._thread.join(timeout)
        assert not self._thread.is_alive(), f"lucille-host {self.argv} did not end in {timeout} s"
        if "exc" in self._result:
            raise self._result["exc"]
        return int(self._result["rc"])

    def drain(self) -> None:
        """Drain it, as the first Ctrl-C does."""
        for made in self.controls:
            made.draining.set()

    @classmethod
    def stop_all(cls) -> None:
        for made in cls.made:
            made.close_stdin()
            deadline = time.monotonic() + 30
            while made._thread.is_alive() and time.monotonic() < deadline:
                made.drain()
                made._thread.join(0.1)
            if isinstance(made.stdin, io.BufferedReader):
                made.stdin.close()
        cls.made.clear()


def folder(dirs: paths.Dirs, alias: str, path: Path) -> None:
    """``roots add <alias> <path>``, written straight into the policy."""
    current = policy.load(dirs.policy_file)
    roots = dict(current.roots)
    roots[alias] = policy.Root(path=str(path))
    paths.ensure_dir(dirs.config)
    policy.save(
        dirs.policy_file,
        policy.Policy(
            enabled=current.enabled,
            roots=MappingProxyType(roots),
            log_results=current.log_results,
        ),
    )


def strings(value: Any) -> list[str]:
    """Every string inside ``value``, object keys included."""
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            found += [key, *strings(item)]
    elif isinstance(value, list):
        for item in value:
            found += strings(item)
    return found
