"""Build the Mac app that runs the host, for Apple silicon Macs, and a disk image that holds it.
The app's name and identifiers are in ``tools/macos/pins.toml``; ``<name>`` below is that name.

    uv run --no-project python tools/macos/build_app.py plan --build-number N   # anywhere
    uv run --no-project python tools/macos/build_app.py wheel --out DIR         # anywhere
    uv run --no-project python tools/macos/build_app.py toolchain               # on a Mac
    uv run --no-project python tools/macos/build_app.py assemble ...            # on a Mac
    uv run --no-project python tools/macos/build_app.py build ...               # on a Mac
    uv run --no-project python tools/macos/build_app.py compare DIR_A DIR_B     # anywhere

``assemble`` and ``build`` take ``--host-wheel P --host-wheel-sha256 H`` (the wheel ``wheel``
built) and exactly one of ``--tag T`` (a build from a ``v*`` tag) or ``--build-number N`` (any
other build; CI passes its run number). Before doing any work they check the pins, the version
rule (:func:`bundle_versions`), the runtime archive's sha256 when ``--python-archive`` is given,
and the host wheel's name and sha256; then that they run on an Apple silicon Mac with the pinned
Xcode. ``assemble`` then writes, under ``--out`` (``build/macos/`` by default, which git
ignores):

1. The Python runtime that the pins name, downloaded unless given, and refused unless its sha256
   is the pinned one, unpacked into ``<name>.app/Contents/Resources/runtime``.
2. The runtime prepared (:func:`prepare_runtime`): its own pip uninstalled by its ``RECORD``, the
   paths ``tools/macos/prune.txt`` lists removed, the interpreter renamed ``lucille-host-runtime``,
   every symbolic link replaced by its target, and the result checked for exactly the expected
   shape: one program, one library folder, an empty ``site-packages``, no ``.pyc``.
3. ``site-packages`` filled by :func:`payload.assemble_site_packages`: the lock's wheels for this
   platform and then the host wheel, installed with hashes required, and checked file by file
   against the wheels. Nothing is precompiled: no ``.pyc`` is shipped, and the app runs the
   interpreter with ``-B``.
4. The menu bar app in ``macos/App/`` compiled with ``swiftc``; ``Info.plist`` (:func:`info_plist`);
   the wrapper ``Resources/bin/lucille-host`` for Terminal users; the licence texts in
   ``Resources/licenses/``; and ``Resources/MANIFEST.json`` (:func:`manifest`).
5. ``<name>-unsigned-macOS-arm64.tar.gz``, the unsigned app as a tarball whose bytes depend on the
   files alone (:func:`payload.write_tarball`), so that two builds can be compared.

``build`` runs ``assemble``, then signs every Mach-O file ad hoc in the order
:func:`signing_plan` gives (the libraries deepest first, then the interpreter with its own
identifier, then the bundle; with the hardened runtime when the pins say so; no entitlements),
and builds ``<name>-<version>-<commit>-adhoc-macOS-arm64.dmg``, which holds the app and a link to
``/Applications``. A signature macOS trusts needs a Developer ID and notarization, which this
script does not do.

``plan`` reads the pins, the build constraints, the prune list and the version, and writes the
``Info.plist`` the app would get, without running any other program. ``toolchain`` checks that the
pinned Xcode is installed and is the one ``xcodebuild`` runs, and with ``--github-env FILE``
writes its ``DEVELOPER_DIR`` there for the later steps of a GitHub Actions job. ``compare`` prints
whether two builds' unsigned tarballs are the same, as Markdown, and always exits 0.

Exit codes: 0 done, 1 refused or failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

if __package__ in (None, ""):  # run as a script: make ``tools.macos`` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.macos import payload  # noqa: E402

# This script and its pins live in tools/macos/; the app's Swift sources stay in macos/.
TOOLS_DIR: Final = Path(__file__).resolve().parent
REPO_ROOT: Final = TOOLS_DIR.parents[1]
MACOS_DIR: Final = REPO_ROOT / "macos"
PINS_FILE: Final = TOOLS_DIR / "pins.toml"
BUILD_CONSTRAINTS: Final = TOOLS_DIR / "build-constraints.txt"
PRUNE_FILE: Final = TOOLS_DIR / "prune.txt"
PYPROJECT: Final = REPO_ROOT / "pyproject.toml"
LOCK_FILE: Final = REPO_ROOT / "uv.lock"
SWIFT_DIR: Final = MACOS_DIR / "App"
VERSION_FILE: Final = REPO_ROOT / "src" / "lucille_host" / "__init__.py"
DEFAULT_OUT: Final = REPO_ROOT / "build" / "macos"
# Every disk image this script makes starts with the app's name and a hyphen, and ends with
# DMG_SUFFIX. A release's image, made elsewhere with a Developer ID signature, will not end so.
DMG_SUFFIX: Final = "-adhoc-macOS-arm64.dmg"
# The unsigned app, as a tarball: the app's name, then this.
UNSIGNED_SUFFIX: Final = "-unsigned-macOS-arm64.tar.gz"

PBS_RELEASES: Final = "https://github.com/astral-sh/python-build-standalone/releases/download/"
# Folder and file names inside the runtime. The pins allow Python 3.13 only, so these are fixed.
STDLIB: Final = "lib/python3.13"
SITE_PACKAGES: Final = f"{STDLIB}/site-packages"
UPSTREAM_INTERPRETER: Final = "bin/python3.13"
INTERPRETER: Final = "bin/lucille-host-runtime"
# Where the runtime and the rest of the payload are in the app (0002 §6).
RUNTIME_DIR: Final = "Contents/Resources/runtime"
RUNTIME_IN_APP: Final = f"{RUNTIME_DIR}/{INTERPRETER}"
WRAPPER_IN_APP: Final = "Contents/Resources/bin/lucille-host"
LICENSES_IN_APP: Final = "Contents/Resources/licenses"
MANIFEST_PATH: Final = "Contents/Resources/MANIFEST.json"
# How the app, the wrapper and the checks run the interpreter (0002 §6): -I ignores every
# PYTHON* variable and the user's site folder, -B writes no .pyc into the signed bundle, and
# -X utf8 makes the text encoding UTF-8 whatever the locale.
RUNTIME_FLAGS: Final = ("-I", "-B", "-X", "utf8")
# The profile the app runs every verb on (0002 §9), apart from the ``default`` profile of a
# command-line install. The Swift app names it too (``HostCLI.appProfile``).
APP_PROFILE: Final = "mac-app"
# The wrapper for Terminal users: it finds the bundle from its own real path, so a link to it from
# a folder on PATH still runs this bundle's interpreter, never one next to the link. It runs the
# host as the app does, with the app's flags and on the app's profile; a --profile given to it
# comes later, so it replaces that one.
WRAPPER_TEXT: Final = (
    "#!/bin/sh\n"
    'self=$(/bin/realpath -- "$0") || exit 1\n'
    'exec "${self%/*}/../runtime/bin/lucille-host-runtime" '
    + " ".join(RUNTIME_FLAGS)
    + f' -m lucille_host --profile {APP_PROFILE} "$@"\n'
)
# The distributions of the runtime itself that the build uninstalls, by their RECORD, before any
# wheel is installed: pip is the runtime's own installer and is in no locked wheel. It is the only
# *.dist-info folder the build removes; prune.txt never names one.
RUNTIME_DISTRIBUTIONS_REMOVED: Final = ("pip",)
# The Info.plist keys that hold folder_usage(): Documents, Desktop, Downloads, removable and
# network volumes, and file providers, the places macOS asks about before the host may read them.
# Each name is Apple's (developer.apple.com, Bundle Resources, Information Property List).
FOLDER_USAGE_KEYS: Final = (
    "NSDocumentsFolderUsageDescription",
    "NSDesktopFolderUsageDescription",
    "NSDownloadsFolderUsageDescription",
    "NSRemovableVolumesUsageDescription",
    "NSNetworkVolumesUsageDescription",
    "NSFileProviderDomainUsageDescription",
)
# Keys an Info.plist of this app must never carry: a scripting surface, Services, network
# exceptions, an update feed, or a URL scheme (0002 §11.5: none until the release that adds the
# nonce check of §11.4).
FORBIDDEN_PLIST_KEYS: Final = frozenset(
    {
        "NSAppleScriptEnabled",
        "OSAScriptingDefinition",
        "NSServices",
        "NSAppTransportSecurity",
        "SUFeedURL",
        "SUPublicEDKey",
        "CFBundleURLTypes",
        "CFBundleURLSchemes",
    }
)
# The tables of pins.toml, each with its keys.
PIN_TABLES: Final = {
    "python": frozenset({"version", "asset", "url", "sha256"}),
    "app": frozenset({"name", "bundle_id", "runtime_id", "deployment_target", "category"}),
    "xcode": frozenset({"version"}),
    "ripgrep": frozenset({"commit"}),
    "signing": frozenset({"adhoc_hardened_runtime"}),
}

_ASSET = re.compile(
    r"cpython-(?P<version>3\.13\.\d+)\+(?P<release>\d{8})-aarch64-apple-darwin-"
    r"install_only(?:_stripped)?\.tar\.gz"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
# The app's name becomes a file name, a volume name and part of a glob pattern, so it is only
# letters and digits, with single spaces between words.
_APP_NAME = re.compile(r"[A-Za-z0-9]+(?: [A-Za-z0-9]+)*")
_BUNDLE_ID = re.compile(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*){2,}")
_MACOS_VERSION = re.compile(r"(?P<major>\d+)\.\d+")
_XCODE_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
_DEVELOPER_DIR = re.compile(r"/Applications/Xcode_\d+\.\d+(?:\.\d+)?\.app/Contents/Developer")
_VERSION_LINE = re.compile(r'^__version__ = "([^"\n]+)"$', re.MULTILINE)
_RELEASE = re.compile(r"\d+(?:\.\d+){0,2}")
# A version a tag build may carry: three numbers, nothing after them.
_FINAL_RELEASE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")
_BUILD_NUMBER = re.compile(r"[1-9]\d{0,17}")
_COMMIT = re.compile(r"[0-9a-f]{7,40}")
# One exact requirement, ``name==version``, as pyproject.toml and build-constraints.txt write it.
_EXACT = re.compile(r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)==(?P<version>[^\s;,]+)")
# The first four bytes of a Mach-O file: thin 64- and 32-bit, either byte order, and universal.
_MACHO_MAGIC: Final = frozenset(
    {
        b"\xcf\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xce",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    }
)


class PinError(ValueError):
    """pins.toml, build-constraints.txt or prune.txt is not well formed."""


class BuildError(RuntimeError):
    """The build cannot go on; the message says why."""


@dataclass(frozen=True)
class Pins:
    """What pins.toml pins."""

    python_version: str
    asset: str
    url: str
    sha256: str
    app_name: str
    bundle_id: str
    runtime_id: str
    deployment_target: str
    category: str
    xcode: str
    ripgrep_commit: str
    adhoc_hardened_runtime: bool


def _table(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    keys = PIN_TABLES[name]
    table = data.get(name)
    if not isinstance(table, dict) or set(table) != keys:
        raise PinError(f"[{name}] holds exactly these keys: {', '.join(sorted(keys))}")
    for key in sorted(keys):
        value = table[key]
        if name == "signing":
            if not isinstance(value, bool):
                raise PinError(f"[{name}] {key} must be true or false")
        elif name == "ripgrep":
            if not isinstance(value, str):
                raise PinError(f"[{name}] {key} must be a string")
        elif not isinstance(value, str) or not value:
            raise PinError(f"[{name}] {key} must be a non-empty string")
    return table


def parse_pins(text: str) -> Pins:
    """The pins in ``text`` (the contents of pins.toml), checked for consistency."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PinError(f"pins.toml does not parse: {exc}") from exc
    if set(data) != set(PIN_TABLES):
        raise PinError(
            "pins.toml holds exactly these tables: "
            + ", ".join(f"[{name}]" for name in sorted(PIN_TABLES))
        )
    python = _table(data, "python")
    app = _table(data, "app")
    xcode = _table(data, "xcode")
    ripgrep = _table(data, "ripgrep")
    signing = _table(data, "signing")

    match = _ASSET.fullmatch(python["asset"])
    if match is None:
        raise PinError(
            "[python] asset must name a python-build-standalone CPython 3.13 build for "
            "aarch64-apple-darwin, flavour install_only or install_only_stripped"
        )
    if match["version"] != python["version"]:
        raise PinError(
            f"[python] version is {python['version']}, but the asset holds Python "
            f"{match['version']}"
        )
    url = f"{PBS_RELEASES}{match['release']}/{python['asset'].replace('+', '%2B')}"
    if python["url"] != url:
        raise PinError(f"[python] url must be the asset's release URL, {url}")
    if _SHA256.fullmatch(python["sha256"]) is None:
        raise PinError("[python] sha256 must be 64 lower-case hexadecimal digits")

    if _APP_NAME.fullmatch(app["name"]) is None:
        raise PinError("[app] name must be letters and digits, with single spaces between words")
    if _BUNDLE_ID.fullmatch(app["bundle_id"]) is None:
        raise PinError("[app] bundle_id must be a lower-case reverse-DNS name")
    if app["runtime_id"] != app["bundle_id"] + ".runtime":
        raise PinError("[app] runtime_id must be the bundle_id followed by .runtime")
    macos = _MACOS_VERSION.fullmatch(app["deployment_target"])
    if macos is None or int(macos["major"]) < 15:
        raise PinError("[app] deployment_target must be 15.0 or later (0002 §5: macOS 15 or later)")
    if not app["category"].startswith("public.app-category."):
        raise PinError("[app] category must be a public.app-category.* value")
    if _XCODE_VERSION.fullmatch(xcode["version"]) is None:
        raise PinError("[xcode] version must be numbers, such as 16.4")
    if ripgrep["commit"] and _GIT_COMMIT.fullmatch(ripgrep["commit"]) is None:
        raise PinError("[ripgrep] commit must be empty or 40 lower-case hexadecimal digits")
    return Pins(
        python_version=python["version"],
        asset=python["asset"],
        url=python["url"],
        sha256=python["sha256"],
        app_name=app["name"],
        bundle_id=app["bundle_id"],
        runtime_id=app["runtime_id"],
        deployment_target=app["deployment_target"],
        category=app["category"],
        xcode=xcode["version"],
        ripgrep_commit=ripgrep["commit"],
        adhoc_hardened_runtime=signing["adhoc_hardened_runtime"],
    )


