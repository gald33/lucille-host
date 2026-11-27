"""What the host makes of ripgrep's ``--json`` output: ``files.search``'s document
(spec/PROTOCOL.md section 11, ``files.search.schema.json``).

ripgrep writes one JSON message a line. The host reads them after the child has ended, from the
bytes it kept of standard output (at most ``_output.RAW_CEILING``), and keeps only the ``match``
messages: each one's path, line number and line. ``begin``, ``end``, ``context`` and ``summary``
are read and dropped. ripgrep gives a path or a line as ``{"text": ...}`` when it is valid UTF-8
and as ``{"bytes": <base64>}`` when it is not; the document does the same, so a name or a line
that is not UTF-8 comes back exactly, and a newline in a name is a character of a JSON string, not
the end of a line of output. A text that holds U+0000 goes as bytes, since no reply string may
hold it.

Each message is checked before anything is taken from it: a line of ripgrep's that is not UTF-8,
nests deeper than :data:`DEPTH_MOST`, is not JSON, repeats a key, is not one of the five kinds,
or is a ``match`` whose path, line or line number is not as ripgrep writes them (a path that is
absolute or holds a ``..`` part included) makes the whole document the one with no matches and
``error`` ``unreadable_output``. Nothing of such output goes in the reply. So does a last line with
no newline, unless the host cut the output short (it kept no more, or the child did not exit):
that line is then dropped, and the document says it is not whole.

Bounds: at most :data:`MATCHES_MOST` matches; each line cut at :data:`LINE_MOST` bytes (ripgrep's
``--max-columns`` does nothing under ``--json``), at a character's end when it is text, with
``line_truncated``; a match whose path passes :data:`PATH_MOST` bytes is left out; and the
document is fit to the stream's cap (rule ``stream-size``), measured as its JSON-encoded string,
by leaving out the last matches. ``truncated`` is true when not every match ripgrep wrote, or would
have written, is in the document.

Redaction (spec/PROTOCOL.md section 13) runs on each path and each line, after the cut, on its
bytes read as Latin-1, so that it works the same on bytes that are not UTF-8: every pattern it
looks for is ASCII. A private-key block a path or a line opens and does not end runs on into
whatever ripgrep wrote after it, as it would over the raw text: the match is kept with its line
redacted (left out, when it was the path that opened it), no later match is kept, and
``truncated`` is true.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from typing import Any, Final

from lucille_host import _output, nesting

__all__ = [
    "DEPTH_MOST",
    "LINE_MOST",
    "LINE_NUMBER_MOST",
    "MATCHES_MOST",
    "PATH_MOST",
    "UNREADABLE",
    "document",
]

# The most matches in one document.
MATCHES_MOST: Final = 1000
# The most bytes of a line the document keeps, as --max-columns 1000 kept before.
LINE_MOST: Final = 1000
# The most bytes of a path; a match whose path is longer is left out.
PATH_MOST: Final = 4096
# The largest line number the document holds: the largest integer every JSON reader holds exactly.
LINE_NUMBER_MOST: Final = 2**53 - 1
# How deep a message of ripgrep's may nest: a match nests five deep (its submatches' text).
DEPTH_MOST: Final = 8
# The error the document names when ripgrep's output is not what the host reads.
UNREADABLE: Final = "unreadable_output"

_KINDS: Final = frozenset({"begin", "end", "context", "match", "summary"})


class _Unreadable(Exception):
    """A line of ripgrep's output that is not one of its messages, as the host reads them."""


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found = dict(pairs)
    if len(found) != len(pairs):
        raise _Unreadable
    return found


def _no_constant(_: str) -> Any:
    raise _Unreadable  # NaN and Infinity, which JSON does not have


def _message(line: bytes) -> tuple[str, Any]:
    """One line's message: its kind and its data."""
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        raise _Unreadable from None
    if nesting.json_deeper(text, DEPTH_MOST):
        raise _Unreadable
    try:
        message = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except ValueError:
        raise _Unreadable from None
    if not isinstance(message, dict) or set(message) != {"type", "data"}:
        raise _Unreadable
    kind, data = message["type"], message["data"]
    if kind not in _KINDS or not isinstance(data, dict):
        raise _Unreadable
    return kind, data


def _item(value: Any) -> tuple[bytes, bool]:
    """A path's or a line's bytes, and whether ripgrep gave them as text."""
    if not isinstance(value, dict) or len(value) != 1:
        raise _Unreadable
    if "text" in value and isinstance(value["text"], str):
        try:
            return value["text"].encode("utf-8"), True
        except UnicodeEncodeError:  # a surrogate, which ripgrep never writes
            raise _Unreadable from None
    if "bytes" in value and isinstance(value["bytes"], str):
        try:
            return base64.b64decode(value["bytes"], validate=True), False
        except ValueError:
            raise _Unreadable from None
    raise _Unreadable


