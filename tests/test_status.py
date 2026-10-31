"""``lucille-host status``, ``log`` and ``forget`` (spec/PROTOCOL.md sections 9 and 16).

The commands run in-process against Switchboard's test hub, with a fake Lucille on the other
side: a profile paired through the ``pair`` verbs (``tests/_cli.py``), or paired and running
``run`` on a thread (``tests/_run.py``), each command then run through ``main`` as a user would.
"""

from __future__ import annotations

import errno
import json
import os
import re
import signal
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

from lucille_host import __version__, _switchboard, audit, gate, link, loop, paths, rooms, state
from lucille_host.audit import AuditLog
from lucille_host.cli import main
from lucille_host.controls import Controls
from tests import _elsewhere
from tests._cli import HostCli, collecting
from tests._host import HostEnv, lock_free, lock_held
from tests._run import RunHost, body, fast, nothing, until

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
        f"{link.iso(found.deadline)}; the next `lucille-host pair status`, `lucille-host pair "
        "confirm` or `run` forgets it"
    ) in outputs[-1]
    assert cli.held() == [lucille.name]
    assert cli.run("pair", "status").rc == 1
    outputs.append(cli.run("status").text)
    assert "pair: none: this computer holds no pair" in outputs[-1]
    for text in [*outputs, cli.audit()]:
        assert sas not in text and sas.replace("-", "") not in text


def test_status_shows_no_sas_the_host_has_not_shown(cli: HostCli) -> None:
    # The SAS has reached the host (`pair confirm` resolved, and refused: nothing had shown it),
    # and no command has shown it. `status` says so, shows none, and starts no deadline.
    lucille = cli.lucille()
    cli.interrupted(lucille.start())
    assert lucille.collect().state == "paired"
    ran = cli.run("pair", "confirm")
    assert ran.rc == 1 and ran.err.startswith("sas_ready: "), ran.text
    ran = cli.run("status")
    assert ran.rc == 0, ran.text
    assert _line(ran.out, "pair") == (
        f"pair: sas_ready: pair {lucille.name}: the SAS has reached this computer, and no command "
        "has shown it yet: `lucille-host pair status` shows it to compare with Lucille's page"
    )
    sas = lucille.sas()
    assert sas not in ran.text and sas.replace("-", "") not in ran.text
    found = cli.gate()
    assert found is not None and (found.shown_at, found.deadline) == (None, None)
    assert '"sas_shown"' not in cli.audit()


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
    # With the pair held, `status` says the room's leave is tried again (by `run`, or `forget`),
    # and never that this computer holds no work room.
    ran = command("status")
    (line,) = [line for line in ran.lines if line.startswith("work room: ")]
    assert line.startswith(f"work room: {room} ("), line
    assert line.endswith(
        "abandoned (pair over): nothing is taken from it, and its leave is tried again)"
    ), line
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


def test_forget_verb_leaves_the_rooms_of_a_pair_the_library_dropped(
    host: RunHost, command: Commands, monkeypatch: pytest.MonkeyPatch, host_env: HostEnv
) -> None:
    # `pair status` finds the pair ended from this side, the library drops it, and its work room
    # cannot be left then. Before, the command said the pair was kept, to try again, while no
    # command could leave the room any more: `forget` found no pair. Now it says the pair is gone
    # and that `forget` leaves the room, `status` says so of the room, and `forget` does, with no
    # pair held.
    host.start()
    room = host.offer("work-1")
    job_id = host.request("work-1")
    host.reply("work-1", job_id)
    host.stop()
    config = host.lucille.room_config("work-1")
    kept = host.opened.pairs.get(host.name)
    assert kept is not None
    host.opened.forget(host.name)  # publishes this side's end in the pair room
    host.opened.pairs.save(kept)  # this computer's copy, as if that forget never finished
    real = _switchboard.Link.leave_room

    def cannot(opened: _switchboard.Link, name: str) -> _switchboard.RoomLeft:
        raise _switchboard.RoomError("store", "the share store could not be written")

    monkeypatch.setattr(_switchboard.Link, "leave_room", cannot)
    ran = command("pair", "status")
    assert ran.rc == 1 and ran.err == (
        f"error: pair {host.name} is gone from this computer, and a work room held over it could "
        "not be left: its keys stay in the share store until `lucille-host forget` leaves it\n"
    )
    assert host.opened.names() == [] and host.gate() is None
    (stored,) = host_env.keyring.items.values()
    assert config.key in stored
    ran = command("status")
    assert ran.rc == 0 and _line(ran.out, "pair") == "pair: none: this computer holds no pair"
    (line,) = [line for line in ran.lines if line.startswith("work room: ")]
    assert line.startswith(f"work room: {room} ("), line
    assert line.endswith(
        "abandoned (pair over): nothing is taken from it, and `lucille-host forget` leaves it)"
    ), line
    monkeypatch.setattr(_switchboard.Link, "leave_room", real)
    ran = command("forget")
    assert ran.rc == 0, ran.err
    assert ran.out == (
        f"left: pair {host.name} was gone from this computer already, and the work rooms still "
        "held over it are left now\n"
    )
    (stored,) = host_env.keyring.items.values()
    assert config.key not in stored and config.write_key not in stored
    record = host.record()
    assert record == rooms.Record(left=record.left, named=record.named)
    assert [gone.room for gone in record.left.values()] == [room]
    assert not host.dirs.cursors_file.exists()
    ran = command("forget")
    assert ran.rc == 1 and ran.err == "none: this computer holds no pair\n"


