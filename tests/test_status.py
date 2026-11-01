"""``lucille-host status``, ``log`` and ``forget`` (spec/PROTOCOL.md sections 9 and 16).

The commands run in-process against Switchboard's test hub, with a fake Lucille on the other
side: a profile paired through the ``pair`` verbs (``tests/_cli.py``), or paired and running
``run`` on a thread (``tests/_run.py``), each command then run through ``main`` as a user would.
"""

from __future__ import annotations

import dataclasses
import errno
import json
import os
import re
import signal
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import httpx
import pytest
from switchboard import pairing, writekey
from switchboard.testing import hub

from lucille_host import (
    __version__,
    _switchboard,
    audit,
    gate,
    link,
    loop,
    paths,
    policy,
    rooms,
    state,
)
from lucille_host.audit import AuditLog
from lucille_host.cli import main
from lucille_host.controls import Controls
from lucille_host.ops import OPERATIONS, Operation
from tests import _elsewhere
from tests._cli import HostCli, collecting
from tests._host import HostEnv, lock_free, lock_held
from tests._run import RunHost, body, fast, nothing, until
from tests.spec import _support

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


def _objects(out: str) -> list[dict[str, Any]]:
    """What a command run with ``--json`` wrote: one object per line, each held to its schema."""
    return _support.cli_objects(out)


# The text of each word status --json gives for why a room was abandoned or left.
WHY = {
    "replaced": rooms.REPLACED,
    "left_by_lucille": rooms.LUCILLE_LEFT,
    "pair_moved": rooms.MOVED,
    "not_over_current_pair": rooms.NOT_LISTED,
    "pair_over": rooms.PAIR_OVER,
    "gone": rooms.GONE,
    "check_3": rooms.CHECK_HUB,
    "check_4": rooms.CHECK_TOKEN,
    "left_before": rooms.LEFT_BEFORE,
}


# The text of each word status --json gives for why the policy holds an operation disabled.
DISABLED = {
    "disabled_by_upgrade": ": an upgrade changed it; `lucille-host ops enable ",
    "unavailable": ": not on this computer",
    "not_in_catalog": ": not in this release's catalog",
}


