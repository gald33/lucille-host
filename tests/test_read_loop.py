"""``lucille-host run``: the read loop, requests and replies (spec/PROTOCOL.md sections 5 to 16).

Each test runs the host's own :class:`lucille_host.loop.Runner` on a thread, with real threads and
real long-polls, against Switchboard's in-process test hub and a fake Lucille that offers work
rooms, posts requests and reads the replies (``tests/_run.py``). Long-polls hold for a fraction
of a second here; a test that needs longer says so.
"""

from __future__ import annotations

import collections
import contextlib
import datetime
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from switchboard import pairing, sharing, signing
from switchboard.testing import hub

from lucille_host import _switchboard, cli, jobs, link, loop
from lucille_host.controls import Controls
from tests import _audit_hook
from tests._host import HostEnv
from tests._lucille import EXEC, FakeLucille
from tests._run import RunHost, body, counting, fast, until

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)

# How long a test watches for something that must not happen.
QUIET = 1.5


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
def host(host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunHost:
    fast(monkeypatch)
    return RunHost(h, host_env, tmp_path)


def _job_ids(lines: list[dict[str, Any]]) -> list[Any]:
    return [line.get("job_id") for line in lines]


def _failing_posts(monkeypatch: pytest.MonkeyPatch, failing: list[bool]) -> list[float]:
    """Make the host's posts fail while ``failing[0]`` is true, as a hub outage would; returns
    the times of the failed attempts."""
    attempts: list[float] = []
    real = _switchboard.RoomClient.post

    def post(client: _switchboard.RoomClient, channel: str, sent: Any, **options: Any) -> Any:
        if failing[0]:
            attempts.append(time.monotonic())
            raise _switchboard.HubError("the hub could not be reached (ConnectError)")
        return real(client, channel, sent, **options)

    monkeypatch.setattr(_switchboard.RoomClient, "post", post)
    return attempts


def _plant_unopenable(lucille: FakeLucille, room: str) -> int:
    """Post, in ``room``, a body that no holder of the room's key can open: a writer that drops
    the room's key posts its body unsealed, on the channel the host reads. Returns its seq."""
    with lucille.client(room) as sealed:
        hub_channel = sealed.cipher.blind_channel(EXEC)
    with lucille.client(room, agent_id="stranger", key="") as plain:
        return int(plain.post(hub_channel, {"garbage": True}, ttl=600)["seq"])


def _nested(depth: int) -> Any:
    """A list nested ``depth`` levels deep."""
    value: Any = []
    for _ in range(depth - 1):
        value = [value]
    return value


def _plant_unparsable(host: RunHost, kind: str) -> int:
    """Post in work-1 a message whose body the room's key opens but the host cannot read: not
    JSON, or a request nested deeper than the host reads. Returns its seq."""
    if kind == "not json":
        return host.lucille.post_unparsable(host.lucille.room_config("work-1"), EXEC)
    # A request, its args 2 levels below the body, nested so that the body is ``depth`` deep.
    depth = {"nested 65 deep": 65, "nested past the parser": 3000}[kind]
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(20000)
    try:
        return host.lucille.request("work-1", body(args={"a": _nested(depth - 2)}))
    finally:
        sys.setrecursionlimit(limit)


def _ttl(row: dict[str, Any]) -> float:
    """The TTL a message was posted with, as the hub keeps it."""

    def stamp(text: str) -> float:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()

    return stamp(row["expires_at"]) - stamp(row["created_at"])


def _status(host: RunHost) -> dict[str, Any]:
    return json.loads(host.dirs.status_file.read_text())


# --- the numbers --------------------------------------------------------------------------------


def test_the_loop_s_numbers_are_the_specs() -> None:
    assert link.LONG_POLL == 25
    assert loop.PAGE == 200
    assert loop.RECOVER == 1000
    assert _switchboard.CLIENT_TIMEOUT == 40
    assert loop.FLUSH == 30
    assert loop.WAKE_GAP == 30
    assert _switchboard.EXEC_CHANNEL == "lucille.exec"
    assert _switchboard.PAIR_CHANNEL == pairing.CHANNEL


# --- reading ------------------------------------------------------------------------------------


def test_inbox_always_has_since(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast(monkeypatch)
    calls: list[tuple[Any, Any, dict[str, Any]]] = []
    built = h.client_class()

    class Spy(built):  # type: ignore[misc, valid-type]
        def __init__(self, config: Any = None, *, agent_id: Any = None, **options: Any) -> None:
            super().__init__(config, agent_id=agent_id, **options)
            self.spied = (agent_id, options.get("timeout"))

        def inbox(self, **options: Any) -> Any:
            calls.append((*self.spied, dict(options, workspace=self.workspace)))
            return super().inbox(**options)

    host = RunHost(h, host_env, tmp_path, client_class=Spy)
    host.start()
    host.offer("work-1")
    job = host.request("work-1")
    host.reply("work-1", job)
    host.stop()
    # The host's own reads: every client it builds as itself, never the library's own ones.
    mine = [(timeout, options) for agent, timeout, options in calls if agent == "lucille-host"]
    room = host.lucille.room_config("work-1").workspace
    read = {options["workspace"] for _, options in mine}
    assert room in read and len(read) == 2, "the room, and pair:<name>"
    for timeout, options in mine:
        assert options["since"] is not None, options
        assert options["wait"] == link.LONG_POLL
        assert options["limit"] == loop.PAGE
        assert timeout == _switchboard.CLIENT_TIMEOUT
        assert len(options["channels"]) == 1


def test_cursor_never_skips_full_page(host: RunHost, monkeypatch: pytest.MonkeyPatch) -> None:
    def message(seq: int, channel: str) -> _switchboard.Message:
        return _switchboard.Message(seq, channel, False, "note", None, {})

    a, b = "lucille.exec", "other"
    # Channel a came back full at 6 and may hold more past it; b did not. Past 6 is unread.
    page = [message(1, a), message(2, b), message(4, a), message(6, a), message(7, b)]
    assert loop.next_cursor(page, [a, b], 3, 0) == 6
    # Both full: the lower of the two last ones.
    assert loop.next_cursor(page, [a, b], 2, 0) == 6
    assert loop.next_cursor(page[:4], [a, b], 1, 0) == 2
    # None full: the highest returned.
    assert loop.next_cursor(page, [a, b], 4, 0) == 7
    assert loop.next_cursor([], [a], 3, 5) == 5
    assert loop.next_cursor([message(3, a)], [a], 3, 5) == 5
    # Through the loop: a room filled past several pages before the host reads it.
    monkeypatch.setattr("lucille_host.loop.PAGE", 3)
    host.lucille.offer("work-1")
    sent = [host.request("work-1") for _ in range(8)]
    host.start()
    for job in sent:
        host.reply("work-1", job)
    host.stop()
    assert _job_ids(host.events("received")) == sent


def test_cursor_persisted_after_audit(host: RunHost, monkeypatch: pytest.MonkeyPatch) -> None:
    problems: list[str] = []
    written: list[int] = []
    real = loop.Cursors.set

    def checked(cursors: loop.Cursors, workspace: str, seq: int) -> None:
        lines = [
            line
            for line in host.audit()
            if line.get("seq") == seq and line["kind"] in ("received", "ignored", "unreadable")
        ]
        if not lines:
            problems.append(f"cursor {seq} written before its audit line")
        for line in lines:
            if line["kind"] == "received":
                entry = host.journal().get(line["job_id"])
                if entry is None or entry.decision is None:
                    problems.append(f"cursor {seq} written before the journal held its decision")
        written.append(seq)
        real(cursors, workspace, seq)

    monkeypatch.setattr(loop.Cursors, "set", checked)
    host.start()
    host.offer("work-1")
    ignored = host.lucille.request("work-1", {"dir": "reply", "job_id": "x"})
    job = host.request("work-1")
    host.reply("work-1", job)
    host.stop()
    assert problems == []
    assert ignored in written and len(written) == 2
    saved = json.loads(host.dirs.cursors_file.read_text())
    assert saved["rooms"] == {host.lucille.room_config("work-1").workspace: max(written)}


def test_unopenable_message_skipped_not_lost(
    host: RunHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Before the host reads the room: a request, a message nobody can open, another request.
    host.lucille.offer("work-1")
    first = host.request("work-1")
    planted = _plant_unopenable(host.lucille, "work-1")
    second = host.request("work-1")
    host.start()
    host.reply("work-1", first)
    host.reply("work-1", second)
    kinds = [
        (line["kind"], line.get("job_id") or line.get("seq"))
        for line in host.audit()
        if line["kind"] in ("received", "unreadable")
    ]
    assert kinds == [("received", first), ("unreadable", planted), ("received", second)]
    # An unopenable message the recovery read does not show above the cursor: read_stuck, logged
    # once, and the cursor does not move past what is unread.
    stuck = [True]
    real = _switchboard.RoomClient.recover

    def recover(client: _switchboard.RoomClient, channel: str, **options: Any) -> Any:
        if stuck[0]:
            return []
        return real(client, channel, **options)

    monkeypatch.setattr(_switchboard.RoomClient, "recover", recover)
    again = _plant_unopenable(host.lucille, "work-1")
    third = host.request("work-1")
    until(lambda: host.events("read_stuck"), what="read_stuck")
    until(lambda: _status(host)["read_stuck"] == ["room-1"], what="status naming the room")
    time.sleep(QUIET / 3)
    assert host.lucille.reply("work-1", third) is None
    assert len(host.events("read_stuck")) == 1
    stuck[0] = False
    host.reply("work-1", third)
    host.stop()
    assert [line["seq"] for line in host.events("unreadable")] == [planted, again]


@pytest.mark.parametrize("kind", ["not json", "nested 65 deep", "nested past the parser"])
def test_unparsable_message_skipped_not_lost(host: RunHost, kind: str) -> None:
    # A body that opens with the room's key but cannot be read: before, it stopped `run`, and
    # every start after, until it expired on the hub.
    host.lucille.offer("work-1")
    first = host.request("work-1")
    planted = _plant_unparsable(host, kind)
    second = host.request("work-1")
    host.start()
    host.reply("work-1", first)
    host.reply("work-1", second)
    assert host.running
    host.stop()
    kinds = [
        (line["kind"], line.get("job_id") or line.get("seq"))
        for line in host.audit()
        if line["kind"] in ("received", "unreadable")
    ]
    assert kinds == [("received", first), ("unreadable", planted), ("received", second)]


def test_a_body_nested_64_deep_is_read(host: RunHost) -> None:
    host.lucille.offer("work-1")
    job = host.request("work-1", body(args={"a": _nested(62)}))
    host.start()
    reply = host.reply("work-1", job)
    host.stop()
    assert reply["body"]["status"] == "refused"
    assert reply["body"]["code"] == "bad_args"
    assert not host.events("unreadable")


def test_the_nesting_bound() -> None:
    assert _switchboard.NEST_MOST == 64
    deeper = _switchboard._deeper
    assert not deeper(1, 0) and not deeper("a", 0)
    assert deeper([], 0) and not deeper([], 1) and not deeper({}, 1)
    assert not deeper({"a": [1, {"b": 2}]}, 3) and deeper({"a": [1, {"b": {}}]}, 3)
    assert not deeper(_nested(64), 64) and deeper(_nested(65), 64)
    # Found without recursion, so far past any recursion limit.
    assert deeper(_nested(200000), 64)
    assert not deeper([[1]] * 1000, 2)


def test_unparsable_note_on_the_pair_channel_skipped(host: RunHost) -> None:
    host.start()
    until(lambda: host.runner is not None and host.runner._pair is not None, what="pair reader")
    pair_room = host.lucille.resolve().config
    planted = host.lucille.post_unparsable(pair_room, pairing.CHANNEL)
    until(lambda: host.events("unreadable", room="pair"), what="the unreadable note logged")
    # The library's notes after it still wake the host: an offer is taken.
    host.offer("work-1")
    assert host.running
    host.stop()
    assert [line["seq"] for line in host.events("unreadable", room="pair")] == [planted]


def test_long_poll_per_room_concurrent(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast(monkeypatch)
    monkeypatch.setattr("lucille_host.link.LONG_POLL", 3.0)
    polls: list[tuple[str, float, float]] = []
    built = h.client_class()

    class Spy(built):  # type: ignore[misc, valid-type]
        def inbox(self, **options: Any) -> Any:
            started = time.monotonic()
            try:
                return super().inbox(**options)
            finally:
                if options.get("wait"):
                    polls.append((self.workspace, started, time.monotonic()))

    host = RunHost(h, host_env, tmp_path, client_class=Spy)
    host.start()
    host.offer("work-1")
    time.sleep(0.5)
    # Both long-polls are parked. A request in the room is handled at once, not once the pair's
    # long-poll ends.
    posted = time.monotonic()
    job = host.request("work-1")
    host.reply("work-1", job, timeout=10)
    assert time.monotonic() - posted < 2.0
    # And the library's note on the pair channel is handled while the room's long-poll waits.
    time.sleep(0.5)
    offered = time.monotonic()
    host.offer("work-2")
    assert time.monotonic() - offered < 2.5
    host.stop()
    room = host.lucille.room_config("work-1").workspace
    on_room = [(start, end) for ws, start, end in polls if ws == room]
    elsewhere = [(start, end) for ws, start, end in polls if ws != room]
    assert any(
        start < other_end and other_start < end
        for start, end in on_room
        for other_start, other_end in elsewhere
    ), "the room's long-poll and another were in flight at once"


def test_backoff_honours_retry_after(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backoff = loop.Backoff()
    assert backoff.next(retry_after=7.5) == 7.5
    backoff.reset()
    for doubled in (1, 2, 4, 8, 16, 30, 30, 30):
        assert doubled <= backoff.next() <= doubled * 1.25
    backoff.reset()
    assert backoff.next() <= 1.25
    # Through the loop: a 429 with retry_after holds the room's reader that long, and asks main
    # for a resolve.
    fast(monkeypatch)
    host = RunHost(h, host_env, tmp_path)
    errors = counting(monkeypatch, link.Session, "hub_error")
    peeks: list[float] = []
    busy = [True]
    real = _switchboard.RoomClient.peek

    def peek(client: _switchboard.RoomClient, channel: str, **options: Any) -> Any:
        if channel == EXEC:
            peeks.append(time.monotonic())
            if busy[0]:
                busy[0] = False
                raise _switchboard.HubError("the hub answered 429", status=429, retry_after=0.8)
        return real(client, channel, **options)

    monkeypatch.setattr(_switchboard.RoomClient, "peek", peek)
    host.start()
    host.offer("work-1")
    until(lambda: len(peeks) >= 2, what="a second read")
    host.stop()
    assert peeks[1] - peeks[0] >= 0.8
    assert errors["hub_error"] >= 1


# --- what is acted on ---------------------------------------------------------------------------


def test_acts_only_in_accepted_rooms(host: RunHost, monkeypatch: pytest.MonkeyPatch) -> None:
    host.start()
    host.offer("work-1")
    answered = host.request("work-1")
    # On the pair channel of the room itself.
    on_pair = host.request("work-1", channel="pair")
    # In pair:<name>, on the protocol's channel.
    lucille = host.lucille
    resolution = pairing.resolve(
        lucille.name, lucille.base, store=lucille.pairs, connect=lucille.connect, now=host.h.now
    )
    in_pair_room = body()
    with lucille.connect(resolution.config, agent_id="lucille") as client:
        client.post(EXEC, in_pair_room, ttl=600)
    # In a room offered over the pair and not accepted (check 5 refuses it, here at one room).
    monkeypatch.setattr("lucille_host.rooms.CAP", 1)
    lucille.offer("work-2")
    not_taken = host.request("work-2")
    host.reply("work-1", answered)
    until(lambda: host.events("offer", event="refused", check="check 5"), what="check 5")
    time.sleep(QUIET)
    host.stop()
    assert host.lucille.reply("work-1", on_pair) is None
    assert host.lucille.reply("work-2", not_taken) is None
    assert _job_ids(host.events("received")) == [answered]
    assert sorted(host.record().held) == ["room-1"]
    assert in_pair_room["job_id"] not in json.dumps(host.audit())


def test_own_posts_ignored(host: RunHost) -> None:
    host.lucille.offer("work-1")
    # A request under the host's own id. The hub leaves it out of the host's reads; the recovery
    # read, which a message nobody can open forces here, returns it.
    mine = host.request("work-1", agent_id="lucille-host")
    _plant_unopenable(host.lucille, "work-1")
    answered = host.request("work-1")
    host.start()
    host.reply("work-1", answered)
    host.stop()
    assert host.lucille.reply("work-1", mine) is None
    assert [line["why"] for line in host.events("ignored")] == ["own"]
    assert _job_ids(host.events("received")) == [answered]


def test_non_note_or_threaded_ignored(host: RunHost) -> None:
    host.start()
    host.offer("work-1")
    other_type = host.request("work-1", kind="status")
    threaded = host.request("work-1", thread="t-1")
    answered = host.request("work-1")
    host.reply("work-1", answered)
    host.stop()
    assert host.lucille.reply("work-1", other_type) is None
    assert host.lucille.reply("work-1", threaded) is None
    assert [line["why"] for line in host.events("ignored")] == ["not_note", "threaded"]
    assert _job_ids(host.events("received")) == [answered]


def test_refusal_order(host: RunHost) -> None:
    host.enable("files.sha256")
    host.start()
    host.offer("work-1")
    now = int(time.time())
    duplicate = body()
    first: list[tuple[dict[str, Any], str | None]] = [
        # host_too_old comes before the envelope check.
        (body(min_host_version="99.0.0", argv=["x"]), "host_too_old"),
        # bad_request before expired.
        (body(not_after=now - 10, exe="/bin/sh"), "bad_request"),
        # A malformed min_host_version is bad_request, not host_too_old.
        (body(min_host_version="01.0.0"), "bad_request"),
        (duplicate, None),
    ]
    for sent, _ in first:
        host.request("work-1", sent)
    host.reply("work-1", duplicate["job_id"])
    # A duplicate gets nothing, even with a min_host_version this host is older than.
    host.request("work-1", dict(duplicate, min_host_version="99.0.0"))
    # A body that is not a request gets nothing, whatever else it carries.
    not_request = dict(body(min_host_version="99.0.0"), dir="reply")
    host.request("work-1", not_request)
    # expired before paused; paused before unknown_op.
    Controls(host.dirs.state).pause()
    late = body(op="host.sleep", args={"n": 1}, not_after=now - 10)
    paused = body(op="host.nothing")
    host.request("work-1", late)
    host.request("work-1", paused)
    host.reply("work-1", paused["job_id"])
    Controls(host.dirs.state).resume()
    later: list[tuple[dict[str, Any], str]] = [
        # unknown_op (a well-formed op outside the catalog) before path_outside_root.
        (body(op="host.nothing", args={"root": "nowhere", "path": "/x"}), "unknown_op"),
        # not_enabled before bad_args.
        (body(op="host.sleep", args={"n": 0}), "not_enabled"),
        # bad_args before path_outside_root.
        (body(op="files.sha256", args={"root": "Nowhere", "path": "/x"}), "bad_args"),
        (body(op="files.sha256", args={"root": "nowhere", "path": "x"}), "path_outside_root"),
    ]
    for sent, _ in later:
        host.request("work-1", sent)
    for sent, code in [*first, (late, "expired"), (paused, "paused"), *later]:
        if code is None:
            continue
        reply = host.reply("work-1", sent["job_id"])["body"]
        assert (reply["status"], reply.get("code")) == ("refused", code), sent
    time.sleep(QUIET / 3)
    host.stop()
    replies = [row["body"]["job_id"] for row in host.lucille.replies("work-1")]
    assert replies.count(duplicate["job_id"]) == 1
    assert not_request["job_id"] not in replies
    # Nothing refused started a process.
    assert host.events("start") == []


def test_duplicate_runs_once(host: RunHost) -> None:
    host.start()
    host.offer("work-1")
    sent = body()
    host.request("work-1", sent)
    host.request("work-1", sent)
    host.reply("work-1", sent["job_id"])
    until(lambda: host.events("ignored", why="duplicate"), what="the duplicate")
    # The same job id in another room: a newer room, which replaces the first.
    host.offer("work-2")
    host.request("work-2", sent)
    until(lambda: len(host.events("ignored", why="duplicate")) == 2, what="the second duplicate")
    time.sleep(QUIET / 3)
    host.stop()
    assert len(host.lucille.replies("work-1")) == 1
    assert host.lucille.replies("work-2") == []
    assert _job_ids(host.events("received")) == [sent["job_id"]]
    rooms_seen = [line["room"] for line in host.events("ignored", why="duplicate")]
    assert rooms_seen == ["room-1", "room-2"]


def test_dedupe_prune_time(host: RunHost) -> None:
    host.start()
    host.offer("work-1")
    now = int(time.time())
    valid = body(not_after=now + 100)
    past = body(not_after=now - 50)
    invalid = body(not_after=1.5)
    for sent in (valid, past, invalid):
        host.request("work-1", sent)
    for sent in (valid, past, invalid):
        host.reply("work-1", sent["job_id"])
    host.stop()
    journal = host.journal()
    entries = {sent["job_id"]: journal.get(sent["job_id"]) for sent in (valid, past, invalid)}
    for sent in (valid, past):
        entry = entries[sent["job_id"]]
        assert entry is not None and entry.not_after == sent["not_after"]
        assert entry.prune_at == max(sent["not_after"], entry.received_at) + 900
    # Received after its not_after: kept 15 minutes past the receipt, not pruned at once.
    late = entries[past["job_id"]]
    assert late is not None and late.prune_at == late.received_at + 900
    gone = entries[invalid["job_id"]]
    assert gone is not None and gone.not_after is None
    assert gone.prune_at == gone.received_at + 86400 + 900
    # The journal forgets an entry only past its prune time, when it is next written.
    first = entries[valid["job_id"]]
    assert first is not None
    host.offset = first.prune_at - time.time() - 1
    other = "00000000-0000-4000-8000-000000000001"
    journal.received(other, room="r", seq=1, received_at=host.now(), not_after=None, body={})
    assert journal.seen(valid["job_id"])
    host.offset += 2
    journal.sent(other)
    assert not journal.seen(valid["job_id"]) and not journal.seen(past["job_id"])
    assert journal.seen(invalid["job_id"])


# --- restarts -----------------------------------------------------------------------------------


def _unsent(host: RunHost, monkeypatch: pytest.MonkeyPatch, sent: dict[str, Any]) -> None:
    """Run until ``sent`` is decided and its reply cannot be posted, then stop: the journal holds
    the decision and no record that the reply was sent."""
    monkeypatch.setattr("lucille_host.loop.FLUSH", 0.3)
    failing = [True]
    attempts = _failing_posts(monkeypatch, failing)
    host.start()
    host.offer("work-1")
    host.request("work-1", sent)
    until(lambda: attempts, what="a failed post")
    host.stop()
    failing[0] = False
    entry = host.journal().get(sent["job_id"])
    assert entry is not None and entry.decision is not None and not entry.sent


def test_restart_gives_interrupted(host: RunHost, monkeypatch: pytest.MonkeyPatch) -> None:
    host.enable("host.sleep")
    sent = body(op="host.sleep", args={"n": 1})
    _unsent(host, monkeypatch, sent)
    assert host.events("execution", job_id=sent["job_id"])[0]["ended"] == "exited"
    host.start()
    reply = host.reply("work-1", sent["job_id"])["body"]
    host.stop()
    assert (reply["status"], reply["code"], reply["duration_ms"]) == ("failed", "interrupted", 0)
    # Never a second run: one start, and the request read again is a duplicate.
    assert len(host.events("start", job_id=sent["job_id"])) == 1
    assert len(host.events("decision", job_id=sent["job_id"])) == 1
    assert host.events("restart", job_id=sent["job_id"])[0]["reply"] == "interrupted"
    entry = host.journal().get(sent["job_id"])
    assert entry is not None and entry.sent


def test_restart_refusal_reposted_unchanged(host: RunHost, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = body(op="host.sleep", args={"n": 1})
    _unsent(host, monkeypatch, sent)
    entry = host.journal().get(sent["job_id"])
    assert entry is not None and entry.reply is not None
    stored = entry.reply.encode("ascii")
    first_sha = host.events("sent", job_id=sent["job_id"])[0]["sha256"]
    host.start()
    reply = host.reply("work-1", sent["job_id"])["body"]
    host.stop()
    assert reply["code"] == "not_enabled"
    assert jobs.encode(reply) == stored
    assert hashlib.sha256(stored).hexdigest() == first_sha
    assert len(host.events("decision", job_id=sent["job_id"])) == 1
    shas = [line["sha256"] for line in host.events("sent", job_id=sent["job_id"])]
    assert shas == [first_sha, first_sha]


def test_restart_decides_received_request_afresh(
    host: RunHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Stop(Exception):
        pass

    def stop(*_: Any, **__: Any) -> bytes:
        raise Stop

    # Valid had it been received 10 s later; as received, past the not_after range.
    sent = body(not_after=int(time.time()) + 86400 + 5)
    with monkeypatch.context() as patch:
        patch.setattr("lucille_host.jobs.process", stop)
        host.start()
        host.offer("work-1")
        host.request("work-1", sent)
        with pytest.raises(loop.Failed):
            host.wait()
    entry = host.journal().get(sent["job_id"])
    assert entry is not None and entry.decision is None and entry.body == sent
    host.offset = 10.0
    host.start()
    reply = host.reply("work-1", sent["job_id"])["body"]
    time.sleep(QUIET / 3)
    host.stop()
    # Decided as received the first time, not as received again.
    assert (reply["status"], reply["code"]) == ("refused", "bad_request")
    assert host.events("restart", job_id=sent["job_id"])[0]["reply"] == "decide"
    assert len(host.lucille.replies("work-1")) == 1
    assert host.events("ignored", job_id=sent["job_id"])[0]["why"] == "duplicate"
    # A request received before a stop, whose not_after passed while the host was stopped: it is
    # judged expired when it is decided, not when it was received.
    late = body(not_after=int(host.now()) - 5)
    host.journal().received(
        late["job_id"],
        room=entry.room,
        seq=entry.seq + 1,
        received_at=host.now() - 60,
        not_after=late["not_after"],
        body=late,
    )
    host.start()
    expired = host.reply("work-1", late["job_id"])["body"]
    host.stop()
    assert (expired["status"], expired["code"]) == ("refused", "expired")


def test_no_post_in_abandoned_room_after_restart(
    host: RunHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("lucille_host.loop.FLUSH", 0.3)
    failing = [True]
    attempts = _failing_posts(monkeypatch, failing)
    host.start()
    host.offer("work-1")
    refused = body(op="host.sleep", args={"n": 1})
    ran = body()
    host.request("work-1", refused)
    host.request("work-1", ran)
    until(lambda: len(attempts) >= 2, what="two failed posts")
    host.stop()
    failing[0] = False
    pending = [job for job, _ in host.journal().pending()]
    assert pending == [refused["job_id"], ran["job_id"]]
    # And a refusal not sent in a room this host never reads again: it is posted in no other room.
    stray = body()
    host.journal().received(
        stray["job_id"], room="ws_elsewhere", seq=1, received_at=host.now(), not_after=None, body={}
    )
    outcome = jobs.Outcome(status="refused", code="paused")
    host.journal().decided(
        stray["job_id"], "refused", jobs.encode(jobs.reply_body(stray["job_id"], outcome, "0" * 64))
    )
    # While the host is stopped, Lucille heals the pair: the room counts as leaked.
    host.lucille.heal()
    host.start()
    until(lambda: host.record().held == {}, what="the room left")
    host.lucille.resolve()
    host.offer("work-2")
    time.sleep(QUIET / 3)
    host.stop()
    assert host.lucille.replies("work-1") == []
    assert host.lucille.replies("work-2") == []
    assert [job for job, _ in host.journal().pending()] == [stray["job_id"]]
    assert host.events("restart") == []
    dropped = host.events("reply", event="not_posted", why="abandoned")
    assert sorted(_job_ids(dropped)) == sorted([refused["job_id"], ran["job_id"]])


# --- replies ------------------------------------------------------------------------------------


def test_reply_ttl_explicit(host: RunHost, monkeypatch: pytest.MonkeyPatch) -> None:
    given: list[Any] = []
    real = _switchboard.RoomClient.post

    def post(client: _switchboard.RoomClient, channel: str, sent: Any, **options: Any) -> Any:
        given.append(options.get("ttl"))
        return real(client, channel, sent, **options)

    monkeypatch.setattr(_switchboard.RoomClient, "post", post)
    host.start()
    host.offer("work-1")
    soon = body(not_after=int(time.time()) + 120)
    far = body(not_after=int(time.time()) + 86000)
    before = time.time()
    host.request("work-1", soon)
    host.request("work-1", far)
    near = host.reply("work-1", soon["job_id"])
    after = time.time()
    capped = host.reply("work-1", far["job_id"])
    host.stop()
    assert len(given) == 2 and all(isinstance(ttl, int) for ttl in given)
    # (not_after - now) + 900, in whole seconds, as posted and as the hub keeps it.
    assert soon["not_after"] - after + 899 <= _ttl(near) <= soon["not_after"] - before + 900
    # At most a day.
    assert _ttl(capped) == 86400
    posted = {line["job_id"]: line["ttl"] for line in host.events("reply", event="posted")}
    assert posted[soon["job_id"]] == round(_ttl(near))


def test_reply_ttl_without_valid_not_after(host: RunHost) -> None:
    host.start()
    host.offer("work-1")
    missing = body()
    del missing["not_after"]
    fraction = body(not_after=float(int(time.time()) + 300))
    # Valid, but long past: its reply would live less than a second, so it is not posted.
    gone = body(not_after=int(time.time()) - 950)
    before = time.time()
    for sent in (missing, fraction, gone):
        host.request("work-1", sent)
    replies = [host.reply("work-1", sent["job_id"]) for sent in (missing, fraction)]
    until(lambda: host.events("reply", event="not_posted", job_id=gone["job_id"]), what="ttl")
    after = time.time()
    host.stop()
    for row in replies:
        assert (row["body"]["status"], row["body"]["code"]) == ("refused", "bad_request")
        # (received - now) + 900: at most 900 s.
        assert 900 - (after - before) - 1 <= _ttl(row) <= 900
    assert host.lucille.reply("work-1", gone["job_id"]) is None
    assert host.events("reply", event="not_posted", job_id=gone["job_id"])[0]["why"] == "ttl"
    assert host.events("decision", job_id=gone["job_id"])[0]["code"] == "expired"


def test_reply_retried_until_ttl(host: RunHost, monkeypatch: pytest.MonkeyPatch) -> None:
    host.enable("host.sleep")
    failing = [True]
    attempts = _failing_posts(monkeypatch, failing)
    real = _switchboard.RoomClient.peek

    def peek(client: _switchboard.RoomClient, channel: str, **options: Any) -> Any:
        # The hub fails the pair's reads too, while the child runs: the child is not stopped.
        if failing[0] and channel == _switchboard.PAIR_CHANNEL:
            raise _switchboard.HubError("the hub answered 502", status=502)
        return real(client, channel, **options)

    host.start()
    host.offer("work-1")
    monkeypatch.setattr(_switchboard.RoomClient, "peek", peek)
    slept = body(op="host.sleep", args={"n": 1})
    host.request("work-1", slept)
    until(lambda: len(attempts) >= 3, what="three failed posts")
    failing[0] = False
    reply = host.reply("work-1", slept["job_id"])["body"]
    assert (reply["status"], reply["exit_code"]) == ("done", 0)
    assert len(host.events("reply", event="retry", job_id=slept["job_id"])) >= 3
    # A reply whose TTL runs out while its posts fail is given up, never turned into `failed`.
    failing[0] = True
    late = body(not_after=int(time.time()) - 898)
    host.request("work-1", late)
    until(lambda: host.events("reply", event="not_posted", job_id=late["job_id"]), what="ttl")
    failing[0] = False
    host.stop()
    assert host.events("reply", event="not_posted", job_id=late["job_id"])[0]["why"] == "ttl"
    assert host.lucille.reply("work-1", late["job_id"]) is None
    assert all(row["body"]["status"] != "failed" for row in host.lucille.replies("work-1"))


def test_reply_is_note_without_thread(host: RunHost) -> None:
    host.start()
    host.offer("work-1")
    row = host.reply("work-1", host.request("work-1"))
    host.stop()
    assert (row["type"], row["thread"], row["channel"]) == ("note", None, EXEC)
    assert row["body"]["dir"] == "reply"


# --- rooms that move ----------------------------------------------------------------------------


def test_cursor_resets_on_move(host: RunHost) -> None:
    host.start()
    host.offer("work-1")
    host.reply("work-1", host.request("work-1"))
    # Lucille heals the pair, offers the replacement room and posts in it before the host has
    # accepted it.
    host.lucille.heal()
    host.lucille.offer("work-2")
    early = host.request("work-2")
    host.reply("work-2", early)
    host.stop()
    held = host.record().held
    assert list(held) == ["room-2"]
    workspace = held["room-2"].workspace
    cursors = json.loads(host.dirs.cursors_file.read_text())["rooms"]
    assert list(cursors) == [workspace], "the old room's cursor is gone"
    reads = [line for line in host.events("received") if line["job_id"] == early]
    assert reads and reads[0]["seq"] <= cursors[workspace]


def test_abandon_kills_without_reply(host: RunHost, monkeypatch: pytest.MonkeyPatch) -> None:
    host.enable("host.sleep")
    # A kill is noticed up to a second late here, so the leave would come before the kill's end if
    # abandoning did not wait for it.
    monkeypatch.setattr("lucille_host._exec.POLL_S", 1.0)
    kills = counting(monkeypatch, Controls, "kill_done")
    host.start()
    host.offer("work-1")
    sent = body(op="host.sleep", args={"n": 60})
    host.request("work-1", sent)
    until(lambda: host.events("start", job_id=sent["job_id"]), what="the child started")
    started = time.monotonic()
    host.lucille.heal()
    until(lambda: host.events("execution", job_id=sent["job_id"]), what="the child ended")
    assert time.monotonic() - started < 15
    until(lambda: host.record().held == {}, what="the room left")
    time.sleep(QUIET / 3)
    host.stop()
    execution = host.events("execution", job_id=sent["job_id"])[0]
    assert (execution["ended"], execution["abandoned"]) == ("killed", True)
    assert host.events("sent", job_id=sent["job_id"]) == []
    assert host.lucille.replies("work-1") == []
    assert kills["kill_done"] == 0
    entry = host.journal().get(sent["job_id"])
    assert entry is not None and entry.abandoned
    # The abandonment was logged before the kill, and the leave after it.
    order = [
        (line["kind"], line.get("event"))
        for line in host.audit()
        if line["kind"] == "execution" or (line["kind"] == "room" and line["room"] == "room-1")
    ]
    assert order[-3:] == [("room", "abandoned"), ("execution", None), ("room", "left")]


def test_pause_kill_drain_in_run(host: RunHost) -> None:
    host.enable("host.sleep")
    controls = Controls(host.dirs.state)
    host.start()
    host.offer("work-1")
    # Paused: refused, and nothing runs.
    controls.pause()
    paused = host.request("work-1", body(op="host.sleep", args={"n": 1}))
    assert host.reply("work-1", paused)["body"]["code"] == "paused"
    controls.resume()
    # Kill: the running child's group is killed, the reply says so, and the host is paused.
    killed = body(op="host.sleep", args={"n": 60})
    host.request("work-1", killed)
    until(lambda: host.events("start", job_id=killed["job_id"]), what="the child started")
    controls.kill()
    reply = host.reply("work-1", killed["job_id"])["body"]
    assert (reply["status"], reply["code"]) == ("failed", "killed_locally")
    assert controls.paused()
    controls.resume()
    # Drain: the running operation finishes and its reply is posted; nothing new starts. Two
    # requests posted while another runs are read together; the drain begins while the first
    # of them runs, and the second is neither decided nor written down.
    first = body(op="host.sleep", args={"n": 2})
    host.request("work-1", first)
    until(lambda: host.events("start", job_id=first["job_id"]), what="the child started")
    running = body(op="host.sleep", args={"n": 2})
    host.request("work-1", running)
    same_page = host.request("work-1", body())
    until(lambda: host.events("start", job_id=running["job_id"]), what="the next child started")
    host.controls.draining.set()
    after = host.request("work-1", body())
    assert host.wait() == "drained: run stopped"
    for job in (first, running):
        finished = host.lucille.reply("work-1", job["job_id"])
        assert finished is not None and finished["body"]["status"] == "done"
    for job_id in (same_page, after):
        assert host.lucille.reply("work-1", job_id) is None
        assert host.journal().get(job_id) is None
        assert host.events("received", job_id=job_id) == []


def test_nothing_before_local_confirm(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast(monkeypatch)
    monkeypatch.setattr("lucille_host.link.TOUCH_EVERY", 0.2)
    host = RunHost(h, host_env, tmp_path, open_gate=False)
    calls = counting(monkeypatch, _switchboard.Link, "touch", "accept_room", "leave_room")
    posts = counting(monkeypatch, _switchboard.RoomClient, "post")
    with _audit_hook.recording() as events:
        host.start()
        host.lucille.offer("work-1")
        job = host.request("work-1")
        time.sleep(QUIET)
        assert (calls["touch"], calls["accept_room"], calls["leave_room"]) == (0, 0, 0)
        assert posts["post"] == 0
        assert host.lucille.reply("work-1", job) is None
        assert not host.dirs.rooms_file.exists()
        assert not host.dirs.journal_file.exists()
        assert _audit_hook.spawns(events) == []
        assert any("`lucille-host pair confirm`" in line for line in host.said)
        # `run` had the SAS, and showed it nowhere: a `y` now has nothing to confirm, and the
        # gate stays shut.
        with pytest.raises(link.Stopped) as unshown:
            host.session().open_gate(host.name)
        assert unshown.value.state == "sas_pending"
        found = host.gate()
        assert found is not None and (found.shown_at, found.opened_at) == (None, None)
        time.sleep(QUIET)
        assert (calls["touch"], calls["accept_room"], posts["post"]) == (0, 0, 0)
        # `pair status` and `pair confirm`, in another shell: the gate opens, and the request
        # runs.
        host.confirm()
        assert host.reply("work-1", job)["body"]["status"] == "done"
    host.stop()
    assert calls["touch"] >= 1 and calls["accept_room"] == 1


# --- the footprint ------------------------------------------------------------------------------


def _flow(host: RunHost) -> None:
    """A room taken, a request answered, a heal, and the replacement room taken and used."""
    host.start()
    host.offer("work-1")
    host.reply("work-1", host.request("work-1"))
    host.lucille.heal()
    until(lambda: host.record().held == {}, what="the room left")
    host.lucille.resolve()
    host.offer("work-2")
    host.reply("work-2", host.request("work-2"))
    host.stop()


def test_pairing_and_read_loop_spawn_nothing(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast(monkeypatch)
    with _audit_hook.recording() as events:
        host = RunHost(h, host_env, tmp_path)
        _flow(host)
    assert _audit_hook.spawns(events) == []
    assert len(host.events("execution")) == 2


def test_no_listening_socket(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast(monkeypatch)
    with _audit_hook.recording() as events:
        host = RunHost(h, host_env, tmp_path)
        _flow(host)
    assert [event for event in events if event[0] == "socket.bind"] == []
    assert len(host.events("execution")) == 2


@contextlib.contextmanager
def _planted(path: Path) -> Iterator[socket.socket]:
    """A listening socket at ``path``, which must not exist yet; removed afterwards."""
    if path.exists() or path.is_symlink():
        pytest.fail(f"{path} already exists; this test plants its own socket there")
    path.parent.mkdir(parents=True, exist_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        listener.listen(8)
        listener.setblocking(False)
        yield listener
    finally:
        listener.close()
        path.unlink(missing_ok=True)


def _connected(listener: socket.socket) -> bool:
    try:
        accepted, _ = listener.accept()
    except BlockingIOError:
        return False
    accepted.close()
    return True


@pytest.mark.parametrize("where", ["runtime folder", "temporary folder"])
def test_planted_signer_socket_never_connected(
    where: str,
    host_env: HostEnv,
    h: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast(monkeypatch)
    # A short folder: a socket's path must fit in about a hundred bytes.
    short = Path(tempfile.mkdtemp(prefix="host-", dir="/tmp"))
    try:
        if where == "runtime folder":
            monkeypatch.setenv("XDG_RUNTIME_DIR", str(short))
            expected = short
        else:
            monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
            expected = Path(tempfile.gettempdir())
        # Where Switchboard's client looks for a signer for the host's agent id.
        planted = Path(signing.socket_path("lucille-host"))
        assert planted.parent == expected / "switchboard"
        with _planted(planted) as listener:
            # A client built as the host here, without the host's own set-up, connects to it.
            config = h.client_config(workspace="probe", agent_id="lucille-host")
            with h.client_class()(config, agent_id="lucille-host"):
                pass
            assert _connected(listener), "the planted socket is where the library looks"
            with _audit_hook.recording() as events:
                host = RunHost(h, host_env, tmp_path)
                host.start()
                host.offer("work-1")
                host.reply("work-1", host.request("work-1"))
                host.stop()
            assert not _connected(listener)
            assert ("socket.connect", str(planted)) not in events
            assert os.environ["XDG_RUNTIME_DIR"] == str(host_env.dirs.runtime)
    finally:
        for leftover in sorted(short.rglob("*"), reverse=True):
            if leftover.is_dir():
                leftover.rmdir()
            else:
                leftover.unlink()
        short.rmdir()


def test_host_never_invites(host: RunHost, monkeypatch: pytest.MonkeyPatch) -> None:
    offered_from: list[Any] = []
    real = sharing.share_new

    def share_new(*args: Any, **options: Any) -> Any:
        offered_from.append(options.get("store"))
        return real(*args, **options)

    monkeypatch.setattr("switchboard.sharing.share_new", share_new)
    _flow(host)
    host.session().forget()
    # Every room was offered by Lucille, from its own share store; none by the host.
    assert len(offered_from) == 2
    assert all(store is host.lucille.rooms for store in offered_from)
    # No agent announced itself in any room the host was in.
    workspaces = set(host.record().left)
    assert len(workspaces) == 2
    for workspace in workspaces:
        assert host.h.agents(workspace) == []


def test_sharing_calls_limited(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every public function of Switchboard's pairing and sharing modules is recorded when it is
    called from outside the library, by the host: not by the fake Lucille, and not by the
    library itself on the way through another call."""
    fast(monkeypatch)
    local = threading.local()
    by_host: collections.Counter[str] = collections.Counter()
    lock = threading.Lock()

    def recorded(name: str, real: Any) -> Any:
        def call(*args: Any, **options: Any) -> Any:
            depth = getattr(local, "depth", 0)
            if depth == 0 and not getattr(local, "lucille", False):
                with lock:
                    by_host[name] += 1
            local.depth = depth + 1
            try:
                return real(*args, **options)
            finally:
                local.depth = depth

        return call

    def inside(real: Any) -> Any:
        """A library method the host calls: what it calls is the library's own doing."""

        def call(*args: Any, **options: Any) -> Any:
            depth = getattr(local, "depth", 0)
            local.depth = depth + 1
            try:
                return real(*args, **options)
            finally:
                local.depth = depth

        return call

    for module in (pairing, sharing):
        for name in dir(module):
            member = getattr(module, name)
            if (
                not name.startswith("_")
                and isinstance(member, types.FunctionType)
                and member.__module__ == module.__name__
            ):
                monkeypatch.setattr(module, name, recorded(f"{module.__name__}.{name}", member))
    for owner in (pairing.PairRecord, pairing.PendingHandshake):
        monkeypatch.setattr(owner, "from_json", staticmethod(inside(owner.from_json)))
        monkeypatch.setattr(owner, "to_json", inside(owner.to_json))
    decode = pairing.PairingCode.decode
    monkeypatch.setattr(
        pairing.PairingCode,
        "decode",
        staticmethod(recorded("switchboard.pairing.PairingCode.decode", decode)),
    )

    def as_lucille(real: Any) -> Any:
        def call(*args: Any, **options: Any) -> Any:
            local.lucille = True
            try:
                return real(*args, **options)
            finally:
                local.lucille = False

        return call

    for name in dir(FakeLucille):
        if not name.startswith("_") and callable(getattr(FakeLucille, name)):
            monkeypatch.setattr(FakeLucille, name, as_lucille(getattr(FakeLucille, name)))
    host = RunHost(h, host_env, tmp_path)
    _flow(host)
    host.session().forget()
    called = set(by_host)
    assert {name for name in called if name.startswith("switchboard.sharing.")} == {
        "switchboard.sharing.touch",
        "switchboard.sharing.accept",
        "switchboard.sharing.resolve_room",
        "switchboard.sharing.leave",
    }
    pairing_calls = {"decode", "accept", "resolve", "forget"}
    # Besides those: the tag of a note read on the pair channel, and the hub's error types.
    helpers = {"message_tag", "hub_errors"}
    used = {name.rsplit(".", 1)[1] for name in called if name.startswith("switchboard.pairing.")}
    assert pairing_calls <= used <= pairing_calls | helpers


# --- run as a whole -----------------------------------------------------------------------------


def test_run_stops_when_lucille_ends_the_pair(host: RunHost) -> None:
    host.start()
    room = host.offer("work-1")
    host.lucille.forget()
    said = host.wait()
    assert said.startswith("ended: ")
    assert host.record().held == {}
    assert host.opened.names() == []
    lines = host.audit()
    left = [line for line in lines if line["kind"] == "room" and line.get("event") == "left"]
    forgotten = [
        line for line in lines if line["kind"] == "pair" and line.get("event") == "forgotten"
    ]
    assert [line["room"] for line in left] == [room] and len(forgotten) == 1
    assert lines.index(left[0]) < lines.index(forgotten[0])


def test_wake_from_sleep_touches(host: RunHost, monkeypatch: pytest.MonkeyPatch) -> None:
    assert loop.wake_gap(3600.0, 1.0)
    assert not loop.wake_gap(31.0, 1.5)
    wakes = counting(monkeypatch, link.Session, "wake")
    touches = counting(monkeypatch, _switchboard.Link, "touch")
    host.start()
    host.offer("work-1")
    time.sleep(0.3)
    assert wakes["wake"] == 0
    before = touches["touch"]
    host.offset += 3600
    until(lambda: wakes["wake"] == 1, what="the wake")
    until(lambda: touches["touch"] > before, what="a touch after the wake")
    host.stop()


def test_status_file_says_what_run_sees(host: RunHost) -> None:
    host.start()
    host.offer("work-1")
    host.reply("work-1", host.request("work-1"))
    until(lambda: _status(host)["reading"] == ["room-1"], what="status naming the room")
    # The skew is measured once the post returns, and written on main's next look around.
    until(lambda: _status(host)["skew"] is not None, what="the skew written")
    status = _status(host)
    assert status["running"] is True and status["pid"] == os.getpid()
    assert status["last_contact"] is not None and status["skewed"] is False
    assert isinstance(status["skew"], float)
    host.stop()
    assert _status(host)["running"] is False
    assert host.dirs.status_file.stat().st_mode & 0o777 == 0o600


def test_run_refuses_with_no_pair(host_env: HostEnv, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["run"]) == 1
    assert capsys.readouterr().err.startswith("none: ")
    assert not host_env.dirs.status_file.exists()


def test_run_through_the_command(
    host_env: HostEnv,
    h: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fast(monkeypatch)
    host = RunHost(h, host_env, tmp_path)
    real = _switchboard.open_link
    monkeypatch.setattr(
        "lucille_host._switchboard.open_link",
        lambda dirs, config, **_: real(dirs, config, client_class=h.client_class()),
    )
    made: list[Controls] = []

    def controls(state_dir: Path) -> Controls:
        made.append(Controls(state_dir))
        return made[-1]

    monkeypatch.setattr("lucille_host.cli.Controls", controls)
    result: dict[str, int] = {}
    thread = threading.Thread(target=lambda: result.update(rc=cli.main(["run"])), daemon=True)
    thread.start()
    until(lambda: made, what="run started")
    host.lucille.offer("work-1")
    host.reply("work-1", host.request("work-1"))
    made[0].draining.set()
    thread.join(30)
    assert result == {"rc": 0}
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "running: pair lucille; Ctrl-C stops once the running operation ends"
    assert lines[-1] == "drained: run stopped"