def load_pins(path: Path = PINS_FILE) -> Pins:
    return parse_pins(path.read_text(encoding="utf-8"))


def _pins_name() -> str:
    return PINS_FILE.relative_to(REPO_ROOT).as_posix()


def host_version(path: Path = VERSION_FILE) -> str:
    """The host's ``__version__``, read from its source (the one place pyproject.toml reads it)."""
    found = _VERSION_LINE.findall(path.read_text(encoding="utf-8"))
    if len(found) != 1:
        raise BuildError(f"{path} must assign __version__ exactly once")
    return found[0]


def release_triple(version: str) -> str:
    """The three numbers Apple's CFBundleShortVersionString takes: the release part of a PEP 440
    version, padded with zeros. ``0.1.0.dev0`` gives ``0.1.0``."""
    match = _RELEASE.match(version)
    if match is None:
        raise BuildError(f"{version!r} does not start with a release number")
    parts = match.group(0).split(".")
    return ".".join(parts + ["0"] * (3 - len(parts)))


@dataclass(frozen=True)
class BundleVersions:
    """The app's two version strings: CFBundleShortVersionString and CFBundleVersion."""

    short: str
    build: str


def bundle_versions(version: str, tag: str | None, build_number: str | None) -> BundleVersions:
    """The version rule (0002 §7). A build from a ``v*`` tag passes ``tag``: the tag must be
    ``v`` and the host's ``__version__``, which must be three numbers and nothing else (no
    ``.devN``, ``rcN``, ``.postN`` or ``+local``), and both strings are that version. Any other
    build passes ``build_number`` (CI's run number): the short version is the release part of
    ``__version__`` and the bundle version is that number. Exactly one of the two is given."""
    if tag is not None and build_number is not None:
        raise BuildError("pass --tag or --build-number, not both")
    if tag is not None:
        if _FINAL_RELEASE.fullmatch(version) is None:
            raise BuildError(
                "a build from a tag needs a release version of three numbers with no suffix; "
                f"__version__ is {version!r}"
            )
        if tag != f"v{version}":
            raise BuildError(f"the tag is {tag!r}, but __version__ is {version!r} (tag v{version})")
        return BundleVersions(version, version)
    if build_number is None:
        raise BuildError("pass --build-number (CI passes the run number) or --tag")
    if _BUILD_NUMBER.fullmatch(build_number) is None:
        raise BuildError(f"the build number must be a positive whole number, not {build_number!r}")
    return BundleVersions(release_triple(version), build_number)