def _agree(command: Callable[..., Ran]) -> dict[str, Any]:
    """``status`` and ``status --json`` run one after the other, and each field of the object says
    what the matching line of the text says. Returns the object."""
    text = command("status")
    ran = command("status", "--json")
    assert (text.rc, ran.rc) == (0, 0), (text.err, ran.err)
    (found,) = _objects(ran.out)
    lines = text.lines
    assert lines[0] == f"lucille-host {found['host_version']}"
    checked = found["policy"]
    if checked["state"] == "ok":
        where = checked["file"] or "no file yet: the fresh policy"
        assert _line(text.out, "policy") == f"policy: ok ({where})"
        assert _line(text.out, "enabled") == "enabled: " + (", ".join(checked["enabled"]) or "none")
        disabled = [line for line in lines if line.startswith("disabled: ")]
        assert len(disabled) == len(checked["disabled"])
        for line, entry in zip(disabled, checked["disabled"], strict=True):
            assert line.startswith(f"disabled: {entry['name']}{DISABLED[entry['why']]}"), line
        assert _line(text.out, "folders") == "folders: " + (", ".join(checked["folders"]) or "none")
    else:
        assert _line(text.out, "policy") == f"policy: invalid: {checked['error']}"
    assert _line(text.out, "controls").startswith(
        "controls: paused: " if found["paused"] else "controls: not paused"
    )
    if found["store"] == "keyring":
        assert _line(text.out, "store") == "store: the OS keyring"
    else:
        assert found["store"] == "file" and _line(text.out, "store").startswith("store: files in ")
    # The loopback hub is in the object exactly when the line names one, and is the one it names.
    hub_line = f"hub: {found['hub']}"
    if found["loopback_hub"] is not None:
        hub_line += (
            f", and the loopback hub {found['loopback_hub']} that `lucille-host pair --hub` named"
        )
    assert _line(text.out, "hub") == hub_line
    word = {"several_pairs": "error"}.get(found["pair"]["state"], found["pair"]["state"])
    assert _line(text.out, "pair").startswith(f"pair: {word}: ")
    if "name" in found["pair"]:
        assert found["pair"]["name"] in _line(text.out, "pair")
    if found["pair"]["state"] == "confirmed":
        generation = found["pair"]["generation"]
        assert _line(text.out, "pair") == f"pair: confirmed: pair {found['pair']['name']}" + (
            f" (generation {generation})" if generation is not None else ""
        )
    held = [line for line in lines if line.startswith("work room: ")]
    if not found["work_rooms"]:
        assert held[0].startswith("work room: none: ")
        if found["last_left_because"] is not None:
            assert f": {WHY[found['last_left_because']]}: " in held[0]
        else:
            assert held == [NO_ROOM]
    else:
        assert len(held) == len(found["work_rooms"])
        for line, room in zip(held, found["work_rooms"], strict=True):
            assert line.startswith(f"work room: {room['name']} ("), line
            if room["state"] != "accept_interrupted":
                assert f"(accepted {room['accepted_at']}" in line
            notes = {
                "reading": "; read by `run`)",
                "read_stuck": "; read_stuck: ",
                "pair_gone": "the pair it was taken over is gone from this computer",
                "accept_interrupted": "an accept a stop interrupted may have kept it",
            }
            if room["state"] == "abandoned":
                assert f"abandoned ({WHY[room['why']]})" in line
            elif room["state"] == "not_read":
                assert line.endswith(f"(accepted {room['accepted_at']})")
            else:
                assert notes[room["state"]] in line
    offers = [line for line in lines if line.startswith("offers: ")]
    if found["offers_waiting"]:
        assert offers[0].startswith(f"offers: {found['offers_waiting']} waiting")
    else:
        assert offers == []
    run = found["run"]
    expected_run = {
        "none": "run: no record: ",
        "running": f"run: running (pid {run.get('pid')}), as of {run.get('as_of')}",
        "not_seen": f"run: not seen since {run.get('as_of')} (pid {run.get('pid')}): ",
        "stopped": f"run: stopped at {run.get('as_of')}",
    }[run["state"]]
    assert _line(text.out, "run").startswith(expected_run)
    contact = found["last_hub_contact"] or "none recorded"
    assert _line(text.out, "last hub contact") == f"last hub contact: {contact}"
    clock = found["clock"]
    assert _line(text.out, "clock").startswith(f"clock: {clock['state']}: ")
    if clock["state"] == "ok":
        side = "behind" if clock["skew_s"] > 0 else "ahead of"
        assert f"{abs(clock['skew_s']):.1f} s {side}" in _line(text.out, "clock")
    return found


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
    # With --json, the accepted object says so, and stderr warns the same.
    ahead[0] = True
    second = cli.lucille()
    listened = cli.listens
    running = cli.start(
        "--profile", "json", "--json", "pair", "--code-stdin", stdin=second.start() + "\n"
    )
    until(lambda: cli.listens > listened, what="the wait")
    ahead[0] = False
    with collecting(second):
        ran = running.wait(30)
    assert ran.rc == 0, ran.text
    assert _objects(ran.out)[1] == {
        "v": 1,
        "type": "pair",
        "state": "accepted",
        "name": second.name,
        "clock_skewed": True,
    }
    assert ran.err == f"lucille-host: warning: {link.SKEWED}\n"


def test_status_never_shows_sas(cli: HostCli) -> None:
    lucille = cli.lucille()
    code = lucille.start()
    cli.interrupted(code)
    ran = cli.run("status")
    assert ran.rc == 0, ran.text
    # It makes no hub call, so it says only that no command has seen Lucille collect the answer.
    waiting = (
        f"pair: waiting: pair {lucille.name}: no command here has seen Lucille collect this "
        "computer's answer, and it may have since: `lucille-host pair status` asks the hub"
    )
    assert _line(ran.out, "pair") == waiting
    assert _line(ran.out, "hub") == f"hub: {_switchboard.LUCILLE_HUB}"
    # Lucille collects. Until the host resolves the pair, it does not know: status makes no call,
    # and says the same, which stays true.
    assert lucille.collect().state == "paired"
    assert _line(cli.run("status").out, "pair") == waiting
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


