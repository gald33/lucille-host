"""The parts of tools/macos/build_app.py and tools/macos/check_app.py that run anywhere: the pins,
the sha256 gate, the hashed build backend, Info.plist, the disk image's name, the parsers the
checks use, the bytecode program (run here by the interpreter that runs these tests), and the
rules the Swift sources keep. The build itself runs on a Mac, in ci.yml's macos-app job, whose
steps tests/test_ci_workflow.py reads.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import types
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

import lucille_host
from lucille_host import policy
from lucille_host.cli import build_parser
from lucille_host.ops import OPERATIONS
from tools.guards import ast_lint, macos_lint
from tools.macos import build_app, check_app, payload

PINS_TEXT = build_app.PINS_FILE.read_text(encoding="utf-8")
CONSTRAINTS_TEXT = build_app.BUILD_CONSTRAINTS.read_text(encoding="utf-8")
SWIFT_FILES = sorted(build_app.SWIFT_DIR.glob("*.swift"))
WORKFLOW = build_app.REPO_ROOT / ".github" / "workflows" / "ci.yml"


def test_the_pins_are_well_formed() -> None:
    pins = build_app.load_pins()
    assert pins.python_version.startswith("3.13.")
    assert pins.asset.startswith(f"cpython-{pins.python_version}+")
    assert pins.asset.endswith("-aarch64-apple-darwin-install_only_stripped.tar.gz")
    assert pins.url.startswith(build_app.PBS_RELEASES)
    assert pins.url.endswith("/" + pins.asset.replace("+", "%2B"))
    assert re.fullmatch(r"[0-9a-f]{64}", pins.sha256)
    assert pins.app_name == "Capsule"
    assert pins.bundle_id == "com.lucille-ai.capsule"
    assert pins.runtime_id == "com.lucille-ai.capsule.runtime"
    assert pins.deployment_target == "15.0"
    assert re.fullmatch(r"\d+\.\d+(\.\d+)?", pins.xcode)
    assert pins.ripgrep_commit == ""
    assert isinstance(pins.adhoc_hardened_runtime, bool)
    assert build_app.PINS_FILE == build_app.TOOLS_DIR / "pins.toml"
    assert (
        build_app.PINS_FILE.relative_to(build_app.REPO_ROOT).as_posix() == "tools/macos/pins.toml"
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('sha256 = "8392', 'sha256 = "8392Z'),
        ('sha256 = "8392b6ea', 'sha256 = "8392B6EA'),
        ("releases/download/20261026/", "releases/download/20261020/"),
        ("https://github.com/astral-sh/", "https://example.com/astral-sh/"),
        ('version = "3.13.18"', 'version = "3.13.17"'),
        (
            'aarch64-apple-darwin-install_only_stripped.tar.gz"\nurl',
            'x86_64-apple-darwin-install_only_stripped.tar.gz"\nurl',
        ),
        ('deployment_target = "15.0"', 'deployment_target = "14.0"'),
        ('deployment_target = "15.0"', 'deployment_target = "15"'),
        ('deployment_target = "15.0"', 'minimum_macos = "15.0"'),
        ('bundle_id = "com.lucille-ai.capsule"', 'bundle_id = "Capsule"'),
        (
            'runtime_id = "com.lucille-ai.capsule.runtime"',
            'runtime_id = "com.lucille-ai.capsule.py"',
        ),
        ('runtime_id = "com.lucille-ai.capsule.runtime"', 'runtime_id = ""'),
        ('version = "16.4"', 'version = "latest"'),
        ('version = "16.4"', 'version = "16.4\\nEVIL=1"'),
        ('version = "16.4"', "version = 16.4"),
        ("[xcode]", "[xcodes]"),
        ('commit = ""', 'commit = "main"'),
        ('commit = ""', 'commit = "4466aba"'),
        ('commit = ""', "commit = false"),
        ("adhoc_hardened_runtime = true", 'adhoc_hardened_runtime = "yes"'),
        ("adhoc_hardened_runtime = true", "adhoc_hardened_runtime = 1"),
        ("[signing]", "[signing]\nnotarize = false"),
        ("[ripgrep]", '[extra]\nx = "y"\n\n[ripgrep]'),
        ('name = "Capsule"', 'name = "Capsule/Capsule"'),
        ('name = "Capsule"', 'name = " Capsule"'),
        ('name = "Capsule"', 'name = "Capsule  Two"'),
        ('name = "Capsule"', 'name = "C*"'),
        ('name = "Capsule"', 'name = "Cap?ule"'),
        ('name = "Capsule"', 'name = "Cap[s]ule"'),
        ('name = "Capsule"', 'name = "Capsule:"'),
        ('name = "Capsule"', 'name = ""'),
        ('category = "public.app-category.utilities"', 'category = "utilities"'),
        ("[app]", '[app]\nextra = "x"'),
        ("[app]", "[apps]"),
    ],
)
def test_a_changed_pin_is_refused(old: str, new: str) -> None:
    assert old in PINS_TEXT
    with pytest.raises(build_app.PinError):
        build_app.parse_pins(PINS_TEXT.replace(old, new, 1))


def test_a_pins_file_without_a_table_is_refused() -> None:
    for table in ("xcode", "ripgrep", "signing"):
        text = PINS_TEXT.replace(f"[{table}]", "#", 1)
        with pytest.raises(build_app.PinError):
            build_app.parse_pins(text)
    # A pin the build reads, with no other change, still parses: the refusals above are each
    # for the one change they make.
    assert build_app.parse_pins(PINS_TEXT.replace('commit = ""', 'commit = "' + "a" * 40 + '"'))
    off = build_app.parse_pins(PINS_TEXT.replace("= true", "= false"))
    assert off.adhoc_hardened_runtime is False


def _host_wheel(folder: Path) -> tuple[Path, str]:
    """A file named as this checkout's host wheel, and its sha256. The checks that read it run
    before anything else, so it need not be a real wheel."""
    folder.mkdir(parents=True, exist_ok=True)
    wheel = folder / payload.host_wheel_name(build_app.host_version())
    wheel.write_bytes(b"a stand-in for the host wheel")
    return wheel, hashlib.sha256(wheel.read_bytes()).hexdigest()


def test_runtime_sha256_mismatch_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pins = build_app.load_pins()
    archive = tmp_path / pins.asset
    archive.write_bytes(b"not the pinned runtime")
    with pytest.raises(
        build_app.BuildError, match=r"tools/macos/pins\.toml pins .*refusing to build"
    ):
        build_app.fetch_runtime(pins, tmp_path / "cache", archive)
    # A cached download that does not match is refused and deleted, so the next build fetches it
    # again; no network is needed to reach the check, because the file is already there.
    cache = tmp_path / "cache"
    cache.mkdir()
    cached = cache / pins.asset
    cached.write_bytes(b"a tampered download")
    with pytest.raises(build_app.BuildError, match="refusing to build"):
        build_app.fetch_runtime(pins, cache)
    assert not cached.exists()
    # The commands refuse other bytes before any work, on any system: nothing is written.
    wheel, digest = _host_wheel(tmp_path / "wheel")
    for command in ("assemble", "build"):
        out = tmp_path / f"out-{command}"
        argv = [command, "--out", str(out), "--python-archive", str(archive)]
        argv += ["--host-wheel", str(wheel), "--host-wheel-sha256", digest, "--build-number", "1"]
        assert build_app.main(argv) == 1
        err = capsys.readouterr().err
        assert "tools/macos/pins.toml pins" in err and "refusing to build" in err, err
        assert not out.exists()


def test_host_wheel_sha256_mismatch_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wheel, digest = _host_wheel(tmp_path / "wheel")
    other = tmp_path / "wheel" / "lucille_host-9.9.9-py3-none-any.whl"
    other.write_bytes(wheel.read_bytes())
    cases = [
        (wheel, "0" * 64, f"not the {'0' * 64} given; refusing to build"),
        (wheel, digest.upper(), "must be 64 lower-case hexadecimal digits"),
        (other, digest, f"must be a file named {wheel.name}; refusing to build"),
        (tmp_path / "missing" / wheel.name, digest, "refusing to build"),
    ]
    for path, sha, said in cases:
        out = tmp_path / "out"
        argv = ["build", "--out", str(out), "--host-wheel", str(path)]
        argv += ["--host-wheel-sha256", sha, "--build-number", "1"]
        assert build_app.main(argv) == 1
        assert said in capsys.readouterr().err
        assert not out.exists()


def test_a_runtime_whose_sha256_matches_is_used(tmp_path: Path) -> None:
    content = b"stand-in runtime"
    pins = dataclasses.replace(build_app.load_pins(), sha256=hashlib.sha256(content).hexdigest())
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / pins.asset).write_bytes(content)
    assert build_app.fetch_runtime(pins, cache) == cache / pins.asset


def test_the_version_is_the_packages() -> None:
    assert build_app.host_version() == lucille_host.__version__


@pytest.mark.parametrize(
    ("version", "triple"),
    [
        ("0.1.0.dev0", "0.1.0"),
        ("0.1.0", "0.1.0"),
        ("1.2", "1.2.0"),
        ("3", "3.0.0"),
        ("2.0.1rc1", "2.0.1"),
        ("1.2.3.4", "1.2.3"),
    ],
)
def test_release_triple(version: str, triple: str) -> None:
    assert build_app.release_triple(version) == triple


# The six folder usage keys (0002 §12: Documents, Desktop, Downloads, removable and network
# volumes, and file providers), each name as Apple's Information Property List reference spells it.
SIX_USAGE_KEYS = {
    "NSDocumentsFolderUsageDescription",
    "NSDesktopFolderUsageDescription",
    "NSDownloadsFolderUsageDescription",
    "NSRemovableVolumesUsageDescription",
    "NSNetworkVolumesUsageDescription",
    "NSFileProviderDomainUsageDescription",
}


def test_plist_keys() -> None:
    pins = build_app.load_pins()
    plist = build_app.info_plist(pins, build_app.bundle_versions("0.1.0.dev0", None, "523"))
    assert set(plist) == {
        "CFBundleDevelopmentRegion",
        "CFBundleDisplayName",
        "CFBundleExecutable",
        "CFBundleIdentifier",
        "CFBundleInfoDictionaryVersion",
        "CFBundleName",
        "CFBundlePackageType",
        "CFBundleShortVersionString",
        "CFBundleVersion",
        "LSApplicationCategoryType",
        "LSArchitecturePriority",
        "LSMinimumSystemVersion",
        "LSUIElement",
        "NSHumanReadableCopyright",
        "NSSupportsAutomaticTermination",
        "NSSupportsSuddenTermination",
        *SIX_USAGE_KEYS,
    }
    assert plist["LSUIElement"] is True
    assert plist["CFBundleIdentifier"] == pins.bundle_id
    assert plist["CFBundleExecutable"] == plist["CFBundleName"] == pins.app_name
    # The app reads its name from this key (HostText.appName).
    assert plist["CFBundleDisplayName"] == pins.app_name
    assert plist["CFBundlePackageType"] == "APPL"
    assert plist["CFBundleShortVersionString"] == "0.1.0"
    assert plist["CFBundleVersion"] == "523"
    assert plist["LSMinimumSystemVersion"] == "15.0" == pins.deployment_target
    assert plist["LSArchitecturePriority"] == ["arm64"]
    assert plist["NSSupportsAutomaticTermination"] is False
    assert plist["NSSupportsSuddenTermination"] is False
    # No URL scheme, no App Transport Security exception, no scripting, no update feed.
    assert not build_app.FORBIDDEN_PLIST_KEYS & set(plist)
    assert {"CFBundleURLTypes", "CFBundleURLSchemes", "NSAppTransportSecurity"} <= (
        build_app.FORBIDDEN_PLIST_KEYS
    )
    usage = {key: value for key, value in plist.items() if key.endswith("UsageDescription")}
    assert set(usage) == set(build_app.FOLDER_USAGE_KEYS) == SIX_USAGE_KEYS
    for text in usage.values():
        assert not re.search(r"\b(safe|secure|private|protect)", text, re.IGNORECASE)
    assert plistlib.loads(plistlib.dumps(plist)) == plist


def _plist_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for key, item in value.items() for s in (key, *_plist_strings(item))]
    if isinstance(value, list):
        return [s for item in value for s in _plist_strings(item)]
    return []


def test_info_plist_values_make_no_forbidden_claim() -> None:
    # macos_lint reads the plist functions' string constants and the pins' [app] strings; this
    # reads every string the rendered Info.plist holds, whatever it was built from.
    phrases = macos_lint.must_not_claim_phrases(
        (build_app.REPO_ROOT / "PRIVACY.md").read_text(encoding="utf-8")
    )
    for versions in (
        build_app.bundle_versions("0.1.0.dev0", None, "523"),
        build_app.bundle_versions("1.2.3", "v1.2.3", None),
    ):
        strings = _plist_strings(build_app.info_plist(build_app.load_pins(), versions))
        assert build_app.folder_usage(build_app.load_pins().app_name) in strings
        for text in strings:
            assert macos_lint.claims(text, phrases) == [], text
    # The check is not vacuous: a name the pins could hold is caught.
    named = dataclasses.replace(build_app.load_pins(), app_name="Sandboxed Capsule")
    plist = build_app.info_plist(named, build_app.bundle_versions("1.2.3", "v1.2.3", None))
    assert any(macos_lint.claims(text, phrases) for text in _plist_strings(plist))


def test_runtime_flags_match_the_app() -> None:
    # The lint holds the Swift process file to the runtime path and flags the build uses.
    assert macos_lint.RUNTIME_IN_APP == build_app.RUNTIME_IN_APP
    assert macos_lint.RUNTIME_ARGV == (*build_app.RUNTIME_FLAGS, "-m", "lucille_host")
    wrapper_argv = f'{" ".join(macos_lint.RUNTIME_ARGV)} --profile {build_app.APP_PROFILE} "$@"'
    assert wrapper_argv in build_app.WRAPPER_TEXT
    # ... and reads the files the build reads.
    assert macos_lint.PLIST_SOURCE == "tools/macos/build_app.py"
    assert (build_app.REPO_ROOT / macos_lint.PLIST_SOURCE).resolve() == (
        build_app.TOOLS_DIR / "build_app.py"
    )
    assert build_app.REPO_ROOT / macos_lint.PINS_FILE == build_app.PINS_FILE
    assert (build_app.REPO_ROOT / macos_lint.PROCESS_FILE).parent == build_app.SWIFT_DIR
    # The functions whose strings the lint reads are the two that render Info.plist (both are
    # called by name in this file, so a rename fails here and in the lint).
    assert macos_lint.PLIST_FUNCTIONS == ("info_plist", "folder_usage")


def _swift_array(items: tuple[str, ...]) -> str:
    return "[" + ", ".join(f'"{item}"' for item in items) + "]"


def test_the_app_and_its_self_test_run_the_bundled_runtime() -> None:
    # The lint sees only that HostCLI.swift writes the runtime's path and arguments. These lines
    # are where its Process takes them, and the self-test, which the lint does not read for this,
    # expects the same arguments. Both compile only on a Mac, so their text is pinned here.
    cli = (build_app.REPO_ROOT / macos_lint.PROCESS_FILE).read_text(encoding="utf-8")
    assert (
        "static func python(inApp app: URL) -> URL {\n"
        f'        app.appendingPathComponent("{build_app.RUNTIME_IN_APP}")\n'
        "    }"
    ) in cli
    # The runtime's arguments, then the app's own profile (0002 §9), then the verb.
    assert (
        "func arguments(for verb: [String]) -> [String] {\n"
        f'        {_swift_array(macos_lint.RUNTIME_ARGV)} + ["--profile", profile] + verb\n'
        "    }"
    ) in cli
    assert "process.executableURL = python\n" in cli
    assert "process.arguments = arguments(for: verb)\n" in cli
    selftest = (build_app.MACOS_DIR / "Tests" / "SelfTest.swift").read_text(encoding="utf-8")
    expected = _swift_array((*macos_lint.RUNTIME_ARGV, "--profile", "mac-app", "pause"))
    assert f'cli.arguments(for: ["pause"]) == {expected}' in selftest
    assert "HostCLI(python: HostCLI.python(inApp: app), home: home)" in selftest
    # The self-test's stand-in for the interpreter skips exactly those arguments before the verb.
    skipped = len(macos_lint.RUNTIME_ARGV) + 2
    assert f"\n            shift {skipped}\n" in selftest
    assert selftest.count("\n            shift ") == 1
    for source in (cli, selftest):
        assert "python/bin/python3" not in source
        assert '["-I", "-B", "-m"' not in source


@pytest.mark.parametrize(
    ("version", "build_number", "short"),
    [("0.1.0.dev0", "523", "0.1.0"), ("1.2", "7", "1.2.0"), ("2.0.1rc1", "1", "2.0.1")],
)
def test_version_rule_maps_dev_builds(version: str, build_number: str, short: str) -> None:
    versions = build_app.bundle_versions(version, None, build_number)
    assert (versions.short, versions.build) == (short, build_number)
    plist = build_app.info_plist(build_app.load_pins(), versions)
    assert plist["CFBundleShortVersionString"] == short
    assert plist["CFBundleVersion"] == build_number
    for bad in ("0", "", "x", "-1", "1.2", "01", " 7", "7\n"):
        with pytest.raises(build_app.BuildError):
            build_app.bundle_versions(version, None, bad)
    with pytest.raises(build_app.BuildError, match="pass --build-number"):
        build_app.bundle_versions(version, None, None)


def test_version_rule_refuses_suffix_on_tag_builds() -> None:
    versions = build_app.bundle_versions("0.1.0", "v0.1.0", None)
    assert (versions.short, versions.build) == ("0.1.0", "0.1.0")
    assert build_app.bundle_versions("12.0.3", "v12.0.3", None).build == "12.0.3"
    for version in ("0.1.0.dev0", "0.1.0+mac1", "0.1.0rc1", "0.1.0.post1", "0.1", "0.1.0.0"):
        with pytest.raises(build_app.BuildError, match="three numbers with no suffix"):
            build_app.bundle_versions(version, f"v{version}", None)
    for tag in ("0.1.0", "v0.1.1", "v0.1.0 ", "V0.1.0", "v0.1.0+mac1"):
        with pytest.raises(build_app.BuildError):
            build_app.bundle_versions("0.1.0", tag, None)
    with pytest.raises(build_app.BuildError, match="not both"):
        build_app.bundle_versions("0.1.0", "v0.1.0", "7")


@pytest.mark.parametrize("command", ["plan", "assemble", "build"])
def test_version_flags_are_exclusive(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [command, "--out", str(tmp_path / "out")]
    if command != "plan":
        argv += ["--host-wheel", str(tmp_path / "w.whl"), "--host-wheel-sha256", "0" * 64]
    with pytest.raises(SystemExit) as raised:
        build_app.main([*argv, "--tag", "v0.1.0", "--build-number", "1"])
    assert raised.value.code == 2
    assert "argument --build-number: not allowed with argument --tag" in capsys.readouterr().err
    if command != "plan":
        with pytest.raises(SystemExit) as raised:
            build_app.main(argv)
        assert raised.value.code == 2
        err = capsys.readouterr().err
        assert "one of the arguments --tag --build-number is required" in err
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "key",
    [
        "NSDocumentsFolderUsageDescription",
        "NSDesktopFolderUsageDescription",
        "NSDownloadsFolderUsageDescription",
        "NSRemovableVolumesUsageDescription",
        "NSNetworkVolumesUsageDescription",
        "NSFileProviderDomainUsageDescription",
    ],
)
def test_each_folder_question_says_what_the_app_is_for(key: str) -> None:
    # macOS is meant to show this text under its question about a protected folder or volume (not
    # yet tested on a Mac). It names Lucille, says that the user chooses, and says what Lucille
    # sees without being asked: host.describe is on by default.
    versions = build_app.BundleVersions("0.1.0", "1")
    text = build_app.info_plist(build_app.load_pins(), versions)[key]
    assert isinstance(text, str) and text.strip()
    assert text.startswith(f"{build_app.load_pins().app_name} lets Lucille ")
    assert "the folders you choose" in text and "abilities you turn on" in text
    assert "which abilities are on" in text and "names you gave your folders" in text
    assert [name for name, op in OPERATIONS.items() if op.default_enabled] == ["host.describe"]


def test_plan_writes_the_info_plist(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert build_app.main(["plan", "--out", str(tmp_path), "--build-number", "41"]) == 0
    with (tmp_path / "Info.plist").open("rb") as handle:
        written = plistlib.load(handle)
    versions = build_app.bundle_versions(lucille_host.__version__, None, "41")
    assert written == build_app.info_plist(build_app.load_pins(), versions)
    out = capsys.readouterr().out
    assert build_app.load_pins().sha256 in out and "placeholder" not in out
    # Without a build number the bundle version is 1, and plan says it is a placeholder.
    assert build_app.main(["plan", "--out", str(tmp_path)]) == 0
    assert "placeholder" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "darwin", reason="the refusal is for other systems")
def test_build_refuses_off_a_mac(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Valid arguments, so that the platform is what stops it: nothing is written first.
    wheel, digest = _host_wheel(tmp_path / "wheel")
    for command in ("assemble", "build"):
        out = tmp_path / "out"
        argv = [command, "--out", str(out), "--host-wheel", str(wheel)]
        argv += ["--host-wheel-sha256", digest, "--build-number", "1"]
        assert build_app.main(argv) == 1
        assert "runs on an Apple silicon Mac" in capsys.readouterr().err
        assert not out.exists()
    github_env = tmp_path / "github-env"
    assert build_app.main(["toolchain", "--github-env", str(github_env)]) == 1
    assert "runs on an Apple silicon Mac" in capsys.readouterr().err
    assert not github_env.exists()


def test_macho_detection(tmp_path: Path) -> None:
    thin = tmp_path / "deep" / "er" / "lib.so"
    thin.parent.mkdir(parents=True)
    thin.write_bytes(b"\xcf\xfa\xed\xfe" + bytes(28))
    fat = tmp_path / "tool"
    fat.write_bytes(b"\xca\xfe\xba\xbe" + bytes(28))
    (tmp_path / "script.py").write_text("print('not native')\n", encoding="ascii")
    (tmp_path / "empty").write_bytes(b"")
    (tmp_path / "link.so").symlink_to(thin)
    assert build_app.macho_files(tmp_path) == [thin, fat]
    assert build_app.symlinks_in(tmp_path) == [tmp_path / "link.so"]


def test_the_build_writes_bytecode_after_the_payload_and_before_the_tarball() -> None:
    # The installer compiles nothing; the bundled interpreter writes the .pyc files once
    # site-packages is filled and checked, and they are checked again, with M10, before the
    # tarball is written (0002 §6). No build script runs compileall or py_compile.
    for name in ("install_packages", "remove_after_install"):
        assert not hasattr(build_app, name), name
    assemble = _python_function(
        (build_app.TOOLS_DIR / "build_app.py").read_text(encoding="utf-8"), "assemble"
    )
    steps = (
        "assemble_site_packages(",
        "compile_bytecode(app)",
        "bytecode_layout_problems(app)",
        "bytecode_tag=BYTECODE_TAG",
        "write_tarball(",
    )
    places = [assemble.find(step) for step in steps]
    assert -1 not in places and places == sorted(places), dict(zip(steps, places, strict=True))
    assert "bytecode_in(app)" not in assemble
    for script in ("build_app.py", "payload.py", "check_app.py"):
        text = (build_app.TOOLS_DIR / script).read_text(encoding="utf-8")
        assert "compileall" not in text and "py_compile" not in text, script
    assert "--no-compile" in payload.install_argv(Path("t"), Path("f"), Path("r"), "3.13.18")


def test_the_pruned_modules_are_not_ones_the_runtime_imports() -> None:
    pruned = {
        Path(rel).name.removesuffix(".py")
        for rel in build_app.load_prune_list()
        if rel.startswith(build_app.STDLIB + "/")
        and "/lib-dynload/" not in rel
        and "/site-packages/" not in rel
        and "__pycache__" not in rel
    }
    assert pruned == {
        "config-3.13-darwin",
        "ensurepip",
        "idlelib",
        "tkinter",
        "turtle",
        "turtledemo",
    }
    assert not pruned & ast_lint.RUNTIME_IMPORTS


def test_the_alias_vectors_are_valid_host_aliases() -> None:
    vectors = json.loads(check_app.ALIAS_VECTORS.read_text(encoding="utf-8"))
    cases = [(vector["folder"], tuple(vector.get("taken", ()))) for vector in vectors]
    assert len(cases) == len(set(cases))
    for vector in vectors:
        assert set(vector) in ({"folder", "alias"}, {"folder", "taken", "alias"})
        assert policy.valid_alias(vector["alias"]), vector
        assert vector["alias"] not in vector.get("taken", ()), vector
        assert all(policy.valid_alias(alias) for alias in vector.get("taken", ())), vector
    assert any(vector["alias"] == "folder" for vector in vectors)
    assert sum(len(vector["alias"]) == 32 for vector in vectors) >= 2
    assert any(vector["alias"].endswith("-10") for vector in vectors)


# What a Swift file may import. The logic files run in the self-test without a screen, so they
# use Foundation alone; the app's other files add AppKit and SwiftUI, and nothing else.
_SWIFT_IMPORT = re.compile(
    r"^\s*(?:@\w+\s+)*import\s+(?:(?:typealias|struct|class|enum|protocol|let|var|func)\s+)?"
    r"(\w+)",
    re.MULTILINE,
)
_LOGIC_IMPORTS = {"Foundation", "Darwin", "Glibc"}
_APP_IMPORTS = _LOGIC_IMPORTS | {"AppKit", "SwiftUI"}


def _swift_imports(text: str) -> set[str]:
    return set(_SWIFT_IMPORT.findall(text))


def test_the_swift_import_scan_sees_each_form() -> None:
    text = "import Cocoa\n@_exported import Network\nimport struct WebKit.WKWebView\n  import os\n"
    assert _swift_imports(text) == {"Cocoa", "Network", "WebKit", "os"}


def test_the_logic_sources_draw_nothing() -> None:
    for name in check_app.LOGIC_SOURCES:
        text = (build_app.SWIFT_DIR / name).read_text(encoding="utf-8")
        assert _swift_imports(text) <= _LOGIC_IMPORTS, name


def test_the_self_test_compiles_every_file_that_draws_nothing() -> None:
    # The self-test compiles LOGIC_SOURCES and nothing else of the app. Every file that imports
    # no UI framework is in it, so its code runs on every build; no file that draws is.
    drawless = {
        path.name
        for path in SWIFT_FILES
        if _swift_imports(path.read_text(encoding="utf-8")) <= _LOGIC_IMPORTS
    }
    assert drawless == set(check_app.LOGIC_SOURCES)
    assert len(check_app.LOGIC_SOURCES) == len(drawless)


def test_the_app_imports_only_apples_ui_frameworks() -> None:
    assert SWIFT_FILES
    for path in SWIFT_FILES:
        assert _swift_imports(path.read_text(encoding="utf-8")) <= _APP_IMPORTS, path.name


# What the Swift sources may not do (start a process other than the bundled runtime, use the
# network, read the pasteboard, ...) is tools/guards/macos_lint.py's, tested in
# tests/guards/test_macos_lint.py.


def _swift_string_table(text: str, name: str) -> dict[str, str]:
    body = re.search(rf"static let {name}: \[String: String\] = \[(.*?)\]", text, re.DOTALL)
    assert body is not None, name
    return dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', body.group(1)))


def _swift_string_set(text: str, name: str) -> set[str]:
    body = re.search(rf"static let {name}: Set<String> = \[(.*?)\]", text, re.DOTALL)
    assert body is not None, name
    return set(re.findall(r'"([^"]+)"', body.group(1)))


def test_every_operation_has_plain_words_or_is_left_out_of_the_menu() -> None:
    text = (build_app.SWIFT_DIR / "HostText.swift").read_text(encoding="utf-8")
    titles = _swift_string_table(text, "abilityTitles")
    unlisted = _swift_string_set(text, "unlistedAbilities")
    assert not set(titles) & unlisted
    assert set(titles) | unlisted == set(OPERATIONS)
    assert {"host.sleep", "host.describe"} <= unlisted
    assert titles == check_app.ABILITY_TITLES
    for title in titles.values():
        assert title[0].isupper() and "." not in title, title


def test_every_listed_ability_says_what_lucille_gets() -> None:
    text = (build_app.SWIFT_DIR / "HostText.swift").read_text(encoding="utf-8")
    details = _swift_string_table(text, "abilityDetails")
    assert set(details) == set(_swift_string_table(text, "abilityTitles"))
    assert details == check_app.ABILITY_DETAILS
    for detail in details.values():
        assert detail.startswith("Lucille gets ") and detail.endswith("."), detail


def _named_in_swift(words: str, reference: str = "appName") -> str:
    """``words`` as the Swift source spells them: the app's name is read from the bundle there."""
    return words.replace(check_app.APP_NAME, f"\\({reference})")


