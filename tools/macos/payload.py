"""What goes into the Mac app's ``site-packages``, and the checks on it. Standard library only.

:func:`assemble_site_packages` is the one sequence that fills the app's ``site-packages``. Both
``build_app.py assemble`` (on a Mac) and the Linux tests call it, so the tests run the code that
ships:

1. Read the requirements ``uv export`` writes from ``uv.lock`` (each ``name==version``, its hashes
   and its environment marker) and keep those whose marker holds on the app's runtime, CPython
   3.13 on macOS on Apple silicon (:func:`marker_holds`). That is the set of distributions the
   build expects, worked out here, apart from the installer, which evaluates the same markers.
2. For each of them choose one wheel file of the lock, by the order of tags such a runtime
   accepts (:func:`supported_tags`); download it from the lock's URL into a cache; and refuse it
   unless its sha256 is the lock's and is among the requirement's hashes. A file already in the
   cache is hashed again before each use. Then copy exactly the chosen files into a folder of
   this build's own, the only place the installer may look.
3. Install them with ``uv pip install --target`` for ``aarch64-apple-darwin``: hashes required,
   wheels only, no dependencies, no index, no bytecode, and every ``UV_*`` variable removed from
   uv's environment (:func:`uv_env`). Then the host's wheel, by the sha256 the caller passes, from
   a folder that holds only it.
4. Remove what the installer writes that no wheel holds (:func:`clean_installer_output`): its
   lock file ``.lock`` (after each of the two installs), the console scripts in ``bin/``, which
   start with the build machine's interpreter path, and each ``uv_cache.json``, with their rows in
   ``RECORD``.
5. Refuse unless the installed distributions are exactly the expected ones plus the host, each
   at its pinned version, and :func:`record_problems` finds nothing: every file is byte for byte
   the file of the same name in its wheel, apart from the installer's own ``INSTALLER``,
   ``REQUESTED`` (and ``direct_url.json`` where written) and the rewritten ``RECORD``, which must
   hold the wheel's own lines; and there is no ``.pyc``, no ``__pycache__`` and no symbolic link.

A pin with no wheel for this platform fails step 2: nothing is ever built from source. A marker
this module cannot read fails step 1.

Also here: :func:`uninstall_by_record`, which the build uses to remove the runtime's own pip, and
:func:`write_tarball`, the unsigned app as a tarball whose bytes depend on the files alone.
"""

from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile
import tomllib
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# The platform and the Python version the wheels are installed for, as uv names them.
PYTHON_PLATFORM: Final = "aarch64-apple-darwin"
# Files the installer writes in a ``*.dist-info`` folder that no wheel holds, and which stay.
INSTALLER_FILES: Final = ("INSTALLER", "REQUESTED", "direct_url.json")
# What uv writes that no wheel holds, and the build removes: its lock file at the top of the
# target folder, the console scripts it writes into ``bin/``, and its cache note in each
# ``*.dist-info`` folder.
UV_LOCK_FILE: Final = ".lock"
UV_SCRIPTS_DIR: Final = "bin"
UV_CACHE_NOTE: Final = "uv_cache.json"
HOST_DISTRIBUTION: Final = "lucille-host"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_RELEASE_VERSION = re.compile(r"\d+(?:\.\d+)*")
_CPYTHON_TAG = re.compile(r"cp3(\d+)")
_PY_TAG = re.compile(r"py3(\d*)")
_MACOS_PLATFORM = re.compile(r"macosx_(\d+)_(\d+)_(arm64|universal2)")
_LICENSE_NAMES = ("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS")


class AssemblyError(RuntimeError):
    """The payload cannot be assembled or does not pass its checks; the message says why."""


