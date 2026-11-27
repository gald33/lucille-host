"""Check a copy of the Mac app, or the disk image that holds it, against its ``MANIFEST.json``,
the pins and the source it says it was built from (decisions/0002 §18 and §21, VERIFYING.md "The
Mac app", step 4). Anyone can run it; it needs no secret and changes nothing.

    uv run --no-project python tools/macos/verify_app.py PATH [--host-wheel FILE]
        [--cache DIR] [--offline] [--summary FILE]

``PATH`` is ``<name>.app``, or, on a Mac only, a disk image that holds it, which is mounted
read-only and detached again. Run it from a checkout of the commit ``MANIFEST.json`` names
(``build.commit``), with no change to a tracked file: the source it compares with is that
checkout's ``tools/macos/pins.toml``, ``prune.txt``, ``uv.lock``, ``macos/Icons/`` and the
build's own code in ``tools/macos/``. It runs ``uv export`` once, as the build does, to read the
lock. It runs nothing from the app: the bundled interpreter is never started.

What it checks, in order. Each check prints ``ok:`` or ``FAIL:``.

1. **The source.** The checkout is at ``build.commit`` and has no change to a tracked file. If
   not, nothing else is checked.
2. **The version rule.** ``build.tag`` or ``build.build_number`` gives the app's two version
   strings from the checkout's ``__version__`` (``build_app.bundle_versions``).
3. **The downloads.** The wheels the build installs, chosen from the checkout's ``uv.lock`` by
   the build's own rule (``payload.planned_wheels``), each downloaded from the lock's URL and
   refused unless its sha256 is the lock's; the host's wheel, from ``--host-wheel`` or else from
   PyPI, whose JSON for the version gives its sha256 and its URL; and the runtime archive
   ``pins.toml`` names, refused unless its sha256 is the pin.
4. **MANIFEST.json**, field by field, against what the build writes from those: the app's name,
   identifiers, deployment target and version strings; the commit, the tag or build number, and
   the pinned Xcode; the host's version and its wheel's file name and sha256; the runtime's
   version, file name, URL, sha256 and what the build removed from it; every wheel's name,
   version, file name, URL and sha256; and ripgrep. Only ``build.swiftc`` is taken as written.
   The file must also be in the form the build writes (``build_app.manifest_text``).
5. **Every file outside site-packages.** A reference is made in a temporary folder by the build's
   own functions: the pinned runtime unpacked and prepared (pip uninstalled, ``prune.txt``
   removed, the interpreter renamed, links replaced), ``Info.plist``, ``PkgInfo``, the wrapper,
   the licence texts, the icons and ``MANIFEST.json``. Each file of the app must be the
   reference's file, byte for byte and with the same execute bit, and each file of the
   reference must be in the app. Admitted by name only: the app's own executable,
   ``Contents/MacOS/<name>``, which must be a Mach-O file (the Swift app, compiled from
   ``macos/App/``: see below), codesign's ``Contents/_CodeSignature/CodeResources``, and the
   ``.pyc`` files of step 7. A symbolic link anywhere fails.
6. **site-packages** (M10): the installed distributions are exactly the planned wheels and the
   host; every file is byte for byte a file of its wheel's ``RECORD``, apart from the
   installer's named files and the build's ``.pyc`` files (``payload.record_problems``); each
   ``RECORD`` is compared line by line with the wheel's own.
7. **The bytecode's layout**: one checked-hash ``.pyc`` beside each ``.py`` of the runtime's
   library, and no other bytecode (``build_app.bytecode_layout_problems``). What each holds,
   ``check_app.py bytecode`` judges, with the bundled interpreter.
8. **On a Mac**: ``codesign --verify --deep --strict`` on the app, which holds every sealed file
   to ``CodeResources``. A disk image must hold the app and a link to ``/Applications``.

**Reported, not failed.** Signing rewrites each Mach-O file in place, so a Mach-O file that
differs from its reference (the runtime's, or its wheel's) is not failed. On a Mac each is
compared again after ``codesign --remove-signature`` on a copy of both, and the verdict, SAME or
DIFFERENT, is reported; elsewhere it is reported as not compared. Whether stripping gives back
the reference's bytes is not yet established for a signed build (decisions/0002 §6). The app's
own executable is reported as not verified. The rebuild job in ``ci.yml`` (``macos-rebuild``)
compares a second build's unsigned tarball, executable included, with the first's.

**What this cannot establish**: that the Swift app, the runtime and ripgrep are what their
sources and pins say beyond review and the rebuild job; what Apple's notarization looked for;
what each ``.pyc`` holds (``check_app.py bytecode`` does); that the copy in Applications is still
the one checked here after a process running as the same user touched it.

Exit codes: 0 every check passed, 1 a check failed, 2 a usage error or a check that cannot run
here (a disk image off a Mac, or no checkout).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import plistlib
import re
import struct
import subprocess
import sys
import tempfile
import urllib.error
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

if __package__ in (None, ""):  # run as a script: make ``tools.macos`` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.macos import build_app, check_app, icons, payload  # noqa: E402
from tools.release import check_release  # noqa: E402

DEFAULT_CACHE: Final = build_app.REPO_ROOT / "build" / "macos" / "verify-cache"
PYPI_JSON: Final = "https://pypi.org/pypi/{project}/{version}/json"
# The files codesign writes into a bundle, admitted by their names: ``codesign --verify`` holds
# every other file to the one listed here.
SIGNATURE_FILES: Final = frozenset({"Contents/_CodeSignature/CodeResources"})
SIGNATURE_FOLDERS: Final = frozenset({"Contents/_CodeSignature"})
MANIFEST_SCHEMA: Final = 1
_COMMIT: Final = re.compile(r"[0-9a-f]{7,40}")
_SHA256: Final = re.compile(r"[0-9a-f]{64}")

Fetch = Callable[[str, Mapping[str, str]], bytes]


class VerifyError(RuntimeError):
    """A check cannot go on; the message says why."""


@dataclass(frozen=True)
class Source:
    """What the build reads from the checkout: the pins, the prune list, the lock and what
    ``uv export`` makes of it, the host's ``__version__``, and the icons' folder."""

    pins: build_app.Pins
    prune: tuple[str, ...]
    lock_text: str
    requirements_text: str
    host_version: str
    icons: Path


def source_from_checkout() -> Source:
    """The :class:`Source` of this checkout, read and checked as the build reads and checks it."""
    build_app.load_build_constraints()
    return Source(
        pins=build_app.load_pins(),
        prune=build_app.load_prune_list(),
        lock_text=build_app.LOCK_FILE.read_text(encoding="utf-8"),
        requirements_text=build_app.export_requirements_text(),
        host_version=build_app.host_version(),
        icons=icons.ICONS_DIR,
    )


@dataclass
class Report:
    """What :func:`verify` found: the checks, and what it reports without failing."""

    checker: check_app.Checker = field(default_factory=check_app.Checker)
    reported: list[str] = field(default_factory=list)
    # Mach-O files left uncompared, off a Mac.
    not_compared: int = 0

    @property
    def passed(self) -> bool:
        return not self.checker.failures


# ---------------------------------------------------------------------------------------------
# The source


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def checkout_problems(repo: Path, commit: object) -> list[str]:
    """Why ``repo`` is not the source of an app built from ``commit`` (``MANIFEST.json``'s
    ``build.commit``, an abbreviated commit id): it is not a git checkout, its ``HEAD`` is
    another commit, or a tracked file has a change, staged or not."""
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
        return [f"MANIFEST.json's build.commit is {commit!r}, not a commit id"]
    head = _git(repo, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        return [f"{repo} is not a git checkout with a commit: {head.stderr.strip()}"]
    at = head.stdout.strip()
    if not at.startswith(commit):
        return [
            f"this checkout is at {at[:12]}, but the app was built from {commit}: run "
            f"`git checkout {commit}` (after `git fetch` if it is not here) and run this again"
        ]
    status = _git(repo, "status", "--porcelain", "--untracked-files=no")
    if status.returncode != 0:
        return [f"git status failed: {status.stderr.strip()}"]
    if status.stdout.strip():
        changed = ", ".join(line[3:] for line in status.stdout.splitlines())
        return [f"this checkout has changes to tracked files ({changed}): undo them first"]
    return []


# ---------------------------------------------------------------------------------------------
# The downloads


def _offline_file(path: Path, sha256: str, pinned_by: str) -> None:
    """With ``--offline``: ``path`` must be in the cache with its pinned sha256. A copy that
    does not match is refused and left where it is, where a download would replace it."""
    if not path.is_file():
        raise VerifyError(f"--offline, and {path.parent} lacks {path.name}")
    actual = payload.sha256_of(path)
    if actual != sha256:
        raise VerifyError(f"{path}: its sha256 is {actual}, but {pinned_by} pins {sha256}")


def fetch_lock_wheels(
    plan: Sequence[payload.LockWheel], cache: Path, dest: Path, *, offline: bool
) -> list[payload.WheelFile]:
    """The planned wheels, from ``cache`` or their lock URLs, each refused unless its sha256 is
    the lock's (``payload.fetch_wheels``). ``offline``: none is downloaded, and a wheel that is
    not in ``cache`` is refused."""
    if offline:
        for wheel in plan:
            _offline_file(cache / wheel.filename, wheel.sha256, "uv.lock")
    try:
        return payload.fetch_wheels(plan, cache, dest)
    except (payload.AssemblyError, OSError) as exc:
        raise VerifyError(str(exc)) from exc


def pypi_host_wheel(
    version: str, cache: Path, fetch: Fetch = check_release.fetch_url
) -> payload.WheelFile:
    """The host's wheel for ``version`` as PyPI serves it: the sha256 and URL its JSON lists for
    the file, the bytes from that URL refused unless they have that sha256. Kept in ``cache``."""
    name = payload.host_wheel_name(version)
    url = PYPI_JSON.format(project=payload.HOST_DISTRIBUTION, version=version)
    try:
        listing = json.loads(fetch(url, {"Accept": "application/json"}))
    except urllib.error.HTTPError as exc:
        raise VerifyError(
            f"PyPI has no {payload.HOST_DISTRIBUTION} {version} ({exc.code}): a build that is "
            "not a release needs --host-wheel, a wheel built from this checkout with "
            "`build_app.py wheel`"
        ) from exc
    except (urllib.error.URLError, ValueError, check_release.CheckError) as exc:
        raise VerifyError(f"cannot read {url}: {exc}") from exc
    entries = [
        entry
        for entry in (listing.get("urls") if isinstance(listing, dict) else None) or []
        if isinstance(entry, dict) and entry.get("filename") == name
    ]
    if len(entries) != 1:
        raise VerifyError(f"PyPI's JSON for {version} lists {len(entries)} files named {name}")
    digest = (entries[0].get("digests") or {}).get("sha256")
    file_url = entries[0].get("url")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise VerifyError(f"PyPI's JSON gives {name} no sha256")
    if not isinstance(file_url, str):
        raise VerifyError(f"PyPI's JSON gives {name} no URL")
    try:
        data = fetch(file_url, {})
    except (urllib.error.URLError, check_release.CheckError) as exc:
        raise VerifyError(f"cannot download {file_url}: {exc}") from exc
    actual = hashlib.sha256(data).hexdigest()
    if actual != digest:
        raise VerifyError(f"{name}: PyPI served bytes with the sha256 {actual}, not its {digest}")
    folder = cache / "host-wheel"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return payload.WheelFile(payload.HOST_DISTRIBUTION, version, name, file_url, digest, path)


def given_host_wheel(path: Path, version: str) -> payload.WheelFile:
    """The host's wheel the user gave, by its name: the checkout's version's."""
    name = payload.host_wheel_name(version)
    if path.name != name or not path.is_file():
        raise VerifyError(f"--host-wheel must be a file named {name}, for this checkout's version")
    digest = payload.sha256_of(path)
    return payload.WheelFile(payload.HOST_DISTRIBUTION, version, name, None, digest, path)


