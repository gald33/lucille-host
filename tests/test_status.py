"""``lucille-host status``, ``log`` and ``forget`` (spec/PROTOCOL.md sections 9 and 16).

The commands run in-process against Switchboard's test hub, with a fake Lucille on the other
side: a profile paired through the ``pair`` verbs (``tests/_cli.py``), or paired and running
``run`` on a thread (``tests/_run.py``), each command then run through ``main`` as a user would.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from switchboard import pairing, writekey
from switchboard.testing import hub

from lucille_host import __version__, _switchboard, audit, link, loop, paths, rooms, state
from lucille_host.audit import AuditLog
from lucille_host.cli import main
from tests._cli import HostCli, collecting
from tests._host import HostEnv
from tests._run import RunHost, body, fast, until

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)

LOOPBACK = "http://127.0.0.1:8765"
NO_ROOM = "work room: none: this computer holds no work room"


@pytest.fixture
def h() -> Iterator[Any]:
    with hub(start=time.time()) as handle:
        yield handle


@pytest.fixture(autouse=True)
def _runs_stopped(h: Any) -> Iterator[None]:
    """Every run a test started is drained before its hub closes, even when the test fails."""
    yield
    RunHost.stop_all()


@pytest.fixture
def cli(
    host_env: HostEnv,
    h: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[HostCli]:
    host = HostCli(h, host_env, tmp_path, capsys, monkeypatch)
    yield host
    host.stopping.set()


@pytest.fixture
def host(host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunHost:
    fast(monkeypatch)
    return RunHost(h, host_env, tmp_path)


@dataclass(frozen=True)
class Ran:
    rc: int
    out: str
    err: str

    @property
    def lines(self) -> list[str]:
        return self.out.splitlines()


class Commands:
    """The command, run as a user would run it beside a :class:`RunHost`: its stores reach the
    test hub through ``client_class`` (the hub's own unless a test changes it), on the wall
    clock."""

    def __init__(
        self, h: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.capsys = capsys
        self.client_class: type = h.client_class()
        real = _switchboard.open_link

        def open_link(dirs: Any, config: Any, **_: Any) -> _switchboard.Link:
            return real(dirs, config, client_class=self.client_class)

        monkeypatch.setattr("lucille_host._switchboard.open_link", open_link)

    def __call__(self, *argv: str) -> Ran:
        self.capsys.readouterr()
        rc = main(list(argv))
        out, err = self.capsys.readouterr()
        return Ran(rc, out, err)


@pytest.fixture
def command(
    h: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Commands:
    return Commands(h, monkeypatch, capsys)


class NoHub:
    """A client class that refuses to be built: any hub call fails the command that makes it."""

    built: list[Any] = []

    def __init__(self, config: Any, **_: Any) -> None:
        NoHub.built.append(config)
        raise AssertionError("a client was built")


class Down:
    """A client class whose every call finds the hub out of reach."""

    def __new__(cls, h: Any) -> type:
        class Unreachable(h.client_class()):  # type: ignore[misc]
            def _call(self, *_: Any, **__: Any) -> Any:
                raise httpx.ConnectError("the hub is out of reach")

        return Unreachable


def _line(out: str, label: str) -> str:
    """The one line of ``out`` with this label."""
    (found,) = [line for line in out.splitlines() if line.startswith(f"{label}: ")]
    return found


def _status(host: RunHost) -> dict[str, Any]:
    return json.loads(host.dirs.status_file.read_text())


# --- status ---------------------------------------------------------------------------------------


def test_status_shows_everything(host: RunHost, command: Commands, tmp_path: Path) -> None:
    state.save_host_config(host.dirs, state.HostConfig(hub=LOOPBACK))
    host.enable("host.sleep")
    folder = tmp_path / "docs"
    folder.mkdir()
    assert command("roots", "add", "docs", str(folder)).rc == 0
    # Before `run` has run: nothing it saw, and no work room.
    ran = command("status")
    assert ran.rc == 0, ran.err
    assert ran.lines[:7] == [
        f"lucille-host {__version__}",
        f"policy: ok ({host.dirs.policy_file})",
        "enabled: host.describe, host.sleep",
        "folders: docs",
        "controls: not paused",
        "store: the OS keyring",
        f"hub: {_switchboard.LUCILLE_HUB}, and the loopback hub {LOOPBACK} that "
        "`lucille-host pair --hub` named",
    ]
    generation = host.lucille.resolve().generation
    assert _line(ran.out, "pair") == f"pair: confirmed: pair {host.name} (generation {generation})"
    assert _line(ran.out, "work room") == NO_ROOM
    assert ran.lines[-3:] == [
        "run: no record: `lucille-host run` writes one while it runs",
        "last hub contact: none recorded",
        "clock: unknown: `run` measures it against the hub on each reply it posts",
    ]

    # A room left at once for carrying a hub token: the host holds none, and says which check.
    host.start()
    host.lucille.offer("work-1", token="sb_room_token")
    until(lambda: host.record().no_room == rooms.CHECK_TOKEN, what="the room refused")
    assert _line(command("status").out, "work room") == (
        f"{NO_ROOM}; it left its last one on its own: check 4: "
        "Lucille's offer of the room carried a hub token"
    )

    # A room taken and read, a request answered, and the controls paused.
    room = host.offer("work-2")
    host.reply("work-2", host.request("work-2"))
    until(lambda: _status(host)["skew"] is not None, what="a skew measured")
    assert command("pause").rc == 0
    NoHub.built.clear()
    command.client_class = NoHub
    ran = command("status")
    assert ran.rc == 0, ran.err
    assert NoHub.built == [], "status made a hub call"
    accepted = link.iso(host.record().held[room].accepted_at)
    seen = _status(host)
    assert _line(ran.out, "controls") == (
        "controls: paused: nothing starts until `lucille-host resume`"
    )
    assert _line(ran.out, "work room") == f"work room: {room} (accepted {accepted}; read by `run`)"
    assert _line(ran.out, "run") == (
        f"run: running (pid {seen['pid']}), as of {link.iso(seen['written_at'])}"
    )
    contact = link.iso(seen["last_contact"])
    assert _line(ran.out, "last hub contact") == f"last hub contact: {contact}"
    assert re.fullmatch(
        r"clock: ok: \d+\.\d s (behind|ahead of) the hub's, on the last reply posted "
        r"\(the hub refuses posts past 300 s\)",
        _line(ran.out, "clock"),
    )
    assert not any(line.startswith("offers: ") for line in ran.lines)

    # Stopped: what it last saw stays, and the room is no longer read.
    host.stop()
    command.client_class = host.client_class
    ran = command("status")
    assert _line(ran.out, "run") == f"run: stopped at {link.iso(_status(host)['written_at'])}"
    assert _line(ran.out, "work room") == f"work room: {room} (accepted {accepted})"
    assert _line(ran.out, "last hub contact").startswith("last hub contact: 20")


def test_status_says_what_it_cannot_judge(host: RunHost, command: Commands) -> None:
    # A record a run left when it stopped without saying so; offers waiting; an invalid policy.
    state.write_json(
        host.dirs.status_file,
        {
            "v": 1,
            "running": True,
            "pid": 4242,
            "written_at": time.time() - 4 * loop.STATUS_EVERY,
            "last_contact": None,
            "skew": None,
            "skewed": False,
            "reading": ["room-1"],
            "read_stuck": [],
        },
    )
    record = rooms.load(host.dirs.rooms_file)
    record.offers = {"work-1": 1.0, "work-2": 2.0}
    rooms.save(host.dirs.rooms_file, record)
    paths.ensure_dir(host.dirs.config)
    host.dirs.policy_file.write_text("enabled = 3\n")
    ran = command("status")
    assert ran.rc == 0, ran.err
    assert ran.lines[1].startswith("policy: invalid: ")
    assert not any(line.startswith(("enabled: ", "folders: ")) for line in ran.lines)
    assert _line(ran.out, "run").startswith("run: not seen since ")
    assert _line(ran.out, "run").endswith("(pid 4242): it stopped without saying so")
    assert _line(ran.out, "offers") == (
        "offers: 2 waiting, and none is taken while more than one waits"
    )
    # A run record that holds something else stops the command, as the host's other files do.
    host.dirs.status_file.write_text('{"v": 1}\n')
    ran = command("status")
    assert ran.rc == 1 and "not a lucille-host run record" in ran.err


def test_status_clock_skew_own_status(
    host: RunHost, command: Commands, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _switchboard.SKEW_WINDOW == writekey.WINDOW == 300
    # The host's posts are signed by a clock 400 s ahead of the hub's, which the hub refuses.
    skewing = threading.local()
    sign = writekey.RoomWriteKey.sign_request
    post = _switchboard.RoomClient.post

    def signed(key: Any, *args: Any, **options: Any) -> Any:
        if getattr(skewing, "on", False):
            options["now"] = time.time() + 400
        return sign(key, *args, **options)

    def posted(client: Any, *args: Any, **options: Any) -> Any:
        skewing.on = ahead[0]
        try:
            return post(client, *args, **options)
        finally:
            skewing.on = False

    ahead = [True]
    monkeypatch.setattr(writekey.RoomWriteKey, "sign_request", signed)
    monkeypatch.setattr(_switchboard.RoomClient, "post", posted)
    host.start()
    host.offer("work-1")
    job_id = host.request("work-1")
    until(lambda: _status(host)["skewed"], what="the skew reported")
    skewed = (
        "clock: skewed: this computer's clock is more than 300 s from the hub's, so the hub "
        "refuses what the host writes; set the clock right"
    )
    assert _line(command("status").out, "clock") == skewed
    assert host.lucille.reply("work-1", job_id) is None
    # Set right: the reply is posted on the next try, and the clock is judged again.
    ahead[0] = False
    host.reply("work-1", job_id)
    until(
        lambda: not _status(host)["skewed"] and _status(host)["skew"] is not None,
        what="the skew cleared and measured",
    )
    assert _line(command("status").out, "clock").startswith("clock: ok: ")
    host.stop()
    # A skew measured past the window is skewed too, flagged or not.
    seen = _status(host)
    state.write_json(host.dirs.status_file, {**seen, "skew": -301.0, "skewed": False})
    assert _line(command("status").out, "clock") == skewed


def _host_writes_ahead(monkeypatch: pytest.MonkeyPatch, ahead: list[bool]) -> None:
    """Sign every write made off the test's own thread (the host's, and the library's for the
    host) with a clock 400 s ahead of the hub's while ``ahead[0]`` is true. The fake Lucille
    writes from the test's thread, with the right clock."""
    tester = threading.current_thread()
    sign = writekey.RoomWriteKey.sign_request

    def signed(key: Any, *args: Any, **options: Any) -> Any:
        if ahead[0] and threading.current_thread() is not tester:
            options["now"] = time.time() + 400
        return sign(key, *args, **options)

    monkeypatch.setattr(writekey.RoomWriteKey, "sign_request", signed)