def folder_usage(app_name: str) -> str:
    """What the bundle's Info.plist says for macOS to show when it asks the user about a protected
    folder: what the app is for, in the sentences the app opens with (HostText.appPurpose). The
    second sentence is there because host.describe is on by default. macOS is meant to show this
    under its own question; whether it does, and whether that question names the app or the
    interpreter, is not yet tested on a Mac (macos/README.md, "What CI does not check")."""
    return (
        f"{app_name} lets Lucille use only the folders you choose on this Mac, and only for the "
        "abilities you turn on. Lucille can also see which abilities are on, and the names you "
        "gave your folders."
    )


def info_plist(pins: Pins, versions: BundleVersions) -> dict[str, Any]:
    """The app's Info.plist (0002 §6-§12): a menu bar app with no Dock icon, for Apple silicon on
    the pinned macOS or later, that macOS must not quit on its own, with the six folder usage
    texts. MANIFEST.json, not this file, records the host's exact version."""
    usage = dict.fromkeys(FOLDER_USAGE_KEYS, folder_usage(pins.app_name))
    return {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": pins.app_name,
        "CFBundleExecutable": pins.app_name,
        "CFBundleIdentifier": pins.bundle_id,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": pins.app_name,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": versions.short,
        "CFBundleVersion": versions.build,
        "LSApplicationCategoryType": pins.category,
        "LSArchitecturePriority": ["arm64"],
        "LSMinimumSystemVersion": pins.deployment_target,
        "LSUIElement": True,
        "NSHumanReadableCopyright": "MIT License",
        "NSSupportsAutomaticTermination": False,
        "NSSupportsSuddenTermination": False,
        **usage,
    }


def dmg_name(app_name: str, version: str, commit: str) -> str:
    """The disk image's file name. It names the commit it was built from and says that its
    signature is ad hoc, so that a build from CI cannot be taken for a release asset."""
    if _COMMIT.fullmatch(commit) is None:
        raise BuildError(f"{commit!r} is not an abbreviated commit id")
    return f"{app_name}-{version}-{commit}{DMG_SUFFIX}"


def disk_images(out: Path, app_name: str) -> list[Path]:
    """The disk images this script has made in ``out``."""
    return sorted(out.glob(f"{glob.escape(app_name)}-*{DMG_SUFFIX}"))


def unsigned_tarball_name(app_name: str) -> str:
    return f"{app_name}{UNSIGNED_SUFFIX}"


def _normal_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_build_constraints(text: str) -> dict[str, tuple[str, tuple[str, ...]]]:
    """``{name: (version, sha256s)}`` for each requirement of build-constraints.txt. Each one is
    ``name==version`` with at least one ``--hash=sha256:...``, and no name appears twice."""
    found: dict[str, tuple[str, tuple[str, ...]]] = {}
    for raw in text.replace("\\\n", " ").splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        head, *options = line.split()
        match = _EXACT.fullmatch(head)
        if match is None:
            raise PinError(f"build-constraints.txt: {head!r} is not name==version")
        hashes = []
        for option in options:
            digest = option.removeprefix("--hash=sha256:")
            if digest == option or _SHA256.fullmatch(digest) is None:
                raise PinError(f"build-constraints.txt: {option!r} is not --hash=sha256:<hex>")
            hashes.append(digest)
        if not hashes:
            raise PinError(f"build-constraints.txt: {head} has no --hash")
        name = _normal_name(match["name"])
        if name in found:
            raise PinError(f"build-constraints.txt: {name} appears twice")
        found[name] = (match["version"], tuple(hashes))
    return found