def test_the_ui_check_looks_for_the_apps_own_words() -> None:
    text = (build_app.SWIFT_DIR / "HostText.swift").read_text(encoding="utf-8")
    model = (build_app.SWIFT_DIR / "HostModel.swift").read_text(encoding="utf-8")
    welcome = _named_in_swift(check_app.WELCOME_TITLE)
    assert f'static let welcomeTitle = "{welcome}"' in text
    assert f'static let pausedNote = "{check_app.PAUSED_NOTE}"' in text
    assert check_app.STATUS_ITEM_NAMES == (check_app.APP_NAME, f"{check_app.APP_NAME}, paused")
    assert 'paused ? "\\(appName), paused" : appName' in text
    prompt = _named_in_swift(check_app.OPEN_PROMPT, "HostText.appName")
    assert f'panel.prompt = "{prompt}"' in model
    assert "by the clock" in text
    assert '"Once this Mac is connected to Lucille, Lucille can use' in text
    source = (build_app.TOOLS_DIR / "check_app.py").read_text(encoding="utf-8")
    assert source.count('"Once this Mac is connected to Lucille"') == 2
    # The welcome note says what the app is for in the sentences the folder usage keys hold.
    assert f'static let appPurpose = "{_named_in_swift(check_app.PURPOSE)}"' in text
    assert "        \\(appPurpose)\n\n" in text
    # The note that refuses the whole disk.
    assert f'"{_named_in_swift(check_app.WHOLE_DISK_TITLE)}",' in text
    assert f'"{check_app.WHOLE_DISK_TEXT}\\(diskName), your startup disk: ' in text


_BROAD_FLAG = '"--allow-home-or-root"'


def test_the_app_never_passes_the_broad_flag_for_the_whole_disk() -> None:
    text = (build_app.SWIFT_DIR / "HostText.swift").read_text(encoding="utf-8")
    model = (build_app.SWIFT_DIR / "HostModel.swift").read_text(encoding="utf-8")
    # One place builds the argument, and it leaves the flag out for the whole disk. Elsewhere the
    # app only reads the flag's name in the host's refusals.
    for path in SWIFT_FILES:
        source = path.read_text(encoding="utf-8")
        if path.name != "HostText.swift":
            assert _BROAD_FLAG not in source, path.name
    assert text.count(f"append({_BROAD_FLAG})") == 1
    assert text.count(_BROAD_FLAG) == 1 + text.count(f".contains({_BROAD_FLAG})")
    adds = re.search(r"static func addFolderArguments\(.*?\n    \}\n", text, re.DOTALL)
    assert adds is not None
    assert (
        f"if confirmedHome && !isWholeDisk(path: path) {{\n            verb.append({_BROAD_FLAG})"
    ) in adds.group(0)
    assert model.count("HostText.addFolderArguments(") == 2
    # The whole disk the app refuses holds the two folders the design names, which the ui check
    # chooses, and the two folders between them.
    listed = re.search(r"static let wholeDisk = \[(.*?)\]", text)
    assert listed is not None
    disks = set(re.findall(r'"([^"]+)"', listed.group(1)))
    assert disks == {"/", "/System", "/System/Volumes", "/System/Volumes/Data"}
    assert {str(folder) for folder in check_app.WHOLE_DISK_FOLDERS} == {"/", "/System/Volumes/Data"}
    # Its note has exactly two buttons, a smaller folder, the default, and Cancel. The ui check
    # finds both on the screen, and no button that adds the folder; the "no other" is this pin.
    refusal = re.search(
        r"private func refuseWholeDisk\(\) -> Bool \{(.*?)\n    \}\n", model, re.DOTALL
    )
    assert refusal is not None
    buttons = re.findall(r'addButton\(withTitle: "([^"]+)"\)', refusal.group(1))
    assert buttons == list(check_app.WHOLE_DISK_BUTTONS)
    assert "return alert.runModal() == .alertFirstButtonReturn" in refusal.group(1)


def _swift_function(text: str, signature: str) -> str:
    found = re.search(re.escape(signature) + r".*?\n    \}\n", text, re.DOTALL)
    assert found is not None, signature
    return found.group(0)


def test_the_whole_disk_check_reads_the_disk_only_when_it_must() -> None:
    # The app runs these on its main thread, where a stalled network folder would freeze the
    # menu with no Cancel. A path that names the whole disk is never read; a folder that is not
    # the home folder or above it is read once, where the app chooses it, and not again to ask
    # about it or to build the command line.
    text = (build_app.SWIFT_DIR / "HostText.swift").read_text(encoding="utf-8")
    whole = _swift_function(text, "static func isWholeDisk(path: String) -> Bool {")
    assert whole.index("if namesWholeDisk(path) {") < whole.index("resolvingSymlinksInPath")
    assert whole.count("resolvingSymlinksInPath") == 1
    identity = _swift_function(text, "private static func fileIdentity(")
    assert "resolvingSymlinksInPath" not in identity
    broad = _swift_function(text, "static func broadFolder(path: String, home: String)")
    assert broad.count("isWholeDisk(") == 1
    assert broad.index("return nil") < broad.index("isWholeDisk(")
    adds = _swift_function(text, "static func addFolderArguments(")
    assert "confirmedHome && !isWholeDisk(path: path)" in adds


def test_the_app_and_its_checks_use_the_apps_name() -> None:
    name = build_app.load_pins().app_name
    text = (build_app.SWIFT_DIR / "HostText.swift").read_text(encoding="utf-8")
    model = (build_app.SWIFT_DIR / "HostModel.swift").read_text(encoding="utf-8")
    app = (build_app.SWIFT_DIR / "MenuBarApp.swift").read_text(encoding="utf-8")
    assert check_app.APP_NAME == name
    assert check_app.WELCOME_TITLE == f"{name} is in your menu bar"
    assert check_app.STATUS_ITEM_NAMES == (name, f"{name}, paused")
    assert check_app.OPEN_PROMPT == f"Allow {name} to Use This Folder"
    assert f"Quit {name}" in check_app.MENU_ITEMS
    assert 'static let quitApp = "Quit \\(appName)"' in text
    assert app.count("HostText.quitApp") == 2
    assert 'version.map { "\\(HostText.appName) \\($0)" } ?? HostText.appName' in model
    assert check_app.PURPOSE == build_app.folder_usage(name)
    assert check_app.PURPOSE.startswith(f"{name} lets Lucille ")
    # The Swift code reads the name from the bundle's Info.plist and never spells it. Code run
    # outside the app (the self-test) gets a fallback that is not the name, so the ui check, which
    # looks for the name, fails if the app stops reading it from Info.plist.
    fallback = "the app"
    assert fallback != name
    assert (
        'static let appName = Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName")'
        f' as? String ?? "{fallback}"'
    ) in text
    versions = build_app.BundleVersions("0.1.0", "1")
    assert build_app.info_plist(build_app.load_pins(), versions)["CFBundleDisplayName"] == name
    # The workflow spells the name only in its three artifacts' names, in lower case: the app's
    # paths come from check_app.py and build_app.py, which read the pins.
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert f"name: {name.lower()}-macos-app-logs" in workflow
    assert f"name: {name.lower()}-adhoc-macos-arm64-dmg-" in workflow
    assert f"name: {name.lower()}-macos-smoke-" in workflow
    assert len(re.findall(rf"\b{re.escape(name)}\b", workflow, re.IGNORECASE)) == 3
    assert name not in workflow
    for path in [*SWIFT_FILES, build_app.MACOS_DIR / "Tests" / "SelfTest.swift"]:
        source = path.read_text(encoding="utf-8")
        assert not re.search(rf"\b{re.escape(name)}\b", source), path
    # The app's earlier names are gone from what the app shows and what checks it. The host
    # program inside keeps its own name, lucille-host.
    for path in [
        *SWIFT_FILES,
        build_app.MACOS_DIR / "Tests" / "SelfTest.swift",
        build_app.TOOLS_DIR / "build_app.py",
        build_app.TOOLS_DIR / "check_app.py",
        build_app.MACOS_DIR / "README.md",
        build_app.PINS_FILE,
        WORKFLOW,
    ]:
        source = path.read_text(encoding="utf-8")
        assert "Lucille Host" not in source, path
        assert "loft" not in source.lower(), path


def _constraints_with(old: str, new: str) -> str:
    assert old in CONSTRAINTS_TEXT
    return CONSTRAINTS_TEXT.replace(old, new, 1)


def _requires() -> list[str]:
    import tomllib

    data = tomllib.loads(build_app.PYPROJECT.read_text(encoding="utf-8"))
    return data["build-system"]["requires"]


def test_the_build_backend_is_pinned_by_hash() -> None:
    constraints = build_app.load_build_constraints()
    assert set(constraints) == {
        "hatchling",
        "packaging",
        "pathspec",
        "pluggy",
        "tomlkit",
        "trove-classifiers",
    }
    for version, hashes in constraints.values():
        assert version and hashes
        assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in hashes)
    assert not build_app.build_constraint_problems(_requires(), constraints)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("hatchling==1.32.4 \\\n    --hash=sha256:08ec", "hatchling==1.32.4\n#"),
        ("--hash=sha256:08ec", "--hash=sha256:08EC"),
        ("--hash=sha256:08ec", "--hash=md5:08ec"),
        ("hatchling==1.32.4", "hatchling>=1.32.4"),
        ("tomlkit==0.15.2", "packaging==26.3"),
    ],
)
def test_a_malformed_constraint_is_refused(old: str, new: str) -> None:
    with pytest.raises(build_app.PinError):
        build_app.parse_build_constraints(_constraints_with(old, new))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("hatchling==1.32.4", "hatchling==1.32.3"),
        ("pluggy==1.7.0", "pluggy-extra==1.7.0"),
    ],
)
def test_constraints_that_differ_from_pyproject_are_refused(old: str, new: str) -> None:
    constraints = build_app.parse_build_constraints(_constraints_with(old, new))
    assert build_app.build_constraint_problems(_requires(), constraints)


def test_an_unpinned_build_requirement_is_refused() -> None:
    constraints = build_app.parse_build_constraints(CONSTRAINTS_TEXT)
    requires = ["hatchling>=1.32" if r.startswith("hatchling") else r for r in _requires()]
    problems = build_app.build_constraint_problems(requires, constraints)
    assert any("not pinned with ==" in problem for problem in problems)


def test_the_wheel_is_built_with_the_hashed_build_backend(tmp_path: Path) -> None:
    argv = build_app.build_wheel_argv(tmp_path)
    assert argv[:3] == ["uv", "build", "--wheel"]
    position = argv.index("--build-constraints")
    assert argv[position + 1] == str(build_app.BUILD_CONSTRAINTS)
    assert "--require-hashes" in argv
    assert "--no-build-isolation" not in argv
    # A uv.toml could turn build isolation off, or add an index: uv reads none.
    assert "--no-config" in argv
    assert "--no-config" in build_app.export_requirements_argv()


def test_uv_settings_from_the_environment_do_not_reach_the_wheel_or_the_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With build isolation off, uv build would use whatever hatchling is installed, unpinned and
    # never hashed, and still exit 0. So neither uv build nor uv export sees any UV_* variable.
    monkeypatch.setenv("UV_NO_BUILD_ISOLATION", "1")
    monkeypatch.setenv("UV_INDEX_URL", "https://example.invalid/simple")
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs.get("env")
        calls.append((list(argv), dict(env) if isinstance(env, dict) else None))
        if argv[:2] == ["uv", "build"]:
            out = Path(argv[argv.index("--out-dir") + 1])
            (out / payload.host_wheel_name("0.1.0")).write_bytes(b"")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", run)
    build_app.build_wheel(tmp_path, "0.1.0")
    build_app.export_requirements_text()
    assert [argv[:2] for argv, _ in calls] == [["uv", "build"], ["uv", "export"]]
    for argv, env in calls:
        assert "--no-config" in argv, argv
        assert env is not None, argv
        assert [key for key in env if key.startswith("UV_")] == [], argv
        assert env["PATH"] == os.environ["PATH"]


def test_the_disk_image_name_says_it_is_ad_hoc() -> None:
    name = build_app.dmg_name("Capsule", "0.1.0", "4466aba0c0de")
    assert name == "Capsule-0.1.0-4466aba0c0de-adhoc-macOS-arm64.dmg"
    assert name.endswith(build_app.DMG_SUFFIX)
    assert name != "Capsule-0.1.0-macOS-arm64.dmg"
    for commit in ("", "HEAD", "4466ABA", "4466ab", "../x"):
        with pytest.raises(build_app.BuildError):
            build_app.dmg_name("Capsule", "0.1.0", commit)


def test_only_this_scripts_disk_images_are_found(tmp_path: Path) -> None:
    ours = tmp_path / build_app.dmg_name("Capsule", "0.1.0", "abcdef1")
    ours.write_bytes(b"")
    (tmp_path / "Capsule-0.1.0-macOS-arm64.dmg").write_bytes(b"")
    (tmp_path / build_app.dmg_name("Other", "0.1.0", "abcdef1")).write_bytes(b"")
    (tmp_path / "other.dmg").write_bytes(b"")
    assert build_app.disk_images(tmp_path, "Capsule") == [ours]


def test_the_disk_image_glob_takes_the_name_literally(tmp_path: Path) -> None:
    # build() deletes every image disk_images() returns, so a name that is also a pattern must
    # match its own images only.
    other = tmp_path / build_app.dmg_name("Capsule", "0.1.0", "abcdef1")
    other.write_bytes(b"")
    assert build_app.disk_images(tmp_path, "C*") == []
    assert build_app.disk_images(tmp_path, "Cap?ule") == []
    ours = tmp_path / build_app.dmg_name("Cap[s]ule", "0.1.0", "abcdef1")
    ours.write_bytes(b"")
    assert build_app.disk_images(tmp_path, "Cap[s]ule") == [ours]


OTOOL_L = """\
Load command 9
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 1
    minos 11.0
      sdk 15.0
   ntools 1
Load command 10
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 6
    minos 17.0
      sdk 18.0
Load command 11
      cmd LC_VERSION_MIN_MACOSX
  cmdsize 16
  version 10.9
      sdk 10.9
"""