def runtime_archive(pins: build_app.Pins, cache: Path, *, offline: bool) -> Path:
    """The runtime archive the pins name, from ``cache`` or its URL, refused unless its sha256
    is the pin (``build_app.fetch_runtime``)."""
    if offline:
        _offline_file(cache / pins.asset, pins.sha256, "pins.toml")
    try:
        return build_app.fetch_runtime(pins, cache)
    except (build_app.BuildError, OSError) as exc:
        raise VerifyError(str(exc)) from exc


# ---------------------------------------------------------------------------------------------
# MANIFEST.json


def read_manifest(app: Path) -> tuple[dict[str, Any], bytes]:
    path = app / build_app.MANIFEST_PATH
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise VerifyError(f"{build_app.MANIFEST_PATH} cannot be read: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("build"), dict):
        raise VerifyError(f"{build_app.MANIFEST_PATH} is not an object with a build object")
    return data, raw


def expected_manifest(
    source: Source,
    versions: build_app.BundleVersions,
    found: Mapping[str, Any],
    *,
    removed: Sequence[str],
    wheels: Sequence[payload.WheelFile],
    host: payload.WheelFile,
) -> dict[str, Any]:
    """The ``MANIFEST.json`` the build writes for this source, these downloads and what the
    runtime's preparation removed, with ``build``'s commit, tag, build number and ``swiftc``
    taken from ``found``: the commit is checked against the checkout, the other two by the
    version rule, and ``swiftc`` is not checked."""
    pins = source.pins
    build = found.get("build") or {}
    return {
        "schema": MANIFEST_SCHEMA,
        "app": {
            "name": pins.app_name,
            "bundle_id": pins.bundle_id,
            "runtime_id": pins.runtime_id,
            "deployment_target": pins.deployment_target,
            "short_version": versions.short,
            "bundle_version": versions.build,
        },
        "build": {
            "commit": build.get("commit"),
            "tag": build.get("tag"),
            "build_number": build.get("build_number"),
            "xcode": pins.xcode,
            "swiftc": build.get("swiftc"),
        },
        "host": {
            "version": source.host_version,
            "wheel": {"file": host.filename, "sha256": host.sha256},
        },
        "runtime": {
            "version": pins.python_version,
            "asset": pins.asset,
            "url": pins.url,
            "sha256": pins.sha256,
            "removed": sorted(removed),
        },
        "wheels": [
            {
                "name": wheel.name,
                "version": wheel.version,
                "file": wheel.filename,
                "url": wheel.url,
                "sha256": wheel.sha256,
            }
            for wheel in sorted(wheels, key=lambda wheel: wheel.name)
        ],
        "ripgrep": {"commit": pins.ripgrep_commit} if pins.ripgrep_commit else None,
    }


def manifest_problems(found: Any, expected: Any, where: str = "MANIFEST.json") -> list[str]:
    """Each place ``found`` differs from ``expected``, by its path of keys and list indexes."""
    if isinstance(expected, dict) and isinstance(found, dict):
        problems = []
        for key in sorted(set(found) | set(expected)):
            here = f"{where}.{key}"
            if key not in found:
                problems.append(f"{here} is missing; the build writes {expected[key]!r}")
            elif key not in expected:
                problems.append(f"{here} is {found[key]!r}; the build writes no such field")
            else:
                problems += manifest_problems(found[key], expected[key], here)
        return problems
    if isinstance(expected, list) and isinstance(found, list) and len(found) == len(expected):
        problems = []
        for number, (one, other) in enumerate(zip(found, expected, strict=True)):
            problems += manifest_problems(one, other, f"{where}[{number}]")
        return problems
    if found != expected:
        return [f"{where} is {found!r}; the build writes {expected!r}"]
    return []


# ---------------------------------------------------------------------------------------------
# The reference and the comparison


def write_reference(
    ref: Path, source: Source, versions: build_app.BundleVersions, archive: Path
) -> list[str]:
    """Make the reference app at ``ref`` (``<name>.app`` in a temporary folder) from the source,
    by the build's own functions, without site-packages, the executable, the licence texts and
    ``MANIFEST.json``, which :func:`finish_reference` adds. Returns what preparing the runtime
    removed."""
    contents = ref / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    runtime = build_app.unpack_runtime(archive, contents)
    removed = build_app.prepare_runtime(runtime, source.prune)
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(build_app.info_plist(source.pins, versions), handle, sort_keys=True)
    (contents / "PkgInfo").write_text("APPL????", encoding="ascii")
    build_app.write_wrapper(ref)
    icons.write_icons(contents / "Resources", source.icons)
    return removed


def finish_reference(ref: Path, site: Path, manifest: Mapping[str, Any]) -> None:
    """Add to the reference the licence texts, copied as the build copies them from ``site``
    (the app's site-packages, which :func:`site_problems` holds to the wheels), and
    ``MANIFEST.json``."""
    runtime = ref / build_app.RUNTIME_DIR
    build_app.copy_licenses(
        ref / build_app.LICENSES_IN_APP, site, runtime / build_app.STDLIB / "LICENSE.txt"
    )
    (ref / build_app.MANIFEST_PATH).write_text(build_app.manifest_text(manifest), encoding="utf-8")


def _entries(app: Path) -> tuple[dict[str, Path], set[str], list[str]]:
    """``(files, folders, links)`` of ``app`` by path from it, leaving out what the other checks
    judge: everything under site-packages, and each entry of a ``__pycache__`` folder in the
    runtime's library. Links are not followed."""
    site = f"{build_app.RUNTIME_DIR}/{build_app.SITE_PACKAGES}"
    library = f"{build_app.RUNTIME_DIR}/{build_app.STDLIB}"
    files: dict[str, Path] = {}
    folders: set[str] = set()
    links: list[str] = []
    for folder, dirs, names in os.walk(app):
        here = Path(folder)
        rel_here = here.relative_to(app).as_posix()
        if rel_here == site:
            dirs[:] = []
            continue
        if here.name == "__pycache__" and rel_here.startswith(library + "/"):
            dirs[:] = []
            continue
        for name in sorted(dirs):
            path = here / name
            rel = path.relative_to(app).as_posix()
            if path.is_symlink():
                links.append(rel)
            elif not (name == "__pycache__" and rel.startswith(library + "/")):
                folders.add(rel)
        dirs[:] = [name for name in dirs if not (here / name).is_symlink()]
        for name in sorted(names):
            path = here / name
            rel = path.relative_to(app).as_posix()
            if path.is_symlink():
                links.append(rel)
            else:
                files[rel] = path
    return files, folders, sorted(links)


def _executable(path: Path) -> bool:
    return bool(path.stat().st_mode & 0o111)


@dataclass
class TreeResult:
    """:func:`compare_trees`' findings: what fails, the Mach-O files that differ from their
    reference (``rel``), and what is admitted by name only."""

    problems: list[str] = field(default_factory=list)
    signed: list[str] = field(default_factory=list)
    admitted: list[str] = field(default_factory=list)


def compare_trees(app: Path, ref: Path, app_name: str) -> TreeResult:
    """Every file of ``app`` outside site-packages and the library's ``__pycache__`` folders
    against the reference ``ref``: the same bytes and the same execute bit. A Mach-O file whose
    reference is a Mach-O file and whose bytes differ is listed in ``signed``, not failed. The
    app's executable and codesign's files are admitted by name; a link fails."""
    result = TreeResult()
    ours, our_folders, links = _entries(app)
    theirs, their_folders, _ = _entries(ref)
    executable = f"Contents/MacOS/{app_name}"
    result.problems += [f"symbolic link in the app: {rel}" for rel in links]
    for rel in sorted(our_folders - their_folders - SIGNATURE_FOLDERS):
        result.problems.append(f"{rel}/ is a folder the build does not make")
    for rel in sorted(their_folders - our_folders):
        result.problems.append(f"{rel}/ is missing: the build makes it")
    if executable not in ours:
        result.problems.append(f"{executable} is missing")
    for rel, path in sorted(ours.items()):
        if rel == executable:
            if build_app.is_macho(path):
                result.admitted.append(f"`{rel}`: the app's executable, not verified here")
            else:
                result.problems.append(f"{rel} is not a Mach-O file")
            continue
        if rel in SIGNATURE_FILES:
            result.admitted.append(
                f"`{rel}`: codesign's, which `codesign --verify` holds the app to"
            )
            continue
        reference = theirs.get(rel)
        if reference is None:
            result.problems.append(f"{rel} is in the app, but the build makes no such file")
            continue
        if _executable(path) != _executable(reference):
            result.problems.append(
                f"{rel} is {'' if _executable(path) else 'not '}executable; the build makes it "
                f"{'' if _executable(reference) else 'not '}executable"
            )
        if path.read_bytes() == reference.read_bytes():
            continue
        if build_app.is_macho(path) and build_app.is_macho(reference):
            result.signed.append(rel)
        else:
            result.problems.append(f"{rel} differs from the file the build makes from the source")
    for rel in sorted(set(theirs) - set(ours)):
        result.problems.append(f"{rel} is missing: the build makes it")
    return result


# Mach-O, as <mach-o/loader.h> and <mach-o/fat.h> lay it out: a 64-bit file's magic (read little
# endian), a universal file's (read big endian, 32- and 64-bit offsets), the 64-bit segment
# command, and where a segment command's vmsize is, from its start (cmd, cmdsize, segname[16],
# vmaddr, then vmsize).
_MH_MAGIC_64: Final = 0xFEEDFACF
_FAT_MAGIC: Final = 0xCAFEBABE
_FAT_MAGIC_64: Final = 0xCAFEBABF
_LC_SEGMENT_64: Final = 0x19
_VMSIZE_AT: Final = 32
_LINKEDIT: Final = b"__LINKEDIT"
SAME: Final = "SAME"
SAME_BUT_VMSIZE: Final = (
    "SAME, apart from __LINKEDIT's vmsize, which `codesign --remove-signature` leaves as "
    "signing set it"
)


def _thin_vmsize_offsets(data: bytes, start: int) -> list[int]:
    try:
        if struct.unpack_from("<I", data, start)[0] != _MH_MAGIC_64:
            return []
        (ncmds,) = struct.unpack_from("<I", data, start + 16)
        found, at = [], start + 32
        for _ in range(ncmds):
            cmd, size = struct.unpack_from("<II", data, at)
            if size < 8:
                return []
            if cmd == _LC_SEGMENT_64 and data[at + 8 : at + 24].rstrip(b"\0") == _LINKEDIT:
                struct.unpack_from("<Q", data, at + _VMSIZE_AT)
                found.append(at + _VMSIZE_AT)
            at += size
        return found
    except struct.error:
        return []


def linkedit_vmsize_offsets(data: bytes) -> list[int]:
    """Where the vmsize of each ``__LINKEDIT`` segment is in ``data``, a 64-bit Mach-O file or a
    universal file of them; empty for anything else, or a file cut short."""
    try:
        (magic,) = struct.unpack_from(">I", data, 0)
        if magic not in (_FAT_MAGIC, _FAT_MAGIC_64):
            return _thin_vmsize_offsets(data, 0)
        (count,) = struct.unpack_from(">I", data, 4)
        entry, form = (20, ">8xI") if magic == _FAT_MAGIC else (32, ">8xQ")
        found = []
        for number in range(count):
            (offset,) = struct.unpack_from(form, data, 8 + number * entry)
            slice_offsets = _thin_vmsize_offsets(data, offset)
            if not slice_offsets:
                return []
            found += slice_offsets
        return found
    except struct.error:
        return []


def compare_without_signatures(ours: bytes, theirs: bytes) -> str | None:
    """:data:`SAME` when two Mach-O files, each with its signature removed, are the same bytes;
    :data:`SAME_BUT_VMSIZE` when they are apart from the vmsize of each ``__LINKEDIT`` segment,
    at the same places in both (measured on a Mac with an ad-hoc signature: that is the one field
    ``codesign --remove-signature`` leaves as signing grew it); None when they differ more."""
    if ours == theirs:
        return SAME
    places = linkedit_vmsize_offsets(ours)
    if len(ours) != len(theirs) or not places or places != linkedit_vmsize_offsets(theirs):
        return None
    a, b = bytearray(ours), bytearray(theirs)
    for at in places:
        a[at : at + 8] = b[at : at + 8] = bytes(8)
    return SAME_BUT_VMSIZE if a == b else None


def remove_signature(path: Path) -> str | None:
    """``codesign --remove-signature`` on ``path``: None when it worked, else what it said."""
    done = subprocess.run(
        ["codesign", "--remove-signature", str(path)], capture_output=True, check=False
    )
    if done.returncode == 0:
        return None
    said = (done.stderr or done.stdout).decode(errors="replace").strip().splitlines()
    return f"exit {done.returncode}: {said[0] if said else 'it said nothing'}"


@dataclass(frozen=True)
class Verdict:
    """A Mach-O file of the app against its reference, both without their signatures."""

    rel: str
    text: str
    same: bool


def stripped_verdicts(
    pairs: Sequence[tuple[str, bytes, bytes]],
    strip: Callable[[Path], str | None] = remove_signature,
) -> list[Verdict]:
    """Each ``(rel, app's bytes, reference's bytes)`` compared after ``strip`` (codesign, on a
    Mac) on a copy of each (:func:`compare_without_signatures`). A copy codesign cannot strip is
    named, not compared, and is not the same."""
    verdicts = []
    with tempfile.TemporaryDirectory(prefix="lucille-host-verify-strip-") as tmp:
        for number, (rel, ours, theirs) in enumerate(pairs):
            copies = {"app": Path(tmp) / f"{number}-app", "reference": Path(tmp) / f"{number}-ref"}
            copies["app"].write_bytes(ours)
            copies["reference"].write_bytes(theirs)
            failed = next(
                (
                    f"not compared: `codesign --remove-signature` failed on the {whose}'s copy "
                    f"({said})"
                    for whose, copy in copies.items()
                    if (said := strip(copy)) is not None
                ),
                None,
            )
            if failed is not None:
                verdicts.append(Verdict(rel, failed, False))
                continue
            a, b = copies["app"].read_bytes(), copies["reference"].read_bytes()
            verdict = compare_without_signatures(a, b)
            if verdict is None:
                verdict = (
                    f"DIFFERENT (without signatures, the app's copy is {len(a)} bytes and the "
                    f"reference's {len(b)})"
                )
                verdicts.append(Verdict(rel, verdict, False))
            else:
                verdicts.append(Verdict(rel, verdict, True))
    return verdicts


def mach_o_pairs(
    app: Path, ref: Path, signed: Sequence[str], site_macho: Sequence[str], wheels: Sequence[Path]
) -> list[tuple[str, bytes, bytes]]:
    """``(rel, app's bytes, reference's bytes)`` for each Mach-O file to compare: those outside
    site-packages that differ from the reference app's, and every one in site-packages, against
    the same file in its wheel. A site-packages file in no wheel is left out: the RECORD check
    fails it."""
    pairs = [(rel, (app / rel).read_bytes(), (ref / rel).read_bytes()) for rel in signed]
    site = f"{build_app.RUNTIME_DIR}/{build_app.SITE_PACKAGES}"
    holders: dict[str, Path] = {}
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                holders.setdefault(name, wheel)
    for rel in site_macho:
        holder = holders.get(rel)
        if holder is None:
            continue
        with zipfile.ZipFile(holder) as archive:
            pairs.append((f"{site}/{rel}", (app / site / rel).read_bytes(), archive.read(rel)))
    return pairs


def site_problems(
    site: Path, wheels: Sequence[Path], manifest: Mapping[str, Any]
) -> tuple[list[str], list[str], list[str]]:
    """``(failing, signed, macho)`` for site-packages: the installed distributions against
    ``manifest``'s wheels and host (``check_app.distribution_set_problems``), and each file
    against the wheels (``payload.record_problems``, the build's ``.pyc`` files admitted by
    name), split as ``check_app.split_record_problems`` splits them; and the Mach-O files there,
    by path from ``site``."""
    try:
        installed = payload.installed_versions(site)
    except payload.AssemblyError as exc:
        return [str(exc)], [], []
    failing = check_app.distribution_set_problems(installed, manifest)
    macho = [path.relative_to(site).as_posix() for path in build_app.macho_files(site)]
    problems = payload.record_problems(site, wheels, bytecode_tag=build_app.BYTECODE_TAG)
    record_failing, signed = check_app.split_record_problems(problems, macho)
    return failing + record_failing, signed, macho


def codesign_problems(app: Path) -> tuple[list[str], str]:
    """``codesign --verify --deep --strict`` on ``app``, and what ``codesign -dv`` says of its
    signer. Mac only."""
    verify = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)],
        capture_output=True,
        text=True,
        check=False,
    )
    shown = subprocess.run(
        ["codesign", "-dv", "--verbose=2", str(app)], capture_output=True, text=True, check=False
    )
    said = [
        line
        for line in shown.stderr.splitlines()
        if line.startswith(
            ("Identifier=", "Authority=", "TeamIdentifier=", "Signature=", "Format=")
        )
    ]
    if verify.returncode != 0:
        return [f"codesign --verify fails: {verify.stderr.strip()}"], "\n".join(said)
    return [], "\n".join(said)


