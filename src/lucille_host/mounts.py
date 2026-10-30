"""Linux's table of the mounts this process sees, ``/proc/self/mountinfo``.

Two checks read it: whether a folder holds ``$HOME`` through another mount of the same filesystem
(``policy.holds``), and which devices are ``/proc``, whose per-process names no path may go through
(``containment.py``). Where there is no table to read (macOS, or a Linux without ``/proc``), it is
empty, and each check says what it then does.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Final

__all__ = ["MOUNTINFO", "Mount", "beneath", "table", "unescape"]

MOUNTINFO: Final = "/proc/self/mountinfo"
# A root or mount point in the table writes a space, tab, newline or backslash as \ooo.
_OCTAL: Final = re.compile(rb"\\([0-7]{3})")


@dataclass(frozen=True)
class Mount:
    """One line of the table."""

    # The filesystem's device, as st_dev gives it.
    device: int
    # The folder of that filesystem that is mounted ("/" for all of it).
    root: str
    # Where it is mounted.
    point: str
    # The filesystem's type: "ext4", "btrfs", "proc".
    fstype: str


def unescape(field: bytes) -> str:
    """A root or mount point from the table, as a path."""
    return os.fsdecode(_OCTAL.sub(lambda match: bytes([int(match.group(1), 8)]), field))


def table() -> list[Mount]:
    """The mounts this process sees, in the table's order (a later mount at one place hides an
    earlier one). Empty where there is no table to read; a line it cannot read is left out."""
    try:
        with open(MOUNTINFO, "rb") as handle:
            # Only "\n" ends a line: the kernel escapes it in a path, but not "\r", which
            # splitlines() would also split on.
            lines = handle.read().split(b"\n")
    except OSError:
        return []
    mounts = []
    for line in lines:
        fields = line.split(b" ")
        try:
            # Optional fields come between the sixth field and a lone "-".
            separator = fields.index(b"-", 6)
            major, minor = (int(number) for number in fields[2].split(b":"))
            mounts.append(
                Mount(
                    device=os.makedev(major, minor),
                    root=unescape(fields[3]),
                    point=unescape(fields[4]),
                    fstype=fields[separator + 1].decode("ascii", "replace"),
                )
            )
        except (ValueError, IndexError):
            continue
    return mounts


def beneath(path: str, folder: str) -> str | None:
    """``path`` relative to ``folder`` ("" for the folder itself), or None if it is not below it.
    Both are absolute and normalised."""
    if folder == "/":
        return path.lstrip("/")
    if path == folder:
        return ""
    if path.startswith(folder + "/"):
        return path[len(folder) + 1 :]
    return None