def test_the_minimum_macos_is_read_from_the_load_commands() -> None:
    lines = OTOOL_L.splitlines()
    assert check_app.minimum_macos_versions(lines) == ["11.0", "10.9"]
    assert check_app.minimum_macos_versions(lines[:4]) == []
    assert check_app.minimum_macos_versions(
        [line.replace("platform 1", "platform MACOS") for line in lines]
    ) == ["11.0", "10.9"]


@pytest.mark.parametrize(
    ("version", "later"),
    [
        ("11.0", False),
        ("15.0", False),
        ("15", False),
        ("15.0.1", True),
        ("15.1", True),
        ("26.0", True),
        ("x", True),
    ],
)
def test_newer_than(version: str, later: bool) -> None:
    assert check_app.newer_than(version, "15.0") is later


def test_forbidden_imports() -> None:
    allowed = [
        "_objc_msgSend",
        "_kill",
        "_OBJC_CLASS_$_NSTask",
        "_OBJC_CLASS_$_NSPipe",
        "_$sSS17UnicodeScalarViewV6append10contentsOfyqd___tSTRd__7ElementQyd__AERtsF",
    ]
    refused = [
        "_posix_spawn",
        "_posix_spawnp",
        "_socket",
        "_connect",
        "_nw_connection_create",
        "_OBJC_CLASS_$_NSURLSession",
        "_OBJC_CLASS_$_NSWorkspace",
        "_$s10Foundation10URLSessionC6sharedACvgZ",
        "_$s10Foundation4DataV10contentsOf7optionsAcA3URLV_So20NSDataReadingOptionsVtKcfC",
        "_OBJC_CLASS_$_NSPasteboard",
    ]
    assert check_app.forbidden_imports(allowed + refused) == refused


OTOOL_LOADS = """\
Load command 3
          cmd LC_ID_DYLIB
      cmdsize 72
         name @rpath/libself.dylib (offset 24)
Load command 4
          cmd LC_LOAD_DYLINKER
      cmdsize 32
         name /usr/lib/dyld (offset 12)
Load command 5
          cmd LC_LOAD_DYLIB
      cmdsize 56
         name @rpath/libtcl9.0.dylib (offset 24)
   time stamp 0 Thu Jan  1 00:00:00 1970
      current version 9.0.4
Load command 6
          cmd LC_LOAD_WEAK_DYLIB
      cmdsize 56
         name /usr/lib/libSystem.B.dylib (offset 24)
Load command 7
          cmd LC_RPATH
      cmdsize 32
         path @loader_path/../.. (offset 12)
Load command 8
          cmd LC_LOAD_DYLIB
      cmdsize 56
         name /usr/lib/libSystem.B.dylib (offset 24)
Load command 9
          cmd LC_RPATH
      cmdsize 48
         path /opt/My Libs/lib (offset 12)
"""


def test_the_libraries_and_run_paths_are_read_from_the_load_commands() -> None:
    assert check_app.load_commands(OTOOL_LOADS.splitlines()) == (
        ["@rpath/libtcl9.0.dylib", "/usr/lib/libSystem.B.dylib"],
        ["@loader_path/../..", "/opt/My Libs/lib"],
    )
    assert check_app.load_commands([]) == ([], [])


def _bundle(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A bundle laid out like the app's, with a space in its name: the interpreter, one extension
    module, and one library kept in ``lib/``."""
    app = tmp_path / "Example App.app"
    runtime = app / build_app.RUNTIME_DIR
    dynload = runtime / build_app.STDLIB / "lib-dynload"
    dynload.mkdir(parents=True)
    (runtime / "bin").mkdir()
    python = app / build_app.RUNTIME_IN_APP
    module = dynload / "_mod.cpython-313-darwin.so"
    for path in (python, module, runtime / "lib" / "libkept.dylib"):
        path.write_bytes(b"")
    return app, python, module


def test_libraries_that_resolve_inside_the_bundle_pass(tmp_path: Path) -> None:
    app, python, module = _bundle(tmp_path)
    libraries = [
        "/usr/lib/libSystem.B.dylib",
        "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
        "@rpath/libkept.dylib",
        "@loader_path/../../libkept.dylib",
        "@executable_path/../lib/libkept.dylib",
    ]
    shown, problems = check_app.resolve_links(
        app, module, python, (libraries, ["@loader_path/../.."])
    )
    assert problems == []
    kept = "Contents/Resources/runtime/lib/libkept.dylib"
    assert shown == [
        "run path @loader_path/../.. -> Contents/Resources/runtime/lib",
        "/usr/lib/libSystem.B.dylib (macOS)",
        "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation (macOS)",
        f"@rpath/libkept.dylib -> {kept}",
        f"@loader_path/../../libkept.dylib -> {kept}",
        f"@executable_path/../lib/libkept.dylib -> {kept}",
    ]
    # @rpath also tries the interpreter's own run paths, resolved against the interpreter.
    shown, problems = check_app.resolve_links(
        app, module, python, (["@rpath/libkept.dylib"], []), ["@executable_path/../lib"]
    )
    assert problems == [] and shown == [f"@rpath/libkept.dylib -> {kept}"]


@pytest.mark.parametrize(
    ("libraries", "rpaths", "problem"),
    [
        # A library the build removed, still loaded by a module it kept.
        (["@rpath/libtcl9.0.dylib"], ["@loader_path/../.."], "no such file in the app"),
        (["@loader_path/../../libgone.dylib"], [], "no such file in the app"),
        # A relative path that climbs out of the bundle.
        (
            ["@loader_path/../../../../../../../../opt/homebrew/lib/libssl.3.dylib"],
            [],
            "is outside the app",
        ),
        (["@executable_path/../../../../../../usr/local/lib/x.dylib"], [], "is outside the app"),
        (["@rpath/../../../../../../../../x.dylib"], ["@loader_path/../.."], "is outside the app"),
        # A run path outside the bundle, even one nothing uses.
        ([], ["@loader_path/../../../../../../../.."], "is outside the app"),
        ([], ["/usr/local/lib"], "not a path inside the app"),
        ([], ["@rpath/lib"], "not a path inside the app"),
        # A library from anywhere but macOS or the bundle.
        (["/opt/homebrew/lib/libssl.3.dylib"], [], "not from macOS"),
        (["/usr/local/lib/libffi.dylib"], [], "not from macOS"),
        (["/Library/Frameworks/Python.framework/Versions/3.13/Python"], [], "not from macOS"),
        (["libfoo.dylib"], [], "not from macOS"),
        (["@rpath/libkept.dylib"], [], "no run path inside the app"),
    ],
)
def test_libraries_that_do_not_resolve_inside_the_bundle_fail(
    tmp_path: Path, libraries: list[str], rpaths: list[str], problem: str
) -> None:
    app, python, module = _bundle(tmp_path)
    _, problems = check_app.resolve_links(app, module, python, (libraries, rpaths))
    assert len(problems) == 1 and problem in problems[0], problems


def test_only_the_title_or_description_names_the_menu_bar_item() -> None:
    # What System Events reported when only the tooltip carried the name.
    tooltip_only = {"title": "Computer", "description": "status menu", "help": "Capsule"}
    assert not check_app._named(tooltip_only, "Capsule")
    assert check_app._named({**tooltip_only, "title": "Capsule"}, "Capsule")
    assert check_app._named({"description": "Capsule, paused"}, "Capsule, paused")
    assert not check_app._named({}, "Capsule")


@pytest.mark.parametrize(
    ("said", "width"), [("30", 30.0), ("24.0", 24.0), ("24,5", 24.5), ("", 0.0), ("missing", 0.0)]
)
def test_the_menu_bar_items_width_is_read(said: str, width: float) -> None:
    assert check_app._width({"width": said}) == width
    assert check_app._width({}) == 0.0


class _Hdiutil:
    """A stand-in for subprocess.run that answers each hdiutil call from a script."""

    def __init__(self, answers: list[tuple[int, str]]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_: object) -> object:
        import subprocess

        self.calls.append(argv)
        code, stderr = self.answers[len(self.calls) - 1]
        return subprocess.CompletedProcess(argv, code, stdout="", stderr=stderr)


@pytest.mark.parametrize("busy", build_app.HDIUTIL_BUSY)
def test_hdiutil_tries_again_while_the_machine_is_busy(
    busy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []
    fake = _Hdiutil([(1, f"hdiutil: verify failed - {busy}\n"), (1, busy), (0, "")])
    monkeypatch.setattr("subprocess.run", fake)
    done = build_app.hdiutil(["verify", "x.dmg"], sleep=waits.append)
    assert done.returncode == 0
    assert fake.calls == [["hdiutil", "verify", "x.dmg"]] * 3
    assert waits == [5, 10]


def test_hdiutil_returns_any_other_failure_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    fake = _Hdiutil([(1, "hdiutil: attach failed - no mountable file systems\n")])
    monkeypatch.setattr("subprocess.run", fake)
    done = build_app.hdiutil(["attach", "x.dmg"], sleep=waits.append)
    assert done.returncode == 1 and len(fake.calls) == 1 and waits == []


def test_hdiutil_gives_up_after_its_last_try(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    busy = build_app.HDIUTIL_BUSY[1]
    fake = _Hdiutil([(1, busy)] * build_app.HDIUTIL_TRIES)
    monkeypatch.setattr("subprocess.run", fake)
    done = build_app.hdiutil(["create", "x.dmg"], sleep=waits.append)
    assert done.returncode == 1 and busy in done.stderr
    assert len(fake.calls) == build_app.HDIUTIL_TRIES
    assert waits == [5 * n for n in range(1, build_app.HDIUTIL_TRIES)]


def test_every_hdiutil_call_goes_through_the_retry() -> None:
    for script in ("build_app.py", "check_app.py"):
        source = (build_app.TOOLS_DIR / script).read_text(encoding="utf-8")
        assert '["hdiutil"' not in source.replace('argv = ["hdiutil", *args]', ""), script


# ---------------------------------------------------------------------------------------------
# The runtime: the prune list, pip, the interpreter's name, links


def _runtime_like_the_pinned_one(root: Path, prune: tuple[str, ...]) -> Path:
    """A runtime shaped like the pinned archive's ``python/``: the interpreter and the links to
    it, pip with a RECORD that names files the archive no longer holds and scripts in ``bin/``,
    each path of ``prune``, and a module, an extension and a link that stay."""
    runtime = root / "python"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin" / "python3.13").write_bytes(b"\xcf\xfa\xed\xfe interpreter")
    (runtime / "bin" / "python3.13").chmod(0o755)
    for entry in prune:
        path = runtime / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        if entry in ("bin/python", "bin/python3"):
            path.symlink_to("python3.13")
        elif entry.startswith("bin/") or Path(entry).suffix in (".py", ".so", ".dylib", ".txt"):
            path.write_bytes(b"pruned")
        else:
            path.mkdir()
            (path / "inside.pyc" if "__pycache__" in entry else path / "inside").write_bytes(b"")
    stdlib = runtime / build_app.STDLIB
    (stdlib / "lib-dynload").mkdir(parents=True, exist_ok=True)
    (stdlib / "lib-dynload" / "_dbm.cpython-313-darwin.so").write_bytes(b"\xcf\xfa\xed\xfe")
    (stdlib / "os.py").write_text("import sys\n", encoding="ascii")
    (stdlib / "osalias.py").symlink_to("os.py")
    site = runtime / build_app.SITE_PACKAGES
    (site / "pip").mkdir(parents=True, exist_ok=True)
    (site / "pip" / "__init__.py").write_bytes(b"")
    info = site / "pip-26.2.2.dist-info"
    info.mkdir()
    rows = ["pip/__init__.py,sha256=x,0", "pip/__pycache__/__init__.cpython-313.pyc,sha256=x,1"]
    for script in ("pip", "pip3", "pip3.13"):
        (runtime / "bin" / script).write_bytes(b"#!/the/build/machine/python3.13\n")
        rows.append(f"../../../bin/{script},sha256=x,208")
    rows.append("pip-26.2.2.dist-info/RECORD,,")
    (info / "RECORD").write_text("\n".join(rows) + "\n", encoding="ascii")
    return runtime


def test_prune_keeps_dist_info(tmp_path: Path) -> None:
    prune = build_app.load_prune_list()
    assert prune and len(prune) == len(set(prune))
    for entry in prune:
        assert not any(part.endswith(".dist-info") for part in entry.split("/")), entry
        if entry.startswith(build_app.SITE_PACKAGES):
            assert entry == f"{build_app.SITE_PACKAGES}/README.txt"
        assert not (build_app.UPSTREAM_INTERPRETER + "/").startswith(entry + "/"), entry
    assert "lib/python3.13/encodings/__pycache__" in prune
    # The runtime's pip goes whole, by its RECORD; any other *.dist-info is never deleted: the
    # build refuses the runtime instead.
    runtime = _runtime_like_the_pinned_one(tmp_path, prune)
    other = runtime / build_app.SITE_PACKAGES / "other-1.0.dist-info"
    other.mkdir()
    (other / "METADATA").write_text("Name: other\nVersion: 1.0\n", encoding="ascii")
    with pytest.raises(build_app.BuildError, match=r"site-packages is not empty: \['other-1.0"):
        build_app.prepare_runtime(runtime, prune)
    assert other.is_dir()
    site = runtime / build_app.SITE_PACKAGES
    assert sorted(path.name for path in site.iterdir()) == ["other-1.0.dist-info"]
    assert not list((runtime / "bin").glob("pip*"))


@pytest.mark.parametrize(
    "entry",
    [
        "lib/python3.13/site-packages/pip-26.2.2.dist-info",
        "lib/python3.13/site-packages/keyring-25.7.0.dist-info/entry_points.txt",
        # A *.dist-info folder anywhere, not only in site-packages.
        "lib/python3.13/vendored-1.0.dist-info",
        "lib/python3.13/vendor/x-1.0.dist-info/RECORD",
        "lib/python3.13/site-packages/*",
        "lib/python3.13/site-packages/keyring",
        "lib/python3.13/site-packages",
        "lib/python3.13",
        "lib",
        "bin",
        "bin/python3.13",
        "lib/python3.13/*.py",
        "lib/python3.13/turtle?.py",
        "lib/python3.13/[a]",
        "../x",
        "lib/../bin",
        "./include",
        "/include",
        "include/",
        "include # the headers",
        "lib\\pkgconfig",
    ],
)
def test_a_prune_entry_that_could_cut_too_much_is_refused(entry: str) -> None:
    with pytest.raises(build_app.PinError):
        build_app.parse_prune_list(f"include\n{entry}\n")


def test_a_prune_list_is_literal_paths_once_each() -> None:
    assert build_app.parse_prune_list("# headers\n\ninclude\n  share  \n") == ("include", "share")
    with pytest.raises(build_app.PinError, match="listed twice"):
        build_app.parse_prune_list("include\ninclude\n")


def test_prepare_runtime_on_a_runtime_like_the_pinned_one(tmp_path: Path) -> None:
    prune = build_app.load_prune_list()
    runtime = _runtime_like_the_pinned_one(tmp_path, prune)
    removed = build_app.prepare_runtime(runtime, prune)
    assert removed == sorted([*prune, "pip==26.2.2"])
    assert build_app.runtime_shape_problems(runtime) == []
    assert sorted(path.name for path in (runtime / "bin").iterdir()) == ["lucille-host-runtime"]
    interpreter = runtime / build_app.INTERPRETER
    assert interpreter.read_bytes() == b"\xcf\xfa\xed\xfe interpreter"
    assert interpreter.stat().st_mode & 0o111
    # The link that stayed is now a copy of its target.
    alias = runtime / build_app.STDLIB / "osalias.py"
    assert not alias.is_symlink() and alias.read_text(encoding="ascii") == "import sys\n"
    assert build_app.symlinks_in(runtime) == [] and build_app.bytecode_in(runtime) == []
    assert not (runtime / build_app.SITE_PACKAGES / "pip").exists()


def _plant_bytecode(runtime: Path) -> list[Path]:
    """A __pycache__ folder with a .pyc in it, a stray .pyc and a stray .pyo, outside every
    prune entry; returns the four paths, sorted."""
    stdlib = runtime / build_app.STDLIB
    cache = stdlib / "json" / "__pycache__"
    cache.mkdir(parents=True)
    compiled = cache / "decoder.cpython-313.pyc"
    compiled.write_bytes(b"\xf3\r\r\n" + bytes(12))
    stray = stdlib / "legacy.pyc"
    stray.write_bytes(b"\xf3\r\r\n" + bytes(12))
    optimized = stdlib / "old.pyo"
    optimized.write_bytes(b"")
    return sorted([cache, compiled, stray, optimized])


def test_bytecode_is_found_and_refused(tmp_path: Path) -> None:
    prune = build_app.load_prune_list()
    # In a prepared runtime: each one found, and each one a shape problem.
    runtime = _runtime_like_the_pinned_one(tmp_path / "prepared", prune)
    build_app.prepare_runtime(runtime, prune)
    planted = _plant_bytecode(runtime)
    assert build_app.bytecode_in(runtime) == planted
    problems = build_app.runtime_shape_problems(runtime)
    assert [problem for problem in problems if problem.startswith("bytecode: ")] == [
        f"bytecode: {path.relative_to(runtime)}" for path in planted
    ]
    # Before preparing: the build refuses the runtime, and deletes none of them.
    runtime = _runtime_like_the_pinned_one(tmp_path / "unprepared", prune)
    planted = _plant_bytecode(runtime)
    with pytest.raises(build_app.BuildError, match=r"bytecode: lib/python3\.13/json/__pycache__"):
        build_app.prepare_runtime(runtime, prune)
    assert all(path.exists() for path in planted)


def test_a_prune_entry_the_runtime_lacks_is_refused(tmp_path: Path) -> None:
    prune = build_app.load_prune_list()
    runtime = _runtime_like_the_pinned_one(tmp_path, prune)
    with pytest.raises(build_app.BuildError, match="prune.txt names lib/tk8.6, which the pinned"):
        build_app.prepare_runtime(runtime, (*prune, "lib/tk8.6"))
    # Refused before anything was pruned.
    assert all((runtime / entry).exists() or (runtime / entry).is_symlink() for entry in prune)


def test_what_the_prune_list_misses_is_refused_not_deleted(tmp_path: Path) -> None:
    prune = build_app.load_prune_list()
    runtime = _runtime_like_the_pinned_one(tmp_path, prune)
    (runtime / "bin" / "2to3").write_bytes(b"new in this runtime")
    (runtime / "etc").mkdir()
    with pytest.raises(build_app.BuildError) as raised:
        build_app.prepare_runtime(runtime, prune)
    assert "bin/ holds ['2to3', 'lucille-host-runtime']" in str(raised.value)
    assert "['bin', 'etc', 'lib'], not bin and lib" in str(raised.value)
    assert (runtime / "bin" / "2to3").is_file() and (runtime / "etc").is_dir()


@pytest.mark.parametrize(
    ("make", "words"),
    [
        (lambda root: (root / "dangling").symlink_to("nowhere"), "not a file inside"),
        (lambda root: (root / "outside").symlink_to("/etc/hostname"), "not a file inside"),
        (lambda root: (root / "folder").symlink_to("lib"), "not a file inside"),
    ],
)
def test_symlinks_replaced_or_refused(
    tmp_path: Path, make: Callable[[Path], object], words: str
) -> None:
    root = tmp_path / "runtime"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "file.txt").write_text("text\n", encoding="ascii")
    (root / "lib" / "file.txt").chmod(0o750)
    (root / "copy.txt").symlink_to("lib/file.txt")
    assert build_app.replace_symlinks(root) == ["copy.txt"]
    copy = root / "copy.txt"
    assert not copy.is_symlink() and copy.read_text(encoding="ascii") == "text\n"
    assert copy.stat().st_mode & 0o777 == 0o750
    make(root)
    with pytest.raises(build_app.BuildError, match=words):
        build_app.replace_symlinks(root)


# ---------------------------------------------------------------------------------------------
# The bytecode: one checked-hash .pyc beside each .py of the runtime's library (0002 §6)

# The bundled interpreter runs only on a Mac, so here a stand-in runs the interpreter that runs
# these tests, and the .pyc files carry its tag. On a Mac, check_app.py suite runs these tests
# with the bundled interpreter itself.
TAG = sys.implementation.cache_tag
ANOTHER_TAG = "cpython-312" if TAG != "cpython-312" else "cpython-313"
# Plain sources that compile on every Python the tests run on: a module, a package, and a
# package in site-packages. pkg/checked.py holds what -O or -OO would compile away: a docstring, an
# assert and an `if __debug__:` block.
_LIBRARY: Mapping[str, bytes] = {
    "mod.py": b"VALUE = 1\n\n\ndef double(x):\n    return 2 * x\n",
    "pkg/__init__.py": b"",
    "pkg/sub.py": b"WORDS = ('one', 'two')\n",
    "pkg/checked.py": (
        b'"""Kept at -O0."""\n\n\n'
        b"def half(x):\n    assert x % 2 == 0, x\n    return x // 2\n\n\n"
        b"if __debug__:\n    DEBUG = True\n"
    ),
    "site-packages/demo/__init__.py": b"",
    "site-packages/demo/m.py": b"class Demo:\n    name = 'demo'\n",
}
_MOD_PYC = f"lib/python3.13/__pycache__/mod.{TAG}.pyc"
# Run as the app runs the interpreter, in a process of its own, with pairs of paths: a .py, then
# its .pyc. It prints each .pyc that is not what the importer would make of its .py: the header
# the importer checks (this interpreter's magic number, checked-hash, the source's hash), and the
# code it would compile from that .py (`==` ignores co_filename, which names the source by its
# path from the runtime).
_IMPORTER_VIEW = """\
import importlib.machinery
import importlib.util
import marshal
import sys

for source, cached in zip(sys.argv[1::2], sys.argv[2::2]):
    with open(source, "rb") as handle:
        data = handle.read()
    with open(cached, "rb") as handle:
        pyc = handle.read()
    header = importlib.util.MAGIC_NUMBER + (3).to_bytes(4, "little")
    if pyc[:16] != header + importlib.util.source_hash(data):
        print("another header:", cached)
    loader = importlib.machinery.SourceFileLoader("module", source)
    if marshal.loads(pyc[16:]) != loader.source_to_code(data, source):
        print("other code:", cached)
"""


def _bytecode_app(root: Path, library: Mapping[str, bytes] = _LIBRARY) -> Path:
    """An app that holds only a runtime: ``bin/lucille-host-runtime``, a stand-in that runs the
    interpreter running these tests, and ``library`` under ``lib/python3.13``. No .pyc."""
    app = root / "Capsule.app"
    runtime = app / build_app.RUNTIME_DIR
    interpreter = runtime / build_app.INTERPRETER
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n', "utf-8")
    interpreter.chmod(0o755)
    for rel, data in library.items():
        path = runtime / build_app.STDLIB / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return app


def _compiled_app(root: Path) -> Path:
    app = _bytecode_app(root)
    build_app.compile_bytecode(app, tag=TAG)
    return app


def _lib(app: Path) -> Path:
    return app / build_app.RUNTIME_DIR / build_app.STDLIB


def _pyc_of(source: Path) -> Path:
    return source.parent / "__pycache__" / f"{source.stem}.{TAG}.pyc"


def _mod_pyc(app: Path) -> Path:
    return _pyc_of(_lib(app) / "mod.py")


def _set_flags(flags: int) -> Callable[[Path], None]:
    def damage(app: Path) -> None:
        data = _mod_pyc(app).read_bytes()
        _mod_pyc(app).write_bytes(data[:4] + flags.to_bytes(4, "little") + data[8:])

    return damage


def _edit_source(app: Path) -> None:
    (_lib(app) / "mod.py").write_bytes(_LIBRARY["mod.py"] + b"VALUE = 2\n")


def _other_magic(app: Path) -> None:
    data = _mod_pyc(app).read_bytes()
    _mod_pyc(app).write_bytes(bytes([data[0] ^ 1]) + data[1:])


def _another_body(app: Path) -> None:
    # The right 16-byte header, and the code of another source, as the program wrote it.
    body = _pyc_of(_lib(app) / "pkg" / "sub.py").read_bytes()[16:]
    _mod_pyc(app).write_bytes(_mod_pyc(app).read_bytes()[:16] + body)


def _too_short(app: Path) -> None:
    _mod_pyc(app).write_bytes(_mod_pyc(app).read_bytes()[:10])


def _no_source(app: Path) -> None:
    (_lib(app) / "mod.py").unlink()


def _no_pyc(app: Path) -> None:
    _pyc_of(_lib(app) / "pkg" / "sub.py").unlink()


def _plant(rel: str) -> Callable[[Path], None]:
    """Plant a copy of mod's .pyc at ``rel``, a path from the app."""

    def damage(app: Path) -> None:
        path = app / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_mod_pyc(app).read_bytes())

    return damage


