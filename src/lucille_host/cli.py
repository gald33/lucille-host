"""The ``lucille-host`` command.

In this release the command has no verbs: with no arguments, or with ``--version``, it prints
``lucille-host <version>`` and exits 0.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from lucille_host import __version__


def version_line() -> str:
    """The line ``lucille-host`` and ``lucille-host --version`` print."""
    return f"lucille-host {__version__}"


def build_parser() -> argparse.ArgumentParser:
    """The argument parser for the ``lucille-host`` command."""
    parser = argparse.ArgumentParser(
        prog="lucille-host",
        description=(
            "Will run a closed set of named operations for a paired Lucille account. "
            "This release is a skeleton: it has no verbs, pairs with nothing and runs nothing."
        ),
    )
    parser.add_argument("--version", action="version", version=version_line())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``lucille-host`` console script and ``python -m lucille_host``."""
    build_parser().parse_args(argv)
    print(version_line())
    return 0