def test_status_json_never_shows_sas(cli: HostCli) -> None:
    lucille = cli.lucille()
    cli.interrupted(lucille.start())
    outputs = []

    def pair_of_status() -> dict[str, Any]:
        ran = cli.run("status", "--json")
        assert ran.rc == 0, ran.text
        outputs.append(ran.text)
        (found,) = _objects(ran.out)
        return found["pair"]

    assert pair_of_status() == {"state": "waiting", "name": lucille.name}
    assert lucille.collect().state == "paired"
    # The SAS has reached the host (`pair confirm` resolved, and refused), and no command has
    # shown it: status --json says so, shows none, and starts no deadline.
    assert cli.run("pair", "confirm").rc == 1
    assert pair_of_status() == {"state": "sas_ready", "name": lucille.name}
    found = cli.gate()
    assert found is not None and (found.shown_at, found.deadline) == (None, None)
    assert '"sas_shown"' not in cli.audit()
    # Shown by `pair status`: status --json gives the deadline, and still no SAS.
    sas = lucille.sas()
    assert sas in cli.run("pair", "status").out
    found = cli.gate()
    assert found is not None and found.deadline is not None
    assert pair_of_status() == {
        "state": "sas_pending",
        "name": lucille.name,
        "deadline": link.iso(found.deadline),
    }
    cli.h.advance(86400 + 1)
    assert pair_of_status() == {
        "state": "expired",
        "name": lucille.name,
        "deadline": link.iso(found.deadline),
    }
    assert cli.gate() == found
    for text in outputs:
        assert sas not in text and sas.replace("-", "") not in text


def test_status_json_reports_no_generation_past_the_bound(cli: HostCli) -> None:
    # Lucille's side can raise the pair's generation, and the library takes any integer from 0.
    # Every one up to 2^53 - 1, the bound the schema gives, is reported as it is; one past it is
    # reported as unknown, and status still answers, as text and as JSON.
    lucille = cli.lucille()
    cli.paired(lucille)
    assert cli.run("pair", "confirm").rc == 0
    for generation, reported in (
        (2, 2),
        (5, 5),
        (2**53 - 1, 2**53 - 1),
        (2**53, None),
        (2**70, None),
    ):
        record = lucille.pairs.get(lucille.name)
        record.generation = generation
        lucille.pairs.save(record)
        lucille.resolve()
        assert cli.run("pair", "status", "--json").rc == 0
        kept = _switchboard.open_link(cli.dirs, state.load_host_config(cli.dirs)).kept(lucille.name)
        assert kept is not None and kept.generation == generation
        found = _agree(_through(cli))
        assert found["pair"] == {"state": "confirmed", "name": lucille.name, "generation": reported}
    # The host's own record of the pair, which the library does not check when it reads it: a
    # generation that is not a whole number from 0 is reported as unknown too.
    here = _switchboard.open_link(cli.dirs, state.load_host_config(cli.dirs))
    for generation in (True, -1, "7"):
        record = here.pairs.get(lucille.name)
        record.generation = generation
        here.pairs.save(record)
        found = _agree(_through(cli))
        assert found["pair"] == {"state": "confirmed", "name": lucille.name, "generation": None}