def build_constraint_problems(
    requires: Sequence[str], constraints: Mapping[str, tuple[str, tuple[str, ...]]]
) -> list[str]:
    """How the hashed build constraints differ from pyproject.toml's ``[build-system] requires``:
    each requirement there must be exact, and the two must name the same versions."""
    problems = []
    wanted: dict[str, str] = {}
    for requirement in requires:
        match = _EXACT.fullmatch(requirement.strip())
        if match is None:
            problems.append(f"the build requirement {requirement!r} is not pinned with ==")
            continue
        wanted[_normal_name(match["name"])] = match["version"]
    for name, version in sorted(wanted.items()):
        if name not in constraints:
            problems.append(f"{name}=={version} has no line in build-constraints.txt")
        elif constraints[name][0] != version:
            problems.append(
                f"build-constraints.txt pins {name}=={constraints[name][0]}, but pyproject.toml "
                f"requires {name}=={version}"
            )
    for name in sorted(set(constraints) - set(wanted)):
        problems.append(f"build-constraints.txt pins {name}, which pyproject.toml does not require")
    return problems


def load_build_constraints(
    constraints_file: Path = BUILD_CONSTRAINTS, pyproject: Path = PYPROJECT
) -> dict[str, tuple[str, tuple[str, ...]]]:
    """The hashed build constraints, checked against pyproject.toml."""
    constraints = parse_build_constraints(constraints_file.read_text(encoding="utf-8"))
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    requires = data.get("build-system", {}).get("requires", [])
    problems = build_constraint_problems(requires, constraints)
    if problems:
        raise PinError(
            "the build constraints do not match pyproject.toml:\n  " + "\n  ".join(problems)
        )
    return constraints


def parse_prune_list(text: str) -> tuple[str, ...]:
    """The paths in ``text`` (the contents of prune.txt), relative to the runtime's top folder,
    in order. A line that is empty or starts with ``#`` is skipped. Refused: a pattern (``*``,
    ``?``, ``[``), an absolute path, ``.`` or ``..``, a backslash or ``#`` inside a path, a path
    given twice, the interpreter or a folder that holds it or ``site-packages``, a
    ``*.dist-info`` folder or anything in one, and anything in ``site-packages`` but its
    ``README.txt``."""
    found: list[str] = []
    keep = (UPSTREAM_INTERPRETER, SITE_PACKAGES)
    for number, raw in enumerate(text.splitlines(), start=1):
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        where = f"prune.txt:{number}: {entry!r}"
        if any(char in entry for char in "*?[]\\#"):
            raise PinError(f"{where}: one literal path per line, with no pattern or comment")
        parts = entry.split("/")
        if entry.startswith("/") or any(part in ("", ".", "..") for part in parts):
            raise PinError(f"{where}: a path relative to the runtime's top folder, with no . or ..")
        if entry in found:
            raise PinError(f"{where}: listed twice")
        if any(path == entry or path.startswith(entry + "/") for path in keep):
            raise PinError(f"{where}: the build keeps this, or a folder of what it keeps")
        if any(part.endswith(".dist-info") for part in parts):
            raise PinError(f"{where}: a *.dist-info folder is never pruned")
        if entry.startswith(SITE_PACKAGES + "/") and entry != f"{SITE_PACKAGES}/README.txt":
            raise PinError(f"{where}: site-packages is emptied by uninstalling, not by pruning")
        found.append(entry)
    return tuple(found)


def load_prune_list(path: Path = PRUNE_FILE) -> tuple[str, ...]:
    return parse_prune_list(path.read_text(encoding="utf-8"))


def sha256_of(path: Path) -> str:
    return payload.sha256_of(path)


def check_sha256(path: Path, expected: str) -> None:
    """Refuse a file whose sha256 is not the pinned one."""
    actual = sha256_of(path)
    if actual != expected:
        raise BuildError(
            f"{path.name}: its sha256 is {actual}, but {_pins_name()} pins {expected}; "
            "refusing to build"
        )


def check_host_wheel(path: Path, expected: str, version: str) -> None:
    """Refuse a host wheel that is not this version's, or whose sha256 is not ``expected``."""
    name = payload.host_wheel_name(version)
    if path.name != name or not path.is_file():
        raise BuildError(f"the host wheel must be a file named {name}; refusing to build")
    if _SHA256.fullmatch(expected) is None:
        raise BuildError("--host-wheel-sha256 must be 64 lower-case hexadecimal digits")
    actual = sha256_of(path)
    if actual != expected:
        raise BuildError(
            f"{path.name}: its sha256 is {actual}, not the {expected} given; refusing to build"
        )


def fetch_runtime(pins: Pins, cache: Path, archive: Path | None = None) -> Path:
    """The pinned runtime archive: ``archive`` if given, else a download into ``cache``. Either
    way it is used only if its sha256 matches the pin; a download that does not is deleted."""
    if archive is not None:
        check_sha256(archive, pins.sha256)
        return archive
    target = cache / pins.asset
    if not target.exists():
        cache.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        print(f"downloading {pins.url}", flush=True)
        with urllib.request.urlopen(pins.url, timeout=300) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out)
        partial.replace(target)
    try:
        check_sha256(target, pins.sha256)
    except BuildError:
        target.unlink()
        raise
    return target


def is_macho(path: Path) -> bool:
    """Whether ``path`` is a regular file that starts like a Mach-O file."""
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as handle:
        return handle.read(4) in _MACHO_MAGIC


def macho_files(root: Path) -> list[Path]:
    """Every Mach-O file under ``root``, the most deeply nested first: the order to sign them."""
    found = [path for path in root.rglob("*") if is_macho(path)]
    return sorted(found, key=lambda path: (-len(path.parts), str(path)))


def symlinks_in(root: Path) -> list[Path]:
    """Every symbolic link under ``root``, without following any."""
    links = []
    for folder, dirs, files in os.walk(root):
        links.extend(
            Path(folder) / name for name in (*dirs, *files) if (Path(folder) / name).is_symlink()
        )
    return sorted(links)


def bytecode_in(root: Path) -> list[Path]:
    """Every ``.pyc`` or ``.pyo`` file and every ``__pycache__`` folder under ``root``."""
    found = []
    for folder, dirs, files in os.walk(root):
        found += [Path(folder) / name for name in dirs if name == "__pycache__"]
        found += [Path(folder) / name for name in files if name.endswith((".pyc", ".pyo"))]
    return sorted(found)


