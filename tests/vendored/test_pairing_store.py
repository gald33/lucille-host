# Vendored from agent-switchboard v2.6.0 (tag v2.6.0, commit dbff172), tests/test_pairing_store.py.
#
# MIT License
#
# Copyright (c) 2026 Switchboard contributors
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# Changed here, and nothing else:
# - The store under test is the host's keyring store (KeyringPairs, over one keyring item per
#   side, in a keyring kept in memory, which an autouse fixture puts in place), not the template's
#   MemoryStore, which is removed with the imports only it used. A thin subclass, Instrumented,
#   adds the template's instrumentation to it (held, locks, broken; enabled is the store's own),
#   so the signature case reads the host's own methods.
# - Each Side is given the folder its store keeps its lock files in, and the "memory" case of the
#   signature test is the "keyring" case.
# - The test hub's deprecation warning from starlette is not shown.
# - Formatted, and its imports sorted, by this repository's ruff settings.
"""Pairing with a store that is not a directory.

A server that pairs on its users' behalf — one pairing per user who asks —
cannot keep pairs in `~/.switchboard/pairs`: its containers are replaced on
every deploy, and its users' secrets belong in its own encrypted table. So
every pairing call takes `store=`, and anything implementing
`pairing.Store` works there. These tests pair, talk, heal, expire and forget
through an in-memory store that has the protocol's members and nothing else,
the way such a server would write one, and hold the core to the contract it
documents: the lock never nested, never held across a hub call.
"""

from __future__ import annotations

import contextlib
import inspect
from dataclasses import replace

import pytest
from switchboard import pairing
from switchboard.pairing import PairingError, PairStore
from switchboard.testing import hub as make_hub

from lucille_host import store as host_store
from lucille_host._switchboard import KeyringPairs

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)


class Instrumented(KeyringPairs):
    """The host's keyring store, with the instrumentation the template's MemoryStore has: it
    records a nested lock in `broken`, counts the locks taken, and says whether one is held."""

    def __init__(self, item: host_store.Item) -> None:
        super().__init__(item)
        self.held = False
        self.locks = 0
        self.broken: list[str] = []

    @contextlib.contextmanager
    def lock(self):
        if self.held:
            self.broken.append("lock() taken while already held")
            raise AssertionError("lock() nested")
        self.held, self.locks = True, self.locks + 1
        try:
            with super().lock():
                yield
        finally:
            self.held = False


def keyring_store(folder, label: str) -> Instrumented:
    return Instrumented(host_store.Item(folder / label, label))


class Side:
    """One owner: a memory store, the shared hub, a client that checks the lock."""

    def __init__(self, h, label: str, folder) -> None:
        self.h = h
        self.label = label
        self.store = keyring_store(folder, label)
        self.base = h.client_config(workspace=f"team-{label}", agent_id=label)
        store = self.store

        class Guarded(h.client_class()):
            def _call(self, method, path, **kwargs):
                if store.held:
                    store.broken.append(f"{method} {path} with the store lock held")
                return super()._call(method, path, **kwargs)

        self.connect = Guarded

    def kw(self, **extra):
        return {
            "store": self.store,
            "connect": self.connect,
            "now": self.h.now,
            "agent_id": self.label,
            **extra,
        }


@pytest.fixture(autouse=True)
def _keyring(memory_keyring):
    return memory_keyring


@pytest.fixture
def h():
    with make_hub() as hub_:
        yield hub_


@pytest.fixture
def server(h, tmp_path):
    side = Side(h, "server", tmp_path)
    yield side
    assert not side.store.broken, side.store.broken


@pytest.fixture
def dev(h, tmp_path):
    side = Side(h, "dev", tmp_path)
    yield side
    assert not side.store.broken, side.store.broken


def pair(server: Side, dev: Side) -> str:
    started = pairing.start(
        server.base, store=server.store, connect=server.connect, now=server.h.now
    )
    pairing.accept(started.code, dev.base, **dev.kw())
    assert pairing.collect(started.name, server.base, **server.kw()).state == "paired"
    return started.name


@pytest.mark.parametrize(
    "make",
    [lambda tmp: PairStore(tmp / "pairs", env={}), lambda tmp: keyring_store(tmp, "keyring")],
    ids=["directory", "keyring"],
)
def test_a_store_has_every_member_the_core_uses_with_its_signature(make, tmp_path):
    store: pairing.Store = make(tmp_path)
    members = [n for n in vars(pairing.Store) if not n.startswith("_")]
    assert {"get", "save", "lock", "require", "enabled", "taken"} <= set(members)
    for member in members:
        assert hasattr(store, member), member
        wanted = vars(pairing.Store)[member]
        if isinstance(wanted, property):
            continue
        shape = [(p.name, p.kind) for p in inspect.signature(wanted).parameters.values()]
        given = [
            (p.name, p.kind)
            for p in inspect.signature(getattr(type(store), member)).parameters.values()
        ]
        assert given == shape, member