def normal_name(name: str) -> str:
    """A distribution's name as packaging compares names: lower case, runs of ``-_.`` as ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


# ---------------------------------------------------------------------------------------------
# Requirements, markers and the lock


@dataclass(frozen=True)
class Requirement:
    """One line of ``uv export``: an exact pin, its allowed sha256s and its marker ('' if none)."""

    name: str
    version: str
    hashes: tuple[str, ...]
    marker: str


@dataclass(frozen=True)
class LockWheel:
    """One wheel file uv.lock lists for a pinned distribution."""

    name: str
    version: str
    filename: str
    url: str
    sha256: str


def parse_requirements(text: str) -> list[Requirement]:
    """The requirements in ``uv export --format requirements-txt`` output. Each one is
    ``name==version``, optionally ``; marker``, then one or more ``--hash=sha256:<hex>``. Any
    other form is refused, and so is a name given twice."""
    found: list[Requirement] = []
    seen: set[str] = set()
    for raw in text.replace("\\\n", " ").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head, sep, tail = line.partition("--hash=")
        if not sep:
            raise AssemblyError(f"requirements: {line!r} has no --hash")
        spec, _, marker = head.partition(";")
        name, eq, version = spec.strip().partition("==")
        if not eq or _NAME.fullmatch(name) is None or not version or " " in version.strip():
            raise AssemblyError(f"requirements: {spec.strip()!r} is not name==version")
        hashes = []
        for option in ("--hash=" + tail).split():
            digest = option.removeprefix("--hash=sha256:")
            if digest == option or _SHA256.fullmatch(digest) is None:
                raise AssemblyError(f"requirements: {option!r} is not --hash=sha256:<hex>")
            hashes.append(digest)
        key = normal_name(name)
        if key in seen:
            raise AssemblyError(f"requirements: {key} appears twice")
        seen.add(key)
        found.append(Requirement(key, version.strip(), tuple(hashes), marker.strip()))
    return found


# The marker variables of the app's runtime. A marker naming any other variable is refused.
_VERSION_VARIABLES: Final = frozenset(
    {"python_version", "python_full_version", "implementation_version"}
)


def marker_environment(python_version: str) -> dict[str, str]:
    """The values of the environment markers on the app's runtime: python-build-standalone's
    CPython ``python_version`` (``3.13.18``, say) on macOS on Apple silicon."""
    if re.fullmatch(r"3\.\d+\.\d+", python_version) is None:
        raise AssemblyError(f"{python_version!r} is not a full Python 3 version")
    return {
        "python_version": ".".join(python_version.split(".")[:2]),
        "python_full_version": python_version,
        "implementation_version": python_version,
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "sys_platform": "darwin",
        "platform_system": "Darwin",
        "platform_machine": "arm64",
        "os_name": "posix",
    }


_MARKER_TOKEN = re.compile(
    r"\s*(?:(?P<string>'[^']*'|\"[^\"]*\")|(?P<op>===|==|!=|<=|>=|~=|<|>|\(|\))"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_.]*))"
)


def _marker_tokens(marker: str) -> list[tuple[str, str]]:
    tokens = []
    position = 0
    text = marker.rstrip()
    while position < len(text):
        match = _MARKER_TOKEN.match(text, position)
        if match is None:
            raise AssemblyError(f"cannot read the marker {marker!r}")
        kind = match.lastgroup or ""
        tokens.append((kind, match.group(kind)))
        position = match.end()
    return tokens


def _version_tuple(text: str, marker: str) -> tuple[int, ...]:
    if _RELEASE_VERSION.fullmatch(text) is None:
        raise AssemblyError(f"the marker {marker!r} compares with {text!r}, not a version")
    return tuple(int(part) for part in text.split("."))


def _compare(variable: str, op: str, value: str, env: Mapping[str, str], marker: str) -> bool:
    if variable not in env:
        raise AssemblyError(
            f"the marker {marker!r} names {variable!r}, which the build cannot read"
        )
    if variable in _VERSION_VARIABLES:
        left = _version_tuple(env[variable], marker)
        right = _version_tuple(value, marker)
        width = max(len(left), len(right))
        left += (0,) * (width - len(left))
        right += (0,) * (width - len(right))
    else:
        if op not in ("==", "!="):
            raise AssemblyError(
                f"the marker {marker!r} orders {variable!r}, which is not a version"
            )
        left, right = (env[variable],), (value,)
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    raise AssemblyError(f"the marker {marker!r} uses {op!r}, which the build does not evaluate")


def marker_holds(marker: str, env: Mapping[str, str]) -> bool:
    """Whether a requirement's environment marker holds in ``env``. It reads ``and``, ``or``,
    parentheses, and comparisons of a marker variable with a quoted value; versions compare as
    numbers. Anything else (``in``, ``~=``, ``extra``, an unknown variable) is refused, never
    guessed: a marker the build cannot read stops the build."""
    if not marker.strip():
        return True
    tokens = _marker_tokens(marker)
    position = 0

    def peek() -> tuple[str, str]:
        return tokens[position] if position < len(tokens) else ("end", "")

    def take() -> tuple[str, str]:
        nonlocal position
        token = peek()
        position += 1
        return token

    def atom() -> bool:
        kind, value = take()
        if (kind, value) == ("op", "("):
            result = either()
            if take() != ("op", ")"):
                raise AssemblyError(f"the marker {marker!r} has an unclosed parenthesis")
            return result
        op_kind, op = take()
        right_kind, right = take()
        if op_kind != "op" or op in ("(", ")"):
            raise AssemblyError(f"cannot read the marker {marker!r}")
        if kind == "word" and right_kind == "string":
            return _compare(value, op, right[1:-1], env, marker)
        if kind == "string" and right_kind == "word":
            flipped = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}.get(op, op)
            return _compare(right, flipped, value[1:-1], env, marker)
        raise AssemblyError(f"cannot read the marker {marker!r}")

    def both() -> bool:
        result = atom()
        while peek() == ("word", "and"):
            take()
            result = atom() and result
        return result

    def either() -> bool:
        result = both()
        while peek() == ("word", "or"):
            take()
            result = both() or result
        return result

    result = either()
    if position != len(tokens):
        raise AssemblyError(f"cannot read the marker {marker!r}")
    return result


def parse_lock(text: str) -> dict[tuple[str, str], tuple[LockWheel, ...]]:
    """``{(name, version): wheels}`` for each package uv.lock pins, from each ``wheels`` entry's
    URL and ``sha256:`` hash. A package with no wheels maps to an empty tuple."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise AssemblyError(f"uv.lock does not parse: {exc}") from exc
    found: dict[tuple[str, str], tuple[LockWheel, ...]] = {}
    for package in data.get("package", []):
        key = (normal_name(str(package.get("name", ""))), str(package.get("version", "")))
        if key in found:
            raise AssemblyError(f"uv.lock lists {key[0]}=={key[1]} twice")
        wheels = []
        for wheel in package.get("wheels", []):
            url = str(wheel.get("url", ""))
            digest = str(wheel.get("hash", "")).removeprefix("sha256:")
            if not url.startswith("https://") or _SHA256.fullmatch(digest) is None:
                raise AssemblyError(f"uv.lock: {key[0]}=={key[1]} has a wheel with {url!r}")
            filename = urllib.parse.unquote(url.rsplit("/", 1)[1])
            wheels.append(LockWheel(key[0], key[1], filename, url, digest))
        found[key] = tuple(wheels)
    return found


