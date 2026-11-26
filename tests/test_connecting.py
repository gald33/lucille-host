"""A Connect request on this computer (decisions/0002 §11.4, steps 1 to 8), on Switchboard's
in-process test hub, with a fake Lucille that signs with a throwaway key pinned in place of
Lucille's.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest
from switchboard import pairing
from switchboard.testing import hub

from lucille_host import _switchboard, connect, connecting, delivery, state
from tests._cli import HostCli
from tests._host import HostEnv
from tests._lucille import FakeLucille, Signer

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)


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
    host.stopping.set()


@pytest.fixture
def signer(monkeypatch: pytest.MonkeyPatch) -> Signer:
    """A throwaway key, pinned in place of Lucille's for the test."""
    made = Signer()
    monkeypatch.setattr("lucille_host._switchboard.LUCILLE_SIGNING_KEYS", made.keys)
    return made


class Clock:
    """This computer's clock, set by the test; it starts at the hub's."""

    def __init__(self, h: Any) -> None:
        self.at = float(h.now)

    def __call__(self) -> float:
        return self.at


@pytest.fixture
def clock(h: Any) -> Clock:
    return Clock(h)


def _link(cli: HostCli) -> _switchboard.Link:
    return _switchboard.open_link(
        cli.dirs, state.load_host_config(cli.dirs), client_class=cli.client_class
    )


def _path(cli: HostCli) -> Path:
    return cli.dirs.connect_file


def _begin(cli: HostCli, clock: Clock, username: str = "ada") -> connecting.Live:
    return connecting.begin(
        _link(cli),
        _path(cli),
        service="telegram",
        username=username,
        hub=_switchboard.LUCILLE_HUB,
        now=clock,
    )


def _poll(cli: HostCli, live: connecting.Live, clock: Clock) -> connecting.Step:
    return connecting.poll(_link(cli), _path(cli), live, now=clock)


def _ended(cli: HostCli, live: connecting.Live, clock: Clock) -> connecting.Ended:
    with pytest.raises(connecting.Ended) as caught:
        _poll(cli, live, clock)
    assert not _path(cli).exists()
    return caught.value


def _any_code() -> str:
    """A pairing code in its one encoding, for a post signed by a key this release does not pin."""
    return pairing.PairingCode(
        url=_switchboard.LUCILLE_HUB,
        secret=os.urandom(32),
        initiator=os.urandom(32),
        expires_at=2_000_000_000,
        commit=os.urandom(16),
        hello=True,
    ).encode()


def _shown(live: connecting.Live) -> str:
    return connect.shown(live.request.code)


# --- begin ---------------------------------------------------------------------------------


def test_begin_posts_the_hello_and_keeps_the_request(cli: HostCli, clock: Clock, h: Any) -> None:
    live = _begin(cli, clock)
    request = live.request
    assert connect.parse_code(request.code) == request.code
    assert request.epoch == connect.epoch(clock.at)
    assert request.service == "telegram"
    assert request.username == "ada"
    assert request.hub == _switchboard.LUCILLE_HUB
    assert not request.used
    assert request.last_read == "none"
    assert connecting.load(_path(cli)) == request
    assert _path(cli).stat().st_mode & 0o777 == 0o600
    lucille = cli.lucille()
    rows = lucille.read_drop(live.key)
    assert [row["body"] for row in rows] == [connect.HELLO]


def test_the_request_expires_twenty_minutes_from_the_earlier_clock(
    cli: HostCli, clock: Clock, h: Any
) -> None:
    # This computer's clock ahead of the hub's: the hub's time is the earlier.
    clock.at = float(h.now) + 100
    live = _begin(cli, clock)
    assert live.request.posted_at == clock.at
    assert abs(live.request.expires_at - (float(h.now) + connecting.LIFE)) < 2
    # Behind it: this computer's own.
    clock.at = float(h.now) - 100
    live = _begin(cli, clock)
    assert live.request.expires_at == clock.at + connecting.LIFE


def test_a_new_begin_replaces_the_request(cli: HostCli, clock: Clock) -> None:
    first = _begin(cli, clock)
    second = _begin(cli, clock)
    assert first.request.code != second.request.code
    assert connecting.load(_path(cli)) == second.request