# ---------------------------------------------------------------------------------------------
# The whole check


def _fail(report: Report, what: str, problems: Sequence[str], *, limit: int = 100) -> bool:
    shown = list(problems[:limit])
    if len(problems) > limit:
        shown.append(f"... and {len(problems) - limit} more")
    return report.checker.expect(not problems, what, "\n".join(shown))


def verify(
    app: Path,
    source: Source,
    *,
    host_wheel: Path | None,
    cache: Path,
    offline: bool,
    on_mac: bool,
    fetch: Fetch = check_release.fetch_url,
    strip: Callable[[Path], str | None] = remove_signature,
) -> Report:
    """Steps 2 to 8 of this module's docstring on ``app``, with ``source`` as the checkout. The
    caller has checked the checkout (step 1)."""
    report = Report()
    expect = report.checker.expect
    pins = source.pins
    try:
        found, raw = read_manifest(app)
    except VerifyError as exc:
        expect(False, "MANIFEST.json can be read", str(exc))
        return report
    build = found["build"]
    try:
        versions = build_app.bundle_versions(
            source.host_version, build.get("tag"), build.get("build_number")
        )
    except build_app.BuildError as exc:
        expect(False, "MANIFEST.json's tag or build number gives the app's versions", str(exc))
        return report
    expect(
        True,
        f"the version rule gives {versions.short} ({versions.build}) for host "
        f"{source.host_version}",
    )

    with tempfile.TemporaryDirectory(prefix="lucille-host-verify-") as tmp:
        work = Path(tmp)
        try:
            plan = payload.planned_wheels(
                source.requirements_text,
                source.lock_text,
                pins.python_version,
                pins.deployment_target,
            )
            wheels = fetch_lock_wheels(plan, cache / "wheels", work / "wheels", offline=offline)
            if host_wheel is not None:
                host = given_host_wheel(host_wheel, source.host_version)
            elif offline:
                raise VerifyError("--offline needs --host-wheel")
            else:
                host = pypi_host_wheel(source.host_version, cache, fetch)
            archive = runtime_archive(pins, cache, offline=offline)
        except (VerifyError, payload.AssemblyError) as exc:
            expect(
                False, "the wheels and the runtime archive, each with its pinned sha256", str(exc)
            )
            return report
        expect(
            True,
            f"the {len(wheels)} wheels uv.lock pins for the app, each with the lock's sha256; "
            f"the host's wheel {host.filename} ({host.sha256}"
            + (", PyPI's)" if host.url else ", given)")
            + f"; and the runtime archive {pins.asset}, with the pinned sha256",
        )

        ref = work / "reference" / f"{pins.app_name}.app"
        try:
            removed = write_reference(ref, source, versions, archive)
        except (build_app.BuildError, payload.AssemblyError, OSError) as exc:
            expect(False, "the reference app is made from the source", str(exc))
            return report
        expected = expected_manifest(
            source, versions, found, removed=removed, wheels=wheels, host=host
        )
        _fail(
            report,
            "MANIFEST.json is what the build writes for this source and these downloads",
            manifest_problems(found, expected),
        )
        expect(
            raw == build_app.manifest_text(found).encode("utf-8"),
            "MANIFEST.json is in the form the build writes",
        )

        site = app / build_app.RUNTIME_DIR / build_app.SITE_PACKAGES
        wheel_paths = [wheel.path for wheel in wheels] + [host.path]
        failing, signed_in_site, macho_in_site = site_problems(site, wheel_paths, expected)
        _fail(
            report,
            f"site-packages holds exactly the {len(wheels)} wheels and the host's, every file "
            "byte for byte a file of its wheel's RECORD, apart from the installer's named files, "
            "the .pyc files the build writes, and Mach-O files, which signing rewrites (M10)",
            failing,
        )

        try:
            finish_reference(ref, site, expected)
        except (payload.AssemblyError, OSError, StopIteration) as exc:
            expect(False, "the licence texts are copied as the build copies them", str(exc))
            return report
        tree = compare_trees(app, ref, pins.app_name)
        _fail(
            report,
            "every file outside site-packages is the file the build makes from the source, byte "
            "for byte and with its execute bit, and none is missing, apart from the app's "
            "executable, codesign's files, the .pyc files and Mach-O files, which signing "
            "rewrites",
            tree.problems,
        )
        _fail(
            report,
            "one checked-hash .pyc beside each .py of the runtime's library, and no other "
            "bytecode (check_app.py bytecode judges what each holds)",
            build_app.bytecode_layout_problems(app),
        )

        if on_mac:
            problems, signer = codesign_problems(app)
            _fail(report, "codesign --verify --deep --strict passes on the app", problems)
            if signer:
                report.reported.append("The app's signature, as `codesign -dv` gives it:")
                report.reported += ["", "```", signer, "```", ""]
        pairs = mach_o_pairs(app, ref, tree.signed, macho_in_site, wheel_paths)
        if on_mac:
            verdicts = stripped_verdicts(pairs, strip)
            _fail(
                report,
                f"each of the {len(verdicts)} Mach-O files signing rewrote is, without "
                "signatures, its reference's: the pinned runtime's or its wheel's",
                [f"{verdict.rel}: {verdict.text}" for verdict in verdicts if not verdict.same],
            )
        else:
            not_compared = "not compared: `codesign` runs only on a Mac"
            verdicts = [Verdict(rel, not_compared, False) for rel, _, _ in pairs]
            report.not_compared = len(verdicts)
        report.reported += mach_o_table(verdicts, len(signed_in_site))
        report.reported += ["Admitted by name, not compared:", ""]
        report.reported += [f"- {line}" for line in tree.admitted]
        report.reported.append("")
    return report


