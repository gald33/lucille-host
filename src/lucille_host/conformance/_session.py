"""One paired host, driven as a user and Lucille would drive it, every program a process of its own.

:class:`Session` starts the local hub (``switchboard serve`` on 127.0.0.1), pairs the installed
``lucille-host`` with :class:`~lucille_host.conformance._lucille.FakeLucille` through it, confirms
the SAS, starts ``run``, and offers one work room. It gives the host a private home, so that its
profile, policy and audit log are the suite's own, and ``--store-file``, so that no keyring is
needed.

The host is paired the way it pairs with Lucille: ``pair begin --hub <loopback url> --test-key
<key>``. The host takes a loopback hub only when its local configuration names it with ``--hub``,
and an answer signed by a test key only on that hub, so this pairs nothing with Lucille's hub.

Every wait has a bound, and :meth:`Session.close` kills every child, whatever happened.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from lucille_host import audit, paths
from lucille_host.conformance import _spawn
from lucille_host.conformance._lucille import FakeLucille, Signer

__all__ = ["WAIT", "Child", "Session", "SessionError"]

# What the hub says on standard error once it listens, with the port it picked.
LISTENING = re.compile(r"Uvicorn running on (http://127\.0\.0\.1:[0-9]+) ")
# The longest any one wait may take, in seconds.
WAIT = 60.0
WORK_ROOM = "conformance-1"


class SessionError(Exception):
    """The session could not be set up, or a wait ran out."""


class Child:
    """A child process whose output streams are each read, a line at a time, on a thread of
    their own."""

    def __init__(self, name: str, process: subprocess.Popen[bytes]) -> None:
        self.name = name
        self.proc = process
        self.out: list[bytes] = []
        self.err: list[bytes] = []
        self._readers = [
            threading.Thread(target=_read, args=(process.stdout, self.out), daemon=True),
            threading.Thread(target=_read, args=(process.stderr, self.err), daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    def write(self, data: bytes) -> None:
        """Write ``data`` on standard input, then close it."""
        stdin = self.proc.stdin
        if stdin is None:
            raise SessionError(f"{self.name} has no standard input")
        with contextlib.suppress(BrokenPipeError):
            stdin.write(data)
        self.close_stdin()

    def close_stdin(self) -> None:
        stdin = self.proc.stdin
        if stdin is not None and not stdin.closed:
            with contextlib.suppress(BrokenPipeError):
                stdin.close()

    def wait(self, timeout: float = WAIT) -> int:
        try:
            code = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            raise SessionError(f"{self.name} did not exit within {timeout} s") from None
        for reader in self._readers:
            reader.join(timeout)
        return code

    def kill(self) -> None:
        self.close_stdin()
        if self.proc.poll() is None:
            self.proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(10)
        for reader in self._readers:
            reader.join(10)

    @property
    def text(self) -> str:
        """Standard output so far, whole lines only."""
        return "".join(line.decode() for line in list(self.out) if line.endswith(b"\n"))

    @property
    def stderr(self) -> str:
        return b"".join(self.err).decode("utf-8", "replace")

    def objects(self, kind: str | None = None, **match: Any) -> list[dict[str, Any]]:
        """The JSON objects written so far, of type ``kind`` if given, with the fields ``match``."""
        found = []
        for line in self.text.splitlines():
            with contextlib.suppress(ValueError):
                value = json.loads(line)
                if (
                    isinstance(value, dict)
                    and (kind is None or value.get("type") == kind)
                    and all(value.get(key) == wanted for key, wanted in match.items())
                ):
                    found.append(value)
        return found

    def describe(self) -> str:
        return (
            f"--- {self.name}: exit {self.proc.poll()}\n"
            f"stdout:\n{b''.join(self.out[-20:]).decode('utf-8', 'replace')}"
            f"stderr:\n{b''.join(self.err[-40:]).decode('utf-8', 'replace')}"
        )


def _read(stream: IO[bytes] | None, into: list[bytes]) -> None:
    if stream is None:
        return
    for line in iter(stream.readline, b""):
        into.append(line)
    stream.close()


def host_env(home: Path) -> dict[str, str]:
    """This process's environment for the host, with ``home`` as its home: no ``SWITCHBOARD_*``
    variable, which the host refuses, and no ``XDG_*`` one, so that the host keeps its files in
    that home and sets its own runtime folder."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("SWITCHBOARD_", "XDG_"))
    }
    env["HOME"] = str(home)
    return env