def test_pair_status_refuses_a_sas_the_host_never_writes(cli: HostCli) -> None:
    # The host's own record of the pair, which the library does not check when it reads it: a SAS
    # of any other shape than the library's is never shown, in JSON or in text. The pair cannot
    # be used here, and every verb that would show the SAS says so.
    lucille = cli.lucille()
    cli.paired(lucille)
    here = _switchboard.open_link(cli.dirs, state.load_host_config(cli.dirs))
    shown = here.pairs.get(lucille.name).sas
    said = "the pair cannot be used here (invalid_sas)"
    for sas in (
        "SAS is 1234-5678‮",
        "1234-5678‮",
        "1234-56789",
        "12345678",
        "1234_5678",
        "1234-567a",
        "١٢٣٤-٥٦٧٨",
        # Wrong in the first four only, at either end of them, or only just past the `-`.
        "a234-5678",
        "123a-5678",
        "١٢٣٤-5678",
        "1234-a678",
        12345678,
    ):
        record = here.pairs.get(lucille.name)
        record.sas = sas
        here.pairs.save(record)
        ran = cli.run("pair", "status", "--json")
        assert ran.rc == 1 and _objects(ran.out) == [
            {"v": 1, "type": "error", "code": "pair_unusable", "message": said}
        ], (sas, ran.text)
        ran = cli.run("pair", "status")
        assert (ran.rc, ran.out, ran.err) == (1, "", f"error: {said}\n"), sas
    record = here.pairs.get(lucille.name)
    record.sas = shown
    here.pairs.save(record)
    ran = cli.run("pair", "status", "--json")
    assert ran.rc == 0 and _objects(ran.out)[0]["sas"] == shown


def test_status_json_matches_text(host: RunHost, command: Commands, tmp_path: Path) -> None:
    state.save_host_config(host.dirs, state.HostConfig(hub=LOOPBACK))
    host.enable("host.sleep")
    folder = tmp_path / "docs"
    folder.mkdir()
    assert command("roots", "add", "docs", str(folder)).rc == 0
    found = _agree(command)
    assert found["pair"]["state"] == "confirmed" and found["run"] == {"state": "none"}
    assert found["pair"]["generation"] == host.lucille.resolve().generation
    assert (found["store"], found["loopback_hub"]) == ("keyring", LOOPBACK)
    # A room taken and read, a reply posted, and the controls paused.
    host.start()
    room = host.offer("work-1")
    host.reply("work-1", host.request("work-1"))
    until(lambda: _status(host)["skew"] is not None, what="a skew measured")
    assert command("pause").rc == 0
    found = _agree(command)
    assert found["paused"] and found["run"]["state"] == "running"
    assert found["work_rooms"] == [
        {
            "state": "reading",
            "name": room,
            "accepted_at": link.iso(host.record().held[room].accepted_at),
        }
    ]
    assert found["clock"]["state"] == "ok"
    host.stop()
    found = _agree(command)
    assert found["run"]["state"] == "stopped" and found["work_rooms"][0]["state"] == "not_read"
    # Abandoned; offers waiting; the clock skewed; a run that stopped without saying so.
    record = host.record()
    record.held[room].abandoned = rooms.REPLACED
    record.offers = {"work-2": 1.0, "work-3": 2.0}
    rooms.save(host.dirs.rooms_file, record)
    seen = _status(host)
    state.write_json(
        host.dirs.status_file,
        {**seen, "running": True, "written_at": time.time() - 4 * loop.STATUS_EVERY},
    )
    found = _agree(command)
    assert found["work_rooms"][0]["why"] == "replaced" and found["offers_waiting"] == 2
    assert found["run"]["state"] == "not_seen"
    state.write_json(host.dirs.status_file, {**seen, "skewed": True})
    assert _agree(command)["clock"] == {"state": "skewed", "skew_s": seen["skew"]}
    # No room held, the last one left on its own; an invalid policy.
    rooms.save(host.dirs.rooms_file, rooms.Record(pair=record.pair, made=record.made))
    record = host.record()
    record.no_room = rooms.CHECK_TOKEN
    rooms.save(host.dirs.rooms_file, record)
    host.dirs.policy_file.write_text("enabled = 3\n")
    found = _agree(command)
    assert found["work_rooms"] == [] and found["last_left_because"] == "check_4"
    assert found["policy"]["state"] == "invalid"


def _through(cli: HostCli) -> Callable[..., Ran]:
    """The command run through ``cli``, as :func:`_agree` runs it."""

    def command(*argv: str) -> Ran:
        ran = cli.run(*argv)
        return Ran(ran.rc, ran.out, ran.err)

    return command