def _match(data: dict[str, Any]) -> tuple[bytes, bool, int, bytes, bool]:
    """A match's path, whether it is text, its line number, its line and whether that is text."""
    if not {"path", "lines", "line_number"} <= set(data):
        raise _Unreadable
    path, path_text = _item(data["path"])
    line, line_text = _item(data["lines"])
    number = data["line_number"]
    if type(number) is not int or not 1 <= number <= LINE_NUMBER_MOST:
        raise _Unreadable
    # ripgrep runs in the folder with no path, so every path it writes is relative to it.
    if not path or path.startswith(b"/") or b".." in path.split(b"/"):
        raise _Unreadable
    return path, path_text, number, line, line_text


def _redact(raw: bytes) -> tuple[bytes, bool]:
    """The redaction pass over ``raw`` read as Latin-1, and whether a key block ran to its end."""
    text, to_end = _output.redact_to_end(raw.decode("latin-1"))
    return text.encode("latin-1"), to_end


def _cut(raw: bytes, is_text: bool) -> tuple[bytes, bool]:
    """``raw`` cut at :data:`LINE_MOST` bytes, at a character's end when it is text."""
    if len(raw) <= LINE_MOST:
        return raw, False
    kept = raw[:LINE_MOST]
    if is_text:
        kept = kept.decode("utf-8", errors="ignore").encode("utf-8")
    return kept, True


def _value(raw: bytes, is_text: bool) -> dict[str, str]:
    """``{"text": ...}``, or ``{"bytes": <base64>}`` for bytes that are not UTF-8 or hold NUL."""
    if is_text and b"\x00" not in raw:
        try:
            return {"text": raw.decode("utf-8")}
        except UnicodeDecodeError:
            pass
    return {"bytes": base64.b64encode(raw).decode("ascii")}


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _stream_size(text: str) -> int:
    """The size of ``text``, which is ASCII, as a reply's JSON-encoded stream (rule stream-size):
    its quotes, and two bytes for each quote and backslash in it."""
    return 2 + len(text) + text.count('"') + text.count("\\")


def _head() -> str:
    return '{"matches":['


def _tail(truncated: bool, error: str | None) -> str:
    return f'],"truncated":{_compact(truncated)},"error":{_compact(error)}}}'


def _fit(fragments: Iterable[str], cap: int, truncated: bool) -> tuple[str, bool]:
    """The document holding as many of ``fragments`` as fit ``cap``, and its ``truncated``."""
    # "false" is the longer of the two values, so a document measured with it fits with either.
    used = _stream_size(_head() + _tail(False, None))
    if used > cap:
        return "", True
    kept: list[str] = []
    for fragment in fragments:
        more = _stream_size(fragment) - 2 + (1 if kept else 0)
        if used + more > cap:
            truncated = True
            break
        kept.append(fragment)
        used += more
    return _head() + ",".join(kept) + _tail(truncated, None), truncated


def _unreadable(cap: int) -> tuple[str, bool]:
    found = _head() + _tail(True, UNREADABLE)
    return (found if _stream_size(found) <= cap else ""), True


def document(raw: bytes, cap: int, *, cut: bool) -> tuple[str, bool]:
    """``files.search``'s document from the bytes ripgrep wrote on standard output, as the
    reply's ``stdout``, and the stream's truncation flag, which is the document's ``truncated``.

    ``cut`` says the host has less than all ripgrep would have written: it kept no more
    (rule ``raw-ceiling``), or ripgrep did not exit on its own. ``cap`` is the stream's cap; when
    not even a document with no match fits it, ``stdout`` is empty and the flag is true.
    """
    lines = raw.split(b"\n")
    last = lines.pop()  # empty when the output ends with a newline
    truncated = cut
    if last and not cut:
        return _unreadable(cap)
    fragments: list[str] = []
    try:
        for line in lines:
            kind, data = _message(line)
            if kind != "match":
                continue
            if len(fragments) == MATCHES_MOST:
                truncated = True
                break
            path, path_text, number, text, text_is_text = _match(data)
            if len(path) > PATH_MOST:
                truncated = True
                continue
            path, path_ends = _redact(path)
            if path_ends:
                truncated = True
                break
            text, line_cut = _cut(text, text_is_text)
            text, text_ends = _redact(text)
            match = {
                "path": _value(path, path_text),
                "line_number": number,
                "lines": _value(text, text_is_text),
                "line_truncated": line_cut,
            }
            fragments.append(_compact(match))
            if text_ends:
                truncated = True
                break
    except _Unreadable:
        return _unreadable(cap)
    return _fit(fragments, cap, truncated)