def _run(argv: list[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None) -> None:
    print("+ " + shlex.join(argv), flush=True)
    subprocess.run(argv, check=True, cwd=cwd, env=None if env is None else dict(env))


# hdiutil fails now and then on a busy machine, while the disk-image helper is still at work on
# an earlier call: "Resource busy" or "Resource temporarily unavailable" (EAGAIN). Both pass if
# the same call is made again a few seconds later.
HDIUTIL_BUSY: Final = ("Resource busy", "Resource temporarily unavailable")
HDIUTIL_TRIES: Final = 4


def hdiutil(args: Sequence[str], *, sleep: Any = time.sleep) -> subprocess.CompletedProcess[str]:
    """Run ``hdiutil`` with ``args`` and return how it ended, output captured as text. A call
    that fails because the machine is busy is made again, up to ``HDIUTIL_TRIES`` times in all,
    waiting 5, 10 and 15 s between them; any other failure is returned at once."""
    argv = ["hdiutil", *args]
    for attempt in range(1, HDIUTIL_TRIES + 1):
        print("+ " + shlex.join(argv), flush=True)
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        busy = done.returncode != 0 and any(word in done.stderr for word in HDIUTIL_BUSY)
        if not busy or attempt == HDIUTIL_TRIES:
            return done
        said = done.stderr.strip()
        print(f"hdiutil is busy; trying again (try {attempt} of {HDIUTIL_TRIES}): {said}")
        sleep(5 * attempt)
    raise AssertionError("unreachable")


def _output(
    argv: list[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None
) -> str:
    done = subprocess.run(
        argv,
        check=True,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=None if env is None else dict(env),
    )
    return done.stdout.strip()


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def build_wheel_argv(out: Path) -> list[str]:
    """``uv build`` for the host's wheel: its build backend comes only from the files
    build-constraints.txt names by hash. No uv configuration file is read, since one could
    turn build isolation off; :func:`build_wheel` removes the ``UV_*`` variables too."""
    return [
        "uv",
        "build",
        "--wheel",
        "--build-constraints",
        str(BUILD_CONSTRAINTS),
        "--require-hashes",
        "--no-config",
        "--out-dir",
        str(out),
        str(REPO_ROOT),
    ]


def build_wheel(out: Path, version: str) -> Path:
    """The host's wheel, built from this checkout into ``out``, with no uv setting from the
    environment (:func:`payload.without_uv_settings`). Any older host wheel there is removed
    first, so that ``out`` then holds exactly one."""
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("lucille_host-*.whl"):
        old.unlink()
    _run(build_wheel_argv(out), env=payload.without_uv_settings(os.environ))
    wheels = sorted(out.glob("*.whl"))
    expected = payload.host_wheel_name(version)
    if [wheel.name for wheel in wheels] != [expected]:
        raise BuildError(f"expected exactly {expected} from uv build, got {wheels}")
    return wheels[0]


def export_requirements_argv() -> list[str]:
    """``uv export`` of the runtime dependencies uv.lock pins, with every hash, to stdout. No
    uv configuration file is read."""
    return [
        "uv",
        "export",
        "--no-config",
        "--frozen",
        "--no-dev",
        "--no-editable",
        "--no-emit-project",
        "--format",
        "requirements-txt",
    ]


def export_requirements_text() -> str:
    """The runtime dependencies uv.lock pins, one per line with their markers and hashes."""
    print("+ " + shlex.join(export_requirements_argv()), flush=True)
    env = payload.without_uv_settings(os.environ)
    return _output(export_requirements_argv(), cwd=REPO_ROOT, env=env) + "\n"


def unpack_runtime(archive: Path, contents: Path) -> Path:
    """Unpack the runtime into ``Contents/Resources/runtime``; returns that folder."""
    resources = contents / "Resources"
    staging = resources / "unpack"
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(staging, filter="data")
    top = staging / "python"
    if not (top / UPSTREAM_INTERPRETER).is_file():
        raise BuildError(f"{archive.name} has no python/{UPSTREAM_INTERPRETER}")
    runtime = contents / RUNTIME_DIR.removeprefix("Contents/")
    top.rename(runtime)
    staging.rmdir()
    return runtime


def prune_runtime(runtime: Path, entries: Sequence[str]) -> list[str]:
    """Remove each path of the prune list from the runtime. Every one must be there: a list that
    names something the pinned runtime does not hold is out of date, and refused."""
    for entry in entries:
        path = runtime / entry
        if not (path.is_symlink() or path.exists()):
            raise BuildError(f"prune.txt names {entry}, which the pinned runtime does not hold")
    for entry in entries:
        _remove(runtime / entry)
    return list(entries)


def replace_symlinks(runtime: Path) -> list[str]:
    """Replace each symbolic link in the runtime that points to a regular file inside it with a
    copy of that file, mode included. Any other link (to a folder, outside the runtime, or to
    nothing) is refused. Returns the links replaced."""
    top = Path(os.path.realpath(runtime))
    replaced = []
    for link in symlinks_in(runtime):
        target = Path(os.path.realpath(link))
        if top not in target.parents or not target.is_file():
            raise BuildError(
                f"{link.relative_to(runtime)} links to {os.readlink(link)}, which is not a file "
                "inside the runtime"
            )
        link.unlink()
        shutil.copyfile(target, link)
        shutil.copymode(target, link)
        replaced.append(link.relative_to(runtime).as_posix())
    return replaced


def runtime_shape_problems(runtime: Path) -> list[str]:
    """How the prepared runtime differs from what the app ships: ``bin/`` holds only the renamed
    interpreter, ``lib/`` only ``python3.13/``, ``site-packages`` is empty, and there is no other
    top-level entry, no symbolic link and no ``.pyc``. Anything the prune list missed is reported
    here, not deleted."""
    problems = []

    def names(folder: Path) -> set[str]:
        return {entry.name for entry in folder.iterdir()} if folder.is_dir() else set()

    if names(runtime) != {"bin", "lib"}:
        problems.append(f"the runtime's top folder holds {sorted(names(runtime))}, not bin and lib")
    if names(runtime / "bin") != {Path(INTERPRETER).name}:
        problems.append(f"bin/ holds {sorted(names(runtime / 'bin'))}, not only the interpreter")
    interpreter = runtime / INTERPRETER
    if interpreter.is_symlink() or not interpreter.is_file():
        problems.append(f"{INTERPRETER} is not a file")
    if names(runtime / "lib") != {"python3.13"}:
        problems.append(f"lib/ holds {sorted(names(runtime / 'lib'))}, not only python3.13")
    site = runtime / SITE_PACKAGES
    if not site.is_dir():
        problems.append(f"{SITE_PACKAGES} is missing")
    elif names(site):
        problems.append(f"{SITE_PACKAGES} is not empty: {sorted(names(site))}")
    problems += [f"symbolic link: {link.relative_to(runtime)}" for link in symlinks_in(runtime)]
    problems += [f"bytecode: {path.relative_to(runtime)}" for path in bytecode_in(runtime)]
    return problems


def prepare_runtime(runtime: Path, entries: Sequence[str]) -> list[str]:
    """Make the unpacked runtime the app's: uninstall the runtime's own distributions
    (:data:`RUNTIME_DISTRIBUTIONS_REMOVED`, pip) by their ``RECORD``, remove the prune list's
    paths, rename the interpreter ``lucille-host-runtime``, replace each symbolic link by its
    target, and refuse unless :func:`runtime_shape_problems` finds nothing. Returns what was
    removed, sorted: each uninstalled distribution as ``name==version``, and each pruned path."""
    site = runtime / SITE_PACKAGES
    removed = []
    for name in RUNTIME_DISTRIBUTIONS_REMOVED:
        try:
            removed.append(payload.uninstall_by_record(runtime, site, name))
        except payload.AssemblyError as exc:
            raise BuildError(str(exc)) from exc
    removed += prune_runtime(runtime, entries)
    (runtime / UPSTREAM_INTERPRETER).rename(runtime / INTERPRETER)
    replace_symlinks(runtime)
    problems = runtime_shape_problems(runtime)
    if problems:
        raise BuildError("the runtime is not ready for the app:\n  " + "\n  ".join(problems))
    return sorted(removed)


def developer_dir(pins: Pins) -> str:
    """Where GitHub's macOS images keep the pinned Xcode's developer folder."""
    path = f"/Applications/Xcode_{pins.xcode}.app/Contents/Developer"
    if _DEVELOPER_DIR.fullmatch(path) is None:
        raise PinError(f"{path!r} is not an Xcode developer folder")
    return path


def toolchain_problems(pins: Pins, xcodebuild_version: str) -> list[str]:
    """What is wrong with the Xcode that ``xcodebuild -version`` described: its first line must be
    ``Xcode`` and the pinned version."""
    lines = xcodebuild_version.strip().splitlines()
    first = lines[0].strip() if lines else ""
    if first != f"Xcode {pins.xcode}":
        return [
            f"xcodebuild runs {first or 'nothing'!r}, but {_pins_name()} pins Xcode {pins.xcode}"
        ]
    return []


def xcode_env(pins: Pins, environ: Mapping[str, str]) -> dict[str, str]:
    """``environ`` with ``DEVELOPER_DIR`` set: the one it already names (a local build's own
    choice), or the pinned Xcode's. Refused unless ``xcodebuild -version``, run with that
    environment, reports the pinned version."""
    env = dict(environ)
    env["DEVELOPER_DIR"] = environ.get("DEVELOPER_DIR") or developer_dir(pins)
    text = _output(["xcodebuild", "-version"], env=env)
    problems = toolchain_problems(pins, text)
    if problems:
        raise BuildError("; ".join(problems))
    print(f"Xcode {pins.xcode} at {env['DEVELOPER_DIR']}")
    return env


def require_apple_silicon(what: str) -> None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise BuildError(f"{what} runs on an Apple silicon Mac; `plan` runs anywhere")


def select_toolchain(pins: Pins, github_env: Path | None) -> None:
    """Check that the pinned Xcode is installed and is the one ``xcodebuild`` runs with its
    ``DEVELOPER_DIR``, and append that ``DEVELOPER_DIR`` to ``github_env`` (``$GITHUB_ENV``) for
    the job's later steps."""
    require_apple_silicon("toolchain")
    path = developer_dir(pins)
    if not Path(path).is_dir():
        raise BuildError(f"{path} does not exist: Xcode {pins.xcode} is not installed")
    environ = {key: value for key, value in os.environ.items() if key != "DEVELOPER_DIR"}
    xcode_env(pins, {**environ, "DEVELOPER_DIR": path})
    if github_env is not None:
        line = f"DEVELOPER_DIR={path}"
        if re.fullmatch(r"DEVELOPER_DIR=" + _DEVELOPER_DIR.pattern, line) is None:
            raise BuildError(f"refusing to write {line!r}")
        with github_env.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        print(f"wrote {line} to {github_env}")


def compile_app(executable: Path, deployment_target: str, env: Mapping[str, str]) -> None:
    sources = sorted(str(path) for path in SWIFT_DIR.glob("*.swift"))
    if not sources:
        raise BuildError(f"no Swift sources in {SWIFT_DIR}")
    _run(
        [
            "xcrun",
            "swiftc",
            "-parse-as-library",
            "-O",
            "-target",
            f"arm64-apple-macos{deployment_target}",
            "-o",
            str(executable),
            *sources,
        ],
        env=env,
    )


def signing_identifier(app: Path, path: Path, pins: Pins) -> str:
    """The signing identifier of a Mach-O file in the app: the pin's runtime identifier for the
    interpreter; for any other file, the bundle identifier, a dot, and the file's path (from the
    runtime's ``lib/python3.13/`` if it is there, else from ``Contents/``) with ``/`` as ``.``,
    without a last ``.so`` or ``.dylib``, and any character but letters, digits, ``.``, ``_`` and
    ``-`` as ``-``. codesign's ``--prefix`` is not used: it adds the prefix only to a derived
    identifier with no dot in it, and ``_rust.abi3`` has one."""
    if path == app / RUNTIME_IN_APP:
        return pins.runtime_id
    stdlib = app / RUNTIME_DIR / STDLIB
    base = stdlib if stdlib in path.parents else app / "Contents"
    parts = list(path.relative_to(base).parts)
    if path.suffix in (".so", ".dylib"):
        parts[-1] = parts[-1].removesuffix(path.suffix)
    name = re.sub(r"[^A-Za-z0-9._-]", "-", ".".join(parts))
    return f"{pins.bundle_id}.{name}"


def signing_plan(app: Path, pins: Pins) -> list[list[str]]:
    """The ``codesign`` calls that sign the app ad hoc, in order (0002 §7): every Mach-O file but
    the app's executable and the interpreter, the most deeply nested first, each with its own
    identifier; then the interpreter, with the pin's runtime identifier; then the bundle, which
    signs its executable. ``--options runtime`` (the hardened runtime) on every call or on none,
    as the pins say. Never ``--deep``, no entitlements, no timestamp."""
    main = app / "Contents" / "MacOS" / pins.app_name
    interpreter = app / RUNTIME_IN_APP
    options = ["--options", "runtime"] if pins.adhoc_hardened_runtime else []
    common = ["codesign", "--force", *options, "--timestamp=none", "--sign", "-"]
    plan = []
    for path in macho_files(app):
        if path not in (main, interpreter):
            plan.append([*common, "--identifier", signing_identifier(app, path, pins), str(path)])
    plan.append([*common, "--identifier", pins.runtime_id, str(interpreter)])
    plan.append([*common, str(app)])
    return plan


def sign_app(app: Path, pins: Pins) -> None:
    """Sign the app as :func:`signing_plan` says, then verify the signature, deep and strict."""
    for argv in signing_plan(app, pins):
        _run(argv)
    _run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])