def test_status_json_matches_text_with_no_pair_held(
    cli: HostCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No pair held: a room left over from a pair the library dropped, the room of an accept a stop
    # interrupted, and a room abandoned for each reason. No folder, and an operation disabled for
    # each reason.
    monkeypatch.setattr(Operation, "available", lambda op, _family: op.name != "files.sha256")
    paths.ensure_dir(cli.dirs.config)
    enabled = {
        "host.sleep": "0" * 64,
        "files.sha256": OPERATIONS["files.sha256"].definition_hash,
        "host.nothing": "1" * 64,
    }
    policy.save(cli.dirs.policy_file, policy.Policy(enabled=MappingProxyType(enabled)))
    held = {"left-over": rooms.Held("workspace-0", 1.0)}
    for word, why in WHY.items():
        held[word.replace("_", "-")] = rooms.Held(f"workspace-{word}", 2.0, why)
    paths.ensure_dir(cli.dirs.state)
    interrupted = rooms.Accepting("offer-1", "room-9", ())
    rooms.save(
        cli.dirs.rooms_file,
        rooms.Record(pair="pair-x", made=1.0, held=held, accepting=interrupted),
    )
    found = _agree(_through(cli))
    assert found["pair"] == {"state": "none"}
    assert found["policy"]["folders"] == [] and found["policy"]["disabled"] == [
        {"name": "host.sleep", "why": "disabled_by_upgrade"},
        {"name": "files.sha256", "why": "unavailable"},
        {"name": "host.nothing", "why": "not_in_catalog"},
    ]
    abandoned = {
        word.replace("_", "-"): {
            "state": "abandoned",
            "name": word.replace("_", "-"),
            "accepted_at": link.iso(2.0),
            "why": word,
        }
        for word in WHY
    }
    left_over = {"state": "pair_gone", "name": "left-over", "accepted_at": link.iso(1.0)}
    assert found["work_rooms"] == [
        *[{**abandoned, "left-over": left_over}[name] for name in sorted(held)],
        {"state": "accept_interrupted", "name": "room-9"},
    ]


def test_status_json_matches_text_for_the_rooms_run_reads(cli: HostCli) -> None:
    # A pair held, with a room `run` reads and one where it stopped reading; `run` has not reached
    # the hub yet. As `run` writes it, a room it is stuck in is still among those it reads.
    lucille = cli.lucille()
    cli.paired(lucille)
    assert cli.run("pair", "confirm").rc == 0
    now = cli.h.now
    held = {"room-1": rooms.Held("workspace-1", now), "room-2": rooms.Held("workspace-2", now)}
    rooms.save(cli.dirs.rooms_file, rooms.Record(pair=lucille.name, made=now, held=held))
    written = {
        "v": 1,
        "running": True,
        "pid": 4242,
        "written_at": now,
        "last_contact": None,
        "skew": None,
        "skewed": False,
        "reading": ["room-1", "room-2"],
        "read_stuck": ["room-2"],
    }
    state.write_json(cli.dirs.status_file, written)
    found = _agree(_through(cli))
    accepted = link.iso(now)
    assert found["work_rooms"] == [
        {"state": "reading", "name": "room-1", "accepted_at": accepted},
        {"state": "read_stuck", "name": "room-2", "accepted_at": accepted},
    ]
    assert found["run"] == {"state": "running", "pid": 4242, "as_of": accepted}
    assert found["last_hub_contact"] is None
    # The same record, left by a `run` that stopped without saying so, and by one that stopped:
    # neither reads any room now, whatever its last record lists.
    gone = now - 4 * loop.STATUS_EVERY
    for running, written_at, run in (
        (True, gone, {"state": "not_seen", "pid": 4242, "as_of": link.iso(gone)}),
        (False, now, {"state": "stopped", "as_of": accepted}),
    ):
        state.write_json(
            cli.dirs.status_file, {**written, "running": running, "written_at": written_at}
        )
        found = _agree(_through(cli))
        assert found["work_rooms"] == [
            {"state": "not_read", "name": "room-1", "accepted_at": accepted},
            {"state": "not_read", "name": "room-2", "accepted_at": accepted},
        ]
        assert found["run"] == run


def test_status_json_names_each_check_the_last_room_failed(cli: HostCli) -> None:
    paths.ensure_dir(cli.dirs.state)
    words = {
        rooms.CHECK_HUB: "check_3",
        rooms.CHECK_TOKEN: "check_4",
        rooms.LEFT_BEFORE: "left_before",
    }
    assert set(words) == set(rooms.CHECKS)
    for check, word in words.items():
        rooms.save(cli.dirs.rooms_file, rooms.Record(no_room=check))
        found = _agree(_through(cli))
        assert (found["work_rooms"], found["last_left_because"]) == ([], word)


def test_status_json_matches_text_with_the_file_store(cli: HostCli) -> None:
    # The pair kept in files, through the states of the pair the tests above do not reach, and a
    # clock the hub refused before any skew was measured.
    lucille = cli.lucille()
    cli.paired(lucille, "--store-file")
    found = _agree(_through(cli))
    assert (found["store"], found["loopback_hub"]) == ("file", None)
    assert found["pair"]["state"] == "sas_pending"
    # No gate record: it can never be confirmed here.
    cli.dirs.gate_file.unlink()
    found = _agree(_through(cli))
    assert found["pair"] == {"state": "refused", "name": lucille.name}
    assert cli.run("pair", "reject").rc == 0
    second = cli.lucille()
    cli.paired(second, "--store-file")
    assert cli.run("pair", "confirm").rc == 0
    found = _agree(_through(cli))
    assert found["store"] == "file"
    assert found["pair"] == {"state": "confirmed", "name": second.name, "generation": 0}
    # Lucille moves the pair to a new generation, and `pair status` resolves it.
    second.heal()
    assert cli.run("pair", "status").rc == 0
    found = _agree(_through(cli))
    assert second.resolve().generation == 1
    assert found["pair"] == {"state": "confirmed", "name": second.name, "generation": 1}
    # A kept pair whose generation the store does not say.
    real = _switchboard.Link.kept

    def no_generation(link_: Any, name: str) -> Any:
        kept = real(link_, name)
        return None if kept is None else dataclasses.replace(kept, generation=None)

    with cli.monkeypatch.context() as patch:
        patch.setattr(_switchboard.Link, "kept", no_generation)
        found = _agree(_through(cli))
    assert found["pair"] == {"state": "confirmed", "name": second.name, "generation": None}
    # The hub refused a write for the clock before any reply measured a skew.
    now = cli.h.now
    state.write_json(
        cli.dirs.status_file,
        {
            "v": 1,
            "running": False,
            "pid": 4242,
            "written_at": now,
            "last_contact": None,
            "skew": None,
            "skewed": True,
            "reading": [],
            "read_stuck": [],
        },
    )
    found = _agree(_through(cli))
    assert found["clock"] == {"state": "skewed", "skew_s": None}
    assert found["run"] == {"state": "stopped", "as_of": link.iso(now)}


def test_status_refuses_a_room_record_it_cannot_report(cli: HostCli) -> None:
    # A rooms file that holds a value no status could report is a file of the host's own that
    # holds something else: it stops the command, as every other such file does.
    good = rooms.Record(pair="pair-abc", made=1.0, held={"room-1": rooms.Held("workspace", 1.0)})
    bad_records = [
        dataclasses.replace(good, held={"room-1": rooms.Held("workspace", 1.0, "no idea")})
    ]
    # A name the pairing library does not allow, as a room held, the room of an accept, or the
    # pair: wrong at the first character, or only further on (a character it does not take, a
    # 33rd, a `-` first).
    for name in ("Room 1", "Pair ABC", "Room!", "Room-1", "room-1!", "room_1", "r" * 33, "-room"):
        bad_records += [
            dataclasses.replace(good, held={name: rooms.Held("workspace", 1.0)}),
            dataclasses.replace(good, pair=name),
            dataclasses.replace(good, accepting=rooms.Accepting("offer", name, ())),
        ]
    paths.ensure_dir(cli.dirs.state)
    for bad in bad_records:
        rooms.save(cli.dirs.rooms_file, bad)
        ran = cli.run("--json", "status")
        assert ran.rc == 1, bad
        assert [found["code"] for found in _objects(ran.out)] == ["state_invalid"]
        assert "not a lucille-host room record" in ran.err
    rooms.save(cli.dirs.rooms_file, good)
    assert cli.run("--json", "status").rc == 0
    # Every name the library allows loads, and is reported as its schema takes it: 32 characters,
    # and a digit first. With no pair held, the rooms are over a pair gone, then the accept's.
    for pair, held, kept in (("p" * 32, "r" * 32, "a" * 32), ("2026-mac", "1room", "9")):
        record = rooms.Record(
            pair=pair,
            made=1.0,
            held={held: rooms.Held("workspace", 1.0)},
            accepting=rooms.Accepting("offer", kept, ()),
        )
        rooms.save(cli.dirs.rooms_file, record)
        ran = cli.run("--json", "status")
        assert ran.rc == 0, ran.err
        (found,) = _objects(ran.out)
        assert [(room["state"], room["name"]) for room in found["work_rooms"]] == [
            ("pair_gone", held),
            ("accept_interrupted", kept),
        ]


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


def test_log_json_lines_oldest_first(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = paths.ensure_dir(paths.dirs().state)
    assert main(["log", "--json"]) == 0
    out, err = capsys.readouterr()
    assert out == "" and "the audit log has no lines yet" in err
    log = AuditLog(folder, max_bytes=700)
    for index in range(30):
        log.write("test", n=index)
    with (folder / "audit.log").open("ab") as handle:
        handle.write(b'{"kind":"x","note":"\x1b[2J\xe2\x80\xae"}\n')
    assert main(["log", "--limit", "3"]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert main(["log", "--limit", "3", "--json"]) == 0
    out = capsys.readouterr().out
    assert out.isascii()
    assert _objects(out) == [{"v": 1, "type": "log_line", "text": line} for line in printed]
    assert [json.loads(found["text"]).get("n") for found in _objects(out)] == [28, 29, None]
    assert _objects(out)[-1]["text"] == '{"kind":"x","note":"?[2J???"}'
    # Across the rotated files, oldest first.
    assert main(["--json", "log", "--limit", "25"]) == 0
    texts = [found["text"] for found in _objects(capsys.readouterr().out)]
    assert [json.loads(text).get("n") for text in texts[:-1]] == list(range(6, 30))
    for bad in ("0", "-1"):
        assert main(["log", "--limit", bad, "--json"]) == 1
        out, err = capsys.readouterr()
        assert [found["code"] for found in _objects(out)] == ["bad_limit"]
        assert "--limit takes a number of lines" in err


# --- forget ---------------------------------------------------------------------------------------


def test_forget_json_says_whether_lucille_was_told(cli: HostCli) -> None:
    ran = cli.run("forget", "--json")
    assert ran.rc == 1
    assert _objects(ran.out) == [
        {"v": 1, "type": "error", "code": "none", "message": "this computer holds no pair"}
    ]
    lucille = cli.lucille()
    cli.paired(lucille)
    assert cli.run("pair", "confirm").rc == 0
    ran = cli.run("--json", "forget")
    assert ran.rc == 0, ran.text
    assert _objects(ran.out) == [
        {"v": 1, "type": "forget", "state": "forgotten", "name": lucille.name, "lucille_told": True}
    ]
    assert cli.held() == []
    # The hub out of reach: forgotten here all the same, and Lucille was not told.
    second = cli.lucille()
    cli.paired(second)
    cli.client_class = Down(cli.h)
    ran = cli.run("forget", "--json")
    assert ran.rc == 0, ran.text
    assert _objects(ran.out) == [
        {"v": 1, "type": "forget", "state": "forgotten", "name": second.name, "lucille_told": False}
    ]
    assert cli.held() == [] and second.resolve().state == "paired"


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
    found = _objects(command("status", "--json").out)[0]
    assert found["pair"] == {"state": "none"}
    assert [(r["name"], r["state"], r.get("why")) for r in found["work_rooms"]] == [
        (room, "abandoned", "pair_over")
    ]
    ran = command("forget", "--json")
    assert ran.rc == 0, ran.err
    assert _objects(ran.out) == [{"v": 1, "type": "forget", "state": "left", "name": host.name}]
    (stored,) = host_env.keyring.items.values()
    assert config.key not in stored and config.write_key not in stored
    record = host.record()
    assert record == rooms.Record(left=record.left, named=record.named)
    assert [gone.room for gone in record.left.values()] == [room]
    assert not host.dirs.cursors_file.exists()
    ran = command("forget")
    assert ran.rc == 1 and ran.err == "none: this computer holds no pair\n"


def test_forget_verb_says_it_left_the_rooms_of_a_pair_the_library_dropped(
    host: RunHost, command: Commands, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The text of `forget` once the library dropped the pair and a room could not be left then.
    host.start()
    host.offer("work-1")
    host.stop()
    kept = host.opened.pairs.get(host.name)
    assert kept is not None
    host.opened.forget(host.name)
    host.opened.pairs.save(kept)
    real = _switchboard.Link.leave_room

    def cannot(opened: _switchboard.Link, name: str) -> _switchboard.RoomLeft:
        raise _switchboard.RoomError("store", "the share store could not be written")

    monkeypatch.setattr(_switchboard.Link, "leave_room", cannot)
    ran = command("pair", "status", "--json")
    assert ran.rc == 1
    assert [found["code"] for found in _objects(ran.out)] == ["room_not_left"]
    monkeypatch.setattr(_switchboard.Link, "leave_room", real)
    ran = command("forget")
    assert ran.rc == 0, ran.err
    assert ran.out == (
        f"left: pair {host.name} was gone from this computer already, and the work rooms still "
        "held over it are left now\n"
    )


def test_forget_json_says_a_room_could_not_be_left(
    host: RunHost, command: Commands, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.start()
    host.offer("work-1")
    host.stop()
    real = _switchboard.Link.leave_room

    def cannot(opened: _switchboard.Link, name: str) -> _switchboard.RoomLeft:
        raise _switchboard.RoomError("store", "the share store could not be written")

    # The pair still held: it is kept, and the same command can be tried again.
    monkeypatch.setattr(_switchboard.Link, "leave_room", cannot)
    kept = (
        f"pair {host.name} is kept on this computer: a work room held over it could not be left, "
        "and its keys would stay behind; try again"
    )
    ran = command("forget", "--json")
    assert ran.rc == 1 and ran.err == f"error: {kept}\n"
    assert _objects(ran.out) == [
        {"v": 1, "type": "error", "code": "room_not_left", "message": kept}
    ]
    assert host.opened.names() == [host.name]

    # The leave has the library drop the pair, and then fails: the pair is gone here, and
    # `forget` leaves the room.
    def dropped(opened: _switchboard.Link, name: str) -> _switchboard.RoomLeft:
        with host.opened.pairs.lock():
            host.opened.pairs.delete(host.name)
        return cannot(opened, name)

    monkeypatch.setattr(_switchboard.Link, "leave_room", dropped)
    stranded = (
        f"pair {host.name} is gone from this computer, and a work room held over it could not be "
        "left: its keys stay in the share store until `lucille-host forget` leaves it"
    )
    ran = command("--json", "forget")
    assert ran.rc == 1 and ran.err == f"error: {stranded}\n"
    assert _objects(ran.out) == [
        {"v": 1, "type": "error", "code": "room_not_left", "message": stranded}
    ]
    assert host.opened.names() == [] and host.gate() is None
    monkeypatch.setattr(_switchboard.Link, "leave_room", real)
    ran = command("--json", "forget")
    assert ran.rc == 0, ran.err
    assert _objects(ran.out) == [{"v": 1, "type": "forget", "state": "left", "name": host.name}]


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