# ---------------------------------------------------------------------------------------------
# Wheel tags


Tag = tuple[str, str, str]


def wheel_tags(filename: str) -> tuple[str, str, frozenset[Tag]]:
    """``(name, version, tags)`` of a wheel file name, with each compressed tag set expanded:
    ``cp315-abi3.abi3t-macosx_11_0_arm64`` holds two tags, one per ABI."""
    if not filename.endswith(".whl") or "/" in filename:
        raise AssemblyError(f"{filename!r} is not a wheel file name")
    parts = filename[: -len(".whl")].split("-")
    if len(parts) not in (5, 6):
        raise AssemblyError(f"{filename!r} is not a wheel file name")
    name, version = parts[0], parts[1]
    pythons, abis, platforms = (part.split(".") for part in parts[-3:])
    tags = frozenset((py, abi, plat) for py in pythons for abi in abis for plat in platforms)
    return normal_name(name), version, tags


def _mac_platforms(macos: tuple[int, int]) -> list[str]:
    """The macOS platform tags an Apple silicon Mac on ``macos`` accepts, best first, as
    packaging lists them: ``macosx_<major>_0`` from that major down to 11, each ``arm64`` then
    ``universal2``, then ``universal2`` for macOS 10.16 down to 10.4."""
    major = macos[0]
    if major < 11:
        raise AssemblyError(f"macOS {macos[0]}.{macos[1]} does not run on Apple silicon")
    found = []
    for version in range(major, 10, -1):
        found += [f"macosx_{version}_0_arm64", f"macosx_{version}_0_universal2"]
    found += [f"macosx_10_{minor}_universal2" for minor in range(16, 3, -1)]
    return found


def supported_tags(python: tuple[int, int], macos: tuple[int, int]) -> tuple[Tag, ...]:
    """Every wheel tag a CPython ``python`` on macOS ``macos`` on Apple silicon accepts, best
    first, in the order packaging's ``sys_tags`` gives (the order pip and uv use)."""
    major, minor = python
    if major != 3:
        raise AssemblyError(f"Python {major}.{minor} is not Python 3")
    platforms = _mac_platforms(macos)
    cp = f"cp3{minor}"
    tags: list[Tag] = [(cp, cp, plat) for plat in platforms]
    tags += [(cp, "abi3", plat) for plat in platforms]
    tags += [(cp, "none", plat) for plat in platforms]
    for older in range(minor - 1, 1, -1):
        tags += [(f"cp3{older}", "abi3", plat) for plat in platforms]
    pys = [f"py3{minor}", "py3", *(f"py3{older}" for older in range(minor - 1, -1, -1))]
    for py in pys:
        tags += [(py, "none", plat) for plat in platforms]
    tags.append((cp, "none", "any"))
    tags += [(py, "none", "any") for py in pys]
    return tuple(tags)


def wheel_rank(filename: str, python: tuple[int, int], macos: tuple[int, int]) -> int | None:
    """Where the best of a wheel file's tags falls in :func:`supported_tags` (0 is the best), or
    ``None`` if the runtime accepts none of them."""
    order = {tag: index for index, tag in enumerate(supported_tags(python, macos))}
    ranks = [order[tag] for tag in wheel_tags(filename)[2] if tag in order]
    return min(ranks) if ranks else None


def select_wheel(
    files: Sequence[LockWheel], python: tuple[int, int], macos: tuple[int, int]
) -> LockWheel:
    """The one file of ``files`` the runtime prefers. Refused when the runtime accepts none of
    them, or when two of them tie for the best place."""
    ranked = sorted(
        (
            (rank, wheel.filename, wheel)
            for wheel in files
            if (rank := wheel_rank(wheel.filename, python, macos)) is not None
        ),
        key=lambda entry: (entry[0], entry[1]),
    )
    if not ranked:
        names = ", ".join(wheel.filename for wheel in files) or "none"
        raise AssemblyError(f"no wheel for CPython 3.{python[1]} on macOS arm64 among: {names}")
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        raise AssemblyError(f"{ranked[0][1]} and {ranked[1][1]} are equally preferred")
    return ranked[0][2]


