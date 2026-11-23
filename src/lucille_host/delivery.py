"""Lucille's signed answer to a Connect: the checks the host makes on a delivery (decisions/0002
§11.5).

When the user sends ``connect <code>`` in Lucille's chat, Lucille starts a pairing and posts its
pairing code into the drop room, signed with its key. :func:`verify` takes the body of one post
that opened under the room's key, and this request's 16-byte nonce ``N``, and either returns the
:class:`Delivery` or raises :class:`Refused`. It checks, in this order:

- the body is a string of at most :data:`MOST_BYTES` bytes in UTF-8, checked before it is parsed;
- it is a JSON object, one level deep, with no key twice (``json.loads`` would keep the last one
  silently), holding exactly the keys ``t``, ``code``, ``kid``, ``from``, ``handle`` and ``sig``,
  each a string;
- ``t`` is :data:`TYPE`, and ``kid``, ``from``, ``handle`` and ``sig`` are each in their form,
  ``sig`` equal to its own re-encoding;
- ``code`` is a pairing code in its one encoding (:func:`_switchboard.code_in_one_encoding`);
- ``kid`` names a key the release pins and does not list as revoked;
- the signature verifies under that key over :func:`signed_bytes`, with this request's ``N``.

A refusal names only which check failed, never anything from the post, so that it can be logged
as it is: the design logs, for an ignored post, only that one was ignored and why. A
:class:`Delivery` keeps the code and the handle out of its ``repr``, since neither may be
logged.

What this module does not do: read the room, tell a delivery from the host's own hello, count
deliveries (two that pass mean the key is misused, and the request is spent), compare the signed
account with the username the user typed, spend ``N`` or redeem the code. Those come with
``pair begin`` (decisions/0002 §11.4).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from lucille_host import _switchboard, nesting

__all__ = [
    "LABEL",
    "MOST_BYTES",
    "NONCE_BYTES",
    "TYPE",
    "Delivery",
    "Refused",
    "signed_bytes",
    "verify",
]

# The value of a delivery's "t".
TYPE: Final = "lucille-host/delivery/1"
# The first bytes of what Lucille signs, so that a signature made for anything else never passes.
LABEL: Final = b"lucille-host/connect-sig/v1"
# The most a delivery may take, in UTF-8.
MOST_BYTES: Final = 4096
# The length of a request's nonce N.
NONCE_BYTES: Final = 16

_KEYS: Final = frozenset({"t", "code", "kid", "from", "handle", "sig"})
_KEY_ID: Final = re.compile(r"[0-9a-f]{16}")
# The signed account, ``<service>:<id>``: the service's stable identifier, never a display name.
_FROM: Final = re.compile(r"[a-z]{1,16}:[0-9A-Za-z_-]{1,64}")
# ``""``, or ``@`` and a letter, then up to 63 letters, digits and underscores.
_HANDLE: Final = re.compile(r"(?:@[A-Za-z][A-Za-z0-9_]{0,63})?")
# 64 bytes in unpadded base64url.
_SIG: Final = re.compile(r"[A-Za-z0-9_-]{86}")

# Why a post is not a delivery. Each names a check, and nothing from the post.
NOT_TEXT: Final = "the body is not a string"
NOT_UTF8: Final = "the body is not UTF-8"
TOO_LARGE: Final = f"the body is larger than {MOST_BYTES} bytes"
TOO_DEEP: Final = "the body nests deeper than one object"
NOT_JSON: Final = "the body is not JSON"
KEY_TWICE: Final = "the body names a key twice"
NOT_OBJECT: Final = "the body is not a JSON object"
WRONG_KEYS: Final = "the body does not hold exactly a delivery's keys"
NOT_STRING: Final = "a value is not a string"
WRONG_TYPE: Final = "the type is not a delivery's"
BAD_KID: Final = "the key id is not 16 lowercase hex digits"
BAD_FROM: Final = "the signed account is not in its form"
BAD_HANDLE: Final = "the handle is not in its form"
BAD_SIG: Final = "the signature is not 64 bytes in unpadded base64url"
BAD_CODE: Final = "the code is not a pairing code in its one encoding"
REVOKED_KEY: Final = "the key id names a revoked key"
UNKNOWN_KEY: Final = "the key id names no key this release pins"
FORGED: Final = "the signature does not verify"


class Refused(Exception):
    """A post that is not a delivery. ``reason`` names the check it failed, and nothing from the
    post. ``unknown_key`` is true when only the key id was unknown to this release, which the app
    reads as Lucille perhaps needing a newer Capsule."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    @property
    def unknown_key(self) -> bool:
        return self.reason == UNKNOWN_KEY


@dataclass(frozen=True)
class Delivery:
    """A delivery that passed every check."""

    # The pairing code, byte for byte as it was posted. Never shown, never logged.
    code: str = field(repr=False)
    # The id of the pinned key that signed it.
    kid: str
    # The signed account, ``<service>:<id>``, the post's "from".
    sender: str
    # The handle as signed, ``""`` or ``@name``. Never logged.
    handle: str = field(repr=False)


