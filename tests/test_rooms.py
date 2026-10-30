"""Taking the work rooms Lucille offers over the pair: the accept policy, rooms held, pair events.

The host's :class:`lucille_host.link.Session` is driven here one event at a time, with no thread,
against Switchboard's in-process test hub and a fake Lucille on the other side
(``tests/_lucille.py``). Every call it makes goes through the host's own stores and client factory,
on the hub's clock.
Lucille's codes and rooms name Lucille's hub unless a test gives another URL; the in-process
transport reaches the test hub whatever the address.
"""

from __future__ import annotations

import collections
import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from switchboard import client, pairing, sharing, writekey
from switchboard.testing import hub

from lucille_host import _switchboard, gate, link, rooms, state
from tests import _audit_hook
from tests._host import HostEnv, lock_held
from tests._lucille import FakeLucille

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)

KEYRING = state.HostConfig()
FILE = state.HostConfig(store="file")
OTHER_HUB = "https://other.example"
SHARED = _switchboard.TAG_SHARED


@pytest.fixture
def h() -> Iterator[Any]:
    with hub() as handle:
        yield handle


class Spy:
    """Counts the host's own pairing and sharing calls, and keeps what some of them returned."""

    METHODS = ("resolve", "touch", "accept_room", "leave_room", "forget")

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: collections.Counter[str] = collections.Counter()
        self.returned: dict[str, list[Any]] = collections.defaultdict(list)
        self.built: list[str] = []
        for method in self.METHODS:
            real = getattr(_switchboard.Link, method)
            monkeypatch.setattr(_switchboard.Link, method, self._counted(method, real))
        connect = _switchboard.Link.connect

        def counted_connect(opened: _switchboard.Link, config: Any, **options: Any) -> Any:
            self.built.append(config.workspace)
            return connect(opened, config, **options)

        monkeypatch.setattr(_switchboard.Link, "connect", counted_connect)

    def _counted(self, method: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def counted(opened: _switchboard.Link, *args: Any, **options: Any) -> Any:
            self.calls[method] += 1
            result = real(opened, *args, **options)
            self.returned[method].append(result)
            return result

        return counted

    def notes(self, method: str) -> list[tuple[str, str | None]]:
        return [(n.kind, n.room) for result in self.returned[method] for n in result.notes]


@dataclass
class Host:
    """A profile paired with a fake Lucille, its gate open unless the test said otherwise."""

    h: Any
    env: HostEnv
    folder: Path
    config: state.HostConfig
    opened: _switchboard.Link
    session: link.Session
    lucille: FakeLucille
    name: str
    lucilles: int = 1
    stopped: list[str] = field(default_factory=list)

    @property
    def s(self) -> link.Session:
        return self.session

    def restart(self) -> link.Session:
        """A new session over the same stores, as a new run of the command would make."""
        self.session = link.Session(self.env.dirs, self.opened, say=lambda _: None)
        self.session.stop_room = self.stopped.append
        return self.session

    def record(self) -> rooms.Record:
        return rooms.load(self.env.dirs.rooms_file)

    def gate(self) -> gate.Gate | None:
        return gate.load(self.env.dirs.gate_file)

    def held(self) -> list[str]:
        return sorted(self.record().held)

    def store_rooms(self) -> list[str]:
        """The rooms the share store holds, read without the session."""
        return sorted(self.opened.rooms.load().get("rooms", {}))

    def room(self, name: str) -> _switchboard.Room:
        return self.opened.room(name)

    def audit(self) -> list[dict[str, Any]]:
        path = self.env.dirs.state / "audit.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [line for line in self.audit() if line["kind"] == kind]

    def pair_again(self) -> FakeLucille:
        """Pair again under the same name, with a fresh Lucille, and open the gate."""
        self.lucilles += 1
        lucille = FakeLucille(
            self.h, self.folder / f"lucille-{self.lucilles}", url=_switchboard.LUCILLE_HUB
        )
        self.lucille = lucille
        self.name = _confirm(self.session, lucille, self.config, self.name)
        return lucille


def _confirm(
    session: link.Session,
    lucille: FakeLucille,
    config: state.HostConfig,
    name: str,
    *,
    open_gate: bool = True,
) -> str:
    code = lucille.start(name=name)
    pair = session.accept(session.check_code(code, config))
    lucille.collect()
    assert session.resolve(pair).sas_known
    if open_gate:
        _open(session, pair, lucille)
    return pair


def _open(session: link.Session, name: str, lucille: FakeLucille) -> None:
    """The SAS shown, as ``pair status`` shows it, and confirmed, as ``pair confirm`` does."""
    shown = session.show(name)
    assert shown is not None and shown[0] == lucille.sas()
    assert session.open_gate(name) == f"pair {name} is confirmed on this computer"


def _host(
    h: Any,
    env: HostEnv,
    folder: Path,
    *,
    config: state.HostConfig = KEYRING,
    open_gate: bool = True,
) -> Host:
    opened = _switchboard.open_link(
        env.dirs, config, client_class=h.client_class(), clock=lambda: h.now
    )
    session = link.Session(env.dirs, opened, say=lambda _: None)
    lucille = FakeLucille(h, folder / "lucille-1", url=_switchboard.LUCILLE_HUB)
    name = _confirm(session, lucille, config, "lucille", open_gate=open_gate)
    host = Host(h, env, folder, config, opened, session, lucille, name)
    session.stop_room = host.stopped.append
    return host


@pytest.fixture
def host(host_env: HostEnv, h: Any, tmp_path: Path) -> Host:
    return _host(h, host_env, tmp_path)


def _taken(host: Host, offer: str) -> str:
    """Lucille offers ``offer``; the host reads it and takes it. Returns the host's name for it."""
    before = set(host.held())
    host.lucille.offer(offer)
    host.s.note(SHARED)
    host.s.step()
    (taken,) = set(host.held()) - before
    return taken


# --- touching and resolving -----------------------------------------------------------------------


def test_the_rhythms_are_the_specs() -> None:
    assert link.TOUCH_EVERY == 300
    assert link.RESOLVE_AT_LEAST == 86400
    assert link.RESOLVE_EVERY == 5
    assert link.BACKOFF_MOST == 30
    assert rooms.CAP == 2
    # The library's own tags, which the host names in its own constants.
    assert _switchboard.TAG_PAIRED == pairing.TAG_PAIRED
    assert _switchboard.TAG_MOVED == pairing.TAG_MOVED
    assert _switchboard.TAG_ENDED == pairing.TAG_ENDED
    assert _switchboard.TAG_SHARED == pairing.TAG_SHARED


def test_no_touch_before_sas_gate(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(h, host_env, tmp_path, open_gate=False)
    spy = Spy(monkeypatch)
    # Lucille offers a room (its own gate is its business); the host reads nothing of it.
    host.lucille.offer("work-1")
    s = host.s
    s.start()
    for event in (lambda: s.note(SHARED), lambda: s.note(_switchboard.TAG_PAIRED), s.wake):
        event()
        s.step()
    h.advance(link.TOUCH_EVERY * 2)
    s.hub_error()
    s.step()
    assert spy.calls["resolve"] >= 3
    assert spy.calls["touch"] == spy.calls["accept_room"] == spy.calls["leave_room"] == 0
    # No sharing call ran: the share store was never written.
    assert host.opened.rooms.load() == {}
    assert not host_env.dirs.rooms_file.exists()
    assert host.gate() is not None and host.gate().touched_at is None
    # The gate opens: the next step touches, finds the offer and takes it.
    _open(s, host.name, host.lucille)
    s.step()
    assert spy.calls["touch"] == 1
    assert host.held() == ["room-1"]
    assert host.gate().touched_at == h.now


def test_touch_rhythm(host: Host, h: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Spy(monkeypatch)
    named: list[Any] = []
    real = sharing.touch

    def touch(*args: Any, **options: Any) -> Any:
        named.append(options.get("pairs"))
        return real(*args, **options)

    monkeypatch.setattr("switchboard.sharing.touch", touch)
    s = host.s
    s.start()
    # At start: a resolve, and one touch, which also follows that resolve.
    assert (spy.calls["resolve"], spy.calls["touch"]) == (1, 1)
    s.step()
    assert spy.calls["touch"] == 1, "nothing was due"
    s.note(SHARED)
    s.step()
    assert (spy.calls["resolve"], spy.calls["touch"]) == (1, 2)
    s.note(_switchboard.TAG_PAIRED)
    s.step()
    assert (spy.calls["resolve"], spy.calls["touch"]) == (2, 3), "a touch follows a resolve"
    s.wake()
    s.step()
    assert spy.calls["touch"] == 4
    h.advance(link.TOUCH_EVERY - 1)
    s.step()
    assert spy.calls["touch"] == 4
    assert s.due() == h.now + 1
    h.advance(1)
    s.step()
    assert spy.calls["touch"] == 5, "every 5 minutes, with no room held"
    assert host.held() == []
    h.advance(link.BACKOFF_MOST)
    s.hub_error()
    s.step()
    assert (spy.calls["resolve"], spy.calls["touch"]) == (3, 6)
    # A hub error reported right after a resolve asks for none.
    s.hub_error()
    s.step()
    assert (spy.calls["resolve"], spy.calls["touch"]) == (3, 6)
    # Every touch named the pair, and no other.
    assert named == [[host.name]] * 6


def test_no_touch_after_a_failed_touch_resolve(
    host: Host, h: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = [False]
    real = _switchboard.Link.touch

    def touch(opened: _switchboard.Link, pair: str) -> _switchboard.Touch:
        if failing[0]:
            return _switchboard.Touch(state="unreachable", notes=())
        return real(opened, pair)

    monkeypatch.setattr(_switchboard.Link, "touch", touch)
    spy = Spy(monkeypatch)
    s = host.s
    s.start()
    resolves, touches = spy.calls["resolve"], spy.calls["touch"]
    failing[0] = True
    s.wake()
    s.step()
    # The failed touch asked for a resolve, and that resolve for no touch.
    assert (spy.calls["resolve"], spy.calls["touch"]) == (resolves + 1, touches + 1)
    for _ in range(3):
        s.step()
    assert (spy.calls["resolve"], spy.calls["touch"]) == (resolves + 1, touches + 1)
    # The next touch comes on the rhythm, and fails again: one more resolve, no more.
    h.advance(link.TOUCH_EVERY)
    s.step()
    assert (spy.calls["resolve"], spy.calls["touch"]) == (resolves + 2, touches + 2)
    # Once touches work again, a resolve for another reason is followed by a touch.
    failing[0] = False
    s.note(_switchboard.TAG_MOVED)
    s.step()
    assert (spy.calls["resolve"], spy.calls["touch"]) == (resolves + 3, touches + 3)


def test_a_failed_resolve_is_tried_again_after_a_back_off(
    host: Host, h: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = host.s
    s.start()
    spy = Spy(monkeypatch)
    down = [True]
    real = _switchboard.Link.resolve

    def resolve(opened: _switchboard.Link, name: str) -> _switchboard.Pair:
        if down[0]:
            raise _switchboard.HubError("the hub answered 429", status=429, retry_after=12.0)
        return real(opened, name)

    monkeypatch.setattr(_switchboard.Link, "resolve", resolve)
    s.note(_switchboard.TAG_PAIRED)
    s.step()
    assert s.due() == h.now + 12, "the hub's retry_after"
    s.step()
    h.advance(11)
    s.step()
    assert spy.calls["touch"] == 0, "no touch follows a resolve that failed"
    down[0] = False
    h.advance(1)
    s.step()
    assert spy.calls["touch"] == 1


def test_a_resolve_the_hub_turns_away_waits_its_retry_after(
    host: Host, h: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The library does not raise when the hub turns away its read of the pair room: it returns
    # what this computer last saw, unchecked, with the hub's error beside it. That error's
    # retry_after is the hub's word on when to ask again.
    s = host.s
    s.start()
    spy = Spy(monkeypatch)
    busy = [True]
    real = client.Client.board_list

    def board_list(listing: client.Client, *args: Any, **options: Any) -> Any:
        if busy[0]:
            raise client.SwitchboardError(
                "busy", status=429, payload={"error": "busy", "retry_after": 12}
            )
        return real(listing, *args, **options)

    monkeypatch.setattr(client.Client, "board_list", board_list)
    s.note(_switchboard.TAG_PAIRED)
    s.step()
    (turned_away,) = spy.returned["resolve"]
    assert not turned_away.checked and turned_away.retry_after == 12.0
    assert s.due() == h.now + 12, "the hub's retry_after"
    h.advance(11)
    s.step()
    assert spy.calls["resolve"] == 1
    busy[0] = False
    h.advance(1)
    s.step()
    assert spy.calls["resolve"] == 2
    assert spy.returned["resolve"][1].checked and spy.returned["resolve"][1].retry_after is None


def test_resolves_at_least_daily(host: Host, h: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Spy(monkeypatch)
    s = host.s
    s.start()
    assert spy.calls["resolve"] == 1
    for _ in range(23):
        h.advance(3600)
        s.step()
    h.advance(3599)
    s.step()
    # A touch every time (it resolves the pair itself, without the host's own resolve).
    assert spy.calls["touch"] == 25
    assert spy.calls["resolve"] == 1
    assert s.due() == h.now + 1
    h.advance(1)
    s.step()
    assert spy.calls["resolve"] == 2


def test_pair_notes_are_wakes_only(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Spy(monkeypatch)
    s = host.s
    s.start()
    resolves, touches = spy.calls["resolve"], spy.calls["touch"]
    for tag in (_switchboard.TAG_PAIRED, _switchboard.TAG_MOVED, _switchboard.TAG_ENDED):
        # A burst of notes is one resolve, and the touch that follows it.
        for _ in range(3):
            s.note(tag)
        s.step()
        resolves, touches = resolves + 1, touches + 1
        assert (spy.calls["resolve"], spy.calls["touch"]) == (resolves, touches), tag
    s.note(SHARED)
    s.note(SHARED)
    s.step()
    touches += 1
    assert (spy.calls["resolve"], spy.calls["touch"]) == (resolves, touches)
    for tag in (None, "", "swbp1/hello", "swbp1/answer", "note", "swbp1/ended "):
        s.note(tag)
        s.step()
    assert (spy.calls["resolve"], spy.calls["touch"]) == (resolves, touches)
    # An `ended` note ends nothing on its own: the pair is still there, as the resolve found.
    assert host.opened.names() == [host.name]
    assert spy.calls["forget"] == 0


# --- the accept policy ----------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["all", "check 1", "check 2", "check 3", "check 4", "check 5"])
def test_accept_by_policy(
    case: str, host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(h, host_env, tmp_path, open_gate=case != "check 2")
    if case == "check 5":
        monkeypatch.setattr("lucille_host.rooms.CAP", 1)
        host.lucille.offer("work-0")
        host.s.start()
        assert host.held() == ["room-1"]
    if case == "check 1":
        real = _switchboard.Link.touch

        def touch(opened: _switchboard.Link, pair: str) -> _switchboard.Touch:
            found = real(opened, pair)
            other = _switchboard.ShareNote(kind="offered", pair="another", room="work-1")
            return _switchboard.Touch(state=found.state, notes=(*found.notes, other))

        monkeypatch.setattr(_switchboard.Link, "touch", touch)
    else:
        options = {"check 3": {"url": OTHER_HUB}, "check 4": {"token": "sb_room_token"}}
        host.lucille.offer("work-1", **options.get(case, {}))
    if case == "check 2":
        found = host.gate()
        assert found is not None and found.opened_at is None
        rooms.save(
            host_env.dirs.rooms_file,
            rooms.Record(pair=host.name, made=found.created_at, offers={"work-1": h.now}),
        )
        host.s.consider_offers()
        assert host.record().offers == {"work-1": h.now}, "not final"
        assert host.held() == []
    elif case != "check 5":
        host.s.start()
    else:
        host.s.note(SHARED)
        host.s.step()
    refused = [line for line in host.events("offer") if line["event"] == "refused"]
    left = [line for line in host.events("room") if line["event"] == "refused"]
    if case == "all":
        assert host.held() == ["room-1"]
        assert [view.name for view in host.s.usable()] == ["room-1"]
        assert not refused and not left
    elif case in ("check 1", "check 2", "check 5"):
        assert [line["check"] for line in refused] == [case]
        assert host.held() == (["room-1"] if case == "check 5" else [])
        assert host.store_rooms() == host.held()
    else:
        assert [line["check"] for line in left] == [case]
        assert host.held() == [] and host.store_rooms() == []


def test_refusal_before_accept_not_final(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Check 2: an offer written down while the gate is shut stays, and is taken once it opens.
    host = _host(h, host_env, tmp_path, open_gate=False)
    host.lucille.offer("work-1")
    found = host.gate()
    assert found is not None
    rooms.save(
        host_env.dirs.rooms_file,
        rooms.Record(pair=host.name, made=found.created_at, offers={"work-1": h.now}),
    )
    host.s.consider_offers()
    assert host.record().offers and host.held() == []
    _open(host.s, host.name, host.lucille)
    host.s.start()
    assert host.held() == ["room-1"]
    # Check 5: with no room to spare, a new offer is refused and kept, not left.
    monkeypatch.setattr("lucille_host.rooms.CAP", 1)
    host.lucille.offer("work-2")
    host.s.note(SHARED)
    host.s.step()
    assert host.held() == ["room-1"]
    assert list(host.record().offers) == ["work-2"]
    with pytest.raises(_switchboard.RoomError) as pending:
        host.room("work-2")
    assert pending.value.state == "offered", "still offered: nothing was left"
    # Lucille leaves the room it held: the host abandons it, and considers the offer again.
    host.lucille.leave("work-1")
    host.s.note(SHARED)
    host.s.step()
    assert host.held() == ["room-2"]
    assert host.record().left[next(iter(host.record().left))].why == rooms.LUCILLE_LEFT
    assert host.record().offers == {}


@pytest.mark.parametrize("check", [rooms.CHECK_HUB, rooms.CHECK_TOKEN])
def test_check_3_or_4_failure_leaves_at_once(
    check: str, host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = Spy(monkeypatch)
    if check == rooms.CHECK_HUB:
        host.lucille.offer("work-1", url=OTHER_HUB)
    else:
        host.lucille.offer("work-1", token="sb_room_token")
    host.s.start()
    # Accepted, then left at once, by the host's own call.
    assert spy.calls["accept_room"] == 1 and spy.calls["leave_room"] == 1
    record = host.record()
    assert record.held == {} and host.store_rooms() == []
    ((workspace, gone),) = record.left.items()
    assert (gone.room, gone.why) == ("room-1", check)
    # The log names the check, and the host says it holds no work room because of it.
    refused = [e for e in host.events("room") if e["event"] == "refused"]
    assert [(e["room"], e["check"]) for e in refused] == [("room-1", check)]
    assert record.no_room == check
    # Never read nor posted in: no client was ever built for it.
    assert workspace not in spy.built
    assert host.s.usable() == []
    # Lucille sees that the host left it.
    assert ("left", "work-1") in [(n["kind"], n["room"]) for n in host.lucille.touch().notes]


def test_newer_room_only_after_checks(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    host.lucille.offer("work-1")
    host.s.start()
    assert host.held() == ["room-1"]
    # A newer room that fails check 3 replaces nothing.
    host.lucille.offer("work-2", url=OTHER_HUB)
    host.s.note(SHARED)
    host.s.step()
    assert host.held() == ["room-1"]
    assert [view.name for view in host.s.usable()] == ["room-1"]
    assert host.record().no_room is None, "the host still holds a room"
    # One that passes replaces the room held over the pair.
    assert _taken(host, "work-3") == "room-3"
    assert host.held() == ["room-3"]
    whys = {gone.room: gone.why for gone in host.record().left.values()}
    assert whys == {"room-2": rooms.CHECK_HUB, "room-1": rooms.REPLACED}
    assert host.stopped == ["room-2", "room-1"]
    # A stop between the accept and the replacement: the next start completes it.
    real = _switchboard.Link.accept_room

    def stop_after(opened: _switchboard.Link, *args: Any) -> _switchboard.RoomTaken:
        real(opened, *args)
        raise KeyboardInterrupt

    monkeypatch.setattr(_switchboard.Link, "accept_room", stop_after)
    host.lucille.offer("work-4")
    host.s.note(SHARED)
    with pytest.raises(KeyboardInterrupt):
        host.s.step()
    record = host.record()
    assert record.accepting == rooms.Accepting("work-4", "room-4", ("room-3",))
    assert host.held() == ["room-3"] and host.store_rooms() == ["room-3", "room-4"]
    monkeypatch.setattr(_switchboard.Link, "accept_room", real)
    host.restart().start()
    assert host.held() == ["room-4"] and host.store_rooms() == ["room-4"]
    assert host.record().accepting is None
    whys = {gone.room: gone.why for gone in host.record().left.values()}
    assert whys["room-3"] == rooms.REPLACED


def test_no_accept_while_several_offers_pending(host: Host) -> None:
    host.lucille.offer("work-1")
    host.lucille.offer("work-2")
    host.s.start()
    assert host.held() == []
    assert sorted(host.record().offers) == ["work-1", "work-2"]
    pending = [e for e in host.events("offer") if e["event"] == "several_pending"]
    assert pending and pending[-1]["offers"] == ["work-1", "work-2"]
    # Lucille leaves the room it no longer uses; a touch says so, and the other is taken.
    host.lucille.leave("work-2")
    host.s.note(SHARED)
    host.s.step()
    assert host.held() == ["room-1"]
    assert host.record().offers == {}
    dropped = [e for e in host.events("offer") if e["event"] == "dropped"]
    assert [(e["offer"], e["why"]) for e in dropped] == [("work-2", "left")]


def test_an_offer_the_share_store_forgot_is_dropped_first(host: Host, h: Any) -> None:
    # An offer withdrawn while a stop hid its note: the share store no longer knows it.
    found = host.gate()
    assert found is not None
    rooms.save(
        host.env.dirs.rooms_file,
        rooms.Record(pair=host.name, made=found.created_at, offers={"ghost": h.now}),
    )
    host.lucille.offer("work-1")
    host.s.start()
    assert host.held() == ["room-1"]
    dropped = [e for e in host.events("offer") if e["event"] == "dropped"]
    assert [(e["offer"], e["why"]) for e in dropped] == [("ghost", "unknown")]


def test_offers_reported_by_any_call_are_written_down(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.lucille.offer("work-1")
    real = _switchboard.Link.accept_room
    offered = []

    def accept_room(opened: _switchboard.Link, *args: Any) -> _switchboard.RoomTaken:
        # Lucille offers another room while the host is taking the first: the accept's own read
        # of the offers is the only one that reports it.
        first = not offered
        if first:
            offered.append(host.lucille.offer("work-2"))
        taken = real(opened, *args)
        if first:
            assert ("offered", "work-2") in [(n.kind, n.room) for n in taken.notes]
        return taken

    monkeypatch.setattr(_switchboard.Link, "accept_room", accept_room)
    host.s.start()
    # The second offer was written down, and taken next, as the newer room.
    assert host.held() == ["room-2"]
    whys = {gone.room: gone.why for gone in host.record().left.values()}
    assert whys == {"room-1": rooms.REPLACED}


@pytest.mark.parametrize("failure", ["unreachable", "hub", "name_taken"])
def test_an_accept_that_failed_in_passing_is_tried_again(
    host: Host, h: Any, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    # The hub, or the pair's room, could not be read inside the accept, once. The library notes
    # an offer once, so no later touch reports it again: before, nothing ever took it.
    real = sharing.accept
    calls: list[str] = []

    def accept(*args: Any, **options: Any) -> Any:
        calls.append(args[0])
        if len(calls) == 1:
            if failure == "hub":
                raise httpx.ConnectError("the hub is out of reach")
            raise sharing.SharingError("could not read the pair's room", state=failure)
        return real(*args, **options)

    monkeypatch.setattr("switchboard.sharing.accept", accept)
    host.s.start()
    host.lucille.offer("work-1")
    host.s.note(SHARED)
    host.s.step()
    assert len(calls) == 1 and host.held() == []
    assert list(host.record().offers) == ["work-1"]
    failed = [e for e in host.events("offer") if e["event"] == "not_accepted"]
    assert [e["state"] for e in failed] == [failure]
    # Tried again after the next touch that reads the pair's room, and no sooner.
    for _ in range(3):
        host.s.step()
        if host.held():
            break
        h.advance(link.TOUCH_EVERY)
    assert len(calls) == 2
    assert len(host.held()) == 1 and host.record().offers == {}


def test_an_offer_noted_inside_a_refused_accept_is_written_down(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Lucille leaves the room it offered and offers another, while the host reads neither: the
    # accept's own first read sees both, then refuses the gone offer. Its notes were lost with
    # the refusal, and no later touch reported the new offer again.
    host.lucille.offer("work-1")
    real = _switchboard.Link.accept_room
    spy = Spy(monkeypatch)

    def accept_room(opened: _switchboard.Link, *args: Any) -> _switchboard.RoomTaken:
        if args[1] == "work-1":
            host.lucille.leave("work-1")
            host.lucille.offer("work-2")
        return real(opened, *args)

    monkeypatch.setattr(_switchboard.Link, "accept_room", accept_room)
    host.s.start()
    assert host.held() == ["room-2"] and host.record().offers == {}
    assert host.room("room-2").workspace == host.lucille.room_config("work-2").workspace
    refused = [e for e in host.events("offer") if e["event"] == "not_accepted"]
    assert [(e["offer"], e["state"]) for e in refused] == [("work-1", "withdrawn")]
    # The new offer was not in any touch's own notes: only the store had it.
    assert ("offered", "work-2") not in spy.notes("touch")


def test_notes_come_from_the_share_store_once_each(host: Host) -> None:
    opened = host.opened
    assert opened.notes() == ()
    host.lucille.offer("work-1")
    touched = opened.touch(host.name)
    assert ("offered", "work-1") in [(n.kind, n.room) for n in touched.notes]
    assert opened.notes() == ()
    # A refused call's notes are kept for the next look.
    host.lucille.leave("work-1")
    host.lucille.offer("work-2")
    with pytest.raises(_switchboard.RoomError) as refused:
        opened.accept_room(host.name, "work-1", "room-9")
    assert refused.value.state == "withdrawn"
    assert ("offered", "work-2") in [(n.kind, n.room) for n in opened.notes()]
    assert opened.notes() == ()
    assert opened.touch(host.name).notes == ()
    # A call that returned gives what it noted, and the next look has nothing more.
    host.lucille.offer("work-3")
    taken = opened.accept_room(host.name, "work-2", "room-9")
    assert ("offered", "work-3") in [(n.kind, n.room) for n in taken.notes]
    assert opened.notes() == ()
    host.lucille.offer("work-4")
    assert ("offered", "work-4") in [(n.kind, n.room) for n in opened.leave_room("room-9").notes]
    assert opened.notes() == ()


def test_an_offer_noted_by_a_touch_that_then_refused_is_written_down(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = sharing.touch
    refused: list[bool] = []

    def touch(*args: Any, **options: Any) -> Any:
        touched = real(*args, **options)
        if not refused:
            refused.append(True)
            raise sharing.SharingError("the share store could not be saved", state="store")
        return touched

    monkeypatch.setattr("switchboard.sharing.touch", touch)
    host.lucille.offer("work-1")
    host.s.start()
    # Written down from the store's notes, and taken.
    assert refused and host.held() == ["room-1"]


def _host_writes_ahead(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """While the flag it returns is true, every write is signed by a clock 400 s ahead of the
    hub's, which the hub refuses. Here the host and the fake Lucille share one thread, so a test
    sets it around the host's calls only."""
    ahead = [False]
    sign = writekey.RoomWriteKey.sign_request

    def signed(key: Any, *args: Any, **options: Any) -> Any:
        if ahead[0]:
            options["now"] = time.time() + 400
        return sign(key, *args, **options)

    monkeypatch.setattr(writekey.RoomWriteKey, "sign_request", signed)
    return ahead


def test_every_sharing_call_says_when_the_hub_refused_it_for_the_clock(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    ahead = _host_writes_ahead(monkeypatch)
    opened = host.opened
    host.lucille.offer("work-1")
    ahead[0] = True
    # A touch that owes no write is not refused.
    touched = opened.touch(host.name)
    assert touched.reached and not touched.skewed
    # The accept keeps the room, and its write telling the pair is refused.
    assert opened.accept_room(host.name, "work-1", "room-1").skewed
    # That write is owed now: the next touch makes it, and is refused.
    touched = opened.touch(host.name)
    assert touched.state == "unreachable" and touched.skewed
    # So is an accept's own first read of the pair, which makes it too.
    ahead[0] = False
    host.lucille.offer("work-2")
    ahead[0] = True
    with pytest.raises(_switchboard.RoomError) as refused:
        opened.accept_room(host.name, "work-2", "room-2")
    assert refused.value.state == "unreachable" and refused.value.skewed
    left = opened.leave_room("room-1")
    assert not left.told and left.skewed
    # Set right: what is owed is written, and nothing says skewed.
    ahead[0] = False
    touched = opened.touch(host.name)
    assert touched.reached and not touched.skewed


def test_the_session_says_when_the_hub_refused_it_for_the_clock(
    host: Host, h: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ahead = _host_writes_ahead(monkeypatch)
    host.lucille.offer("work-1")
    ahead[0] = True
    host.s.start()
    assert host.held() == ["room-1"] and host.s.skewed
    ahead[0] = False
    h.advance(link.TOUCH_EVERY)
    host.s.step()
    assert not host.s.skewed


def test_a_move_found_by_a_touch_is_followed_before_an_offer_is_taken(
    host: Host, h: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    room = _taken_at_start(host, "work-1")
    spy = Spy(monkeypatch)
    # Lucille heals and offers the replacement; the `moved` note never reaches the host.
    host.lucille.heal()
    host.lucille.offer("work-2")
    h.advance(link.TOUCH_EVERY)
    host.s.step()
    # The touch followed the move and said so; the host resolved and abandoned the old room
    # before it took the replacement.
    assert ("pair_moved", None) in spy.notes("touch")
    assert host.held() == ["room-2"]
    whys = {gone.room: gone.why for gone in host.record().left.values()}
    assert whys == {room: rooms.MOVED}
    # So the replacement survives the next resolve, which finds no move.
    h.advance(link.RESOLVE_AT_LEAST)
    host.s.step()
    assert host.held() == ["room-2"]


def test_at_most_two_rooms(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    host.lucille.offer("work-1")
    host.s.start()
    real = _switchboard.Link.leave_room

    def cannot(opened: _switchboard.Link, name: str) -> _switchboard.RoomLeft:
        raise _switchboard.RoomError("store", "the share store could not be written")

    monkeypatch.setattr(_switchboard.Link, "leave_room", cannot)
    # A newer room replaces room-1, whose leave fails: it stays held, abandoned.
    assert _taken(host, "work-2") == "room-2"
    record = host.record()
    assert sorted(record.held) == ["room-1", "room-2"]
    assert record.held["room-1"].abandoned == rooms.REPLACED
    assert [view.name for view in host.s.usable()] == ["room-2"]
    # Two held in all: a third offer is refused by check 5, and kept.
    host.lucille.offer("work-3")
    host.s.note(SHARED)
    host.s.step()
    assert host.held() == ["room-1", "room-2"]
    assert list(host.record().offers) == ["work-3"]
    refused = [e for e in host.events("offer") if e["event"] == "refused"]
    assert [(e["offer"], e["check"]) for e in refused] == [("work-3", rooms.CHECK_CAP)]
    # Once room-1 is left, the offer is taken, and replaces room-2.
    monkeypatch.setattr(_switchboard.Link, "leave_room", real)
    host.s.wake()
    host.s.step()
    assert host.held() == ["room-3"] and host.store_rooms() == ["room-3"]


def test_current_pair_judged_after_touch(host: Host) -> None:
    room = _taken_at_start(host, "work-1")
    # The pair is forgotten while its room stays in the share store (as a store copied from
    # before, or an older release, could leave it), and the computer is paired again under the
    # same name.
    pairing.forget(host.name, None, store=host.opened.pairs, connect=host.opened.connect)
    gate.remove(host.env.dirs.gate_file)
    host.lucille.forget()
    host.pair_again()
    # By name alone, the room's list still names the pair.
    assert host.room(room).pairs == (host.name,)
    s = host.restart()
    assert s.check_room(room) is None, "not judged before a touch of the new pair"
    assert host.held() == [room] and host.record().held[room].abandoned is None
    assert host.store_rooms() == [room]
    s.start()
    assert host.held() == [] and host.store_rooms() == []
    whys = [(e["room"], e["why"]) for e in host.events("room") if e["event"] == "abandoned"]
    assert whys == [(room, rooms.NOT_LISTED)]


def _taken_at_start(host: Host, offer: str) -> str:
    host.lucille.offer(offer)
    host.s.start()
    (room,) = host.held()
    return room


def test_no_re_accept(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    room = _taken_at_start(host, "work-1")
    workspace = host.room(room).workspace
    assert host.s.forget().told
    # Paired again under the same name; this Lucille offers the room the host left over the new
    # pair. The library would keep it: the host takes it, finds it left before, and leaves it.
    host.lucille.forget()
    lucille = host.lucille
    code = lucille.start(name=host.name)
    spy = Spy(monkeypatch)
    host.s.accept(host.s.check_code(code, host.config))
    lucille.collect()
    assert host.s.resolve(host.name).sas_known
    _open(host.s, host.name, lucille)
    lucille.offer_again("work-1")
    host.restart().start()
    assert spy.calls["accept_room"] == 1
    assert host.held() == [] and host.store_rooms() == []
    refused = [e for e in host.events("room") if e["event"] == "refused"]
    assert [e["check"] for e in refused] == [rooms.LEFT_BEFORE]
    assert workspace not in spy.built
    assert host.record().no_room == rooms.LEFT_BEFORE


# --- pair events ----------------------------------------------------------------------------------


def test_heal_abandons_room_and_takes_replacement(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    room = _taken_at_start(host, "work-1")
    first = host.s.pair
    assert first is not None and host.record().generation == first.generation
    spy = Spy(monkeypatch)
    logged = len(host.audit())
    # Lucille heals the pair and offers the replacement before the host has noticed either.
    host.lucille.heal()
    host.lucille.offer("work-2")
    host.s.note(_switchboard.TAG_MOVED)
    host.s.step()
    moved = host.s.pair
    assert moved is not None and moved.generation != first.generation
    # The library's notes are followed in the new pair:<name>.
    assert moved.workspace != first.workspace
    assert host.record().generation == moved.generation
    assert host.held() == ["room-2"]
    whys = {gone.room: gone.why for gone in host.record().left.values()}
    assert whys == {room: rooms.MOVED}
    assert host.stopped == [room]
    # The replacement was reported once, by the leave's own read of the offers: the host wrote
    # it down from there, since its own touch did not report it again.
    assert ("offered", "work-2") in spy.notes("leave_room")
    assert ("offered", "work-2") not in spy.notes("touch")
    events = [(e["kind"], e["event"]) for e in host.audit()[logged:] if e["kind"] == "room"]
    assert events == [
        ("room", "abandoned"),
        ("room", "left"),
        ("room", "accepting"),
        ("room", "accepted"),
    ]


def test_an_interrupted_accept_is_abandoned_on_a_move_found_at_start(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    room = _taken_at_start(host, "work-1")
    # A stop between the accept of a newer room and its checks.
    real = _switchboard.Link.accept_room

    def stop_after(opened: _switchboard.Link, *args: Any) -> _switchboard.RoomTaken:
        real(opened, *args)
        raise KeyboardInterrupt

    monkeypatch.setattr(_switchboard.Link, "accept_room", stop_after)
    host.lucille.offer("work-2")
    host.s.note(SHARED)
    with pytest.raises(KeyboardInterrupt):
        host.s.step()
    monkeypatch.setattr(_switchboard.Link, "accept_room", real)
    assert host.record().accepting is not None
    assert host.store_rooms() == sorted([room, host.record().accepting.room])
    # While the host is stopped, Lucille heals the pair: a suspected leak.
    host.lucille.heal()
    host.restart().start()
    # Both rooms held over the pair before the move are left; neither is used.
    assert host.held() == [] and host.store_rooms() == []
    assert host.record().accepting is None
    whys = {gone.room: gone.why for gone in host.record().left.values()}
    assert whys == {room: rooms.MOVED, "room-2": rooms.MOVED}
    assert host.s.usable() == []
    assert not [e for e in host.events("room") if e["event"] == "accepted" and e["room"] != room]


def test_a_move_another_command_followed_is_found_by_the_next_touch(
    host: Host, h: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    room = _taken_at_start(host, "work-1")
    first = host.record().generation
    running = host.s
    # Lucille heals pair:<name>, and its note never reaches `run`. `pair status`, run in another
    # shell, resolves the confirmed pair and follows the move itself: the store holds the new
    # generation, so no touch of `run`'s moves anything, and no note says it moved.
    host.lucille.heal()
    other = link.Session(host.env.dirs, host.opened, say=lambda _: None)
    assert other.status()[0] == "confirmed"
    kept = host.opened.kept(host.name)
    assert kept is not None and kept.generation != first
    spy = Spy(monkeypatch)
    # The next touch, on the rhythm, finds it by the generation: the room is abandoned there.
    h.advance(link.TOUCH_EVERY)
    running.step()
    assert (spy.calls["touch"], spy.calls["resolve"]) == (1, 1)
    assert host.record().generation == kept.generation
    assert host.held() == [] and host.stopped == [room]
    whys = {gone.room: gone.why for gone in host.record().left.values()}
    assert whys == {room: rooms.MOVED}
    assert running.usable() == []


def test_lapse_handled_as_heal(host: Host, h: Any) -> None:
    room = _taken_at_start(host, "work-1")
    first = host.record().generation
    # Lucille is silent for 8 days: its record in the pair room expires. `pair status`, run in
    # another shell, resolves first and moves pair:<name> itself; the library reports that move
    # to that resolve alone.
    h.advance(8 * 86400)
    assert host.restart().status()[0] == "confirmed"
    after = pairing.resolve(
        host.name, None, store=host.opened.pairs, connect=host.opened.connect, now=h.now
    )
    assert after.moved_from is None and after.record.moved_reason == "lapsed"
    # A new run sees the move by its generation, at start.
    host.session = running = host.restart()
    running.start()
    assert host.record().generation == after.generation != first
    assert host.held() == []
    whys = {gone.room: gone.why for gone in host.record().left.values()}
    assert whys == {room: rooms.MOVED}
    # Lucille follows, and offers a replacement, which the host takes.
    host.lucille.resolve()
    assert _taken(host, "work-2") == "room-2"


def test_the_gate_deadline_forgets_the_pair(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(h, host_env, tmp_path, open_gate=False)
    spy = Spy(monkeypatch)
    host.s.start()
    # `run` resolves, and has the SAS, but shows it nowhere: no deadline starts, and a day later
    # the pair is still kept, its gate shut.
    found = host.gate()
    assert found is not None and (found.shown_at, found.deadline) == (None, None)
    h.advance(gate.WINDOW + 60)
    host.s.step()
    assert host.opened.names() == [host.name] and host.gate() == found
    # `pair status` shows it: the deadline counts from there.
    shown = host.s.show(host.name)
    assert shown is not None
    found = shown[1]
    assert (found.shown_at, found.deadline) == (h.now, h.now + gate.WINDOW)
    h.advance(found.deadline - h.now)
    host.s.step()
    assert host.opened.names() == [host.name]
    h.advance(1)
    with pytest.raises(link.Stopped) as stopped:
        host.s.step()
    assert stopped.value.state == "expired"
    assert host.opened.names() == [] and host.gate() is None
    assert spy.calls["touch"] == 0


def _shown(host: Host) -> gate.Gate:
    """The SAS shown, as `pair status` shows it, and not confirmed yet."""
    shown = host.s.show(host.name)
    assert shown is not None
    return shown[1]


def test_a_confirm_while_a_step_resolves_is_kept_past_the_deadline(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(h, host_env, tmp_path, open_gate=False)
    running = host.s
    running.start()
    found = _shown(host)
    assert found.deadline is not None
    h.advance(found.deadline - h.now - 1)
    # While a step of `run` resolves, the user confirms, a second before the deadline; the
    # resolve returns two seconds later. The step then reads an open gate past the deadline.
    real = _switchboard.Link.resolve
    confirmed: list[str] = []

    def slow(opened: _switchboard.Link, name: str) -> _switchboard.Pair:
        if not confirmed:
            other = link.Session(host.env.dirs, host.opened, say=lambda _: None)
            confirmed.append(other.open_gate(name))
            h.advance(2)
        return real(opened, name)

    monkeypatch.setattr(_switchboard.Link, "resolve", slow)
    running.note(_switchboard.TAG_PAIRED)
    running.step()
    assert confirmed == [f"pair {host.name} is confirmed on this computer"]
    assert host.opened.names() == [host.name]
    opened = host.gate()
    assert opened is not None and opened.opened_at == found.deadline - 1
    assert not [e for e in host.events("pair") if e["event"] == "forgotten"]


class _Turns:
    """Two commands at once: ``first`` is held where ``pause`` says, then ``second`` runs while
    it is held, for as long as it can before it must wait for ``first``."""

    def __init__(self) -> None:
        self.paused, self.resume = threading.Event(), threading.Event()
        self.outcomes: dict[str, Any] = {}

    def hold(self) -> None:
        """Called where ``first`` is to be held, the first time only."""
        if not self.paused.is_set():
            self.paused.set()
            assert self.resume.wait(10)

    def _call(self, key: str, action: Callable[[], Any]) -> threading.Thread:
        def body() -> None:
            try:
                self.outcomes[key] = action()
            except link.Stopped as exc:
                self.outcomes[key] = exc.state

        thread = threading.Thread(target=body, daemon=True)
        thread.start()
        return thread

    def run(self, first: Callable[[], Any], second: Callable[[], Any]) -> None:
        held = self._call("first", first)
        assert self.paused.wait(10)
        other = self._call("second", second)
        other.join(1)  # it waits for `first`, or runs meanwhile
        self.resume.set()
        held.join(10)
        other.join(10)
        assert not held.is_alive() and not other.is_alive()


def test_a_confirm_beside_forgetting_waits_for_it(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `forget` is between forgetting the pair and removing the gate's record when `pair confirm`
    # reads that record. Before, the confirm opened the gate of a pair already forgotten and said
    # so; now it waits, finds no record, and says the pair is gone (not that it needs a reject).
    host = _host(h, host_env, tmp_path, open_gate=False)
    _shown(host)
    other = _switchboard.open_link(
        host.env.dirs, host.config, client_class=h.client_class(), clock=lambda: h.now
    )
    forgetting = link.Session(host.env.dirs, other, say=lambda _: None)
    turns = _Turns()
    real = _switchboard.Link.forget

    def slow_forget(opened: _switchboard.Link, name: str) -> _switchboard.Told:
        told = real(opened, name)
        if opened is other:
            turns.hold()
        return told

    monkeypatch.setattr(_switchboard.Link, "forget", slow_forget)
    turns.run(forgetting.forget, lambda: host.s.open_gate(host.name))
    assert isinstance(turns.outcomes["first"], _switchboard.Told)
    assert turns.outcomes["second"] == "none"
    assert host.opened.names() == [] and host.gate() is None


def _held_in_save(turns: _Turns, monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    """Hold the first write of the gate's record that sets ``field``, before it is written."""
    real = gate.save

    def slow_save(path: Path, record: gate.Gate) -> None:
        if getattr(record, field) is not None:
            turns.hold()
        real(path, record)

    monkeypatch.setattr("lucille_host.gate.save", slow_save)


def test_showing_the_sas_beside_forgetting_leaves_no_record(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `pair status` has read the gate's record and is about to note that it showed the SAS when
    # `forget` runs. Before, `forget` removed the record first, and the note then wrote one back
    # for a pair that was gone.
    host = _host(h, host_env, tmp_path, open_gate=False)
    turns = _Turns()
    _held_in_save(turns, monkeypatch, "shown_at")
    forgetting = link.Session(host.env.dirs, host.opened, say=lambda _: None)
    turns.run(lambda: host.s.show(host.name), forgetting.forget)
    assert turns.outcomes["first"] is not None
    assert host.opened.names() == []
    assert not host.env.dirs.gate_file.exists()


@pytest.mark.parametrize("judge", ["run", "pair status"])
def test_a_confirm_in_time_is_never_expired_beside_it(
    judge: str, host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `pair confirm` opens the gate a second before the deadline, and is about to write that
    # when the deadline passes and `run`, or `pair status`, judges the gate. Before, either read
    # the gate still shut, past its deadline, and forgot the pair the user had just confirmed.
    host = _host(h, host_env, tmp_path, open_gate=False)
    running = host.s
    running.start()
    found = _shown(host)
    assert found.deadline is not None
    h.advance(found.deadline - h.now - 1)
    turns = _Turns()
    _held_in_save(turns, monkeypatch, "opened_at")
    confirming = link.Session(host.env.dirs, host.opened, say=lambda _: None)

    def judged() -> Any:
        h.advance(2)
        if judge == "run":
            running.note(_switchboard.TAG_PAIRED)
            return running.step()
        return link.Session(host.env.dirs, host.opened, say=lambda _: None).status()

    turns.run(lambda: confirming.open_gate(host.name), judged)
    assert turns.outcomes["first"] == f"pair {host.name} is confirmed on this computer"
    assert turns.outcomes["second"] == (
        None if judge == "run" else ("confirmed", f"pair {host.name}")
    )
    assert host.opened.names() == [host.name]
    opened = host.gate()
    assert opened is not None and opened.opened_at is not None


def test_ended_by_peer_deletes_local_copy(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    room = _taken_at_start(host, "work-1")
    seen: list[list[str]] = []
    real = pairing.forget

    def forget(*args: Any, **options: Any) -> Any:
        seen.append(host.store_rooms())
        return real(*args, **options)

    # Lucille removes the computer: it deletes its half and tells the host.
    host.lucille.forget()
    monkeypatch.setattr("switchboard.pairing.forget", forget)
    host.s.note(_switchboard.TAG_ENDED)
    with pytest.raises(link.Stopped) as stopped:
        host.s.step()
    assert stopped.value.state == "ended" and "ended_by_peer" in str(stopped.value)
    # Its rooms were left before its own copy of the pair was deleted.
    assert seen == [[]]
    assert host.opened.names() == [] and host.gate() is None
    assert host.store_rooms() == []
    whys = {gone.room: gone.why for gone in host.record().left.values()}
    assert whys == {room: rooms.PAIR_OVER}
    order = [(e["kind"], e["event"]) for e in host.audit() if e["kind"] in ("room", "pair")]
    assert order[-4:] == [
        ("pair", "forget"),
        ("room", "abandoned"),
        ("room", "left"),
        ("pair", "forgotten"),
    ]


def test_forget_leaves_rooms_first(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    room = _taken_at_start(host, "work-1")
    real = _switchboard.Link.leave_room

    def cannot(opened: _switchboard.Link, name: str) -> _switchboard.RoomLeft:
        raise _switchboard.RoomError("store", "the share store could not be written")

    # A room that cannot be left keeps the pair.
    monkeypatch.setattr(_switchboard.Link, "leave_room", cannot)
    with pytest.raises(link.Stopped) as stopped:
        host.s.forget()
    assert stopped.value.state == "error"
    assert host.opened.names() == [host.name] and host.gate() is not None
    assert host.record().held[room].abandoned == rooms.PAIR_OVER
    monkeypatch.setattr(_switchboard.Link, "leave_room", real)
    seen: list[list[str]] = []
    forget = pairing.forget

    def watched(*args: Any, **options: Any) -> Any:
        seen.append(host.store_rooms())
        return forget(*args, **options)

    monkeypatch.setattr("switchboard.pairing.forget", watched)
    told = host.restart().forget()
    assert told.told
    assert seen == [[]], "the share store held no room when the pair was forgotten"
    assert host.opened.names() == [] and host.gate() is None
    assert host.stopped == [room]
    record = host.record()
    assert record == rooms.Record(left=record.left, named=record.named)
    assert [gone.room for gone in record.left.values()] == [room]


def test_forget_marks_every_room_abandoned_before_it_leaves_one(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A leave is a hub call, which can take up to the client's timeout, and a `run` beside this
    # stops its work in a room once the room record marks it abandoned. So each room is marked,
    # and stopped, before the first leave, not one leave after the other.
    room = _taken_at_start(host, "work-1")
    # Two rooms held: a stop came between the accept of a newer room and its checks.
    accept = _switchboard.Link.accept_room

    def stop_after(opened: _switchboard.Link, *args: Any) -> _switchboard.RoomTaken:
        accept(opened, *args)
        raise KeyboardInterrupt

    monkeypatch.setattr(_switchboard.Link, "accept_room", stop_after)
    host.lucille.offer("work-2")
    host.s.note(SHARED)
    with pytest.raises(KeyboardInterrupt):
        host.s.step()
    monkeypatch.setattr(_switchboard.Link, "accept_room", accept)
    accepting = host.record().accepting
    assert accepting is not None
    both = sorted([room, accepting.room])
    stopped = len(host.stopped)
    seen: list[tuple[list[str], dict[str, str | None]]] = []
    leave = _switchboard.Link.leave_room

    def watched(opened: _switchboard.Link, name: str) -> _switchboard.RoomLeft:
        marks = {held: kept.abandoned for held, kept in host.record().held.items()}
        seen.append((host.stopped[stopped:], marks))
        return leave(opened, name)

    monkeypatch.setattr(_switchboard.Link, "leave_room", watched)
    assert host.restart().forget().told
    assert len(seen) == 2
    assert seen[0] == (both, dict.fromkeys(both, rooms.PAIR_OVER))
    assert host.held() == [] and host.store_rooms() == []
    order = [(e["event"], e.get("room")) for e in host.events("room")][-4:]
    assert order == [("abandoned", both[0]), ("abandoned", both[1])] + [
        ("left", name) for name in both
    ]


def test_forget_and_run_never_change_the_room_record_at_once(
    host: Host, h: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `run` takes a newer room while `forget`, another process, is between its read of the room
    # record and its write. Before, that write dropped the room `run` had recorded, and when
    # `run` then found the pair gone, it left only the rooms the record named: the newer room,
    # and its keys, stayed in the share store after the pair was over.
    _taken_at_start(host, "work-1")
    running = host.s
    other = _switchboard.open_link(
        host.env.dirs, host.config, client_class=h.client_class(), clock=lambda: h.now
    )
    forgetting = link.Session(host.env.dirs, other, say=lambda _: None)
    paused, resume = threading.Event(), threading.Event()
    real = _switchboard.Link.leave_room

    def slow_leave(opened: _switchboard.Link, name: str) -> _switchboard.RoomLeft:
        if opened is other and not paused.is_set():
            paused.set()
            resume.wait(10)
        return real(opened, name)

    monkeypatch.setattr(_switchboard.Link, "leave_room", slow_leave)
    outcomes: list[Any] = []

    def call(action: Callable[[], Any]) -> threading.Thread:
        def body() -> None:
            try:
                outcomes.append(action())
            except link.Stopped as exc:
                outcomes.append(exc.state)

        thread = threading.Thread(target=body, daemon=True)
        thread.start()
        return thread

    forgotten = call(forgetting.forget)
    assert paused.wait(10)
    host.lucille.offer("work-2")
    running.note(SHARED)
    stepped = call(running.step)
    stepped.join(2)  # it waits for `forget` to finish, or takes the room meanwhile
    resume.set()
    forgotten.join(10)
    stepped.join(10)
    assert not forgotten.is_alive() and not stepped.is_alive()
    # A step that resolves finds the pair gone, and leaves the rooms the record names.
    running.note(_switchboard.TAG_ENDED)
    with pytest.raises(link.Stopped):
        running.step()
    assert host.opened.names() == []
    assert host.store_rooms() == [], "a room of the pair that is over kept its keys here"
    assert host.record().held == {}


@pytest.mark.parametrize("again", ["second", "lucille"], ids=["another name", "the same name"])
def test_a_run_whose_pair_another_command_forgot_touches_no_other_pair(
    again: str, host: Host, h: Any
) -> None:
    # `run` serves the pair. Another command forgets it, pairs again (under another name, or
    # under the same one), confirms, and takes a room, all before `run`'s next step. Before,
    # that step resolved the old pair, found it unknown and forgot it again, which deleted the
    # new pair's gate record (or, under the same name, served the new pair as its own).
    old = host.s
    old.start()
    _taken(host, "work-1")
    other = link.Session(host.env.dirs, host.opened, say=lambda _: None)
    other.stop_room = host.stopped.append
    assert other.forget().told
    # A minute later (the gate's record says when it was made; the test hub's clock stands still).
    h.advance(60)
    lucille = FakeLucille(h, host.folder / "lucille-2", url=_switchboard.LUCILLE_HUB)
    host.name = _confirm(other, lucille, host.config, again)
    host.session, host.lucille = other, lucille
    other.start()
    room = _taken(host, "work-2")
    before = (host.gate(), host.record(), host.store_rooms(), host.opened.names())
    assert before[0] is not None and before[0].opened_at is not None
    assert before[2] == [room] and before[3] == [host.name]
    lines = len(host.audit())
    # Each way the old `run` acts finds its pair gone, and stops there.
    old.note(_switchboard.TAG_ENDED)
    for action in (old.step, lambda: old.check_room(room), old.usable, old.consider_offers):
        with pytest.raises(link.Stopped) as stopped:
            action()
        assert stopped.value.state == "none"
        assert str(stopped.value) == (
            "pair lucille is gone from this computer: another command forgot it"
        )
    assert (host.gate(), host.record(), host.store_rooms(), host.opened.names()) == before
    assert host.audit()[lines:] == []
    # The new pair goes on as it was.
    assert other.check_room(room) is not None


def test_a_command_that_finds_its_pair_gone_leaves_another_pairs_state(
    host: Host, h: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `pair status` takes the pair held, and resolves it. Meanwhile another command forgets
    # that pair, pairs again under another name, confirms, and takes a room. The resolve then
    # finds the first pair gone. Before, `pair status` forgot it again: it left the new pair's
    # room and deleted its gate record.
    stale = _switchboard.open_link(
        host.env.dirs, host.config, client_class=h.client_class(), clock=lambda: h.now
    )
    status = link.Session(host.env.dirs, stale, say=lambda _: None)
    other = host.s
    real = _switchboard.Link.resolve
    made: list[str] = []

    def resolve(opened: _switchboard.Link, name: str) -> _switchboard.Pair:
        if opened is stale and not made:
            assert other.forget().told
            lucille = FakeLucille(h, host.folder / "lucille-2", url=_switchboard.LUCILLE_HUB)
            host.name = _confirm(other, lucille, host.config, "second")
            host.lucille = lucille
            other.start()
            made.append(_taken(host, "work-2"))
        return real(opened, name)

    monkeypatch.setattr(_switchboard.Link, "resolve", resolve)
    with pytest.raises(link.Stopped):
        status.status()
    (room,) = made
    found = host.gate()
    assert found is not None and found.pair == "second" and found.opened_at is not None
    assert host.held() == [room] and host.store_rooms() == [room]
    assert host.record().held[room].abandoned is None
    assert host.opened.names() == ["second"]
    assert other.check_room(room) is not None


def test_a_reject_beside_a_confirm_waits_for_it(
    host_env: HostEnv, h: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `pair confirm` is about to write that the gate opened when `pair reject` (or `n` at
    # `pair`'s prompt) runs. Before, the reject read the gate still shut and forgot a pair the
    # confirm then reported confirmed; now it waits, finds the gate open, and refuses.
    host = _host(h, host_env, tmp_path, open_gate=False)
    _shown(host)
    turns = _Turns()
    _held_in_save(turns, monkeypatch, "opened_at")
    rejecting = link.Session(host.env.dirs, host.opened, say=lambda _: None)
    turns.run(lambda: host.s.open_gate(host.name), rejecting.reject)
    assert turns.outcomes["first"] == f"pair {host.name} is confirmed on this computer"
    assert turns.outcomes["second"] == "refused"
    assert host.opened.names() == [host.name]
    found = host.gate()
    assert found is not None and found.opened_at is not None


def _waits_for(lock: Path, action: Callable[[], Any]) -> Any:
    """Run ``action`` while another process holds ``lock``: it must not finish until the lock
    is let go. Returns what it returned."""
    done = threading.Event()
    outcome: list[Any] = []

    def body() -> None:
        try:
            outcome.append(action())
        except link.Stopped as exc:
            outcome.append(exc.state)
        done.set()

    with lock_held(lock):
        thread = threading.Thread(target=body, daemon=True)
        thread.start()
        assert not done.wait(0.5), "it did not wait for the other process"
    assert done.wait(10)
    return outcome[0]


def test_the_record_lock_keeps_another_process_out(
    host_env: HostEnv, h: Any, tmp_path: Path
) -> None:
    # The record lock is an os.lockf lock beside a thread lock: another process (a `pair
    # confirm`, a `forget`, a `run` in another shell) holding it keeps out each change of the
    # gate's record and each step, not only the threads of this process.
    host = _host(h, host_env, tmp_path, open_gate=False)
    s = host.s
    s.start()
    lock = host.env.dirs.state / link.RECORD_LOCK
    shown = _waits_for(lock, lambda: s.show(host.name))
    assert shown is not None and shown[0] == host.lucille.sas()
    confirmed = _waits_for(lock, lambda: s.open_gate(host.name))
    assert confirmed == f"pair {host.name} is confirmed on this computer"
    host.lucille.offer("work-1")
    s.note(SHARED)
    _waits_for(lock, s.step)
    assert host.held() == ["room-1"]
    assert _waits_for(lock, lambda: s.check_room("room-1")) is not None
    assert _waits_for(lock, link.Session(host.env.dirs, host.opened, say=print).reject) == (
        "refused"
    )
    told = _waits_for(lock, s.forget)
    assert isinstance(told, _switchboard.Told) and told.told
    assert host.opened.names() == [] and host.gate() is None


def test_run_refuses_a_pair_with_no_gate_record(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Such a pair can never be confirmed here (`pair status` and `pair confirm` refuse it), so
    # `run` refuses it too, rather than waiting for ever on a gate nothing can open.
    gate.remove(host.env.dirs.gate_file)
    spy = Spy(monkeypatch)
    s = host.restart()
    with pytest.raises(link.Stopped) as stopped:
        s.start()
    assert stopped.value.state == "refused"
    assert str(stopped.value) == (
        f"pair {host.name} has no local gate record, so it cannot be confirmed here: run "
        "`lucille-host pair reject` and start again in Lucille"
    )
    assert sum(spy.calls.values()) == 0
    assert host.opened.names() == [host.name]


def test_keyring_writes_bounded_over_a_day(host: Host, h: Any) -> None:
    room = _taken_at_start(host, "work-1")
    keyring = host.env.keyring
    before = keyring.writes
    # A day of the host's rhythm: a look-up of the room before each read (a long-poll every
    # 25 s), and a touch every 5 minutes. Unthrottled, each would write the keyring.
    for _ in range(288):
        for _ in range(12):
            h.advance(link.LONG_POLL)
            assert host.s.check_room(room) is not None
        host.s.step()
    written = keyring.writes - before
    # Once an hour for the stamps, and the few real changes of a day.
    assert 24 <= written <= 30, written


@pytest.mark.parametrize("config", [KEYRING, FILE], ids=["keyring", "file"])
def test_no_write_under_home_switchboard(
    config: state.HostConfig, host_env: HostEnv, h: Any, tmp_path: Path
) -> None:
    before = {path for path in host_env.home.rglob("*") if not path.is_dir()}
    with _audit_hook.recording() as events:
        host = _host(h, host_env, tmp_path, config=config)
        _taken_at_start(host, "work-1")
        host.lucille.heal()
        host.lucille.offer("work-2")
        host.s.note(_switchboard.TAG_MOVED)
        host.s.step()
        assert host.held() == ["room-2"]
        host.s.forget()
    assert not (host_env.home / ".switchboard").exists()
    written = {path for path in host_env.home.rglob("*") if not path.is_dir()} - before
    own = (host_env.dirs.config, host_env.dirs.state)
    assert written
    assert all(any(path.is_relative_to(folder) for folder in own) for path in written), written
    assert _audit_hook.spawns(events) == []


# --- the record -----------------------------------------------------------------------------------


def test_the_room_record(tmp_path: Path) -> None:
    path = tmp_path / "rooms.json"
    assert rooms.load(path) == rooms.Record()
    record = rooms.Record(
        pair="p",
        made=10.0,
        generation=3,
        offers={"work-1": 11.0},
        accepting=rooms.Accepting("work-1", "room-2", ("room-1",)),
        held={"room-1": rooms.Held("ws_a", 12.0), "room-0": rooms.Held("ws_b", 9.0, "replaced")},
        left={"ws_c": rooms.Left("room-x", rooms.CHECK_HUB, 8.0)},
        no_room=rooms.CHECK_HUB,
        named=2,
    )
    rooms.save(path, record)
    assert rooms.load(path) == record
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert record.in_use() == ["room-1"]
    assert rooms.local_name(record) == "room-3" and record.named == 3
    # A new pair, or the same name made again, keeps the rooms and starts its offers afresh.
    assert record.bind("p", 10.0) is False
    assert record.bind("p", 20.0) is True
    assert (record.offers, record.generation, record.no_room) == ({}, None, None)
    assert set(record.held) == {"room-0", "room-1"} and record.accepting is not None
    data = json.loads(path.read_text())
    for bad in (
        {**data, "v": 2},
        {**data, "extra": 1},
        {**data, "named": -1},
        {**data, "named": True},
        {**data, "generation": 1.5},
        {**data, "no_room": "check 9"},
        {**data, "offers": {"a": "soon"}},
        {**data, "held": {"r": {"workspace": "w"}}},
        {**data, "held": []},
        {**data, "left": {"w": {"room": "r", "why": 3, "at": 1}}},
        {**data, "accepting": {"offer": "a", "room": "b", "replaces": "c"}},
        [data],
    ):
        path.write_text(json.dumps(bad))
        with pytest.raises(state.StateError, match="not a lucille-host room record"):
            rooms.load(path)


def test_failed_check() -> None:
    record = rooms.Record(left={"ws_old": rooms.Left("room-1", rooms.REPLACED, 1.0)})
    hub_url = _switchboard.LUCILLE_HUB

    def check(url: str = hub_url, token: bool = False, workspace: str = "ws_new") -> str | None:
        return rooms.failed_check(
            url=url, carries_token=token, workspace=workspace, hub=hub_url, record=record
        )

    assert check() is None
    assert check(url=hub_url + "//") is None
    assert check(url=hub_url.upper()) == rooms.CHECK_HUB
    assert check(url=hub_url + ":443") == rooms.CHECK_HUB
    assert check(token=True) == rooms.CHECK_TOKEN
    assert check(workspace="ws_old") == rooms.LEFT_BEFORE
    assert set(rooms.CHECKS) == {rooms.CHECK_HUB, rooms.CHECK_TOKEN, rooms.LEFT_BEFORE}


def test_dirs_name_the_room_record(home: Path) -> None:
    from lucille_host import paths

    dirs = paths.dirs()
    assert dirs.rooms_file == dirs.state / "rooms.json"
