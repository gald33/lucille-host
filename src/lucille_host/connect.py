"""Connect from Lucille's chat: the computer's side of decisions/0002 §11.4 that needs no hub.

The user asks Capsule to connect, sends Lucille the connect code it shows, and Lucille posts its
signed pairing code into a drop room only the two of them can find. This module holds what both
ends must compute alike, and how the computer judges one read of that room:

- **The connect code** ``C`` (:func:`draw_code`, :func:`parse_code`, :func:`shown`): ten symbols
  of Crockford's base32, 50 bits from ``secrets``, shown as two groups of five.
- **The epoch** ``E`` (:func:`epoch`), **the key** ``K`` (:func:`stretch`), scrypt of the code
  salted with the epoch, and **the nonce** ``N`` (:func:`nonce`), HKDF-SHA256 of ``K``. The drop
  room is the library's handshake room for ``K``, which the hub-facing code builds.
- **The user's own username** (:func:`normalize_username`), typed before the code is shown, and
  the check that a delivery's signed account names it (:func:`names_user`, decisions/0002 §11.5).
- **Lucille's official chat accounts** (:data:`OFFICIAL_ACCOUNTS`, :func:`official_accounts`), which
  Capsule names beside the code (decisions/0002 §11.4 step 1, §11.7).
- **One read of the drop room** (:func:`judge`): the host's hello, the deliveries that pass
  ``delivery.verify``, and the posts it ignores.

What this module does not do: talk to the hub, keep the request in the state folder, poll, tell a
hub failure from interference in a read that raised, redeem a code, or show anything. Those come
with ``pair begin``.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from lucille_host import _switchboard, delivery

__all__ = [
    "ALPHABET",
    "CHANNEL",
    "EPOCH_SECONDS",
    "HELLO",
    "MOST_ACCOUNTS",
    "OFFICIAL_ACCOUNTS",
    "POST_TTL",
    "READ_LIMIT",
    "SERVICES",
    "SYMBOLS",
    "Account",
    "Read",
    "check_accounts",
    "draw_code",
    "epoch",
    "judge",
    "names_user",
    "nonce",
    "normalize_username",
    "official_accounts",
    "parse_code",
    "shown",
    "stretch",
]

# Crockford's base32: the digits and the capitals, less I, L, O and U.
ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
# Ten symbols of five bits: 50 random bits.
SYMBOLS: Final = 10
# The length of an epoch, which salts the stretch, and of a request's life: 20 minutes.
EPOCH_SECONDS: Final = 1200

# The stretch: scrypt with n = 2^17, r = 8, p = 1, a 32-byte key. It needs 128 * r * n bytes,
# 128 MiB, past Python's default cap of 32 MiB, so the cap is raised to fit it and a little more.
SCRYPT_N: Final = 2**17
SCRYPT_R: Final = 8
SCRYPT_P: Final = 1
_SCRYPT_MAXMEM: Final = 128 * SCRYPT_R * (SCRYPT_N + SCRYPT_P + 2)
_SALT_PREFIX: Final = "lucille-host/connect/v1/"
_NONCE_INFO: Final = b"lucille-host/connect/nonce"
NONCE_BYTES: Final = delivery.NONCE_BYTES

# The drop room's channel, the TTL of every post there, and how many posts one read asks for.
CHANNEL: Final = "lucille-host/connect"
POST_TTL: Final = 1500
READ_LIMIT: Final = 50
# The host's hello: this object and nothing else.
HELLO: Final = {"t": "lucille-host/hello/1"}

# The chat services Capsule offers, by the name a signed account starts with.
SERVICES: Final = frozenset({"telegram"})


@dataclass(frozen=True)
class Account:
    """One of Lucille's official chat accounts: the handle Capsule shows as it is, and the stable
    id, the ``from`` Lucille signs a delivery from that account with."""

    handle: str
    account: str


# Lucille's official chat accounts, by service, in the order Capsule names them: at most
# :data:`MOST_ACCOUNTS` for each. None is listed until the maintainer confirms them; a service
# with none has the code shown with no account named, and connects as it would with one.
OFFICIAL_ACCOUNTS: Final[Mapping[str, tuple[Account, ...]]] = MappingProxyType({"telegram": ()})
MOST_ACCOUNTS: Final = 4
# An account's handle and its stable id, as a delivery's signed ``handle`` and ``from`` are.
_HANDLE: Final = re.compile(r"@[A-Za-z][A-Za-z0-9_]{0,63}")
_ACCOUNT_ID: Final = re.compile(r"[a-z]{1,16}:[0-9A-Za-z_-]{1,64}")

# Crockford's readings: I and L are one, O is zero.
_READS_AS: Final = str.maketrans({"I": "1", "L": "1", "O": "0"})
# A typed username once normalized: a letter, then up to 63 letters, digits and underscores.
_USERNAME: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")


def check_accounts(table: Mapping[str, Sequence[Account]]) -> None:
    """Refuse a list of official accounts that is not one :data:`OFFICIAL_ACCOUNTS` may hold: a
    service Capsule does not offer, more than :data:`MOST_ACCOUNTS` for one, a handle or an id
    outside its grammar, an id on another service, or an id or a handle listed twice."""
    for service, accounts in table.items():
        if service not in SERVICES:
            raise ValueError(f"official accounts for a service Capsule does not offer: {service!r}")
        if len(accounts) > MOST_ACCOUNTS:
            raise ValueError(f"more than {MOST_ACCOUNTS} official accounts on {service}")
        for entry in accounts:
            if not isinstance(entry, Account):
                raise ValueError("an official account is an Account")
            if _HANDLE.fullmatch(entry.handle) is None:
                raise ValueError(f"an official account's handle is not one: {entry.handle!r}")
            if _ACCOUNT_ID.fullmatch(entry.account) is None or not entry.account.startswith(
                service + ":"
            ):
                raise ValueError(f"an official account's id is not one on {service}")
        ids = [entry.account for entry in accounts]
        handles = [entry.handle.lower() for entry in accounts]
        if len(set(ids)) != len(ids) or len(set(handles)) != len(handles):
            raise ValueError(f"an official account is listed twice on {service}")


check_accounts(OFFICIAL_ACCOUNTS)


def official_accounts(service: str) -> tuple[Account, ...]:
    """Lucille's official accounts on ``service``, in order; none when it lists none."""
    return tuple(OFFICIAL_ACCOUNTS.get(service, ()))


