"""Programs started as processes of their own, for the checks that drive ``lucille-host`` that way.

A :class:`Child` is a child process whose two output streams are read on threads of their own, as
spec/CLI.md section 13.1 asks of a shell; :class:`LoopbackHub` is what
:class:`tests._lucille.FakeLucille` asks of a hub, for a real Switchboard hub started as a child
(``switchboard serve``, which the ``conformance`` extra installs beside the interpreter running
this). tests/test_run_process.py uses them, and so does the Mac app's end-to-end check
(``tools/macos/check_app.py pairing``), which imports this module only when that check runs.

Every wait has a bound.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from switchboard.client import Client
from switchboard.config import ClientConfig

# The hub the conformance extra installs, beside the interpreter running this.
SWITCHBOARD = Path(sys.executable).parent / "switchboard"
# What the hub says on standard error once it listens, with the port it picked.
LISTENING = re.compile(r"Uvicorn running on (http://127\.0\.0\.1:[0-9]+) ")
# The longest any one wait may take, in seconds, unless a caller gives another bound.
WAIT = 60.0


class Child:
    """A child process. Its standard output and standard error are each read, a line at a time,
    on a thread that does nothing else, as spec/CLI.md section 13.1 asks of a shell; its standard
    input is a pipe whose write end this process holds, or nothing."""

    def __init__(self, argv: list[str], *, env: dict[str, str], cwd: Path, piped: bool) -> None:
        self.argv = argv
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if piped else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
        self.out: list[bytes] = []
        self.err: list[bytes] = []
        self._readers = [
            threading.Thread(target=_read, args=(self.proc.stdout, self.out), daemon=True),
            threading.Thread(target=_read, args=(self.proc.stderr, self.err), daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    def write(self, data: bytes) -> None:
        """Write ``data`` on standard input, then close it; a child that already exited is
        not written to (spec/CLI.md section 7: a shell ignores a broken pipe)."""
        stdin = self.proc.stdin
        assert stdin is not None
        with contextlib.suppress(BrokenPipeError):
            stdin.write(data)
        self.close_stdin()

    def close_stdin(self) -> None:
        """Close this process's write end of standard input, as a shell that quits closes it."""
        stdin = self.proc.stdin
        if stdin is not None and not stdin.closed:
            with contextlib.suppress(BrokenPipeError):
                stdin.close()

    def wait(self, timeout: float = WAIT) -> int:
        """Wait for the child to exit, then for both readers to reach the end; its exit code."""
        try:
            code = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError(f"it did not exit within {timeout} s\n{self.describe()}") from None
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
        """The objects written so far, of type ``kind`` if given, with the fields ``match``."""
        return [
            found
            for found in (json.loads(line) for line in self.text.splitlines())
            if (kind is None or found["type"] == kind)
            and all(found.get(key) == value for key, value in match.items())
        ]

    def describe(self) -> str:
        """What a failing assertion shows of this child."""
        return (
            f"--- {' '.join(self.argv[1:])}: exit {self.proc.poll()}\n"
            f"stdout:\n{b''.join(self.out[-20:]).decode('utf-8', 'replace')}"
            f"stderr:\n{b''.join(self.err[-40:]).decode('utf-8', 'replace')}"
        )


def _read(stream: IO[bytes], into: list[bytes]) -> None:
    for line in iter(stream.readline, b""):
        into.append(line)
    stream.close()


def until(
    condition: Callable[[], Any], what: str, children: list[Child], *, seconds: float = WAIT
) -> Any:
    """Wait until ``condition()`` is true, for at most ``seconds``; returns what it returned. A
    wait that runs out shows what every child wrote."""
    deadline = time.monotonic() + seconds
    while True:
        found = condition()
        if found:
            return found
        if time.monotonic() >= deadline:
            shown = "\n".join(child.describe() for child in children)
            raise AssertionError(f"{what} never held within {seconds} s\n{shown}")
        time.sleep(0.05)


class LoopbackHub:
    """What :class:`tests._lucille.FakeLucille` asks of a hub, for one started as a child: a
    configuration naming it, the library's own client, and the wall clock. Lucille's client keeps
    nothing on disk (no peer log, no stash)."""

    def __init__(self, url: str) -> None:
        self.url = url

    @property
    def now(self) -> float:
        return time.time()

    def client_class(self) -> type[Client]:
        return Client

    def client_config(self, *, workspace: str, agent_id: str) -> ClientConfig:
        return ClientConfig(
            url=self.url,
            url_source="explicit",
            token=None,
            workspace=workspace,
            workspace_source="explicit",
            agent_id=agent_id,
            peer_log="",
            stash_db="",
        )


def child_env(home: Path) -> dict[str, str]:
    """This process's environment, for a child, with ``home`` as its home: no ``SWITCHBOARD_*``
    variable, which the host refuses (spec/CLI.md section 13.3, exit 24), and no ``XDG_*`` one,
    so that the host keeps its files in that home and sets its own runtime folder."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("SWITCHBOARD_", "XDG_"))
    }
    env["HOME"] = str(home)
    return env