def plan_downloads(
    lock: Mapping[tuple[str, str], Sequence[LockWheel]],
    requirements: Sequence[Requirement],
    python: tuple[int, int],
    macos: tuple[int, int],
) -> list[LockWheel]:
    """One wheel file per requirement, sorted by name: the lock's preferred file for the
    runtime, whose sha256 must also be one the requirement allows."""
    chosen = []
    for requirement in requirements:
        files = lock.get((requirement.name, requirement.version))
        if not files:
            raise AssemblyError(
                f"uv.lock has no wheel for {requirement.name}=={requirement.version}"
            )
        wheel = select_wheel(files, python, macos)
        name, version, _ = wheel_tags(wheel.filename)
        if (name, version) != (requirement.name, requirement.version):
            raise AssemblyError(
                f"{wheel.filename} is not {requirement.name}=={requirement.version}"
            )
        if wheel.sha256 not in requirement.hashes:
            raise AssemblyError(
                f"{wheel.filename}: uv.lock's sha256 is not among the hashes the requirements "
                f"allow for {requirement.name}"
            )
        chosen.append(wheel)
    return sorted(chosen, key=lambda wheel: wheel.name)


# ---------------------------------------------------------------------------------------------
# Files and hashes


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def record_hash(data: bytes) -> str:
    """A file's hash as a wheel's ``RECORD`` writes it."""
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class WheelFile:
    """A wheel the build installs: its name, version and file, where it came from (``url`` is
    ``None`` for the host's wheel, which this checkout builds), its sha256 and its local copy."""

    name: str
    version: str
    filename: str
    url: str | None
    sha256: str
    path: Path


def fetch_wheels(plan: Sequence[LockWheel], cache: Path, dest: Path) -> list[WheelFile]:
    """Each planned wheel, downloaded into ``cache`` when it is not there yet, checked against
    its sha256 (a cached copy too, each time), and copied into ``dest``, a new folder that then
    holds exactly the planned files. A download that does not match is deleted."""
    cache.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    found = []
    for wheel in plan:
        if not wheel.url.startswith("https://"):
            raise AssemblyError(f"{wheel.filename}: refusing to download from {wheel.url!r}")
        cached = cache / wheel.filename
        if cached.exists() and sha256_of(cached) != wheel.sha256:
            print(f"{cached.name}: the cached copy does not match uv.lock; downloading it again")
            cached.unlink()
        if not cached.exists():
            partial = cached.with_name(cached.name + ".part")
            print(f"downloading {wheel.url}", flush=True)
            with (
                urllib.request.urlopen(wheel.url, timeout=300) as response,
                partial.open("wb") as out,
            ):
                shutil.copyfileobj(response, out)
            partial.replace(cached)
            actual = sha256_of(cached)
            if actual != wheel.sha256:
                cached.unlink()
                raise AssemblyError(
                    f"{wheel.filename}: its sha256 is {actual}, but uv.lock pins {wheel.sha256}; "
                    "refusing to build"
                )
        copy = dest / wheel.filename
        shutil.copyfile(cached, copy)
        if sha256_of(copy) != wheel.sha256:
            raise AssemblyError(f"{wheel.filename}: the copy for this build does not match")
        found.append(
            WheelFile(wheel.name, wheel.version, wheel.filename, wheel.url, wheel.sha256, copy)
        )
    return found


# ---------------------------------------------------------------------------------------------
# Installing


def without_uv_settings(environ: Mapping[str, str]) -> dict[str, str]:
    """``environ`` without any ``UV_*`` variable. Each is a uv setting that ``--no-config`` does
    not cover: it could add an index, find-links, overrides or constraints, or turn build
    isolation off. Every uv command the build and its checks run gets this environment and
    ``--no-config``."""
    return {key: value for key, value in environ.items() if not key.startswith("UV_")}


def uv_env(environ: Mapping[str, str], deployment_target: str) -> dict[str, str]:
    """uv's environment for the install: :func:`without_uv_settings`, with
    ``MACOSX_DEPLOYMENT_TARGET`` set to the app's, so that uv accepts the same macOS platform
    tags as :func:`supported_tags`."""
    env = without_uv_settings(environ)
    env["MACOSX_DEPLOYMENT_TARGET"] = deployment_target
    return env


def install_argv(
    target: Path, find_links: Path, requirements: Path, python_version: str
) -> list[str]:
    """``uv pip install`` of exactly the files in ``find_links`` into ``target``, for the app's
    platform and Python: hashes required, wheels only, no dependencies, no index, no bytecode,
    files copied, no cache and no configuration file."""
    return [
        "uv",
        "pip",
        "install",
        "--target",
        str(target),
        "--python-platform",
        PYTHON_PLATFORM,
        "--python-version",
        python_version,
        "--only-binary",
        ":all:",
        "--require-hashes",
        "--no-deps",
        "--no-compile",
        "--no-index",
        "--find-links",
        str(find_links),
        "--link-mode",
        "copy",
        "--no-cache",
        "--no-config",
        "--no-python-downloads",
        "-r",
        str(requirements),
    ]