def write_wrapper(app: Path) -> Path:
    """The three-line ``sh`` wrapper for Terminal users, at ``Resources/bin/lucille-host``."""
    wrapper = app / WRAPPER_IN_APP
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(WRAPPER_TEXT, encoding="ascii")
    wrapper.chmod(0o755)
    return wrapper


def copy_licenses(dest: Path, site: Path, runtime_license: Path) -> list[str]:
    """Copy the licence texts into ``dest``, one folder per distribution: ``CPython`` for the
    runtime's ``LICENSE.txt``, and each installed distribution's own files
    (:func:`payload.license_files`), by their path in its ``licenses/`` folder or their name.
    Returns the files written, relative to ``dest``."""
    copies = {f"CPython/{runtime_license.name}": runtime_license}
    for name, files in sorted(payload.license_files(site).items()):
        for path in files:
            info = next(parent for parent in path.parents if parent.name.endswith(".dist-info"))
            rel = path.relative_to(info)
            if rel.parts[0] == "licenses":
                rel = rel.relative_to("licenses")
            copies[f"{name}/{rel.as_posix()}"] = path
    for rel, source in sorted(copies.items()):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o644)
    return sorted(copies)


def manifest(
    pins: Pins,
    versions: BundleVersions,
    assembly: payload.Assembly,
    *,
    commit: str,
    tag: str | None,
    build_number: str | None,
    swiftc: str,
    removed: Sequence[str],
) -> dict[str, Any]:
    """MANIFEST.json (0002 §6): the app's identity and version strings; the commit, the version
    inputs and the tools; the host's real ``__version__`` and its wheel's sha256; the runtime's
    archive and what the build removed from it; and every installed wheel, with its file, URL and
    sha256. No time and no signature: it describes the unsigned payload."""
    return {
        "schema": 1,
        "app": {
            "name": pins.app_name,
            "bundle_id": pins.bundle_id,
            "runtime_id": pins.runtime_id,
            "deployment_target": pins.deployment_target,
            "short_version": versions.short,
            "bundle_version": versions.build,
        },
        "build": {
            "commit": commit,
            "tag": tag,
            "build_number": build_number,
            "xcode": pins.xcode,
            "swiftc": swiftc,
        },
        "host": {
            "version": assembly.host.version,
            "wheel": {"file": assembly.host.filename, "sha256": assembly.host.sha256},
        },
        "runtime": {
            "version": pins.python_version,
            "asset": pins.asset,
            "url": pins.url,
            "sha256": pins.sha256,
            "removed": sorted(removed),
        },
        "wheels": payload.manifest_wheels(
            payload.installed_versions(assembly.site), assembly.wheels
        ),
        "ripgrep": {"commit": pins.ripgrep_commit} if pins.ripgrep_commit else None,
    }


