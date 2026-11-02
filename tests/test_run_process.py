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

Every wait has a bound, and every child is killed at the end, whatever happened. The children,
the hub's configuration and the waits come from tests/_process.py, which the Mac app's
end-to-end check (``tools/macos/check_app.py pairing``) uses too.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from switchboard.client import Client

from tests._lucille import FakeLucille
from tests._process import LISTENING, SWITCHBOARD, Child, LoopbackHub, child_env, until
from tests._run import body
from tests.spec._support import cli_objects

# The longest line of an event, newline included (spec/CLI.md section 13.2).
LINE_MOST = 16384
WORK_ROOM = "work-1"


def _while_up(child: Child, condition: Callable[[], Any]) -> Callable[[], Any]:
    """``condition``, failing at once, with what ``child`` wrote, if ``child`` has exited."""

    def check() -> Any:
        assert child.proc.poll() is None, child.describe()
        return condition()

    return check


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
    env = child_env(home)
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

        url = until(listening, "the hub listening", children)
        lucille = FakeLucille(LoopbackHub(url), tmp_path / "lucille", connect=Client)

        # `pair --code-stdin --json`: the code on standard input, Lucille collecting the answer.
        code = lucille.start()
        pair = start([*host, "pair", "--code-stdin", "--json", "--hub", url, "--store-file"])
        pair.write(code.encode() + b"\n")

        def collected() -> bool:
            assert pair.proc.poll() in (None, 0), pair.describe()
            return lucille.collect().state == "paired"

        until(collected, "Lucille collecting the answer", children)
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
        until(_while_up(run, lambda: len(run.objects()) >= 2), "ready, then state", children)
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

        until(_while_up(run, reading), "a state with the room read", children)

        # A request for an operation the fresh policy enables, answered.
        sent = body()
        job_id = sent["job_id"]
        lucille.request(WORK_ROOM, sent)
        until(
            _while_up(run, lambda: run.objects("request", job_id=job_id, phase="finished")),
            "the request finished",
            children,
        )
        reply = until(
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
