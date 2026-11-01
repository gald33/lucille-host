"""``lucille-host run`` as a shell supervises it, every program a process of its own.

Every other test of ``run`` drives the command inside the test process, against Switchboard's
in-process test hub, with the user id patched, because a test may run as root and the host refuses
root. This one patches nothing in the host. It starts a real Switchboard hub as a child process
(``switchboard serve``, which the ``conformance`` extra installs beside the interpreter running the
suite) on a loopback port the hub picks; plays Lucille in this process, with the Switchboard
library, against that hub; and runs the command as child processes of the interpreter running the
suite (``python -m lucille_host``), with a private home and ``--store-file``, so that no keyring is
needed: ``pair --code-stdin --json``, ``pair confirm --json``, then ``run --json-events
--exit-on-stdin-eof`` with a standard input whose write end this test holds, as spec/CLI.md section
13.1 says a shell starts it.

It is skipped when the suite runs as root, where the host refuses to start, and on no other
condition: CI runs the suite as an ordinary user.

Every wait has a bound, and every child is killed at the end, whatever happened.
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

import pytest
from switchboard.client import Client
from switchboard.config import ClientConfig

from tests._lucille import FakeLucille
from tests._run import body
from tests.spec._support import cli_objects

# The hub the conformance extra installs, beside the interpreter running the suite.
SWITCHBOARD = Path(sys.executable).parent / "switchboard"
# What the hub says on standard error once it listens, with the port it picked.
LISTENING = re.compile(r"Uvicorn running on (http://127\.0\.0\.1:[0-9]+) ")
# The longest any one wait may take, in seconds.
WAIT = 60.0
# The longest line of an event, newline included (spec/CLI.md section 13.2).
LINE_MOST = 16384
WORK_ROOM = "work-1"


class Child:
    """A child process. Its standard output and standard error are each read, a line at a time,
    on a thread that does nothing else, as spec/CLI.md section 13.1 asks of a shell; its standard
    input is a pipe whose write end this test holds, or nothing."""

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
        """Close this test's write end of standard input, as a shell that quits closes it."""
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


def _until(condition: Callable[[], Any], what: str, children: list[Child]) -> Any:
    """Wait until ``condition()`` is true, for at most :data:`WAIT` seconds; returns what it
    returned. A wait that runs out shows what every child wrote."""
    deadline = time.monotonic() + WAIT
    while True:
        found = condition()
        if found:
            return found
        if time.monotonic() >= deadline:
            shown = "\n".join(child.describe() for child in children)
            raise AssertionError(f"{what} never held within {WAIT} s\n{shown}")
        time.sleep(0.05)


def _while_up(child: Child, condition: Callable[[], Any]) -> Callable[[], Any]:
    """``condition``, failing at once, with what ``child`` wrote, if ``child`` has exited."""

    def check() -> Any:
        assert child.proc.poll() is None, child.describe()
        return condition()

    return check


