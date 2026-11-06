"""The Mac app's site-packages, assembled and checked on Linux by the same function the Mac build
runs (tools/macos/payload.py): the requirements and their markers, the choice of one wheel file
per pin, the installer's leftovers, the RECORD check, the manifest's wheels, the runtime's pip
removed by its RECORD, and the unsigned tarball.

The ``linux_assembly`` fixture downloads the pinned wheels from PyPI and installs them with uv,
as the build does. It needs uv and the network, and fails without them: it is never skipped.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import shutil
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

import lucille_host
from tools.macos import build_app, check_app, payload

PINS = build_app.load_pins()
PYTHON = (3, 13)
MACOS = (15, 0)
# The distributions the app installs from uv.lock for CPython 3.13 on macOS on Apple silicon,
# written out here rather than worked out, so that a wrong marker evaluation or a dropped wheel
# fails this list. The six pins left out are for Linux, Windows or Python before 3.12:
# backports-tarfile, importlib-metadata, jeepney, pywin32-ctypes, secretstorage, zipp.
EXPECTED_DISTRIBUTIONS = frozenset(
    {
        "agent-switchboard",
        "anyio",
        "certifi",
        "cffi",
        "cryptography",
        "h11",
        "httpcore",
        "httpx",
        "idna",
        "jaraco-classes",
        "jaraco-context",
        "jaraco-functools",
        "keyring",
        "more-itertools",
        "pycparser",
        "typing-extensions",
    }
)
NOT_FOR_THE_MAC = (
    "backports-tarfile",
    "importlib-metadata",
    "jeepney",
    "pywin32-ctypes",
    "secretstorage",
    "zipp",
)


@pytest.fixture(scope="module")
def requirements_text() -> str:
    if shutil.which("uv") is None:
        pytest.fail("uv is not on PATH: the Mac payload is exported and installed with uv")
    return build_app.export_requirements_text()


@pytest.fixture(scope="module")
def linux_assembly(
    tmp_path_factory: pytest.TempPathFactory, requirements_text: str
) -> payload.Assembly:
    """The app's site-packages, assembled on this machine by payload.assemble_site_packages,
    from the host wheel built from this checkout and the pinned wheels downloaded from PyPI."""
    root = tmp_path_factory.mktemp("assembly")
    version = build_app.host_version()
    host_wheel = build_app.build_wheel(root / "host-wheel", version)
    return payload.assemble_site_packages(
        root / "site",
        requirements_text=requirements_text,
        lock_text=build_app.LOCK_FILE.read_text(encoding="utf-8"),
        host_wheel=host_wheel,
        host_sha256=payload.sha256_of(host_wheel),
        host_version=version,
        python_version=PINS.python_version,
        deployment_target=PINS.deployment_target,
        cache=root / "cache",
        work=root / "work",
        environ=os.environ,
    )


def _files(site: Path) -> list[str]:
    return sorted(
        str(Path(folder, name).relative_to(site))
        for folder, dirs, files in os.walk(site)
        for name in (*dirs, *files)
    )


def test_linux_assembly_two_macho(linux_assembly: payload.Assembly) -> None:
    site = linux_assembly.site
    found = build_app.macho_files(site)
    assert sorted(path.relative_to(site).as_posix() for path in found) == [
        "_cffi_backend.cpython-313-darwin.so",
        "cryptography/hazmat/bindings/_rust.abi3.so",
    ]
    for path in found:
        header = path.read_bytes()[:8]
        # A thin 64-bit Mach-O file, little-endian, for arm64 (CPU_TYPE_ARM64, 0x0100000c).
        assert header[:4] == b"\xcf\xfa\xed\xfe", path
        assert int.from_bytes(header[4:8], "little") == 0x0100000C, path
    installed = payload.installed_versions(site)
    assert set(installed) == EXPECTED_DISTRIBUTIONS | {"lucille-host"}
    for name in NOT_FOR_THE_MAC:
        assert name not in installed
    entries = _files(site)
    assert "bin" not in entries and ".lock" not in entries
    assert not build_app.symlinks_in(site)
    assert not [entry for entry in entries if entry.endswith(".pth")]
    assert not [entry for entry in entries if entry.endswith("uv_cache.json")]


def test_no_pyc_in_site_packages(linux_assembly: payload.Assembly, tmp_path: Path) -> None:
    assert build_app.bytecode_in(linux_assembly.site) == []
    # And the build guard refuses one: a .pyc planted in an installed site.
    site, wheel = _installed_demo(tmp_path)
    assert payload.record_problems(site, [wheel]) == []
    compiled = site / "demo" / "__pycache__" / "m.cpython-313.pyc"
    compiled.parent.mkdir()
    compiled.write_bytes(b"\xf3\r\r\n" + bytes(12))
    # The finder the build's own refusals use sees both.
    assert build_app.bytecode_in(site) == [compiled.parent, compiled]
    problems = payload.record_problems(site, [wheel])
    assert "bytecode folder: demo/__pycache__" in problems
    assert "bytecode: demo/__pycache__/m.cpython-313.pyc" in problems


def test_linux_assembly_matches_every_record(
    linux_assembly: payload.Assembly, tmp_path: Path
) -> None:
    wheels = [wheel.path for wheel in linux_assembly.wheels] + [linux_assembly.host.path]
    assert payload.record_problems(linux_assembly.site, wheels) == []
    # The same checks find what is planted in a copy of the real assembly.
    site = tmp_path / "site"
    shutil.copytree(linux_assembly.site, site, symlinks=True)
    (site / ".lock").write_bytes(b"")
    (site / "keyring" / "planted.py").write_text("print('planted')\n", encoding="ascii")
    core = site / "keyring" / "core.py"
    core.write_bytes(core.read_bytes() + b"\n# edited\n")
    record = site / "keyring-25.7.0.dist-info" / "RECORD"
    record.write_text(
        record.read_text(encoding="utf-8") + "keyring/ghost.py,sha256=AAAA,1\n", encoding="utf-8"
    )
    problems = payload.record_problems(site, wheels)
    assert ".lock is in no wheel's RECORD" in problems
    assert "keyring/planted.py is in no wheel's RECORD" in problems
    assert "keyring/core.py does not match its RECORD row" in problems
    assert "keyring/core.py differs from the file in keyring-25.7.0-py3-none-any.whl" in problems
    assert "keyring-25.7.0.dist-info/RECORD lists keyring/ghost.py, which is not installed" in (
        problems
    )
    assert any("RECORD has a line its wheel's RECORD does not" in line for line in problems)


def test_manifest_lists_every_wheel(
    linux_assembly: payload.Assembly, requirements_text: str
) -> None:
    lock = payload.parse_lock(build_app.LOCK_FILE.read_text(encoding="utf-8"))
    lock_sha = {wheel.filename: wheel.sha256 for files in lock.values() for wheel in files}
    exported = {req.name: req for req in payload.parse_requirements(requirements_text)}
    data = build_app.manifest(
        PINS,
        build_app.bundle_versions(build_app.host_version(), None, "523"),
        linux_assembly,
        commit="0123456789ab",
        tag=None,
        build_number="523",
        swiftc="swift-driver version: test",
        removed=["pip==26.2.2", "share"],
    )
    wheels = data["wheels"]
    assert {entry["name"] for entry in wheels} == EXPECTED_DISTRIBUTIONS
    assert [entry["name"] for entry in wheels] == sorted(EXPECTED_DISTRIBUTIONS)
    by_name = {wheel.name: wheel for wheel in linux_assembly.wheels}
    for entry in wheels:
        wheel = by_name[entry["name"]]
        assert entry["file"] == wheel.filename and entry["url"] == wheel.url
        assert entry["sha256"] == payload.sha256_of(wheel.path) == lock_sha[wheel.filename]
        assert entry["sha256"] in exported[entry["name"]].hashes
        assert entry["version"] == exported[entry["name"]].version
        # The file's tags are the ones its installed dist-info says it was built for.
        info = next(
            info
            for info in payload.dist_infos(linux_assembly.site)
            if payload.name_and_version(info)[0] == entry["name"]
        )
        said = {
            line.removeprefix("Tag: ").strip()
            for line in (info / "WHEEL").read_text(encoding="utf-8").splitlines()
            if line.startswith("Tag: ")
        }
        assert {"-".join(tag) for tag in payload.wheel_tags(wheel.filename)[2]} <= said
    assert data["host"]["version"] == lucille_host.__version__
    assert data["host"]["wheel"] == {
        "file": linux_assembly.host.filename,
        "sha256": payload.sha256_of(linux_assembly.host.path),
    }
    # What check_app.py record (M10) holds the signed bundle's MANIFEST.json to.
    host_sha256 = payload.sha256_of(linux_assembly.host.path)
    assert check_app.lock_wheel_problems(data, lock, host_wheel_sha256=host_sha256) == []
    assert data["app"] == {
        "name": PINS.app_name,
        "bundle_id": PINS.bundle_id,
        "runtime_id": PINS.runtime_id,
        "deployment_target": PINS.deployment_target,
        "short_version": "0.1.0",
        "bundle_version": "523",
    }
    assert data["runtime"]["sha256"] == PINS.sha256 and data["ripgrep"] is None
    assert data["runtime"]["removed"] == ["pip==26.2.2", "share"]
    text = build_app.manifest_text(data)
    assert text.endswith("}\n") and text == build_app.manifest_text(dict(reversed(data.items())))


def _wheel_file(name: str, version: str = "1.0") -> payload.WheelFile:
    return payload.WheelFile(
        name, version, f"{name}-{version}-py3-none-any.whl", None, "0" * 64, Path()
    )


@pytest.mark.parametrize(
    ("installed", "wheels", "problem"),
    [
        ({"a": "1.0", "b": "1.0"}, ["a"], "installed ['a', 'b'], but the wheels are for ['a']"),
        ({"a": "1.0"}, ["a", "b"], "installed ['a'], but the wheels are for ['a', 'b']"),
        ({"a": "1.0"}, ["a", "a"], "two wheels for a"),
        ({"a": "2.0"}, ["a"], "a 2.0 is installed, the wheel is 1.0"),
    ],
)
def test_manifest_refuses_a_wheel_it_cannot_match(
    installed: dict[str, str], wheels: list[str], problem: str
) -> None:
    with pytest.raises(payload.AssemblyError, match=re.escape(problem)):
        payload.manifest_wheels(installed, [_wheel_file(name) for name in wheels])
    # The host's own wheel is recorded apart, never among the wheels.
    entries = payload.manifest_wheels({"a": "1.0", "lucille-host": "0.1"}, [_wheel_file("a")])
    assert [entry["name"] for entry in entries] == ["a"]


def test_the_licences_of_every_distribution(
    linux_assembly: payload.Assembly, tmp_path: Path
) -> None:
    runtime_license = tmp_path / "LICENSE.txt"
    runtime_license.write_text("PSF\n", encoding="ascii")
    written = build_app.copy_licenses(tmp_path / "licenses", linux_assembly.site, runtime_license)
    folders = {rel.split("/")[0] for rel in written}
    assert folders == EXPECTED_DISTRIBUTIONS | {"lucille-host", "CPython"}
    assert "cryptography/LICENSE.APACHE" in written and "CPython/LICENSE.txt" in written
    assert "jaraco-classes/LICENSE" in written
    for rel in written:
        assert (tmp_path / "licenses" / rel).read_bytes()


# ---------------------------------------------------------------------------------------------
# The RECORD check, on a wheel made here


def _make_wheel(folder: Path, name: str = "demo", version: str = "1.0") -> Path:
    info = f"{name}-{version}.dist-info"
    entries = {
        f"{name}/__init__.py": b"",
        f"{name}/m.py": b"VALUE = 1\n",
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n".encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{info}/licenses/LICENSE": b"MIT\n",
    }
    rows = [f"{path},{payload.record_hash(data)},{len(data)}" for path, data in entries.items()]
    rows.append(f"{info}/RECORD,,")
    folder.mkdir(parents=True, exist_ok=True)
    wheel = folder / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, data in entries.items():
            archive.writestr(path, data)
        archive.writestr(f"{info}/RECORD", "\n".join(rows) + "\n")
    return wheel


def _add_row(record: Path, rel: str, data: bytes | None) -> None:
    row = f"{rel},{payload.record_hash(data)},{len(data)}" if data is not None else f"{rel},,"
    record.write_text(record.read_text(encoding="utf-8") + row + "\n", encoding="utf-8")


def _installed_demo(tmp_path: Path) -> tuple[Path, Path]:
    """A site holding what an installer writes for the demo wheel: its files, INSTALLER and
    REQUESTED, and a RECORD with their rows."""
    wheel = _make_wheel(tmp_path / "wheels")
    site = tmp_path / "site"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
    info = site / "demo-1.0.dist-info"
    for name, data in (("INSTALLER", b"uv"), ("REQUESTED", b"")):
        (info / name).write_bytes(data)
        _add_row(info / "RECORD", f"demo-1.0.dist-info/{name}", data)
    return site, wheel


def _plant_lock(site: Path) -> None:
    (site / ".lock").write_bytes(b"")


def _plant_unlisted(site: Path) -> None:
    (site / "demo" / "extra.py").write_text("print('extra')\n", encoding="ascii")


def _plant_pth(site: Path) -> None:
    (site / "evil.pth").write_text("import os\n", encoding="ascii")


def _plant_link(site: Path) -> None:
    (site / "demo" / "link.py").symlink_to("m.py")


def _plant_edit(site: Path) -> None:
    (site / "demo" / "m.py").write_bytes(b"VALUE = 2\n")


def _plant_edit_and_record(site: Path) -> None:
    record = site / "demo-1.0.dist-info" / "RECORD"
    data = b"VALUE = 2\n"
    (site / "demo" / "m.py").write_bytes(data)
    text = record.read_text(encoding="utf-8").splitlines()
    text = [line for line in text if not line.startswith("demo/m.py,")]
    record.write_text("\n".join(text) + "\n", encoding="utf-8")
    _add_row(record, "demo/m.py", data)


def _plant_uv_cache(site: Path) -> None:
    data = b'{"timestamp":{}}'
    (site / "demo-1.0.dist-info" / "uv_cache.json").write_bytes(data)
    _add_row(site / "demo-1.0.dist-info" / "RECORD", "demo-1.0.dist-info/uv_cache.json", data)


def _plant_script(site: Path) -> None:
    data = b"#!/build/machine/python\n"
    (site / "bin").mkdir()
    (site / "bin" / "demo").write_bytes(data)
    _add_row(site / "demo-1.0.dist-info" / "RECORD", "bin/demo", data)


def _plant_escape(site: Path) -> None:
    _add_row(site / "demo-1.0.dist-info" / "RECORD", "../../escape.py", b"")


def _plant_missing(site: Path) -> None:
    (site / "demo" / "m.py").unlink()


def _plant_empty(site: Path) -> None:
    (site / "empty").mkdir()


def _plant_other_dist_info(site: Path) -> None:
    other = site / "other-1.0.dist-info"
    other.mkdir()
    (other / "METADATA").write_text("Name: other\nVersion: 1.0\n\n", encoding="ascii")


def _plant_no_dist_info(site: Path) -> None:
    shutil.rmtree(site / "demo-1.0.dist-info")


@pytest.mark.parametrize(
    ("plant", "problem"),
    [
        (_plant_lock, ".lock is in no wheel's RECORD"),
        (_plant_unlisted, "demo/extra.py is in no wheel's RECORD"),
        (_plant_pth, "evil.pth is in no wheel's RECORD"),
        (_plant_link, "symbolic link: demo/link.py"),
        (_plant_edit, "demo/m.py does not match its RECORD row"),
        (_plant_edit_and_record, "demo/m.py differs from the file in demo-1.0-py3-none-any.whl"),
        (_plant_edit_and_record, "demo-1.0.dist-info/RECORD lacks a line of its wheel's RECORD"),
        (_plant_uv_cache, "demo-1.0.dist-info/RECORD has a line its wheel's RECORD does not"),
        (_plant_script, "demo-1.0.dist-info/RECORD has a line its wheel's RECORD does not"),
        (_plant_escape, "names ../../escape.py, which is not a path in site"),
        (_plant_missing, "demo-1.0.dist-info/RECORD lists demo/m.py, which is not installed"),
        (_plant_empty, "empty folder: empty"),
        (_plant_other_dist_info, "other-1.0.dist-info comes from no wheel the build installed"),
        (_plant_no_dist_info, "demo-1.0-py3-none-any.whl is not installed"),
    ],
)
def test_record_problems_finds_what_was_planted(
    tmp_path: Path, plant: Callable[[Path], None], problem: str
) -> None:
    site, wheel = _installed_demo(tmp_path)
    assert payload.record_problems(site, [wheel]) == []
    plant(site)
    problems = payload.record_problems(site, [wheel])
    assert any(problem in line for line in problems), problems


def test_record_problems_is_not_vacuous(tmp_path: Path) -> None:
    wheel = _make_wheel(tmp_path / "wheels")
    (tmp_path / "site").mkdir()
    assert payload.record_problems(tmp_path / "site", [wheel]) == [
        "demo-1.0-py3-none-any.whl is not installed: there is no demo-1.0.dist-info"
    ]


def test_clean_installer_output_removes_what_uv_adds(tmp_path: Path) -> None:
    site, wheel = _installed_demo(tmp_path)
    for plant in (_plant_lock, _plant_uv_cache, _plant_script):
        plant(site)
    assert payload.record_problems(site, [wheel]) != []
    assert payload.clean_installer_output(site) == [
        ".lock",
        "bin/",
        "demo-1.0.dist-info/uv_cache.json",
    ]
    assert payload.record_problems(site, [wheel]) == []
    assert payload.clean_installer_output(site) == []


# ---------------------------------------------------------------------------------------------
# Requirements, markers, tags and the choice of files


def test_requirements_are_read_with_their_markers(requirements_text: str) -> None:
    requirements = payload.parse_requirements(requirements_text)
    by_name = {req.name: req for req in requirements}
    assert len(requirements) == len(by_name) == 22
    assert by_name["jeepney"].marker == "sys_platform == 'linux'"
    assert by_name["keyring"].marker == "" and by_name["keyring"].version == "25.7.0"
    assert all(req.hashes for req in requirements)
    env = payload.marker_environment(PINS.python_version)
    kept = {req.name for req in requirements if payload.marker_holds(req.marker, env)}
    assert kept == EXPECTED_DISTRIBUTIONS


@pytest.mark.parametrize(
    "text",
    [
        "keyring==25.7.0\n",
        "keyring>=25.7.0 --hash=sha256:" + "a" * 64 + "\n",
        "keyring==25.7.0 --hash=md5:abc\n",
        "keyring==25.7.0 --hash=sha256:" + "A" * 64 + "\n",
        "keyring==25.7.0 --hash=sha256:" + "a" * 64 + "\nkeyring==25.7.0 --hash=sha256:" + "b" * 64,
        "-e . --hash=sha256:" + "a" * 64 + "\n",
    ],
)
def test_a_malformed_requirement_is_refused(text: str) -> None:
    with pytest.raises(payload.AssemblyError):
        payload.parse_requirements(text)


@pytest.mark.parametrize(
    ("marker", "holds"),
    [
        ("", True),
        ("python_full_version < '3.12'", False),
        ("python_full_version < '3.15'", True),
        ("python_version >= '3.13'", True),
        ("'3.12' < python_version", True),
        ("sys_platform == 'linux'", False),
        ("sys_platform == 'win32'", False),
        ("sys_platform == 'darwin'", True),
        ("platform_python_implementation != 'PyPy'", True),
        ("implementation_name != 'PyPy' and platform_python_implementation != 'PyPy'", True),
        ("sys_platform == 'linux' or platform_machine == 'arm64'", True),
        (
            "(sys_platform == 'linux' or sys_platform == 'darwin') and python_version < '3.12'",
            False,
        ),
        ('platform_system == "Darwin"', True),
    ],
)
def test_markers_on_the_apps_runtime(marker: str, holds: bool) -> None:
    env = payload.marker_environment("3.13.18")
    assert payload.marker_holds(marker, env) is holds


@pytest.mark.parametrize(
    "marker",
    [
        "extra == 'server'",
        "sys_platform in 'darwin linux'",
        "python_version ~= '3.13'",
        "platform_release >= '20'",
        "sys_platform < 'linux'",
        "(sys_platform == 'darwin'",
        "sys_platform == 'darwin' and",
        "python_version >= '3.13a1'",
    ],
)
def test_a_marker_the_build_cannot_read_is_refused(marker: str) -> None:
    with pytest.raises(payload.AssemblyError):
        payload.marker_holds(marker, payload.marker_environment("3.13.18"))


def test_wheel_tags_expand_compressed_tag_sets() -> None:
    assert payload.wheel_tags("cryptography-50.0.2-cp315-abi3.abi3t-macosx_11_0_arm64.whl") == (
        "cryptography",
        "50.0.2",
        frozenset(
            {
                ("cp315", "abi3", "macosx_11_0_arm64"),
                ("cp315", "abi3t", "macosx_11_0_arm64"),
            }
        ),
    )
    assert payload.wheel_tags("cryptography-50.0.2-pp311-pypy311_pp80-macosx_11_0_arm64.whl")[
        2
    ] == frozenset({("pp311", "pypy311_pp80", "macosx_11_0_arm64")})
    assert payload.wheel_tags("x-1.0-1build-py2.py3-none-any.whl")[2] == frozenset(
        {("py2", "none", "any"), ("py3", "none", "any")}
    )
    assert payload.wheel_tags("x-1.0-cp313-cp313-macosx_10_9_universal2.macosx_11_0_arm64.whl")[
        2
    ] == frozenset(
        {("cp313", "cp313", "macosx_10_9_universal2"), ("cp313", "cp313", "macosx_11_0_arm64")}
    )
    assert payload.wheel_tags("jaraco.classes-3.4.0-py3-none-any.whl")[0] == "jaraco-classes"
    for name in ("x-1.0.tar.gz", "x-1.0-py3-none.whl", "a/b-1.0-py3-none-any.whl"):
        with pytest.raises(payload.AssemblyError):
            payload.wheel_tags(name)
    # None of the compressed sets above is for this runtime, and a free-threaded build is not.
    for name in (
        "cryptography-50.0.2-cp315-abi3.abi3t-macosx_11_0_arm64.whl",
        "cryptography-50.0.2-pp311-pypy311_pp80-macosx_11_0_arm64.whl",
        "cryptography-50.0.2-cp314-cp314t-macosx_11_0_arm64.whl",
        "x-1.0-cp313-cp313-manylinux_2_28_aarch64.whl",
        "x-1.0-cp313-cp313-macosx_11_0_x86_64.whl",
    ):
        assert payload.wheel_rank(name, PYTHON, MACOS) is None, name


def test_supported_tags_are_packagings() -> None:
    # The order packaging (pip's and uv's tag logic) gives for CPython 3.13 on macOS 15, arm64.
    from packaging import tags

    platforms = list(tags.mac_platforms((15, 0), "arm64"))
    expected = [
        (tag.interpreter, tag.abi, tag.platform)
        for tag in (
            *tags.cpython_tags((3, 13), abis=["cp313"], platforms=platforms),
            *tags.compatible_tags((3, 13), interpreter="cp313", platforms=platforms),
        )
    ]
    assert list(payload.supported_tags(PYTHON, MACOS)) == expected


def test_wheel_selection_matches_the_lock(requirements_text: str) -> None:
    lock = payload.parse_lock(build_app.LOCK_FILE.read_text(encoding="utf-8"))
    env = payload.marker_environment(PINS.python_version)
    wanted = [
        req
        for req in payload.parse_requirements(requirements_text)
        if payload.marker_holds(req.marker, env)
    ]
    chosen = {
        wheel.name: wheel.filename for wheel in payload.plan_downloads(lock, wanted, PYTHON, MACOS)
    }
    assert set(chosen) == EXPECTED_DISTRIBUTIONS
    # cryptography has two files this runtime accepts; cp311-abi3 ranks above cp39-abi3.
    assert chosen["cryptography"] == "cryptography-50.0.2-cp311-abi3-macosx_11_0_arm64.whl"
    assert chosen["cffi"] == "cffi-2.1.1-cp313-cp313-macosx_11_0_arm64.whl"
    for name, filename in chosen.items():
        if name not in ("cffi", "cryptography"):
            assert filename.endswith("-py3-none-any.whl"), filename
    files = lock[("cryptography", "50.0.2")]
    ranks = {wheel.filename: payload.wheel_rank(wheel.filename, PYTHON, MACOS) for wheel in files}
    assert (
        ranks["cryptography-50.0.2-cp311-abi3-macosx_11_0_arm64.whl"]
        < ranks["cryptography-50.0.2-cp39-abi3-macosx_11_0_arm64.whl"]
    )


def _lock_wheel(filename: str, sha: str = "a" * 64) -> payload.LockWheel:
    name, version, _ = payload.wheel_tags(filename)
    return payload.LockWheel(name, version, filename, f"https://example.invalid/{filename}", sha)


def test_wheel_selection_refuses_no_file_and_a_tie() -> None:
    with pytest.raises(payload.AssemblyError, match="no wheel for CPython 3.13"):
        payload.select_wheel([_lock_wheel("x-1.0-cp313-cp313-win_amd64.whl")], PYTHON, MACOS)
    with pytest.raises(payload.AssemblyError, match="no wheel"):
        payload.select_wheel([], PYTHON, MACOS)
    tie = [_lock_wheel("x-1.0-py3-none-any.whl"), _lock_wheel("x-1.0-1-py3-none-any.whl")]
    with pytest.raises(payload.AssemblyError, match="equally preferred"):
        payload.select_wheel(tie, PYTHON, MACOS)
    requirement = payload.Requirement("x", "1.0", ("b" * 64,), "")
    lock = {("x", "1.0"): (_lock_wheel("x-1.0-py3-none-any.whl"),)}
    with pytest.raises(payload.AssemblyError, match="not among the hashes"):
        payload.plan_downloads(lock, [requirement], PYTHON, MACOS)
    with pytest.raises(payload.AssemblyError, match="uv.lock has no wheel for y==1.0"):
        payload.plan_downloads(
            lock, [payload.Requirement("y", "1.0", ("a" * 64,), "")], PYTHON, MACOS
        )


def test_install_argv(tmp_path: Path) -> None:
    argv = payload.install_argv(
        tmp_path / "site", tmp_path / "links", tmp_path / "r.txt", "3.13.18"
    )
    assert argv[:3] == ["uv", "pip", "install"]
    pairs = {argv[i]: argv[i + 1] for i in range(3, len(argv) - 1) if argv[i].startswith("--")}
    assert pairs["--target"] == str(tmp_path / "site")
    assert pairs["--python-platform"] == "aarch64-apple-darwin"
    assert pairs["--python-version"] == "3.13.18"
    assert pairs["--only-binary"] == ":all:"
    assert pairs["--find-links"] == str(tmp_path / "links")
    assert pairs["--link-mode"] == "copy"
    for flag in (
        "--require-hashes",
        "--no-deps",
        "--no-compile",
        "--no-index",
        "--no-cache",
        "--no-config",
        "--no-python-downloads",
    ):
        assert flag in argv
    assert argv[-2:] == ["-r", str(tmp_path / "r.txt")]
    assert "--compile" not in argv and "--index-url" not in argv


def test_uv_env_drops_every_uv_variable() -> None:
    environ = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "UV_FIND_LINKS": "/elsewhere",
        "UV_INDEX_URL": "https://example.invalid/simple",
        "UV_OVERRIDE": "o.txt",
        "UV_CONSTRAINT": "c.txt",
        "UV_EXCLUDE_NEWER": "2020-01-01",
    }
    env = payload.uv_env(environ, "15.0")
    assert env == {"PATH": "/usr/bin", "HOME": "/home/x", "MACOSX_DEPLOYMENT_TARGET": "15.0"}


def test_a_cached_wheel_is_hashed_again(tmp_path: Path) -> None:
    good = b"the real wheel"
    wheel = _lock_wheel("x-1.0-py3-none-any.whl", hashlib.sha256(good).hexdigest())
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / wheel.filename).write_bytes(good)
    found = payload.fetch_wheels([wheel], cache, tmp_path / "links")
    assert [path.name for path in (tmp_path / "links").iterdir()] == [wheel.filename]
    assert found[0].path.read_bytes() == good
    # The folder uv reads holds exactly the planned files: anything left there is removed.
    (tmp_path / "links" / "stale-1.0-py3-none-any.whl").write_bytes(b"")
    payload.fetch_wheels([wheel], cache, tmp_path / "links")
    assert [path.name for path in (tmp_path / "links").iterdir()] == [wheel.filename]
    with pytest.raises(payload.AssemblyError, match="refusing to download"):
        payload.fetch_wheels(
            [payload.LockWheel("x", "1.0", wheel.filename, "http://plain", wheel.sha256)],
            tmp_path / "empty-cache",
            tmp_path / "links",
        )


# ---------------------------------------------------------------------------------------------
# The runtime's pip, uninstalled by its RECORD


def _pip_runtime(root: Path) -> tuple[Path, Path]:
    site = root / "lib" / "python3.13" / "site-packages"
    (site / "pip" / "_vendor").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "bin" / "python3.13").write_bytes(b"interpreter")
    files = {
        "pip/__init__.py": b"",
        "pip/_vendor/x.py": b"x = 1\n",
        "../../../bin/pip": b"#!/runtime/bin/python3.13\nstub\n",
    }
    for rel, data in files.items():
        (site / rel).write_bytes(data)
    info = site / "pip-26.2.2.dist-info"
    (info / "licenses").mkdir(parents=True)
    (info / "licenses" / "LICENSE.txt").write_text("MIT\n", encoding="ascii")
    rows = [f"{rel},{payload.record_hash(data)},{len(data)}" for rel, data in files.items()]
    rows[2] = "../../../bin/pip,sha256=not-the-file,208"  # rewritten after it was recorded
    rows.append("pip/__pycache__/__init__.cpython-313.pyc,sha256=absent,10")  # stripped
    rows.append("pip-26.2.2.dist-info/RECORD,,")
    (info / "RECORD").write_text("\n".join(rows) + "\n", encoding="ascii")
    (site / "README.txt").write_text("site-packages\n", encoding="ascii")
    return site, info


def test_uninstall_by_record_is_tolerant(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    site, _ = _pip_runtime(root)
    assert payload.uninstall_by_record(root, site, "pip") == "pip==26.2.2"
    assert sorted(path.name for path in site.iterdir()) == ["README.txt"]
    assert sorted(path.name for path in (root / "bin").iterdir()) == ["python3.13"]


def test_uninstall_by_record_refuses_a_path_outside_the_runtime(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    site, info = _pip_runtime(root)
    (tmp_path / "outside").write_text("keep me\n", encoding="ascii")
    _add_row(info / "RECORD", "../../../../outside", b"keep me\n")
    with pytest.raises(payload.AssemblyError, match="outside"):
        payload.uninstall_by_record(root, site, "pip")
    # Refused before anything was removed.
    assert (tmp_path / "outside").is_file() and (site / "pip" / "__init__.py").is_file()
    assert (root / "bin" / "pip").is_file() and info.is_dir()


def test_uninstall_by_record_refuses_a_folder_before_removing_anything(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    site, info = _pip_runtime(root)
    record = info / "RECORD"
    record.write_text(record.read_text(encoding="ascii") + "pip/_vendor,,\n", encoding="ascii")
    with pytest.raises(payload.AssemblyError, match="pip/_vendor, which is a folder"):
        payload.uninstall_by_record(root, site, "pip")
    assert (site / "pip" / "__init__.py").is_file() and (root / "bin" / "pip").is_file()


def test_uninstall_by_record_needs_exactly_one_distribution(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    site, _ = _pip_runtime(root)
    with pytest.raises(payload.AssemblyError, match="expected one setuptools"):
        payload.uninstall_by_record(root, site, "setuptools")
    (site / "pip-1.0.dist-info").mkdir()
    with pytest.raises(payload.AssemblyError, match="found 2"):
        payload.uninstall_by_record(root, site, "pip")


# ---------------------------------------------------------------------------------------------
# The unsigned tarball


def _tree(top: Path, order: list[str], mode: int) -> None:
    contents = {
        "Contents/Info.plist": b"<plist/>",
        "Contents/MacOS/Capsule": b"\xcf\xfa\xed\xfe binary",
        "Contents/Resources/runtime/lib/python3.13/os.py": b"import sys\n",
        "Contents/Resources/bin/lucille-host": b"#!/bin/sh\n",
    }
    for rel in order:
        path = top / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents[rel])
        path.chmod(0o755 if rel.endswith(("Capsule", "lucille-host")) else mode)


def test_tarball_is_deterministic(tmp_path: Path) -> None:
    names = [
        "Contents/Info.plist",
        "Contents/MacOS/Capsule",
        "Contents/Resources/runtime/lib/python3.13/os.py",
        "Contents/Resources/bin/lucille-host",
    ]
    first = tmp_path / "one" / "Capsule.app"
    second = tmp_path / "two" / "Capsule.app"
    _tree(first, names, 0o644)
    _tree(second, list(reversed(names)), 0o664)
    os.utime(second / "Contents" / "Info.plist", (1, 1))
    a = payload.write_tarball(first, tmp_path / "a.tar.gz", 1700000000)
    b = payload.write_tarball(second, tmp_path / "b.tar.gz", 1700000000)
    assert a == b
    assert a == payload.tarball_digests(tmp_path / "a.tar.gz")
    raw = (tmp_path / "a.tar.gz").read_bytes()
    assert raw[:3] == b"\x1f\x8b\x08"
    assert raw[3] & 0x08 == 0, "no file name in the gzip header"
    assert raw[4:8] == bytes(4), "no time in the gzip header"
    with tarfile.open(tmp_path / "a.tar.gz", "r:gz") as tar:
        members = tar.getmembers()
    assert [member.name for member in members][:3] == [
        "Capsule.app",
        "Capsule.app/Contents",
        "Capsule.app/Contents/Info.plist",
    ]
    assert [member.name for member in members] == sorted(
        (member.name for member in members), key=lambda name: name.split("/")
    )
    for member in members:
        assert (member.uid, member.gid, member.uname, member.gname) == (0, 0, "", "")
        assert member.mtime == 1700000000
        assert member.isdir() or member.isfile()
        executable = member.isdir() or member.name.endswith(("Capsule", "lucille-host"))
        assert member.mode == (0o755 if executable else 0o644), member.name
    assert payload.tar_differences(tmp_path / "a.tar.gz", tmp_path / "b.tar.gz") == []
    (second / "Contents" / "Info.plist").write_bytes(b"<plist>changed</plist>")
    payload.write_tarball(second, tmp_path / "b.tar.gz", 1700000000)
    assert payload.tar_differences(tmp_path / "a.tar.gz", tmp_path / "b.tar.gz") == [
        "Capsule.app/Contents/Info.plist"
    ]


def test_tarball_refuses_a_link_and_flattens_a_hard_link(tmp_path: Path) -> None:
    top = tmp_path / "Capsule.app"
    top.mkdir()
    (top / "a").write_bytes(b"same file")
    os.link(top / "a", top / "b")
    payload.write_tarball(top, tmp_path / "t.tar.gz", 0)
    with tarfile.open(tmp_path / "t.tar.gz", "r:gz") as tar:
        assert [(m.name, m.type) for m in tar.getmembers()] == [
            ("Capsule.app", tarfile.DIRTYPE),
            ("Capsule.app/a", tarfile.REGTYPE),
            ("Capsule.app/b", tarfile.REGTYPE),
        ]
        handle = tar.extractfile("Capsule.app/b")
        assert handle is not None and handle.read() == b"same file"
    (top / "c").symlink_to("a")
    with pytest.raises(payload.AssemblyError, match="symbolic link"):
        payload.write_tarball(top, tmp_path / "t.tar.gz", 0)


def test_tarball_digests_read_the_tar_inside(tmp_path: Path) -> None:
    data = b"not really a tar"
    path = tmp_path / "x.tar.gz"
    with path.open("wb") as out, gzip.GzipFile(filename="", fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(data)
    digests = payload.tarball_digests(path)
    assert digests.tar_sha256 == hashlib.sha256(data).hexdigest()
    assert digests.gzip_sha256 == payload.sha256_of(path)