def mach_o_table(verdicts: Sequence[Verdict], findings: int) -> list[str]:
    lines = [
        "### Each Mach-O file signing rewrote, against its reference",
        "",
        "Signing rewrites a Mach-O file in place. Each is compared again after "
        "`codesign --remove-signature` on a copy of both, the app's and its reference (the "
        "pinned runtime's, or its wheel's). On a Mac a file that is not the same fails; "
        "elsewhere none is compared.",
        "",
        "| file | without signatures |",
        "|---|---|",
    ]
    lines += [f"| `{verdict.rel}` | {verdict.text} |" for verdict in verdicts]
    lines += ["", f"{findings} findings of the RECORD check were about these files only.", ""]
    return lines


# ---------------------------------------------------------------------------------------------
# Disk images


def image_problems(mount: Path, app_name: str) -> tuple[list[str], list[str]]:
    """``(failing, extra)`` for a mounted disk image: it must hold ``<name>.app`` and a link
    ``Applications`` to ``/Applications``; anything else it holds is listed in ``extra``."""
    problems = []
    app, link = mount / f"{app_name}.app", mount / "Applications"
    if app.is_symlink() or not app.is_dir():
        problems.append(f"the image holds no folder {app.name}")
    if not link.is_symlink() or os.readlink(link) != "/Applications":
        problems.append("the image holds no link Applications to /Applications")
    extra = sorted(entry.name for entry in mount.iterdir()) if mount.is_dir() else []
    return problems, [name for name in extra if name not in (app.name, link.name)]