def _folder_in_pycache(app: Path) -> None:
    (_mod_pyc(app).parent / "inner").mkdir()


def _link_to_the_same_bytes(app: Path) -> None:
    elsewhere = app.parent / "elsewhere.pyc"
    elsewhere.write_bytes(_mod_pyc(app).read_bytes())
    _mod_pyc(app).unlink()
    _mod_pyc(app).symlink_to(elsewhere)


def _empty_pycache(app: Path) -> None:
    (_lib(app) / "empty" / "__pycache__").mkdir(parents=True)


def _folder_named_like_the_pyc(app: Path) -> None:
    _mod_pyc(app).unlink()
    _mod_pyc(app).mkdir()


def _pycache_is_a_link(app: Path) -> None:
    # The same files, in a folder outside the library, linked to from where the build writes them.
    cache = _lib(app) / "__pycache__"
    elsewhere = app.parent / "elsewhere"
    shutil.move(cache, elsewhere)
    cache.symlink_to(elsewhere, target_is_directory=True)


_IN_LIB = f"{build_app.RUNTIME_DIR}/{build_app.STDLIB}"


@pytest.mark.parametrize(
    ("damage", "said"),
    [
        (_set_flags(1), f"{_MOD_PYC} is not checked-hash: its flags are 1"),
        (_set_flags(0), f"{_MOD_PYC} is not checked-hash: its flags are 0"),
        (
            _edit_source,
            f"{_MOD_PYC} has the source hash of another file, not lib/python3.13/mod.py",
        ),
        (_other_magic, f"{_MOD_PYC} has another Python's magic number"),
        (
            _another_body,
            f"{_MOD_PYC} is not what this interpreter compiles from lib/python3.13/mod.py",
        ),
        (_too_short, f"{_MOD_PYC} is too short to be a .pyc"),
        (_no_source, f"{_MOD_PYC} has no source: there is no lib/python3.13/mod.py"),
        (_no_pyc, "lib/python3.13/pkg/sub.py has no .pyc"),
        (
            _plant(f"{_IN_LIB}/__pycache__/notes.txt"),
            "lib/python3.13/__pycache__/notes.txt is not a file the build writes",
        ),
        (
            _plant(f"{_IN_LIB}/__pycache__/mod.{TAG}.opt-1.pyc"),
            f"lib/python3.13/__pycache__/mod.{TAG}.opt-1.pyc is not a file the build writes",
        ),
        (
            _plant(f"{_IN_LIB}/__pycache__/mod.{ANOTHER_TAG}.pyc"),
            f"lib/python3.13/__pycache__/mod.{ANOTHER_TAG}.pyc is not a file the build writes",
        ),
        (_folder_in_pycache, "lib/python3.13/__pycache__/inner is not a file the build writes"),
        (_folder_named_like_the_pyc, f"{_MOD_PYC} is not a file the build writes"),
        (
            _plant(f"{_IN_LIB}/__pycache__/.{TAG}.pyc"),
            f"lib/python3.13/__pycache__/.{TAG}.pyc is not a file the build writes",
        ),
        (_link_to_the_same_bytes, f"{_MOD_PYC} is not a file the build writes"),
        (
            _pycache_is_a_link,
            "lib/python3.13/__pycache__ is a symbolic link, not the folder the build writes",
        ),
        (
            _plant(f"{_IN_LIB}/mod.pyc"),
            "bytecode outside a __pycache__ folder: lib/python3.13/mod.pyc",
        ),
        (_empty_pycache, "an empty __pycache__ folder: lib/python3.13/empty/__pycache__"),
        (
            _plant(f"{build_app.RUNTIME_DIR}/bin/__pycache__/x.{TAG}.pyc"),
            f"bytecode outside the runtime's library: bin/__pycache__/x.{TAG}.pyc",
        ),
        (
            _plant(f"Contents/Resources/bin/__pycache__/x.{TAG}.pyc"),
            "bytecode outside the runtime's library: "
            f"Contents/Resources/bin/__pycache__/x.{TAG}.pyc",
        ),
    ],
    ids=[
        "unchecked-hash",
        "timestamp",
        "source-edited",
        "another-magic",
        "another-body",
        "too-short",
        "no-source",
        "no-pyc",
        "text-file",
        "optimized",
        "another-tag",
        "folder",
        "folder-named-like-the-pyc",
        "no-module-name",
        "link",
        "pycache-link",
        "beside-its-source",
        "empty-pycache",
        "in-the-runtime-outside-the-library",
        "outside-the-runtime",
    ],
)
def test_every_pyc_is_checked_hash_and_matches_its_source(
    tmp_path: Path, damage: Callable[[Path], None], said: str
) -> None:
    # Acceptance 1: the build writes, with the interpreter it ships, one checked-hash .pyc beside
    # each .py of the runtime's library, and nothing else; a second run of the same program finds
    # each one byte for byte what it compiles from its source.
    app = _compiled_app(tmp_path)
    sources = sorted(_lib(app).rglob("*.py"))
    assert len(sources) == len(_LIBRARY)
    written = sorted(_pyc_of(source) for source in sources)
    assert [path for path in build_app.bytecode_in(app) if path.is_file()] == written
    for pyc in written:
        assert int.from_bytes(pyc.read_bytes()[4:8], "little") == build_app.CHECKED_HASH == 0b11
    # Each is what the importer, run as the app runs it, would make of its .py.
    pairs = [str(path) for source in sources for path in (source, _pyc_of(source))]
    done = subprocess.run(
        [sys.executable, *build_app.RUNTIME_FLAGS, "-c", _IMPORTER_VIEW, *pairs],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert (done.returncode, done.stdout) == (0, ""), done
    assert build_app.bytecode_problems(app, tag=TAG) == []
    build_app.require_bytecode(app, tag=TAG)
    # Each .pyc the build would not write fails the check and the build's own gate.
    damage(app)
    problems = build_app.bytecode_problems(app, tag=TAG)
    assert any(said in problem for problem in problems), problems
    with pytest.raises(build_app.BuildError, match=re.escape(said)):
        build_app.require_bytecode(app, tag=TAG)


def test_the_build_stops_if_a_second_run_finds_other_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An interpreter whose marshal output is not stable writes one thing and, in the build's
    # second, fresh run, compiles another. Here one byte of one body changes after the first run.
    real = build_app.run_bytecode_program

    def unstable(runtime: Path, mode: str) -> subprocess.CompletedProcess[str]:
        done = real(runtime, mode)
        if mode == "write":
            pyc = runtime / build_app.STDLIB / "__pycache__" / f"mod.{TAG}.pyc"
            data = bytearray(pyc.read_bytes())
            data[-1] ^= 1
            pyc.write_bytes(bytes(data))
        return done

    monkeypatch.setattr(build_app, "run_bytecode_program", unstable)
    said = f"{_MOD_PYC} is not what this interpreter compiles from lib/python3.13/mod.py"
    with pytest.raises(build_app.BuildError, match=re.escape(said)):
        build_app.compile_bytecode(_bytecode_app(tmp_path), tag=TAG)


_ALL = len(_LIBRARY)
_CRASH = "Traceback (most recent call last):\nPermissionError: [Errno 13] Permission denied\n"


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        # Python exits 1 on an exception it does not catch, and the program prints only at the end.
        (1, ""),
        (1, f"found the same bytes in {_ALL} of {_ALL} .pyc files, {TAG}\n"),
        (1, f"one\ntwo\nfound the same bytes in 1 of {_ALL} .pyc files, {TAG}\n"),
        (0, ""),
        (0, f"found the same bytes in 1 of {_ALL} .pyc files, {TAG}\n"),
        (0, f"found the same bytes in {_ALL} of {_ALL} .pyc files, cpython-0\n"),
    ],
    ids=["crash", "no-problem-named", "lines-do-not-add-up", "silent", "not-all", "another-tag"],
)
def test_a_check_run_that_does_not_end_as_the_program_ends_is_a_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
) -> None:
    # The build's gate and check_app.py bytecode read what a check run found from its exit code
    # and its lines. An ending the program never gives, an exception among them, is a problem.
    app = _compiled_app(tmp_path)
    ended = subprocess.CompletedProcess(["program"], returncode, stdout, _CRASH)
    monkeypatch.setattr(build_app, "run_bytecode_program", lambda runtime, mode: ended)
    problems = build_app.bytecode_problems(app, tag=TAG)
    assert len(problems) == 1, problems
    assert problems[0].startswith(f"the bytecode check did not run (exit {returncode}): ")
    assert "PermissionError" in problems[0]
    with pytest.raises(build_app.BuildError, match="the bytecode check did not run"):
        build_app.require_bytecode(app, tag=TAG)


def test_a_check_run_that_crashes_is_a_problem(tmp_path: Path) -> None:
    # A folder where a .pyc should be makes the program's open() raise: it exits 1 and prints
    # nothing. The layout names the folder, and the check run is a problem of its own.
    app = _compiled_app(tmp_path)
    _folder_named_like_the_pyc(app)
    done = build_app.run_bytecode_program(app / build_app.RUNTIME_DIR, "check")
    assert (done.returncode, done.stdout) == (1, ""), done
    assert "IsADirectoryError" in done.stderr
    problems = build_app.bytecode_problems(app, tag=TAG)
    assert any(line.startswith("the bytecode check did not run (exit 1): ") for line in problems)


def test_the_build_prints_what_both_runs_found(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _compiled_app(tmp_path)
    printed = capsys.readouterr().out.splitlines()
    assert f"wrote {_ALL} of {_ALL} .pyc files, {TAG}" in printed
    assert printed[-1] == f"found the same bytes in {_ALL} of {_ALL} .pyc files, {TAG}"


def test_two_builds_write_the_same_bytes_wherever_the_runtime_is(tmp_path: Path) -> None:
    first = _compiled_app(tmp_path / "a")
    second = tmp_path / "b c" / first.name
    shutil.copytree(first, second, ignore=shutil.ignore_patterns("__pycache__"))
    assert build_app.bytecode_in(second) == []
    build_app.compile_bytecode(second, tag=TAG)
    built = [
        {
            path.relative_to(_lib(app)).as_posix(): path.read_bytes()
            for path in _lib(app).rglob("*.pyc")
        }
        for app in (first, second)
    ]
    assert len(built[0]) == len(_LIBRARY) and built[0] == built[1]
    # No .pyc holds the build folder: each names its source by its path from the runtime, which
    # the importer replaces with the real path when it loads the file.
    for rel, data in built[0].items():
        assert os.fsencode(tmp_path) not in data, rel
        folder, _, name = rel.rpartition("__pycache__/")
        source = f"{build_app.STDLIB}/{folder}{name.removesuffix(f'.{TAG}.pyc')}.py"
        assert source.encode() in data, (rel, source)


def _bytecode_run(flags: Sequence[str], args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """The bytecode program, run by the interpreter running these tests with ``flags``."""
    argv = [sys.executable, *flags, "-c", build_app.BYTECODE_PROGRAM, *args]
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=dict(build_app.BYTECODE_ENV),
        cwd="/",
        timeout=120,
        check=False,
    )


_Flags = Callable[[tuple[str, ...]], tuple[str, ...]]


def _drop(*dropped: str) -> _Flags:
    return lambda flags: tuple(flag for flag in flags if flag not in dropped)


def _swap(old: str, new: str) -> _Flags:
    return lambda flags: tuple(new if flag == old else flag for flag in flags)


def _add(*added: str) -> _Flags:
    return lambda flags: (*flags, *added)


@pytest.mark.parametrize(
    ("change", "args"),
    [
        (_drop("-I"), ("check", "RUNTIME", "LIB")),
        (_drop("-B"), ("check", "RUNTIME", "LIB")),
        (_drop("-S"), ("check", "RUNTIME", "LIB")),
        (_drop("-W", "ignore"), ("check", "RUNTIME", "LIB")),
        (_swap("ignore", "default"), ("check", "RUNTIME", "LIB")),
        (_swap("utf8", "utf8=0"), ("check", "RUNTIME", "LIB")),
        (_add("-X", "pycache_prefix=/nowhere"), ("check", "RUNTIME", "LIB")),
        (_add("-O"), ("check", "RUNTIME", "LIB")),
        (_add(), ("other", "RUNTIME", "LIB")),
        (_add(), ("check", "RUNTIME")),
        (_add(), ("check", "RUNTIME", "LIB", "more")),
    ],
    ids=[
        "no-I",
        "no-B",
        "no-S",
        "no-W-ignore",
        "W-default",
        "utf8-off",
        "pycache-prefix",
        "optimized",
        "another-mode",
        "missing-argument",
        "extra-argument",
    ],
)
def test_the_bytecode_program_runs_only_with_its_flags(
    tmp_path: Path, change: _Flags, args: tuple[str, ...]
) -> None:
    runtime = _bytecode_app(tmp_path) / build_app.RUNTIME_DIR
    assert build_app.bytecode_argv(runtime, "check") == [
        str(runtime / build_app.INTERPRETER),
        "-I",
        "-B",
        "-S",
        "-W",
        "ignore",
        "-X",
        "utf8",
        "-c",
        build_app.BYTECODE_PROGRAM,
        "check",
        str(runtime),
        "lib/python3.13",
    ]
    # The app's flags, with -S, so that no .pth file or site step loads a module first, and
    # -W ignore, so that no warning does while it compiles; and a fixed environment.
    assert set(build_app.RUNTIME_FLAGS) <= set(build_app.BYTECODE_FLAGS)
    assert _drop("-S", "-W", "ignore")(build_app.BYTECODE_FLAGS) == build_app.RUNTIME_FLAGS
    assert build_app.BYTECODE_ENV == {"PATH": "/usr/bin:/bin"}
    usage = "usage: lucille-host-runtime " + " ".join(build_app.BYTECODE_FLAGS)
    usage += " -c PROGRAM write|check RUNTIME LIB\n"
    # The control: with its own flags it gets past the usage check, and finds no .pyc.
    done = _bytecode_run(build_app.BYTECODE_FLAGS, ("check", str(runtime), build_app.STDLIB))
    assert done.returncode == 1, done
    assert (
        done.stdout.splitlines()[-1]
        == f"found the same bytes in 0 of {len(_LIBRARY)} .pyc files, {TAG}"
    )
    places = {"RUNTIME": str(runtime), "LIB": build_app.STDLIB}
    done = _bytecode_run(change(build_app.BYTECODE_FLAGS), [places.get(a, a) for a in args])
    assert (done.returncode, done.stdout) == (2, usage), done
    assert build_app.bytecode_in(runtime) == []


def test_the_bytecode_program_refuses_to_write_twice_and_stops_on_a_loaded_module(
    tmp_path: Path,
) -> None:
    app = _compiled_app(tmp_path / "twice")
    runtime = app / build_app.RUNTIME_DIR
    held = {path: path.read_bytes() for path in build_app.bytecode_in(app) if path.is_file()}
    done = build_app.run_bytecode_program(runtime, "write")
    assert done.returncode == 1, done
    assert done.stdout.startswith("the runtime already holds bytecode: "), done.stdout
    with pytest.raises(build_app.BuildError) as raised:
        build_app.compile_bytecode(app, tag=TAG)
    assert "already holds bytecode" in str(raised.value) and "__pycache__" in str(raised.value)
    assert {
        path: path.read_bytes() for path in build_app.bytecode_in(app) if path.is_file()
    } == held
    # A source in another encoding makes the compiler load that codec mid-run, which could change
    # the bytes marshal writes for every file after it: the program stops and names the module.
    cookie = b"# -*- coding: cp1252 -*-\nWORD = 'caf\xe9'\n"
    app = _bytecode_app(tmp_path / "cookie", {**_LIBRARY, "latin.py": cookie})
    with pytest.raises(build_app.BuildError, match=r"encodings\.cp1252"):
        build_app.compile_bytecode(app, tag=TAG)
    done = build_app.run_bytecode_program(app / build_app.RUNTIME_DIR, "check")
    assert done.returncode == 2, done
    assert done.stdout.startswith("compiling loaded modules, so the bytes would depend on them: ")
    assert "encodings.cp1252" in done.stdout
    problems = build_app.bytecode_problems(app, tag=TAG)
    assert any(line.startswith("the bytecode check did not run (exit 2): ") for line in problems)


def test_the_bytecode_program_imports_only_what_a_fresh_interpreter_has() -> None:
    import ast

    program = build_app.BYTECODE_PROGRAM
    tree = ast.parse(program)
    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert sorted(imported) == ["_frozen_importlib_external", "_imp", "marshal", "os", "sys"]
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert "\\" not in program and max(map(len, program.splitlines())) < 100
    assert program.endswith("raise SystemExit(main())\n")
    # It compiles in sorted order of each path from the runtime, not in the walk's own order,
    # which is the file system's and need not be the same in two builds' folders (0002 §6).
    (main,) = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    steps = [ast.unparse(node).splitlines()[0] for node in main.body]
    walk = steps.index("for folder, dirs, files in os.walk(os.path.join(runtime, lib)):")
    assert steps.index("sources.sort()") == walk + 1
    assert steps.index("for source in sources:") > walk + 1
    # Each is built in or frozen: importing it reads no file, from the tree or anywhere else.
    probe = (
        "import sys, _imp\n"
        "builtin = sys.builtin_module_names\n"
        "print([n for n in sys.argv[1:] if not (n in builtin or _imp.is_frozen(n))])\n"
    )
    done = subprocess.run(
        [sys.executable, *build_app.BYTECODE_FLAGS, "-c", probe, *imported],
        capture_output=True,
        text=True,
        env=dict(build_app.BYTECODE_ENV),
        cwd="/",
        timeout=60,
        check=False,
    )
    assert (done.returncode, done.stdout) == (0, "[]\n"), done
    # The build and the checks run it only through bytecode_argv: one program text, for both modes.
    source = (build_app.TOOLS_DIR / "build_app.py").read_text(encoding="utf-8")
    (argv_function,) = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "bytecode_argv"
    ]
    loads = [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Name)
        and node.id == "BYTECODE_PROGRAM"
        and isinstance(node.ctx, ast.Load)
    ]
    assert loads and all(
        argv_function.lineno <= line <= (argv_function.end_lineno or 0) for line in loads
    ), loads
    checks = _CHECK_APP.read_text(encoding="utf-8")
    assert "BYTECODE_PROGRAM" not in checks
    assert "marshal" not in checks and "_frozen_importlib_external" not in checks
    assert "build_app.run_bytecode_program(" in checks