class Session:
    """A hub, a host paired with a fake Lucille through it, ``run``, and one work room."""

    def __init__(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="lucille-host-conformance-"))
        self.home = self.folder / "home"
        self.home.mkdir(mode=0o700)
        self.workdir = self.folder / "work"
        self.workdir.mkdir(mode=0o700)
        self.env = host_env(self.home)
        self.children: list[Child] = []
        self.room = WORK_ROOM
        self.lucille: FakeLucille | None = None
        self.run: Child | None = None
        self._saved_env: dict[str, str | None] = {}

    # --- set up -------------------------------------------------------------------------------

    def until(self, condition: Callable[[], Any], what: str, *, seconds: float = WAIT) -> Any:
        """Wait until ``condition()`` is true, for at most ``seconds``; returns what it returned."""
        deadline = time.monotonic() + seconds
        while True:
            found = condition()
            if found:
                return found
            if time.monotonic() >= deadline:
                shown = "\n".join(child.describe() for child in self.children)
                raise SessionError(f"{what} never held within {seconds} s\n{shown}")
            time.sleep(0.05)

    def _alive(self, child: Child) -> None:
        if child.proc.poll() is not None:
            raise SessionError(f"{child.name} exited\n{child.describe()}")

    def _isolate_lucille(self) -> None:
        """Lucille's client, in this process, reads no Switchboard variable and finds no signer
        socket: a private runtime folder of its own."""
        runtime = self.folder / "lucille-runtime"
        runtime.mkdir(mode=0o700)
        names = [name for name in os.environ if name.startswith("SWITCHBOARD_")]
        for name in [*names, "XDG_RUNTIME_DIR"]:
            self._saved_env[name] = os.environ.get(name)
        for name in names:
            del os.environ[name]
        os.environ["XDG_RUNTIME_DIR"] = str(runtime)

    def start(self) -> None:
        """Start the hub, pair the host, confirm the SAS, start ``run`` and offer a work room."""
        if os.geteuid() == 0:
            raise SessionError("the host refuses to run as root: run the suite as an ordinary user")
        if not _spawn.SWITCHBOARD_EXE.is_file():
            raise SessionError(
                f"{_spawn.SWITCHBOARD_EXE} is missing: install lucille-host[conformance]"
            )
        if not _spawn.LUCILLE_HOST_EXE.is_file():
            raise SessionError(f"{_spawn.LUCILLE_HOST_EXE} is missing")
        self._isolate_lucille()

        hub = Child(
            "switchboard serve",
            _spawn.hub_process(self.folder / "hub.db", env=self.env, cwd=self.folder),
        )
        self.children.append(hub)

        def listening() -> str | None:
            self._alive(hub)
            found = LISTENING.search(hub.stderr)
            return found.group(1) if found else None

        url = self.until(listening, "the hub listening")
        lucille = FakeLucille(url, self.folder / "lucille")
        self.lucille = lucille

        # `pair begin --json` with a test key, on the loopback hub; the chat service and the
        # username on standard input. Lucille delivers its signed code, then collects the answer.
        signer = Signer()
        pair = self.start_host(
            "pair", "begin", "--json", "--hub", url, "--store-file", f"--test-key={signer.test_key}"
        )
        pair.write(b"telegram\nconformance\n")

        def shown_code() -> str | None:
            if pair.proc.poll() not in (None, 0):
                raise SessionError(f"pair begin failed\n{pair.describe()}")
            found = pair.objects("connect", state="code")
            return str(found[0]["code"]) if found else None

        lucille.deliver(self.until(shown_code, "pair begin showing its connect code"), signer)

        def collected() -> bool:
            if pair.proc.poll() not in (None, 0):
                raise SessionError(f"pair begin failed\n{pair.describe()}")
            return bool(lucille.collect().state == "paired")

        self.until(collected, "Lucille collecting the answer")
        if pair.wait() != 0:
            raise SessionError(f"pair begin failed\n{pair.describe()}")
        shown = pair.objects("pair", state="sas_pending")
        if not shown or shown[-1].get("sas") != lucille.sas():
            raise SessionError(f"the host showed no SAS, or not Lucille's\n{pair.describe()}")

        confirmed = self.host("--json", "pair", "confirm")
        if confirmed.returncode != 0:
            raise SessionError(f"pair confirm failed: {confirmed.stdout}{confirmed.stderr}")

        run = self.start_host("run", "--json-events", "--exit-on-stdin-eof")
        self.run = run

        def ready() -> bool:
            self._alive(run)
            return bool(run.objects("ready"))

        self.until(ready, "run ready")
        lucille.offer(self.room)

        def reading() -> bool:
            self._alive(run)
            return any(
                room.get("state") == "reading"
                for found in run.objects("state")
                for room in found.get("rooms", [])
            )

        self.until(reading, "run reading the work room")

    # --- the host ------------------------------------------------------------------------------

    def start_host(self, *args: str) -> Child:
        child = Child(
            f"lucille-host {' '.join(args)}",
            _spawn.host_process(args, env=self.env, cwd=self.folder, piped=True),
        )
        self.children.append(child)
        return child

    def host(self, *args: str, timeout: float = WAIT) -> subprocess.CompletedProcess[str]:
        """Run a ``lucille-host`` verb on the session's profile to its end."""
        return _spawn.host(args, env=self.env, cwd=self.folder, timeout=timeout)

    def audit_lines(self) -> list[dict[str, Any]]:
        """Every line of the host's audit log, oldest first, across its rotated files."""
        state = paths.dirs(environ=self.env).state
        files = [state / f"{audit.FILE_NAME}.{index}" for index in range(audit.KEEP, 0, -1)]
        files.append(state / audit.FILE_NAME)
        lines: list[dict[str, Any]] = []
        for path in files:
            with contextlib.suppress(FileNotFoundError):
                for raw in path.read_text(encoding="ascii").splitlines():
                    with contextlib.suppress(ValueError):
                        value = json.loads(raw)
                        if isinstance(value, dict):
                            lines.append(value)
        return lines

    # --- tear down -----------------------------------------------------------------------------

    def close(self) -> None:
        if self.run is not None:
            self.run.close_stdin()
            with contextlib.suppress(SessionError):
                self.run.wait(15)
        for child in reversed(self.children):
            child.kill()
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(self.folder, ignore_errors=True)