def test_forget_verb_with_nothing_left_of_a_dropped_pair_says_no_pair_is_held(
    host: RunHost, command: Commands
) -> None:
    # The library dropped a pair that held no work room, and no accept was interrupted: the room
    # record still names the pair, from `run`. `forget` has no room to leave, and says that no
    # pair is held, not that it left the rooms still held over the pair.
    host.start()
    host.stop()
    record = host.record()
    assert record.pair == host.name and record.held == {} and record.accepting is None
    with host.opened.pairs.lock():
        host.opened.pairs.delete(host.name)  # the library's drop, and nothing after it
    ran = command("forget")
    assert ran.rc == 1 and ran.err == "none: this computer holds no pair\n", (ran.out, ran.err)
    assert ran.out == ""


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
    assert said == (
        f"none: pair {host.name} is gone from this computer: another command forgot it; run stopped"
    )
    # The request it was running is killed, with no reply, well before its 60 s.
    (execution,) = host.events("execution", job_id=sent["job_id"])
    assert (execution["ended"], execution["abandoned"]) == ("killed", True)
    assert time.monotonic() - started < 20
    assert host.events("sent", job_id=sent["job_id"]) == []
    assert host.lucille.replies("work-1") == []
    entry = host.journal().get(sent["job_id"])
    assert entry is not None and entry.abandoned


def _in_files(
    host_env: HostEnv,
    h: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    open_gate: bool = True,
) -> RunHost:
    """A :class:`RunHost` whose profile keeps its pair in files, which another process can
    read (``tests/_elsewhere.py``)."""
    fast(monkeypatch)
    host = RunHost(
        h, host_env, tmp_path, open_gate=open_gate, config=state.HostConfig(store="file")
    )
    state.save_host_config(host.dirs, host.config)
    return host


def _marked(host: RunHost, room: str) -> bool:
    """Whether the room record marks ``room`` abandoned, or has it left."""
    record = host.record()
    held = record.held.get(room)
    if held is not None:
        return held.abandoned is not None
    return any(left.room == room for left in record.left.values())


# How long each hub request of `forget` takes to fail, in the tests where it runs in another
# process with a hub that does not answer (which holds a request until the client's timeout).
SLOW = 3.0


