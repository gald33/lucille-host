"""A Connect request on this computer: decisions/0002 §11.4, steps 1 to 8.

``pair begin`` draws a connect code, posts the host's hello into the request's drop room and
keeps the request in the state folder (:func:`begin`). ``pair begin`` and ``pair resume`` then
read the room every :data:`POLL_EVERY` seconds (:func:`poll`) until a delivery passes, the request
ends, or the user stops them; ``pair status`` reads it once and redeems nothing. A delivery that
passed goes through :func:`redeemable` before the code is answered.

**The request** (:class:`Request`) is kept in ``state/connect.json``, 0600, written in one step:
the code, its epoch, the hub, the chat service and the normalized username the user typed, when
the hello was posted and when the request expires, whether it was used, and what the reads so far
saw. One request is outstanding at a time; a new ``begin`` replaces it. The file is deleted when
the request ends, whatever ended it.

**When it expires.** 20 minutes after the earlier of the hub's time of the hello and this
computer's clock when it posted it, checked against this computer's clock at every read and
before the code is answered. A read counts for the rules below only if it completed before the
expiry. At the expiry, in this order (:func:`expiry_code`):

1. a delivery read and never answered: ``interference`` (§11.4 step 5, the third rule);
2. else the last read failed at the hub: ``interference`` (the second rule);
3. else a post was ignored only for a key id this release does not pin: ``newer_capsule``;
4. else ``connect_expired``.

A read that holds no hello, or two, or 50 posts or more, or that failed other than at the hub, is
``interference`` at once (the first rule, and "The wire"); two deliveries are ``key_misused``.
Each of these spends the request.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Final

from lucille_host import _switchboard, connect, delivery, state

__all__ = [
    "LIFE",
    "POLL_EVERY",
    "Ended",
    "Live",
    "Request",
    "Step",
    "begin",
    "expiry_code",
    "load",
    "open_request",
    "poll",
    "redeemable",
    "remove",
    "save",
]

VERSION: Final = 1
# How long a request lives, from the earlier of the hub's time of the hello and this computer's.
LIFE: Final = float(connect.EPOCH_SECONDS)
# How often ``pair begin`` and ``pair resume`` read the drop room.
POLL_EVERY: Final = 5.0

# What the last read of the drop room ended in: none yet, read, or a hub failure.
_READS: Final = frozenset({"none", "ok", "hub_error"})
_FIELDS: Final = frozenset(
    {
        "v",
        "code",
        "epoch",
        "hub",
        "service",
        "username",
        "posted_at",
        "expires_at",
        "used",
        "last_read",
        "delivery_seen",
        "unknown_key_seen",
    }
)
# The note Lucille gives the pairing it delivers: the request's nonce, in lowercase hex.
_NOTE: Final = re.compile(r"lucille-host/2 n=([0-9a-f]{32})")

# How a request ends. Each is an ``error`` code of the command line.
INTERFERENCE: Final = "interference"
EXPIRED: Final = "connect_expired"
NEWER_CAPSULE: Final = "newer_capsule"
KEY_MISUSED: Final = "key_misused"
OTHER_ACCOUNT: Final = "other_account"
NOT_THIS_MAC: Final = "not_this_mac"

MESSAGES: Final = {
    INTERFERENCE: (
        "Capsule couldn't read Lucille's answer, and the code may be known to someone else. "
        "Start again."
    ),
    EXPIRED: "The code expired. Start again.",
    NEWER_CAPSULE: ("Lucille may need a newer Capsule. Get it from where you got this one."),
    KEY_MISUSED: (
        "Lucille's answer came twice, which Lucille never does. Nothing was connected. Start again."
    ),
    OTHER_ACCOUNT: (
        "This code was sent to Lucille from a different account. Nothing was connected. Check "
        "that you sent it to the official Lucille account, then start again."
    ),
    NOT_THIS_MAC: "This connection wasn't started on this Mac. Nothing was connected.",
}


class Ended(Exception):
    """The request ended. ``code`` is the command line's error code, ``why`` names the rule that
    ended it, for the log, and nothing from the room. ``sender`` is the other account's signed
    ``from`` (service and stable id, never its handle), for an ``other_account`` only."""

    def __init__(self, code: str, why: str = "", *, sender: str = "") -> None:
        super().__init__(MESSAGES[code])
        self.code = code
        self.why = why
        self.sender = sender


@dataclass(frozen=True)
class Request:
    """A Connect request, as the state folder keeps it."""

    # The connect code, canonical. Shown to the user while the request is live; never logged.
    code: str = field(repr=False)
    epoch: int
    hub: str
    service: str
    # The user's own username, normalized. Checked on this computer and sent nowhere.
    username: str = field(repr=False)
    posted_at: float
    expires_at: float
    used: bool = False
    last_read: str = "none"
    delivery_seen: bool = False
    unknown_key_seen: bool = False


def _valid(fields: dict[str, object]) -> bool:
    code, service, username = fields["code"], fields["service"], fields["username"]
    epoch = fields["epoch"]
    return (
        isinstance(code, str)
        and connect.parse_code(code) == code
        and isinstance(epoch, int)
        and not isinstance(epoch, bool)
        and epoch >= 0
        and isinstance(fields["hub"], str)
        and isinstance(service, str)
        and service in connect.SERVICES
        and isinstance(username, str)
        and connect.normalize_username(username, service) == username
        and state.valid_time(fields["posted_at"])
        and state.valid_time(fields["expires_at"])
        and all(
            isinstance(fields[key], bool) for key in ("used", "delivery_seen", "unknown_key_seen")
        )
        and fields["last_read"] in _READS
    )


def load(path: Path) -> Request | None:
    """The request in ``path``, or None when there is none. Anything else stops the host
    (:class:`state.StateError`)."""
    data = state.read_json(path)
    if data is None:
        return None
    if not isinstance(data, dict) or set(data) != _FIELDS or data["v"] != VERSION:
        raise state.StateError(f"{path} is not a lucille-host Connect request")
    fields = {key: data[key] for key in _FIELDS - {"v"}}
    if not _valid(fields):
        raise state.StateError(f"{path} is not a lucille-host Connect request")
    return Request(**fields)  # type: ignore[arg-type]


def save(path: Path, request: Request) -> None:
    """Write the request in one step, synced to disk before this returns."""
    state.write_json(path, {"v": VERSION, **asdict(request)})


def remove(path: Path) -> bool:
    return state.remove_file(path)


@dataclass(frozen=True)
class Live:
    """A request with what this process derived from it: ``K``, the drop room and ``N``. The
    stretch costs a quarter of a second or more, so a process derives them once."""

    request: Request
    key: bytes = field(repr=False)
    nonce: bytes = field(repr=False)
    drop: _switchboard.DropRoom


def open_request(request: Request) -> Live:
    key = connect.stretch(request.code, request.epoch)
    return Live(
        request=request,
        key=key,
        nonce=connect.nonce(key),
        drop=_switchboard.drop_room(key, request.hub),
    )


def begin(
    link: _switchboard.Link,
    path: Path,
    *,
    service: str,
    username: str,
    hub: str,
    now: Callable[[], float],
) -> Live:
    """Draw a code, post the hello into its drop room, and keep the request, replacing any
    other. ``username`` is normalized already. A hub failure is a :class:`_switchboard.HubError`,
    and no request is kept: the hello may or may not have reached the room, and no one will look
    for it there without the code."""
    if connect.normalize_username(username, service) != username:
        raise ValueError("not a username as connect.normalize_username gives one")
    code = connect.draw_code()
    request = Request(
        code=code,
        epoch=connect.epoch(now()),
        hub=hub,
        service=service,
        username=username,
        posted_at=0.0,
        expires_at=0.0,
    )
    live = open_request(request)
    posted_at = now()
    with link.drop_client(live.drop) as client:
        posted = client.post(connect.CHANNEL, connect.HELLO, ttl=connect.POST_TTL)
    start = posted_at if posted.created_at is None else min(posted_at, posted.created_at)
    request = replace(request, posted_at=posted_at, expires_at=max(start, 0.0) + LIFE)
    save(path, request)
    return replace(live, request=request)


def expiry_code(request: Request) -> str:
    """The code a request that has expired ends with (the order is the module's)."""
    if request.delivery_seen and not request.used:
        return INTERFERENCE
    if request.last_read == "hub_error":
        return INTERFERENCE
    if request.unknown_key_seen:
        return NEWER_CAPSULE
    return EXPIRED


@dataclass(frozen=True)
class Step:
    """What one read of the drop room found, the request having not ended: ``found`` is the
    delivery that passed, or None to wait on; ``ignored`` names why each other post was
    ignored, and nothing from it; ``hub_error`` is set when the hub could not be read."""

    live: Live
    found: delivery.Delivery | None = None
    ignored: tuple[str, ...] = ()
    hub_error: _switchboard.HubError | None = None


def _end(path: Path, code: str, why: str = "", *, sender: str = "") -> Ended:
    remove(path)
    return Ended(code, why, sender=sender)


def poll(
    link: _switchboard.Link,
    path: Path,
    live: Live,
    *,
    now: Callable[[], float],
    spend: bool = True,
) -> Step:
    """Read the drop room once and judge what it holds. Raises :class:`Ended` when the request
    has expired or the read ends it, with the file deleted unless ``spend`` is false; otherwise
    records what the read saw and returns a :class:`Step`. Redeems nothing, and posts nothing.
    ``pair status`` reads with ``spend`` false: it reports how the request ended, and leaves the
    request for ``pair resume`` or ``pair begin`` to spend or replace."""

    def ending(code: str, why: str) -> Ended:
        return _end(path, code, why) if spend else Ended(code, why)

    request = live.request
    if now() >= request.expires_at:
        raise ending(expiry_code(request), "the request expired")
    failure: _switchboard.HubError | None = None
    rows: list[object] = []
    try:
        with link.drop_client(live.drop) as client:
            rows = client.read(connect.CHANNEL, limit=connect.READ_LIMIT)
    except _switchboard.HubError as exc:
        failure = exc
    except _switchboard.Interfered as exc:
        raise ending(INTERFERENCE, str(exc)) from None
    # A read counts only if it completed before the expiry, by this computer's clock.
    if now() >= request.expires_at:
        raise ending(expiry_code(request), "the request expired")
    if failure is not None:
        request = replace(request, last_read="hub_error")
        save(path, request)
        return Step(replace(live, request=request), hub_error=failure)
    # Looked up at each read, not bound when this module loads: the release's own keys.
    read = connect.judge(
        rows,
        live.nonce,
        keys=_switchboard.LUCILLE_SIGNING_KEYS,
        revoked=_switchboard.LUCILLE_REVOKED_KEY_IDS,
    )
    if read.outcome == connect.INTERFERENCE:
        raise ending(INTERFERENCE, read.why)
    if read.outcome == connect.TWO_DELIVERIES:
        raise ending(KEY_MISUSED, "more than one delivery passed")
    request = replace(
        request,
        last_read="ok",
        delivery_seen=request.delivery_seen or read.found is not None,
        unknown_key_seen=request.unknown_key_seen or read.unknown_key,
    )
    save(path, request)
    return Step(replace(live, request=request), found=read.found, ignored=read.ignored)


def redeemable(
    path: Path,
    live: Live,
    found: delivery.Delivery,
    code: _switchboard.Code,
    *,
    now: Callable[[], float],
) -> Live:
    """The last checks on a delivery before its code is answered (§11.4, steps 7 and 8), with
    ``code`` already decoded and its hub and token checked: the signed account is the user's own,
    the code has the library's hello, its note names this request's nonce, and the request is
    unexpired and unused. Then the request is marked used, and synced to disk, before this
    returns. Any failure raises :class:`Ended` with the file deleted, but a used request, which
    is kept as it is."""
    request = live.request
    if not connect.names_user(found, request.service, request.username):
        raise _end(
            path, OTHER_ACCOUNT, "the signed account is not the one typed", sender=found.sender
        )
    if now() >= request.expires_at:
        raise _end(path, INTERFERENCE, "the request expired before the code was answered")
    if request.used:
        raise Ended(NOT_THIS_MAC, "the request was used already")
    matched = _NOTE.fullmatch(code.note)
    if not code.hello or matched is None:
        raise _end(path, NOT_THIS_MAC, "the code's note does not name a request")
    if not hmac.compare_digest(matched.group(1).encode("ascii"), live.nonce.hex().encode("ascii")):
        raise _end(path, NOT_THIS_MAC, "the code's note names another request")
    request = replace(request, used=True)
    save(path, request)
    return replace(live, request=request)