def test_the_verbose_import_lines_are_read(tmp_path: Path) -> None:
    # check_app.py bytecode reads `python -v` to see each module of the bundle loaded from its
    # .pyc: -B stops writes, not reads. Here mod has the .pyc the program wrote, other has none.
    app = _bytecode_app(tmp_path / "with space", {"mod.py": b"VALUE = 1\n"})
    runtime = app / build_app.RUNTIME_DIR
    done = build_app.run_bytecode_program(runtime, "write")
    assert done.returncode == 0, done
    lib = _lib(app)
    (lib / "other.py").write_bytes(b"VALUE = 2\n")
    code = f"import sys; sys.path.insert(0, {str(lib)!r}); import mod, other"
    done = subprocess.run(
        [sys.executable, "-v", *build_app.RUNTIME_FLAGS, "-S", "-c", code],
        capture_output=True,
        text=True,
        env=dict(build_app.BYTECODE_ENV),
        cwd="/",
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    pyc = _pyc_of(lib / "mod.py")
    assert f"# {pyc} matches {lib / 'mod.py'}\n" in done.stderr
    assert check_app.loaded_from(done.stderr, runtime) == ([str(pyc)], [str(lib / "other.py")])
    assert not _pyc_of(lib / "other.py").exists()
    # Nothing outside the root is counted: the interpreter's own modules load from elsewhere.
    assert check_app.loaded_from(done.stderr, tmp_path / "elsewhere") == ([], [])


# A stand-in for the bundled interpreter, for check_app.check_bytecode: the interpreter running
# these tests, which runs a module (-m) with @SITE@, the stand-in's own site-packages, first on
# sys.path, as the bundled interpreter has its own. -I ignores PYTHONPATH, so the stand-in puts it
# there. @PREFIX@ goes before the module's code: nothing, or an option for that run alone.
_BYTECODE_CHECK_RUNTIME = """\
#!/bin/sh
site=@SITE@
code='import runpy, sys; sys.path.insert(0, sys.argv.pop(1))'
code="$code; runpy.run_module(sys.argv.pop(1), run_name='__main__', alter_sys=True)"
count=$#
while [ "$count" -gt 0 ]; do
    arg=$1
    shift
    count=$((count - 1))
    if [ "$arg" = -m ]; then
        set -- "$@" @PREFIX@ -c "$code" "$site" "$1"
        shift
        count=$((count - 1))
    else
        set -- "$@" "$arg"
    fi
done
exec @PYTHON@ "$@"
"""
# The host, as far as `--version` goes: a package and its __main__, in site-packages.
_STUB_HOST: Mapping[str, bytes] = {
    "site-packages/lucille_host/__init__.py": b"__version__ = '0.0.0'\n",
    "site-packages/lucille_host/__main__.py": (
        b"from lucille_host import __version__\n\nprint('lucille-host', __version__)\n"
    ),
}
# What check_bytecode says when each of its three parts fails.
_LAYOUT_FAILS = "one checked-hash "
_PROGRAM_FAILS = "each .pyc has the bundled interpreter's magic number"
_LOADS_FAIL = "--version, run as the app runs it, loads "


def _edit_the_hosts_source(app: Path) -> None:
    init = _lib(app) / "site-packages" / "lucille_host" / "__init__.py"
    init.write_bytes(init.read_bytes() + b"EDITED = True\n")


def _another_host(app: Path) -> None:
    # A host outside the bundle, beside the app, where the "another-host" stand-in finds it.
    for rel, data in _STUB_HOST.items():
        path = app.parent / "another" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


# Where the stand-in's site-packages is: the bundle's own, or the other host's beside the app.
_OWN_SITE = '$(cd "${0%/*}/.." && pwd)/lib/python3.13/site-packages'
_OTHER_SITE = '$(cd "${0%/*}/../../../../.." && pwd)/another/site-packages'


@pytest.mark.parametrize(
    ("damage", "site", "prefix", "said"),
    [
        (None, _OWN_SITE, "", []),
        # The program finds a body that is not what it compiles; layout and the run see nothing.
        (_another_body, _OWN_SITE, "", [_PROGRAM_FAILS]),
        # A file the build does not write, in __pycache__: only the layout sees it.
        (_plant(f"{_IN_LIB}/__pycache__/notes.txt"), _OWN_SITE, "", [_LAYOUT_FAILS]),
        # The host's source edited after the build: the program sees its .pyc is stale, and the
        # run compiles that module from source, though the other comes from its .pyc.
        (_edit_the_hosts_source, _OWN_SITE, "", [_PROGRAM_FAILS, _LOADS_FAIL]),
        # An interpreter that reads .pyc files from another tree compiles every module of the
        # host from source, though each .pyc is right.
        (None, _OWN_SITE, "-X pycache_prefix=/nowhere", [_LOADS_FAIL]),
        # An interpreter that runs a host from outside the bundle loads none of the bundle's
        # modules, from their .pyc or from source.
        (_another_host, _OTHER_SITE, "", [_LOADS_FAIL]),
    ],
    ids=["clean", "another-body", "planted", "stale", "reads-elsewhere", "another-host"],
)
def test_the_bytecode_check_on_a_downloaded_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: Callable[[Path], None] | None,
    site: str,
    prefix: str,
    said: list[str],
) -> None:
    # check_app.py bytecode, as a person runs it on a copy: the layout, then the program in check
    # mode, then --version with -v, run as the app runs it. Here the .pyc files carry this
    # interpreter's tag.
    monkeypatch.setattr(build_app, "BYTECODE_TAG", TAG)
    app = _bytecode_app(tmp_path / "Downloaded Copy", {**_LIBRARY, **_STUB_HOST})
    stand_in = _BYTECODE_CHECK_RUNTIME.replace("@PYTHON@", shlex.quote(sys.executable))
    stand_in = stand_in.replace("@SITE@", site).replace("@PREFIX@", prefix)
    check_app.python_in(app).write_text(stand_in, "utf-8")
    build_app.compile_bytecode(app, tag=TAG)
    if damage is not None:
        damage(app)
    held = {path: path.read_bytes() for path in _lib(app).rglob("*") if path.is_file()}
    checker = check_app.Checker()
    check_app.check_bytecode(app, checker)
    assert len(checker.failures) == len(said), checker.failures
    for failure, start in zip(checker.failures, said, strict=True):
        assert failure.startswith(start), checker.failures
    # It writes nothing into the copy it checks.
    assert {path: path.read_bytes() for path in _lib(app).rglob("*") if path.is_file()} == held


# ---------------------------------------------------------------------------------------------
# Signing, the wrapper, the toolchain, the comparison


def _macho(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xcf\xfa\xed\xfe" + bytes(28))
    return path


@pytest.mark.parametrize("hardened", [True, False])
def test_signing_order(tmp_path: Path, hardened: bool) -> None:
    pins = dataclasses.replace(build_app.load_pins(), adhoc_hardened_runtime=hardened)
    app = tmp_path / "Capsule.app"
    stdlib = app / build_app.RUNTIME_DIR / build_app.STDLIB
    main = _macho(app / "Contents" / "MacOS" / "Capsule")
    interpreter = _macho(app / build_app.RUNTIME_IN_APP)
    dbm = _macho(stdlib / "lib-dynload" / "_dbm.cpython-313-darwin.so")
    backend = _macho(stdlib / "site-packages" / "_cffi_backend.cpython-313-darwin.so")
    rust = _macho(stdlib / "site-packages" / "cryptography/hazmat/bindings/_rust.abi3.so")
    shallow = _macho(app / "Contents" / "Resources" / "x.dylib")
    (app / "Contents" / "Info.plist").write_bytes(b"<plist/>")
    (app / "Contents" / "Resources" / "script.py").write_text("print()\n", encoding="ascii")
    plan = build_app.signing_plan(app, pins)
    # Libraries deepest first, every one before the interpreter (even the shallow one), then the
    # interpreter, then the bundle; the app's executable only through the bundle.
    assert [argv[-1] for argv in plan] == [
        str(rust),
        str(dbm),
        str(backend),
        str(shallow),
        str(interpreter),
        str(app),
    ]
    assert not any(str(main) in argv for argv in plan)
    identifiers = [argv[argv.index("--identifier") + 1] for argv in plan[:-1]]
    assert identifiers == [
        "com.lucille-ai.capsule.site-packages.cryptography.hazmat.bindings._rust.abi3",
        "com.lucille-ai.capsule.lib-dynload._dbm.cpython-313-darwin",
        "com.lucille-ai.capsule.site-packages._cffi_backend.cpython-313-darwin",
        "com.lucille-ai.capsule.Resources.x",
        "com.lucille-ai.capsule.runtime",
    ]
    assert identifiers[-1] == pins.runtime_id
    assert all(name.startswith(pins.bundle_id + ".") for name in identifiers)
    assert len(set(identifiers)) == len(identifiers)
    assert "--identifier" not in plan[-1]
    for argv in plan:
        assert argv[:2] == ["codesign", "--force"]
        assert argv[argv.index("--sign") + 1] == "-"
        assert "--timestamp=none" in argv
        assert not {"--deep", "--prefix", "--entitlements", "--timestamp"} & set(argv)
        has_runtime = "--options" in argv and argv[argv.index("--options") + 1] == "runtime"
        assert has_runtime is hardened


def test_the_signing_identifier_never_takes_codesigns_derived_name(tmp_path: Path) -> None:
    pins = build_app.load_pins()
    app = tmp_path / "Capsule.app"
    path = app / build_app.RUNTIME_DIR / build_app.STDLIB / "site-packages" / "a b@c.so"
    assert (
        build_app.signing_identifier(app, path, pins)
        == "com.lucille-ai.capsule.site-packages.a-b-c"
    )
    assert (
        build_app.signing_identifier(app, app / build_app.RUNTIME_IN_APP, pins) == pins.runtime_id
    )


def test_wrapper_text() -> None:
    assert build_app.WRAPPER_TEXT == (
        "#!/bin/sh\n"
        'self=$(/bin/realpath -- "$0") || exit 1\n'
        'exec "${self%/*}/../runtime/bin/lucille-host-runtime" -I -B -X utf8 -m lucille_host'
        ' --profile mac-app "$@"\n'
    )
    assert len(build_app.WRAPPER_TEXT.splitlines()) == 3
    assert build_app.RUNTIME_FLAGS == ("-I", "-B", "-X", "utf8")
    # The wrapper runs on the app's profile, the one the checks and the Swift app use (0002 §9),
    # and the wrapper check on a Mac sees the host write that profile's pause flag through it.
    assert build_app.APP_PROFILE == check_app.APP_PROFILE == "mac-app"
    assert f'static let appProfile = "{build_app.APP_PROFILE}"' in _swift("HostCLI.swift")
    check = _python_function(_CHECK_APP.read_text(encoding="utf-8"), "check_wrapper")
    assert '_run_in([str(link), "pause"], home)' in check
    assert '(hosts / APP_PROFILE / "state" / "paused").is_file()' in check
    assert 'not (hosts / "default").exists()' in check
    # The wrapper finds the runtime from its own real path, not from dirname "$0".
    assert "dirname" not in build_app.WRAPPER_TEXT
    wrapper_dir = Path(build_app.WRAPPER_IN_APP).parent
    target = os.path.normpath(wrapper_dir / "../runtime/bin/lucille-host-runtime")
    assert target == build_app.RUNTIME_IN_APP


# A stand-in for the bundled runtime, for the runs check_wrapper makes. It keeps the host's layout
# on macOS (src/lucille_host/paths.py): the last --profile given, or default, whose state folder
# status makes (link.Session does) and pause writes its flag in; and, as the real interpreter
# does, unless given -B it writes the .pyc of lucille_host/__init__.py if there is none, and
# writes nothing where every .pyc is there.
_WRAPPER_CHECK_RUNTIME = """\
#!/bin/sh
here=${0%/*}
cache=$here/../lib/python3.13/site-packages/lucille_host/__pycache__
profile=default
verb=
bytecode=yes
while [ "$#" -gt 0 ]; do
    case $1 in
        -B) bytecode=no ;;
        -I) ;;
        -X | -m) shift ;;
        --profile) profile=$2; shift ;;
        *) [ -n "$verb" ] || verb=$1 ;;
    esac
    shift
done
if [ "$bytecode" = yes ] && [ ! -e "$cache/__init__.@TAG@.pyc" ]; then
    mkdir -p "$cache" && : > "$cache/__init__.@TAG@.pyc" || exit 1
fi
state="$HOME/Library/Application Support/lucille-host/$profile/state"
case $verb in
    --version) echo "lucille-host @VERSION@" ;;
    status) mkdir -p "$state" ;;
    ops) ;;
    pause) @PAUSE@ ;;
    *) exit 2 ;;
esac
"""
_PAUSE = 'mkdir -p "$state" && : > "$state/paused"'
# Two stand-ins the wrapper check must fail: one that writes a file that is not bytecode into the
# bundle even with -B, and one that writes nothing without -B, so the check's control cannot fire.
_WRITES_WITH_B = _WRAPPER_CHECK_RUNTIME.replace(
    'state="$HOME', ': > "$here/written" || exit 1\nstate="$HOME'
)
_WRITES_NOTHING = _WRAPPER_CHECK_RUNTIME.replace(
    'mkdir -p "$cache" && : > "$cache/__init__.@TAG@.pyc" || exit 1', ":"
)


def _wrapper_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wrapper: str = build_app.WRAPPER_TEXT,
    pause: str = _PAUSE,
    stand_in: str = _WRAPPER_CHECK_RUNTIME,
) -> list[str]:
    """Run check_wrapper on an app holding ``wrapper`` and the stand-in runtime, with ``cp`` for
    ``ditto``, and return what failed."""
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "ditto").write_text('#!/bin/sh\nexec cp -pR -- "$1" "$2"\n', encoding="ascii")
    (tools / "ditto").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    app = tmp_path / "Capsule.app"
    build_app.write_wrapper(app).write_text(wrapper, encoding="ascii")
    runtime = app / build_app.RUNTIME_IN_APP
    runtime.parent.mkdir(parents=True)
    script = stand_in.replace("@VERSION@", build_app.host_version())
    script = script.replace("@TAG@", build_app.BYTECODE_TAG)
    runtime.write_text(script.replace("@PAUSE@", pause), encoding="ascii")
    runtime.chmod(0o755)
    # The .pyc the build wrote for the host's package, which the check removes from its copy.
    package = app / build_app.RUNTIME_DIR / build_app.SITE_PACKAGES / "lucille_host"
    pyc = package / "__pycache__" / f"__init__.{build_app.BYTECODE_TAG}.pyc"
    pyc.parent.mkdir(parents=True)
    pyc.write_bytes(b"the build's .pyc")
    checker = check_app.Checker()
    check_app.check_wrapper(app, checker)
    assert pyc.read_bytes() == b"the build's .pyc"
    return checker.failures


def test_the_wrapper_check_passes_a_wrapper_on_the_apps_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _wrapper_check(tmp_path, monkeypatch) == []


@pytest.mark.parametrize(
    ("wrapper", "pause"),
    [
        # The wrapper before the app had a profile of its own: the host runs on default.
        (build_app.WRAPPER_TEXT.replace(f" --profile {build_app.APP_PROFILE}", ""), _PAUSE),
        # The flag lands in the app's profile, but the pause fails.
        (build_app.WRAPPER_TEXT, _PAUSE + " && exit 1"),
        # The flag lands in the app's profile, and a default profile appears beside it.
        (build_app.WRAPPER_TEXT, 'mkdir -p "${state%/*/*}/default" && ' + _PAUSE),
    ],
    ids=["on-default", "pause-fails", "default-appears"],
)
def test_the_wrapper_check_fails_a_pause_that_is_not_on_the_apps_profile_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrapper: str, pause: str
) -> None:
    failures = _wrapper_check(tmp_path, monkeypatch, wrapper, pause)
    assert len(failures) == 1
    assert check_app.APP_PROFILE in failures[0]


@pytest.mark.parametrize(
    ("wrapper", "stand_in", "said"),
    [
        (
            build_app.WRAPPER_TEXT,
            _WRITES_WITH_B,
            "with -B, none of those runs changed anything in the writable copy",
        ),
        (
            build_app.WRAPPER_TEXT,
            _WRITES_NOTHING,
            "without -B the same run writes into the copy",
        ),
        # Every .pyc the build wrote is valid, so a wrapper without -B writes one only where one
        # is missing: the check removes one first, so that it would.
        (
            build_app.WRAPPER_TEXT.replace(" -B", ""),
            _WRAPPER_CHECK_RUNTIME,
            "with -B, none of those runs changed anything in the writable copy",
        ),
    ],
    ids=["writes-with-B", "control-cannot-fire", "wrapper-without-B"],
)
def test_the_wrapper_check_sees_any_write_and_needs_its_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrapper: str, stand_in: str, said: str
) -> None:
    # The bundle ships .pyc files, so the check compares listings of the copy: it sees a write of
    # any file, and a .pyc rewritten in place, not only a new .pyc.
    assert (wrapper, stand_in) != (build_app.WRAPPER_TEXT, _WRAPPER_CHECK_RUNTIME)
    assert " -B " in build_app.WRAPPER_TEXT
    failures = _wrapper_check(tmp_path, monkeypatch, wrapper, stand_in=stand_in)
    assert len(failures) == 1, failures
    assert said in failures[0]


def test_listing_changes_names_each_path_that_differs() -> None:
    before = {"a": (0o644, 1, 1), "b": (0o644, 1, 1), "c": (0o644, 1, 1)}
    after = {"a": (0o644, 1, 1), "b": (0o644, 1, 2), "d": (0o644, 1, 1)}
    assert check_app.listing_changes(before, after) == ["b", "c", "d"]
    assert check_app.listing_changes(before, dict(before)) == []


def test_the_self_test_runs_the_pairing_verbs_on_a_profile_of_its_own() -> None:
    # The self-test runs the real host's pairing verbs on a throwaway profile, never the app's,
    # and checks that HostCLI passes it after the runtime's arguments. It compiles only on a Mac.
    selftest = (build_app.MACOS_DIR / "Tests" / "SelfTest.swift").read_text(encoding="utf-8")
    check = _swift_function(selftest, "static func checkPairingVerbs(")
    assert 'HostCLI(python: python, home: home, profile: "selftest-pair", timeout: 60)' in check
    expected = _swift_array((*macos_lint.RUNTIME_ARGV, "--profile", "selftest-pair", "status"))
    assert f'cli.arguments(for: ["status"]) == {expected},' in check


def test_the_wrapper_runs_the_bundled_runtime_through_a_link(tmp_path: Path) -> None:
    app = tmp_path / "Some Folder" / "Capsule.app"
    wrapper = build_app.write_wrapper(app)
    assert wrapper == app / build_app.WRAPPER_IN_APP
    assert wrapper.stat().st_mode & 0o777 == 0o755
    runtime = app / build_app.RUNTIME_IN_APP
    runtime.parent.mkdir(parents=True)
    runtime.write_text("#!/bin/sh\nprintf 'bundled\\n'\nprintf '%s\\n' \"$@\"\n", encoding="ascii")
    runtime.chmod(0o755)
    # A runtime beside the link, where dirname "$0" would look, must never run.
    link_dir = tmp_path / "bin"
    decoy = link_dir.parent / "runtime" / "bin" / "lucille-host-runtime"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("#!/bin/sh\necho decoy\n", encoding="ascii")
    decoy.chmod(0o755)
    link_dir.mkdir()
    (link_dir / "lucille-host").symlink_to(wrapper)
    done = subprocess.run(
        [str(link_dir / "lucille-host"), "status", "--json", "two words"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "bundled",
        "-I",
        "-B",
        "-X",
        "utf8",
        "-m",
        "lucille_host",
        "--profile",
        "mac-app",
        "status",
        "--json",
        "two words",
    ]
    # A --profile the user gives the wrapper comes after its own, and the host takes the last.
    parser = build_parser()
    assert parser.parse_args(["--profile", "mac-app", "status"]).profile == "mac-app"
    given = parser.parse_args(["--profile", "mac-app", "--profile", "default", "status"])
    assert given.profile == "default"


def test_toolchain_problems() -> None:
    pins = build_app.load_pins()
    xcode = f"Xcode {pins.xcode}\nBuild version 16F6\n"
    assert build_app.toolchain_problems(pins, xcode) == []
    for text in (
        f"Xcode {pins.xcode}.1\n",
        "Xcode 26.0\nBuild version 17A324\n",
        f"Xcode {pins.xcode}0\n",
        f"xcode {pins.xcode}\n",
        "",
    ):
        problems = build_app.toolchain_problems(pins, text)
        assert problems and "tools/macos/pins.toml pins Xcode" in problems[0], text
    assert build_app.developer_dir(pins) == (
        f"/Applications/Xcode_{pins.xcode}.app/Contents/Developer"
    )
    for bad in ("16.4\nEVIL=1", "16.4/../../tmp", "latest"):
        with pytest.raises(build_app.PinError):
            build_app.developer_dir(dataclasses.replace(pins, xcode=bad))


def test_compare_reports_same_different_and_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    name = build_app.load_pins().app_name
    for folder in ("a", "b"):
        app = tmp_path / folder / f"{name}.app"
        (app / "Contents").mkdir(parents=True)
        (app / "Contents" / "Info.plist").write_bytes(b"<plist/>")
        payload.write_tarball(app, tmp_path / folder / build_app.unsigned_tarball_name(name), 7)
    report = build_app.compare(tmp_path / "a", tmp_path / "b", name)
    assert "**SAME**" in report and "sha256 of the .tar inside" in report
    changed = tmp_path / "b" / f"{name}.app" / "Contents" / "Info.plist"
    changed.write_bytes(b"<plist>other</plist>")
    payload.write_tarball(
        changed.parents[1], tmp_path / "b" / build_app.unsigned_tarball_name(name), 7
    )
    report = build_app.compare(tmp_path / "a", tmp_path / "b", name)
    assert "**DIFFERENT**" in report and f"- `{name}.app/Contents/Info.plist`" in report
    assert build_app.main(["compare", str(tmp_path / "a"), str(tmp_path / "missing")]) == 0
    assert "**FAILED**" in capsys.readouterr().out


# The pairing screen, run supervision and Activity: the Foundation-only files they are built from,
# and the rules those keep. The self-test (macos/Tests/SelfTest.swift) checks their behaviour on a
# Mac.
SPEC_CLI = build_app.REPO_ROOT / "spec" / "CLI.md"
NEW_LOGIC = (
    "HostJSON.swift",
    "Pairing.swift",
    "Supervisor.swift",
    "ConnectionText.swift",
    "RequestLog.swift",
)


def _swift(name: str) -> str:
    return (build_app.SWIFT_DIR / name).read_text(encoding="utf-8")


def test_the_new_logic_files_are_compiled_by_the_self_test_and_draw_nothing() -> None:
    # The self-test compiles LOGIC_SOURCES only, and test_the_logic_sources_draw_nothing holds
    # each to Foundation, Darwin and Glibc; so these files can move into a package unchanged.
    assert set(NEW_LOGIC) <= set(check_app.LOGIC_SOURCES)
    for name in NEW_LOGIC:
        assert _swift_imports(_swift(name)) == {"Foundation"}, name
    # The self-test reads the host's golden fixtures, which check_app gives it.
    assert len(list(check_app.CLI_FIXTURES.glob("*.json"))) >= 50
    source = (build_app.TOOLS_DIR / "check_app.py").read_text(encoding="utf-8")
    assert '"--fixtures",\n                str(CLI_FIXTURES),' in source


def test_the_app_runs_every_verb_on_its_own_profile() -> None:
    cli = _swift("HostCLI.swift")
    assert 'static let appProfile = "mac-app"' in cli
    assert (
        "init(python: URL, home: String, profile: String = HostCLI.appProfile, "
        "timeout: TimeInterval = 30)"
    ) in cli
    assert f'{_swift_array(macos_lint.RUNTIME_ARGV)} + ["--profile", profile] + verb' in cli
    # The app itself never chooses another profile: only the self-test does. No file but
    # HostCLI.swift names a profile argument at all, however the call around it is spelled.
    for path in SWIFT_FILES:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"HostCLI\([^)]*\bprofile:", text), path.name
        if path.name != "HostCLI.swift":
            assert not re.search(r"\bprofile\s*:", text), path.name
            assert '"--profile"' not in text, path.name