# --- The connect code -------------------------------------------------------------------------


def draw_code() -> str:
    """A new connect code: :data:`SYMBOLS` symbols of :data:`ALPHABET`, from ``secrets``."""
    return "".join(secrets.choice(ALPHABET) for _ in range(SYMBOLS))


def shown(code: str) -> str:
    """A canonical code as the user sees it: two groups of five, ``7KQ4M-X2D9P``."""
    if parse_code(code) != code:
        raise ValueError("not a connect code in its canonical form")
    return f"{code[:5]}-{code[5:]}"


def parse_code(text: str) -> str | None:
    """The canonical code a typed text names, or None if it names none.

    A character outside ASCII is refused first, before any case is changed (``upper`` would turn
    the dotless ``ı`` into ``I``). Then ASCII case, the space (U+0020) and ``-`` are ignored, and
    ``I``, ``L`` and ``O`` read as ``1``, ``1`` and ``0``. Any other character outside the
    alphabet, ``U`` among them, makes the text not a code, and so does any length but ten."""
    if not text.isascii():
        return None
    canonical = text.replace(" ", "").replace("-", "").upper().translate(_READS_AS)
    if len(canonical) != SYMBOLS or any(symbol not in ALPHABET for symbol in canonical):
        return None
    return canonical


# --- The epoch, the key and the nonce -----------------------------------------------------------


