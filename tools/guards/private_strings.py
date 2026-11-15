"""Private-string check: no tracked file contains a string from a list this repository never holds.

This repository is public, and some strings must never appear in it. A committed denylist would
publish the very strings it protects, so the list lives in a repository secret and reaches this
check only through an environment variable of the same name, on the one CI step that runs it:

- ``LUCILLE_HOST_PRIVATE_STRINGS_GZ``, the list compressed with gzip and then written in base64,
  so that a list longer than a secret can hold (48 KB) still fits; whitespace in it is ignored, so
  a value wrapped over several lines reads the same;
- else ``LUCILLE_HOST_PRIVATE_STRINGS``, the list as it is.

When the first holds anything, the second is not read: the compressed list replaces it, and is
meant to hold every entry it held. Its value, from a plain list file, is the output of
``gzip -9c list.txt | base64 | tr -d '\n'`` (VERIFYING.md gives the command that sets the
secret). A compressed value that is not base64, not one whole gzip stream with nothing after it,
larger than 8 MiB once unpacked, or not UTF-8 then, fails the check (exit 1), and the message
names only the variable and which of these it is.

The list: one case-sensitive literal substring per line. Leading and trailing whitespace is not
part of an entry; blank lines and lines starting with ``#`` are ignored.

What is checked: every path git tracks under ROOT, a gitlink to another repository included
(outside a git work tree, every file under ROOT, skipping ``.git``, ``.venv``, ``__pycache__`` and
other build caches), and the contents of every such file, whatever bytes it holds:

- each line of its text, read as UTF-8 with any byte that is not UTF-8 replaced, so a line in
  another encoding is still checked wherever its bytes are ASCII, and a NUL byte does not stop it;
- its raw bytes again, for each entry encoded as UTF-16 (little- and big-endian), which the UTF-8
  reading cannot see. Such a hit names no line.

A symbolic link is checked by the path it holds, which is what git stores and publishes for it,
not by the file it points to. A gitlink is checked by its path only: its contents are another
repository's.

What is printed on a hit: the file, the line, and the number of the list entry that matched (its
line in the list, unpacked). Never the entry and never the line: a CI log is public, and printing
either would publish the string. The last line also names the variable the list came from.

With an empty list:

- on a pull request from a fork (``IS_FORK_PR=true``) the check is skipped, and says so, both on
  its own line and as a GitHub Actions ``::warning::`` annotation, so the skip shows on the pull
  request's checks page: GitHub gives fork pull requests no secrets, and a maintainer reviews
  every fork pull request before it merges;
- anywhere else it fails (exit 1): a missing secret must not read as a clean tree.

Exit codes: 0 clean or skipped, 1 a hit or a missing list, 2 a usage error.
"""

from __future__ import annotations

import base64
import binascii
import os
import sys
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make ``tools.guards`` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.guards._common import REPO_ROOT, Violation, repo_files, tracked_paths  # noqa: E402

LIST_VARIABLE = "LUCILLE_HOST_PRIVATE_STRINGS"
GZ_VARIABLE = "LUCILLE_HOST_PRIVATE_STRINGS_GZ"
# The most the compressed list may unpack to. The list is about 58 KB; this only stops a value
# that would unpack without bound.
GZ_LIMIT = 8 * 1024 * 1024
FORK_VARIABLE = "IS_FORK_PR"
NAME = "private_strings"


@dataclass(frozen=True)
class Entry:
    """One list entry and the line of the list it came from (1-based)."""

    number: int
    text: str


def parse_entries(raw: str) -> list[Entry]:
    """The entries of a newline-separated list, skipping blank lines and ``#`` comments."""
    entries: list[Entry] = []
    for number, line in enumerate(raw.split("\n"), start=1):
        text = line.strip()
        if text and not text.startswith("#"):
            entries.append(Entry(number, text))
    return entries


class ListError(Exception):
    """The compressed list cannot be read. The message says why, and holds nothing of the value."""


def unpack(value: str) -> str:
    """The list ``value`` holds as gzip written in base64, whitespace ignored."""
    try:
        data = base64.b64decode("".join(value.split()), validate=True)
    except (binascii.Error, ValueError):
        raise ListError("is not base64") from None
    inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    try:
        raw = inflater.decompress(data, GZ_LIMIT + 1)
    except zlib.error:
        raise ListError("is not gzip") from None
    if len(raw) > GZ_LIMIT:
        raise ListError(f"unpacks to more than {GZ_LIMIT} bytes")
    if not inflater.eof:
        raise ListError("is a gzip stream cut short")
    if inflater.unused_data:
        raise ListError("holds data after its gzip stream")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ListError("is not UTF-8 once unpacked") from None


