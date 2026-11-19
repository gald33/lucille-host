"""How deep a JSON or TOML text nests, found from the text itself before any parser reads it.

Python's parsers follow nesting as far as their own limits go, and those differ by version: on 3.11
``tomllib`` stops at about 500 arrays and ``json`` at about 1000, on 3.13 ``json`` at about 10000,
and on 3.14 both go on much further. So the host sets its own limit, :data:`FILES_MOST`, and a file
of its own nested deeper is one it does not read, whatever version runs it.

Each check counts the arrays and objects (or inline tables) a text opens, outside its strings (and,
for TOML, its comments), and stops at the first that goes past the limit. It reads strings as the
parser does, so on the part of a text the parser accepts the count is the parser's own depth; past
the first thing the parser refuses the count no longer matters, since the parser goes no deeper.
TOML's table headers count too (``[[a]]`` is two), which only makes the count larger.
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
# which end at a line's end; then comments; then one bracket.
_TOML: Final = re.compile(
    r'"""(?:[^\\]|\\.)*?(?:"""|\Z)"{0,2}'
    r"|'''.*?(?:'''|\Z)'{0,2}"
    r'|"(?:[^"\\\n]+|\\.)*"?'
    r"|'[^'\n]*'?"
    r"|#[^\n]*"
    r"|[\[\]{}]",
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
    """Whether the TOML ``text`` opens more than ``most`` arrays, inline tables and table headers
    one inside another."""
    return _deeper(_TOML, text, most)