def test_status_clock_skew_from_the_library_s_writes(
    host: RunHost, command: Commands, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Before the host posts any reply, every write is the library's: the touch's records and
    # the accept's offer-back. The hub refusing those for the clock showed as `clock: unknown`.
    ahead = [True]
    _host_writes_ahead(monkeypatch, ahead)
    host.lucille.offer("work-1")
    host.start()
    until(
        lambda: host.dirs.status_file.exists() and _status(host)["skewed"],
        what="the skew reported",
    )
    assert _status(host)["skew"] is None, "no reply was posted"
    assert _line(command("status").out, "clock").startswith("clock: skewed: ")
    # Set right: the next touch reaches the pair's room and writes what it owes, and the clock is
    # no longer judged skewed.
    ahead[0] = False
    host.runner.session.wake()  # type: ignore[union-attr]
    until(lambda: host.reading() == ["room-1"], what="the room taken")
    until(lambda: not _status(host)["skewed"], what="the skew cleared")
    host.stop()


def test_pair_says_when_the_hub_refuses_its_answer_for_the_clock(
    cli: HostCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    ahead = [True]
    _host_writes_ahead(monkeypatch, ahead)
    lucille = cli.lucille()
    running = cli.start("pair", "--code-stdin", stdin=lucille.start() + "\n")
    until(lambda: cli.listens > 0, what="the wait")
    ahead[0] = False
    with collecting(lucille):
        ran = running.wait(30)
    assert ran.rc == 0, ran.text
    assert f"lucille-host: warning: {link.SKEWED}" in ran.err.splitlines()
    lines = [json.loads(line) for line in cli.audit().splitlines()]
    (accepted,) = [line for line in lines if line.get("event") == "accepted"]
    assert (accepted["sent"], accepted["skewed"]) == (False, True)


def test_status_never_shows_sas(cli: HostCli) -> None:
    lucille = cli.lucille()
    code = lucille.start()
    cli.interrupted(code)
    ran = cli.run("status")
    assert ran.rc == 0, ran.text
    assert _line(ran.out, "pair") == (
        f"pair: waiting: pair {lucille.name}: Lucille has not collected this computer's answer yet"
    )
    assert _line(ran.out, "hub") == f"hub: {_switchboard.LUCILLE_HUB}"
    # Lucille collects. Until the host resolves the pair, it does not know: status makes no call.
    assert lucille.collect().state == "paired"
    assert "pair: waiting: " in cli.run("status").out
    # `pair status` resolves, and shows the SAS; `status` points to it, and shows none.
    sas = lucille.sas()
    assert sas in cli.run("pair", "status").out
    found = cli.gate()
    assert found is not None and found.deadline is not None
    outputs = [cli.run("status").text]
    assert (
        f"pair: sas_pending: pair {lucille.name}: `lucille-host pair status` shows the SAS to "
        "compare with Lucille's page, and `lucille-host pair confirm` confirms it before "
        f"{link.iso(found.deadline)}"
    ) in outputs[0]
    # Past the deadline: said so, and left for a command that resolves to forget.
    cli.h.advance(86400 + 1)
    outputs.append(cli.run("status").text)
    assert (
        f"pair: expired: pair {lucille.name}: the SAS was not confirmed before "
        f"{link.iso(found.deadline)}; the next `lucille-host pair status` or `run` forgets it"
    ) in outputs[-1]
    assert cli.held() == [lucille.name]
    assert cli.run("pair", "status").rc == 1
    outputs.append(cli.run("status").text)
    assert "pair: none: this computer holds no pair" in outputs[-1]
    for text in [*outputs, cli.audit()]:
        assert sas not in text and sas.replace("-", "") not in text


# --- log ------------------------------------------------------------------------------------------


def test_log_prints_last_lines(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = paths.ensure_dir(paths.dirs().state)
    assert main(["log"]) == 0
    out, err = capsys.readouterr()
    assert out == "" and "the audit log has no lines yet" in err
    # 30 lines, rotated across three files.
    log = AuditLog(folder, max_bytes=700)
    for index in range(30):
        log.write("test", n=index)
    assert (folder / "audit.log.2").exists()
    assert main(["log"]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["n"] for line in printed] == list(range(10, 30))
    assert main(["log", "--limit", "25"]) == 0
    assert [json.loads(line)["n"] for line in capsys.readouterr().out.splitlines()] == list(
        range(5, 30)
    )
    assert main(["log", "--limit", "1000"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 30
    # A line someone else wrote shows no control character; a line being written is left out.
    with (folder / "audit.log").open("ab") as handle:
        handle.write(b'{"kind":"x","note":"\x1b[2J\xe2\x80\xae"}\n{"kind":"half')
    assert main(["log", "--limit", "2"]) == 0
    assert capsys.readouterr().out.splitlines() == [printed[-1], '{"kind":"x","note":"?[2J???"}']
    # Only the files the log keeps are read.
    assert len(audit.tail(folder, 1000, keep=1)) < 30
    for bad in ("0", "-1"):
        assert main(["log", "--limit", bad]) == 1
        assert "--limit takes a number of lines" in capsys.readouterr().err


# --- forget ---------------------------------------------------------------------------------------


def test_forget_reports_whether_told(cli: HostCli) -> None:
    ran = cli.run("forget")
    assert ran.rc == 1 and ran.err.startswith("none: "), ran.text
    lucille = cli.lucille()
    cli.paired(lucille)
    assert cli.run("pair", "confirm").rc == 0
    ran = cli.run("forget")
    assert ran.rc == 0, ran.text
    assert ran.out == (
        f"forgotten: pair {lucille.name} is forgotten on this computer, its work rooms left "
        "first; Lucille was told\n"
    )
    assert cli.held() == [] and cli.gate() is None
    with pytest.raises(pairing.PairingError) as ended:
        lucille.resolve()
    assert ended.value.state == "ended_by_peer"

    # The hub out of reach: forgotten here all the same, and Lucille is not told.
    second = cli.lucille()
    cli.paired(second)
    cli.client_class = Down(cli.h)
    ran = cli.run("forget")
    assert ran.rc == 0, ran.text
    assert ran.out == (
        f"forgotten: pair {second.name} is forgotten on this computer, its work rooms left "
        "first; Lucille was not told (the hub could not be reached): it keeps its half until "
        "the computer is removed there\n"
    )
    assert cli.held() == [] and cli.gate() is None
    assert second.resolve().state == "paired"


def test_forget_with_the_hub_out_of_reach(host: RunHost, command: Commands, h: Any) -> None:
    host.start()
    room = host.offer("work-1")
    host.stop()
    command.client_class = Down(h)
    ran = command("forget")
    assert ran.rc == 0, ran.err
    assert ran.out.endswith(
        "; Lucille was not told (the hub could not be reached): it keeps its half until the "
        "computer is removed there\n"
    )
    # Left and forgotten here all the same: no room's keys and no pair kept.
    assert host.opened.names() == [] and host.opened.rooms.load()["rooms"] == {}
    (left,) = host.events("room", event="left", room=room)
    assert left["told"] is False
    assert host.lucille.resolve().state == "paired"


def test_forget_verb_leaves_rooms_first(
    host: RunHost, command: Commands, monkeypatch: pytest.MonkeyPatch, host_env: HostEnv
) -> None:
    host.start()
    room = host.offer("work-1")
    job_id = host.request("work-1")
    host.reply("work-1", job_id)
    host.stop()
    assert host.dirs.cursors_file.exists()
    # What the keyring item holds now: the pair's secret, and the room's keys.
    record = host.lucille.pairs.get(host.lucille.name)
    config = host.lucille.room_config("work-1")
    assert record is not None and config.key and config.write_key
    secrets = (record.secret, config.key, config.write_key)
    (stored,) = host_env.keyring.items.values()
    assert all(secret in stored for secret in secrets)
    # A room that cannot be left keeps the pair, and the command says so.
    real = _switchboard.Link.leave_room

    def cannot(opened: _switchboard.Link, name: str) -> _switchboard.RoomLeft:
        raise _switchboard.RoomError("store", "the share store could not be written")

    monkeypatch.setattr(_switchboard.Link, "leave_room", cannot)
    ran = command("forget")
    assert ran.rc == 1 and ran.err.startswith("error: "), ran.err
    assert host.opened.names() == [host.name] and host.gate() is not None
    monkeypatch.setattr(_switchboard.Link, "leave_room", real)
    # Then: every room left before the pair is forgotten, and the pair's room files go with it.
    held: list[list[str]] = []
    forget = pairing.forget

    def watched(*args: Any, **options: Any) -> Any:
        held.append(sorted(host.opened.rooms.load()["rooms"]))
        return forget(*args, **options)

    monkeypatch.setattr("switchboard.pairing.forget", watched)
    ran = command("forget")
    assert ran.rc == 0, ran.err
    assert ran.out.endswith("; Lucille was told\n")
    assert held == [[]], "the share store held a room when the pair was forgotten"
    assert host.opened.names() == [] and host.gate() is None
    record = host.record()
    assert record == rooms.Record(left=record.left, named=record.named)
    assert [gone.room for gone in record.left.values()] == [room]
    assert not host.dirs.cursors_file.exists()
    # The journal stays: a job id is answered once per profile.
    assert host.journal().seen(job_id)
    # The keyring item stays, and keeps no pair and no room key.
    (stored,) = host_env.keyring.items.values()
    assert not any(secret in stored for secret in secrets)
    kept = json.loads(stored)
    assert kept["pairs"]["records"] == {} and kept["pairs"]["pending"] == {}
    assert kept["shared"]["rooms"] == {} and kept["shared"]["offers"] == {}
    # The log: the room abandoned and its leave failed, then, on the second forget, the room
    # left before the pair was forgotten.
    order = [
        (line["kind"], line.get("event"))
        for line in host.audit()
        if line["kind"] in ("room", "pair") and line.get("room", room) == room
    ]
    assert order[-6:] == [
        ("pair", "forget"),
        ("room", "abandoned"),
        ("room", "leave_failed"),
        ("pair", "forget"),
        ("room", "left"),
        ("pair", "forgotten"),
    ]


def test_forget_while_run_runs_stops_it(host: RunHost, command: Commands) -> None:
    host.enable("host.sleep")
    host.start()
    host.offer("work-1")
    sent = body(op="host.sleep", args={"n": 60})
    host.request("work-1", sent)
    until(lambda: host.events("start", job_id=sent["job_id"]), what="the child started")
    started = time.monotonic()
    ran = command("forget")
    assert ran.rc == 0 and ran.out.endswith("; Lucille was told\n"), ran.text
    said = host.wait(30)
    assert said == "none: the pair is gone from this computer (unknown); run stopped"
    # The request it was running is killed, with no reply, well before its 60 s.
    (execution,) = host.events("execution", job_id=sent["job_id"])
    assert (execution["ended"], execution["abandoned"]) == ("killed", True)
    assert time.monotonic() - started < 20
    assert host.events("sent", job_id=sent["job_id"]) == []
    assert host.lucille.replies("work-1") == []
    entry = host.journal().get(sent["job_id"])
    assert entry is not None and entry.abandoned