def test_the_app_environment_has_an_empty_path() -> None:
    cli = _swift("HostCLI.swift")
    assert '"PATH": "/var/empty"' in cli
    assert cli.count('"PATH"') == 1
    for path in SWIFT_FILES:
        assert "/usr/bin:/bin" not in path.read_text(encoding="utf-8"), path.name


def _function_body(text: str, signature: str) -> str:
    start = text.index(signature)
    end = text.index("\n    }\n", start)
    return text[start:end]


def test_the_child_runner_guards_its_pipes() -> None:
    cli = _swift("HostCLI.swift")
    # One place starts every child, under one lock, and only it calls run().
    assert cli.count("try process.run()") == 1
    assert cli.count("HostCLI.spawnLock.lock()") == 1
    spawn = _function_body(cli, "    func spawn(\n")
    assert spawn.index("HostCLI.spawnLock.lock()") < spawn.index("let process = Process()")
    assert "try process.run()" in spawn
    # The ends the app keeps are close-on-exec, the lifeline's included, before anything starts.
    assert "HostCLI.closeOnExec(inPipe.fileHandleForWriting.fileDescriptor)" in spawn
    assert "_ = fcntl(fd, F_SETFD, flags | FD_CLOEXEC)" in cli
    # A short input is written into the pipe before the child starts, non-blocking, and the app's
    # end closed after: no write can meet a broken pipe or wait forever.
    assert spawn.index("writeWithoutWaiting(data") < spawn.index("try process.run()")
    assert spawn.index("try process.run()") < spawn.index("try? written?.close()")
    assert "fcntl(fd, F_SETFL, flags | O_NONBLOCK)" in cli
    assert "static let maxInputBytes = 8193" in cli
    # The handlers and both readers exist before the child does.
    for before in (
        "process.terminationHandler = {",
        "PipeDrain.start(outPipe",
        "PipeDrain.start(errPipe",
    ):
        assert spawn.index(before) < spawn.index("try process.run()"), before
    # read(2) and write(2), never the FileHandle calls that raise an exception Swift cannot catch,
    # or that split lines on more than a newline.
    assert "read(fd, raw.baseAddress, raw.count)" in cli
    for banned in (
        "availableData",
        "readDataToEndOfFile",
        "readabilityHandler",
        ".write(",
        "FileHandle.write",
        ".bytes.lines",
    ):
        assert banned not in cli, banned
    # No signal reaches a child that has ended, whose pid may be another process's now.
    lines = cli.splitlines()
    kills = [number for number, line in enumerate(lines) if re.search(r"\bkill\(", line)]
    assert kills
    for number in kills:
        assert any("isRunning" in line for line in lines[max(0, number - 4) : number]), number
    # The lifeline is closed, never written to.
    assert "case lifeline" in cli and "func closeLifeline()" in cli


def _matches(patterns: tuple[str, ...], text: str) -> list[str]:
    """The patterns that match ``text``."""
    return [pattern for pattern in patterns if re.search(pattern, text)]


# Ways Swift code could read the pasteboard. The user pastes into a field, through the field; the
# app never reads the pasteboard itself (macOS 15.4 and later may ask the user about that).
_READS_THE_PASTEBOARD = (
    r"NSPasteboard",
    r"UIPasteboard",
    r"generalPasteboard",
    r"pasteboardItems",
    r"\breadObjects\s*\(",
    r"\bstring\s*\(\s*forType\s*:",
    r"\bdata\s*\(\s*forType\s*:",
    r"\bpropertyList\s*\(\s*forType\s*:",
)


@pytest.mark.parametrize(
    "line",
    [
        "let pasteboard = NSPasteboard.general",
        "let text = NSPasteboard.general.string(forType: .string)",
        "pasteboard.readObjects(forClasses: [NSString.self], options: nil)",
        "let text = pasteboard.string(forType: .string)",
        "let raw = item.data(forType: .png)",
        "let items = pasteboard.pasteboardItems",
    ],
)
def test_the_pasteboard_scan_sees_each_way(line: str) -> None:
    assert _matches(_READS_THE_PASTEBOARD, line), line


def test_the_app_never_reads_the_pasteboard() -> None:
    # Forwarding Command-V to the field is the user's own paste, and passes.
    assert not _matches(
        _READS_THE_PASTEBOARD, "NSApp.sendAction(#selector(NSText.paste(_:)), to: nil, from: self)"
    )
    for path in SWIFT_FILES:
        assert not _matches(_READS_THE_PASTEBOARD, path.read_text(encoding="utf-8")), path.name
    assert check_app.forbidden_imports(["_OBJC_CLASS_$_NSPasteboard"]) == [
        "_OBJC_CLASS_$_NSPasteboard"
    ]


_NAMES_A_TEST_HUB_OR_FILE_STORE = (r"--hub\b", r"--store-file\b")


@pytest.mark.parametrize(
    "line", ['["pair", "--code-stdin", "--hub", hub, "--json"]', 'verb.append("--store-file")']
)
def test_the_test_hub_scan_sees_each_way(line: str) -> None:
    assert _matches(_NAMES_A_TEST_HUB_OR_FILE_STORE, line), line


def test_the_app_never_names_a_test_hub_or_a_file_store() -> None:
    # The host takes a loopback hub only when a command's own --hub names it, and --store-file
    # keeps the pair outside the Keychain: both are for tests, which the self-test may use, and the
    # app never does (decisions/0001, spec/CLI.md section 7).
    for path in SWIFT_FILES:
        text = path.read_text(encoding="utf-8")
        assert not _matches(_NAMES_A_TEST_HUB_OR_FILE_STORE, text), path.name


def _swift_strings(text: str) -> list[str]:
    """Every string literal in a Swift source: multi-line ones first, then raw and plain ones."""
    found: list[str] = []
    rest = text
    for block in re.findall(r'"""\n(.*?)\n\s*"""', text, re.DOTALL):
        found.append(block)
    rest = re.sub(r'"""\n.*?\n\s*"""', "", rest, flags=re.DOTALL)
    found += re.findall(r'#"(.*?)"#', rest)
    rest = re.sub(r'#".*?"#', "", rest)
    found += re.findall(r'"((?:[^"\\\n]|\\.)*)"', rest)
    return found


def test_the_swift_string_scan_sees_each_form() -> None:
    text = 'let a = "one \\"two\\""\nlet b = #"three "four""#\nlet c = """\n    five\n    """\n'
    assert _swift_strings(text) == ["    five", 'three "four"', 'one \\"two\\"']


def test_every_word_the_app_says_makes_no_claim_privacy_forbids() -> None:
    from tests.test_catalog import _must_never_claim

    phrases = _must_never_claim()
    for name in ("ConnectionText.swift", "HostText.swift"):
        assert len(_swift_strings(_swift(name))) > 50, name
    # Every file, not only the two that hold most of the words: the menu's own lines and the
    # folder notes are written where they are shown.
    for path in SWIFT_FILES:
        name = path.name
        for words in _swift_strings(path.read_text(encoding="utf-8")):
            for phrase in phrases:
                found = re.search(rf"(?<![\w-]){re.escape(phrase)}(?![\w-])", words, re.IGNORECASE)
                assert found is None, (name, phrase, words)


def _spec_verb_codes() -> dict[str, set[str]]:
    """The error codes each verb the app runs can give: its row in spec/CLI.md section 7's table,
    and the codes every verb can give, from the sentence before it. A row is named by the first
    verb in it: ``pause`` is the row of ``pause``, ``resume`` and ``kill``, which Stop What's
    Running runs, and ``log [--limit N]`` is the one Activity's log runs."""
    text = SPEC_CLI.read_text(encoding="utf-8")
    section = text.split("## 7. Verbs", 1)[1].split("\n## ", 1)[0]
    sentence = re.search(r"one of the codes every verb can give: (.*?)\.\n", section, re.DOTALL)
    assert sentence is not None
    every = set(re.findall(r"`([a-z_]+)`", sentence.group(1)))
    assert every == {"usage", "internal", "bad_profile", "unsupported_platform", "os_error"}
    rows: dict[str, set[str]] = {}
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        verb = re.match(r"`([^`]+)`", cells[0])
        assert verb is not None, line
        rows[verb.group(1)] = set(re.findall(r"`([a-z_]+)`", cells[-1])) | every
    wanted = {
        "status",
        "forget",
        "pair --code-stdin [--hub URL] [--store-file]",
        "pair status",
        "pair confirm",
        "pair reject",
        "run --json-events [--exit-on-stdin-eof] [--allow-downgrade]",
        "pause",
        "log [--limit N]",
    }
    assert wanted <= set(rows), sorted(rows)
    return {verb: rows[verb] for verb in wanted}


