"""``lucille-host pair``: taking Lucille's code, waiting for it to collect the answer, and the gate.

The command runs in-process against Switchboard's test hub, with a fake Lucille on the other side
(``tests/_cli.py``). Lucille's codes name Lucille's hub; the in-process transport reaches the test
hub whatever the address.
"""

from __future__ import annotations

import dataclasses
import errno
import io
import json
import os
import queue
import random
import re
import select
import termios
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from keyring.backends.fail import Keyring as FailKeyring
from switchboard import client, pairing
from switchboard.testing import hub

from lucille_host import _switchboard, connect, gate, link, loop, paths, policy, spec, state, store
from lucille_host.cli import ALREADY_PAIRING, PAIR_LOCK, PASTE_IS_GONE, main
from tests import _audit_hook
from tests._cli import STDIN, HostCli, collecting, until
from tests._host import HostEnv, lock_free, lock_held
from tests._keyring import NULL_SENTENCE, MemoryKeyring, no_session_bus, turned_off
from tests._lucille import FakeLucille
from tests.spec import _support

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)

LOOPBACK = "http://127.0.0.1:8765"


@pytest.fixture
def h() -> Iterator[Any]:
    with hub() as handle:
        yield handle


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
    # A run still waiting when the test ends stops before the hub does.
    host.stopping.set()


def _delivered(cli: HostCli, lucille: FakeLucille, running: Any, **delivery: Any) -> None:
    """Lucille answers the request ``running`` keeps, once it keeps one."""
    request = cli.waiting(running)
    assert request is not None, "no Connect request was kept"
    lucille.deliver(connect.shown(request.code), cli.signer, **delivery)


def _recode(code: str, **changes: Any) -> str:
    """The same code with some of its fields changed, as somebody who held it could make."""
    return dataclasses.replace(pairing.PairingCode.decode(code), **changes).encode()


def _enable_and_add_a_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "docs"
    folder.mkdir()
    assert main(["ops", "enable", "files.sha256"]) == 0
    assert main(["roots", "add", "docs", str(folder)]) == 0
    return folder


def _objects(ran: Any) -> list[dict[str, Any]]:
    """What a command run with ``--json`` wrote: one object per line, each held to its schema."""
    return _support.cli_objects(ran.out)


def _nothing_changed(cli: HostCli, before: policy.Policy) -> None:
    assert cli.gate() is None
    assert cli.held() == []
    assert cli.policy() == before
    assert not cli.dirs.previous_policy_file.exists()
    assert not cli.dirs.host_file.exists()


# --- the code ------------------------------------------------------------------------------------


def _refused_and_spent(cli: HostCli, ran: Any, before: policy.Policy) -> None:
    """A delivered code the host refused: nothing answered or changed, and the request spent."""
    assert ran.rc == 1
    _nothing_changed(cli, before)
    assert not cli.dirs.connect_file.exists()


def test_pair_refuses_foreign_hub(cli: HostCli, tmp_path: Path) -> None:
    _enable_and_add_a_folder(tmp_path)
    before = cli.policy()
    for url in ("https://hub.example", "https://switchboard.lucille-ai.com.example"):
        lucille = cli.lucille(url=url)
        ran = cli.connect(lucille)
        assert "refused: this code is for another hub" in ran.err, ran.err
        _refused_and_spent(cli, ran, before)
        # Nothing was answered: Lucille has nothing to collect.
        assert lucille.collect().state == "waiting"
    # A code for Lucille's own hub is accepted.
    ran = cli.connect(cli.lucille())
    assert ran.rc == 0, ran.text


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://switchboard.lucille-ai.com", id="http"),
        pytest.param("HTTPS://SWITCHBOARD.LUCILLE-AI.COM", id="upper-case"),
        pytest.param("https://Switchboard.Lucille-AI.com", id="host-case"),
        pytest.param("https://switchboard.lucille-ai.com:443", id="default-port"),
        pytest.param("https://switchboard.lucille-ai.com/path", id="path"),
        pytest.param("https://switchboard.lucille-ai.com/?q", id="query"),
        pytest.param("https://switchboard.lucille-ai.com#f", id="fragment"),
    ],
)
def test_pair_compares_the_hub_as_a_string(url: str, cli: HostCli) -> None:
    # Only Lucille's hub URL as it is written, every trailing / aside: another spelling of it is
    # another hub, as check 3 has it for a room's hub.
    lucille = cli.lucille(url="https://hub.example")
    delivered = []

    def respelled(code: str) -> str:
        delivered.append(_recode(code, url=url))
        return delivered[-1]

    ran = cli.connect(lucille, recode=respelled)
    assert pairing.PairingCode.decode(delivered[0]).url == url
    assert ran.rc == 1
    assert "refused: this code is for another hub" in ran.err, ran.err
    assert cli.gate() is None and cli.held() == []


def test_pair_refuses_code_with_token(cli: HostCli, tmp_path: Path) -> None:
    _enable_and_add_a_folder(tmp_path)
    before = cli.policy()
    lucille = cli.lucille()
    ran = cli.connect(lucille, recode=lambda code: _recode(code, token="sb_a_hub_token"))
    assert "refused: this code carries a hub token" in ran.err, ran.err
    assert "sb_a_hub_token" not in ran.text
    _refused_and_spent(cli, ran, before)
    assert lucille.collect().state == "waiting"


def test_pair_refuses_unconfigured_loopback(cli: HostCli, tmp_path: Path) -> None:
    _enable_and_add_a_folder(tmp_path)
    before = cli.policy()
    for options in ([], ["--hub", "http://127.0.0.1:8766"], ["--hub", "http://localhost:8765"]):
        lucille = cli.lucille(url=LOOPBACK)
        ran = cli.connect(lucille, *options)
        assert "refused: " in ran.err, ran.err
        assert "--hub" in ran.err
        assert ran.rc == 1 and cli.gate() is None and cli.held() == []
        assert cli.policy() == before and not cli.dirs.connect_file.exists()
        assert lucille.collect().state == "waiting"


def test_pair_accepts_configured_loopback(cli: HostCli) -> None:
    for wrong in ("https://127.0.0.1:8765", "http://127.0.0.1:0", "http://10.0.0.1:8765"):
        ran = cli.run("pair", "begin", "--hub", wrong, stdin="telegram\nada\n")
        assert ran.rc == 1 and ran.err.startswith("lucille-host: a configured hub"), ran.err
    lucille = cli.lucille(url=LOOPBACK)
    ran = cli.paired(lucille, "--hub", LOOPBACK)
    assert f"on {LOOPBACK}" in ran.out
    saved = json.loads(cli.dirs.host_file.read_text())
    assert saved == {"v": 1, "hub": LOOPBACK, "store": "keyring"}
    found = cli.gate()
    assert found is not None and found.hub == LOOPBACK
    # With --json, in another profile: the code's hub is the loopback hub, as the text says.
    second = cli.lucille(url=LOOPBACK)
    ran = cli.connect(second, "--json", "--hub", LOOPBACK, profile="json")
    assert ran.rc == 0, ran.text
    first = next(o for o in _objects(ran) if o["type"] == "pair_code")
    assert (first["name"], first["hub"]) == (second.name, LOOPBACK)


def test_pair_note_shown_sanitized(cli: HostCli) -> None:
    # Lucille's own note names the request; anything else in a note is refused before it is
    # shown (`not_this_mac`), so a note with more in it is never shown at all.
    lucille = cli.lucille()
    unsafe = "Gal's‮ laptop\x1b[2J⁦x‏؜ \x85\ud800" + "n" * 400
    ran = cli.connect(lucille, note=unsafe)
    assert ran.rc == 1, ran.text
    assert "laptop" not in ran.text and "\x1b" not in ran.text
    assert cli.gate() is None and cli.held() == []
    # The note of a code that answers the request is shown, made safe, in text and in JSON.
    lucille = cli.lucille()
    ran = cli.paired(lucille)
    notes = [line for line in ran.out.splitlines() if line.startswith("note: ")]
    assert len(notes) == 1
    shown = notes[0].removeprefix("note: ")
    assert re.fullmatch(r"lucille-host/2 n=[0-9a-f]{32}", shown), shown
    second = cli.lucille()
    ran = cli.connect(second, "--json", profile="json")
    assert ran.rc == 0, ran.text
    first = next(o for o in _objects(ran) if o["type"] == "pair_code")
    assert first["note"] == link.sanitize(first["note"])
    assert re.fullmatch(r"lucille-host/2 n=[0-9a-f]{32}", first["note"])
    assert list(_support.validator_for(spec.load_cli_schema("pair_code")).iter_errors(first)) == []


def test_sanitize_replaces_every_unsafe_character() -> None:
    unsafe = [chr(code) for code in range(0x20)] + [chr(code) for code in range(0x7F, 0xA0)]
    unsafe += ["\ud800", "\udfff", "؜", "‎", "‏", " ", " "]
    unsafe += [chr(code) for code in range(0x202A, 0x202F)]
    unsafe += [chr(code) for code in range(0x2066, 0x206A)]
    for char in unsafe:
        assert link.sanitize(f"a{char}b") == "a b", repr(char)
    assert link.sanitize("Gal's laptop, 2nd floor é") == "Gal's laptop, 2nd floor é"
    assert link.sanitize("x" * 300) == "x" * 200


# What the host says in place of each sentence of switchboard 2.6.0's that a refusal ending nothing
# can carry (link.refusal), and in place of any other. Written out here, not read from link.py, so
# that a change to one of them is a change to this test too.
NO_CRYPTOGRAPHY = (
    "this computer could not load the encryption library pairing needs (`cryptography`)"
)
FILE_NOT_JSON = (
    "the pair's file in the host's store folder is not valid JSON; it holds the pair secret, so "
    "it is left as it is: move it aside"
)
OTHER_FILE_NOT_JSON = (
    "a file the pairing library keeps in the host's store folder is not valid JSON; it is left "
    "as it is: move it aside"
)
NOT_A_RECORD = "the pair's record in the host's store is damaged: it is not a pair record"
OTHER_PROTOCOL = (
    "the pair's record in the host's store is for a pairing protocol this release does not speak"
)
MISSING_FIELD = "the pair's record in the host's store is missing a field"
UNKNOWN_ROLE = (
    "the pair's record in the host's store names a role or a healing strategy this release does "
    "not know"
)
BAD_PENDING = "an unfinished pairing in the host's store is damaged"
BAD_NAME = "the pair's record in the host's store holds a name pairing does not allow"
BAD_GENERATION = (
    "the pair's record in the host's store holds a work-room number that cannot be used"
)
BAD_KEY = (
    "the code's handshake key cannot be used: the code was tampered with, or was not made by a "
    "pairing library"
)
TAMPERED = (
    "the pair room holds a record that is not what either side wrote: the hub is serving "
    "something it was not given, and nothing was changed"
)
UPGRADE = (
    "the pair was moved on with a healing strategy this release does not know: update the host "
    "before using the pair again"
)
ODD_SAS = "the store holds a SAS of another shape, which the host never shows"
NO_PAIRS = "this profile keeps no pairs"
UNREPEATED = "the pairing library refused it for a reason this host does not repeat"
HOST_WORDS = frozenset(
    {
        NO_CRYPTOGRAPHY,
        FILE_NOT_JSON,
        OTHER_FILE_NOT_JSON,
        NOT_A_RECORD,
        OTHER_PROTOCOL,
        MISSING_FIELD,
        UNKNOWN_ROLE,
        BAD_PENDING,
        BAD_NAME,
        BAD_GENERATION,
        BAD_KEY,
        TAMPERED,
        UPGRADE,
        ODD_SAS,
        NO_PAIRS,
        UNREPEATED,
    }
)
# The library's own state words, and the host's `invalid_sas`, which a refusal repeats.
STATE_WORDS = frozenset(
    {
        "error",
        "store",
        "disabled",
        "unconfirmed",
        "name_taken",
        "tampered",
        "upgrade",
        "pending",
        "unreachable",
        "conflict",
        "invalid_sas",
        "already_redeemed",
        "aborted",
        "superseded",
        "expired",
        "never_confirmed",
        "unsent",
        "wrong_hub",
        "ended",
        "ended_by_peer",
        "unknown",
    }
)

