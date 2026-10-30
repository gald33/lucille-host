"""The host's own small files, the checks it makes before it starts, and its private runtime folder.

- Each file is JSON, mode 0600, written whole: a temporary file in the same folder, synced, then
  renamed over the old one, and the folder synced. A file that is missing reads as nothing; one
  that cannot be read, or holds something else, stops the host (it fails closed), as an invalid
  policy does.
- ``config/host.json`` says where the pair is kept (``keyring``, the default, or ``file`` under
  ``--store-file``) and which loopback hub, if any, the local configuration names. Only a hub of
  the form ``http://127.0.0.1:<port>`` or ``http://localhost:<port>`` can be named there.
- Before it uses a store or a hub, the host refuses to run as root (uid 0) and refuses to start
  while any ``SWITCHBOARD_*`` variable is set: those configure the Switchboard library, and could
  point it at another hub or another pair.
- Switchboard's client looks for a signing socket at ``$XDG_RUNTIME_DIR/switchboard/<tag>.sock``
  (under the system temporary folder when the variable is unset or empty), and connects to one it
  finds there while it is being built (agent-switchboard 2.6.0, ``client.py:753-755``,
  ``signing.py:238-247`` and ``:392-406``). So before any client is built the host points
  ``XDG_RUNTIME_DIR`` at a private folder of its own, ``<state>/run`` (mode 0700), where no socket
  is; the client it builds checks that the variable still names that folder, and refuses
  otherwise. The variable is set for the whole process, not only while a client is built. The
  host's children never see it: they get a built environment (``_exec.py``). Read from the
  source, not tried on a desktop: keyring 25.7, and secretstorage 3.5 and jeepney 0.9, which it
  uses for the Secret Service on Linux, never read ``XDG_RUNTIME_DIR``; jeepney finds the session
  bus from ``DBUS_SESSION_BUS_ADDRESS`` alone (its ``bus.py:41-43``). Other keyring backends were
  not checked.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from lucille_host import paths

__all__ = [
    "STORE_KINDS",
    "HostConfig",
    "Refused",
    "StateError",
    "Unprepared",
    "check_runtime",
    "load_host_config",
    "prepare_runtime",
    "read_json",
    "remove_file",
    "save_host_config",
    "startup_checks",
    "valid_loopback_hub",
    "write_json",
]

STORE_KINDS: Final = ("keyring", "file")
ENV_PREFIX: Final = "SWITCHBOARD_"
_LOOPBACK_HUB = re.compile(r"http://(?:127\.0\.0\.1|localhost):([1-9][0-9]{0,4})", re.ASCII)
_HOST_KEYS = frozenset({"v", "hub", "store"})


class StateError(RuntimeError):
    """A file of the host's own cannot be read, or holds something else: the host stops."""


class Refused(RuntimeError):
    """The host will not start here, and says why."""


class Unprepared(RuntimeError):
    """A Switchboard client was asked for before the private runtime folder was in place."""


def _sync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(path: Path, value: Any) -> None:
    """Write ``value`` as JSON to ``path``, mode 0600, replacing the file in one step."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    _sync_dir(path.parent)


def remove_file(path: Path) -> bool:
    """Delete ``path`` and sync its folder; whether there was a file to delete."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    _sync_dir(path.parent)
    return True


def read_json(path: Path) -> Any:
    """The JSON in ``path``, or None when there is no file. Anything unreadable is a
    :class:`StateError`."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise StateError(f"{path} cannot be read ({exc})") from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise StateError(f"{path} is not valid JSON ({exc})") from exc


def valid_loopback_hub(url: str) -> bool:
    """Whether ``url`` is ``http://127.0.0.1:<port>`` or ``http://localhost:<port>``, exactly."""
    match = _LOOPBACK_HUB.fullmatch(url)
    return match is not None and int(match.group(1)) <= 65535


@dataclass(frozen=True)
class HostConfig:
    """Where the pair is kept, and the loopback hub the local configuration names, if any."""

    hub: str | None = None
    store: str = "keyring"

    def __post_init__(self) -> None:
        if self.store not in STORE_KINDS:
            raise ValueError(f"a store is one of {', '.join(STORE_KINDS)}")
        if self.hub is not None and not valid_loopback_hub(self.hub):
            raise ValueError(
                "a configured hub is http://127.0.0.1:<port> or http://localhost:<port>"
            )


def load_host_config(dirs: paths.Dirs) -> HostConfig:
    """The profile's ``host.json``; the defaults when there is none."""
    data = read_json(dirs.host_file)
    if data is None:
        return HostConfig()
    if not isinstance(data, dict) or set(data) != _HOST_KEYS or data["v"] != 1:
        raise StateError(f"{dirs.host_file} is not a lucille-host configuration")
    hub, store = data["hub"], data["store"]
    if not (hub is None or isinstance(hub, str)) or not isinstance(store, str):
        raise StateError(f"{dirs.host_file} is not a lucille-host configuration")
    try:
        return HostConfig(hub=hub, store=store)
    except ValueError as exc:
        raise StateError(f"{dirs.host_file}: {exc}") from exc


def save_host_config(dirs: paths.Dirs, config: HostConfig) -> None:
    paths.ensure_dir(dirs.config)
    write_json(dirs.host_file, {"v": 1, "hub": config.hub, "store": config.store})


def startup_checks(environ: Mapping[str, str] | None = None) -> None:
    """Refuse to run as root, or with any ``SWITCHBOARD_*`` variable set (by name, never value)."""
    env = os.environ if environ is None else environ
    if os.geteuid() == 0:
        raise Refused(
            "lucille-host does not run as root (uid 0); run it as the user whose files it may read"
        )
    names = sorted(name for name in env if name.startswith(ENV_PREFIX))
    if names:
        one = len(names) == 1
        raise Refused(
            f"{', '.join(names)} {'is' if one else 'are'} set: lucille-host does not start with "
            "Switchboard settings in its environment, which could point it at another hub or "
            f"pair; unset {'it' if one else 'them'} and start lucille-host again"
        )


def _check_private(path: Path) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise Unprepared(f"the private runtime folder {path} is missing ({exc.strerror})") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise Unprepared(f"{path} is not a private folder (a directory of mode 0700)")


def prepare_runtime(path: Path) -> Path:
    """Create ``path`` (mode 0700) and point ``XDG_RUNTIME_DIR`` at it, for this process."""
    paths.ensure_dir(path)
    _check_private(path)
    os.environ["XDG_RUNTIME_DIR"] = str(path)
    return path


def check_runtime(path: Path) -> None:
    """Raise :class:`Unprepared` unless ``XDG_RUNTIME_DIR`` names ``path`` and it is private."""
    if os.environ.get("XDG_RUNTIME_DIR") != str(path):
        raise Unprepared(
            "no Switchboard client is built before XDG_RUNTIME_DIR names the host's private "
            "runtime folder"
        )
    _check_private(path)
