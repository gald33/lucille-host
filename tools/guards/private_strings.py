"""Private-string check: no tracked file contains a string from a list this repository never holds.

This repository is public, and some strings must never appear in it. A committed denylist would
publish the very strings it protects, so the list lives in the repository secret
``LUCILLE_HOST_PRIVATE_STRINGS`` and reaches this check only through that environment variable,
on the one CI step that runs it.

The list: one case-sensitive literal substring per line. Leading and trailing whitespace is not
part of an entry; blank lines and lines starting with ``#`` are ignored.

What is checked: every file git tracks under ROOT (outside a git work tree, every file under ROOT,
skipping ``.git``, ``.venv``, ``__pycache__`` and other build caches), its path and each line of
its text. Text is read as UTF-8, with any byte that is not UTF-8 replaced, so a line in another
encoding is still checked wherever its bytes are ASCII. A symbolic link is checked by the path it
holds, which is what git stores and publishes for it, not by the file it points to. Files holding
a NUL byte (UTF-16 text among them) are treated as binary and their contents are skipped.

What is printed on a hit: the file, the line, and the number of the list entry that matched.
Never the entry and never the line: a CI log is public, and printing either would publish the
string.

With an empty list:

- on a pull request from a fork (``IS_FORK_PR=true``) the check is skipped, and says so, both on
  its own line and as a GitHub Actions ``::warning::`` annotation, so the skip shows on the pull
  request's checks page: GitHub gives fork pull requests no secrets, and a maintainer reviews
  every fork pull request before it merges;
- anywhere else it fails (exit 1): a missing secret must not read as a clean tree.

Exit codes: 0 clean or skipped, 1 a hit or a missing list, 2 a usage error.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make ``tools.guards`` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.guards._common import REPO_ROOT, Violation, repo_files  # noqa: E402

LIST_VARIABLE = "LUCILLE_HOST_PRIVATE_STRINGS"
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
    for index, rel in enumerate(tracked_files(root), start=1):
        path_hits = [entry for entry in entries if entry.text in rel]
        shown = f"<checked file #{index}, path withheld>" if path_hits else rel
        for entry in path_hits:
            message = f"the file's path matches list entry {entry.number}"
            found.append(Violation(shown, 0, "private-string", message))
        path = root / rel
        data = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
        if b"\0" in data:
            continue
        lines = data.decode("utf-8", "replace").split("\n")
        for line_number, line in enumerate(lines, start=1):
            for entry in entries:
                if entry.text in line:
                    message = f"matches list entry {entry.number}"
                    found.append(Violation(shown, line_number, "private-string", message))
    return sorted(found)


def run(root: Path, environ: Mapping[str, str]) -> int:
    """Run the check over ``root`` with the list and fork flag taken from ``environ``."""
    entries = parse_entries(environ.get(LIST_VARIABLE, ""))
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
            f"{NAME}: failed: {LIST_VARIABLE} is not set or holds no entries. The check fails "
            "closed: a missing list must not read as a clean tree.",
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
        f"{NAME}: {len(entries)} list entries, {len(files)} file(s) checked, "
        f"{len(violations)} hit(s)"
    )
    return 1 if violations else 0


def main(argv: list[str] | None = None) -> int:
    """Script entry point: ``python tools/guards/private_strings.py [ROOT]``."""
    usage = f"usage: python tools/guards/{NAME}.py [ROOT]  (list in ${LIST_VARIABLE})"
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