MAC_STORE = "/Users/x/Library/Application Support/lucille-host/default/state/pairs"
LINUX_STORE = "/home/x/.local/state/lucille-host/default/pairs"
NOT_OVERWRITTEN = "it holds a pair secret, so it is not overwritten — move it aside"
# Each sentence of switchboard 2.6.0's that the host has words for, as the library writes it, with
# its variable parts filled in as a host call can meet them (pairing.py, at the line given).
LIBRARY_SENTENCES = (
    # :299, accept and resolve, with `cryptography` not loaded.
    ("pairing needs the `cryptography` package", NO_CRYPTOGRAPHY),
    # :938, the file store's record of the pair, or another of its files, that is not JSON.
    (
        f"{MAC_STORE}/pair-ab12cd.json is not valid JSON (Expecting property name enclosed in "
        f"double quotes: line 1 column 2 (char 1)); {NOT_OVERWRITTEN}",
        FILE_NOT_JSON,
    ),
    (
        f"{LINUX_STORE}/p.json is not valid JSON ('utf-8' codec can't decode byte 0xff in "
        f"position 0: invalid start byte); {NOT_OVERWRITTEN}",
        FILE_NOT_JSON,
    ),
    (
        f"{LINUX_STORE}/p.json is not valid JSON ('utf-8' codec can't decode bytes in position "
        f"4-5: unexpected end of data); {NOT_OVERWRITTEN}",
        FILE_NOT_JSON,
    ),
    (
        f"{MAC_STORE}/pending/p.json is not valid JSON ('ascii' codec can't decode byte 0xc3 in "
        f"position 3: ordinal not in range(128)); {NOT_OVERWRITTEN}",
        OTHER_FILE_NOT_JSON,
    ),
    (
        f"{LINUX_STORE}/voided/p-2.json is not valid JSON (Extra data: line 1 column 3 (char 2)); "
        f"{NOT_OVERWRITTEN}",
        OTHER_FILE_NOT_JSON,
    ),
    # :729, :731, :740, :742, a record read from the keyring item or a file of the file store.
    ("pair 'pair-ab12cd' in the keyring is not a pair record", NOT_A_RECORD),
    (f"{MAC_STORE}/pair-ab12cd.json is not a pair record", NOT_A_RECORD),
    (
        "pair 'p' in the keyring is pairing protocol 2; this release speaks 1. Upgrade "
        "agent-switchboard rather than editing it.",
        OTHER_PROTOCOL,
    ),
    (
        f"{LINUX_STORE}/p.json is pairing protocol None; this release speaks 1. Upgrade "
        "agent-switchboard rather than editing it.",
        OTHER_PROTOCOL,
    ),
    (
        "pair 'p' in the keyring is missing a field (PairRecord.__init__() missing 1 required "
        "positional argument: 'secret')",
        MISSING_FIELD,
    ),
    (
        f"{MAC_STORE}/p.json is missing a field (PairRecord.__init__() missing 3 required "
        "positional arguments: 'role', 'secret', and 'transcript')",
        MISSING_FIELD,
    ),
    (
        "pair 'p' in the keyring names role 'boss' / strategy 'derive', which this release does "
        "not know — upgrade.",
        UNKNOWN_ROLE,
    ),
    # :787, :792, an unfinished pairing (the host never starts one).
    ("pairing 'p' in the keyring is not a protocol 1 pending handshake", BAD_PENDING),
    (
        f"{LINUX_STORE}/pending/p.json is missing a field (PendingHandshake.__init__() missing 1 "
        "required positional argument: 'secret')",
        BAD_PENDING,
    ),
    # :375, the name in a record of the store.
    (
        "'Pair A' is not a usable pair name: use lowercase letters, digits and dashes, starting "
        "with a letter or digit, at most 32 characters.",
        BAD_NAME,
    ),
    # :442, the generation in a record of the store.
    ("invalid work-room generation '7'", BAD_GENERATION),
    ("invalid work-room generation -1", BAD_GENERATION),
    # :340, accept, with a code whose handshake key is a low-order point.
    (
        "the other side's handshake key is not a usable X25519 key — the answer was tampered with "
        "or written by something that is not a switchboard client. Nothing was paired.",
        BAD_KEY,
    ),
    # :2124, :2131, :2136, a resolve that read the pair room.
    (
        "the pair room holds a record this pair's key cannot open — the hub is serving something "
        "it was not given. Nothing was changed.",
        TAMPERED,
    ),
    (
        "the pair room's initiator record is not what was written there — refusing it as "
        "tampering.",
        TAMPERED,
    ),
    (
        "the other side moved this pair on with healing strategy 'rotate', which this release "
        "does not know. Upgrade agent-switchboard here before using the pair again.",
        UPGRADE,
    ),
    (
        "the other side moved this pair on with healing strategy None, which this release does "
        "not know. Upgrade agent-switchboard here before using the pair again.",
        UPGRADE,
    ),
    # The host's own (_switchboard.py: Link.sas, KeyringPairs.require).
    ("pair pair-ab12cd: the store holds a SAS of another shape", ODD_SAS),
    ("this profile keeps no pairs", NO_PAIRS),
)

# What must never reach a message: a pairing code, a pair's export, a room's join code, a token,
# a SAS, a path, and words the other side wrote.
CODE = "swbp1_eyJ2IjoxLCJ1IjoiaHR0cHM6Ly9zd2l0Y2hib2FyZC5sdWNpbGxlLWFpLmNvbSJ9"
EXPORT = "swbsecret1_eyJ2IjoxLCJzZWNyZXQiOiJBQUFBIn0"
OLD_EXPORT = "swbps1_eyJ2IjoxLCJzZWNyZXQiOiJBQUFBIn0"
# A room's join code, the library's other kind of code. Built from two literals, as
# tools/guards/switchboard_lint.py builds the prefix it refuses in any one literal: the host takes
# none, and this test only shows that none is ever repeated.
JOIN = "swb1" + "_eyJ1IjoiaHR0cHM6Ly9odWIuZXhhbXBsZSJ9"
TOKEN = "token=sk_live_4f9a1c2e7d"
SAS = "4821-0937"
PATH = "/Users/x/Documents/taxes 2026/return.pdf"
WRITTEN = "Lucille's side says: wire the money to account 12345"
SECRETS = (CODE, EXPORT, OLD_EXPORT, JOIN, TOKEN, SAS, PATH, WRITTEN)


def _hostile() -> tuple[list[str], list[str]]:
    """Sentences no host call can get from 2.6.0, made to carry what must never be repeated, in two
    lists.

    The first holds each known sentence with a secret after it (after a space, a line break or
    nothing) or before it, with a character a reader may not see before or after it, and in
    capitals: none is a sentence the host has words for. An absolute path before a sentence that
    begins with a path is left out of it, since the two read as one longer path, which the bounds
    allow. The second holds those, each secret alone or in a sentence of its own, and each put where
    a variable part is: within that part's bounds, it is a sentence of the library's all the
    same."""
    added: list[str] = []
    placed: list[str] = []
    for secret in SECRETS:
        placed += [
            secret,
            f"pair 'p' cannot be used: {secret}",
            f"{secret} is not a pair record",
            f"pair '{secret}' in the keyring is not a pair record",
            f"{secret}.json is not valid JSON (Expecting value: line 1 column 1 (char 0)); "
            f"{NOT_OVERWRITTEN}",
            f"invalid work-room generation {secret!r}",
            f"the other side moved this pair on with healing strategy {secret + 'x' * 64!r}, "
            "which this release does not know. Upgrade agent-switchboard here before using the "
            "pair again.",
            f"pair {secret}: the store holds a SAS of another shape",
        ]
        for sentence, _words in LIBRARY_SENTENCES:
            added += [f"{sentence} {secret}", f"{sentence}\n{secret}", f"{sentence}{secret}"]
            before = f"{secret} {sentence}"
            if secret.startswith("/") and sentence.startswith("/"):
                placed.append(before)
            else:
                added.append(before)
    for sentence, _words in LIBRARY_SENTENCES:
        # A known sentence made unlike itself by a character a reader may not see.
        for odd in ("​", "﻿", "⁠", "­", "᠎", "￹", "\U000e0041"):
            added += [sentence + odd, odd + sentence]
        added.append(sentence.upper())
    return added, placed


def test_a_refusal_says_each_known_sentence_in_the_hosts_words() -> None:
    # The host's words, the state, and the host's own sentence for the library's: never the
    # library's, whose path, name or value is not repeated.
    for sentence, words in LIBRARY_SENTENCES:
        refused = _switchboard.PairFailed("error", sentence)
        assert link.refusal("pair p cannot be used", refused) == (
            f"pair p cannot be used (error): {words}"
        ), sentence
        for part in re.findall(r"/[^ ]+|'[^']*'", sentence):
            assert part not in words, (part, sentence)


def test_a_refusal_repeats_no_text_of_the_library() -> None:
    # A sentence the host has no words for, however close to one it is, is never repeated: the
    # host says it does not repeat it.
    added, placed = _hostile()
    assert len(added) > 1000 and len(placed) > 50
    for sentence in added:
        said = link.refusal("the code was not answered", _switchboard.PairFailed("error", sentence))
        assert said == f"the code was not answered (error): {UNREPEATED}", sentence
    for sentence in placed:
        said = link.refusal("the code was not answered", _switchboard.PairFailed("error", sentence))
        assert said in {
            f"the code was not answered (error): {UNREPEATED}",
            # A secret put in a variable part within its bounds is a sentence of the library's
            # all the same, said in the host's words, which hold none of it.
            *(f"the code was not answered (error): {words}" for words in HOST_WORDS),
        }, sentence
        assert not any(secret in said for secret in SECRETS), sentence
    # Each variable part past its bounds: a file outside the store folder, or not absolute, or
    # with a line break, a control or a name of over 255 characters in its path; a name the
    # library does not allow; a protocol that is not a number; a work room or a strategy that is
    # not a short word; a reason a file is not JSON that is not Python's; a number written in
    # digits other than 0 to 9.
    for sentence in (
        f"pairing needs the `cryptography` package {CODE}",
        f"{MAC_STORE}/p.json is not a pair record. {TOKEN}",
        SAS,
        PATH,
        f"pair 'p' in the keyring is pairing protocol {EXPORT!r}; this release speaks 1. "
        "Upgrade agent-switchboard rather than editing it.",
        "/Users/x/Documents/p.json is not a pair record",
        "pairs/p.json is not a pair record",
        f"{CODE}/pairs/p.json is not a pair record",
        "/Users/x\n/pairs/p.json is not a pair record",
        "/Users/x\x1b[2J/pairs/p.json is not a pair record",
        f"/Users/{'x' * 256}/pairs/p.json is not a pair record",
        f"{MAC_STORE}/P.json is not a pair record",
        "pair 'P' in the keyring is not a pair record",
        "pair 'p' in the keyring is pairing protocol '2'; this release speaks 1. Upgrade "
        "agent-switchboard rather than editing it.",
        "invalid work-room generation 'Seven'",
        f"invalid work-room generation '{'7' * 33}'",
        f"the other side moved this pair on with healing strategy '{'r' * 33}', which this "
        "release does not know. Upgrade agent-switchboard here before using the pair again.",
        f"{MAC_STORE}/p.json is not valid JSON ({TOKEN}); {NOT_OVERWRITTEN}",
        "pair P: the store holds a SAS of another shape",
        "invalid work-room generation \u0663",
        f"{MAC_STORE}/p.json is not valid JSON (Expecting value: line \u0661 column 1 (char 0)); "
        f"{NOT_OVERWRITTEN}",
    ):
        assert link.refusal("h", _switchboard.PairFailed("error", sentence)) == (
            f"h (error): {UNREPEATED}"
        ), sentence


def test_a_refusal_repeats_only_the_librarys_own_states() -> None:
    for word in sorted(STATE_WORDS):
        refused = _switchboard.PairFailed(word, "pairing needs the `cryptography` package")
        assert link.refusal("h", refused) == f"h ({word}): {NO_CRYPTOGRAPHY}", word
    # A state the library read from a record of the store is whatever the record holds
    # (sb pairing.py:2264-2265): any but the library's own words is said as `error`.
    for odd in ("", "odd", "Expired", "expired ", "aborted\x1b[2J", CODE, SAS, "x" * 100):
        refused = _switchboard.PairFailed(odd, "this profile keeps no pairs")
        assert link.refusal("h", refused) == f"h (error): {NO_PAIRS}", odd


def test_a_refusal_with_no_sentence_gives_the_state_alone() -> None:
    for blank in ("", " ", "\n"):
        refused = _switchboard.PairFailed("error", blank)
        assert link.refusal("the code was not answered", refused) == (
            "the code was not answered (error)"
        )
    # A sentence of nothing but a character a terminal does not show is not blank: it is one the
    # host has no words for.
    refused = _switchboard.PairFailed("error", "\x1b")
    assert link.refusal("h", refused) == f"h (error): {UNREPEATED}"


def test_whatever_the_library_says_a_refusal_is_one_of_the_hosts_sentences() -> None:
    # Sentences made at random from pieces of the known ones, secrets and characters a terminal
    # treats specially: each refusal is the host's words, one of its states and one of its
    # sentences, and holds no secret.
    chance = random.Random(20261109)
    pieces = [sentence for sentence, _words in LIBRARY_SENTENCES]
    pieces += [part for sentence in pieces for part in re.split(r"(\W)", sentence) if part]
    pieces += list(SECRETS) + ["\x1b[2J", "‮", "‍", "\n", "\x00", "'", "(", ")", " "]
    states = sorted(STATE_WORDS) + ["", "odd", CODE, "\x1b[2J"]
    for _ in range(3000):
        sentence = "".join(chance.choice(pieces) for _ in range(chance.randint(0, 6)))
        state = chance.choice(states)
        said = link.refusal("h", _switchboard.PairFailed(state, sentence))
        head, _, rest = said.partition(": ")
        assert head in {f"h ({word})" for word in STATE_WORDS}, (state, sentence)
        assert rest in HOST_WORDS or (said == head and not sentence.strip()), (state, sentence)
        assert not any(secret in said for secret in SECRETS), (state, sentence)


