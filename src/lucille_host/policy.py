"""The local policy: which catalog operations are enabled, and which folders they may work in.

One TOML file per profile, ``policy.toml`` in the config directory, read with ``tomllib``. Only the
local CLI verbs change it (``ops enable|disable``, ``roots add|remove``, and ``pair``, below); no
protocol message has a field that maps onto it. The file looks like this::

    version = 1
    log_results = false

    [ops."files.sha256"]
    definition = "<the entry's definition hash>"

    [roots."docs"]
    path = "/home/me/Documents"
    broad = false

- An operation is enabled when it has a table under ``ops``. The table stores the hash of the
  catalog definition it was enabled with. If an upgrade changes that definition, the operation is
  disabled until the user enables it again; the file is not rewritten, so the user sees why.
- A root maps a folder alias to an absolute path. A root that is ``$HOME``, holds it (``/home`` or
  ``/Users``, say, or macOS's ``/System/Volumes/Data``), or is ``/`` is refused unless it was added
  with the explicit flag, stored as ``broad = true``. :func:`holds` says which spellings of "holds
  it" are seen, and which one is not.
- ``log_results = true`` keeps output bodies in the audit log.
- Anything else makes the file invalid, and an invalid file stops the host: it fails closed.
- With no file at all the policy is the fresh one: only ``host.describe`` is enabled, and no folder
  is held.
- A new pair starts from the fresh policy. When ``lucille-host pair`` takes a new code, it moves the
  policy file to ``policy.previous.toml`` beside it, unless that policy is the fresh one, and writes
  the fresh policy in its place (:func:`archive`). Nothing reads the earlier policy but
  ``lucille-host policy previous``, which lists it; to use any of it again, the user enables it
  again. Because a fresh policy is never moved, trying a pairing twice keeps the earlier policy the
  first try found.

The policy digest is HMAC-SHA256 of the canonical effective policy under a random key kept in the
state directory, which never leaves the computer. The user can match a result to a policy, and
Lucille can tell when the policy changed. A plain hash would let anyone holding it confirm a
guessed folder path.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from lucille_host import mounts
from lucille_host.ops import OPERATIONS

__all__ = [
    "MAX_ROOTS",
    "Effective",
    "Policy",
    "PolicyError",
    "Root",
    "archive",
    "digest",
    "digest_key",
    "effective",
    "fresh",
    "holds",
    "is_broad",
    "load",
    "save",
    "valid_alias",
]

VERSION: Final = 1
MAX_ROOTS: Final = 64
KEY_FILE: Final = "policy-digest.key"
_KEY_BYTES: Final = 32
_ALIAS = re.compile(r"[a-z][a-z0-9_-]{0,31}", re.ASCII)
_OP_NAME = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", re.ASCII)
_HEX64 = re.compile(r"[0-9a-f]{64}", re.ASCII)
# Where macOS mounts the volume holding the users' files. ``/Users`` is a firmlink into it, so it
# holds every home folder without being a folder above one by name. Elsewhere it does not exist.
_DATA_VOLUME: Final = "/System/Volumes/Data"


class PolicyError(Exception):
    """The policy file is invalid, or a change to it was refused."""


@dataclass(frozen=True)
class Root:
    path: str
    broad: bool = False


@dataclass(frozen=True)
class Policy:
    """The policy as the file states it."""

    # Operation name -> the definition hash it was enabled with.
    enabled: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    roots: Mapping[str, Root] = field(default_factory=lambda: MappingProxyType({}))
    log_results: bool = False


@dataclass(frozen=True)
class Effective:
    """The policy as it governs this computer now."""

    enabled_ops: tuple[str, ...]
    roots: Mapping[str, Root]
    log_results: bool
    # Enabled in the file but disabled here: the catalog definition changed since it was enabled.
    changed_by_upgrade: tuple[str, ...]
    # Enabled in the file but disabled here: not in this release's catalog.
    not_in_catalog: tuple[str, ...]
    # Enabled in the file but disabled here: its executable is missing on this computer.
    unavailable: tuple[str, ...]


def valid_alias(alias: str) -> bool:
    return _ALIAS.fullmatch(alias) is not None


def fresh() -> Policy:
    """The policy of a fresh install: only the operations the catalog enables by default."""
    return Policy(
        enabled=MappingProxyType(
            {op.name: op.definition_hash for op in OPERATIONS.values() if op.default_enabled}
        )
    )


def _ancestors(path: str) -> list[str]:
    """``path`` and each folder above it, up to ``/``."""
    chain = [path]
    while chain[-1] != os.path.dirname(chain[-1]):
        chain.append(os.path.dirname(chain[-1]))
    return chain


def _same(one: str, other: str) -> bool:
    try:
        return os.path.samefile(one, other)
    except (OSError, ValueError):
        return False


def _mount_spellings(target_real: str) -> list[str]:
    """The other paths that show ``target_real`` through another mount of the filesystem holding
    it, from Linux's mount table: the source of a bind mount, or a btrfs top level holding the
    subvolume it is on. Nothing where there is no mount table to read."""
    table = mounts.table()
    # The mount ``target_real`` is on: the deepest mount point above it, the later of two at one
    # place, as a later mount hides an earlier one.
    holding: mounts.Mount | None = None
    for mount in table:
        if mounts.beneath(target_real, mount.point) is not None:
            if holding is None or len(mount.point) >= len(holding.point):
                holding = mount
    if holding is None:
        return []
    rest = mounts.beneath(target_real, holding.point) or ""
    within = os.path.join(holding.root, rest) if rest else holding.root
    spellings = []
    for other in table:
        tail = mounts.beneath(within, other.root)
        if other.device != holding.device or tail is None:
            continue
        spelling = os.path.join(other.point, tail) if tail else other.point
        if spelling != target_real:
            spellings.append(spelling)
    return spellings


def holds(folder: str, target: str, *, look_below: bool = False) -> bool:
    """Whether ``folder`` is ``target`` or holds it. ``folder`` counts as holding ``target`` when
    it is ``target`` or one of the folders above it:

    - by real path;
    - by device and inode, so that another spelling of one of them passes too (another case on a
      case-insensitive disk, a firmlink, a second mount of it);
    - above another path that shows ``target`` through another mount of the same filesystem, by
      Linux's mount table (``/mnt/storage`` when ``/mnt/storage/homes`` is mounted again at
      ``/home``, or a btrfs top level holding ``@home``);
    - on macOS, above ``target``'s spelling on the volume holding the users' files
      (:data:`_DATA_VOLUME`, which ``/Users`` is a firmlink into);
    - with ``look_below``, when ``folder`` followed by a trailing part of ``target``'s real path is
      ``target``, with no link on the way. Only adding a folder looks below it: a stalled network
      mount there would hang every policy load.

    A folder that reaches ``target`` any other way is not seen: through another filesystem (a FUSE
    or overlay view of it, say), through a mount this process cannot see, or, on macOS, through
    anything but that volume. A link to ``target`` inside a folder does not make the folder hold
    it: containment refuses the link when it is followed (containment.py)."""
    target_real = os.path.realpath(target)
    covering = _ancestors(target_real)
    data_spelling = os.path.join(_DATA_VOLUME, target_real.lstrip("/"))
    for spelling in (data_spelling, *_mount_spellings(target_real)):
        if spelling != target_real and _same(spelling, target_real):
            covering += _ancestors(spelling)
    folder_real = os.path.realpath(folder)
    if folder_real in covering:
        return True
    if any(_same(folder, other) or _same(folder_real, other) for other in covering):
        return True
    if look_below:
        parts = [part for part in target_real.split("/") if part]
        for start in range(len(parts) + 1):
            below = os.path.join(folder_real, *parts[start:])
            if os.path.realpath(below) == below and _same(below, target_real):
                return True
    return False


def is_broad(path: str, *, home: str | None = None, look_below: bool = False) -> bool:
    """Whether a folder is ``$HOME`` or holds it, by :func:`holds`; ``/`` holds it too."""
    target = home if home is not None else str(Path.home())
    return holds(path, target, look_below=look_below)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PolicyError(message)


def _parse(data: Mapping[str, Any], home: str | None) -> Policy:
    _require(not set(data) - {"version", "log_results", "ops", "roots"}, "unknown top-level key")
    _require(data.get("version") == VERSION and type(data.get("version")) is int, "version")
    log_results = data.get("log_results", False)
    _require(type(log_results) is bool, "log_results must be true or false")
    ops = data.get("ops", {})
    _require(isinstance(ops, dict), "ops must be a table")
    enabled: dict[str, str] = {}
    for name, entry in ops.items():
        _require(_OP_NAME.fullmatch(name) is not None, f"ops: {name!r} is not an operation name")
        _require(isinstance(entry, dict) and set(entry) == {"definition"}, f"ops.{name}")
        definition = entry["definition"]
        _require(
            isinstance(definition, str) and _HEX64.fullmatch(definition) is not None,
            f"ops.{name}.definition must be 64 lowercase hex digits",
        )
        enabled[name] = definition
    roots_table = data.get("roots", {})
    _require(isinstance(roots_table, dict), "roots must be a table")
    _require(len(roots_table) <= MAX_ROOTS, f"at most {MAX_ROOTS} roots")
    roots: dict[str, Root] = {}
    for alias, entry in roots_table.items():
        _require(valid_alias(alias), f"roots: {alias!r} is not a folder alias")
        _require(isinstance(entry, dict), f"roots.{alias} must be a table")
        _require(not set(entry) - {"path", "broad"} and "path" in entry, f"roots.{alias}")
        path = entry["path"]
        broad = entry.get("broad", False)
        _require(
            isinstance(path, str) and os.path.isabs(path) and "\x00" not in path,
            f"roots.{alias}.path must be an absolute path",
        )
        _require(type(broad) is bool, f"roots.{alias}.broad must be true or false")
        _require(
            broad or not is_broad(path, home=home),
            f"roots.{alias} is your home folder, holds it, or is /, which needs the explicit flag",
        )
        roots[alias] = Root(path=path, broad=broad)
    return Policy(
        enabled=MappingProxyType(enabled),
        roots=MappingProxyType(roots),
        log_results=log_results,
    )


def load(path: Path, *, home: str | None = None) -> Policy:
    """The policy in ``path``; the fresh policy if there is no file. Raises PolicyError if the
    file is invalid."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return fresh()
    except OSError as exc:
        raise PolicyError(f"{path} cannot be read: {exc.strerror}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PolicyError(f"{path} is not valid TOML") from exc
    return _parse(data, home)


def _toml_string(value: str) -> str:
    out = ['"']
    for char in value:
        code = ord(char)
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif 0x20 <= code < 0x7F:
            out.append(char)
        elif 0xD800 <= code < 0xE000:
            raise PolicyError("a path with an unpaired surrogate cannot be stored")
        elif code < 0x10000:
            out.append(f"\\u{code:04X}")
        else:
            out.append(f"\\U{code:08X}")
    out.append('"')
    return "".join(out)


def dumps(policy: Policy) -> str:
    """The policy as the TOML text :func:`save` writes."""
    lines = [
        "# lucille-host local policy. Change it with `lucille-host ops ...` and",
        "# `lucille-host roots ...`; an invalid file stops the host.",
        f"version = {VERSION}",
        f"log_results = {'true' if policy.log_results else 'false'}",
    ]
    for name in sorted(policy.enabled):
        lines += ["", f"[ops.{_toml_string(name)}]"]
        lines.append(f"definition = {_toml_string(policy.enabled[name])}")
    for alias in sorted(policy.roots):
        root = policy.roots[alias]
        lines += ["", f"[roots.{_toml_string(alias)}]"]
        lines.append(f"path = {_toml_string(root.path)}")
        lines.append(f"broad = {'true' if root.broad else 'false'}")
    return "\n".join(lines) + "\n"


def save(path: Path, policy: Policy) -> None:
    """Write the policy, mode 0600, replacing the file in one step."""
    text = dumps(policy)
    descriptor, temp = tempfile.mkstemp(dir=path.parent, prefix=".policy.", suffix=".tmp")
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


def archive(path: Path, previous: Path) -> bool:
    """Before a new pair: move the policy in ``path`` to ``previous`` and write the fresh policy in
    its place. Returns whether it moved one.

    Nothing is moved when there is no policy file, or when it holds the fresh policy, so an earlier
    ``previous`` is never replaced by a fresh one. A file that is invalid is moved as it is: the new
    pair still starts from the fresh policy."""
    try:
        if load(path) == fresh():
            return False
    except PolicyError:
        pass
    os.replace(path, previous)
    _sync_dir(path.parent)
    save(path, fresh())
    return True


def _sync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def effective(policy: Policy, family: str) -> Effective:
    """Which enabled operations actually run here, and why the others do not."""
    enabled: list[str] = []
    changed: list[str] = []
    unknown: list[str] = []
    unavailable: list[str] = []
    for name, definition in policy.enabled.items():
        op = OPERATIONS.get(name)
        if op is None:
            unknown.append(name)
        elif definition != op.definition_hash:
            changed.append(name)
        elif not op.available(family):
            unavailable.append(name)
        else:
            enabled.append(name)
    return Effective(
        enabled_ops=tuple(sorted(enabled)),
        roots=policy.roots,
        log_results=policy.log_results,
        changed_by_upgrade=tuple(sorted(changed)),
        not_in_catalog=tuple(sorted(unknown)),
        unavailable=tuple(sorted(unavailable)),
    )


def canonical(eff: Effective) -> bytes:
    """The canonical form of an effective policy, which the digest covers."""
    value = {
        "version": VERSION,
        "log_results": eff.log_results,
        "ops": {name: OPERATIONS[name].definition_hash for name in eff.enabled_ops},
        "roots": {a: {"path": r.path, "broad": r.broad} for a, r in eff.roots.items()},
    }
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()


def digest_key(state_dir: Path) -> bytes:
    """The digest key, created on first use: 32 random bytes, mode 0600, never sent anywhere.

    A new key is written whole to a temporary file and then linked into place, so the key file
    never exists half-written, and two processes making one at once end up sharing the first.
    """
    path = state_dir / KEY_FILE
    if not path.exists():
        descriptor, temp = tempfile.mkstemp(dir=state_dir, prefix=".key.", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(secrets.token_bytes(_KEY_BYTES))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temp, path)
            except FileExistsError:
                pass
        finally:
            Path(temp).unlink(missing_ok=True)
        _sync_dir(state_dir)
    key = path.read_bytes()
    if len(key) != _KEY_BYTES:
        raise PolicyError(f"{path} is damaged; delete it to make a new key")
    return key


def digest(eff: Effective, key: bytes) -> str:
    """The policy digest: 64 lowercase hex digits."""
    return hmac.new(key, canonical(eff), "sha256").hexdigest()