def _spec_error_codes() -> set[str]:
    text = SPEC_CLI.read_text(encoding="utf-8")
    section = text.split("## 10. Error codes", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| `([a-z_]+)` \|", section, re.MULTILINE))


def test_every_error_code_the_app_meets_has_words() -> None:
    words = _swift("ConnectionText.swift")
    refusals = _swift_string_table(words, "refusals")
    app_bugs = _swift_string_set(words, "appBugCodes")
    redraw = _swift_string_set(_swift("Pairing.swift"), "redrawCodes")
    assert redraw == {"waiting", "sas_ready"}
    handled = set(refusals) | app_bugs | redraw
    codes = _spec_verb_codes()
    met = set().union(*codes.values())
    assert met - handled == set(), sorted(met - handled)
    # Each code has one meaning here, and none is a word the contract does not have.
    assert not set(refusals) & app_bugs and not (set(refusals) | app_bugs) & redraw
    assert handled <= _spec_error_codes(), sorted(handled - _spec_error_codes())
    # The code window comes back only for refusals that have words.
    again = _swift_string_set(_swift("Pairing.swift"), "newCodeCodes")
    assert again <= set(refusals)
    # A code expiring, the time to compare running out, and a leaked code read apart.
    after = re.search(r'static let expiredAfterSAS = "([^"]+)"', words)
    assert after is not None
    assert len({refusals["expired"], after.group(1), refusals["leaked"]}) == 3
    # held covers a pair still being set up, not only a connected one.
    assert "or one being set up" in refusals["held"]
    assert refusals["busy"] == "An earlier attempt is still finishing. Try again in a minute."


def test_every_stopped_reason_is_read() -> None:
    # Every reason run's stopped object can carry (spec/CLI.md section 6) is a case the app reads,
    # and has the exit code the contract gives it.
    text = SPEC_CLI.read_text(encoding="utf-8")
    table = text.split("### `stopped`: why `run` ended", 1)[1].split("\n### ", 1)[0]
    reasons: dict[str, int] = {}
    for line in table.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[2].isdigit():
            for reason in re.findall(r"`([a-z_]+)`", cells[0]):
                reasons[reason] = int(cells[2])
    assert len(reasons) == 15, reasons
    json = _swift("HostJSON.swift")
    reader = _function_body(json, "    private static func readStopReason(")
    exit_codes = _function_body(json, "    var exitCode: Int32? {")
    for reason in reasons:
        assert f'case "{reason}":' in reader, reason
    by_code = re.findall(r"case (\.[^:]+):\n\s+return (\d+)", exit_codes)
    assert {int(code) for _, code in by_code} == set(reasons.values())


def test_every_request_phase_is_read() -> None:
    # Every phase a request object can be in, and every decision and status it carries
    # (spec/CLI.md section 6, `request`), is a case the app reads. A phase it does not know is
    # kept as other, which Activity ignores; the decision and the status are closed (section 12).
    text = SPEC_CLI.read_text(encoding="utf-8")
    table = text.split("### `request`: one request, at each step", 1)[1].split("\n### ", 1)[0]
    phases: set[str] = set()
    values: dict[str, set[str]] = {"decision": set(), "status": set()}
    for line in table.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        phase = re.fullmatch(r"`([a-z_]+)`", cells[0])
        # The table's header names its first column `phase` too.
        if not line.startswith("| `") or phase is None or cells[1] == "Other fields":
            continue
        phases.add(phase.group(1))
        named = re.match(r"`(decision|status)` `([a-z_]+)`", cells[1])
        if named:
            values[named.group(1)].add(named.group(2))
    assert phases == {"received", "decided", "started", "finished"}
    assert values == {
        "decision": {"run", "refused"},
        "status": {"done", "refused", "failed", "abandoned"},
    }
    host_json = _swift("HostJSON.swift")
    reader = _function_body(host_json, "    private static func readRequest(")
    for word in phases | values["decision"] | values["status"]:
        assert f'case "{word}":' in reader, word
    assert "return .other(phase)" in reader
    # The types Activity reads: the events, the log's lines and the operations' summaries.
    assert {"request", "log_line", "op"} <= _swift_string_set(host_json, "knownTypes")
    # The decoder keeps what a request asked for as the host wrote it, a null as nil.
    assert (
        '.received(op: fields.string("op"), root: fields.string("root"), '
        'path: fields.string("path"))'
    ) in reader


def test_every_request_outcome_has_words() -> None:
    # Activity says each way a request ends in words: one sentence for each of the protocol's
    # refusal codes and one for each code of a request that failed, both closed sets
    # (request.schema.json). No sentence says that anything was received: `finished` says the
    # request finished on this Mac, not that Lucille got the reply (spec/CLI.md section 13.2).
    schema = json.loads(
        (check_app.CLI_FIXTURES.parent / "request.schema.json").read_text(encoding="utf-8")
    )
    words = _swift("RequestLog.swift")
    refused = _swift_string_table(words, "refused")
    failed = _swift_string_table(words, "failed")
    assert set(refused) == set(schema["$defs"]["refused_code"]["enum"])
    assert set(failed) == set(schema["$defs"]["failed_code"]["enum"])
    assert len(set(refused.values())) == len(refused)
    assert len(set(failed.values())) == len(failed)
    for sentence in [*refused.values(), *failed.values()]:
        assert sentence.endswith(".") and "received" not in sentence.lower(), sentence
    # A refusal for an ability that is off is a fact, with no suggestion to turn it on
    # (decisions/0002 §13, M18).
    assert refused["not_enabled"] == "Turned away: that ability is off on this Mac."
    for sentence in refused.values():
        assert sentence.startswith("Turned away"), sentence
        assert not re.search(r"\bturn\b.*\bon\b|\benable", sentence, re.IGNORECASE), sentence
    for name, said in (
        ("running", "Running…"),
        ("finished", "Finished on this Mac."),
        ("abandoned", "Dropped: the connection changed."),
        ("stoppedWithRun", "Stopped when \\(appName)'s helper program stopped."),
    ):
        assert f'static let {name} = "{said}"' in words, name


def test_the_app_stops_run_with_the_ladder_the_design_gives() -> None:
    supervisor = _swift("Supervisor.swift")
    for pin in (
        "static let crashWindow: TimeInterval = 600",
        "static let crashLimit = 5",
        "static let quitBound: TimeInterval = 330",
        "static let logoutBound: TimeInterval = 20",
        "static let afterKill: TimeInterval = 40",
        "static let afterStopped: TimeInterval = 10",
        "static let stateSilence: TimeInterval = 120",
        "static let hubStale: TimeInterval = 120",
    ):
        assert pin in supervisor, pin
    # run is started in one place, which only a confirmed status read reaches. These count text,
    # so a moved start gets past them: they are a quick check, and the self-test's walk of the
    # supervisor is the guard.
    assert supervisor.count("spawn(now: now)") == 2
    reader = _function_body(supervisor, "    private mutating func statusRead(")
    assert reader.count("return spawn(now: now)") == 2
    assert reader.index("case .confirmed:") < reader.index("spawn(now: now)")
    assert supervisor.count("[.spawnRun, .terminationGuard(true)]") == 1
    # resume runs only when the user asks.
    assert supervisor.count("[.runResume]") == 1
    assert "case .userResume:\n            return [.runResume]" in supervisor


def test_the_compare_page_keeps_its_safety_properties() -> None:
    # Neither panel hides when the user looks at Lucille's page in another app.
    for name in ("PairingPanel.swift", "ProgressPanel.swift"):
        assert "panel.hidesOnDeactivate = false" in _swift(name), name
    panel = _swift("PairingPanel.swift")
    # The compare page gives neither button a key equivalent, and starts with nothing focused, so
    # Return and Space answer nothing; Connect, on the code page, is the only default button.
    compare = _swift_function(panel, "    private func comparePage(")
    assert "keyEquivalent" not in compare
    assert panel.count('keyEquivalent = "\\r"') == 1
    assert 'connect.keyEquivalent = "\\r"' in panel
    shown = _swift_function(panel, "    func showCompare(")
    assert "panel.initialFirstResponder = nil" in shown
    assert "_ = panel.makeFirstResponder(nil)" in shown
    # No timer in the default mode, which stops while a note, a menu or the wait to quit runs.
    for path in SWIFT_FILES:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\bTimer\s*\(|scheduledTimer", text), path.name


def test_every_close_of_the_pairing_window_reaches_the_flow() -> None:
    # On macOS 15 the title bar's close button, pressed through accessibility, closed the window
    # without going through GatePanel's performClose(_:), and the flow went on taking the compare
    # page for open, so the menu never offered Finish Connecting… again. The panel is the window's
    # delegate: before a user's close it tells the flow and answers no, the flow hiding the window
    # once it agrees; a close that asked nobody still tells the flow.
    panel = _swift("PairingPanel.swift")
    assert "final class PairingPanel: NSObject, NSWindowDelegate {" in panel
    assert "panel.delegate = self" in _swift_function(panel, "    override init() {")
    should = _swift_function(panel, "    func windowShouldClose(_ sender: NSWindow) -> Bool {")
    assert should.count("onClose?()") == 1
    assert should.index("onClose?()") < should.index("return false")
    assert "return true" not in should
    closing = _swift_function(panel, "    func windowWillClose(_ notification: Notification) {")
    assert closing.index("guard page != .none else {") < closing.index("onClose?()")
    # performClose(_:) still asks the flow and never closes the window itself.
    gate = _swift_function(panel, "    override func performClose(_ sender: Any?) {")
    assert "onCloseAsked?()" in gate and "super" not in gate
    # Nothing closes the panel but AppKit on the user's behalf: the flow's hide() orders it out.
    assert "panel.close()" not in panel
    assert "panel.orderOut(nil)" in _swift_function(panel, "    func hide() {")


def test_modal_loops_never_start_inside_a_main_queue_callout() -> None:
    # A modal loop started inside a main-queue callout (a dispatch source's handler, a main-queue
    # block, a main-actor task) keeps the main queue from being served until it ends: no timer,
    # no task and no child's event, the reply to quit included. So every note, question, Open panel
    # and quit starts in a RunLoopTurn, and SIGTERM is not handled on the main queue at all. These
    # read the source; the pairing check's steps with a note up are what show it on a Mac.
    connection = _swift("Connection.swift")
    model = _swift("HostModel.swift")
    assert "RunLoop.main.perform(inModes: [.common]) {" in connection
    assert "RunLoopTurn.perform {" in _swift_function(connection, "    private func pumpSoon(")
    quit = _swift_function(connection, "    func quitChosen() {")
    assert quit.index("RunLoopTurn.perform") < quit.index("terminate(nil)")
    assert (
        sum(path.read_text(encoding="utf-8").count("terminate(nil)") for path in SWIFT_FILES) == 1
    )
    for chosen in ("    func disconnectChosen() {", "    func stopConnectingChosen() {"):
        assert "RunLoopTurn.perform" in _swift_function(connection, chosen), chosen
    assert "RunLoopTurn.perform" in _swift_function(model, "    func addFolder() {")
    adds = _swift_function(model, "    private func addRoot(")
    assert "await RunLoopTurn.ask({ self.refuseWholeDisk() })" in adds
    assert "await RunLoopTurn.ask({ self.confirmBroadFolder(.holdsHome) })" in adds
    # The modal loops there are, each reached only from one of those turns (or from AppKit's own
    # applicationShouldTerminate, for the quit question).
    assert connection.count("runModal()") == 4
    assert model.count("runModal()") == 4
    for path in SWIFT_FILES:
        if path.name not in ("Connection.swift", "HostModel.swift"):
            assert "runModal()" not in path.read_text(encoding="utf-8"), path.name
    reply = _swift_function(connection, "    private func replyIfDone() {")
    assert "RunLoopTurn.perform" in reply and "DispatchQueue.main.async" not in reply
    # SIGTERM: a source on a queue of its own, which stops the children and exits.
    assert "makeSignalSource(signal: SIGTERM, queue: queue)" in connection
    assert "makeSignalSource(signal: SIGTERM, queue: .main)" not in connection
    assert 'private static let queue = DispatchQueue(label: "signals"' in connection


# --- Activity, and Stop What's Running (decisions/0002 §14 and §15) ---------------------------


def test_stop_whats_running_runs_kill_only_for_the_user() -> None:
    # Stop What's Running is the user's act. The supervisor asks for it only on the menu item's
    # input, and only while run runs, as an effect of its own, so the glue never takes it for a
    # Disconnect's kill. Its path runs kill, which pauses the host, and nothing on it resumes:
    # only the user's Resume does (spec/PROTOCOL.md section 16, "Kill and timeout").
    supervisor = _swift("Supervisor.swift")
    connection = _swift("Connection.swift")
    menu = _swift("MenuBarApp.swift")
    assert "        case userStop\n" in supervisor
    assert "        case stopWhatsRunning\n" in supervisor
    assert supervisor.count(".stopWhatsRunning") == 1
    assert (
        "        case .userStop:\n"
        "            guard case .running = phase else {\n"
        "                return []\n"
        "            }\n"
        "            return [.stopWhatsRunning]\n"
    ) in supervisor
    # The menu's item is the only thing that feeds that input.
    for path in SWIFT_FILES:
        if path.name not in ("Supervisor.swift", "Connection.swift"):
            assert ".userStop" not in path.read_text(encoding="utf-8"), path.name
    assert connection.count(".userStop") == 1
    chosen = _swift_function(connection, "    func stopWhatsRunningChosen() {")
    assert "feedRun(.userStop)" in chosen
    assert menu.count("connection.stopWhatsRunningChosen()") == 1
    assert (
        "Button(ConnectionText.stopWhatsRunningItem) { connection.stopWhatsRunningChosen() }"
    ) in menu
    # The effect runs kill on the queue of the other short verbs, then shows what the host says.
    assert "        case .stopWhatsRunning:\n            stopWhatsRunning()\n" in connection
    stop = _swift_function(connection, "    private func stopWhatsRunning() {")
    assert '["--json", "kill"], on: VerbQueue.other' in stop
    assert "self.controlsChanged(paused: paused)" in stop
    assert "ConnectionText.stoppedAndPausedTitle" in stop
    assert "ConnectionText.couldNotStopWhatsRunning" in stop
    for body in (chosen, stop):
        for resumes in ("setPaused(", ".userResume", ".runResume", '"resume"', "disconnectKilled"):
            assert resumes not in body, resumes
    # A Disconnect's kill is still the only one its note reports as a pause.
    kill = connection.split("        case .runKill:\n", 1)[1].split("\n        case .", 1)[0]
    assert "disconnectKilled = true" in kill
    assert connection.count("disconnectKilled = true") == 1


def test_the_menu_offers_stop_whats_running_only_while_an_operation_runs() -> None:
    # Shown only while an operation's process runs (decisions/0002 §14): from a request's
    # `started` to its `finished`, while run lives; above Pause and Resume. Activity… follows the
    # folders, and is always there.
    menu = _swift("MenuBarApp.swift")
    connection = _swift("Connection.swift")
    shown = (
        "        if connection.operationRunning {\n"
        "            Button(ConnectionText.stopWhatsRunningItem) {"
        " connection.stopWhatsRunningChosen() }\n"
        "        }\n"
    )
    assert shown in menu
    assert menu.index(shown) < menu.index('Button("Resume Lucille")')
    assert menu.index(shown) < menu.index('Button("Pause Lucille")')
    activity = menu.index("Button(ConnectionText.activityItem) { connection.activityChosen() }")
    assert menu.index('Menu("Folders")') < activity < menu.index("if connection.items.connect {")
    assert "@Published private(set) var operationRunning = false" in connection
    assert "operationRunning = supervisor.runAlive && requests.operationRunning" in connection
    # What the menu goes by is fed from run's own events: each request event goes to the request
    # log, and run's end closes every request still open, so a run that crashed in the middle of a
    # request leaves nothing that reads as running.
    start = _swift_function(connection, "    private func startRun() {")
    on_object = start.split("        child.onObject = ", 1)[1].split("\n        }\n", 1)[0]
    on_exit = start.split("        child.onExit = ", 1)[1].split("\n        }\n", 1)[0]
    assert (
        "                if case .request(let event) = object {\n"
        "                    self.requests.note(event, at: Date())\n"
        "                    self.activityPanel.update(self.requests.entries)\n"
        "                }\n"
    ) in on_object
    assert (
        "                self.requests.runEnded(at: Date())\n"
        "                self.activityPanel.update(self.requests.entries)\n"
    ) in on_exit
    noted = on_object.index("self.requests.note(")
    assert noted < on_object.index("self.feedRun(.object(object))")
    ended = on_exit.index("self.requests.runEnded(")
    assert ended < on_exit.index("self.feedRun(.exited(childExit))")
    assert connection.count("self.requests.note(") == 1
    assert connection.count("self.requests.runEnded(") == 1


# What the files that keep and show Activity must not do: write a file, a log line or the
# pasteboard, or keep a setting.
_WRITES_OR_LOGS = (
    "write(to",
    "FileManager",
    "FileHandle",
    "fputs",
    "print(",
    "NSLog",
    "os_log",
    "Logger(",
    "Connection.log(",
    "NSPasteboard",
    "UserDefaults",
)


def test_activity_is_kept_in_memory() -> None:
    # What Lucille asked is kept in the app's memory only, while it is open (decisions/0002 §15),
    # at most 500 requests; neither file that holds or shows it writes anything anywhere.
    for name in ("RequestLog.swift", "ActivityPanel.swift"):
        text = _swift(name)
        for banned in _WRITES_OR_LOGS:
            assert banned not in text, (name, banned)
    assert "static let capacity = 500" in _swift("RequestLog.swift")
    # No diagnostic line the app writes names what a request asked for: none interpolates an
    # object, an event or an entry.
    for path in SWIFT_FILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            if re.search(r"\blog\(|\bprint\(", line):
                assert not re.search(r"\\\((?:object|event|entry|requests)\b", line), (
                    path.name,
                    line,
                )


def test_lucilles_text_is_shown_only_in_activity() -> None:
    # What a request asked for (its op, folder alias and path) is Lucille's text. Only
    # RequestLog.swift reads it, where RequestEntry keeps it fileprivate, and only the Activity
    # panel shows RequestText's words, so it reaches no argument, menu line, button or note
    # (decisions/0002 §13).
    log = _swift("RequestLog.swift")
    stored = re.findall(r"^ {4}((?:\w+ )*)(?:let|var) (op|root|path)\b", log, re.MULTILINE)
    assert sorted(name for _, name in stored) == ["op", "path", "root"]
    for modifiers, name in stored:
        assert "fileprivate" in modifiers.split(), name
    for path in SWIFT_FILES:
        text = path.read_text(encoding="utf-8")
        if path.name not in ("HostJSON.swift", "RequestLog.swift"):
            assert ".received" not in text, path.name
        if path.name not in ("RequestLog.swift", "ActivityPanel.swift"):
            assert "RequestText" not in text, path.name
    assert "RequestText." in _swift("ActivityPanel.swift")


def test_activity_text_cannot_be_selected_or_copied() -> None:
    # The build lead's D15: selected text could be copied, a pasteboard write the app does not
    # make yet (Copy Diagnostics, decisions/0002 §15, is later). So nothing in the Activity panel
    # can be selected or edited, and it has no Copy button.
    panel = _swift("ActivityPanel.swift")
    # AppKit makes a wrapping label and a text view selectable by default, so each view that holds
    # words says otherwise: the two labels, and the text view in each of the two boxes.
    assert panel.count("NSTextField(wrappingLabelWithString:") == 2
    assert panel.count("NSTextView.scrollableTextView()") == 2
    labels = "        for label in [header, caption] {\n            label.isSelectable = false\n"
    assert labels in panel
    boxes = panel.split("        for box in [requestsBox, logBox] {\n", 1)[1]
    boxes = boxes.split("\n        }\n", 1)[0]
    assert "                text.isEditable = false\n" in boxes
    assert "                text.isSelectable = false\n" in boxes
    assert "isSelectable = true" not in panel and "isEditable = true" not in panel
    assert not _matches(_READS_THE_PASTEBOARD, panel)
    assert "copy(" not in panel
    for words in _swift_strings(panel):
        assert "Copy" not in words, words


# How a Swift file makes a window: an initializer that takes a content rectangle.
_MAKES_A_WINDOW = re.compile(r"\b[A-Z]\w*\(\s*contentRect:")


def test_no_window_is_restorable() -> None:
    # A window macOS may restore writes a Saved Application State folder named after the bundle
    # identifier, which M12's check would find; an autosaved frame is a setting, and the app keeps
    # none. So every window the app makes is not restorable, and none autosaves its frame.
    made = 0
    for path in SWIFT_FILES:
        text = path.read_text(encoding="utf-8")
        windows = len(_MAKES_A_WINDOW.findall(text))
        made += windows
        assert text.count(".isRestorable = false") >= windows, path.name
        assert "isRestorable = true" not in text, path.name
        assert "frameAutosaveName" not in text and "setFrameAutosaveName" not in text, path.name
    # The pairing window, the progress panel and the Activity panel.
    assert made == 3
    assert "panel.hidesOnDeactivate = false" in _swift("ActivityPanel.swift")


# --- The macOS checks of pairing and run: check_app.py ui and pairing --------------------------


def _python_function(source: str, name: str) -> str:
    """The source of the function ``name`` in ``source``."""
    import ast

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            found = ast.get_source_segment(source, node)
            assert found is not None, name
            return found
    raise AssertionError(f"no function {name}")


_CHECK_APP = build_app.TOOLS_DIR / "check_app.py"


def test_the_checks_use_the_apps_own_profile() -> None:
    # The ui and pairing checks read and change the host's state on the profile the app runs
    # every verb on, as the app does.
    assert f'static let appProfile = "{check_app.APP_PROFILE}"' in _swift("HostCLI.swift")
    assert check_app._profile_folder() == (
        Path.home() / "Library" / "Application Support" / "lucille-host" / check_app.APP_PROFILE
    )
    source = _CHECK_APP.read_text(encoding="utf-8")
    ui = _python_function(source, "check_ui")
    assert 'flag = _profile_folder() / "state" / "paused"' in ui
    # The code window comes last: after the whole disk, before the app is stopped.
    assert ui.index("_drive_whole_disk(") < ui.index(
        "_drive_code_window(checker, python_in(app), screens, (out_log, err_log))"
    )
    for name in (
        "_op_state",
        "_roots",
        "_drive_abilities",
        "_drive_folders",
        "_drive_whole_disk",
        "_drive_code_window",
        "_pair_state",
    ):
        body = _python_function(source, name)
        assert not re.search(r"(?<!\w)_host\(", body), name
    assert '["--profile", APP_PROFILE, *args]' in _python_function(source, "_app_host")
    # The pairing check and the probe run the host as the app does: the app's flags
    # (build_app.RUNTIME_FLAGS, through host_argv), on the app's profile.
    assert 'return host_argv(self.python, ["--profile", APP_PROFILE, *args])' in source
    assert 'host_argv(python, ["--profile", APP_PROFILE, *args]),' in _python_function(
        source, "_probe_one"
    )
    assert '"-I", "-B", "-m", "lucille_host"' not in source


def test_the_pairing_checks_look_for_the_apps_own_words() -> None:
    text = _swift("ConnectionText.swift")
    words = {
        "connectItem": check_app.CONNECT_ITEM,
        "finishItem": check_app.FINISH_ITEM,
        "disconnectItem": check_app.DISCONNECT_ITEM,
        "notConnected": check_app.NOT_CONNECTED,
        "connecting": check_app.CONNECTING,
        "connected": check_app.CONNECTED,
        "codeTitle": check_app.CODE_TITLE,
        "codeSteps": check_app.CODE_WORDS[0],
        "codePasteHow": check_app.CODE_WORDS[1],
        "codeWarning": check_app.CODE_WORDS[2],
        "codeHint": check_app.CODE_HINT,
        "connectButton": check_app.CONNECT_BUTTON,
        "compareTitle": check_app.COMPARE_TITLE,
        "compareQuestion": check_app.COMPARE_QUESTION,
        "theyMatch": check_app.THEY_MATCH,
        "confirmedTitle": check_app.CONFIRMED_TITLE,
        "quitQuestionTitle": check_app.QUIT_QUESTION,
        "quitQuestionText": check_app.QUIT_TEXT,
        "quitButton": check_app.QUIT_BUTTON,
        "finishing": check_app.FINISHING,
        "stopNow": check_app.STOP_NOW,
        "stoppingNow": check_app.STOPPING_NOW,
        "pausedTitle": check_app.PAUSED_TITLE,
        "resumeButton": check_app.RESUME_BUTTON,
        "disconnectQuestionTitle": check_app.DISCONNECT_TITLE,
        "disconnectButton": check_app.DISCONNECT_BUTTON,
        "disconnectedTold": check_app.DISCONNECTED_TOLD,
        "activityItem": check_app.ACTIVITY_ITEM,
        "activityTitle": check_app.ACTIVITY_TITLE,
        "showLog": check_app.SHOW_LOG,
        "stopWhatsRunningItem": check_app.STOP_WHATS_RUNNING,
        "stoppedAndPausedTitle": check_app.STOPPED_AND_PAUSED,
        "activityHeader": check_app.ACTIVITY_HEADER,
        "logCaption": check_app.LOG_CAPTION,
        "logEmpty": check_app.LOG_EMPTY,
    }
    for name, said in words.items():
        assert f'static let {name} = "{_named_in_swift(said)}"' in text, name
    assert f'"bad_code": "{check_app.BAD_CODE}",' in text
    assert check_app.CONNECT_ITEM in check_app.MENU_ITEMS
    # Activity… is always in the menu; Stop What's Running only while an operation runs.
    assert check_app.ACTIVITY_ITEM in check_app.MENU_ITEMS
    assert check_app.STOP_WHATS_RUNNING not in check_app.MENU_ITEMS
    assert check_app.STOPPED_AND_PAUSED == "Stopped, and paused. Resume when you're ready."
    # Activity's words for the requests the pairing check sends (RequestLog.swift): a refusal of
    # an ability that is off, and the fingerprint of a.txt in the check's folder, finished. The
    # request's words start from the core's summary of the operation.
    request = _swift("RequestLog.swift")
    assert f'"not_enabled": "{check_app.NOT_ENABLED_ENTRY}",' in request
    assert f'static let finished = "{check_app.FINISHED_ENTRY}"' in request
    assert OPERATIONS["files.sha256"].about.summary == "Check a file's fingerprint"
    assert check_app.FINGERPRINT_ENTRY == (
        f"Lucille asked to check a file's fingerprint: “a.txt” in {check_app.PAIRING_ALIAS}."
    )
    assert '"Lucille asked to ' in request
    # The ui check cuts the host's log lines as Activity does, to find them in it.
    assert f"static let longestLogLine = {check_app.LONGEST_LOG_LINE}" in request
    entries = (check_app.NOT_ENABLED_ENTRY, check_app.FINISHED_ENTRY, check_app.FINGERPRINT_ENTRY)
    # The lines the app writes on standard error as run exits (RunSupervisor.describe), which the
    # pairing check reads.
    supervisor = _swift("Supervisor.swift")
    assert 'how = "run exited \\(code)"' in supervisor
    assert 'how = "run ended by signal \\(number)"' in supervisor
    assert 'return how + " (" + (stopped?.words ?? "no stopped object") + ")"' in supervisor
    assert 'return "drained, stdin_end"' in _swift("HostJSON.swift")
    assert check_app.DRAINED_LINE == "run exited 0 (drained, stdin_end)"
    assert check_app.SIGNALED_LINE == "run ended by signal"
    # Every word the checks hand AppleScript can be quoted for it.
    assert f'static let quitApp = "{_named_in_swift(check_app.QUIT_ITEM)}"' in _swift(
        "HostText.swift"
    )
    for said in [*words.values(), *entries, "OK", "Cancel", check_app.QUIT_ITEM]:
        assert check_app._quoted(said) == f'"{said}"'
    # The text the ui check pastes passes the app's own check on a code, and so would the one it
    # leaves on the pasteboard, which the app must never read by itself.
    pairing = _swift("Pairing.swift")
    assert 'static let codePrefix = "swbp1_"' in pairing
    # The whole code is then at most 8192 characters, and with its newline fits the input a child
    # gets.
    assert "static let codeBodyLength = 100...8186" in pairing
    assert "static let maxInputBytes = 8193" in _swift("HostCLI.swift")
    for code in (check_app.PASTED_TEXT, check_app.PASTEBOARD_SENTINEL):
        assert re.fullmatch(r"swbp1_[A-Za-z0-9_-]{100,8186}", code), code
    assert check_app.PASTED_TEXT != check_app.PASTEBOARD_SENTINEL


def test_the_checks_open_activity_and_stop_whats_running() -> None:
    # The ui check opens Activity and its log on every job, so the panel is under M12's check; the
    # pairing check sends a request for an ability that is off and reads Activity's entry (M18),
    # then, between its two quits, stops a running request from the menu and sees the host stay
    # paused until Resume.
    source = _CHECK_APP.read_text(encoding="utf-8")
    ui = _python_function(source, "check_ui")
    assert (
        ui.index("_drive_whole_disk(")
        < ui.index("_drive_activity(")
        < ui.index("_drive_code_window(")
    )
    activity = _python_function(source, "_drive_activity")
    for name in ("ACTIVITY_ITEM", "ACTIVITY_TITLE", "SHOW_LOG", "_screenshot("):
        assert name in activity, name
    go = _python_function(source, "go")
    assert (
        go.index("self.quit_and_drain")
        < go.index("self.stop_whats_running")
        < go.index("self.stop_now")
    )
    stop = _python_function(source, "stop_whats_running")
    for name in (
        "STOP_WHATS_RUNNING",
        "STOPPED_AND_PAUSED",
        "STOP_DURING",
        "_sleeping(",
        '"killed_locally"',
        "RESUME_ITEM",
    ):
        assert name in stop, name
    fingerprint = _python_function(source, "fingerprint")
    assert fingerprint.index("NOT_ENABLED_ENTRY") < fingerprint.index('"ops", "enable"')
    assert "p-activity-not-enabled.png" in fingerprint
    assert "FINGERPRINT_ENTRY" in fingerprint and "FINISHED_ENTRY" in fingerprint
    assert check_app.RESUME_ITEM == "Resume Lucille"
    assert 'Button("Resume Lucille")' in _swift("MenuBarApp.swift")
    # A read of Activity's words that misses says what the window holds where its boxes were read,
    # and only then.
    reader = _python_function(source, "read_activity")
    for body in (activity, reader):
        assert body.count("_activity_holds()") == body.count(" else _activity_holds())") >= 1


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        (
            (True, " [a scroll area: AXTextArea, AXScrollBar; AXTextArea holds: ]"),
            "; the window holds: [a scroll area: AXTextArea, AXScrollBar; AXTextArea holds: ]",
        ),
        ((False, "execution error: no window"), "; (System Events: execution error: no window)"),
    ],
)
def test_a_missed_read_of_activity_says_what_holds_its_boxes(
    monkeypatch: pytest.MonkeyPatch, answer: tuple[bool, str], said: str
) -> None:
    # That System Events reads the text of a text view that cannot be selected as its scroll
    # area's `text area 1` is untried on a Mac; when it is not, the first failing run says what
    # each scroll area of the Activity window holds, and what those hold.
    scripts: list[str] = []

    def fake(body: str, timeout: float = 30) -> tuple[bool, str]:
        scripts.append(body)
        return answer

    monkeypatch.setattr(check_app, "_system_events", fake)
    assert check_app._activity_holds() == said
    [script] = scripts
    window = f"window {check_app._quoted(check_app.ACTIVITY_TITLE)}"
    assert f"repeat with theArea in scroll areas of {window}\n" in script
    assert "(role of every UI element of theArea)" in script
    assert "repeat with theElement in UI elements of theArea\n" in script
    assert "(role of every UI element of theElement)" in script
    assert script.endswith("return held")