class LoopbackHub:
    """What :class:`tests._lucille.FakeLucille` asks of a hub, for the one this test started: a
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


def _child_env(home: Path) -> dict[str, str]:
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


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="the host refuses to run as root; CI runs this test as an ordinary user",
)
def test_run_as_separate_processes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Lucille's client, in this process, reads no Switchboard variable and finds no signer.
    for name in list(os.environ):
        if name.startswith("SWITCHBOARD_"):
            monkeypatch.delenv(name)
    lucille_runtime = tmp_path / "lucille-runtime"
    lucille_runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(lucille_runtime))
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    env = _child_env(home)
    host = [sys.executable, "-m", "lucille_host"]
    children: list[Child] = []

    def start(argv: list[str], *, piped: bool = True) -> Child:
        children.append(Child(argv, env=env, cwd=tmp_path, piped=piped))
        return children[-1]

    assert SWITCHBOARD.is_file(), f"{SWITCHBOARD} is missing: install the conformance extra"
    try:
        # The hub, on a port it picks.
        hub = start(
            [
                str(SWITCHBOARD),
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--db",
                str(tmp_path / "hub.db"),
                "--log-level",
                "info",
            ],
            piped=False,
        )

        def listening() -> str | None:
            assert hub.proc.poll() is None, hub.describe()
            found = LISTENING.search(hub.stderr)
            return found.group(1) if found else None

        url = _until(listening, "the hub listening", children)
        lucille = FakeLucille(LoopbackHub(url), tmp_path / "lucille", connect=Client)

        # `pair --code-stdin --json`: the code on standard input, Lucille collecting the answer.
        code = lucille.start()
        pair = start([*host, "pair", "--code-stdin", "--json", "--hub", url, "--store-file"])
        pair.write(code.encode() + b"\n")

        def collected() -> bool:
            assert pair.proc.poll() in (None, 0), pair.describe()
            return lucille.collect().state == "paired"

        _until(collected, "Lucille collecting the answer", children)
        assert pair.wait() == 0, pair.describe()
        said = cli_objects(pair.text)
        assert said[0] == {
            "v": 1,
            "type": "pair_code",
            "name": lucille.name,
            "hub": url,
            "note": "",
        }
        assert said[1] == {
            "v": 1,
            "type": "pair",
            "state": "accepted",
            "name": lucille.name,
            "clock_skewed": False,
        }
        assert {found["state"] for found in said[2:-1]} == {"waiting"}
        shown = said[-1]
        assert {key: shown[key] for key in ("type", "state", "name", "sas")} == {
            "type": "pair",
            "state": "sas_pending",
            "name": lucille.name,
            "sas": lucille.sas(),
        }

        # `pair confirm --json`: the SAS shown is the one Lucille's page shows.
        confirm = start([*host, "--json", "pair", "confirm"], piped=False)
        assert confirm.wait() == 0, confirm.describe()
        assert cli_objects(confirm.text) == [
            {"v": 1, "type": "pair", "state": "confirmed", "name": lucille.name}
        ]

        # `run`, as a shell starts it.
        run = start([*host, "run", "--json-events", "--exit-on-stdin-eof"])
        _until(_while_up(run, lambda: len(run.objects()) >= 2), "ready, then state", children)
        ready, first = run.objects()[:2]
        assert ready["type"] == "ready" and ready["pair"] == lucille.name
        assert first["type"] == "state" and first["pair"] == lucille.name
        assert (first["confirmed"], first["paused"], first["rooms"]) == (True, False, [])

        # A second `run` of the profile, while the first runs: refused, changing nothing.
        second = start([*host, "run", "--json-events", "--exit-on-stdin-eof"])
        assert second.wait() == 21, second.describe()
        assert cli_objects(second.text) == [
            {"v": 1, "type": "stopped", "reason": "already_running"}
        ]
        assert run.proc.poll() is None, run.describe()

        # Lucille offers a work room, and `run` reads it.
        lucille.offer(WORK_ROOM)

        def reading() -> list[dict[str, Any]]:
            return [
                room
                for found in run.objects("state")
                for room in found["rooms"]
                if room["state"] == "reading"
            ]

        _until(_while_up(run, reading), "a state with the room read", children)

        # A request for an operation the fresh policy enables, answered.
        sent = body()
        job_id = sent["job_id"]
        lucille.request(WORK_ROOM, sent)
        _until(
            _while_up(run, lambda: run.objects("request", job_id=job_id, phase="finished")),
            "the request finished",
            children,
        )
        reply = _until(
            _while_up(run, lambda: lucille.reply(WORK_ROOM, job_id)), "the reply read", children
        )
        answer = reply["body"]
        assert (answer["dir"], answer["job_id"], answer["status"]) == ("reply", job_id, "done")
        steps = run.objects("request", job_id=job_id)
        assert [step["phase"] for step in steps] == ["received", "decided", "finished"]
        received, decided, finished = steps
        assert (received["op"], received["root"], received["path"]) == ("host.describe", None, None)
        assert decided["decision"] == "run"
        assert finished["status"] == "done"

        # The end of standard input drains it.
        run.close_stdin()
        assert run.wait() == 0, run.describe()
        lines = list(run.out)
        assert all(line.endswith(b"\n") and len(line) <= LINE_MOST for line in lines)
        events = cli_objects(run.text)
        assert len(events) == len(lines)
        assert events[0] == ready and events[1] == first
        assert events[-1] == {"v": 1, "type": "stopped", "reason": "drained", "cause": "stdin_end"}
        assert [found["type"] for found in events].count("stopped") == 1
        assert "Traceback" not in run.stderr, run.stderr
    finally:
        for child in reversed(children):
            child.kill()
