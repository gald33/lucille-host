"""Check 3 of the host's accept policy: the room's hub is the one its pairing code named.

The host has no accept code yet, so this tests the spec's reading of the check (spec/PROTOCOL.md,
section 6) and the library facts it rests on, not the host. Check 3 compares the two URLs as
strings once every trailing ``/`` is removed from each, and nothing else is normalised: a URL that
ends in ``//`` is the same hub, and one that differs in case, port, query, fragment or whitespace
is not. agent-switchboard 2.6.0 removes every trailing ``/``, and only that, both when it decodes
a pairing code (its ``pairing.py:599``) and when it keeps an offered room (its
``sharing.py:1613``); the last three tests show each by running the library, and hold the reading
to the URLs the library keeps. A mismatch ends in a leave, and a leave is final for the room, so a
reading that removed fewer would cost the host a room it should have kept, and a room minted from
another spelling of the hub's URL is lost to it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from switchboard import pairing, sharing
from switchboard.pairing import PairingCode
from switchboard.testing import hub

from tests.spec import _support

HUB = "https://hub.example"
CASES = [
    (HUB, HUB, True),
    (HUB, f"{HUB}/", True),
    (HUB, f"{HUB}//", True),
    (f"{HUB}//", f"{HUB}/", True),
    (f"{HUB}/sb", f"{HUB}/sb//", True),
    (HUB, f"{HUB}/sb", False),
    (HUB, "http://hub.example", False),
    (HUB, "https://other.example", False),
    # Nothing but trailing slashes is removed: these are other strings, so other hubs.
    (HUB, "HTTPS://HUB.EXAMPLE", False),
    (HUB, "https://Hub.example", False),
    (HUB, f"{HUB}:443", False),
    (HUB, f"{HUB}/?a", False),
    (HUB, f"{HUB}#top", False),
    (HUB, f" {HUB} ", False),
    (HUB, f"{HUB}/ ", False),
]


@pytest.mark.parametrize(("room", "code", "same"), CASES)
def test_check_3_compares_strings_once_trailing_slashes_are_removed(
    room: str, code: str, same: bool
) -> None:
    assert _support.same_hub(room, code) is same
    assert _support.same_hub(code, room) is same


@pytest.mark.parametrize(
    "url", [HUB, f"{HUB}/", f"{HUB}//", f"{HUB}/sb///", "HTTPS://HUB.EXAMPLE//", f"{HUB}:443/"]
)
def test_the_library_keeps_a_codes_hub_with_every_trailing_slash_removed(url: str) -> None:
    code = PairingCode(
        url=url, secret=bytes(32), initiator=bytes(32), expires_at=0, commit=bytes(16)
    )
    assert PairingCode.decode(code.encode()).url == url.rstrip("/")


def _offer_and_accept(
    tmp_path: Path, *, offered_from: str | None = None, written_as: str | None = None
) -> tuple[str, str, str]:
    """Pair, offer a room and accept it, on the library's in-process test hub.

    Lucille's side mints the room with ``share_new`` from a config whose URL is ``offered_from``
    (the test hub's own URL when None), and, when ``written_as`` is given, the room's record is
    then made to carry that URL, as a sender that did not remove its trailing ``/`` would write
    it. The host accepts with the library's own ``accept``. Returns the hub's URL, the URL the
    host keeps for the room, and the URL it keeps for the pairing code's hub.
    """
    with hub() as h:
        connect = h.client_class()
        lucille, host = (
            h.client_config(agent_id="lucille"),
            h.client_config(agent_id="lucille-host"),
        )
        lucille_pairs, host_pairs = (pairing.PairStore(tmp_path / s) for s in ("l", "h"))
        lucille_rooms, host_rooms = (sharing.ShareStore(tmp_path / s) for s in ("l", "h"))
        started = pairing.start(lucille, store=lucille_pairs, connect=connect, now=h.now)
        pairing.accept(
            started.code, host, store=host_pairs, connect=connect, name="lucille", now=h.now
        )
        pairing.collect(started.name, lucille, store=lucille_pairs, connect=connect, now=h.now)
        minter = lucille if offered_from is None else replace(lucille, url=offered_from(h.url))
        sharing.share_new(
            "work",
            minter,
            pairs=[started.name],
            store=lucille_rooms,
            pair_store=lucille_pairs,
            connect=connect,
            now=h.now,
        )
        if written_as is not None:
            held = lucille_rooms.load()
            held["rooms"]["work"]["url"] = written_as(h.url)
            lucille_rooms.save(held)
        sharing.touch(
            lucille,
            pairs=[started.name],
            store=lucille_rooms,
            pair_store=lucille_pairs,
            connect=connect,
            now=h.now,
        )
        kept = sharing.accept(
            "lucille/work",
            host,
            store=host_rooms,
            pair_store=host_pairs,
            connect=connect,
            now=h.now,
        )
        room = sharing.resolve_room(kept.name, host, store=host_rooms, now=h.now).config.url
        named = host_pairs.get("lucille").url
        return h.url, room, named


# The test hub runs on starlette's test client, which warns that its use of httpx is deprecated;
# that is the library's concern, not this test's.
@pytest.mark.filterwarnings("ignore:Using `httpx` with `starlette.testclient` is deprecated")
@pytest.mark.parametrize("suffix", ["", "/", "//"])
def test_the_library_keeps_an_offered_rooms_hub_with_every_trailing_slash_removed(
    suffix: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The offer carries the hub's URL with `suffix` after it. Check 3 reads the two as one hub.
    seen: list[str] = []
    unusable = sharing._unusable

    def watch(offer: dict) -> str | None:
        seen.append(offer.get("url"))
        return unusable(offer)

    monkeypatch.setattr(sharing, "_unusable", watch)
    url, room, named = _offer_and_accept(tmp_path, written_as=lambda u: u + suffix)
    assert seen and set(seen) == {url + suffix}
    assert room == named == url
    assert _support.same_hub(url + suffix, named)
    assert _support.same_hub(room, named)


@pytest.mark.filterwarnings("ignore:Using `httpx` with `starlette.testclient` is deprecated")
@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param(str.upper, id="upper-case"),
        pytest.param(lambda u: u.replace("://hub.", "://HUB."), id="host-case"),
        pytest.param(lambda u: u + ":80", id="default-port"),
        pytest.param(lambda u: u + "/?a", id="query"),
        pytest.param(lambda u: u + "#top", id="fragment"),
    ],
)
def test_the_library_keeps_an_offered_rooms_hub_as_written_otherwise(
    spelling: Callable[[str], str], tmp_path: Path
) -> None:
    # Lucille mints the room from another spelling of the same hub's URL. The library keeps that
    # spelling for the room, so check 3 reads it as another hub, and the host would leave the room:
    # why section 6 has Lucille mint each room with the URL its pairing code carries.
    url, room, named = _offer_and_accept(tmp_path, offered_from=spelling)
    assert named == url
    assert room == spelling(url).rstrip("/") != url
    assert not _support.same_hub(room, named)