def test_two_owners_in_memory_pair_talk_heal_and_forget(server, dev, h):
    started = pairing.start(server.base, store=server.store, connect=server.connect, now=h.now)
    assert server.store.pending_names() == [started.name]
    assert [n for n, _ in pairing.pending_rooms(server.store, server.base, now=h.now)] == [
        started.name
    ]

    accepted = pairing.accept(started.code, dev.base, **dev.kw())
    assert accepted.state == "unconfirmed"
    assert pairing.collect(started.name, server.base, **server.kw()).state == "paired"
    assert server.store.pending_names() == [], "the ephemeral key must be gone"
    name = started.name

    mine = pairing.resolve(name, server.base, **server.kw())
    theirs = pairing.resolve(name, dev.base, **dev.kw())
    assert mine.state == theirs.state == "paired"
    assert mine.config.workspace == theirs.config.workspace
    with (
        server.connect(mine.config, agent_id="server") as a,
        dev.connect(theirs.config, agent_id="dev") as b,
    ):
        a.post("pair", "hello from the server")
        assert "hello from the server" in [m["body"] for m in b.history("pair")]

    healed = pairing.heal(name, dev.base, **dev.kw(reason="key in a log"))
    assert healed.to_generation == 1
    followed = pairing.resolve(name, server.base, **server.kw())
    assert followed.generation == 1 and followed.moved_from == 0
    assert dev.store.get(name).generation == 1, "the heal was saved through the store"

    [row] = pairing.status(server.base, **server.kw())
    assert row["state"] == "paired"
    assert pairing.active(server.store, now=h.now) == [name]

    gone = pairing.forget(name, dev.base, **dev.kw())
    assert gone.peer_told and dev.store.names() == []
    with pytest.raises(PairingError) as exc:
        pairing.resolve(name, server.base, **server.kw())
    assert exc.value.state == "ended_by_peer"
    assert server.store.locks and dev.store.locks, "the lock was taken, so it was checked"


def test_a_second_redeemer_leaves_nothing_in_its_store(server, dev, h, tmp_path):
    started = pairing.start(server.base, store=server.store, connect=server.connect, now=h.now)
    pairing.accept(started.code, dev.base, **dev.kw())
    thief = Side(h, "thief", tmp_path)
    with pytest.raises(pairing.AlreadyRedeemed):
        pairing.accept(started.code, thief.base, **thief.kw())
    assert thief.store.names() == [] and not thief.store.broken
    assert pairing.collect(started.name, server.base, **server.kw()).state == "paired"


def test_an_expired_code_leaves_a_verdict_until_its_keep_runs_out(server, h):
    started = pairing.start(server.base, store=server.store, connect=server.connect, now=h.now)
    swept = pairing.start(
        server.base, store=server.store, connect=server.connect, name="swept", now=h.now
    )
    h.advance(pairing.DEFAULT_TTL + pairing.COLLECT_WINDOW + 1)

    assert pairing.collect(started.name, server.base, **server.kw()).state == "expired"
    assert pairing.sweep_expired(server.store, now=h.now) == [swept.name]
    assert server.store.pending_names() == [], "no private key outlives its deadline"
    states = {row["name"]: row["state"] for row in pairing.status(server.base, **server.kw())}
    assert states == {started.name: "expired", swept.name: "expired"}

    h.advance(pairing.VERDICT_KEEP + 1)
    pairing.sweep_expired(server.store, now=h.now)
    assert server.store.verdict_names() == []
    assert pairing.status(server.base, **server.kw()) == []


def test_a_forgotten_pair_is_not_brought_back_by_a_stale_copy(server, dev):
    name = pair(server, dev)
    stale = server.store.get(name)
    server.store.delete(name)
    with pytest.raises(PairingError) as exc:
        pairing._keep(server.store, stale)
    assert exc.value.state == "ended"
    assert server.store.get(name) is None


def test_a_stale_copy_never_overwrites_another_pair_under_its_name(server, dev):
    """Forgotten and paired again meanwhile: the name now holds another pair,
    whose secret exists nowhere else."""
    name = pair(server, dev)
    stale = server.store.get(name)
    newer = replace(stale, secret=pairing._b64e(b"n" * 32))
    server.store.save(newer)
    with pytest.raises(PairingError) as exc:
        pairing._keep(server.store, stale)
    assert exc.value.state == "ended"
    assert server.store.get(name).secret == newer.secret


def test_a_store_that_is_off_refuses_rather_than_finds_nothing(h, tmp_path):
    off = Side(h, "off", tmp_path)
    off.store.enabled = False
    with pytest.raises(PairingError) as exc:
        pairing.start(off.base, store=off.store, connect=off.connect, now=h.now)
    assert exc.value.state == "disabled"
    with pytest.raises(PairingError) as exc:
        pairing.status(off.base, **off.kw())
    assert exc.value.state == "disabled"


def test_a_store_failure_costs_only_its_own_row(server, dev, h):
    name = pair(server, dev)
    pairing.start(
        server.base, store=server.store, connect=server.connect, name="waiting", now=h.now
    )
    real_get, real_pending = server.store.get, server.store.pending

    def get(each):
        if each == name:
            raise OSError("connection to the secrets table lost")
        return real_get(each)

    def pending(each):
        if each == "waiting":
            raise OSError("connection to the secrets table lost")
        return real_pending(each)

    server.store.get, server.store.pending = get, pending
    assert pairing.active(server.store, now=h.now) == [name], (
        "listed, so the listener's resolve says why it cannot park there"
    )
    assert pairing.pending_rooms(server.store, server.base, now=h.now) == []
    assert pairing.sweep_expired(server.store, now=h.now) == []
    rows = {row["name"]: row for row in pairing.status(server.base, **server.kw())}
    assert rows[name]["state"] == "error" and "secrets table" in rows[name]["error"]