def _field(text: str) -> bytes:
    """An ASCII field of the signed bytes: its 2-byte big-endian length, then its bytes."""
    raw = text.encode("ascii")
    return len(raw).to_bytes(2, "big") + raw


def signed_bytes(kid: str, nonce: bytes, code: str, sender: str, handle: str) -> bytes:
    """What Lucille signs for a delivery: :data:`LABEL`, one zero byte, the 8 bytes of the key id,
    the 16 bytes of ``N``, the SHA-256 of the code's UTF-8 bytes, then the signed account and the
    handle, each as a 2-byte big-endian length and its ASCII bytes. Every part before the account
    has a fixed length, and the two after it carry theirs, so the bytes split one way only."""
    if not _KEY_ID.fullmatch(kid):
        raise ValueError("a key id is 16 lowercase hex digits")
    if len(nonce) != NONCE_BYTES:
        raise ValueError(f"a request's nonce is {NONCE_BYTES} bytes")
    return b"".join(
        (
            LABEL,
            b"\x00",
            bytes.fromhex(kid),
            nonce,
            hashlib.sha256(code.encode("utf-8")).digest(),
            _field(sender),
            _field(handle),
        )
    )


def _no_key_twice(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``json.loads``'s object hook: an object that names a key twice is refused."""
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise Refused(KEY_TWICE)
        seen[key] = value
    return seen


# What the parse gave when the body is not JSON.
_NOT_JSON: Final = object()


def _parsed(body: object) -> dict[str, str]:
    """The body as a delivery's object of strings, or :class:`Refused`."""
    # Each refusal is raised outside the ``except`` that saw the error, so that it has no
    # ``__context__``: the codec's and the parser's errors both hold the body.
    if not isinstance(body, str):
        raise Refused(NOT_TEXT)
    size: int | None
    try:
        size = len(body.encode("utf-8"))
    except UnicodeEncodeError:
        size = None
    if size is None:
        raise Refused(NOT_UTF8)
    if size > MOST_BYTES:
        raise Refused(TOO_LARGE)
    if nesting.json_deeper(body, 1):
        raise Refused(TOO_DEEP)
    doc: object
    try:
        doc = json.loads(body, object_pairs_hook=_no_key_twice)
    except ValueError:
        doc = _NOT_JSON
    if doc is _NOT_JSON:
        raise Refused(NOT_JSON)
    if not isinstance(doc, dict):
        raise Refused(NOT_OBJECT)
    if set(doc) != _KEYS:
        raise Refused(WRONG_KEYS)
    if not all(isinstance(value, str) for value in doc.values()):
        raise Refused(NOT_STRING)
    return doc


def _signature(text: str) -> bytes:
    """The 64 signature bytes, from unpadded base64url equal to its own re-encoding."""
    if not _SIG.fullmatch(text):
        raise Refused(BAD_SIG)
    raw = base64.urlsafe_b64decode(text + "==")
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != text:
        raise Refused(BAD_SIG)
    return raw


def verify(
    body: object,
    nonce: bytes,
    *,
    keys: Mapping[str, bytes] = _switchboard.LUCILLE_SIGNING_KEYS,
    revoked: frozenset[str] = _switchboard.LUCILLE_REVOKED_KEY_IDS,
) -> Delivery:
    """Check one opened post's body as a delivery for the request whose nonce is ``nonce``. Return
    the :class:`Delivery`, or raise :class:`Refused` naming the first check it failed. ``keys``
    and ``revoked`` are the release's own; only the tests name others."""
    if len(nonce) != NONCE_BYTES:
        raise ValueError(f"a request's nonce is {NONCE_BYTES} bytes")
    doc = _parsed(body)
    if doc["t"] != TYPE:
        raise Refused(WRONG_TYPE)
    kid, sender, handle, code = doc["kid"], doc["from"], doc["handle"], doc["code"]
    if not _KEY_ID.fullmatch(kid):
        raise Refused(BAD_KID)
    if not _FROM.fullmatch(sender):
        raise Refused(BAD_FROM)
    if not _HANDLE.fullmatch(handle):
        raise Refused(BAD_HANDLE)
    signature = _signature(doc["sig"])
    if not _switchboard.code_in_one_encoding(code):
        raise Refused(BAD_CODE)
    if kid in revoked:
        raise Refused(REVOKED_KEY)
    key = keys.get(kid)
    if key is None:
        raise Refused(UNKNOWN_KEY)
    message = signed_bytes(kid, nonce, code, sender, handle)
    if not _switchboard.signature_valid(key, message, signature):
        raise Refused(FORGED)
    return Delivery(code=code, kid=kid, sender=sender, handle=handle)