def manifest_text(data: Mapping[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def make_dmg(app: Path, dmg: Path, volume: str, work: Path) -> None:
    """A compressed disk image holding the app and a link to /Applications to drag it to."""
    staging = work / "dmg"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    _run(["ditto", str(app), str(staging / app.name)])
    (staging / "Applications").symlink_to("/Applications")
    if dmg.exists():
        dmg.unlink()
    create = [
        "create",
        "-volname",
        volume,
        "-srcfolder",
        str(staging),
        "-format",
        "UDZO",
        "-ov",
        str(dmg),
    ]
    for args, what in ((create, "create"), (["verify", str(dmg)], "verify")):
        done = hdiutil(args)
        print(done.stdout, end="", flush=True)
        if done.returncode != 0:
            raise BuildError(f"hdiutil {what} failed: {done.stderr.strip()}")


def plan(out: Path, tag: str | None, build_number: str | None) -> None:
    """Check the pins, the prune list and the version, write the Info.plist the app would get, and
    say what ``build`` would do. Runs no other program. With neither ``tag`` nor
    ``build_number``, the bundle version is a placeholder, 1."""
    pins = load_pins()
    constraints = load_build_constraints()
    prune = load_prune_list()
    version = host_version()
    placeholder = tag is None and build_number is None
    versions = bundle_versions(version, tag, "1" if placeholder else build_number)
    out.mkdir(parents=True, exist_ok=True)
    plist_path = out / "Info.plist"
    with plist_path.open("wb") as handle:
        plistlib.dump(info_plist(pins, versions), handle, sort_keys=True)
    note = " (a placeholder: pass --build-number or --tag)" if placeholder else ""
    hardened = "with" if pins.adhoc_hardened_runtime else "without"
    print(f"host version      {version}")
    print(f"bundle versions   {versions.short} ({versions.build}){note}")
    print(f"python runtime    {pins.asset}")
    print(f"  from            {pins.url}")
    print(f"  sha256          {pins.sha256}")
    print(f"  pruned          {len(prune)} paths ({PRUNE_FILE.name}), and pip uninstalled")
    print(f"app               {out / (pins.app_name + '.app')}")
    print(f"bundle id         {pins.bundle_id}, macOS {pins.deployment_target} or later, arm64")
    print(f"runtime id        {pins.runtime_id} ({RUNTIME_IN_APP})")
    print(f"xcode             {pins.xcode} ({developer_dir(pins)})")
    print(f"signature         ad hoc, {hardened} the hardened runtime")
    print(f"build backend     {len(constraints)} wheels, by hash, from {BUILD_CONSTRAINTS.name}")
    print(f"unsigned tarball  {out / unsigned_tarball_name(pins.app_name)}")
    print(f"disk image        {out}/{pins.app_name}-{version}-<commit>{DMG_SUFFIX}")
    print(f"Info.plist        written to {plist_path}")


@dataclass(frozen=True)
class Assembled:
    app: Path
    version: str
    commit: str


def assemble(
    out: Path,
    *,
    host_wheel: Path,
    host_sha256: str,
    tag: str | None,
    build_number: str | None,
    archive: Path | None,
    cache: Path | None,
) -> Assembled:
    """Assemble the unsigned app and its tarball in ``out``; the steps are in this module's
    docstring. Every check that needs no Mac runs before the first thing is written."""
    pins = load_pins()
    load_build_constraints()
    prune = load_prune_list()
    version = host_version()
    versions = bundle_versions(version, tag, build_number)
    if archive is not None:
        check_sha256(archive, pins.sha256)
    check_host_wheel(host_wheel, host_sha256, version)
    require_apple_silicon("assemble")
    toolchain = xcode_env(pins, os.environ)
    commit = _output(["git", "-C", str(REPO_ROOT), "rev-parse", "--short=12", "HEAD"])
    commit_time = int(_output(["git", "-C", str(REPO_ROOT), "log", "-1", "--format=%ct"]))
    cache = cache if cache is not None else out / "cache"
    work = out / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    runtime_archive = fetch_runtime(pins, cache, archive)

    app = out / f"{pins.app_name}.app"
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    runtime = unpack_runtime(runtime_archive, contents)
    removed = prepare_runtime(runtime, prune)
    assembly = payload.assemble_site_packages(
        runtime / SITE_PACKAGES,
        requirements_text=export_requirements_text(),
        lock_text=LOCK_FILE.read_text(encoding="utf-8"),
        host_wheel=host_wheel,
        host_sha256=host_sha256,
        host_version=version,
        python_version=pins.python_version,
        deployment_target=pins.deployment_target,
        cache=cache / "wheels",
        work=work,
        environ=os.environ,
    )

    executable = contents / "MacOS" / pins.app_name
    compile_app(executable, pins.deployment_target, toolchain)
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(info_plist(pins, versions), handle, sort_keys=True)
    (contents / "PkgInfo").write_text("APPL????", encoding="ascii")
    write_wrapper(app)
    copy_licenses(app / LICENSES_IN_APP, assembly.site, runtime / STDLIB / "LICENSE.txt")
    swiftc = _output(["xcrun", "swiftc", "--version"], env=toolchain).splitlines()[0]
    data = manifest(
        pins,
        versions,
        assembly,
        commit=commit,
        tag=tag,
        build_number=build_number,
        swiftc=swiftc,
        removed=removed,
    )
    (app / MANIFEST_PATH).write_text(manifest_text(data), encoding="utf-8")
    problems = [f"symbolic link in the app: {link}" for link in symlinks_in(app)]
    problems += [f"bytecode in the app: {path}" for path in bytecode_in(app)]
    if problems:
        raise BuildError("the app is not ready to sign:\n  " + "\n  ".join(problems))
    tarball = out / unsigned_tarball_name(pins.app_name)
    digests = payload.write_tarball(app, tarball, commit_time)
    print(f"app               {app} (unsigned)")
    print(f"unsigned tarball  {tarball}")
    print(f"  sha256          {digests.gzip_sha256} (tar inside: {digests.tar_sha256})")
    return Assembled(app, version, commit)


def build(out: Path, **options: Any) -> Path:
    """Assemble the app, sign it ad hoc and build the disk image; returns the disk image's path."""
    built = assemble(out, **options)
    pins = load_pins()
    sign_app(built.app, pins)
    for old in disk_images(out, pins.app_name):
        old.unlink()
    dmg = out / dmg_name(pins.app_name, built.version, built.commit)
    make_dmg(built.app, dmg, pins.app_name, out / "work")
    print(f"app         {built.app}")
    print(f"disk image  {dmg} ({dmg.stat().st_size} bytes, sha256 {sha256_of(dmg)})")
    return dmg


def compare(first: Path, second: Path, app_name: str) -> str:
    """Two builds' unsigned tarballs compared, as Markdown: SAME, DIFFERENT, or FAILED when either
    is missing; the sha256 of each gzip file and of the tar inside it; and up to 50 members that
    differ."""
    a, b = (folder / unsigned_tarball_name(app_name) for folder in (first, second))
    lines = ["### The unsigned app, built twice in this job", ""]
    missing = [str(path) for path in (a, b) if not path.is_file()]
    if missing:
        return "\n".join([*lines, f"**FAILED**: no tarball at {', '.join(missing)}", ""])
    da, db = payload.tarball_digests(a), payload.tarball_digests(b)
    same = da == db
    lines += [
        f"**{'SAME' if same else 'DIFFERENT'}**",
        "",
        "| | first build | second build |",
        "|---|---|---|",
        f"| sha256 of the .tar.gz | `{da.gzip_sha256}` | `{db.gzip_sha256}` |",
        f"| sha256 of the .tar inside | `{da.tar_sha256}` | `{db.tar_sha256}` |",
        "",
    ]
    if not same:
        differing = payload.tar_differences(a, b)
        lines.append(f"{len(differing)} members differ" + (", the first 50:" if differing else "."))
        lines += [f"- `{name}`" for name in differing[:50]]
        lines.append("")
    return "\n".join(lines)


def _version_flags(parser: argparse.ArgumentParser, *, required: bool) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--tag", help="a build from this v* tag: the version must equal it")
    group.add_argument(
        "--build-number", help="any other build: CFBundleVersion (CI passes its run number)"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_app.py", description="Build the Mac app and its disk image."
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    plan_parser = commands.add_parser("plan", help="check the pins and write the Info.plist only")
    plan_parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder")
    _version_flags(plan_parser, required=False)
    wheel_parser = commands.add_parser("wheel", help="build the host's wheel from this checkout")
    wheel_parser.add_argument("--out", type=Path, required=True, help="folder for the wheel")
    wheel_parser.add_argument(
        "--github-output", type=Path, help="also write path= and sha256= lines to this file"
    )
    toolchain_parser = commands.add_parser("toolchain", help="check the pinned Xcode (macOS)")
    toolchain_parser.add_argument(
        "--github-env", type=Path, help="append DEVELOPER_DIR=... to this file ($GITHUB_ENV)"
    )
    for name, text in (
        ("assemble", "assemble the unsigned app and its tarball (macOS)"),
        ("build", "assemble, sign ad hoc, and build the disk image (macOS)"),
    ):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder")
        sub.add_argument("--host-wheel", type=Path, required=True, help="the host's wheel")
        sub.add_argument(
            "--host-wheel-sha256", required=True, help="the host wheel's sha256, checked first"
        )
        _version_flags(sub, required=True)
        sub.add_argument(
            "--python-archive",
            type=Path,
            help="use this copy of the pinned runtime archive instead of downloading it",
        )
        sub.add_argument("--cache", type=Path, help="download cache (default: OUT/cache)")
    compare_parser = commands.add_parser("compare", help="compare two builds' unsigned tarballs")
    compare_parser.add_argument("first", type=Path)
    compare_parser.add_argument("second", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            plan(args.out.resolve(), args.tag, args.build_number)
        elif args.command == "wheel":
            wheel = build_wheel(args.out.resolve(), host_version())
            digest = sha256_of(wheel)
            print(f"wheel   {wheel}\nsha256  {digest}")
            if args.github_output is not None:
                if "\n" in str(wheel):
                    raise BuildError("the wheel's path holds a newline")
                with args.github_output.open("a", encoding="utf-8") as handle:
                    handle.write(f"path={wheel}\nsha256={digest}\n")
        elif args.command == "toolchain":
            select_toolchain(load_pins(), args.github_env)
        elif args.command == "compare":
            print(compare(args.first, args.second, load_pins().app_name))
        else:
            options: dict[str, Any] = {
                "host_wheel": args.host_wheel.resolve(),
                "host_sha256": args.host_wheel_sha256,
                "tag": args.tag,
                "build_number": args.build_number,
                "archive": args.python_archive,
                "cache": args.cache.resolve() if args.cache is not None else None,
            }
            if args.command == "assemble":
                assemble(args.out.resolve(), **options)
            else:
                build(args.out.resolve(), **options)
    except (PinError, BuildError, payload.AssemblyError) as exc:
        print(f"build_app.py: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"build_app.py: {exc.cmd[0]} exited with {exc.returncode}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