def attach_argv(dmg: Path, mount: Path) -> list[str]:
    return ["attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mount), str(dmg)]


@contextlib.contextmanager
def mounted(dmg: Path) -> Iterator[Path]:
    """The disk image mounted read-only in a temporary folder, detached again after."""
    with tempfile.TemporaryDirectory(prefix="lucille-host-verify-dmg-") as tmp:
        mount = Path(tmp) / "mount"
        mount.mkdir()
        done = build_app.hdiutil(attach_argv(dmg, mount))
        if done.returncode != 0:
            raise VerifyError(f"hdiutil attach failed: {done.stderr.strip()}")
        try:
            yield mount
        finally:
            for extra in ([], ["-force"]):
                if build_app.hdiutil(["detach", *extra, str(mount)]).returncode == 0:
                    break


# ---------------------------------------------------------------------------------------------
# The command line


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_app.py",
        description="Check a copy of the Mac app against its MANIFEST.json, the pins and the "
        "source in this checkout.",
    )
    parser.add_argument("path", type=Path, help="<name>.app, or on a Mac a disk image holding it")
    parser.add_argument(
        "--host-wheel",
        type=Path,
        help="the host's wheel to compare with (default: PyPI's for the checkout's version)",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE,
        help="where downloads are kept and looked for (default build/macos/verify-cache in "
        "this checkout)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="download nothing: the wheels and the runtime archive must be in the cache, and "
        "--host-wheel is needed",
    )
    parser.add_argument("--summary", type=Path, help="also append the report here, as Markdown")
    return parser