def test_forget_in_another_process_stops_run_when_lucille_is_not_told(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `forget` runs in another process, with the hub out of reach: Lucille is not told, so no
    # note of the library's wakes `run`, and the next resolve due is a day away. `run` still
    # finds its pair gone at its next step, by the gate's record, and kills what it runs.
    host = _in_files(host_env, h, tmp_path, monkeypatch)
    host.enable("host.sleep")
    host.start()
    host.offer("work-1")
    sent = body(op="host.sleep", args={"n": 60})
    host.request("work-1", sent)
    until(lambda: host.events("start", job_id=sent["job_id"]), what="the child started")
    started = time.monotonic()
    forget = _elsewhere.start("forget")
    out, err = forget.communicate(timeout=60)
    assert forget.returncode == 0, out + err
    assert out.startswith(f"forgotten: pair {host.name} is forgotten on this computer"), out + err
    assert "Lucille was not told" in out
    said = host.wait(20)
    assert said == (
        f"none: pair {host.name} is gone from this computer: another command forgot it; run stopped"
    )
    # The request it was running is killed, with no reply, well before its 60 s.
    (execution,) = host.events("execution", job_id=sent["job_id"])
    assert (execution["ended"], execution["abandoned"]) == ("killed", True)
    assert time.monotonic() - started < 20
    assert host.events("sent", job_id=sent["job_id"]) == []
    assert host.lucille.replies("work-1") == []
    entry = host.journal().get(sent["job_id"])
    assert entry is not None and entry.abandoned
    assert host.opened.names() == [] and host.gate() is None


def test_forget_in_another_process_kills_at_once_while_it_waits_for_the_hub(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `forget`, in another process, marks the room abandoned first, then leaves it and tells
    # Lucille with the record lock held, through a hub that takes SLOW s to fail each request.
    # `run`'s main thread waits for that lock; the request it runs is killed all the same, as
    # soon as the room is marked, with no reply: while `forget` still waits for the hub.
    host = _in_files(host_env, h, tmp_path, monkeypatch)
    host.enable("host.sleep")
    host.start()
    room = host.offer("work-1")
    sent = body(op="host.sleep", args={"n": 60})
    host.request("work-1", sent)
    until(lambda: host.events("start", job_id=sent["job_id"]), what="the child started")
    forget = _elsewhere.start("forget", str(SLOW))
    try:
        until(lambda: _marked(host, room), timeout=30, what="forget marking the room")
        marked = time.monotonic()
        (execution,) = until(
            lambda: host.events("execution", job_id=sent["job_id"]),
            timeout=60,
            what="the child ending",
        )
        killed = time.monotonic()
        # `forget` deletes the gate's record last, once it is done with the hub.
        forgetting = host.gate() is not None
    finally:
        out, err = forget.communicate(timeout=120)
    assert forgetting, "the child was killed only once forget was done with the hub"
    assert killed - marked < SLOW / 2
    assert (execution["ended"], execution["abandoned"]) == ("killed", True)
    assert forget.returncode == 0, out + err
    assert "Lucille was not told" in out
    said = host.wait(20)
    assert said == (
        f"none: pair {host.name} is gone from this computer: another command forgot it; run stopped"
    )
    assert host.events("sent", job_id=sent["job_id"]) == []
    assert host.lucille.replies("work-1") == []
    entry = host.journal().get(sent["job_id"])
    assert entry is not None and entry.abandoned
    assert host.opened.names() == [] and host.gate() is None


@pytest.mark.parametrize("argv", [("forget",), ("pair", "reject")], ids=["forget", "reject"])
def test_a_command_another_forget_beat_says_the_pair_is_gone(
    host_env: HostEnv,
    h: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: Commands,
    argv: tuple[str, ...],
) -> None:
    # The command finds the pair held; then, before it has the record lock, `forget` in another
    # process forgets it, Lucille not told. Before, this command went on to forget a pair that was
    # gone, and said that Lucille was not told because the hub could not be reached: false, since
    # it never tried. It now says the pair is gone, as with no pair at all, and does nothing more.
    host = _in_files(host_env, h, tmp_path, monkeypatch, open_gate=argv == ("forget",))
    found = link.Session._require
    elsewhere: list[tuple[int | None, str]] = []

    def then_forgotten_elsewhere(session: link.Session) -> str:
        name = found(session)
        forget = _elsewhere.start("forget")
        out, err = forget.communicate(timeout=60)
        elsewhere.append((forget.returncode, out + err))
        return name

    monkeypatch.setattr(link.Session, "_require", then_forgotten_elsewhere)
    ran = command(*argv)
    ((code, said),) = elsewhere
    assert code == 0 and said.startswith(f"forgotten: pair {host.name} "), said
    assert ran.rc == 1, ran
    # After the warning every command gives under --store-file.
    assert ran.err.splitlines()[-1] == (
        f"none: pair {host.name} is gone from this computer: another command forgot it"
    )
    assert "Lucille was not told" not in ran.err
    assert ran.out == ""
    # Only the other process forgot it.
    assert [line["pair"] for line in host.events("pair", event="forget")] == [host.name]
    assert host.opened.names() == [] and host.gate() is None


def test_a_stop_signal_while_run_waits_for_forget_in_another_process(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `run` on the main thread, with its own handlers, as `lucille-host run` runs. `forget`, in
    # another process, holds the record lock through a hub that takes SLOW s to fail each request,
    # and `run`'s main thread waits for it. A SIGTERM then does not end that wait: the request
    # `run` runs is killed as soon as `forget` marks its room, as without the signal, and `run`
    # stops once `forget` is done, its pair gone. Before, the wait raised, and `run` stopped with
    # an error.
    assert threading.current_thread() is threading.main_thread()
    host = _in_files(host_env, h, tmp_path, monkeypatch)
    host.enable("host.sleep")
    runner = loop.Runner(host.dirs, host.session(), controls=host.controls, say=host.said.append)
    host.runner = runner
    sent = body(op="host.sleep", args={"n": 60})
    main = threading.main_thread().ident
    assert main is not None
    stops = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    # Each from a handler that does nothing, so that one the test process inherited ignored is
    # handled here too, and one that comes once `run` has returned cannot end the test process.
    inherited = {stop: signal.signal(stop, nothing) for stop in stops}
    seen: dict[str, Any] = {}

    def driver() -> None:
        try:
            room = host.offer("work-1")
            host.request("work-1", sent)
            until(lambda: host.events("start", job_id=sent["job_id"]), what="the child started")
            seen["forget"] = _elsewhere.start("forget", str(SLOW))
            until(lambda: _marked(host, room), timeout=30, what="forget marking the room")
            time.sleep(0.3)  # main waits for the record lock at its next step
            signal.pthread_kill(main, signal.SIGTERM)
            # `forget` deletes the gate's record last, once it is done with the hub.
            seen["while_forgetting"] = host.gate() is not None
            until(
                lambda: host.events("execution", job_id=sent["job_id"]),
                timeout=60,
                what="the child ending",
            )
            seen["killed_while_forgetting"] = host.gate() is not None
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            seen["trouble"] = exc
            host.controls.draining.set()

    thread = threading.Thread(target=driver, daemon=True)
    thread.start()
    try:
        said = runner.run()
    finally:
        thread.join(30)
        for stop, handler in inherited.items():
            signal.signal(stop, handler)
        forget = seen.get("forget")
        out, err = forget.communicate(timeout=120) if forget is not None else ("", "")
    assert "trouble" not in seen, seen
    assert seen["while_forgetting"], "forget was done before the signal came"
    assert seen["killed_while_forgetting"], "the child was killed only once forget was done"
    assert forget is not None and forget.returncode == 0, out + err
    assert said == (
        f"none: pair {host.name} is gone from this computer: another command forgot it; run stopped"
    )
    assert host.controls.draining.is_set()
    (execution,) = host.events("execution", job_id=sent["job_id"])
    assert (execution["ended"], execution["abandoned"]) == ("killed", True)
    assert host.events("sent", job_id=sent["job_id"]) == []
    assert host.lucille.replies("work-1") == []
    assert host.opened.names() == [] and host.gate() is None


def test_a_request_queued_from_a_room_forget_marks_in_another_process_never_starts(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two requests, read in one page. The worker answers the first; before it takes the second,
    # `forget`, in another process, marks the room abandoned, then waits on a hub that takes
    # SLOW s to fail each request, the record lock held. The second request is neither decided
    # nor started: the worker reads the mark before each request it takes, and waits for no
    # lock.
    host = _in_files(host_env, h, tmp_path, monkeypatch)
    host.enable("host.sleep")
    first = body()
    second = body(op="host.sleep", args={"n": 60})
    # Posted before `run` takes the room, so that its first read hands both to the worker.
    host.lucille.offer("work-1")
    host.request("work-1", first)
    host.request("work-1", second)
    # The worker is held between the two, once the first is answered, as its cursor moves.
    between = threading.Event()
    move = loop.Cursors.set

    def held(cursors: loop.Cursors, workspace: str, seq: int) -> None:
        if not between.is_set():
            between.set()
            until(lambda: _marked(host, "room-1"), timeout=30, what="forget marking the room")
        move(cursors, workspace, seq)

    monkeypatch.setattr(loop.Cursors, "set", held)
    host.start()
    assert between.wait(20), "the worker never answered the first request"
    host.reply("work-1", first["job_id"])
    forget = _elsewhere.start("forget", str(SLOW))
    try:
        said = host.wait(120)
    finally:
        out, err = forget.communicate(timeout=120)
    assert forget.returncode == 0, out + err
    assert said == (
        f"none: pair {host.name} is gone from this computer: another command forgot it; run stopped"
    )
    assert host.events("decision", job_id=second["job_id"]) == []
    assert host.events("start", job_id=second["job_id"]) == []
    replied = [row["body"]["job_id"] for row in host.lucille.replies("work-1")]
    assert replied == [first["job_id"]]


def _second_run(host: RunHost) -> tuple[BaseException | str | None, list[str]]:
    """Another ``run`` of the profile, on a thread: what it raised or returned within 10 s (None
    if it was still running then, and was drained), and what it said."""
    said: list[str] = []
    controls = Controls(host.dirs.state)
    runner = loop.Runner(host.dirs, host.session(), controls=controls, say=said.append)
    outcome: list[BaseException | str] = []

    def second() -> None:
        try:
            outcome.append(runner.run())
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            outcome.append(exc)

    thread = threading.Thread(target=second, daemon=True)
    thread.start()
    thread.join(10)
    if thread.is_alive():
        controls.draining.set()
        thread.join(30)
        return None, said
    return outcome[0], said


def test_a_profile_runs_one_run_at_a_time(host: RunHost) -> None:
    # Two runs of one profile would each read every request, and each answer it.
    lock = host.dirs.state / loop.RUN_LOCK
    host.start()
    until(lambda: host.dirs.status_file.exists(), what="the run started")
    assert not lock_free(lock), "another process could take the run lock"
    # A second `run` of the profile, in the same process, is refused before it does anything.
    refused, said = _second_run(host)
    assert isinstance(refused, loop.Failed), refused
    assert str(refused) == loop.ALREADY_RUNNING
    assert said == []
    # The first goes on, and holds the lock until it stops.
    assert host.running
    assert host.offer("work-1") == "room-1"
    assert not lock_free(lock)
    host.stop()
    assert lock_free(lock)
    # Then another `run` may start.
    host.start()
    until(lambda: host.reading() == ["room-1"], what="the next run reading its room")
    host.stop()


def test_a_run_refuses_while_another_process_holds_the_lock(host: RunHost) -> None:
    lock = host.dirs.state / loop.RUN_LOCK
    lines = len(host.audit())
    with lock_held(lock):
        refused, said = _second_run(host)
    assert isinstance(refused, loop.Failed), refused
    assert str(refused) == loop.ALREADY_RUNNING
    # It did nothing: said nothing, wrote no status and no log line, held no lock.
    assert said == []
    assert not host.dirs.status_file.exists()
    assert len(host.audit()) == lines
    assert lock_free(lock)


def test_a_run_refuses_where_the_run_lock_cannot_be_taken(
    host: RunHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Where the filesystem cannot lock (ENOLCK, on a network mount with no lock service, say),
    # `run` cannot make sure that no other `run` of the profile answers Lucille's requests: it
    # says so, naming itself, and does nothing.
    real = os.lockf

    def cannot_lock(descriptor: int, command: int, length: int) -> None:
        if command == os.F_TLOCK:
            raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))
        real(descriptor, command, length)

    lines = len(host.audit())
    monkeypatch.setattr("os.lockf", cannot_lock)
    refused, said = _second_run(host)
    lock = os.path.realpath(host.dirs.state / loop.RUN_LOCK)
    assert isinstance(refused, loop.Failed), refused
    assert str(refused) == (
        f"cannot lock {lock} ({os.strerror(errno.ENOLCK)}), so `run` cannot make sure it is the "
        "only one of this profile"
    )
    assert said == []
    assert not host.dirs.status_file.exists()
    assert len(host.audit()) == lines


def test_run_refused_says_why(
    host: RunHost, command: Commands, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The command, run on a thread, so that a `run` that does not refuse is drained and fails
    # the test, rather than running for ever.
    made: list[Controls] = []

    class Kept(Controls):
        def __init__(self, *args: Any, **options: Any) -> None:
            super().__init__(*args, **options)
            made.append(self)

    monkeypatch.setattr("lucille_host.cli.Controls", Kept)
    outcome: list[Ran] = []
    with lock_held(host.dirs.state / loop.RUN_LOCK):
        thread = threading.Thread(target=lambda: outcome.append(command("run")), daemon=True)
        thread.start()
        thread.join(10)
        if thread.is_alive():
            for controls in made:
                controls.draining.set()
            thread.join(30)
    assert len(outcome) == 1
    (ran,) = outcome
    assert ran.rc == 1, ran
    assert ran.err == f"lucille-host: {loop.ALREADY_RUNNING}\n"
    assert ran.out == ""


def test_run_of_a_pair_with_no_gate_record_stops_at_once(host: RunHost) -> None:
    # Such a pair can never be confirmed here, so `run` says so and stops, rather than telling
    # the user that `pair status` shows a SAS it refuses to show.
    gate.remove(host.dirs.gate_file)
    host.start()
    with pytest.raises(link.Stopped) as stopped:
        host.wait(10)
    assert stopped.value.state == "refused"
    assert "has no local gate record" in str(stopped.value)
    assert host.said == []
    assert not host.dirs.status_file.exists()
    assert host.opened.names() == [host.name]
