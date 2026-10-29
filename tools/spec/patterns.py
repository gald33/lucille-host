"""The places in a lucille.exec/1 schema pattern, and changes to one place.

Every pattern in the schemas, except the one that refuses only U+0000, is anchored (``^...$``)
and built from character classes, literal separators (``\\.`` or ``-``) outside the classes,
counted repetitions (``{n}`` or ``{0,n}``) and, in ``min_host_version``, one choice per part,
``(0|[1-9][0-9]{0,8})``. A place is one class, one separator, one counted repetition or one such
part. Each change here widens one place, and every pattern it returns is compiled first, so a
malformed change raises instead of refusing everything.

A value pins a place when the pattern refuses it, the change at that place accepts it, and the
same change at every other place refuses it. ``gen_fixtures`` writes only values that pin their
place, and the tests check it again on the committed files.

Patterns are read as ECMA-262 reads them: ``$`` is the end of the string.
"""

from __future__ import annotations

import re

CLASS = re.compile(r"\[(?!\^)((?:\\.|[^\]])*)\]")
QUANT = re.compile(r"\{(\d+)(?:,(\d+))?\}")
NO_NUL = "^[^\\u0000]*$"
PART = "(0|[1-9][0-9]{0,8})"


class Kind:
    """A kind of character: its name in fixture names, the one character that stands for it, the
    text that adds that character to a class, and every character of the kind.
    """

    def __init__(self, name: str, char: str, class_text: str, phrase: str, chars: str) -> None:
        self.name, self.char, self.class_text = name, char, class_text
        self.phrase, self.chars = phrase, chars