def _check_app(app: Path, args: argparse.Namespace, on_mac: bool) -> int:
    try:
        found, _ = read_manifest(app)
    except VerifyError as exc:
        print(f"verify_app.py: {exc}", file=sys.stderr)
        return 1
    commit = found["build"].get("commit")
    problems = checkout_problems(build_app.REPO_ROOT, commit)
    if problems:
        print(f"FAIL: this checkout is the source of the app\n{problems[0]}", flush=True)
        return 1
    print(f"ok: this checkout is at {commit}, the commit MANIFEST.json names, unchanged")
    try:
        source = source_from_checkout()
    except (build_app.PinError, build_app.BuildError, subprocess.CalledProcessError) as exc:
        print(f"verify_app.py: the checkout cannot be read as the build reads it: {exc}")
        return 1
    report = verify(
        app,
        source,
        host_wheel=args.host_wheel.resolve() if args.host_wheel else None,
        cache=args.cache.resolve(),
        offline=args.offline,
        on_mac=on_mac,
    )
    text = "\n".join(report.reported)
    print(text)
    if args.summary is not None:
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write("### verify_app.py\n\n")
            handle.write("**PASSED**\n\n" if report.passed else "**FAILED**\n\n")
            handle.writelines(f"- FAIL: {what}\n" for what in report.checker.failures)
            handle.write("\n" + text + "\n")
    if report.passed and report.not_compared:
        print(
            f"verify_app.py: every check passed, but {report.not_compared} Mach-O files were not "
            "compared with their references: run this on a Mac for that"
        )
        return 0
    if report.passed:
        print("verify_app.py: every check passed")
        return 0
    print(f"verify_app.py: {len(report.checker.failures)} checks failed")
    return 1