def _run(argv: list[str], env: Mapping[str, str]) -> None:
    print("+ " + " ".join(argv), flush=True)
    subprocess.run(argv, check=True, env=dict(env))


def _read_record(path: Path) -> list[tuple[str, ...]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [tuple(row) for row in csv.reader(handle) if row]


def _write_record_without(record: Path, paths: Iterable[str]) -> None:
    """Rewrite ``record`` without the rows for ``paths``, keeping every other line as it was."""
    drop = set(paths)
    kept = []
    for line in record.read_text(encoding="utf-8").splitlines(keepends=True):
        row = next(csv.reader([line]), [])
        if not row or row[0] not in drop:
            kept.append(line)
    record.write_text("".join(kept), encoding="utf-8")


def dist_infos(site: Path) -> list[Path]:
    """The ``*.dist-info`` folders at the top of ``site``, sorted."""
    if not site.is_dir():
        return []
    return sorted(path for path in site.iterdir() if path.name.endswith(".dist-info"))


def clean_installer_output(site: Path) -> list[str]:
    """Remove what uv writes into ``site`` that no wheel holds: its ``.lock``, the console
    scripts in ``bin/`` and each ``uv_cache.json``, and their rows in ``RECORD``. Returns what was
    removed, relative to ``site``."""
    removed = []
    lock = site / UV_LOCK_FILE
    if lock.is_file() or lock.is_symlink():
        lock.unlink()
        removed.append(UV_LOCK_FILE)
    scripts = site / UV_SCRIPTS_DIR
    script_rows = set()
    if scripts.is_dir() and not scripts.is_symlink():
        for folder, _, files in os.walk(scripts):
            for name in files:
                script_rows.add((Path(folder) / name).relative_to(site).as_posix())
        shutil.rmtree(scripts)
        removed.append(UV_SCRIPTS_DIR + "/")
    for info in dist_infos(site):
        rows = set(script_rows)
        note = info / UV_CACHE_NOTE
        if note.is_file():
            note.unlink()
            rows.add(f"{info.name}/{UV_CACHE_NOTE}")
            removed.append(f"{info.name}/{UV_CACHE_NOTE}")
        record = info / "RECORD"
        if rows and record.is_file():
            _write_record_without(record, rows)
    return sorted(removed)


def name_and_version(info: Path) -> tuple[str, str]:
    """A ``*.dist-info`` folder's distribution name (normalized) and version, from the ``Name``
    and ``Version`` lines of its ``METADATA``."""
    fields: dict[str, str] = {}
    metadata = info / "METADATA"
    lines = metadata.read_text(encoding="utf-8").splitlines() if metadata.is_file() else []
    for line in lines:
        if not line.strip():
            break
        key, sep, value = line.partition(":")
        if sep and key in ("Name", "Version") and key not in fields:
            fields[key] = value.strip()
    if set(fields) != {"Name", "Version"}:
        raise AssemblyError(f"{info.name} has no Name and Version in its METADATA")
    return normal_name(fields["Name"]), fields["Version"]


def installed_versions(site: Path) -> dict[str, str]:
    """``{name: version}`` of each distribution installed in ``site``. A distribution installed
    twice is refused."""
    found: dict[str, str] = {}
    for info in dist_infos(site):
        name, version = name_and_version(info)
        if name in found:
            raise AssemblyError(f"{name} is installed twice")
        found[name] = version
    return found


def _wheel_record(wheel: Path) -> tuple[str, str]:
    """``(dist-info folder name, RECORD text)`` of a wheel file."""
    with zipfile.ZipFile(wheel) as archive:
        records = [
            name
            for name in archive.namelist()
            if name.count("/") == 1 and name.endswith(".dist-info/RECORD")
        ]
        if len(records) != 1:
            raise AssemblyError(f"{wheel.name} holds {len(records)} dist-info RECORD files")
        return records[0].split("/")[0], archive.read(records[0]).decode("utf-8")


def record_problems(site: Path, wheels: Sequence[Path]) -> list[str]:
    """Each way ``site`` differs from what installing exactly ``wheels`` writes, after
    :func:`clean_installer_output`:

    - a wheel whose ``*.dist-info`` folder is not there, or such a folder no wheel holds;
    - a ``RECORD`` line that is not a line of the wheel's own ``RECORD``, other than the rows of
      the installer's ``INSTALLER``, ``REQUESTED`` and ``direct_url.json``; and a line of the
      wheel's ``RECORD`` missing from the installed one (compared as multisets of rows);
    - a file that is in no ``RECORD`` (``.lock``, a ``.pth`` no wheel holds, anything planted),
      a ``RECORD`` row whose file is missing or outside ``site``, a path two ``RECORD`` files
      claim, a file whose sha256 or size is not its row's, and a file that is not byte for byte
      the file of the same name in its wheel;
    - any ``.pyc``, ``.pyo`` or ``__pycache__``, any symbolic link, and any empty folder.

    An empty list means every check passed; with no wheels and an empty ``site`` it is empty."""
    with ExitStack() as stack:
        archives = {wheel: stack.enter_context(zipfile.ZipFile(wheel)) for wheel in set(wheels)}
        return _record_problems(site, wheels, archives)


def _record_problems(
    site: Path, wheels: Sequence[Path], archives: Mapping[Path, zipfile.ZipFile]
) -> list[str]:
    problems: list[str] = []
    by_info: dict[str, Path] = {}
    wheel_rows: dict[str, Counter[tuple[str, ...]]] = {}
    for wheel in wheels:
        info, text = _wheel_record(wheel)
        if info in by_info:
            problems.append(f"{wheel.name} and {by_info[info].name} both hold {info}")
            continue
        by_info[info] = wheel
        wheel_rows[info] = Counter(tuple(row) for row in csv.reader(text.splitlines()) if row)

    installed = {info.name: info for info in dist_infos(site)}
    for info in sorted(set(by_info) - set(installed)):
        problems.append(f"{by_info[info].name} is not installed: there is no {info}")
    for info in sorted(set(installed) - set(by_info)):
        problems.append(f"{info} comes from no wheel the build installed")

    owner: dict[str, tuple[str, tuple[str, ...]]] = {}
    for info in sorted(set(installed) & set(by_info)):
        record = installed[info] / "RECORD"
        if not record.is_file():
            problems.append(f"{info} has no RECORD")
            continue
        rows = Counter(_read_record(record))
        written = {f"{info}/{name}" for name in INSTALLER_FILES}
        for row in sorted((rows - wheel_rows[info]).elements()):
            if row[0] not in written:
                problems.append(f"{info}/RECORD has a line its wheel's RECORD does not: {row}")
        for row in sorted((wheel_rows[info] - rows).elements()):
            problems.append(f"{info}/RECORD lacks a line of its wheel's RECORD: {row}")
        for row in rows:
            path = os.path.normpath(row[0])
            if path == ".." or path.startswith("../") or os.path.isabs(path) or path != row[0]:
                problems.append(f"{info}/RECORD names {row[0]}, which is not a path in site")
                continue
            if path in owner and owner[path][0] != info:
                problems.append(f"{path} is in the RECORD of both {owner[path][0]} and {info}")
            owner[path] = (info, row)

    for folder, dirs, files in os.walk(site):
        here = Path(folder)
        for name in sorted(dirs):
            path = here / name
            if path.is_symlink():
                problems.append(f"symbolic link: {path.relative_to(site)}")
            elif name == "__pycache__":
                problems.append(f"bytecode folder: {path.relative_to(site)}")
            elif not any(path.iterdir()):
                problems.append(f"empty folder: {path.relative_to(site)}")
        dirs[:] = [name for name in dirs if not (here / name).is_symlink()]
        for name in sorted(files):
            path = here / name
            rel = path.relative_to(site).as_posix()
            if path.is_symlink():
                problems.append(f"symbolic link: {rel}")
                continue
            if path.suffix in (".pyc", ".pyo"):
                problems.append(f"bytecode: {rel}")
            if rel not in owner:
                problems.append(f"{rel} is in no wheel's RECORD")
                continue
            info, row = owner.pop(rel)
            if rel == f"{info}/RECORD":
                continue
            data = path.read_bytes()
            if len(row) < 3 or row[1] != record_hash(data) or row[2] != str(len(data)):
                problems.append(f"{rel} does not match its RECORD row")
            if rel in {f"{info}/{written}" for written in INSTALLER_FILES}:
                continue
            try:
                original = archives[by_info[info]].read(rel)
            except KeyError:
                problems.append(f"{rel} is not in {by_info[info].name}")
                continue
            if original != data:
                problems.append(f"{rel} differs from the file in {by_info[info].name}")
    for rel in sorted(owner):
        problems.append(f"{owner[rel][0]}/RECORD lists {rel}, which is not installed")
    return problems


def uninstall_by_record(root: Path, site: Path, name: str) -> str:
    """Remove the distribution ``name`` from ``site`` by its own ``RECORD``, and return
    ``name==version``. Every path the ``RECORD`` names must stay inside ``root``, or nothing is
    removed. The removal is tolerant: a row whose file is absent is skipped, and no hash or size
    is checked before a file is deleted (python-build-standalone strips the runtime's pip of its
    ``.pyc`` files and rewrites its scripts' first line, so its ``RECORD`` no longer matches).
    Whatever the ``RECORD`` does not name is left, for the caller's checks to find."""
    site = Path(os.path.normpath(site))
    found = sorted(site.glob(f"{name}-*.dist-info"))
    if len(found) != 1:
        raise AssemblyError(f"expected one {name}-*.dist-info in {site}, found {len(found)}")
    info = found[0]
    record = info / "RECORD"
    if not record.is_file():
        raise AssemblyError(f"{info.name} has no RECORD")
    version = info.name[len(name) + 1 : -len(".dist-info")]
    top = os.path.realpath(root)
    targets = []
    for row in _read_record(record):
        target = Path(os.path.normpath(site / row[0]))
        if os.path.commonpath([top, os.path.realpath(target.parent)]) != top:
            raise AssemblyError(f"{info.name}/RECORD names {row[0]}, which is outside {root}")
        if target.is_dir() and not target.is_symlink():
            raise AssemblyError(f"{info.name}/RECORD names {row[0]}, which is a folder")
        targets.append(target)
    folders: set[Path] = set()
    for target in targets:
        if target.is_symlink() or target.exists():
            target.unlink()
        parent = target.parent
        while parent != site and site in parent.parents:
            folders.add(parent)
            parent = parent.parent
    shutil.rmtree(info)
    for folder in sorted(folders, key=lambda path: len(path.parts), reverse=True):
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    return f"{name}=={version}"


# ---------------------------------------------------------------------------------------------
# The whole sequence


@dataclass(frozen=True)
class Assembly:
    """What :func:`assemble_site_packages` installed: the lock's wheels, sorted by name, and the
    host's wheel; and what it removed after the installer, relative to ``site``."""

    site: Path
    wheels: tuple[WheelFile, ...]
    host: WheelFile
    removed: tuple[str, ...]


def host_wheel_name(version: str) -> str:
    return f"lucille_host-{version}-py3-none-any.whl"


def assemble_site_packages(
    site: Path,
    *,
    requirements_text: str,
    lock_text: str,
    host_wheel: Path,
    host_sha256: str,
    host_version: str,
    python_version: str,
    deployment_target: str,
    cache: Path,
    work: Path,
    environ: Mapping[str, str],
) -> Assembly:
    """Fill ``site`` (absent or empty) with the lock's wheels for the app's runtime and the
    host's wheel, and check the result; the steps are in this module's docstring. ``cache`` keeps
    downloaded wheels between builds; ``work`` holds this build's own folders."""
    if site.exists() and any(site.iterdir()):
        raise AssemblyError(f"{site} is not empty")
    if host_wheel.name != host_wheel_name(host_version):
        raise AssemblyError(f"the host wheel must be {host_wheel_name(host_version)}")
    if sha256_of(host_wheel) != host_sha256:
        raise AssemblyError(f"{host_wheel.name}: its sha256 is not {host_sha256}")
    env_markers = marker_environment(python_version)
    python = tuple(int(part) for part in python_version.split(".")[:2])
    target = deployment_target.split(".")
    macos = (int(target[0]), int(target[1]))
    requirements = parse_requirements(requirements_text)
    expected = [req for req in requirements if marker_holds(req.marker, env_markers)]
    plan = plan_downloads(parse_lock(lock_text), expected, (python[0], python[1]), macos)
    wheels = fetch_wheels(plan, cache, work / "find-links")

    env = uv_env(environ, deployment_target)
    exported = work / "requirements.txt"
    exported.write_text(requirements_text, encoding="utf-8")
    _run(install_argv(site, work / "find-links", exported, python_version), env)
    (site / UV_LOCK_FILE).unlink(missing_ok=True)

    host_dir = work / "host-wheel"
    if host_dir.exists():
        shutil.rmtree(host_dir)
    host_dir.mkdir(parents=True)
    host_copy = host_dir / host_wheel.name
    shutil.copyfile(host_wheel, host_copy)
    if sha256_of(host_copy) != host_sha256:
        raise AssemblyError(f"{host_wheel.name}: the copy for this build does not match")
    host_requirements = work / "host-requirements.txt"
    host_requirements.write_text(
        f"{HOST_DISTRIBUTION}=={host_version} --hash=sha256:{host_sha256}\n", encoding="ascii"
    )
    _run(install_argv(site, host_dir, host_requirements, python_version), env)
    (site / UV_LOCK_FILE).unlink(missing_ok=True)
    removed = clean_installer_output(site)

    wanted = {wheel.name: wheel.version for wheel in wheels}
    wanted[HOST_DISTRIBUTION] = host_version
    got = installed_versions(site)
    if got != wanted:
        missing = sorted(f"{n}=={v}" for n, v in wanted.items() if got.get(n) != v)
        extra = sorted(f"{n}=={v}" for n, v in got.items() if wanted.get(n) != v)
        raise AssemblyError(
            f"the installed distributions are not the expected ones; missing {missing}, "
            f"not expected {extra}"
        )
    problems = record_problems(site, [wheel.path for wheel in wheels] + [host_copy])
    if problems:
        raise AssemblyError("site-packages does not match its wheels:\n  " + "\n  ".join(problems))
    host = WheelFile(HOST_DISTRIBUTION, host_version, host_wheel.name, None, host_sha256, host_copy)
    return Assembly(site, tuple(wheels), host, tuple(removed))


def manifest_wheels(
    installed: Mapping[str, str], wheels: Sequence[WheelFile]
) -> list[dict[str, str | None]]:
    """MANIFEST.json's ``wheels``: one entry per installed distribution, the host's aside, each
    from the wheel it came from. Refused: an installed distribution with no wheel, a wheel for a
    distribution not installed or at another version, and two wheels for one distribution."""
    by_name: dict[str, WheelFile] = {}
    for wheel in wheels:
        if wheel.name in by_name:
            raise AssemblyError(f"two wheels for {wheel.name}")
        by_name[wheel.name] = wheel
    names = set(installed) - {HOST_DISTRIBUTION}
    if names != set(by_name):
        raise AssemblyError(f"installed {sorted(names)}, but the wheels are for {sorted(by_name)}")
    entries = []
    for name in sorted(names):
        wheel = by_name[name]
        if wheel.version != installed[name]:
            raise AssemblyError(
                f"{name} {installed[name]} is installed, the wheel is {wheel.version}"
            )
        entries.append(
            {
                "name": wheel.name,
                "version": wheel.version,
                "file": wheel.filename,
                "url": wheel.url,
                "sha256": wheel.sha256,
            }
        )
    return entries


def license_files(site: Path) -> dict[str, list[Path]]:
    """``{distribution: files}``: the licence texts each installed distribution ships, from its
    ``*.dist-info`` folder: everything under ``licenses/``, and any file at its top whose name
    starts with LICENSE, LICENCE, COPYING, NOTICE or AUTHORS. A distribution with none is
    refused."""
    found: dict[str, list[Path]] = {}
    for info in dist_infos(site):
        files = sorted(path for path in (info / "licenses").rglob("*") if path.is_file())
        files += sorted(
            path
            for path in info.iterdir()
            if path.is_file() and path.name.upper().startswith(_LICENSE_NAMES)
        )
        if not files:
            raise AssemblyError(f"{info.name} ships no licence file")
        found[name_and_version(info)[0]] = files
    return found


# ---------------------------------------------------------------------------------------------
# The unsigned tarball


@dataclass(frozen=True)
class TarballDigests:
    gzip_sha256: str
    tar_sha256: str


def _members(top: Path) -> list[Path]:
    found = [top]
    for folder, dirs, files in os.walk(top):
        dirs.sort()
        found += [Path(folder) / name for name in (*dirs, *files)]
    return sorted(found, key=lambda path: path.relative_to(top.parent).parts)


def write_tarball(top: Path, dest: Path, mtime: int) -> TarballDigests:
    """``top`` (a folder) as a gzipped tar file at ``dest``, whose bytes depend only on the
    files' names, contents and execute bits: entries sorted, folders included; every ``mtime``
    the given one (the commit time); owner and group 0 with no names; mode 0755 for a folder or
    a file with any execute bit, else 0644; PAX format; and a gzip header with no file name and
    no time. A symbolic link, or anything but a file or a folder, is refused; a file with several
    hard links is written as a plain file each time."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in _members(top):
            info = tarfile.TarInfo(path.relative_to(top.parent).as_posix())
            status = path.lstat()
            if path.is_symlink():
                raise AssemblyError(f"refusing a symbolic link in the tarball: {path}")
            info.mtime = mtime
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif path.is_file():
                info.type = tarfile.REGTYPE
                info.mode = 0o755 if status.st_mode & 0o111 else 0o644
                info.size = status.st_size
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
            else:
                raise AssemblyError(f"refusing {path}: not a file or a folder")
    data = raw.getvalue()
    with (
        dest.open("wb") as out,
        gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=9) as gz,
    ):
        gz.write(data)
    return TarballDigests(sha256_of(dest), hashlib.sha256(data).hexdigest())


def tarball_digests(path: Path) -> TarballDigests:
    """The sha256 of a gzipped tar file, and of the tar inside it."""
    digest = hashlib.sha256()
    with gzip.open(path, "rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return TarballDigests(sha256_of(path), digest.hexdigest())


def _describe(tar: tarfile.TarFile) -> dict[str, tuple[object, ...]]:
    found: dict[str, tuple[object, ...]] = {}
    for member in tar.getmembers():
        content = ""
        if member.isfile():
            handle = tar.extractfile(member)
            content = hashlib.sha256(handle.read() if handle else b"").hexdigest()
        found[member.name] = (
            member.type,
            member.mode,
            member.size,
            member.mtime,
            member.uid,
            member.gid,
            content,
        )
    return found


def tar_differences(a: Path, b: Path) -> list[str]:
    """The members of two gzipped tar files that differ in type, mode, size, time, owner or
    contents, or are in only one of them, sorted by name."""
    with tarfile.open(a, "r:gz") as first, tarfile.open(b, "r:gz") as second:
        left, right = _describe(first), _describe(second)
    return sorted(name for name in set(left) | set(right) if left.get(name) != right.get(name))
