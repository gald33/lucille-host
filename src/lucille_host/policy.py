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
- Anything else makes the file invalid, and the host fails closed: ``run`` acts on nothing until
  the file is valid again, and every other verb that reads it refuses. ``lucille-host policy reset``
  sets an invalid file aside, as ``policy.invalid.toml`` beside it, which nothing reads, and writes
  the fresh policy in its place (:func:`set_aside_invalid`).
- With no file at all the policy is the fresh one: only ``host.describe`` is enabled, and no folder
  is held.
- A new pair starts from the fresh policy. When ``lucille-host pair`` takes a new code, it moves the
  policy file to ``policy.previous.toml`` beside it, unless that policy is the fresh one, and writes
  the fresh policy in its place (:func:`archive`). Nothing reads the earlier policy but
  ``lucille-host policy previous``, which lists it; to use any of it again, the user enables it
  again. Because a fresh policy is never moved, trying a pairing twice keeps the earlier policy the
  first try found. The file set aside is stamped with the time it was set aside.

For a shell (``spec/CLI.md`` section 9): :func:`suggest_alias` makes an alias from a folder's name,
and :func:`check_root` says whether the host can open a folder and read an entry in it.

For ``run`` (``loop.py``), which notices a change of the file while it runs: :func:`signature`
stats it, and :func:`check_file` checks what it holds without looking at any folder, which
:func:`load` does (a folder on a stalled network mount would hold up whoever loads it).

The policy digest is HMAC-SHA256 of the canonical effective policy under a random key kept in the
state directory, which never leaves the computer. The user can match a result to a policy, and
Lucille can tell when the policy changed. A plain hash would let anyone holding it confirm a
guessed folder path.
"""

from __future__ import annotations

import errno
import hmac
import json
import os
import re
import secrets
import tempfile
import tomllib
from collections.abc import Collection, Mapping
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
    "broad_kind",
    "check_file",
    "check_root",
    "digest",
    "digest_key",
    "effective",
    "fresh",
    "holds",
    "is_broad",
    "load",
    "save",
    "set_aside_invalid",
    "signature",
    "suggest_alias",
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
    # Enabled in the file but disabled here: the catalog definition changed since it was enabled,
    # and this computer can run it, so enabling it again accepts the new definition.
    changed_by_upgrade: tuple[str, ...]
    # Enabled in the file but disabled here: not in this release's catalog.
    not_in_catalog: tuple[str, ...]
    # Enabled in the file but disabled here: its executable is missing on this computer, whatever
    # definition the file holds.
    unavailable: tuple[str, ...]


def valid_alias(alias: str) -> bool:
    return _ALIAS.fullmatch(alias) is not None


def _folds(plain: str, characters: str) -> dict[str, str]:
    return {character: plain for character in characters}


# What :func:`suggest_alias` writes for each Latin letter with a mark, and each Latin ligature, from
# U+00C0 to U+017F: its plain letters, lowercase. The table maps both cases straight to lowercase
# ASCII: str.lower() is never called on a character outside ASCII ("\u0130".lower() is two).
_LATIN: Final = {
    **_folds("a", "\u00c0\u00c1\u00c2\u00c3\u00c4\u00c5\u00e0\u00e1\u00e2\u00e3\u00e4\u00e5"),
    **_folds("ae", "\u00c6\u00e6"),
    **_folds("c", "\u00c7\u00e7"),
    **_folds("e", "\u00c8\u00c9\u00ca\u00cb\u00e8\u00e9\u00ea\u00eb"),
    **_folds("i", "\u00cc\u00cd\u00ce\u00cf\u00ec\u00ed\u00ee\u00ef"),
    **_folds("d", "\u00d0\u00f0"),
    **_folds("n", "\u00d1\u00f1"),
    **_folds("o", "\u00d2\u00d3\u00d4\u00d5\u00d6\u00d8\u00f2\u00f3\u00f4\u00f5\u00f6\u00f8"),
    **_folds("u", "\u00d9\u00da\u00db\u00dc\u00f9\u00fa\u00fb\u00fc"),
    **_folds("y", "\u00dd\u00fd\u00ff"),
    **_folds("th", "\u00de\u00fe"),
    **_folds("ss", "\u00df"),
    **{
        chr(code): plain
        for first, last, plain in (
            (0x0100, 0x0105, "a"),
            (0x0106, 0x010D, "c"),
            (0x010E, 0x0111, "d"),
            (0x0112, 0x011B, "e"),
            (0x011C, 0x0123, "g"),
            (0x0124, 0x0127, "h"),
            (0x0128, 0x0131, "i"),
            (0x0132, 0x0133, "ij"),
            (0x0134, 0x0135, "j"),
            (0x0136, 0x0138, "k"),
            (0x0139, 0x0142, "l"),
            (0x0143, 0x014B, "n"),
            (0x014C, 0x0151, "o"),
            (0x0152, 0x0153, "oe"),
            (0x0154, 0x0159, "r"),
            (0x015A, 0x0161, "s"),
            (0x0162, 0x0167, "t"),
            (0x0168, 0x0173, "u"),
            (0x0174, 0x0175, "w"),
            (0x0176, 0x0178, "y"),
            (0x0179, 0x017E, "z"),
            (0x017F, 0x017F, "s"),
        )
        for code in range(first, last + 1)
    },
}
_ALIAS_MOST: Final = 32
_FALLBACK: Final = "folder"


def _fold(character: str) -> str:
    """One character of a folder's name, as an alias writes it (spec/CLI.md section 9.1)."""
    code = ord(character)
    if 0x0300 <= code <= 0x036F:  # a combining mark, as a decomposed name carries: dropped
        return ""
    if 0xFF01 <= code <= 0xFF5E:  # a full-width form: the ASCII character it stands for
        character = chr(code - 0xFEE0)
    if character.isascii():
        if character.isalpha():
            return character.lower()
        if character.isdigit() or character in "-_":
            return character
        return "-"
    return _LATIN.get(character, "-")