def test_begin_keeps_no_request_when_the_hub_cannot_be_reached(
    cli: HostCli, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(*args: Any, **kwargs: Any) -> Any:
        raise _switchboard.HubError("the hub could not be reached (OSError)")

    monkeypatch.setattr("lucille_host._switchboard.DropClient.post", down)
    with pytest.raises(_switchboard.HubError):
        _begin(cli, clock)
    assert not _path(cli).exists()


def test_begin_takes_only_a_normalized_username(cli: HostCli, clock: Clock) -> None:
    for name in ("@ada", "ADA", "telegram:ada"):
        with pytest.raises(ValueError):
            _begin(cli, clock, username=name)
    assert not _path(cli).exists()


def test_the_request_keeps_its_code_and_username_out_of_its_repr(
    cli: HostCli, clock: Clock
) -> None:
    live = _begin(cli, clock, username="ada_lovelace")
    for text in (repr(live), repr(live.request)):
        assert live.request.code not in text
        assert "ada_lovelace" not in text
    assert live.key.hex() not in repr(live)


# --- the file ------------------------------------------------------------------------------


def _saved(cli: HostCli, clock: Clock) -> dict[str, Any]:
    _begin(cli, clock)
    return json.loads(_path(cli).read_text())


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("v", 2),
        ("code", "7kq4mx2d9p"),
        ("code", "7KQ4M-X2D9P"),
        ("epoch", -1),
        ("epoch", True),
        ("epoch", 1.5),
        ("service", "slack"),
        ("username", "@ada"),
        ("username", "ADA"),
        ("posted_at", -1),
        ("expires_at", "soon"),
        ("used", 0),
        ("last_read", "maybe"),
        ("delivery_seen", None),
        ("unknown_key_seen", "no"),
        ("hub", 1),
    ],
)
def test_a_file_that_is_not_a_request_stops_the_host(
    cli: HostCli, clock: Clock, key: str, value: Any
) -> None:
    data = _saved(cli, clock)
    data[key] = value
    _path(cli).write_text(json.dumps(data))
    with pytest.raises(state.StateError):
        connecting.load(_path(cli))


def test_a_missing_or_extra_field_stops_the_host(cli: HostCli, clock: Clock) -> None:
    data = _saved(cli, clock)
    _path(cli).write_text(json.dumps({**data, "extra": 1}))
    with pytest.raises(state.StateError):
        connecting.load(_path(cli))
    del data["used"]
    _path(cli).write_text(json.dumps(data))
    with pytest.raises(state.StateError):
        connecting.load(_path(cli))
    _path(cli).write_text("[]")
    with pytest.raises(state.StateError):
        connecting.load(_path(cli))


def test_no_file_is_no_request(cli: HostCli) -> None:
    assert connecting.load(_path(cli)) is None
    assert not connecting.remove(_path(cli))


# --- one read ------------------------------------------------------------------------------


def test_a_room_with_only_the_hello_is_waited_on(
    cli: HostCli, clock: Clock, signer: Signer
) -> None:
    live = _begin(cli, clock)
    step = _poll(cli, live, clock)
    assert step.found is None and step.hub_error is None and step.ignored == ()
    assert step.live.request.last_read == "ok"
    assert connecting.load(_path(cli)) == step.live.request


def test_lucilles_delivery_is_found(cli: HostCli, clock: Clock, signer: Signer) -> None:
    live = _begin(cli, clock)
    lucille = cli.lucille()
    code = lucille.deliver(_shown(live), signer)
    step = _poll(cli, live, clock)
    assert step.found is not None
    assert step.found.code == code
    assert step.found.sender == "telegram:123456789"
    assert step.live.request.delivery_seen
    assert connecting.load(_path(cli)) == step.live.request


def test_a_delivery_for_another_epoch_is_not_this_requests(
    cli: HostCli, clock: Clock, signer: Signer
) -> None:
    live = _begin(cli, clock)
    # Lucille's clock in the next epoch: another room, which holds no hello, and so no request.
    lucille = cli.lucille()
    lucille.deliver(_shown(live), signer, at_epoch=live.request.epoch + 1)
    step = _poll(cli, live, clock)
    assert step.found is None