# The kinds of character spec/PROTOCOL.md section 20 names, in its order. A fixture of a kind a
# class refuses holds the one character given here; no other character of the kind is tried.
KINDS = (
    Kind("digit", "0", "0", "a digit", "0123456789"),
    Kind("letter-a-to-f", "a", "a", "a letter from 'a' to 'f'", "abcdef"),
    Kind("letter-past-f", "g", "g", "a lowercase letter past 'f'", "ghijklmnopqrstuvwxyz"),
    Kind("uppercase", "A", "A", "an uppercase letter", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
    Kind("underscore", "_", "_", "'_'", "_"),
    Kind("hyphen", "-", "\\-", "'-'", "-"),
    Kind("dot", ".", ".", "'.'", "."),
    Kind("plus", "+", "+", "'+'", "+"),
    Kind("slash", "/", "/", "'/'", "/"),
    Kind("space", " ", " ", "a space", " "),
    Kind("non-ascii-letter", "\u00e9", "\u00e9", "a non-ASCII letter (U+00E9)", "\u00e9"),
    Kind("non-ascii-digit", "\u0661", "\u0661", "a non-ASCII digit (U+0661)", "\u0661"),
)
KIND = {kind.name: kind for kind in KINDS}
# The kinds put in place of a separator: each of these that the separator is not.
SEPARATOR_KINDS = {
    ".": ("hyphen", "underscore", "slash", "plus", "space"),
    "-": ("dot", "underscore", "slash", "plus", "space"),
}


def _compiled(pattern: str) -> str:
    re.compile(pattern)  # raises re.error: a malformed change is a bug here
    return pattern


def ecma(pattern: str) -> re.Pattern[str]:
    """The pattern as ECMA-262 reads it: ``$`` is the end of the string."""
    if not (pattern.startswith("^") and pattern.endswith("$")):
        raise ValueError(f"pattern {pattern!r} is not anchored with ^ and $")
    return re.compile(pattern[:-1] + r"\Z")


def accepts(pattern: str, value: str) -> bool:
    return ecma(pattern).search(value) is not None


def _outside(spans: list[tuple[int, int]], index: int) -> bool:
    return not any(start <= index < end for start, end in spans)


def classes(pattern: str) -> list[re.Match[str]]:
    """The character classes of the pattern, in order."""
    return list(CLASS.finditer(pattern))


def repetitions(pattern: str) -> list[re.Match[str]]:
    """The counted repetitions of the pattern, in order."""
    return list(QUANT.finditer(pattern))


def separators(pattern: str) -> list[tuple[int, int, str]]:
    """``(start, end, '.' or '-')`` for each literal separator outside the classes."""
    spans = [m.span() for m in CLASS.finditer(pattern)] + [m.span() for m in repetitions(pattern)]
    return [
        (m.start(), m.end(), "." if m.group(0) == "\\." else "-")
        for m in re.finditer(r"\\\.|-", pattern)
        if _outside(spans, m.start())
    ]


def parts(pattern: str) -> list[int]:
    """Where each ``min_host_version`` part, :data:`PART`, starts in the pattern."""
    return [m.start() for m in re.finditer(re.escape(PART), pattern)]


def class_refuses(index: int, pattern: str, kind: str) -> bool:
    """Whether class ``index`` refuses the character that stands for ``kind``."""
    inner = classes(pattern)[index].group(1)
    return re.fullmatch("[" + inner + "]", KIND[kind].char) is None


def widen_class(pattern: str, index: int, kind: str) -> str:
    """Class ``index`` given ``kind``, put right after ``[``, where it cannot form a range."""
    m = classes(pattern)[index]
    return _compiled(pattern[: m.start() + 1] + KIND[kind].class_text + pattern[m.start() + 1 :])


def widen_separator(pattern: str, index: int, kind: str) -> str:
    """Separator ``index`` as a class of itself and ``kind``."""
    start, end, sep = separators(pattern)[index]
    first = "." if sep == "." else "\\-"
    return _compiled(pattern[:start] + "[" + first + KIND[kind].class_text + "]" + pattern[end:])


def optional_separator(pattern: str, index: int) -> str:
    start, end, _ = separators(pattern)[index]
    return _compiled(pattern[:end] + "?" + pattern[end:])


def doubled_separator(pattern: str, index: int) -> str:
    """Separator ``index`` allowed once or twice."""
    start, end, _ = separators(pattern)[index]
    return _compiled(pattern[:start] + "(?:" + pattern[start:end] + "){1,2}" + pattern[end:])


def widen_repetition(pattern: str, index: int, step: int) -> str:
    """Repetition ``index`` with its most raised by one (``step`` 1) or least lowered (-1)."""
    m = repetitions(pattern)[index]
    low, high = int(m.group(1)), int(m.group(2) or m.group(1))
    new = f"{{{low},{high + 1}}}" if step > 0 else f"{{{low - 1},{high}}}"
    return _compiled(pattern[: m.start()] + new + pattern[m.end() :])


def change_part(pattern: str, index: int, new: str) -> str:
    at = parts(pattern)[index]
    return _compiled(pattern[:at] + new + pattern[at + len(PART) :])


def optional_part(pattern: str, index: int) -> str:
    return change_part(pattern, index, PART + "?")


def zeros_part(pattern: str, index: int) -> str:
    """Part ``index`` with its ``0`` allowed to repeat."""
    return change_part(pattern, index, "(0+|[1-9][0-9]{0,8})")


def without_kind(pattern: str, index: int, kind: str) -> str:
    """Class ``index`` without any character of ``kind``; a class left empty never matches.

    Only ASCII is kept: every class in the schemas holds ASCII alone.
    """
    m = classes(pattern)[index]
    whole = re.compile("[" + m.group(1) + "]")
    left = [
        chr(c) for c in range(128) if whole.fullmatch(chr(c)) and chr(c) not in KIND[kind].chars
    ]
    text = "[" + "".join("\\" + c if c in "\\]^-[" else c for c in left) + "]" if left else "(?!)"
    return _compiled(pattern[: m.start()] + text + pattern[m.end() :])


def narrow_repetition(pattern: str, index: int, end: str) -> str:
    """Repetition ``index`` with its least raised by one (``end`` "least") or its most lowered."""
    m = repetitions(pattern)[index]
    low, high = int(m.group(1)), int(m.group(2) or m.group(1))
    new = f"{{{low + 1},{high}}}" if end == "least" else f"{{{low},{high - 1}}}"
    return _compiled(pattern[: m.start()] + new + pattern[m.end() :])


def class_places(pattern: str, value: str, kind: str) -> list[int]:
    """The classes that refuse ``kind`` and, given it, make the pattern accept ``value``."""
    return [
        i
        for i in range(len(classes(pattern)))
        if class_refuses(i, pattern, kind) and accepts(widen_class(pattern, i, kind), value)
    ]


def separator_places(pattern: str, value: str, kind: str) -> list[int]:
    """The separators that, given ``kind``, make the pattern accept ``value``."""
    return [
        i
        for i, (_, _, sep) in enumerate(separators(pattern))
        if kind in SEPARATOR_KINDS[sep] and accepts(widen_separator(pattern, i, kind), value)
    ]


def only_place(pattern: str, value: str, change, count: int, index: int) -> bool:
    """The pattern refuses ``value``, and of the ``count`` places only ``change(index)`` accepts it.

    ``change(i)`` returns the pattern with place ``i`` changed.
    """
    if accepts(pattern, value):
        return False
    return [i for i in range(count) if accepts(change(i), value)] == [index]


def pins_class(pattern: str, value: str, index: int, kind: str) -> bool:
    """Class ``index`` given ``kind`` is the only change of a class or separator that accepts it."""
    if accepts(pattern, value) or class_places(pattern, value, kind) != [index]:
        return False
    return not separator_places(pattern, value, kind)


def pins_separator(pattern: str, value: str, index: int, kind: str) -> bool:
    """Separator ``index`` given ``kind`` is the only such change that accepts it."""
    if accepts(pattern, value) or separator_places(pattern, value, kind) != [index]:
        return False
    return not class_places(pattern, value, kind)