def suggest_alias(name: str, taken: Collection[str]) -> str:
    """A valid alias made from a folder's name that is not one of ``taken``: each character
    folded to lowercase ASCII (Latin letters with a mark lose it, a combining mark is dropped, a
    full-width form is its ASCII character, anything else is ``-``), each run of ``-`` made one,
    ``-`` and ``_`` taken off both ends, ``folder-`` put first when it starts with a digit, and cut
    to 32 characters. ``folder`` when nothing is left. If ``taken`` holds it, ``-2``, ``-3`` and so
    on is added to the first that is free, the alias cut first to make room."""
    folded = re.sub("-+", "-", "".join(_fold(character) for character in name)).strip("-_")
    if not folded:
        folded = _FALLBACK
    elif not folded[0].isalpha():
        folded = f"{_FALLBACK}-{folded}"
    alias = folded[:_ALIAS_MOST].rstrip("-_")
    if alias not in taken:
        return alias
    number = 2
    while True:
        suffix = f"-{number}"
        candidate = alias[: _ALIAS_MOST - len(suffix)].rstrip("-_") + suffix
        if candidate not in taken and valid_alias(candidate):
            return candidate
        number += 1


def check_root(path: str, family: str) -> tuple[str, str | None]:
    """Whether the host can open the folder ``path`` and read one entry in it, as an operation
    working there would: ``ok``, ``denied_by_macos`` (EPERM on macOS, where its privacy protection
    refuses it), ``denied`` (EACCES, or EPERM on Linux), ``missing`` (ENOENT or ENOTDIR), or
    ``error`` with the error's name. Sends nothing, and writes nothing."""
    try:
        with os.scandir(path) as entries:
            next(entries, None)
    except OSError as exc:
        if exc.errno == errno.EPERM:
            return ("denied_by_macos" if family == "macos" else "denied"), None
        if exc.errno == errno.EACCES:
            return "denied", None
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return "missing", None
        return "error", errno.errorcode.get(exc.errno or 0, f"E{exc.errno}")
    return "ok", None


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


def broad_kind(path: str, *, home: str | None = None) -> str:
    """Of a folder :func:`is_broad` found broad: ``root`` for ``/``, ``home`` for the home folder
    itself, by real path or another spelling of it, else ``holds_home``."""
    target = home if home is not None else str(Path.home())
    if os.path.realpath(path) == "/":
        return "root"
    if os.path.realpath(path) == os.path.realpath(target) or _same(path, target):
        return "home"
    return "holds_home"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PolicyError(message)


def _parse(data: Mapping[str, Any], home: str | None, *, folders: bool = True) -> Policy:
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
            broad or not folders or not is_broad(path, home=home),
            f"roots.{alias} is your home folder, holds it, or is /, which needs the explicit flag",
        )
        roots[alias] = Root(path=path, broad=broad)
    return Policy(
        enabled=MappingProxyType(enabled),
        roots=MappingProxyType(roots),
        log_results=log_results,
    )


def _read(path: Path) -> Mapping[str, Any] | None:
    """The TOML in ``path``; None when there is no file. PolicyError for anything else."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PolicyError(f"{path} cannot be read: {exc.strerror}") from exc
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PolicyError(f"{path} is not valid TOML") from exc
    except RecursionError as exc:
        # Nested past what the parser follows, which no policy the host writes is.
        raise PolicyError(f"{path} nests too deep to be read") from exc


def load(path: Path, *, home: str | None = None) -> Policy:
    """The policy in ``path``; the fresh policy if there is no file. Raises PolicyError if the
    file is invalid. It looks at each folder the file holds (:func:`is_broad`)."""
    data = _read(path)
    return fresh() if data is None else _parse(data, home)


def check_file(path: Path) -> None:
    """Raise PolicyError when the file in ``path`` is invalid by what it holds, looking at no
    folder. A file that passes can still be invalid by a folder of it, which only :func:`load`
    sees."""
    data = _read(path)
    if data is not None:
        _parse(data, None, folders=False)


def signature(path: Path) -> tuple[int, ...] | None:
    """What says the policy file changed: its device, inode, size, and modification and change
    times, in nanoseconds; None when there is no file, and ``(-1, errno)`` when it cannot be
    statted. A file replaced (``save``), rewritten in place, or whose mode changed has another
    one, even with its modification time put back."""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return (-1, exc.errno or 0)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


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
        "# `lucille-host roots ...`; the host acts on nothing while it is invalid.",
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
    pair still starts from the fresh policy. The file moved is stamped with the time it was moved,
    which ``policy previous`` reports as when it was set aside."""
    try:
        if load(path) == fresh():
            return False
    except PolicyError:
        pass
    os.replace(path, previous)
    os.utime(previous)
    _sync_dir(path.parent)
    save(path, fresh())
    return True


def set_aside_invalid(path: Path, aside: Path) -> bool:
    """``lucille-host policy reset``: when the policy file in ``path`` is invalid, move it to
    ``aside``, replacing an earlier one, and write the fresh policy in its place. Returns whether
    it did. A valid file, and no file, are left as they are. A link is moved as a link: what it
    points to is not touched. The caller holds the policy lock."""
    try:
        load(path)
    except PolicyError:
        pass
    else:
        return False
    os.replace(path, aside)
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
        elif not op.available(family):
            unavailable.append(name)
        elif definition != op.definition_hash:
            changed.append(name)
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
