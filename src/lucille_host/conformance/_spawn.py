"""The only module of the conformance suite that starts a process.

It starts two programs, each by its absolute path beside the interpreter running the suite, never
by a name looked up on ``PATH``:

- the local hub, ``switchboard serve``, on 127.0.0.1 and a port it picks, with its database in a
  folder the suite gives (:func:`start_hub`);
- the installed ``lucille-host`` (:func:`start_host`, :func:`run_host`).

The conformance lint (``tools/guards/ast_lint.py``, rule [conformance-argv]) holds this module to
those two programs, named by the constants below.

A :class:`Child` reads its standard output and standard error on threads of their own, so that it
never blocks on a full pipe. Every wait has a bound.
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO, Any

# The programs the suite starts: the ones installed beside the interpreter running it.
SWITCHBOARD_EXE = Path(sys.executable).parent / "switchboard"
LUCILLE_HOST_EXE = Path(sys.executable).parent / "lucille-host"

# What the hub writes on standard error once it listens, with the port it picked.
# The hub logs at info, so that it says where it listens.
_HUB_LOG = ("--log-level", "info")
LISTENING = re.compile(r"Uvicorn running on (http://127\.0\.0\.1:[0-9]+) ")


class Child:
    """A child process whose two output streams are each read, a line at a time, on a thread that
    does nothing else. Its standard input is a pipe whose write end this process holds, or
    nothing."""

    def __init__(self, proc: subprocess.Popen[bytes], argv: Sequence[str]) -> None:
        self.proc = proc
        self.argv = list(argv)
        self.out: list[bytes] = []
        self.err: list[bytes] = []
        self._readers = [
            threading.Thread(target=_read, args=(proc.stdout, self.out), daemon=True),
            threading.Thread(target=_read, args=(proc.stderr, self.err), daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    def write(self, data: bytes) -> None:
        """Write ``data`` on standard input, then close it."""
        stdin = self.proc.stdin
        if stdin is None:
            return
        with contextlib.suppress(BrokenPipeError, OSError):
            stdin.write(data)
        self.close_stdin()

    def close_stdin(self) -> None:
        stdin = self.proc.stdin
        if stdin is not None and not stdin.closed:
            with contextlib.suppress(BrokenPipeError, OSError):
                stdin.close()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def wait(self, timeout: float) -> int | None:
        """The exit code, once the child has exited and both readers reached the end; None if it
        did not exit within ``timeout`` seconds."""
        try:
            code = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None
        for reader in self._readers:
            reader.join(timeout)
        return code

    def stop(self, grace: float = 10.0) -> None:
        """End the child: close its standard input, let it exit within ``grace`` seconds, then
        kill it."""
        self.close_stdin()
        if self.proc.poll() is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(grace)
        if self.proc.poll() is None:
            self.proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(10)
        for reader in self._readers:
            reader.join(10)

    @property
    def text(self) -> str:
        """Standard output so far, whole lines only."""
        return "".join(
            line.decode("utf-8", "replace") for line in list(self.out) if line.endswith(b"\n")
        )

    @property
    def stderr(self) -> str:
        return b"".join(list(self.err)).decode("utf-8", "replace")

    def objects(self) -> list[dict[str, Any]]:
        """The JSON objects written on standard output so far, one per whole line; a line that is
        not a JSON object is left out."""
        found: list[dict[str, Any]] = []
        for line in self.text.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                found.append(value)
        return found

    def describe(self) -> str:
        """What a failure shows of this child: its arguments, exit code and last output."""
        out = b"".join(list(self.out)[-20:]).decode("utf-8", "replace")
        err = b"".join(list(self.err)[-40:]).decode("utf-8", "replace")
        return f"--- {' '.join(self.argv)}: exit {self.proc.poll()}\nstdout:\n{out}stderr:\n{err}"


def _read(stream: IO[bytes], into: list[bytes]) -> None:
    for line in iter(stream.readline, b""):
        into.append(line)
    stream.close()


def programs_missing() -> list[Path]:
    """Which of the two programs is not installed beside this interpreter."""
    return [path for path in (SWITCHBOARD_EXE, LUCILLE_HOST_EXE) if not path.is_file()]


def start_hub(folder: Path, env: Mapping[str, str]) -> Child:
    """Start ``switchboard serve`` on 127.0.0.1, on a port it picks, with its database in
    ``folder``. Its address is in what it writes on standard error (:data:`LISTENING`)."""
    db = str(folder / "hub.db")
    proc = subprocess.Popen(
        [SWITCHBOARD_EXE, "serve", "--host", "127.0.0.1", "--port", "0", "--db", db, *_HUB_LOG],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        cwd=folder,
    )
    return Child(proc, ["switchboard", "serve", "--db", db])


def start_host(
    args: Sequence[str], *, env: Mapping[str, str], cwd: Path, piped: bool = True
) -> Child:
    """Start the installed ``lucille-host`` with ``args``. With ``piped``, its standard input is
    a pipe this process writes to (:meth:`Child.write`); otherwise it is empty."""
    proc = subprocess.Popen(
        [LUCILLE_HOST_EXE, *args],
        stdin=subprocess.PIPE if piped else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        cwd=cwd,
    )
    return Child(proc, ["lucille-host", *args])


def run_host(
    args: Sequence[str], *, env: Mapping[str, str], cwd: Path, timeout: float = 60.0
) -> subprocess.CompletedProcess[str]:
    """Run the installed ``lucille-host`` with ``args`` and an empty standard input, to its end,
    for at most ``timeout`` seconds (``subprocess.TimeoutExpired`` past that)."""
    return subprocess.run(
        [LUCILLE_HOST_EXE, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=dict(env),
        cwd=cwd,
        timeout=timeout,
        check=False,
    )