def list_text(environ: Mapping[str, str]) -> tuple[str, str]:
    """The list, and the variable it came from: the compressed one when it holds anything."""
    packed = environ.get(GZ_VARIABLE, "")
    if packed.strip():
        return unpack(packed), GZ_VARIABLE
    return environ.get(LIST_VARIABLE, ""), LIST_VARIABLE


def tracked_files(root: Path) -> list[str]:
    """The files the check reads: git-tracked files, or every file outside a work tree. Symbolic
    links are included, whatever they point to."""
    return repo_files(root, untracked=False, symlinks=True)


def check(root: Path, entries: Sequence[Entry]) -> list[Violation]:
    """Every place under ``root`` where an entry occurs.

    A violation never holds the entry or the line. When a file's own path holds an entry, the
    path is withheld too, and the file is named by its position in the sorted file list.
    """
    found: list[Violation] = []
    wide = [(entry, _utf16_forms(entry.text)) for entry in entries]
    for index, rel in enumerate(tracked_paths(root), start=1):
        path_hits = [entry for entry in entries if entry.text in rel]
        shown = f"<checked file #{index}, path withheld>" if path_hits else rel
        for entry in path_hits:
            message = f"the file's path matches list entry {entry.number}"
            found.append(Violation(shown, 0, "private-string", message))
        path = root / rel
        if path.is_symlink():
            data = os.fsencode(os.readlink(path))
        elif path.is_file():
            data = path.read_bytes()
        else:
            # A gitlink (another repository) or a path missing from the work tree: its path only.
            continue
        lines = data.decode("utf-8", "replace").split("\n")
        for line_number, line in enumerate(lines, start=1):
            for entry in entries:
                if entry.text in line:
                    message = f"matches list entry {entry.number}"
                    found.append(Violation(shown, line_number, "private-string", message))
        for entry, forms in wide:
            if any(form in data for form in forms):
                message = f"matches list entry {entry.number} in UTF-16"
                found.append(Violation(shown, 0, "private-string", message))
    return sorted(found)


def _utf16_forms(text: str) -> tuple[bytes, ...]:
    """The entry as UTF-16, little- and big-endian, with no byte-order mark."""
    return (text.encode("utf-16-le"), text.encode("utf-16-be"))


def run(root: Path, environ: Mapping[str, str]) -> int:
    """Run the check over ``root`` with the list and fork flag taken from ``environ``."""
    try:
        raw, source = list_text(environ)
    except ListError as exc:
        print(
            f"{NAME}: failed: {GZ_VARIABLE} {exc}. The check fails closed: a list that cannot be "
            "read must not read as a clean tree.",
            file=sys.stderr,
        )
        return 1
    entries = parse_entries(raw)
    if not entries:
        if environ.get(FORK_VARIABLE, "") == "true":
            print(
                f"{NAME}: skipped: this is a pull request from a fork, and GitHub gives fork pull "
                "requests no secrets, so the list is not available. A maintainer reviews every "
                "fork pull request before it merges."
            )
            print(
                f"::warning title={NAME} skipped::The private-string check did not run: fork pull "
                "requests get no secrets. A maintainer must review this pull request for private "
                "strings."
            )
            return 0
        print(
            f"{NAME}: failed: the list is not set or holds no entries ({GZ_VARIABLE}, else "
            f"{LIST_VARIABLE}). The check fails closed: a missing list must not read as a clean "
            "tree.",
            file=sys.stderr,
        )
        return 1
    files = tracked_files(root)
    if not files:
        print(f"{NAME}: nothing to check under {root}; not reporting clean", file=sys.stderr)
        return 2
    violations = check(root, entries)
    for violation in violations:
        print(violation)
    print(
        f"{NAME}: {len(entries)} list entries from {source}, {len(files)} file(s) checked, "
        f"{len(violations)} hit(s)"
    )
    return 1 if violations else 0


def main(argv: list[str] | None = None) -> int:
    """Script entry point: ``python tools/guards/private_strings.py [ROOT]``."""
    usage = (
        f"usage: python tools/guards/{NAME}.py [ROOT]  (list in ${GZ_VARIABLE}, gzip then "
        f"base64, else ${LIST_VARIABLE})"
    )
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ("-h", "--help"):
        print(usage)
        return 0
    if len(args) > 1 or (args and args[0].startswith("-")):
        print(usage, file=sys.stderr)
        return 2
    root = Path(args[0]).resolve() if args else REPO_ROOT
    if not root.is_dir():
        print(f"{NAME}: not a directory: {root}", file=sys.stderr)
        return 2
    return run(root, os.environ)


if __name__ == "__main__":
    raise SystemExit(main())
