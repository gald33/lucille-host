"""How deep a JSON or TOML text nests, found from the text itself before any parser reads it.

Python's parsers follow nesting as far as their own limits go, and those differ by version and by
patch level: ``json`` stops at about 1000 arrays on 3.11 and about 10000 on 3.13, and goes on much
further on 3.14; ``tomllib`` stops at about 500 arrays on each, and some patch levels refuse a key
of more than 1000 dotted parts while earlier ones read it. So the host sets its own limit,
:data:`FILES_MOST`, and a file of its own that nests deeper is one it does not read, whatever
version runs it.

The JSON check counts the arrays and objects a text opens, outside its strings, and stops at the
first that goes past the limit. It reads strings as the parser does, so on the part of a text the
parser accepts the count is the parser's own depth; past the first thing the parser refuses the
count no longer matters, since the parser goes no deeper.

The TOML check counts the tables and arrays the parser makes, outside strings and comments: those a
table header names (each dotted part one, and one more for an array of tables), those a key's
dotted parts make below the table it is in, and each array and inline table. Once a document has
an array of tables, each part of a later header is counted twice, as any of them may name the last
table of such an array; the count is then larger than the parser's depth, never smaller.
"""

from __future__ import annotations

import re
from typing import Final

# The most levels of arrays and objects a file of the host's own may nest. The journal keeps a
# request's body a few levels down, and a body may nest 64 (``_switchboard.NEST_MOST``); no other
# file nests more than a few. Python's parsers on 3.11, the shallowest, follow several times this.
FILES_MOST: Final = 128

# A string, whole or cut off at the end of the text, or one bracket.
_JSON: Final = re.compile(r'"(?:[^"\\]+|\\.)*"?|[\[\]{}]', re.DOTALL)

# Multi-line strings first, each with the up to two quotes it may end with; then one-line strings,
# which end at a line's end; then comments; then one character that matters to the count.
_TOML: Final = re.compile(
    r'"""(?:[^\\]|\\.)*?(?:"""|\Z)"{0,2}'
    r"|'''.*?(?:'''|\Z)'{0,2}"
    r'|"(?:[^"\\\n]+|\\.)*"?'
    r"|'[^'\n]*'?"
    r"|#[^\n]*"
    r"|[\[\]{}.=,\n]",
    re.DOTALL,
)


def _deeper(pattern: re.Pattern[str], text: str, most: int) -> bool:
    depth = 0
    for match in pattern.finditer(text):
        token = match.group()
        if token in ("[", "{"):
            depth += 1
            if depth > most:
                return True
        elif token in ("]", "}"):
            depth -= 1
    return False


def json_deeper(text: str, most: int = FILES_MOST) -> bool:
    """Whether the JSON ``text`` opens more than ``most`` arrays and objects one inside another."""
    return _deeper(_JSON, text, most)


def toml_deeper(text: str, most: int = FILES_MOST) -> bool:
    """Whether the TOML ``text`` makes tables and arrays more than ``most`` deep, the root table
    not counted: a header's parts, a key's dotted parts, arrays and inline tables."""
    # Each open array ("a"), inline table ("t") or header ("h"), with the depth of what it makes.
    frames: list[tuple[str, int]] = []
    header = 0  # the depth of the table the last header named
    arrays_of_tables = False  # whether a header has named an array of tables
    many = False  # whether the header being read names an array of tables
    parts = 1  # the parts of the key or header being read
    key = True  # whether a key may be read here
    value: int | None = None  # the depth a container would have here, after a key's "="
    skip = -1  # where the second bracket of "[[" or "]]" is
    for match in _TOML.finditer(text):
        token, at = match.group(), match.start()
        if at == skip:
            continue
        top = frames[-1] if frames else None
        if token == "\n":
            if top is None:
                key, parts, value = True, 1, None
        elif token == ".":
            if key or (top is not None and top[0] == "h"):
                parts += 1
        elif token == "=":
            if key and (top is None or top[0] == "t"):
                base = header if top is None else top[1]
                if base + parts - 1 > most:
                    return True
                key, value = False, base + parts
        elif token == ",":
            if top is not None and top[0] == "t":
                key, parts, value = True, 1, None
        elif token == "[" and top is None and key:
            many = text.startswith("[", match.end())
            if many:
                skip = match.end()
            frames.append(("h", 0))
            parts = 1
        elif token in "[{":
            if top is not None and top[0] == "a":
                depth = top[1] + 1
            elif value is not None:
                depth = value
            else:
                depth = (header if top is None else top[1]) + 1
            if depth > most:
                return True
            frames.append(("a" if token == "[" else "t", depth))
            key, parts, value = token == "{", 1, None
        elif token in "]}" and frames:
            kind, _ = frames.pop()
            if kind == "h":
                arrays_of_tables = arrays_of_tables or many
                header = 2 * parts if arrays_of_tables else parts
                if header > most:
                    return True
                if many and text.startswith("]", match.end()):
                    skip = match.end()
            key, value = False, None
    return False