def test_the_library_says_each_sentence_the_host_has_words_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each sentence, as the release of switchboard installed here writes it, reached the way a host
    # call reaches it: the host has words for every one, so a release that words one differently
    # is found here rather than said as one the host does not repeat.
    def said(call: Any, *args: Any) -> str:
        with pytest.raises(pairing.PairingError) as raised:
            call(*args)
        return link.refusal("h", _switchboard._failed(raised.value))

    with monkeypatch.context() as patch:
        patch.setattr(pairing, "AVAILABLE", False)
        assert said(pairing.handshake_room, b"s" * 32) == f"h (error): {NO_CRYPTOGRAPHY}"
    # The file store, in the host's own folder for it (`<state>/pairs`).
    folder = tmp_path / "state" / "pairs"
    files = _switchboard.FilePairs(pairing.PairStore(folder, env={}))
    (folder / "pending").mkdir(parents=True)
    (folder / "voided").mkdir()
    (folder / "p.json").write_text("{nope")
    assert said(files.get, "p") == f"h (error): {FILE_NOT_JSON}"
    (folder / "p.json").write_bytes(b"\xff")
    assert said(files.get, "p") == f"h (error): {FILE_NOT_JSON}"
    (folder / "pending" / "p.json").write_text("[")
    assert said(files.pending, "p") == f"h (error): {OTHER_FILE_NOT_JSON}"
    (folder / "voided" / "p.json").write_text("")
    assert said(files.verdict, "p") == f"h (error): {OTHER_FILE_NOT_JSON}"
    good = pairing.PairRecord(
        name="p", role=pairing.RESPONDER, secret="c2VjcmV0", transcript="dGg", url="https://x"
    ).to_json()
    records: list[tuple[Any, str]] = [
        ([], NOT_A_RECORD),
        ({**good, "v": 2}, OTHER_PROTOCOL),
        ({"v": 1}, MISSING_FIELD),
        ({**good, "role": "boss"}, UNKNOWN_ROLE),
        ({**good, "strategy": "rotate"}, UNKNOWN_ROLE),
        ({**good, "name": "Pair A"}, BAD_NAME),
    ]
    for data, words in records:
        (folder / "p.json").write_text(json.dumps(data))
        assert said(files.get, "p") == f"h (error): {words}", data
    for data in ([], {"v": 1}):
        (folder / "pending" / "p.json").write_text(json.dumps(data))
        assert said(files.pending, "p") == f"h (error): {BAD_PENDING}", data

    # The keyring item, whose records the host names `pair '<name>' in the keyring`.
    class Item:
        def __init__(self) -> None:
            self.doc: dict[str, Any] = {"pairs": {"records": {}, "pending": {}, "verdicts": {}}}

        def read(self) -> dict[str, Any]:
            return self.doc

    item = Item()
    keyring = _switchboard.KeyringPairs(item)  # type: ignore[arg-type]
    for data, words in records:
        item.doc["pairs"]["records"]["p"] = data
        assert said(keyring.get, "p") == f"h (error): {words}", data
    for data in ([], {"v": 1}):
        item.doc["pairs"]["pending"]["p"] = data
        assert said(keyring.pending, "p") == f"h (error): {BAD_PENDING}", data
    keyring.enabled = False
    assert said(keyring.require) == f"h (disabled): {NO_PAIRS}"
    # A generation of the record that is not one.
    for generation in ("7", -1, True, 1.5):
        found = said(pairing.work_room, b"s" * 32, generation)
        assert found == f"h (error): {BAD_GENERATION}", generation
    # A code whose handshake key is a low-order point, answered.
    mine = X25519PrivateKey.generate()
    assert said(pairing._exchange, mine, b"\x00" * 32) == f"h (error): {BAD_KEY}"
    # The pair room's records, read by a resolve.
    side = pairing._side_value(pairing.PairRecord(**good), now=1.0)
    key = pairing.SIDE_PREFIX + pairing.RESPONDER
    for entry, state_word, words in (
        ({"key": key, "unreadable": True}, "tampered", TAMPERED),
        ({"key": key, "value": {**side, "role": pairing.INITIATOR}}, "tampered", TAMPERED),
        ({"key": key, "value": {**side, "s": "rotate"}}, "upgrade", UPGRADE),
        ({"key": key, "value": {**side, "s": None}}, "upgrade", UPGRADE),
    ):
        assert said(pairing._read_sides, [entry]) == f"h ({state_word}): {words}", entry


def test_spec_gives_every_sentence_a_refusal_can_end_with() -> None:
    # spec/CLI.md section 5 lists what the host says in place of the library's sentence: the
    # sentences of link.py's table, and the one for any other, which are this test's.
    assert {words for _pattern, words in link._REFUSALS} | {link.UNREPEATED} == HOST_WORDS
    text = " ".join((Path(__file__).parents[1] / "spec" / "CLI.md").read_text().split())
    for words in sorted(HOST_WORDS):
        assert f"| {words} |" in text, words


def test_pair_refuses_while_held(cli: HostCli, tmp_path: Path) -> None:
    first = cli.lucille()
    cli.interrupted(first)
    held = cli.held()
    assert len(held) == 1
    _enable_and_add_a_folder(tmp_path)
    before, kept = cli.policy(), cli.gate()
    for argv in (["pair"], ["pair", "begin"], ["pair", "--store-file"]):
        ran = cli.run(*argv, stdin=STDIN)
        assert ran.rc == 1
        assert "Disconnect first" in ran.err, ran.err
        assert cli.held() == held and cli.gate() == kept and cli.policy() == before
        # No request was kept, so none could be answered.
        assert not cli.dirs.connect_file.exists()
    # Once the first pair is confirmed, the same.
    assert first.collect().state == "paired"
    assert cli.run("pair", "status").out.startswith("sas_pending:")
    assert cli.run("pair", "confirm").rc == 0
    ran = cli.run("pair", stdin=STDIN)
    assert ran.rc == 1 and "Disconnect first" in ran.err
    assert cli.held() == held


class _Given:
    """``pair``'s stdin, a pipe: each line it asks for waits until the test gives it
    (:meth:`give`), as a slow pipe or a user who has not typed it yet would."""

    def __init__(self) -> None:
        self.asked = 0
        self._lines: queue.Queue[str] = queue.Queue()
        self._changed = threading.Condition()

    def isatty(self) -> bool:
        return False

    def readline(self) -> str:
        with self._changed:
            self.asked += 1
            self._changed.notify_all()
        return self._lines.get(timeout=30)

    def give(self, line: str) -> None:
        self._lines.put(line)

    def wait_asked(self, lines: int) -> None:
        """Wait until ``pair`` has asked for ``lines`` lines."""
        with self._changed:
            assert self._changed.wait_for(lambda: self.asked >= lines, 30), "never asked"


def test_a_second_pair_refuses_while_one_runs(cli: HostCli, tmp_path: Path) -> None:
    # The first `pair` waits for the chat service on stdin. A pairing holds the pair lock from
    # before it looks for a pair held until it returns: two at once could each answer a code,
    # and the profile hold two pairs, the gate's record naming only the later.
    _enable_and_add_a_folder(tmp_path)
    before = cli.policy()
    first = cli.lucille()
    stdin = _Given()
    running = cli.start("pair", stdin=stdin)
    stdin.wait_asked(1)
    ran = cli.run("pair", "begin", stdin=STDIN, timeout=30)
    assert ran.rc == 1, ran.text
    assert ran.err == f"lucille-host: {ALREADY_PAIRING}\n"
    _nothing_changed(cli, before)
    assert not cli.dirs.connect_file.exists()
    lock = cli.dirs.state / PAIR_LOCK
    assert not lock_free(lock), "another process could take the pair lock"
    # The first goes on, and pairs.
    cli.monkeypatch.setattr("sys.stdin", stdin)
    stdin.give("telegram\n")
    stdin.give("ada\n")
    _delivered(cli, first, running)
    with collecting(first):
        stdin.wait_asked(3)  # the SAS is shown, and the question asked
    stdin.give("y\n")
    ran = running.wait()
    assert ran.rc == 0, ran.text
    assert ran.out.splitlines()[-1] == f"confirmed: pair {first.name} is confirmed on this computer"
    assert cli.held() == [first.name]
    found = cli.gate()
    assert found is not None and found.pair == first.name and found.opened_at is not None
    assert lock_free(lock)


def test_pair_refuses_while_another_process_runs_pair(cli: HostCli) -> None:
    before = cli.policy()
    with lock_held(paths.ensure_dir(cli.dirs.state) / PAIR_LOCK):
        ran = cli.run("pair", stdin=STDIN, timeout=30)
    assert ran.rc == 1, ran.text
    assert ran.err == f"lucille-host: {ALREADY_PAIRING}\n"
    _nothing_changed(cli, before)
    # Once that one has ended, `pair` goes ahead.
    cli.paired(cli.lucille())


def test_pair_refuses_where_the_pair_lock_cannot_be_taken(
    cli: HostCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Where the filesystem cannot lock (ENOLCK, on a network mount with no lock service, say),
    # `pair` cannot make sure that no other `pair` of the profile answers a code: it says so,
    # naming itself, and answers nothing.
    before = cli.policy()
    real = os.lockf

    def cannot_lock(descriptor: int, command: int, length: int) -> None:
        if command == os.F_TLOCK:
            raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))
        real(descriptor, command, length)

    monkeypatch.setattr("os.lockf", cannot_lock)
    ran = cli.run("pair", stdin=STDIN, timeout=30)
    lock = os.path.realpath(cli.dirs.state / PAIR_LOCK)
    assert ran.rc == 1, ran.text
    assert ran.err == (
        f"lucille-host: cannot lock {lock} ({os.strerror(errno.ENOLCK)}), so `pair` cannot make "
        "sure it is the only one of this profile\n"
    )
    _nothing_changed(cli, before)


@pytest.mark.parametrize("verb", ["confirm", "reject"])
def test_pair_status_confirm_and_reject_work_while_pair_asks(cli: HostCli, verb: str) -> None:
    # `pair` holds the pair lock until it returns, its question included. The other pairing verbs
    # take no such lock: the SAS can still be shown, confirmed or rejected in another shell.
    lucille = cli.lucille()
    stdin = _Given()
    stdin.give("telegram\n")
    stdin.give("ada\n")
    running = cli.start("pair", stdin=stdin)
    _delivered(cli, lucille, running)
    with collecting(lucille):
        stdin.wait_asked(3)
    lock = cli.dirs.state / PAIR_LOCK
    assert not lock_free(lock), "the pair lock is not held while `pair` asks"
    # It is `state/.pair.lock`, and not `run`'s lock: a `run` started now is not refused as one
    # already running, and takes no work room until the SAS is confirmed.
    assert not lock_free(cli.dirs.state / ".pair.lock")
    assert lock_free(cli.dirs.state / loop.RUN_LOCK), "`pair` holds the run lock"
    with loop.one_run(cli.dirs.state):
        pass
    cli.capsys.readouterr()  # what `pair` has printed so far
    status = cli.run("pair", "status")
    assert status.rc == 0, status.text
    assert status.out.startswith(f"sas_pending: pair {lucille.name}: the SAS is {lucille.sas()}.")
    ran = cli.run("pair", verb)
    assert ran.rc == 0, ran.text
    stdin.give("y\n")
    asked = running.wait()
    if verb == "confirm":
        assert ran.out == f"confirmed: pair {lucille.name} is confirmed on this computer\n"
        assert asked.rc == 0, asked.text
        assert asked.out.splitlines()[-1] == f"confirmed: pair {lucille.name} is already confirmed"
        assert cli.held() == [lucille.name]
    else:
        assert ran.out.startswith(f"rejected: pair {lucille.name} is forgotten on this computer")
        assert asked.rc == 1
        assert asked.err.startswith(f"none: pair {lucille.name} is gone from this computer")
        assert cli.held() == []
    assert lock_free(lock)