def test_the_applescript_the_checks_build_names_what_it_looks_for() -> None:
    script = check_app._target("button", check_app.THEY_MATCH)
    # Each window's own elements, as `button "OK" of window 1` finds a note's: a walk through
    # `entire contents` found nothing in the code window on macOS 15.
    assert (
        'if exists (button "They Match" of theWindow) then '
        'set theTarget to button "They Match" of theWindow\n'
    ) in script
    assert script.endswith('error "no button named They Match on the screen:" & held\nend if\n')
    assert "role of every UI element of theWindow" in script
    assert "if exists (text field 1 of theWindow) then" in check_app._target("text field")
    assert "entire contents" not in _CHECK_APP.read_text(encoding="utf-8").replace(
        "does not walk ``entire contents``", ""
    )
    for unquotable in ('say "this"', "back\\slash"):
        with pytest.raises(ValueError):
            check_app._quoted(unquotable)


def test_reading_every_text_says_what_the_windows_hold_when_it_finds_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripts: list[str] = []
    answers = iter([(True, ""), (True, " [Connect to Lucille: AXGroup, AXButton]")])

    def fake(body: str, timeout: float = 30) -> tuple[bool, str]:
        scripts.append(body)
        return next(answers)

    monkeypatch.setattr(check_app, "_system_events", fake)
    assert check_app._texts() == []
    # Each window's own elements, never a walk through `entire contents`.
    assert "set found to found & (value of every static text of theWindow)" in scripts[0]
    assert "entire contents" not in scripts[0]
    # Nothing found: it asks what the windows hold, and says so.
    assert scripts[1] == check_app.WINDOWS_HOLD + "return held"
    assert "AXGroup" in capsys.readouterr().out


def test_the_pairing_check_imports_the_harness_only_when_it_runs() -> None:
    # Every other check runs where neither Switchboard nor the test harness is installed, and this
    # suite imports check_app wherever it runs.
    import ast
    import subprocess

    source = _CHECK_APP.read_text(encoding="utf-8")
    tree = ast.parse(source)
    top: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            top.add(node.module.split(".")[0])
    assert not {"tests", "switchboard"} & top, top
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    importing = {
        node.name: {
            inner.module
            for inner in ast.walk(node)
            if isinstance(inner, ast.ImportFrom) and inner.module
        }
        for node in functions
        if any(isinstance(inner, ast.Import | ast.ImportFrom) for inner in ast.walk(node))
    }
    assert importing == {
        "check_pairing": {"switchboard.client", "tests._lucille", "tests._process", "tests._run"}
    }
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\nimport tools.macos.check_app\n"
            "loaded = {name.split('.')[0] for name in sys.modules}\n"
            "print(sorted(loaded & {'switchboard', 'tests'}))",
        ],
        cwd=build_app.REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert (done.returncode, done.stdout) == (0, "[]\n"), done.stderr


def test_the_probe_never_fails_the_job(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The probe prints what the app's verbs do on the runner and gates nothing: whatever it meets,
    # a program that cannot be started included, it says so and returns.
    def refused(*args: object, **kwargs: object) -> None:
        raise OSError("no such program")

    monkeypatch.setattr("subprocess.run", refused)
    check_app.check_probe(Path("/nonexistent/Capsule.app"))
    assert "no such program" in capsys.readouterr().out


def test_the_pairing_check_says_when_its_harness_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Run with an interpreter that lacks Switchboard or the test harness, it fails and says which
    # interpreter it needs, before it touches the app or the host. (None in sys.modules makes an
    # import of that name fail.)
    monkeypatch.setitem(sys.modules, "switchboard.client", None)
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("ran a program"))
    checker = check_app.Checker()
    check_app.check_pairing(tmp_path / "Capsule.app", checker, tmp_path / "check")
    assert checker.failures == ["the pairing check finds Switchboard and the test harness"]
    assert "suite --copy-to" in capsys.readouterr().out


@pytest.mark.parametrize(
    "module", ["switchboard.client", "tests._lucille", "tests._process", "tests._run"]
)
@pytest.mark.parametrize("there", [False, True], ids=["missing", "without-its-names"])
def test_the_pairing_check_fails_cleanly_whichever_harness_import_fails(
    module: str,
    there: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Each import the check needs, made to fail by itself: missing, or there without the names the
    # check takes from it (an ImportError that is not a ModuleNotFoundError). Set in sys.modules,
    # a tests.* module fails even when another test has already imported it, so this holds
    # whatever order the suite runs in.
    monkeypatch.setitem(sys.modules, module, types.ModuleType(module) if there else None)
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("ran a program"))
    checker = check_app.Checker()
    check_app.check_pairing(tmp_path / "Capsule.app", checker, tmp_path / "check")
    assert checker.failures == ["the pairing check finds Switchboard and the test harness"]
    # It names the import that failed, and the interpreter to run the check with.
    out = capsys.readouterr().out
    assert module in out and "suite --copy-to" in out


# --- The Open panel, on every macOS the jobs run: check_app.py ui -----------------------------
# On macOS 26 and 27 (the smoke jobs, 2026-11-02) `button "Allow Capsule to Use This Folder" of
# window 1` was not found, where macOS 15 finds it. The ui check now looks for the panel's buttons
# in every window, sheet and group, and presses Return, the panel's default button, only when the
# prompt is nowhere; what the app shows next is checked as before.

_PLACES = ("theWindow", "theSheet", "theGroup", "theInner")


def _looks_for(script: str, name: str) -> bool:
    return all(
        f'if exists (button "{name}" of {place}) then set theTarget to button "{name}" of '
        f"{place}\n" in script
        for place in _PLACES
    )


def test_choosing_in_the_open_panel_looks_in_every_window_sheet_and_group() -> None:
    script = check_app.CHOOSE_IN_PANEL
    assert _looks_for(script, check_app.OPEN_PROMPT)
    for walk in (
        "repeat with theWindow in windows\n",
        "repeat with theSheet in sheets of theWindow\n",
        "repeat with theInner in groups of theSheet\n",
        "repeat with theGroup in groups of theWindow\n",
        "repeat with theGroup in splitter groups of theWindow\n",
        "repeat with theInner in groups of theGroup\n",
    ):
        assert walk in script, walk
    # Never only window 1, and never a walk through `entire contents`.
    assert "window 1" not in script and "entire contents" not in script
    # Found: clicked, and nothing else is pressed.
    clicked = script.index('click theTarget\ndelay 2\nreturn "clicked"\n')
    assert script.count("click theTarget") == 1
    assert script.index(f'button "{check_app.OPEN_PROMPT}"') < clicked


def test_return_is_pressed_only_when_neither_the_prompt_nor_what_follows_is_found() -> None:
    script = check_app.CHOOSE_IN_PANEL
    clicked = script.index('return "clicked"')
    # One of the app's own windows that follow the panel already up: nothing is pressed, and the
    # caller checks that window.
    for after in check_app.AFTER_THE_PANEL:
        assert _looks_for(script, after), after
        assert script.index(f'button "{after}"') > clicked
    past = script.index('return "already past the Open panel: it shows "')
    # Otherwise: what the windows hold, then Return, once, and it says so.
    held = script.index(check_app.WINDOWS_HOLD)
    pressed = script.index("set frontmost to true\nkey code 36\n")
    assert clicked < past < held < pressed
    assert script.count("key code") == 1
    assert script.endswith(
        f'return "pressed Return, the default button: no button named " & '
        f'"{check_app.OPEN_PROMPT}" & ", and the app\'s windows held" & held'
    )
    # What follows the panel is the app's own: the name dialog's button, and the broad-folder and
    # whole-disk notes' (App/HostModel.swift).
    model = (build_app.SWIFT_DIR / "HostModel.swift").read_text(encoding="utf-8")
    for after in check_app.AFTER_THE_PANEL:
        assert f'alert.addButton(withTitle: "{after}")' in model, after
    assert set(check_app.AFTER_THE_PANEL) >= {"Add Anyway", check_app.WHOLE_DISK_BUTTONS[0]}


def test_what_the_windows_hold_names_their_buttons_and_sheets() -> None:
    hold = check_app.WINDOWS_HOLD
    assert "role of every UI element of theWindow" in hold
    assert "name of every button of theWindow" in hold
    assert "repeat with theSheet in sheets of theWindow\n" in hold
    # Each part in a try of its own: a window that cannot say one thing still says the rest.
    lines = hold.splitlines()
    assert lines.count("try") == lines.count("end try") == 3


_Block = tuple[str, list["str | _Block"]]


def _blocks(script: str) -> list[str | _Block]:
    """The AppleScript ``script`` as nested blocks: a line that opens one (``try``, ``repeat
    ...``, an ``if ... then`` that ends the line) is ``(line, [what it holds])``, and each must be
    closed by the ``end`` of its own kind. A one-line ``if ... then set ...`` is a line."""
    top: list[str | _Block] = []
    within: list[_Block] = [("", top)]
    for line in script.splitlines():
        first = line.split(" ", 1)[0]
        if line == "try" or first == "repeat" or (first == "if" and line.endswith(" then")):
            block: _Block = (line, [])
            within[-1][1].append(block)
            within.append(block)
        elif first == "end":
            opened = within.pop()[0]
            assert opened.split(" ", 1)[0] == line.removeprefix("end "), (opened, line)
        else:
            within[-1][1].append(line)
    assert len(within) == 1, within[-1][0]
    return top


def test_each_place_the_panel_walk_reads_below_a_window_is_in_a_try_of_its_own() -> None:
    # A sheet or group that cannot be read (one closing while the walk runs) ends its own loop,
    # not the script, so the places after it are still read and the Return fallback still runs.
    def loop(place: str, over: str, *inside: _Block) -> _Block:
        return ("try", [(f"repeat with {place} in {over}", [f"look in {place}", *inside])])

    def groups_of(place: str) -> _Block:
        return loop("theInner", f"groups of {place}")

    assert _blocks(check_app._in_each_place("look in {place}\n")) == [
        (
            "repeat with theWindow in windows",
            [
                "look in theWindow",
                loop("theSheet", "sheets of theWindow", groups_of("theSheet")),
                loop("theGroup", "groups of theWindow", groups_of("theGroup")),
                loop("theGroup", "splitter groups of theWindow", groups_of("theGroup")),
            ],
        )
    ]


def test_the_panel_lookup_sets_the_target_only_while_none_is_found() -> None:
    # The walk reads the window itself outside any try, so the lookup's own try is what keeps a
    # window that cannot be read from ending the script before Return is pressed.
    found = 'button "Open" of {place}'
    assert _blocks(check_app._click_in_place("Open")) == [
        (
            "if theTarget is missing value then",
            [("try", [f"if exists ({found}) then set theTarget to {found}"])],
        )
    ]


def test_the_panel_script_clicks_or_stops_only_on_a_target_it_found() -> None:
    # It starts with no target, clicks only a prompt it found, and stops without pressing anything
    # only on one of the app's own windows it found. Neither found, it reaches its top level's
    # end: what the windows hold, then Return.
    def walk(name: str) -> list[str | _Block]:
        return _blocks(check_app._in_each_place(check_app._click_in_place(name)))

    found = "if theTarget is not missing value then"
    script = _blocks(check_app.CHOOSE_IN_PANEL)
    assert script[:-1] == [
        "set theTarget to missing value",
        *walk(check_app.OPEN_PROMPT),
        (found, ["click theTarget", "delay 2", 'return "clicked"']),
        *[block for after in check_app.AFTER_THE_PANEL for block in walk(after)],
        (found, ['return "already past the Open panel: it shows " & (name of theTarget as text)']),
        *_blocks(check_app.WINDOWS_HOLD),
        "set frontmost to true",
        "key code 36",
        "delay 2",
    ]
    assert isinstance(script[-1], str) and script[-1].startswith("return ")


def test_what_follows_the_panel_is_known_by_every_button_of_its_windows_but_cancel() -> None:
    # The name dialog, the broad-folder question and the whole-disk note (App/HostModel.swift):
    # one found means the panel has chosen, and Return would answer that window instead. Cancel
    # is left out, as the Open panel has one of its own.
    model = (build_app.SWIFT_DIR / "HostModel.swift").read_text(encoding="utf-8")
    titles: set[str] = set()
    for signature in (
        "private func askForAlias(",
        "private func confirmBroadFolder(",
        "private func refuseWholeDisk(",
    ):
        body = _swift_function(model, signature)
        titles |= set(re.findall(r'addButton\(withTitle: "([^"]+)"\)', body))
    assert set(check_app.AFTER_THE_PANEL) == titles - {"Cancel"}


def test_what_the_windows_hold_reads_each_sheet_and_closes_each_window() -> None:
    *_, (windows, [(guard, window)]) = _blocks(check_app.WINDOWS_HOLD)
    assert (windows, guard) == ("repeat with theWindow in windows", "try")
    # Each window's part is closed after whatever its own trys could read.
    assert window[0].startswith('set held to held & " [" & ')
    assert window[-1] == 'set held to held & "]"'
    assert [block[0] for block in window[1:-1]] == ["try", "try"]
    # A sheet's part reads the sheet's elements, not its window's again.
    [(sheets, [read])] = window[2][1]
    assert sheets == "repeat with theSheet in sheets of theWindow"
    assert "of theSheet" in read and "theWindow" not in read


@pytest.mark.parametrize(
    ("answer", "worked", "note"),
    [
        ((True, "clicked"), True, False),
        ((True, "already past the Open panel: it shows Add Folder"), True, True),
        (
            (True, "pressed Return, the default button: no button named x [Add a Folder: AXGroup]"),
            True,
            True,
        ),
        ((False, "execution error: no process"), False, False),
    ],
)
def test_going_to_a_folder_chooses_through_the_panel_script(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    answer: tuple[bool, str],
    worked: bool,
    note: bool,
) -> None:
    scripts: list[str] = []
    answers = iter([(True, ""), answer])

    def fake(body: str, timeout: float = 30) -> tuple[bool, str]:
        scripts.append(body)
        return next(answers)

    monkeypatch.setattr(check_app, "_system_events", fake)
    monkeypatch.setattr(check_app, "_screenshot", lambda path: None)
    folder = Path("/Users/runner/lucille-host-ui-check/My Project Files")
    done = check_app._go_to_folder(folder, tmp_path, "5-open-panel.png")
    # Go to Folder, typed; then the panel script, as it is.
    assert f'keystroke "{folder}"\n' in scripts[0]
    assert scripts[1] == check_app.CHOOSE_IN_PANEL
    assert done[0] is worked
    printed = capsys.readouterr().out
    assert (f"note: choosing {folder} in the Open panel: {answer[1]}" in printed) is note
    if not worked:
        assert done[1] == f"choosing the folder: {answer[1]}"


def test_the_panel_script_is_given_ninety_seconds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # CHOOSE_IN_PANEL walks the app's windows, sheets and groups up to four times (the prompt,
    # then each of AFTER_THE_PANEL) and then says what they hold.
    timeouts: dict[str, float] = {}

    def fake(body: str, timeout: float = 30) -> tuple[bool, str]:
        timeouts[body] = timeout
        return True, "clicked"

    monkeypatch.setattr(check_app, "_system_events", fake)
    monkeypatch.setattr(check_app, "_screenshot", lambda path: None)
    check_app._go_to_folder(Path("/Users/runner/My Project Files"), tmp_path, "5-open-panel.png")
    assert timeouts[check_app.CHOOSE_IN_PANEL] >= 90


@pytest.mark.parametrize(
    ("answer", "read"),
    [
        ((True, f"Cancel\n{check_app.OPEN_PROMPT}"), (True, ["Cancel", check_app.OPEN_PROMPT])),
        ((True, ""), (True, [])),
        ((False, "execution error: no process"), (False, ["execution error: no process"])),
    ],
)
def test_the_panel_buttons_are_read_from_every_window_sheet_and_group(
    monkeypatch: pytest.MonkeyPatch, answer: tuple[bool, str], read: tuple[bool, list[str]]
) -> None:
    calls: list[tuple[str, float]] = []

    def fake(body: str, timeout: float = 30) -> tuple[bool, str]:
        calls.append((body, timeout))
        return answer

    monkeypatch.setattr(check_app, "_system_events", fake)
    assert check_app._panel_buttons() == read
    [(script, timeout)] = calls
    # One list, empty to begin with, gathers the names from every place; a place that cannot be
    # read is passed over, not the end of the walk. Then one name per line.
    assert script.startswith("set found to {}\n")
    for place in _PLACES:
        assert f"try\nset found to found & (name of every button of {place})\nend try\n" in script
    assert "window 1" not in script
    assert script.endswith(f"{check_app.LINES}return found as text")
    assert timeout >= 90


def _what_was_sent(body: str) -> str:
    """A short name for an AppleScript body the whole-disk walk sends to System Events."""
    if body.startswith("set found to {}\n") and "(name of every button of theInner)" in body:
        return "read the panel's buttons"
    for button in ("Choose a Smaller Folder", "Cancel"):
        if body.startswith(f'click button "{button}" of window 1'):
            return button
    if body.splitlines()[:2] == ["set frontmost to true", "key code 53"]:
        return "Escape"
    return body


def _walk_the_whole_disk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    chosen: tuple[bool, str],
    answers: dict[str, list[tuple[bool, str]]],
) -> tuple[list[str], list[str]]:
    """Run ``_drive_whole_disk`` with the first folder chosen as ``chosen`` says, the app refusing
    each folder, and System Events answering each kind of body from ``answers`` in turn (else
    ``(True, "")``): what it did, in order, and the checks that failed."""
    steps: list[str] = []

    def system_events(body: str, timeout: float = 30) -> tuple[bool, str]:
        steps.append(_what_was_sent(body))
        queue = answers.get(steps[-1])
        return queue.pop(0) if queue else (True, "")

    def choose(folder: Path, screens: Path, shot: str) -> tuple[bool, str]:
        steps.append("choose")
        return chosen

    def go_to(folder: Path, screens: Path, shot: str) -> tuple[bool, str]:
        steps.append("go to")
        return True, "clicked"

    def refused(checker: check_app.Checker, chosen: str, shot: Path) -> bool:
        steps.append("refused")
        return True

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(check_app, "_system_events", system_events)
    monkeypatch.setattr(check_app, "_choose_folder", choose)
    monkeypatch.setattr(check_app, "_go_to_folder", go_to)
    monkeypatch.setattr(check_app, "_whole_disk_refused", refused)
    monkeypatch.setattr(check_app, "_roots", lambda python: {})
    checker = check_app.Checker()
    check_app._drive_whole_disk(checker, tmp_path / "python", tmp_path)
    return steps, checker.failures


def test_the_whole_disk_walk_reads_the_panel_once_each_click_is_done(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Each check that the Open panel came back, or did not, reads the buttons once the click
    # that should have done it is done.
    panel = [(True, f"Cancel\n{check_app.OPEN_PROMPT}"), (True, "")]
    steps, failed = _walk_the_whole_disk(
        monkeypatch, tmp_path, (True, "clicked"), {"read the panel's buttons": panel}
    )
    assert failed == []
    assert steps == [
        "choose",
        "refused",
        "Choose a Smaller Folder",
        "read the panel's buttons",
        "go to",
        "refused",
        "Cancel",
        "read the panel's buttons",
        "Cancel",
        "Cancel",
    ]


@pytest.mark.parametrize(
    ("cancel", "cleanup"),
    [(True, ["Cancel", "Cancel"]), (False, ["Cancel", "Escape", "Cancel", "Escape"])],
)
def test_the_whole_disk_cleanup_presses_escape_where_cancel_cannot_be_clicked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cancel: bool, cleanup: list[str]
) -> None:
    # The Open panel's Cancel may not be a button of window 1. Where clicking it fails, the
    # cleanup makes the app frontmost, as a key code goes to the frontmost app, and sends Escape;
    # on each of its two passes.
    steps, _ = _walk_the_whole_disk(
        monkeypatch, tmp_path, (False, "no process"), {"Cancel": [(cancel, "")] * 2}
    )
    assert steps == ["choose", *cleanup]


def test_the_home_folder_check_reads_the_panel_once_choose_a_smaller_folder_is_clicked() -> None:
    # _drive_whole_disk is run above; _drive_folders, which needs the host's roots to change as the
    # app is driven, is read.
    folders = _python_function(_CHECK_APP.read_text(encoding="utf-8"), "_drive_folders")
    assert folders.index('click button "Choose a Smaller Folder" of window 1') < folders.index(
        "worked, buttons = _panel_buttons()"
    )


def test_the_panel_checks_read_every_window_sheet_and_group() -> None:
    source = _CHECK_APP.read_text(encoding="utf-8")
    # The checks that the Open panel came back, or did not, read its buttons wherever they sit.
    for name in ("_drive_folders", "_drive_whole_disk"):
        body = _python_function(source, name)
        assert "OPEN_PROMPT in buttons" in body, name
        reads = re.findall(r"(\w+), buttons = (_\w+)\(([^)]*)\)\n", body)
        # Every read of buttons after which the Open panel's prompt is looked for.
        for found, function, _ in reads:
            after = body.split(f"{found}, buttons = {function}(", 1)[1].split("checker.expect", 2)
            if "OPEN_PROMPT" in after[1]:
                assert function == "_panel_buttons", (name, found, function)
    folders = _python_function(source, "_drive_folders")
    disk = _python_function(source, "_drive_whole_disk")
    assert folders.count("worked, buttons = _panel_buttons()") == 1
    assert disk.count("worked, buttons = _panel_buttons()") == 1
    assert disk.count("still, buttons = _panel_buttons()") == 1
    # No check looks for the prompt in window 1 alone any more.
    assert 'button "{OPEN_PROMPT}" of window 1' not in source
    assert "_in_each_place(" in _python_function(source, "_panel_buttons")