def test_a_post_that_is_not_a_delivery_is_ignored_and_named(
    cli: HostCli, clock: Clock, signer: Signer
) -> None:
    live = _begin(cli, clock)
    lucille = cli.lucille()
    lucille.drop(live.key, "junk")
    lucille.drop(
        live.key, Signer().delivery(_any_code(), live.nonce, sender="telegram:1", handle="")
    )
    step = _poll(cli, live, clock)
    assert step.found is None
    assert step.ignored == (delivery.NOT_JSON, delivery.UNKNOWN_KEY)
    assert step.live.request.unknown_key_seen


def test_a_hub_failure_is_recorded_and_waited_on(
    cli: HostCli, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = _begin(cli, clock)

    def down(*args: Any, **kwargs: Any) -> Any:
        raise _switchboard.HubError("the hub could not be reached (OSError)")

    monkeypatch.setattr("lucille_host._switchboard.DropClient.read", down)
    step = _poll(cli, live, clock)
    assert step.hub_error is not None
    assert step.live.request.last_read == "hub_error"
    assert connecting.load(_path(cli)) == step.live.request


def test_a_room_that_lost_its_hello_is_interference(cli: HostCli, clock: Clock) -> None:
    live = _begin(cli, clock)
    # The same request, read in a room that never held its hello: what a hub that dropped the
    # room's posts shows.
    other = connecting.open_request(replace(live.request, epoch=live.request.epoch + 7))
    ended = _ended(cli, other, clock)
    assert ended.code == connecting.INTERFERENCE
    assert ended.why == connect.NO_HELLO


def test_a_second_hello_is_interference(cli: HostCli, clock: Clock) -> None:
    live = _begin(cli, clock)
    cli.lucille().drop(live.key, connect.HELLO)
    assert _ended(cli, live, clock).why == connect.HELLO_TWICE


def test_a_read_that_fails_other_than_at_the_hub_is_interference(
    cli: HostCli, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = _begin(cli, clock)

    def odd(*args: Any, **kwargs: Any) -> Any:
        raise _switchboard.Interfered("ValueError")

    monkeypatch.setattr("lucille_host._switchboard.DropClient.read", odd)
    assert _ended(cli, live, clock).code == connecting.INTERFERENCE


def test_two_deliveries_misuse_the_key(cli: HostCli, clock: Clock, signer: Signer) -> None:
    live = _begin(cli, clock)
    lucille = cli.lucille()
    lucille.deliver(_shown(live), signer)
    lucille.deliver(_shown(live), signer)
    assert _ended(cli, live, clock).code == connecting.KEY_MISUSED


# --- the expiry ------------------------------------------------------------------------------


def _request(**changes: Any) -> connecting.Request:
    base = connecting.Request(
        code="7KQ4MX2D9P",
        epoch=1,
        hub=_switchboard.LUCILLE_HUB,
        service="telegram",
        username="ada",
        posted_at=1.0,
        expires_at=2.0,
    )
    return replace(base, **changes)


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({}, connecting.EXPIRED),
        ({"last_read": "ok"}, connecting.EXPIRED),
        ({"unknown_key_seen": True}, connecting.NEWER_CAPSULE),
        ({"last_read": "hub_error"}, connecting.INTERFERENCE),
        ({"last_read": "hub_error", "unknown_key_seen": True}, connecting.INTERFERENCE),
        ({"delivery_seen": True}, connecting.INTERFERENCE),
        ({"delivery_seen": True, "unknown_key_seen": True}, connecting.INTERFERENCE),
        ({"delivery_seen": True, "used": True}, connecting.EXPIRED),
        ({"delivery_seen": True, "used": True, "last_read": "hub_error"}, connecting.INTERFERENCE),
    ],
)
def test_what_an_expiry_ends_in(changes: dict[str, Any], code: str) -> None:
    assert connecting.expiry_code(_request(**changes)) == code


def test_a_read_at_the_expiry_ends_the_request(cli: HostCli, clock: Clock) -> None:
    live = _begin(cli, clock)
    clock.at = live.request.expires_at
    assert _ended(cli, live, clock).code == connecting.EXPIRED