def epoch(now: float) -> int:
    """The 20-minute epoch of a moment in Unix seconds: ``now // 1200``."""
    if now < 0:
        raise ValueError("a moment before 1970 has no epoch")
    return int(now // EPOCH_SECONDS)


def stretch(code: str, at_epoch: int) -> bytes:
    """``K``: scrypt of the canonical code's ASCII bytes, salted with
    ``lucille-host/connect/v1/<E>``, n = 2^17, r = 8, p = 1, 32 bytes."""
    if parse_code(code) != code:
        raise ValueError("not a connect code in its canonical form")
    if at_epoch < 0:
        raise ValueError("an epoch is not negative")
    salt = f"{_SALT_PREFIX}{at_epoch}".encode("ascii")
    return hashlib.scrypt(
        code.encode("ascii"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=32,
    )


def _hkdf_sha256(key: bytes, info: bytes, length: int, salt: bytes = b"") -> bytes:
    """HKDF with SHA-256 (RFC 5869). An empty salt is 32 zero bytes, as the RFC says."""
    if not 0 < length <= 255 * 32:
        raise ValueError("HKDF-SHA256 makes 1 to 8160 bytes")
    prk = hmac.new(salt or bytes(32), key, hashlib.sha256).digest()
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def nonce(key: bytes) -> bytes:
    """``N``: HKDF-SHA256 of ``K``, no salt, ``info`` ``lucille-host/connect/nonce``, 16 bytes."""
    if len(key) != 32:
        raise ValueError("K is 32 bytes")
    return _hkdf_sha256(key, _NONCE_INFO, NONCE_BYTES)


# --- The user's own username (decisions/0002 §11.5) ---------------------------------------------


def normalize_username(text: str, service: str) -> str | None:
    """The username a typed text names on ``service``, or None if it names none.

    In this order: refuse any character outside ASCII; drop the spaces (U+0020) around the name;
    drop a leading ``<service>:``, matched without regard to ASCII case, which must name the
    chosen service; drop one leading ``@``; lower ASCII letters only. The result must start with
    a letter and be 1 to 64 of ``a-z``, ``0-9`` and ``_``. So ``telegram:@Ada``, ``@ada`` and
    ``ADA`` are one name, and ``@telegram:ada`` is refused."""
    if service not in SERVICES:
        raise ValueError(f"not a chat service Capsule offers: {service!r}")
    if not text.isascii():
        return None
    name = text.strip(" ")
    if ":" in name:
        prefix, _, name = name.partition(":")
        if prefix.lower() != service:
            return None
    name = name.removeprefix("@").lower()
    return name if _USERNAME.fullmatch(name) else None


def names_user(found: delivery.Delivery, service: str, username: str) -> bool:
    """Whether a delivery's signed account is the user's own: its ``from`` names ``service`` and
    its handle, ASCII case aside, is ``username`` as :func:`normalize_username` gave it. An empty
    handle names no one. The comparison runs in constant time; nothing in it is secret, but
    nothing is lost by it."""
    if service not in SERVICES:
        raise ValueError(f"not a chat service Capsule offers: {service!r}")
    if normalize_username(username, service) != username:
        raise ValueError("not a username as normalize_username gives one")
    signed_service = found.sender.partition(":")[0]
    signed_name = found.handle.removeprefix("@").lower()
    same_service = hmac.compare_digest(signed_service.encode("ascii"), service.encode("ascii"))
    same_name = hmac.compare_digest(signed_name.encode("ascii"), username.encode("ascii"))
    return bool(found.handle) and same_service and same_name


# --- One read of the drop room ------------------------------------------------------------------

# What a read is, once judged.
WAITING: Final = "waiting"
DELIVERED: Final = "delivered"
INTERFERENCE: Final = "interference"
TWO_DELIVERIES: Final = "two_deliveries"

# Why a read that returned is interference. Each names a rule, and nothing from the room.
TOO_MANY: Final = f"the read returned {READ_LIMIT} or more posts"
NOT_A_POST: Final = "the read returned something that is not a post"
NO_HELLO: Final = "the room no longer holds the host's hello"
HELLO_TWICE: Final = "the room holds more than one hello"


@dataclass(frozen=True)
class Read:
    """One read of the drop room, judged.

    ``outcome`` is :data:`WAITING` (no delivery yet; wait on), :data:`DELIVERED` (exactly one
    delivery passed, ``found``), :data:`INTERFERENCE` (``why`` names the rule; the request ends,
    spent, with the interference warning) or :data:`TWO_DELIVERIES` (more than one delivery
    passed, which takes Lucille's key misused; the host refuses them all, and the request is
    spent). ``ignored`` names, in the order the posts came, why each post that is neither the
    hello nor a delivery was ignored, and nothing from it."""

    outcome: str
    found: delivery.Delivery | None = None
    why: str = ""
    ignored: tuple[str, ...] = field(default=())

    @property
    def unknown_key(self) -> bool:
        """Whether a post was ignored only for a key id this release does not pin: if the
        request expires with nothing else, Lucille may need a newer Capsule."""
        return delivery.UNKNOWN_KEY in self.ignored


def _is_hello(body: object) -> bool:
    return isinstance(body, dict) and body == HELLO


def judge(
    rows: Sequence[object],
    request_nonce: bytes,
    *,
    keys: Mapping[str, bytes] = _switchboard.LUCILLE_SIGNING_KEYS,
    revoked: frozenset[str] = _switchboard.LUCILLE_REVOKED_KEY_IDS,
) -> Read:
    """Judge the posts one read of the drop room returned (decisions/0002 §11.4, steps 5 and 7,
    and "The wire"). In this order:

    - :data:`READ_LIMIT` or more posts, or a row that is not a post, is interference;
    - so is a room with no hello, which has lost posts while the request is live, and one with
      more than one hello;
    - every other post's body goes to ``delivery.verify``, with this request's nonce. One that
      passes is a delivery; the same delivery twice counts once; one that is refused is ignored,
      for the reason ``verify`` gives;
    - two deliveries or more are :data:`TWO_DELIVERIES`, one is :data:`DELIVERED`, none is
      :data:`WAITING`.

    ``keys`` and ``revoked`` are the release's own; only the tests name others."""
    if len(request_nonce) != NONCE_BYTES:
        raise ValueError(f"a request's nonce is {NONCE_BYTES} bytes")
    if len(rows) >= READ_LIMIT:
        return Read(INTERFERENCE, why=TOO_MANY)
    if not all(isinstance(row, Mapping) and "body" in row for row in rows):
        return Read(INTERFERENCE, why=NOT_A_POST)
    bodies = [row["body"] for row in rows]  # type: ignore[index]
    hellos = sum(1 for body in bodies if _is_hello(body))
    if hellos == 0:
        return Read(INTERFERENCE, why=NO_HELLO)
    if hellos > 1:
        return Read(INTERFERENCE, why=HELLO_TWICE)
    found: list[delivery.Delivery] = []
    ignored: list[str] = []
    for body in bodies:
        if _is_hello(body):
            continue
        try:
            passed = delivery.verify(body, request_nonce, keys=keys, revoked=revoked)
        except delivery.Refused as refused:
            ignored.append(refused.reason)
            continue
        if passed not in found:
            found.append(passed)
    if len(found) > 1:
        return Read(TWO_DELIVERIES, ignored=tuple(ignored))
    if found:
        return Read(DELIVERED, found=found[0], ignored=tuple(ignored))
    return Read(WAITING, ignored=tuple(ignored))
