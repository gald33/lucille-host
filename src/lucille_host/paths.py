"""Where the host keeps its files: one config directory and one state directory per profile.

- Linux: ``$XDG_CONFIG_HOME/lucille-host/<profile>`` (default ``~/.config``) and
  ``$XDG_STATE_HOME/lucille-host/<profile>`` (default ``~/.local/state``). An XDG variable that is
  not an absolute path is ignored, as the XDG spec says.
- macOS: ``~/Library/Application Support/lucille-host/<profile>``, with the state in its ``state``
  subdirectory.

The folders above those, one per kind of directory, hold every profile's files: :attr:`Dirs.own`.

The profile is ``default`` unless ``--profile <name>`` names another, so that two computers can be
simulated on one machine. Every directory the host creates is mode 0700.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "DEFAULT_PROFILE",
    "Dirs",
    "UnsupportedPlatform",
    "dirs",
    "ensure_dir",
    "os_family",
    "valid_profile",
]

APP: Final = "lucille-host"
DEFAULT_PROFILE: Final = "default"
_PROFILE = re.compile(r"[a-z][a-z0-9_-]{0,31}", re.ASCII)


class UnsupportedPlatform(RuntimeError):
    """The host runs on Linux and macOS only."""


def os_family(platform: str | None = None) -> str:
    """``linux`` or ``macos``, by ``sys.platform``; anything else is refused."""
    name = sys.platform if platform is None else platform
    if name.startswith("linux"):
        return "linux"
    if name == "darwin":
        return "macos"
    raise UnsupportedPlatform(f"lucille-host runs on Linux and macOS, not {name}")


def valid_profile(name: str) -> bool:
    return _PROFILE.fullmatch(name) is not None


@dataclass(frozen=True)
class Dirs:
    config: Path
    state: Path

    @property
    def policy_file(self) -> Path:
        return self.config / "policy.toml"

    @property
    def own(self) -> tuple[Path, ...]:
        """The folders holding the host's own files for every profile: the ``lucille-host``
        folder above the config directory and, unless it is inside that one (as on macOS), the
        one above the state directory. No operation may reach inside them (``containment.py``)."""
        folders = [self.config.parent]
        if not self.state.is_relative_to(self.config.parent):
            folders.append(self.state.parent)
        return tuple(folders)


def _home(environ: Mapping[str, str]) -> Path:
    home = environ.get("HOME")
    return Path(home) if home and os.path.isabs(home) else Path.home()


def _xdg(environ: Mapping[str, str], name: str, default: Path) -> Path:
    value = environ.get(name)
    return Path(value) if value and os.path.isabs(value) else default


def dirs(
    profile: str = DEFAULT_PROFILE,
    *,
    family: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Dirs:
    """The directories of one profile. Nothing is created."""
    if not valid_profile(profile):
        raise ValueError(f"a profile name matches {_PROFILE.pattern}")
    env = os.environ if environ is None else environ
    family = os_family() if family is None else family
    home = _home(env)
    if family == "macos":
        base = home / "Library" / "Application Support" / APP / profile
        return Dirs(config=base, state=base / "state")
    config = _xdg(env, "XDG_CONFIG_HOME", home / ".config") / APP / profile
    state = _xdg(env, "XDG_STATE_HOME", home / ".local" / "state") / APP / profile
    return Dirs(config=config, state=state)


def ensure_dir(path: Path) -> Path:
    """Create ``path`` and each missing parent, every one of them mode 0700, and make ``path``
    itself 0700. A parent that already exists keeps its mode."""
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        os.chmod(directory, 0o700)
    os.chmod(path, 0o700)
    return path
