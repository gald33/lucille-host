"""What the host does to a child's output before it goes in a reply (spec/PROTOCOL.md section 13).

For each stream, in this order: decode as UTF-8 with ``errors="replace"``, remove every U+0000,
redact, then cap last. Capping drops whole characters from the end until the stream's JSON-encoded
string fits the cap, and sets the stream's truncation flag. Redaction sets it too when an
unterminated ``BEGIN ... PRIVATE KEY`` block took the rest of the stream: the end of the output is
gone from the reply either way.

The host keeps at most :data:`RAW_CEILING` bytes of each stream's raw output and reads and drops the
rest, so a child never blocks on a full pipe and memory stays bounded. Dropping that rest counts as
capping (the spec's rule ``raw-ceiling``), so it sets the flag too.

Redaction is best effort, a convenience and not a guarantee: it replaces PEM private-key blocks and
some well-known key formats. An unterminated ``BEGIN ... PRIVATE KEY`` block counts as running to
the end of the stream, and sets its truncation flag. Nothing may claim that secrets are removed
from output.
"""

from __future__ import annotations

import json
import re
from typing import Final

__all__ = [
    "RAW_CEILING",
    "STREAM_CAP",
    "cap",
    "decode",
    "json_size",
    "has_key_block",
    "key_block_end",
    "redact",
    "redact_to_end",
    "render",
]

# The host's own cap on each stream: its JSON-encoded string, quotes included (rule stream-size).
STREAM_CAP: Final = 65536
# How much of each stream's raw output the host keeps (rule raw-ceiling).
RAW_CEILING: Final = 4 * 1024 * 1024

PRIVATE_KEY_MARK: Final = "[redacted private key]"
TOKEN_MARK: Final = "[redacted]"

_PEM_BEGIN = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----", re.ASCII)
_PEM_END = re.compile(r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----", re.ASCII)
# Well-known key formats, each matched only where it does not continue a longer word.
_TOKENS = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|gh[pousr]_[A-Za-z0-9]{36,255}"
    r"|github_pat_[A-Za-z0-9_]{22,255}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|xox[abposr]-[A-Za-z0-9-]{10,}"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|pypi-[A-Za-z0-9_-]{50,}"
    r")",
    re.ASCII,
)

# The JSON escapes of the control characters that have a short form.
_SHORT_ESCAPES = frozenset("\b\f\n\r\t")


def decode(raw: bytes) -> str:
    """Decode a stream as UTF-8, replacing each invalid sequence with U+FFFD, and remove U+0000."""
    return raw.decode("utf-8", errors="replace").replace("\x00", "")


def has_key_block(text: str) -> bool:
    """Whether a private-key block begins in ``text``, as the redaction pass reads one."""
    return _PEM_BEGIN.search(text) is not None


def key_block_end(text: str) -> int | None:
    """Where the first end of a private-key block in ``text`` ends; None when there is none."""
    found = _PEM_END.search(text)
    return None if found is None else found.end()


def redact(text: str) -> str:
    """The best-effort redaction pass."""
    return redact_to_end(text)[0]


def redact_to_end(text: str) -> tuple[str, bool]:
    """The best-effort redaction pass, and whether an unterminated private-key block took the rest
    of ``text``."""
    out = []
    to_end = False
    pos = 0
    while True:
        begin = _PEM_BEGIN.search(text, pos)
        if begin is None:
            out.append(text[pos:])
            break
        out.append(text[pos : begin.start()])
        out.append(PRIVATE_KEY_MARK)
        end = _PEM_END.search(text, begin.end())
        if end is None:
            # Unterminated: the block runs to the end of the stream.
            pos = len(text)
            to_end = True
            break
        pos = end.end()
    return _TOKENS.sub(TOKEN_MARK, "".join(out)), to_end


def json_size(text: str) -> int:
    """The byte length of ``text`` as a JSON string, quotes included, as the spec measures it."""
    return len(json.dumps(text, ensure_ascii=True))


def _char_size(char: str) -> int:
    code = ord(char)
    if char == '"' or char == "\\" or char in _SHORT_ESCAPES:
        return 2
    if 0x20 <= code < 0x7F:
        return 1
    if code < 0x10000:
        return 6
    return 12


def cap(text: str, limit: int) -> tuple[str, bool]:
    """Drop whole characters from the end of ``text`` until its JSON string measures at most
    ``limit`` bytes. Returns the text and whether anything was dropped."""
    if limit < 2:
        raise ValueError("a cap is at least 2: the empty string measures 2 bytes")
    budget = limit - 2
    used = 0
    for index, char in enumerate(text):
        used += _char_size(char)
        if used > budget:
            return text[:index], True
    return text, False


def render(raw: bytes, limit: int, *, dropped: bool = False) -> tuple[str, bool]:
    """One stream of a child's output, ready for a reply: decoded, redacted and capped.

    ``dropped`` says the host stopped keeping the stream before its end (rule ``raw-ceiling``);
    the truncation flag is then set whatever the cap leaves. It is set too when an unterminated
    private-key block took the rest of the stream.
    """
    redacted, to_end = redact_to_end(decode(raw))
    text, truncated = cap(redacted, limit)
    return text, truncated or dropped or to_end