@pytest.mark.parametrize("no_keyring", ["fail-backend", "no-session-bus"])
def test_store_file_pairs_where_the_keyring_stores_nothing(
    no_keyring: str, cli: HostCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A computer with no keyring to store in, such as a headless Linux box: the case
    # --store-file is for. Before, `pair --store-file` looked in the keyring first, and refused.
    # Shown with keyring's fail backend, and with the host's own Linux backend and no session bus.
    if no_keyring == "fail-backend":
        monkeypatch.setattr("keyring.core._keyring_backend", FailKeyring())
    else:
        no_session_bus(monkeypatch)
    lucille = cli.lucille()
    ran = cli.run("pair", stdin=STDIN)
    assert ran.rc == 1 and "has no keyring to store in" in ran.err and "--store-file" in ran.err
    assert cli.gate() is None and not cli.dirs.connect_file.exists()
    ran = cli.paired(lucille, "--store-file")
    assert "weaker than the OS keyring" in ran.err
    assert cli.run("pair", "confirm").rc == 0
    assert cli.held() == [lucille.name]
    assert any(cli.dirs.store_files.iterdir()) and cli.env.keyring.items == {}
    status = cli.run("status")
    assert status.rc == 0 and f"pair: confirmed: pair {lucille.name}" in status.out, status.text


@pytest.mark.parametrize("setting", ["variable-null", "variable-fail", "configuration-null"])
def test_keyrings_off_switch_refuses_the_keyring_as_storing_nothing(
    setting: str, cli: HostCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    # keyring's own off switch, PYTHON_KEYRING_BACKEND or what `keyring --disable` writes to
    # keyringrc.cfg, honoured with the host's own backend as a real host builds it: every command
    # says the keyring stores nothing, with insecure_keyring and its exit codes, and the sentence's
    # --store-file still pairs.
    turned_off(monkeypatch, cli.env.home, setting)
    error = {"v": 1, "type": "error", "code": "insecure_keyring", "message": NULL_SENTENCE}
    ran = cli.run("pair", "status")
    assert ran.rc == 1 and ran.err == f"lucille-host: {NULL_SENTENCE}\n", ran.text
    ran = cli.run("--json", "pair", "status")
    assert ran.rc == 1 and _support.cli_objects(ran.out) == [error], ran.text
    # status reports the pair it cannot read, and the rest, as where there is no keyring.
    ran = cli.run("--json", "status")
    assert ran.rc == 0, ran.text
    [found] = _support.cli_objects(ran.out)
    assert found["pair"] == {
        "state": "unreadable",
        "code": "insecure_keyring",
        "message": NULL_SENTENCE,
    }
    lucille = cli.lucille()
    ran = cli.run("pair", stdin=STDIN)
    assert ran.rc == 1 and ran.err == f"lucille-host: {NULL_SENTENCE}\n", ran.text
    assert cli.gate() is None and not cli.dirs.connect_file.exists()
    ran = cli.paired(lucille, "--store-file")
    assert "weaker than the OS keyring" in ran.err
    assert cli.run("pair", "confirm").rc == 0
    assert cli.held() == [lucille.name]
    assert any(cli.dirs.store_files.iterdir()) and cli.env.keyring.items == {}


def test_a_session_bus_that_did_not_answer_refuses_the_keyring(
    cli: HostCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The Secret Service's check never answered within its deadline (issue 39): every command
    # refuses with insecure_keyring and the sentence that says so, status reports the pair it cannot
    # read and the rest, and the sentence's --store-file still pairs.
    monkeypatch.setattr(store, "_system_backend", store.NoAnswer)
    sentence = (
        "the keyring backend here (NoAnswer) has no keyring to store in: the session bus did not "
        f"answer within {store.PROBE_TIMEOUT:g} s, {store.PAIR_REFUSAL}"
    )
    ran = cli.run("pair", "status")
    assert ran.rc == 1 and ran.err == f"lucille-host: {sentence}\n", ran.text
    ran = cli.run("--json", "pair", "status")
    assert ran.rc == 1, ran.text
    assert _support.cli_objects(ran.out) == [
        {"v": 1, "type": "error", "code": "insecure_keyring", "message": sentence}
    ]
    ran = cli.run("--json", "status")
    assert ran.rc == 0, ran.text
    [found] = _support.cli_objects(ran.out)
    assert found["pair"] == {"state": "unreadable", "code": "insecure_keyring", "message": sentence}
    lucille = cli.lucille()
    ran = cli.paired(lucille, "--store-file")
    assert "weaker than the OS keyring" in ran.err
    assert cli.run("pair", "confirm").rc == 0
    assert cli.held() == [lucille.name]


# How long a test waits for a late write, or for Lucille to collect.
LATE_WAIT = 10.0


class LateWriter(MemoryKeyring):
    """A keyring in memory whose first write once ``armed`` says so waits until ``release`` is
    set, then lands, or is dropped when ``lost`` is set; ``done`` is set once it has finished
    either way. Every other write lands at once."""

    def __init__(self, armed: Callable[[], bool]) -> None:
        super().__init__()
        self.armed = armed
        self.release = threading.Event()
        self.lost = threading.Event()
        self.done = threading.Event()

    def set_password(self, service: str, username: str, password: str) -> None:
        if not self.armed() or self.done.is_set():
            super().set_password(service, username, password)
            return
        self.release.wait()
        if not self.lost.is_set():
            super().set_password(service, username, password)
        self.done.set()


def _late_writer(
    cli: HostCli, monkeypatch: pytest.MonkeyPatch, armed: Callable[[], bool]
) -> LateWriter:
    """This system's keyring calls bounded by a short deadline, through a :class:`LateWriter`."""
    monkeypatch.setattr(store, "BOUNDED", frozenset({paths.os_family()}))
    monkeypatch.setattr(store, "CALL_TIMEOUT", 0.2)
    monkeypatch.setattr(store, "_UNANSWERED", threading.Event())
    backend = LateWriter(armed)
    monkeypatch.setattr("keyring.core._keyring_backend", backend)
    return backend


UNKNOWN_WRITE = {
    "v": 1,
    "type": "error",
    "code": "store_error",
    "message": (
        "the keyring did not answer a write within 0.2 s; whether it was saved is not known, "
        "and the next start reads it again"
    ),
}


@pytest.mark.parametrize("lands", [True, False], ids=["lands-late", "never-lands"])
def test_a_write_the_keyring_did_not_answer_in_time_is_read_again_by_the_next_start(
    cli: HostCli, monkeypatch: pytest.MonkeyPatch, lands: bool
) -> None:
    # Issue 39: the pair the library saves within the answer is a keyring write the Secret Service
    # does not answer in time. That is store_error, saying the write's result is not known: the
    # thread left waiting on it can still finish it. The host deletes nothing of its own on the
    # assumption that it failed, and the gate's record stays. The write then lands late, or never,
    # and the next start (a new process) reads the keyring afresh: it carries on to the SAS and
    # confirms, or finds no pair and pairs anew.
    backend = _late_writer(cli, monkeypatch, cli.dirs.gate_file.exists)
    lucille = cli.lucille()
    try:
        # A write with no deadline would wait for the release below: it fails here instead.
        ran = cli.begin(lucille, "--json").wait(LATE_WAIT)
    finally:
        if not lands:
            backend.lost.set()
        backend.release.set()
    assert ran.rc == 1, ran.text
    assert _objects(ran)[-1] == UNKNOWN_WRITE
    kept = cli.gate()
    assert kept is not None and kept.pair == lucille.name
    assert backend.done.wait(LATE_WAIT)
    # The next start: a new process, which asks the keyring again.
    monkeypatch.setattr(store, "_UNANSWERED", threading.Event())
    if lands:
        assert cli.held() == [lucille.name]
        with collecting(lucille):
            deadline = time.monotonic() + LATE_WAIT
            while True:
                ran = cli.run("pair", "status")
                assert ran.rc == 0, ran.text
                if ran.out.startswith("sas_pending:") or time.monotonic() > deadline:
                    break
                time.sleep(0.05)
        assert ran.out.startswith(f"sas_pending: pair {lucille.name}:"), ran.text
        assert cli.run("pair", "confirm").rc == 0
        assert cli.held() == [lucille.name]
        # The same record, stamped as the pairing went on.
        found = cli.gate()
        assert found is not None and (found.pair, found.created_at) == (kept.pair, kept.created_at)
    else:
        assert cli.held() == []
        ran = cli.run("pair", "status")
        assert (ran.rc, ran.out) == (0, "none: this computer holds no pair\n"), ran.text
        assert cli.gate() == kept
        other = cli.lucille()
        cli.paired(other)
        assert cli.run("pair", "confirm").rc == 0
        assert cli.held() == [other.name]
        found = cli.gate()
        assert found is not None and found.pair == other.name


@pytest.mark.parametrize("lands", [True, False], ids=["lands-late", "never-lands"])
def test_a_forget_whose_write_did_not_answer_in_time_keeps_the_gate_record(
    cli: HostCli, monkeypatch: pytest.MonkeyPatch, lands: bool
) -> None:
    # Issue 39: forget's keyring write is not answered in time. Whether the pair was forgotten is
    # not known, so the gate's record is not deleted on the assumption that it was. The next start
    # reads the keyring afresh: a pair the late write forgot is gone, and one it never reached is
    # still held, and forgotten by the next forget.
    arm = threading.Event()
    backend = _late_writer(cli, monkeypatch, arm.is_set)
    lucille = cli.lucille()
    cli.paired(lucille)
    assert cli.run("pair", "confirm").rc == 0
    kept = cli.gate()
    assert kept is not None
    arm.set()
    try:
        ran = cli.run("--json", "forget", timeout=LATE_WAIT)
    finally:
        if not lands:
            backend.lost.set()
        backend.release.set()
    assert ran.rc == 1, ran.text
    assert _objects(ran)[-1] == UNKNOWN_WRITE
    assert cli.gate() == kept
    assert backend.done.wait(LATE_WAIT)
    # The next start: a new process, which asks the keyring again.
    monkeypatch.setattr(store, "_UNANSWERED", threading.Event())
    if lands:
        # Forgotten here; every command says no pair is held, and the record left is replaced by
        # the next pairing's.
        assert cli.held() == []
        ran = cli.run("pair", "status")
        assert (ran.rc, ran.out) == (0, "none: this computer holds no pair\n"), ran.text
        other = cli.lucille()
        cli.paired(other)
        assert cli.run("pair", "confirm").rc == 0
        assert cli.held() == [other.name]
        found = cli.gate()
        assert found is not None and found.pair == other.name
    else:
        # Still held, with its record: the next forget forgets both.
        assert cli.held() == [lucille.name]
        assert cli.run("forget").rc == 0
        assert cli.held() == [] and cli.gate() is None


def test_a_code_is_never_taken_on_the_command_line(cli: HostCli) -> None:
    # No pairing code is pasted: one given to `pair` is refused, in text and in JSON, and written
    # nowhere.
    code = cli.lucille().start()
    for argv in (("pair", code), ("pair", code, "--json"), ("--json", "pair", code)):
        ran = cli.run(*argv)
        assert ran.rc == 1, argv
        assert code not in ran.text, argv
        assert "`lucille-host pair begin` shows a code" in ran.err, ran.err
        if "--json" in argv:
            (refused,) = _objects(ran)
            assert (refused["type"], refused["code"]) == ("error", "code_in_argv"), argv
    assert cli.gate() is None and cli.held() == [] and not cli.dirs.connect_file.exists()
    # Nor on standard input: `--code-stdin` is gone, a usage error.
    cli.capsys.readouterr()
    with pytest.raises(SystemExit) as exit_info:
        main(["--json", "pair", "--code-stdin"])
    out, err = cli.capsys.readouterr()
    assert exit_info.value.code == 2
    assert [json.loads(line)["code"] for line in out.splitlines()] == ["usage"]
    # A code where the parser takes no word: standard error may quote it, the JSON never does.
    with pytest.raises(SystemExit) as exit_info:
        main(["pair", "status", code, "--json"])
    out, err = cli.capsys.readouterr()
    assert exit_info.value.code == 2
    assert [json.loads(line)["code"] for line in out.splitlines()] == ["usage"]
    assert code not in out and code in err
    # The code Lucille delivers is nowhere the host writes: not its output, its files or its
    # keyring item.
    delivered: list[str] = []
    ran = cli.connect(cli.lucille(), before_post=delivered.append)
    assert ran.rc == 0, ran.text
    ran2 = cli.connect(cli.lucille(), "--json", profile="json", before_post=delivered.append)
    assert ran2.rc == 0, ran2.text
    assert len(delivered) == 2
    for given in delivered:
        assert given not in ran.text and given not in ran2.text
        for path in cli.env.home.rglob("*"):
            if path.is_file():
                assert given not in path.read_text(errors="replace"), path
        assert all(given not in value for value in cli.env.keyring.items.values())


@pytest.mark.parametrize("argv", [("pair",), ("pair", "begin"), ("--json", "pair")])
def test_a_code_piped_in_says_pasting_is_gone(cli: HostCli, argv: tuple[str, ...]) -> None:
    # The old habit, or an old script: a pairing code on standard input, where `pair begin` reads
    # the chat service. It is refused as no service, saying plainly that pasting is gone, and
    # the code is never repeated.
    code = cli.lucille().start()
    for given in (code, code.upper(), "  " + code):
        ran = cli.run(*argv, stdin=given + "\n")
        assert ran.rc == 1, ran.text
        assert PASTE_IS_GONE in ran.err, ran.err
        assert code not in ran.text and code.upper() not in ran.text
        if "--json" in argv:
            assert [o["code"] for o in _objects(ran)] == ["bad_service"]
    assert cli.gate() is None and cli.held() == [] and not cli.dirs.connect_file.exists()
    # Any other service is refused as before.
    ran = cli.run("pair", stdin="slack\nada\n")
    assert ran.rc == 1 and PASTE_IS_GONE not in ran.err and "telegram only" in ran.err


def test_pair_alone_connects_as_begin(cli: HostCli) -> None:
    lucille = cli.lucille()
    running = cli.start("pair", stdin=STDIN)
    _delivered(cli, lucille, running)
    with collecting(lucille):
        ran = running.wait()
    # With no answer after the username, the SAS is shown, and the answer left for `pair confirm`
    # or `pair reject`.
    assert ran.rc == 0, ran.text
    assert ran.out.startswith("code: send this to Lucille in your chat: connect "), ran.out
    found = cli.gate()
    assert found is not None and found.deadline is not None
    assert ran.out.splitlines()[-2:] == [
        f"sas_pending: pair {lucille.name}: the SAS is {lucille.sas()}. If Lucille's page shows "
        f"the same, confirm it with `lucille-host pair confirm` before "
        f"{link.iso(found.deadline)}; if not, `lucille-host pair reject`",
        f"sas_pending: pair {lucille.name} is not confirmed yet: `lucille-host pair confirm` "
        f"confirms it before {link.iso(found.deadline)}, and `lucille-host pair reject` "
        "rejects it",
    ]
    assert found.opened_at is None and cli.held() == [lucille.name]


# --- waiting for Lucille to collect --------------------------------------------------------------


def _connect_lines(ran: Any) -> list[str]:
    """``pair begin``'s text lines from the pairing code on: what Lucille's answer started."""
    lines = ran.out.splitlines()
    assert lines[0].startswith("code: send this to Lucille in your chat: connect "), lines
    assert lines[1] == "connecting: as @ada on Telegram", lines
    return lines[2:]


def test_begin_waits_until_sas_pending(cli: HostCli) -> None:
    lucille = cli.lucille()
    ran = cli.paired(lucille)
    lines = _connect_lines(ran)
    assert lines[0] == f"pairing code for pair {lucille.name}, on {_switchboard.LUCILLE_HUB}"
    assert lines[1].startswith("note: lucille-host/2 n=")
    assert [line.split(":", 1)[0] for line in lines[2:]] == [
        "accepted",
        "waiting",
        "sas_pending",
        "sas_pending",
    ]
    found = cli.gate()
    assert found is not None and found.pair == lucille.name
    assert found.shown_at == cli.h.now and found.deadline == found.shown_at + gate.WINDOW
    assert found.opened_at is None
    # It stops once the SAS is shown, as Lucille's page shows it, with the deadline.
    assert f"the SAS is {lucille.sas()}." in lines[-2]
    assert link.iso(found.deadline) in lines[-2]

    # With --json: the same steps, as objects, the SAS and the deadline in the last; no prompt.
    second = cli.lucille()
    ran = cli.connect(second, "--json", profile="json")
    assert ran.rc == 0, ran.text
    found = gate.load(paths.dirs("json").gate_file)
    assert found is not None and found.deadline is not None
    assert found.shown_at == cli.h.now
    name = second.name
    objects = _objects(ran)
    assert [(o["type"], o["state"]) for o in objects[:2]] == [
        ("connect", "code"),
        ("connect", "connecting"),
    ]
    note = objects[2]["note"]
    assert re.fullmatch(r"lucille-host/2 n=[0-9a-f]{32}", note)
    assert objects[2:] == [
        {"v": 1, "type": "pair_code", "name": name, "hub": _switchboard.LUCILLE_HUB, "note": note},
        {"v": 1, "type": "pair", "state": "accepted", "name": name, "clock_skewed": False},
        {"v": 1, "type": "pair", "state": "waiting", "name": name},
        {
            "v": 1,
            "type": "pair",
            "state": "sas_pending",
            "name": name,
            "sas": second.sas(),
            "deadline": link.iso(found.deadline),
        },
    ]
    assert ran.err == ""
    # From a policy that is not the fresh one, the policy is set aside before the code is
    # answered, and it says so first.
    assert cli.run("--profile", "reset", "ops", "enable", "host.sleep").rc == 0
    ran = cli.connect(cli.lucille(), "--json", profile="reset")
    assert ran.rc == 0, ran.text
    assert [(o["type"], o.get("state")) for o in _objects(ran)] == [
        ("connect", "code"),
        ("connect", "connecting"),
        ("pair_code", None),
        ("policy_reset", None),
        ("pair", "accepted"),
        ("pair", "waiting"),
        ("pair", "sas_pending"),
    ]
    # A code that leaked, and one past its time, end with an error object, exit 1.
    elsewhere = pairing.PairStore(cli.folder / "elsewhere", env={})

    def redeemed_first(code: str) -> None:
        pairing.accept(code, None, store=elsewhere, connect=cli.h.client_class(), now=cli.h.now)

    ran = cli.connect(cli.lucille(), "--json", profile="leaked", before_post=redeemed_first)
    assert ran.rc == 1
    assert [o["type"] for o in _objects(ran)][-2:] == ["pair_code", "error"]
    assert _objects(ran)[-1]["code"] == "leaked"
    # A pairing code past its time, while the Connect request is still live.
    ran = cli.connect(
        cli.lucille(),
        "--json",
        profile="late",
        ttl=60,
        before_post=lambda _code: cli.h.advance(1000),
    )
    assert ran.rc == 1 and _objects(ran)[-1]["code"] == "expired", ran.text


class _Typed(io.StringIO):
    """A terminal's stdin, holding what was typed."""

    def isatty(self) -> bool:
        return True


def test_json_pair_asks_nothing_at_a_terminal(cli: HostCli) -> None:
    # As text, `pair begin` asks for the chat service on stderr when stdin is a terminal.
    ran = cli.connect(cli.lucille(), stdin=_Typed(STDIN), profile="text")
    assert ran.rc == 0 and ran.err.startswith("Chat service (telegram): "), ran.text
    # With --json it asks nothing, through to the SAS, and when nothing is typed.
    ran = cli.connect(cli.lucille(), "--json", stdin=_Typed(STDIN))
    assert ran.rc == 0 and ran.err == "", ran.text
    assert [o.get("state") for o in _objects(ran)] == [
        "code",
        "connecting",
        None,
        "accepted",
        "waiting",
        "sas_pending",
    ]
    ran = cli.run("--profile", "empty", "--json", "pair", "begin", stdin=_Typed("\n"))
    assert ran.rc == 1 and not ran.err.startswith("Chat service"), ran.text
    assert [o["code"] for o in _objects(ran)] == ["bad_service"]
    # A code on the command line is refused before anything is read or asked.
    ran = cli.run("--profile", "argv", "pair", "swbp1_x", "--json", stdin=_Typed("\n"))
    assert ran.rc == 1 and ran.err.startswith("lucille-host: `pair` takes no pairing code"), ran.err


def test_pair_completes_without_paired_note(cli: HostCli) -> None:
    suppressed: list[str] = []

    class Quiet(cli.h.client_class()):  # type: ignore[misc]
        """Lucille's client, losing the best-effort note that says the answer was collected."""

        def post(self, channel: str, body: Any, **options: Any) -> Any:
            if isinstance(body, dict) and body.get("t") == pairing.TAG_PAIRED:
                suppressed.append(channel)
                raise httpx.ConnectError("the paired note is lost")
            return super().post(channel, body, **options)

    lucille = cli.lucille(connect=Quiet)
    running = cli.begin(lucille)
    # Lucille collects only once the host has resolved and is listening for a note.
    until(lambda: cli.listens > 0)
    assert lucille.collect().state == "paired"
    assert suppressed and set(suppressed) == {pairing.CHANNEL}
    ran = running.wait(15)
    assert ran.rc == 0, ran.text
    assert ran.out.splitlines()[-1].startswith("sas_pending: ")


def test_a_paired_note_wakes_the_wait_at_once(
    cli: HostCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the periodic resolve far off, only the note can end the wait in time.
    monkeypatch.setattr("lucille_host.link.RESOLVE_EVERY", 600.0)
    lucille = cli.lucille()
    running = cli.begin(lucille)
    until(lambda: cli.listens > 0)
    assert lucille.collect().state == "paired"
    ran = running.wait(15)
    assert ran.rc == 0 and ran.out.splitlines()[-1].startswith("sas_pending: "), ran.text


def _answered_twice(cli: HostCli, lucille: FakeLucille, code: str) -> None:
    """Somebody else holding the code answers it too, and Lucille, seeing two answers, aborts."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("switchboard.pairing._refuse_used", lambda seen, code: None)
        other = pairing.PairStore(cli.folder / "other", env={})
        with pytest.raises(pairing.AlreadyRedeemed):
            pairing.accept(code, None, store=other, connect=cli.h.client_class(), now=cli.h.now)
    assert lucille.collect().state == "aborted"


@pytest.mark.parametrize("case", ["already_redeemed", "aborted", "superseded"])
def test_pair_leaked_states_stop(case: str, cli: HostCli, monkeypatch: pytest.MonkeyPatch) -> None:
    lucille = cli.lucille()
    if case == "already_redeemed":
        other = pairing.PairStore(cli.folder / "other", env={})

        def redeemed_first(code: str) -> None:
            pairing.accept(code, None, store=other, connect=cli.h.client_class(), now=cli.h.now)

        ran = cli.begin(lucille, before_post=redeemed_first).wait()
    elif case == "aborted":
        delivered: list[str] = []
        running = cli.begin(lucille, before_post=delivered.append)
        until(lambda: '"event":"accepted"' in cli.audit())
        _answered_twice(cli, lucille, delivered[0])
        ran = running.wait()
    else:

        def superseded(*_: Any, **__: Any) -> Any:
            raise pairing.PairingError("another answer was collected", state="superseded")

        monkeypatch.setattr("switchboard.pairing.resolve", superseded)
        ran = cli.begin(lucille).wait()
    assert ran.rc == 1
    assert ran.err.startswith("leaked: the pairing code leaked"), ran.err
    assert f"({case})" in ran.err
    assert cli.gate() is None and cli.held() == []
    assert not ran.out.splitlines()[-1].startswith("sas_pending")
    # Nothing of the pair stays in the profile's store: when the library dropped it, aborted, it
    # kept a verdict under its name, which forgetting what is left of the pair here clears.
    opened = _switchboard.open_link(cli.dirs, state.load_host_config(cli.dirs))
    assert opened.pairs.verdict_names() == []


@pytest.mark.parametrize("case", ["expired", "wrong_hub", "never_confirmed", "unsent"])
def test_pair_again_states_stop(case: str, cli: HostCli) -> None:
    lucille = cli.lucille()
    if case == "expired":
        # The pairing code is past its time, while the Connect request is still live (1200 s).
        ran = cli.begin(lucille, ttl=60, before_post=lambda _code: cli.h.advance(1000)).wait()
    elif case == "wrong_hub":
        ran = cli.begin(lucille, recode=lambda code: _recode(code, secret=os.urandom(32))).wait()
    elif case == "never_confirmed":
        running = cli.begin(lucille)
        until(lambda: '"event":"accepted"' in cli.audit())
        # Lucille never collects; its window ends 12 hours after the code expires.
        cli.h.advance(13 * 3600)
        ran = running.wait()
    else:

        class NoAnswer(cli.h.client_class()):  # type: ignore[misc]
            """The host's client, whose answer to the code never reaches the hub."""

            def post(self, channel: str, body: Any, **options: Any) -> Any:
                if isinstance(body, dict) and body.get("t") == "swbp1/answer":
                    raise httpx.ConnectError("the answer is lost")
                return super().post(channel, body, **options)

        cli.client_class = NoAnswer
        running = cli.begin(lucille)
        until(lambda: '"event":"accepted"' in cli.audit())
        assert '"sent":false' in cli.audit()
        cli.h.advance(2 * 3600)
        ran = running.wait()
    assert ran.rc == 1
    assert ran.err.startswith("expired: the pairing did not finish in time"), ran.err
    assert f"({case})" in ran.err
    assert cli.gate() is None and cli.held() == []


def test_a_pair_lucille_ended_is_forgotten_here(cli: HostCli) -> None:
    lucille = cli.lucille()
    cli.paired(lucille)
    assert lucille.forget().peer_told
    ran = cli.run("pair", "status")
    assert ran.rc == 1
    assert ran.err.startswith("ended: ") and "(ended_by_peer)" in ran.err, ran.err
    assert cli.held() == [] and cli.gate() is None
    assert cli.run("pair", "status").out.startswith("none: ")


def test_a_hub_out_of_reach_while_waiting_is_waited_out(cli: HostCli) -> None:
    lucille = cli.lucille()
    out_of_reach = threading.Event()
    failed: list[str] = []

    class Flaky(cli.client_class):  # type: ignore[misc,name-defined]
        """The host's client, while the hub is out of reach."""

        def _call(self, *args: Any, **options: Any) -> Any:
            if out_of_reach.is_set():
                failed.append(str(args[:2]))
                raise httpx.ConnectError("the hub is out of reach")
            return super()._call(*args, **options)

    cli.client_class = Flaky
    running = cli.begin(lucille)
    until(lambda: cli.listens > 0)
    out_of_reach.set()
    until(lambda: len(failed) >= 4)
    assert lucille.collect().state == "paired"
    out_of_reach.clear()
    ran = running.wait(15)
    assert ran.rc == 0, ran.text
    assert ran.out.splitlines()[-1].startswith("sas_pending: ")


def test_an_unparsable_note_in_the_pair_room_is_waited_out(cli: HostCli) -> None:
    # Whoever can write pair:<name> posts a body that opens with its key and is not JSON: the
    # wait falls back to its periodic resolve, as for any message it cannot open.
    lucille = cli.lucille()
    rooms: list[Any] = []
    notes = _switchboard.Link.pair_notes

    def kept(link: _switchboard.Link, pair: _switchboard.Pair) -> Any:
        rooms.append(pair.room)
        return notes(link, pair)

    cli.monkeypatch.setattr(_switchboard.Link, "pair_notes", kept)
    running = cli.begin(lucille)
    until(lambda: cli.listens > 0)
    lucille.post_unparsable(rooms[-1], pairing.CHANNEL)
    listened = cli.listens
    until(lambda: cli.listens > listened + 2)
    assert lucille.collect().state == "paired"
    ran = running.wait(15)
    assert ran.rc == 0, ran.text
    assert ran.out.splitlines()[-1].startswith("sas_pending: ")


# --- pair status -------------------------------------------------------------------------------


def test_pair_status_resumes(cli: HostCli) -> None:
    ran = cli.run("pair", "status")
    assert (ran.rc, ran.out) == (0, "none: this computer holds no pair\n")
    lucille = cli.lucille()
    cli.interrupted(lucille)
    ran = cli.run("pair", "status")
    # It asked the hub, so it says what `status`, which makes no hub call, cannot.
    assert (ran.rc, ran.out) == (
        0,
        f"waiting: pair {lucille.name}: Lucille has not collected this computer's answer yet\n",
    ), ran.text
    # `pair confirm` asked the hub too, so it says so, and not that the hub was out of reach.
    not_collected = (
        f"pair {lucille.name}: Lucille has not collected this computer's answer yet, so there is "
        "no SAS to confirm"
    )
    ran = cli.run("pair", "confirm")
    assert (ran.rc, ran.out, ran.err) == (1, "", f"waiting: {not_collected}\n")
    ran = cli.run("--json", "pair", "confirm")
    assert ran.rc == 1 and _objects(ran) == [
        {"v": 1, "type": "error", "code": "waiting", "message": not_collected}
    ]
    assert lucille.collect().state == "paired"
    # Lucille has collected it, and the hub is out of reach: the library's resolve cannot read the
    # pair room, or the host's raises a hub error. `pair status` still says `waiting`, and says
    # that whether Lucille has collected the answer is not known; `pair confirm` refuses so.
    real = cli.client_class

    class Down(cli.h.client_class()):  # type: ignore[misc]
        def _call(self, *_: Any, **__: Any) -> Any:
            raise httpx.ConnectError("the hub is out of reach")

    def raised(*_: Any, **__: Any) -> Any:
        raise _switchboard.HubError("the hub could not be reached (ConnectError)")

    unknown = (
        f"pair {lucille.name}: the hub could not be reached, so whether Lucille has collected "
        "this computer's answer is not known"
    )
    for how in ("unreadable", "raised"):
        with cli.monkeypatch.context() as patch:
            if how == "raised":
                patch.setattr(_switchboard.Link, "resolve", raised)
            else:
                cli.client_class = Down
            ran = cli.run("pair", "status")
            assert (ran.rc, ran.out, ran.err) == (
                0,
                f"waiting: {unknown}; `lucille-host pair status` asks again\n",
                "",
            ), how
            ran = cli.run("pair", "status", "--json")
            assert ran.rc == 0 and _objects(ran) == [
                {"v": 1, "type": "pair", "state": "waiting", "name": lucille.name}
            ], how
            refused = (
                f"{unknown}, and this computer holds no SAS to confirm; "
                "`lucille-host pair status` asks again"
            )
            ran = cli.run("pair", "confirm")
            assert (ran.rc, ran.out, ran.err) == (1, "", f"waiting: {refused}\n"), how
            ran = cli.run("--json", "pair", "confirm")
            assert ran.rc == 1 and _objects(ran) == [
                {"v": 1, "type": "error", "code": "waiting", "message": refused}
            ], how
            cli.client_class = real
        assert cli.gate() is not None and cli.gate().shown_at is None  # type: ignore[union-attr]
    ran = cli.run("pair", "status")
    found = cli.gate()
    assert found is not None and found.deadline is not None
    assert ran.rc == 0, ran.text
    assert ran.out == (
        f"sas_pending: pair {lucille.name}: the SAS is {lucille.sas()}. If Lucille's page shows "
        f"the same, confirm it with `lucille-host pair confirm` before "
        f"{link.iso(found.deadline)}; if not, `lucille-host pair reject`\n"
    )
    assert cli.run("pair", "confirm").rc == 0
    assert cli.run("pair", "status").out == f"confirmed: pair {lucille.name}\n"
    for action in ("status", "confirm", "reject"):
        ran = cli.run("pair", action, "--hub", LOOPBACK)
        assert ran.rc == 1 and "takes no options" in ran.err

    # With --json, in another profile: the same states, as objects.
    def pair_json(*argv: str, stdin: str = "") -> list[dict[str, Any]]:
        ran = cli.run("--profile", "json", "pair", *argv, "--json", stdin=stdin)
        assert ran.rc == 0, ran.text
        return _objects(ran)

    assert pair_json("status") == [{"v": 1, "type": "pair", "state": "none"}]
    second = cli.lucille()
    ran = cli.interrupted(second, "--json", profile="json")
    assert ran.rc == 1 and ran.err.startswith("interrupted: ")
    assert _objects(ran)[-1] == {
        "v": 1,
        "type": "error",
        "code": "interrupted",
        "message": f"pair {second.name} is kept; `lucille-host pair status` resumes",
    }
    waiting = {"v": 1, "type": "pair", "state": "waiting", "name": second.name}
    assert pair_json("status") == [waiting]
    assert second.collect().state == "paired"
    (pending,) = pair_json("status")
    found = gate.load(paths.dirs("json").gate_file)
    assert found is not None and found.deadline is not None
    assert pending == {
        "v": 1,
        "type": "pair",
        "state": "sas_pending",
        "name": second.name,
        "sas": second.sas(),
        "deadline": link.iso(found.deadline),
    }
    confirmed = {"v": 1, "type": "pair", "state": "confirmed", "name": second.name}
    assert pair_json("confirm") == [confirmed]
    assert pair_json("status") == [confirmed]
    assert pair_json("confirm") == [confirmed]
    for action in ("status", "confirm", "reject"):
        ran = cli.run("--profile", "json", "pair", action, "--hub", LOOPBACK, "--json")
        assert ran.rc == 1 and [o["code"] for o in _objects(ran)] == ["bad_options"]


def test_json_takes_the_longest_pair_name_and_the_most_folders(
    cli: HostCli, tmp_path: Path
) -> None:
    # As many folders as the policy holds, and the longest name the pairing library allows: every
    # object that carries them carries them whole, and its schema takes it.
    for index in range(policy.MAX_ROOTS):
        made = tmp_path / "folders" / f"r{index}"
        made.mkdir(parents=True)
        assert cli.run("roots", "add", "--", f"r{index}", str(made)).rc == 0
    (found,) = _objects(cli.run("--json", "status"))
    assert len(found["policy"]["folders"]) == policy.MAX_ROOTS
    lucille = cli.lucille()
    longest = "p" * 32
    ran = cli.connect(lucille, "--json", name=longest)
    assert ran.rc == 0, ran.text
    objects = _objects(ran)
    assert [(o["type"], o.get("state"), o.get("name")) for o in objects] == [
        ("connect", "code", None),
        ("connect", "connecting", None),
        ("pair_code", None, longest),
        ("policy_reset", None, None),
        ("pair", "accepted", longest),
        ("pair", "waiting", longest),
        ("pair", "sas_pending", longest),
    ]
    (previous,) = _objects(cli.run("--json", "policy", "previous"))
    assert len(previous["folders"]) == policy.MAX_ROOTS
    for argv in (("pair", "status"), ("pair", "confirm"), ("status",), ("forget",)):
        ran = cli.run("--json", *argv)
        assert ran.rc == 0, ran.text
        (found,) = _objects(ran)
        assert found.get("name", found.get("pair", {}).get("name")) == longest, argv


def test_an_interrupted_pair_is_kept(cli: HostCli) -> None:
    lucille = cli.lucille()
    ran = cli.interrupted(lucille)
    assert "pair status` resumes" in ran.err
    assert cli.held() == [lucille.name]
    found = cli.gate()
    assert found is not None and found.pair == lucille.name and found.shown_at is None


# --- the gate ----------------------------------------------------------------------------------


class _User:
    """A user at ``pair``'s prompt: each answer is whatever ``answer`` returns when asked, given
    the gate as it stood then. Past :data:`ASKED_MOST` questions, stdin ends, so that a prompt
    that never takes an answer ends the command rather than asking for ever."""

    ASKED_MOST = 10

    def __init__(self, cli: HostCli, answer: Any) -> None:
        self.cli = cli
        self.answer = answer
        self.asked: list[gate.Gate | None] = []
        # `pair begin` reads the chat service and the username first.
        self.first = STDIN.splitlines(keepends=True)

    def readline(self) -> str:
        if self.first:
            return self.first.pop(0)
        self.asked.append(self.cli.gate())
        if len(self.asked) > self.ASKED_MOST:
            return ""
        return str(self.answer(len(self.asked)))

    def isatty(self) -> bool:
        return False


class _Terminal:
    """``pair``'s stdin as a terminal (a pseudo-terminal), where the user typed ``ahead`` before
    ``pair`` started (or :meth:`type` while it ran), before it asked anything, and types
    ``answer`` once it asks."""

    def __init__(self, ahead: str, answer: str) -> None:
        self.master, slave = os.openpty()
        self.file = open(slave, encoding="utf-8")  # closed by close()
        self.answer = answer
        self.asked = 0
        # `pair begin` reads the chat service and the username first, as typed then; anything
        # typed ahead stays on the terminal.
        self.first = STDIN.splitlines(keepends=True)
        if ahead:
            self.type(ahead)

    def type(self, text: str) -> None:
        os.write(self.master, text.encode())
        # What was typed is on the terminal, a whole line of it at least, before this returns.
        assert select.select([self.file], [], [], 10)[0]

    def isatty(self) -> bool:
        return self.file.isatty()

    def fileno(self) -> int:
        return self.file.fileno()

    def readline(self) -> str:
        if self.first:
            return self.first.pop(0)
        self.asked += 1
        if self.asked > 1:
            return ""  # stdin ends rather than leave a second question waiting for ever
        os.write(self.master, self.answer.encode())
        return self.file.readline()

    def close(self) -> None:
        self.file.close()
        os.close(self.master)


# What `pair begin` asks at a terminal before the SAS: the chat service, then the username.
PROMPTS = "Chat service (telegram): Your username on Telegram: "


def _events(cli: HostCli, event: str) -> list[dict[str, Any]]:
    lines = [json.loads(line) for line in cli.audit().splitlines()]
    return [line for line in lines if line.get("event") == event]


def test_pair_shows_the_sas_and_y_confirms(cli: HostCli) -> None:
    lucille = cli.lucille()
    user = _User(cli, lambda _: "y\n")
    with collecting(lucille):
        ran = cli.begin(lucille, stdin=user).wait()
    assert ran.rc == 0, ran.text
    lines = _connect_lines(ran)
    assert [line.split(":", 1)[0] for line in lines[2:]] == [
        "accepted",
        "waiting",
        "sas_pending",
        "confirmed",
    ]
    # Shown once the pair resolved, as Lucille's page shows it, before the question was asked.
    (asked,) = user.asked
    assert asked is not None and asked.shown_at is not None and asked.opened_at is None
    assert lines[4] == (
        f"sas_pending: pair {lucille.name}: the SAS is {lucille.sas()}. If Lucille's page shows "
        f"the same, confirm it with `lucille-host pair confirm` before "
        f"{link.iso(asked.deadline or 0)}; if not, `lucille-host pair reject`"
    )
    assert lines[5] == f"confirmed: pair {lucille.name} is confirmed on this computer"
    found = cli.gate()
    assert found is not None and found.opened_at == cli.h.now
    assert [line["pair"] for line in _events(cli, "gate_opened")] == [lucille.name]
    # Shown, never kept: not in the audit log, nor in the gate's record.
    shown = cli.audit() + cli.dirs.gate_file.read_text()
    assert lucille.sas() not in shown and lucille.sas().replace("-", "") not in shown


def test_other_answers_are_asked_again(cli: HostCli) -> None:
    lucille = cli.lucille()

    def answers(n: int) -> str:
        # Typing the SAS itself is no answer either: the question is whether the two match.
        return ["maybe\n", lucille.sas() + "\n", "\n", " YES \n"][n - 1]

    with collecting(lucille):
        ran = cli.begin(lucille, stdin=_User(cli, answers)).wait()
    assert ran.rc == 0, ran.text
    asked_again = "type y if Lucille's page shows the same SAS, or n if it does not"
    assert ran.err.splitlines() == [asked_again] * 3
    confirmed = f"confirmed: pair {lucille.name} is confirmed on this computer"
    assert ran.out.splitlines()[-1] == confirmed


def test_what_was_typed_before_the_question_is_discarded(cli: HostCli) -> None:
    # A `y` typed while `pair` waits for Lucille, a whole line and a part of one, was typed before
    # the SAS was shown: it answers nothing. The answer typed once asked is the one taken.
    lucille = cli.lucille()
    terminal = _Terminal(ahead="y\ny", answer="n\n")
    try:
        with collecting(lucille):
            ran = cli.begin(lucille, stdin=terminal).wait()
    finally:
        terminal.close()
    assert terminal.asked == 1
    assert ran.rc == 1, ran.text
    # Asked once, on the terminal, and answered by what was typed after.
    assert ran.err == (
        PROMPTS + "Does Lucille's page show the same SAS? Type y if it does, n if not: "
        f"rejected: pair {lucille.name} is forgotten on this computer; start again in Lucille. "
        "Lucille was told\n"
    )
    assert "confirmed" not in ran.out
    assert cli.held() == [] and cli.gate() is None


def test_a_y_typed_while_pair_waits_for_lucille_is_discarded(cli: HostCli) -> None:
    # `pair` has answered the code and waits for Lucille to collect the answer: no SAS is shown
    # yet. A `y` typed now is discarded right before the question, not only what was typed
    # before `pair` started.
    lucille = cli.lucille()
    terminal = _Terminal(ahead="", answer="n\n")
    try:
        running = cli.begin(lucille, stdin=terminal)
        until(lambda: cli.listens > 0)
        assert cli.gate() is not None and cli.gate().shown_at is None
        terminal.type("y\n")
        assert lucille.collect().state == "paired"
        ran = running.wait(30)
    finally:
        terminal.close()
    assert terminal.asked == 1
    assert ran.rc == 1, ran.text
    assert ran.err == (
        PROMPTS + "Does Lucille's page show the same SAS? Type y if it does, n if not: "
        f"rejected: pair {lucille.name} is forgotten on this computer; start again in Lucille. "
        "Lucille was told\n"
    )
    assert "confirmed" not in ran.out
    assert cli.held() == [] and cli.gate() is None


def test_y_for_a_pair_another_command_forgot_says_it_is_gone(cli: HostCli) -> None:
    # The SAS is shown, and while the question waits, `forget` runs in another shell. The `y`
    # then finds no gate record: the pair is gone, not waiting for `pair reject`.
    lucille = cli.lucille()

    def forgotten_first(_: int) -> str:
        config = state.load_host_config(cli.dirs)
        link.open_session(cli.dirs, config, say=print, warn=print).forget()
        return "y\n"

    with collecting(lucille):
        ran = cli.begin(lucille, stdin=_User(cli, forgotten_first)).wait()
    assert ran.rc == 1, ran.text
    assert ran.err == (
        f"none: pair {lucille.name} is gone from this computer: another command forgot it\n"
    )
    assert cli.held() == [] and cli.gate() is None


def test_pair_waits_as_long_as_the_hub_asks(cli: HostCli, monkeypatch: pytest.MonkeyPatch) -> None:
    # While `pair` waits for Lucille, the hub turns away its reads of the pair room (a 429),
    # saying when to ask again. The library returns that as an unchecked resolve, not an error;
    # `pair` still waits as long as the hub asked, rather than its own 5 s (0.2 s here).
    lucille = cli.lucille()
    busy = [True]

    class Busy(cli.client_class):  # type: ignore[misc,name-defined]
        def board_list(self, *args: Any, **options: Any) -> Any:
            if busy[0]:
                raise client.SwitchboardError(
                    "busy", status=429, payload={"error": "busy", "retry_after": 0.7}
                )
            return super().board_list(*args, **options)

    cli.client_class = Busy
    waited: list[float] = []
    real = time.sleep

    def sleep(seconds: float) -> None:
        if seconds >= 0.7:
            waited.append(seconds)
            busy[0] = False
        real(min(seconds, 0.05))

    monkeypatch.setattr("time.sleep", sleep)
    with collecting(lucille):
        ran = cli.begin(lucille).wait(20)
    assert ran.rc == 0, ran.text
    assert waited[:1] == [0.7]
    assert ran.out.splitlines()[-2].startswith(f"sas_pending: pair {lucille.name}: the SAS is ")


def test_no_question_where_what_was_typed_cannot_be_discarded(
    cli: HostCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fails(*_: Any) -> None:
        raise termios.error(5, "Input/output error")

    monkeypatch.setattr("termios.tcflush", fails)
    lucille = cli.lucille()
    terminal = _Terminal(ahead="y\n", answer="y\n")
    try:
        with collecting(lucille):
            ran = cli.begin(lucille, stdin=terminal).wait()
    finally:
        terminal.close()
    # Nothing is asked, so nothing typed is taken: `pair confirm` answers later.
    assert terminal.asked == 0
    assert ran.rc == 0, ran.text
    assert ran.out.splitlines()[-1].startswith(f"sas_pending: pair {lucille.name} is not ")
    found = cli.gate()
    assert found is not None and found.shown_at is not None and found.opened_at is None
    assert cli.run("pair", "confirm").rc == 0


@pytest.mark.parametrize("no", ["n", "No"])
def test_n_at_the_prompt_forgets(no: str, cli: HostCli) -> None:
    lucille = cli.lucille()
    with collecting(lucille):
        ran = cli.begin(lucille, stdin=_User(cli, lambda _: no + "\n")).wait()
    assert ran.rc == 1
    assert ran.out.splitlines()[-1].startswith("sas_pending: ")
    assert ran.err == (
        f"rejected: pair {lucille.name} is forgotten on this computer; start again in Lucille. "
        "Lucille was told\n"
    )
    assert cli.held() == [] and cli.gate() is None
    assert [line["why"] for line in _events(cli, "forgotten")] == ["rejected"]
    with pytest.raises(pairing.PairingError) as ended:
        lucille.resolve()
    assert ended.value.state == "ended_by_peer"


def test_ctrl_c_at_the_prompt_keeps_the_pair(cli: HostCli) -> None:
    lucille = cli.lucille()

    def interrupt(_: int) -> str:
        raise KeyboardInterrupt

    with collecting(lucille):
        ran = cli.begin(lucille, stdin=_User(cli, interrupt)).wait()
    assert ran.rc == 1
    assert ran.err == (
        f"interrupted: pair {lucille.name} is kept; `lucille-host pair status` shows its SAS "
        "again, and `lucille-host pair confirm` confirms it\n"
    )
    assert cli.held() == [lucille.name]
    found = cli.gate()
    assert found is not None and found.shown_at is not None and found.opened_at is None
    assert cli.run("pair", "confirm").rc == 0


def test_y_past_the_deadline_forgets(cli: HostCli) -> None:
    lucille = cli.lucille()

    def late(_: int) -> str:
        cli.h.advance(gate.WINDOW + 1)
        return "y\n"

    with collecting(lucille):
        ran = cli.begin(lucille, stdin=_User(cli, late)).wait()
    assert ran.rc == 1
    assert ran.err.startswith("expired: the SAS was not confirmed in time"), ran.err
    assert "Lucille was told" in ran.err
    assert cli.held() == [] and cli.gate() is None


def test_pair_confirm_confirms_the_sas_shown(cli: HostCli) -> None:
    lucille = cli.lucille()
    ran = cli.paired(lucille)
    assert f"the SAS is {lucille.sas()}." in ran.out.splitlines()[-2]
    found = cli.gate()
    assert found is not None and found.opened_at is None
    # The `y` given later: it reads nothing, and compares nothing typed.
    ran = cli.run("pair", "confirm", stdin="00000000\n")
    assert ran.rc == 0, ran.text
    assert ran.out == f"confirmed: pair {lucille.name} is confirmed on this computer\n"
    found = cli.gate()
    assert found is not None and found.opened_at == cli.h.now
    assert [line["pair"] for line in _events(cli, "gate_opened")] == [lucille.name]
    ran = cli.run("pair", "confirm")
    assert ran.rc == 0 and ran.out == f"confirmed: pair {lucille.name} is already confirmed\n"
    assert cli.gate() == found


def test_pair_status_shows_the_sas_while_pending(cli: HostCli) -> None:
    lucille = cli.lucille()
    cli.interrupted(lucille)
    ran = cli.run("pair", "status")
    assert ran.rc == 0 and ran.out.startswith(f"waiting: pair {lucille.name}"), ran.text
    assert re.search(r"\d{4}-\d{4}", ran.text) is None
    assert lucille.collect().state == "paired"
    sas = lucille.sas()
    shown = [cli.run("pair", "status"), cli.run("pair", "status")]
    found = cli.gate()
    assert found is not None and found.deadline is not None
    for ran in shown:
        assert ran.rc == 0, ran.text
        assert ran.out == (
            f"sas_pending: pair {lucille.name}: the SAS is {sas}. If Lucille's page shows the "
            f"same, confirm it with `lucille-host pair confirm` before "
            f"{link.iso(found.deadline)}; if not, `lucille-host pair reject`\n"
        )
    assert len(_events(cli, "sas_shown")) == 1
    assert cli.run("pair", "confirm").rc == 0
    ran = cli.run("pair", "status")
    assert ran.out == f"confirmed: pair {lucille.name}\n"
    kept = cli.audit() + cli.dirs.gate_file.read_text()
    assert sas not in kept and sas.replace("-", "") not in kept


def test_confirm_before_the_sas_is_shown_is_refused(cli: HostCli) -> None:
    lucille = cli.lucille()
    cli.interrupted(lucille)
    ran = cli.run("pair", "confirm")
    assert ran.rc == 1 and ran.err.startswith(f"waiting: pair {lucille.name}: "), ran.err
    assert lucille.collect().state == "paired"
    # `pair confirm` resolves, and so has the SAS; but this computer has not shown it, so there
    # is nothing the user can have compared: refused, and no deadline starts.
    ran = cli.run("pair", "confirm")
    assert ran.rc == 1
    assert ran.err == (
        f"sas_ready: pair {lucille.name}: this computer has not shown the SAS yet, so there is "
        "nothing to confirm; `lucille-host pair status` shows it\n"
    )
    found = cli.gate()
    assert found is not None
    assert (found.shown_at, found.deadline, found.opened_at) == (None, None, None)
    assert lucille.sas() in cli.run("pair", "status").out
    ran = cli.run("pair", "confirm")
    assert ran.rc == 0, ran.text
    found = cli.gate()
    assert found is not None and found.opened_at is not None


def test_gate_deadline_from_when_the_sas_is_first_shown(cli: HostCli) -> None:
    lucille = cli.lucille()
    cli.interrupted(lucille)
    accepted = cli.h.now
    cli.h.advance(3600)
    assert lucille.collect().state == "paired"
    cli.h.advance(3600)
    # A resolve that brings the SAS, showing it nowhere, starts nothing.
    assert cli.run("pair", "confirm").rc == 1
    found = cli.gate()
    assert found is not None and found.deadline is None
    cli.h.advance(3600)
    first = cli.h.now
    assert lucille.sas() in cli.run("pair", "status").out
    found = cli.gate()
    assert found is not None
    assert found.created_at == accepted
    assert (found.shown_at, found.deadline) == (first, first + gate.WINDOW)
    # Showing it again keeps the deadline where the first showing set it.
    cli.h.advance(gate.WINDOW - 1)
    assert lucille.sas() in cli.run("pair", "status").out
    assert cli.gate() == found
    cli.h.advance(2)
    ran = cli.run("pair", "confirm")
    assert ran.rc == 1
    assert ran.err.startswith("expired: the SAS was not confirmed in time"), ran.err
    assert cli.held() == [] and cli.gate() is None
    assert [line["why"] for line in _events(cli, "forgotten")] == ["deadline"]
    with pytest.raises(pairing.PairingError) as ended:
        lucille.resolve()
    assert ended.value.state == "ended_by_peer"


def test_sas_in_json_starts_the_deadline(cli: HostCli) -> None:
    # Writing the SAS in JSON is showing it: the program that reads it shows it to the user.
    lucille = cli.lucille()
    cli.interrupted(lucille)
    assert lucille.collect().state == "paired"
    found = cli.gate()
    assert found is not None and found.shown_at is None and found.deadline is None
    ran = cli.run("pair", "status", "--json")
    assert ran.rc == 0, ran.text
    found = cli.gate()
    assert found is not None and found.shown_at == cli.h.now
    assert found.deadline == found.shown_at + gate.WINDOW
    assert _objects(ran) == [
        {
            "v": 1,
            "type": "pair",
            "state": "sas_pending",
            "name": lucille.name,
            "sas": lucille.sas(),
            "deadline": link.iso(found.deadline),
        }
    ]
    assert cli.audit().count('"event":"sas_shown"') == 1
    # Shown again later, it keeps the deadline the first showing set.
    cli.h.advance(600)
    again = cli.run("--json", "pair", "status")
    assert _objects(again) == _objects(ran) and cli.gate() == found
    assert cli.audit().count('"event":"sas_shown"') == 1


def test_pair_status_past_the_deadline_forgets(cli: HostCli) -> None:
    lucille = cli.lucille()
    cli.paired(lucille)
    cli.h.advance(gate.WINDOW + 1)
    ran = cli.run("pair", "status")
    assert ran.rc == 1 and ran.err.startswith("expired: the SAS was not confirmed in time"), ran.err
    assert lucille.sas() not in ran.text
    assert cli.held() == [] and cli.gate() is None


def test_confirm_after_code_expired(cli: HostCli) -> None:
    lucille = cli.lucille()
    cli.paired(lucille)
    # The code itself expired long ago; the pair, and its gate, did not.
    cli.h.advance(3 * 3600)
    ran = cli.run("pair", "confirm")
    assert ran.rc == 0, ran.text
    assert ran.out.startswith("confirmed: ")
    assert cli.run("pair", "status").out == f"confirmed: pair {lucille.name}\n"


def test_reject_forgets_and_says_whether_told(cli: HostCli) -> None:
    ran = cli.run("pair", "reject")
    assert ran.rc == 1 and ran.err.startswith("none: ")
    lucille = cli.lucille()
    cli.paired(lucille)
    cli.h.advance(3 * 3600)
    ran = cli.run("pair", "reject")
    assert ran.rc == 0, ran.text
    assert ran.out == (
        f"rejected: pair {lucille.name} is forgotten on this computer; Lucille was told\n"
    )
    assert cli.held() == [] and cli.gate() is None
    with pytest.raises(pairing.PairingError) as ended:
        lucille.resolve()
    assert ended.value.state == "ended_by_peer"

    # The hub out of reach: forgotten here all the same, and Lucille is not told.
    second = cli.lucille()
    cli.paired(second)

    class Down(cli.h.client_class()):  # type: ignore[misc]
        def _call(self, *_: Any, **__: Any) -> Any:
            raise httpx.ConnectError("the hub is out of reach")

    cli.client_class = Down
    ran = cli.run("pair", "reject")
    assert ran.rc == 0, ran.text
    assert "Lucille was not told" in ran.out
    assert cli.held() == [] and cli.gate() is None
    assert second.resolve().state == "paired"
    # The same, with --json: the object says Lucille was not told.
    cli.client_class = cli.h.client_class()
    fourth = cli.lucille()
    cli.paired(fourth)
    cli.client_class = Down
    ran = cli.run("--json", "pair", "reject")
    assert ran.rc == 0, ran.text
    assert _objects(ran) == [
        {"v": 1, "type": "pair", "state": "rejected", "name": fourth.name, "lucille_told": False}
    ]
    assert cli.held() == [] and fourth.resolve().state == "paired"

    # A confirmed pair is not rejected: forgetting it is another verb's.
    cli.client_class = cli.h.client_class()
    third = cli.lucille()
    cli.paired(third)
    assert cli.run("pair", "confirm").rc == 0
    ran = cli.run("pair", "reject")
    assert ran.rc == 1 and ran.err.startswith("refused: "), ran.err
    assert cli.held() == [third.name]


def test_a_pair_with_no_gate_record_is_never_confirmed(cli: HostCli) -> None:
    lucille = cli.lucille()
    # The record goes while `pair` waits: with nowhere to note that it showed the SAS, it shows
    # none, and asks nothing.
    running = cli.begin(lucille, stdin=STDIN + "y\n")
    until(lambda: cli.listens > 0)
    gate.remove(cli.dirs.gate_file)
    assert lucille.collect().state == "paired"
    ran = running.wait(15)
    assert ran.rc == 1 and ran.err.startswith("refused: ") and "no local gate record" in ran.err
    assert "sas_pending" not in ran.out and lucille.sas() not in ran.text
    # `pair status` calls it what `pair` and `pair confirm` call it: no SAS is shown.
    ran = cli.run("pair", "status")
    assert ran.rc == 1 and ran.err.startswith("refused: ") and "no local gate record" in ran.err
    assert ran.out == "" and lucille.sas() not in ran.text
    ran = cli.run("status")
    assert ran.rc == 0 and f"pair: refused: pair {lucille.name} has no local gate" in ran.out
    assert lucille.sas() not in ran.text
    ran = cli.run("pair", "confirm")
    assert ran.rc == 1 and ran.err.startswith("refused: ") and "no local gate record" in ran.err
    assert cli.gate() is None
    assert cli.run("pair", "reject").rc == 0
    assert cli.held() == []


# --- the policy on a new pair ------------------------------------------------------------------


def test_new_pair_starts_from_fresh_policy(cli: HostCli, tmp_path: Path) -> None:
    _enable_and_add_a_folder(tmp_path)
    assert cli.policy() != policy.fresh()
    ran = cli.paired(cli.lucille())
    assert "the policy is reset for the new pair" in ran.out
    assert cli.policy() == policy.fresh()
    lines = cli.run("ops", "list").out.splitlines()
    assert "host.describe  enabled" in lines
    assert [line for line in lines if " enabled" in line] == ["host.describe  enabled"]
    assert cli.run("roots", "list").out == ""


def test_previous_policy_archived_not_applied(cli: HostCli, tmp_path: Path) -> None:
    folder = _enable_and_add_a_folder(tmp_path)
    earlier = cli.policy()
    first = cli.lucille()
    cli.paired(first)
    assert policy.load(cli.dirs.previous_policy_file) == earlier
    assert cli.policy() == policy.fresh()
    # Pairing again from the fresh policy keeps the earlier one set aside.
    assert cli.run("pair", "reject").rc == 0
    ran = cli.paired(cli.lucille())
    assert "the policy is reset" not in ran.out
    assert policy.load(cli.dirs.previous_policy_file) == earlier
    assert cli.policy() == policy.fresh()
    assert str(folder) not in cli.dirs.policy_file.read_text()


def test_a_refused_or_failed_code_resets_nothing_it_should_not(
    cli: HostCli, tmp_path: Path
) -> None:
    _enable_and_add_a_folder(tmp_path)
    earlier = cli.policy()
    ran = cli.connect(cli.lucille(), recode=lambda code: _recode(code, token="t"))
    assert ran.rc == 1, ran.text
    assert cli.policy() == earlier
    # A code that fails at the hub was still a new pairing: the reset comes before the answer.
    ran = cli.connect(cli.lucille(), ttl=60, before_post=lambda _code: cli.h.advance(1000))
    assert ran.rc == 1 and "expired: " in ran.err, ran.text
    assert cli.policy() == policy.fresh()
    assert policy.load(cli.dirs.previous_policy_file) == earlier


def test_policy_previous_lists_archive(cli: HostCli, tmp_path: Path) -> None:
    ran = cli.run("policy", "previous")
    assert ran.rc == 0 and ran.out.startswith("no earlier policy")
    ran = cli.run("policy", "previous", "--json")
    assert _objects(ran) == [{"v": 1, "type": "previous_policy", "state": "none"}]
    folder = _enable_and_add_a_folder(tmp_path)
    # A broad folder: only `broad` says so, as `roots list` and `roots add` do.
    home = str(cli.env.home)
    assert cli.run("roots", "add", "--allow-home-or-root", "--", "home", home).rc == 0
    # Last written long ago: when it was set aside is when the pairing set it aside.
    os.utime(cli.dirs.policy_file, (1_600_000_000, 1_600_000_000))
    before = time.time()
    cli.paired(cli.lucille())
    set_aside = cli.dirs.previous_policy_file.stat().st_mtime
    assert set_aside <= time.time() + 1
    # To the second, it is at or after the second in which the pairing began, as spec/CLI.md tells a
    # shell to compare it.
    assert link.iso(set_aside) >= link.iso(int(before))
    found = {
        "v": 1,
        "type": "previous_policy",
        "state": "found",
        "set_aside_at": link.iso(set_aside),
        "ops": ["files.sha256", "host.describe"],
        "folders": [
            {"alias": "docs", "path": str(folder), "broad": False},
            {"alias": "home", "path": home, "broad": True},
        ],
    }
    assert _objects(cli.run("--json", "policy", "previous")) == [found]
    listed = [
        f"the last policy a pairing set aside ({cli.dirs.previous_policy_file}); nothing uses it:",
        "operations:",
        "  files.sha256",
        "  host.describe",
        "folders:",
        f"  docs  {folder}",
        f"  home  {home}",
    ]
    ran = cli.run("policy", "previous")
    assert ran.rc == 0
    assert ran.out.splitlines() == listed
    # Read only: nothing it lists is enabled.
    assert cli.policy() == policy.fresh()
    # And listing it changes neither the file nor its time, so `set_aside_at` stays when it was set
    # aside, however often a shell asks.
    moment = 1_700_000_000
    os.utime(cli.dirs.previous_policy_file, (moment, moment))
    found["set_aside_at"] = link.iso(moment)
    kept = cli.dirs.previous_policy_file.read_bytes()
    for _ in range(2):
        assert _objects(cli.run("--json", "policy", "previous")) == [found]
        assert cli.run("policy", "previous").out.splitlines() == listed
        assert cli.dirs.previous_policy_file.read_bytes() == kept
        assert cli.dirs.previous_policy_file.stat().st_mtime_ns == moment * 10**9
    # A later pairing from the fresh policy sets nothing aside, so the file holds what an earlier
    # one set aside, and the header still says what it is. Before, it named the latest pairing.
    assert cli.run("pair", "reject").rc == 0
    ran = cli.paired(cli.lucille())
    assert "the policy is reset" not in ran.out
    assert cli.run("policy", "previous").out.splitlines() == listed
    assert _objects(cli.run("policy", "--json", "previous")) == [found]
    # When it was set aside is the file's time: one no file of the host's holds (before 1970, or
    # past 9999 where the filesystem keeps that) is refused with --json and without it alike.
    for moment in (-86400, 253402300800 + 864000):
        os.utime(cli.dirs.previous_policy_file, (moment, moment))
        if cli.dirs.previous_policy_file.stat().st_mtime != moment:
            continue  # this filesystem keeps no such time
        text, ran = cli.run("policy", "previous"), cli.run("policy", "previous", "--json")
        assert (text.rc, ran.rc) == (1, 1), (moment, ran.text)
        assert [o["code"] for o in _objects(ran)] == ["previous_invalid"]
        assert text.err == ran.err and "cannot be read" in ran.err
    cli.dirs.previous_policy_file.write_text("version = 2\n")
    ran = cli.run("policy", "previous")
    assert ran.rc == 1 and "invalid" in ran.err
    ran = cli.run("policy", "previous", "--json")
    assert ran.rc == 1 and [o["code"] for o in _objects(ran)] == ["previous_invalid"]


def test_policy_and_forget_help_say_what_they_do(capsys: pytest.CaptureFixture[str]) -> None:
    # The help says what `policy previous` lists, as its header does: what a pairing set aside
    # last, which is not the policy before the latest pair once a pairing from the fresh policy
    # set nothing aside (above). And `forget`'s says what it does with no pair held.
    def helped(*argv: str) -> str:
        with pytest.raises(SystemExit) as exit_info:
            main([*argv, "--help"])
        assert exit_info.value.code == 0
        return " ".join(capsys.readouterr().out.split())

    assert " policy show the last policy a pairing set aside, or reset an invalid one " in helped()
    assert (
        " previous list the operations and folders of the last policy a pairing set aside "
        in helped("policy")
    )
    assert " reset set aside an invalid policy file, and start from the fresh policy " in helped(
        "policy"
    )
    assert (
        " With no pair held, it leaves the work rooms still held over a pair already gone from "
        "this computer, and tells Lucille nothing. " in helped("forget")
    )


def test_policy_archive(tmp_path: Path) -> None:
    current, previous = tmp_path / "policy.toml", tmp_path / "policy.previous.toml"
    assert policy.archive(current, previous) is False
    assert not current.exists() and not previous.exists()
    policy.save(current, policy.fresh())
    assert policy.archive(current, previous) is False
    assert not previous.exists()
    current.write_text("not toml [")
    assert policy.archive(current, previous) is True
    assert previous.read_text() == "not toml ["
    assert policy.load(current) == policy.fresh()


# --- footprint -----------------------------------------------------------------------------------


def test_pairing_spawns_nothing(cli: HostCli) -> None:
    lucille, second, third = cli.lucille(), cli.lucille(), cli.lucille()
    with _audit_hook.recording() as events:
        cli.paired(lucille)
        assert cli.run("pair", "status").rc == 0
        assert cli.run("pair", "reject").rc == 0
        assert cli.connect(second, stdin=STDIN + "n\n").rc == 1
        assert cli.connect(third, stdin=STDIN + "y\n").rc == 0
        assert cli.run("pair", "confirm").rc == 0
    assert _audit_hook.spawns(events) == []


def _files(root: Path) -> set[Path]:
    return {path for path in root.rglob("*") if not path.is_dir()}


@pytest.mark.parametrize("store_file", [False, True], ids=["keyring", "file"])
def test_no_write_under_home_switchboard(store_file: bool, cli: HostCli) -> None:
    before = _files(cli.env.home)
    options = ["--store-file"] if store_file else []
    lucille = cli.lucille()
    ran = cli.paired(lucille, *options)
    assert ("weaker" in ran.err) is store_file
    assert cli.run("pair", "status").rc == 0
    assert cli.run("pair", "reject").rc == 0
    cli.paired(cli.lucille(), *options)
    assert cli.run("pair", "confirm").rc == 0
    assert not (cli.env.home / ".switchboard").exists()
    written = _files(cli.env.home) - before
    assert written
    own = (cli.dirs.config, cli.dirs.state)
    assert all(any(path.is_relative_to(folder) for folder in own) for path in written), written
    assert bool(cli.env.keyring.items) is not store_file


def test_pairing_verbs_refuse_root_and_switchboard_settings(
    cli: HostCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWITCHBOARD_URL", "https://hub.example")
    pairing_verbs = (
        ["pair"],
        ["pair", "begin"],
        ["pair", "resume"],
        ["pair", "cancel"],
        ["pair", "status"],
        ["pair", "confirm"],
        ["pair", "reject"],
    )
    # So do `run`, `status` and `forget`, the other verbs that open the pair's store.
    others = (["run"], ["status"], ["forget"])
    # `run` has exit codes of its own: 24 for a refused environment (spec/CLI.md section 13.3).
    for argv in (*pairing_verbs, *others):
        ran = cli.run(*argv, timeout=15)
        assert ran.rc == (24 if argv == ["run"] else 1) and "SWITCHBOARD_URL is set" in ran.err
    monkeypatch.delenv("SWITCHBOARD_URL")
    monkeypatch.setattr("os.geteuid", lambda: 0)
    for argv in (["pair"], ["pair", "begin"], *others):
        ran = cli.run(*argv, timeout=15)
        assert ran.rc == (24 if argv == ["run"] else 1) and "root" in ran.err
    assert cli.gate() is None and not cli.dirs.host_file.exists()
    # Refused before it takes the pair lock: a refused `pair`, root's above all, makes no file.
    assert not (cli.dirs.state / PAIR_LOCK).exists()
    # So are the verbs that touch no pair, since the uid check stands before every verb
    # (decisions/0004-running-as-root.md); tests/test_root_refused.py has each of them.
    for argv in (["policy", "previous"], ["ops", "list"], ["log"]):
        ran = cli.run(*argv, timeout=15)
        assert ran.rc == 1 and "root" in ran.err
    # Where the config folder is one of its own (Linux), the refused verbs made none. On macOS it is
    # the state folder's parent, which the test's set-up has made already.
    if not cli.dirs.state.is_relative_to(cli.dirs.config):
        assert not cli.dirs.config.exists()


# --- the pieces ----------------------------------------------------------------------------------


def test_outcome_of_every_pairing_state() -> None:
    assert {s: link.outcome(s) for s in ("already_redeemed", "aborted", "superseded")} == {
        "already_redeemed": "leaked",
        "aborted": "leaked",
        "superseded": "leaked",
    }
    for word in ("expired", "never_confirmed", "unsent", "wrong_hub"):
        assert link.outcome(word) == "expired"
    assert link.outcome("ended_by_peer") == link.outcome("ended") == "ended"
    assert link.outcome("unknown") == "none"
    for word in ("tampered", "upgrade", "store", "disabled", "pending", "name_taken", "error"):
        assert link.outcome(word) == "error"


def test_gate_answers_and_record(tmp_path: Path) -> None:
    for entry in ("y", "Y", "yes", " Yes \n", "YES"):
        assert gate.answer(entry) is True, entry
    for entry in ("n", "N", "no", " No \n"):
        assert gate.answer(entry) is False, entry
    for entry in ("", "\n", "ye", "yess", "nope", "ok", "1234-5678", "12345678", "y n", "ｙ"):
        assert gate.answer(entry) is None, entry
    record = gate.Gate(pair="p", hub=_switchboard.LUCILLE_HUB, created_at=10.0)
    shown = gate.shown(record, 100.0)
    assert (shown.shown_at, shown.deadline) == (100.0, 100.0 + gate.WINDOW)
    assert gate.shown(shown, 500.0) == shown
    assert not gate.past_deadline(record, 10.0**12)
    assert not gate.past_deadline(shown, 100.0 + gate.WINDOW)
    assert gate.past_deadline(shown, 100.0 + gate.WINDOW + 1)
    # A gate opened in time is past no deadline, whenever it is read.
    opened = dataclasses.replace(shown, opened_at=100.0 + gate.WINDOW)
    assert not gate.past_deadline(opened, 100.0 + gate.WINDOW + 1)
    path = tmp_path / "pair.json"
    gate.save(path, shown)
    assert gate.load(path) == shown
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    data = json.loads(path.read_text())
    assert set(data) == {
        "v",
        "pair",
        "hub",
        "created_at",
        "shown_at",
        "deadline",
        "opened_at",
        "touched_at",
    }
    for bad in (
        {**data, "shown_at": None},
        {**data, "deadline": True},
        {k: v for k, v in data.items() if k != "touched_at"},
        {**data, "shown_at": None, "deadline": None, "opened_at": 200.0},
        {**data, "extra": 1},
        {**data, "v": 2},
        [data],
    ):
        path.write_text(json.dumps(bad))
        with pytest.raises(Exception, match="not a lucille-host gate record"):
            gate.load(path)
    assert gate.remove(path) is True
    assert gate.remove(path) is False
    assert gate.load(path) is None


def test_dirs_name_the_new_files(home: Path) -> None:
    dirs = paths.dirs()
    assert dirs.gate_file == dirs.state / "pair.json"
    assert dirs.previous_policy_file == dirs.config / "policy.previous.toml"


@pytest.mark.parametrize(
    ("said", "waits"),
    [
        (0.7, 0.7),
        (0, 0.0),
        (-5, 0.0),
        (1e9, _switchboard.RETRY_AFTER_MAX),
        (float("inf"), None),
        (float("nan"), None),
        (True, None),
        ("30", None),
    ],
)
def test_the_hubs_retry_after_is_bounded(said: object, waits: float | None) -> None:
    # A hub that asks for a wait longer than RETRY_AFTER_MAX, or for one that is not a finite
    # number, does not stall pair or run for longer.
    error = client.SwitchboardError("busy", status=429, payload={"retry_after": said})
    assert _switchboard._hub_error(error).retry_after == waits