def test_no_read_is_made_at_or_past_the_expiry(
    cli: HostCli, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = _begin(cli, clock)

    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the room was read past the expiry")

    monkeypatch.setattr("lucille_host._switchboard.DropClient.read", never)
    clock.at = live.request.expires_at
    assert _ended(cli, live, clock).code == connecting.EXPIRED


def test_an_unknown_key_seen_once_is_remembered(
    cli: HostCli, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = _begin(cli, clock)
    cli.lucille().drop(
        live.key, Signer().delivery(_any_code(), live.nonce, sender="telegram:1", handle="")
    )
    live = _poll(cli, live, clock).live
    assert live.request.unknown_key_seen
    # A later read that no longer shows that post (it lived out its time on the hub, say).
    monkeypatch.setattr(
        "lucille_host._switchboard.DropClient.read",
        lambda self, *a, **k: [{"body": connect.HELLO}],
    )
    live = _poll(cli, live, clock).live
    assert live.request.unknown_key_seen
    clock.at = live.request.expires_at
    assert _ended(cli, live, clock).code == connecting.NEWER_CAPSULE


def test_a_hub_failure_as_the_last_read_ends_in_interference(
    cli: HostCli, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = _begin(cli, clock)

    def down(*args: Any, **kwargs: Any) -> Any:
        raise _switchboard.HubError("the hub could not be reached (OSError)")

    with monkeypatch.context() as patched:
        patched.setattr("lucille_host._switchboard.DropClient.read", down)
        live = _poll(cli, live, clock).live
    clock.at = live.request.expires_at + 1
    assert _ended(cli, live, clock).code == connecting.INTERFERENCE


def test_a_blip_that_ended_before_the_expiry_is_plain_expiry(
    cli: HostCli, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = _begin(cli, clock)

    def down(*args: Any, **kwargs: Any) -> Any:
        raise _switchboard.HubError("the hub could not be reached (OSError)")

    with monkeypatch.context() as patched:
        patched.setattr("lucille_host._switchboard.DropClient.read", down)
        live = _poll(cli, live, clock).live
    live = _poll(cli, live, clock).live
    clock.at = live.request.expires_at + 1
    assert _ended(cli, live, clock).code == connecting.EXPIRED


def test_a_read_that_completes_after_the_expiry_counts_for_nothing(
    cli: HostCli, clock: Clock, signer: Signer, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = _begin(cli, clock)
    cli.lucille().deliver(_shown(live), signer)
    real = _switchboard.DropClient.read

    def slow(self: Any, *args: Any, **kwargs: Any) -> Any:
        rows = real(self, *args, **kwargs)
        clock.at = live.request.expires_at  # the Mac slept with the read in flight
        return rows

    monkeypatch.setattr("lucille_host._switchboard.DropClient.read", slow)
    # The delivery it found is not counted: the request ends in plain expiry.
    assert _ended(cli, live, clock).code == connecting.EXPIRED


def test_a_delivery_seen_but_not_answered_ends_in_interference(
    cli: HostCli, clock: Clock, signer: Signer
) -> None:
    live = _begin(cli, clock)
    cli.lucille().deliver(_shown(live), signer)
    live = _poll(cli, live, clock).live
    clock.at = live.request.expires_at + 1
    assert _ended(cli, live, clock).code == connecting.INTERFERENCE


def test_only_unknown_key_posts_at_the_expiry_ask_for_a_newer_capsule(
    cli: HostCli, clock: Clock, signer: Signer
) -> None:
    live = _begin(cli, clock)
    cli.lucille().drop(
        live.key, Signer().delivery(_any_code(), live.nonce, sender="telegram:1", handle="")
    )
    live = _poll(cli, live, clock).live
    clock.at = live.request.expires_at + 1
    assert _ended(cli, live, clock).code == connecting.NEWER_CAPSULE


# --- before the code is answered --------------------------------------------------------------


def _delivered(
    cli: HostCli, clock: Clock, signer: Signer, **options: Any
) -> tuple[connecting.Live, delivery.Delivery, _switchboard.Code, FakeLucille]:
    live = _begin(cli, clock)
    lucille = cli.lucille()
    lucille.deliver(_shown(live), signer, **options)
    step = _poll(cli, live, clock)
    assert step.found is not None
    code = _switchboard.decode_code(step.found.code)
    return step.live, step.found, code, lucille


def test_a_delivery_for_the_user_is_redeemable_and_marks_the_request_used(
    cli: HostCli, clock: Clock, signer: Signer
) -> None:
    live, found, code, _ = _delivered(cli, clock, signer)
    used = connecting.redeemable(_path(cli), live, found, code, now=clock)
    assert used.request.used
    assert connecting.load(_path(cli)) == used.request


def test_a_delivery_for_another_account_ends_the_request(
    cli: HostCli, clock: Clock, signer: Signer
) -> None:
    live, found, code, _ = _delivered(
        cli, clock, signer, sender="telegram:987654321", handle="@eve"
    )
    with pytest.raises(connecting.Ended) as caught:
        connecting.redeemable(_path(cli), live, found, code, now=clock)
    assert caught.value.code == connecting.OTHER_ACCOUNT
    assert caught.value.sender == "telegram:987654321"
    assert "eve" not in str(caught.value) + caught.value.why
    assert not _path(cli).exists()


def test_an_expiry_before_the_answer_ends_in_interference(
    cli: HostCli, clock: Clock, signer: Signer
) -> None:
    live, found, code, _ = _delivered(cli, clock, signer)
    clock.at = live.request.expires_at
    with pytest.raises(connecting.Ended) as caught:
        connecting.redeemable(_path(cli), live, found, code, now=clock)
    assert caught.value.code == connecting.INTERFERENCE
    assert not _path(cli).exists()


def test_a_used_request_is_not_this_macs_and_is_kept(
    cli: HostCli, clock: Clock, signer: Signer
) -> None:
    live, found, code, _ = _delivered(cli, clock, signer)
    used = connecting.redeemable(_path(cli), live, found, code, now=clock)
    with pytest.raises(connecting.Ended) as caught:
        connecting.redeemable(_path(cli), used, found, code, now=clock)
    assert caught.value.code == connecting.NOT_THIS_MAC
    assert connecting.load(_path(cli)) == used.request


@pytest.mark.parametrize(
    "note",
    [
        "",
        "lucille-host/2 n=" + "0" * 32,
        "lucille-host/1 n={nonce}",
        "lucille-host/2 n={NONCE}",
        "lucille-host/2 n={nonce} ",
        "lucille-host/2 n={nonce}\n",
        " lucille-host/2 n={nonce}",
        "lucille-host/2  n={nonce}",
    ],
)
def test_a_note_that_does_not_name_this_request_is_not_this_macs(
    cli: HostCli, clock: Clock, signer: Signer, note: str
) -> None:
    live = _begin(cli, clock)
    lucille = cli.lucille()
    nonce = live.nonce.hex()
    # The nonce in upper case; one of digits only has none, so its last digit becomes a capital.
    upper = nonce.upper() if nonce.upper() != nonce else nonce[:-1] + "A"
    lucille.deliver(_shown(live), signer, note=note.format(nonce=nonce, NONCE=upper))
    step = _poll(cli, live, clock)
    assert step.found is not None
    code = _switchboard.decode_code(step.found.code)
    with pytest.raises(connecting.Ended) as caught:
        connecting.redeemable(_path(cli), step.live, step.found, code, now=clock)
    assert caught.value.code == connecting.NOT_THIS_MAC
    assert not _path(cli).exists()


def test_a_code_without_the_librarys_hello_is_not_this_macs(
    cli: HostCli, clock: Clock, signer: Signer
) -> None:
    live, found, code, _ = _delivered(cli, clock, signer)
    with pytest.raises(connecting.Ended) as caught:
        connecting.redeemable(_path(cli), live, found, replace(code, hello=False), now=clock)
    assert caught.value.code == connecting.NOT_THIS_MAC


def test_a_read_that_does_not_spend_leaves_the_request(cli: HostCli, clock: Clock) -> None:
    live = _begin(cli, clock)
    clock.at = live.request.expires_at
    with pytest.raises(connecting.Ended) as caught:
        connecting.poll(_link(cli), _path(cli), live, now=clock, spend=False)
    assert caught.value.code == connecting.EXPIRED
    assert connecting.load(_path(cli)) == live.request


def test_every_ending_has_its_message() -> None:
    for code in (
        connecting.INTERFERENCE,
        connecting.EXPIRED,
        connecting.NEWER_CAPSULE,
        connecting.KEY_MISUSED,
        connecting.OTHER_ACCOUNT,
        connecting.NOT_THIS_MAC,
    ):
        assert str(connecting.Ended(code)) == connecting.MESSAGES[code]


def test_the_saved_request_is_the_dataclass(cli: HostCli, clock: Clock) -> None:
    live = _begin(cli, clock)
    data = json.loads(_path(cli).read_text())
    assert data == {"v": 1, **asdict(live.request)}
