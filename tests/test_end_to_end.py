"""The host end to end, through the command a user runs: ``pair``, ``pair confirm``, ``run``, a
work room Lucille offers taken, a request answered, ``status``, ``log`` and ``forget``.

Every verb runs through ``main`` in this process, against Switchboard's in-process test hub, with
a fake Lucille on the other side (``tests/_lucille.py``); ``run`` runs on a thread of its own, as
it would run in a terminal of its own, and stops when ``forget``, run beside it, ends the pair.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from switchboard import pairing
from switchboard.testing import hub

from lucille_host import _switchboard, cli, jobs
from lucille_host.controls import Controls
from tests import _audit_hook
from tests._cli import HostCli
from tests._host import HostEnv
from tests._run import body, fast, until

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)

NO_ROOM = "work room: none: this computer holds no work room"


@pytest.fixture
def h() -> Iterator[Any]:
    with hub(start=time.time()) as handle:
        yield handle


@dataclass
class Flow:
    """A user's terminal: each command's output, in order, and ``run`` on a thread."""

    cli: HostCli
    controls: list[Controls] = field(default_factory=list)
    run: threading.Thread | None = None
    run_rc: list[int] = field(default_factory=list)
    said: list[str] = field(default_factory=list)

    def __call__(self, *argv: str, stdin: str = "") -> Any:
        ran = self.cli.run(*argv, stdin=stdin)
        self.said.append(ran.out)
        return ran

    def start_run(self) -> None:
        def main() -> None:
            self.run_rc.append(cli.main(["run"]))

        self.run = threading.Thread(target=main, daemon=True)
        self.run.start()
        until(lambda: self.controls, what="run started")

    def stop(self) -> None:
        """Drain ``run``, as Ctrl-C would, if it is still going."""
        for made in self.controls:
            made.draining.set()
        if self.run is not None:
            self.run.join(30)


@pytest.fixture
def flow(
    host_env: HostEnv,
    h: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flow]:
    fast(monkeypatch)
    original = _switchboard.open_link
    host = HostCli(h, host_env, tmp_path, capsys, monkeypatch)
    # Each command on the wall clock, as a user's would be.
    monkeypatch.setattr(
        "lucille_host._switchboard.open_link",
        lambda dirs, config, **_: original(dirs, config, client_class=host.client_class),
    )
    made = Flow(host)

    def controls(state_dir: Path) -> Controls:
        made.controls.append(Controls(state_dir))
        return made.controls[-1]

    monkeypatch.setattr("lucille_host.cli.Controls", controls)
    yield made
    host.stopping.set()
    made.stop()


def _line(out: str, label: str) -> str:
    (found,) = [line for line in out.splitlines() if line.startswith(f"{label}: ")]
    return found


def _status_file(env: HostEnv) -> dict[str, Any]:
    path = env.dirs.status_file
    return json.loads(path.read_text()) if path.exists() else {}


def _pair_run_forget(flow: Flow, env: HostEnv, *options: str) -> dict[str, Any]:
    """Pair, confirm, run, take the room Lucille offers, answer a request, forget. Returns what
    the test checks."""
    seen: dict[str, Any] = {"before": flow("status").out}
    lucille = flow.cli.lucille()
    paired = flow.cli.paired(lucille, *options)
    flow.said.append(paired.out)
    seen["paired"] = paired
    seen["confirm"] = flow("pair", "confirm")
    flow.start_run()
    lucille.offer("work-1")
    until(lambda: _status_file(env).get("reading") == ["room-1"], what="the room read")
    request = body()
    lucille.request("work-1", request)
    seen["reply"] = until(lambda: lucille.reply("work-1", request["job_id"]), what="the reply")
    # `run` measures the skew once the post returns, and writes it on its next look around.
    until(lambda: _status_file(env).get("skew") is not None, what="the skew written")
    seen["running"] = flow("status").out
    seen["forget"] = flow("forget")
    assert flow.run is not None
    flow.run.join(30)
    assert not flow.run.is_alive(), "run did not stop when the pair was forgotten"
    seen["after"] = flow("status").out
    seen["log"] = flow("log", "--limit", "200").out
    seen["lucille"] = lucille
    seen["request"] = request
    return seen


def test_end_to_end_pair_run_forget(flow: Flow, host_env: HostEnv) -> None:
    seen = _pair_run_forget(flow, host_env)
    lucille, request = seen["lucille"], seen["request"]
    # Before pairing: no pair, no room, and nothing `run` saw.
    assert _line(seen["before"], "pair") == "pair: none: this computer holds no pair"
    assert _line(seen["before"], "work room") == NO_ROOM
    # `pair` showed the SAS Lucille's page shows, and `pair confirm` confirmed it.
    assert f"the SAS is {lucille.sas()}." in seen["paired"].out.splitlines()[-1]
    assert seen["confirm"].rc == 0 and seen["confirm"].out.startswith("confirmed: ")
    # The request answered: a `done` reply from the host, as a note with no thread.
    reply = seen["reply"]
    assert (reply["type"], reply.get("thread")) == ("note", None)
    assert reply["body"]["dir"] == "reply" and reply["body"]["status"] == "done"
    assert reply["body"]["job_id"] == request["job_id"]
    # While `run` ran: the pair confirmed, the room it took read, the hub reached.
    running = seen["running"]
    assert _line(running, "pair").startswith(f"pair: confirmed: pair {lucille.name} (generation ")
    assert _line(running, "work room").startswith("work room: room-1 (accepted ")
    assert _line(running, "work room").endswith("; read by `run`)")
    assert _line(running, "run").startswith("run: running (pid ")
    assert _line(running, "last hub contact") != "last hub contact: none recorded"
    assert _line(running, "clock").startswith("clock: ok: ")
    # `forget`: the room left, the pair forgotten, Lucille told; and `run` stopped on its own.
    forget = seen["forget"]
    assert forget.rc == 0, forget.text
    expected = (
        f"forgotten: pair {lucille.name} is forgotten on this computer, its work rooms left "
        "first; Lucille was told"
    )
    assert expected in forget.out.splitlines()
    assert flow.run_rc == [0]
    transcript = "".join(flow.said)
    assert f"running: pair {lucille.name}; Ctrl-C stops once the running operation ends" in (
        transcript
    )
    assert (
        f"none: pair {lucille.name} is gone from this computer: another command forgot it; "
        "run stopped"
    ) in transcript
    with pytest.raises(pairing.PairingError) as ended:
        lucille.resolve()
    assert ended.value.state == "ended_by_peer"
    # Afterwards: nothing held, and `run` says it stopped.
    after = seen["after"]
    assert _line(after, "pair") == "pair: none: this computer holds no pair"
    assert _line(after, "work room") == NO_ROOM
    assert _line(after, "run").startswith("run: stopped at ")
    assert flow.cli.held() == [] and flow.cli.gate() is None
    # The log holds the request, its decision, the reply posted, and the pair forgotten.
    lines = [json.loads(line) for line in seen["log"].splitlines()]
    job = [line for line in lines if line.get("job_id") == request["job_id"]]
    assert [line["kind"] for line in job][:2] == ["received", "decision"]
    assert any(line["kind"] == "reply" and line["event"] == "posted" for line in job)
    # The log's fingerprint is that of the reply Lucille read.
    (sent,) = [line for line in job if line["kind"] == "sent"]
    assert sent["sha256"] == hashlib.sha256(jobs.encode(reply["body"])).hexdigest()
    assert any(
        line["kind"] == "pair" and line["event"] == "forgotten" and line["why"] == "forget"
        for line in lines
    )


@pytest.mark.parametrize("options", [(), ("--store-file",)], ids=["keyring", "file"])
def test_no_write_under_home_switchboard(
    options: tuple[str, ...], flow: Flow, host_env: HostEnv
) -> None:
    before = {path for path in host_env.home.rglob("*") if not path.is_dir()}
    with _audit_hook.recording() as events:
        seen = _pair_run_forget(flow, host_env, *options)
    assert seen["forget"].rc == 0 and flow.run_rc == [0]
    assert not (host_env.home / ".switchboard").exists()
    written = {path for path in host_env.home.rglob("*") if not path.is_dir()} - before
    own = (host_env.dirs.config, host_env.dirs.state)
    assert written
    assert all(any(path.is_relative_to(folder) for folder in own) for path in written), written
    assert _audit_hook.spawns(events) == []
    assert [event for event in events if event[0] == "socket.bind"] == []
    if options:
        assert any(path.is_relative_to(host_env.dirs.store_files) for path in written)
        assert host_env.keyring.items == {}
        assert _line(seen["running"], "store") == (
            f"store: files in {host_env.dirs.store_files}, unencrypted: weaker than the OS keyring"
        )
    else:
        assert _line(seen["running"], "store") == "store: the OS keyring"
