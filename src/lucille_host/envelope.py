"""The first checks of a message read in a work room, before ``jobs.py`` decides it.

spec/PROTOCOL.md sections 10 to 15. In the order the host applies them:

- **No reply at all** (:func:`ignored`). A post under the host's own id, a message whose type is not
  ``note`` or that carries a thread, a body that is not a JSON object, one with no
  ``dir: "request"``, and one with no well-formed ``job_id`` are logged and never answered: a
  reply must name a job, and nothing but a request on ``lucille.exec`` is the protocol's.
- **Dedupe** is the journal's (``journal.py``): a ``job_id`` already seen gets nothing.
- **Steps 2 to 4 of the refusal order** (:func:`early_code`): ``host_too_old`` for a
  well-formed ``min_host_version`` this release is older than, checked before the rest of the
  envelope; ``bad_request`` for the envelope (closed keys, types and ranges, the ``not_after``
  range, the form of ``min_host_version``), the request's size (16 KiB, measured as section 13
  says) and its strings (no U+0000, no unpaired surrogate); ``expired`` once the host's clock
  reads later than ``not_after``. Steps 5 to 9 are ``jobs.check``.
- **Time** (:func:`reply_ttl`, :func:`prune_at`): a reply lives ``(not_after - now) + 900`` s,
  at most 86400 s, or ``(received - now) + 900`` s when the request had no valid ``not_after``,
  and is not posted when that is under 1 s; a job id is remembered until 15 minutes after the
  later of ``not_after`` and its receipt, or a day and 15 minutes after its receipt when it had
  no valid ``not_after``.

The patterns are the request schema's own, read from the spec files this package ships and
matched as ECMA-262 matches them (``$`` is the very end). The runtime holds no JSON Schema
library: these checks are held to the golden fixtures by the tests instead.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from typing import Any, Final

from lucille_host import spec

__all__ = [
    "GRACE",
    "NOT_AFTER_RANGE",
    "REQUEST_LIMIT",
    "TTL_MOST",
    "early_code",
    "ignored",
    "older",
    "prune_at",
    "reply_ttl",
    "size",
    "valid_not_after",
]

# A request whose body measures more than this many bytes is bad_request (rule request-size).
REQUEST_LIMIT: Final = 16384
# A not_after more than this many seconds after the request was received is bad_request.
NOT_AFTER_RANGE: Final = 86400
NOT_AFTER_MOST: Final = 4294967295
# The grace Lucille gives a reply after a job's not_after, and the hub's longest TTL.
GRACE: Final = 900
TTL_MOST: Final = 86400
# How long after the later of not_after and receipt a job id is still remembered.
FORGET_AFTER: Final = 900
OP_MOST: Final = 64
CAP_LEAST: Final = 2
CAP_MOST: Final = 2147483647
REQUIRED: Final = frozenset({"v", "dir", "job_id", "op", "args", "not_after"})
OPTIONAL: Final = frozenset({"caps", "min_host_version"})
CAPS: Final = frozenset({"stdout_bytes", "stderr_bytes"})

# Why a message gets no reply, as the audit log names it.
OWN: Final = "own"
NOT_NOTE: Final = "not_note"
THREADED: Final = "threaded"
NOT_OBJECT: Final = "not_object"
NOT_REQUEST: Final = "not_request"
BAD_JOB_ID: Final = "bad_job_id"


def _pattern(name: str) -> re.Pattern[str]:
    # A schema pattern is anchored with ^ and one final $, which ECMA-262 reads as the very end.
    pattern = spec.load_schema("request")["$defs"][name]["pattern"]
    return re.compile(pattern[:-1] + r"\Z")


_JOB_ID: Final = _pattern("job_id")
_OP: Final = _pattern("op_name")
_MIN_HOST_VERSION: Final = _pattern("min_host_version")
_TRIPLE = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)", re.ASCII)


def _integer(value: Any) -> bool:
    """A JSON integer as parsed: an int, never a bool and never a float such as 1.0."""
    return isinstance(value, int) and not isinstance(value, bool)


def _walk(value: Any) -> Iterator[Any]:
    """Every value inside ``value``, object keys included."""
    yield value
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _bad_string(text: str) -> bool:
    # In a parsed string, every surrogate code point is an unpaired one.
    return any(char == "\x00" or 0xD800 <= ord(char) <= 0xDFFF for char in text)


def size(value: Any) -> int:
    """Bytes of ``value`` as JSON with every non-ASCII character escaped and no whitespace."""
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


def ignored(*, mine: bool, kind: str, thread: str | None, body: Any) -> str | None:
    """Why a message read on ``lucille.exec`` gets no reply, or None for a request to decide."""
    if mine:
        return OWN
    if kind != "note":
        return NOT_NOTE
    if thread is not None:
        return THREADED
    if not isinstance(body, dict):
        return NOT_OBJECT
    if body.get("dir") != "request":
        return NOT_REQUEST
    job_id = body.get("job_id")
    if not isinstance(job_id, str) or _JOB_ID.search(job_id) is None:
        return BAD_JOB_ID
    return None


def older(host_version: str, minimum: str) -> bool:
    """Whether a host of ``host_version`` is older than ``minimum``, a well-formed
    ``min_host_version``: by the numeric triple the version starts with, a version with anything
    after that triple counting as older than the same triple, and one that does not start with
    a triple as older than every minimum."""
    match = _TRIPLE.match(host_version)
    if match is None:
        return True
    have = tuple(int(part) for part in match.groups())
    want = tuple(int(part) for part in minimum.split("."))
    return have < want or (have == want and match.end() != len(host_version))


def _too_old(body: dict[str, Any], host_version: str) -> bool:
    wanted = body.get("min_host_version")
    if not isinstance(wanted, str) or _MIN_HOST_VERSION.search(wanted) is None:
        return False
    return older(host_version, wanted)


def valid_not_after(body: Any, received_at: float) -> int | None:
    """The request's ``not_after`` when it is valid: a JSON integer in range, at most a day
    after the host received the request. None otherwise."""
    not_after = body.get("not_after") if isinstance(body, dict) else None
    if not _integer(not_after) or not 0 <= not_after <= NOT_AFTER_MOST:
        return None
    if not_after > received_at + NOT_AFTER_RANGE:
        return None
    return not_after


def _bad_request(body: dict[str, Any], received_at: float) -> bool:
    if size(body) > REQUEST_LIMIT:
        return True
    if any(isinstance(value, str) and _bad_string(value) for value in _walk(body)):
        return True
    keys = set(body)
    if not REQUIRED <= keys or keys - REQUIRED - OPTIONAL:
        return True
    if body["v"] != spec.PROTOCOL:
        return True
    op = body["op"]
    if not isinstance(op, str) or len(op) > OP_MOST or _OP.search(op) is None:
        return True
    if not isinstance(body["args"], dict):
        return True
    if valid_not_after(body, received_at) is None:
        return True
    if "caps" in body:
        caps = body["caps"]
        if not isinstance(caps, dict) or set(caps) != CAPS:
            return True
        if not all(_integer(cap) and CAP_LEAST <= cap <= CAP_MOST for cap in caps.values()):
            return True
    if "min_host_version" in body:
        wanted = body["min_host_version"]
        if not isinstance(wanted, str) or _MIN_HOST_VERSION.search(wanted) is None:
            return True
    return False


def early_code(
    body: dict[str, Any], *, received_at: float, now: float, host_version: str
) -> str | None:
    """Steps 2 to 4 of the refusal order for a request :func:`ignored` let through:
    ``host_too_old``, ``bad_request`` or ``expired``, or None when the request goes on to
    ``jobs.check``. ``received_at`` is when the host read it; ``now`` is when it decides."""
    if _too_old(body, host_version):
        return "host_too_old"
    if _bad_request(body, received_at):
        return "bad_request"
    if now > body["not_after"]:
        return "expired"
    return None


def reply_ttl(basis: float, now: float) -> int | None:
    """The TTL to post a reply with: ``(basis - now) + 900`` s, at most a day, in whole seconds.
    ``basis`` is the request's valid ``not_after``, or the time it was received when it had
    none. None when it is under 1 s: such a reply is not posted."""
    ttl = min(float(TTL_MOST), basis - now + GRACE)
    if ttl < 1:
        return None
    return math.floor(ttl)


def prune_at(not_after: int | None, received_at: float) -> float:
    """When a job id may be forgotten: 15 minutes after the later of its valid ``not_after`` and
    its receipt, or a day and 15 minutes after its receipt when it had no valid ``not_after``."""
    if not_after is None:
        return received_at + NOT_AFTER_RANGE + FORGET_AFTER
    return max(not_after, received_at) + FORGET_AFTER