def main(argv: Sequence[str] | None = None, *, on_mac: bool | None = None) -> int:
    args = _parser().parse_args(argv)
    on_mac = sys.platform == "darwin" if on_mac is None else on_mac
    path: Path = args.path
    if path.suffix == ".dmg" and path.is_file():
        if not on_mac:
            print(
                "verify_app.py: a disk image can be opened only on a Mac; copy the app out of "
                "it and pass the app's path",
                file=sys.stderr,
            )
            return 2
        print(f"{path.name}: sha256 {payload.sha256_of(path)}")
        app_name = build_app.load_pins().app_name
        try:
            with mounted(path) as mount:
                problems, extra = image_problems(mount, app_name)
                if problems:
                    print("FAIL: the disk image holds the app and a link to /Applications")
                    print("\n".join(problems))
                    return 1
                print("ok: the disk image holds the app and a link to /Applications")
                if extra:
                    print(f"reported: the disk image also holds {extra}")
                return _check_app(mount / f"{app_name}.app", args, on_mac)
        except VerifyError as exc:
            print(f"verify_app.py: {exc}", file=sys.stderr)
            return 1
    if path.suffix == ".app" and path.is_dir() and not path.is_symlink():
        return _check_app(path, args, on_mac)
    print(f"verify_app.py: {path} is neither an app folder nor a disk image", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
