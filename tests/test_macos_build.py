"""The parts of tools/macos/build_app.py and tools/macos/check_app.py that run anywhere: the pins,
the sha256 gate, the hashed build backend, Info.plist, the disk image's name, the parsers the
checks use, the bytecode program (run here by the interpreter that runs these tests), and the
rules the Swift sources keep. The build itself runs on a Mac, in ci.yml's macos-app job, whose
steps tests/test_ci_workflow.py reads.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import dataclasses
import errno
import hashlib
import json
import os
import plistlib
import random
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import types
import warnings
import zlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

import lucille_host
from lucille_host import policy
from lucille_host.cli import build_parser, main
from lucille_host.ops import OPERATIONS
from tools.guards import ast_lint, macos_lint
from tools.macos import build_app, check_app, payload

PINS_TEXT = build_app.PINS_FILE.read_text(encoding="utf-8")
# The signing pin's line as committed: ad hoc builds without the hardened runtime.
SIGNING_PIN = "adhoc_hardened_runtime = false"
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
        (SIGNING_PIN, 'adhoc_hardened_runtime = "yes"'),
        (SIGNING_PIN, "adhoc_hardened_runtime = 1"),
        (SIGNING_PIN, "adhoc_hardened_runtime = 0"),
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
    # The signing pin takes either value: true is still a path the build can take.
    assert SIGNING_PIN in PINS_TEXT
    assert build_app.parse_pins(PINS_TEXT).adhoc_hardened_runtime is False
    on = PINS_TEXT.replace(SIGNING_PIN, "adhoc_hardened_runtime = true", 1)
    assert build_app.parse_pins(on).adhoc_hardened_runtime is True


def test_ad_hoc_builds_are_signed_without_the_hardened_runtime() -> None:
    # On the maintainer's Mac (2026-11-08, macOS 26.2), the ad hoc build of ci run 42509782273,
    # signed with the hardened runtime, could not load cryptography's Rust extension: dyld
    # refused it ("mapping process and mapped file (non-platform) have different Team IDs").
    # The app then failed to pair; that this refusal caused it is inferred from the app's error.
    # In that run the macos-15 runner had loaded the same build, so CI did not show this.
    # decisions/0002 §7 records it. Set the pin back only on new evidence from a Mac.
    assert build_app.load_pins().adhoc_hardened_runtime is False
    assert re.findall(r"^adhoc_hardened_runtime = .*$", PINS_TEXT, re.MULTILINE) == [SIGNING_PIN]
    decision = (build_app.REPO_ROOT / "decisions" / "0002-macos-app.md").read_text(encoding="utf-8")
    section = decision.split("\n## 7. ", 1)[1].split("\n## 8. ", 1)[0]
    assert "run 42509782273" in section
    assert "different Team IDs" in section
    assert SIGNING_PIN in " ".join(section.split())


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
        "CFBundleIconFile",
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
    # The icon is Contents/Resources/AppIcon.icns. There is no asset catalog, so no
    # CFBundleIconName, which would name an asset in one.
    assert plist["CFBundleIconFile"] == "AppIcon" == build_app.icons.APP_ICON
    assert "CFBundleIconName" not in plist
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
    assert "app icon          AppIcon.icns, 10 images from 7 PNGs in macos/Icons" in out
    assert "menu bar icons    6 template PNGs from macos/Icons" in out
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


def test_bad_icon_sources_are_refused_before_anything_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The build reads the committed PNGs and renders nothing. It checks them with the pins, before
    # it checks for a Mac, so the refusal is the same on any system, names the file, and comes
    # before anything is written.
    bad = tmp_path / "Icons"
    shutil.copytree(build_app.icons.ICONS_DIR, bad)
    (bad / "app-64.png").write_bytes((bad / "app-32.png").read_bytes())
    monkeypatch.setattr(build_app.icons, "ICONS_DIR", bad)
    wheel, digest = _host_wheel(tmp_path / "wheel")
    for command in ("assemble", "build"):
        out = tmp_path / f"out-{command}"
        argv = [command, "--out", str(out), "--host-wheel", str(wheel)]
        argv += ["--host-wheel-sha256", digest, "--build-number", "1"]
        assert build_app.main(argv) == 1
        err = capsys.readouterr().err
        assert "app-64.png is 32x32 pixels, not 64x64" in err, err
        assert "Apple silicon" not in err
        assert not out.exists()
    out = tmp_path / "out-plan"
    assert build_app.main(["plan", "--out", str(out), "--build-number", "1"]) == 1
    assert "app-64.png is 32x32 pixels, not 64x64" in capsys.readouterr().err
    assert not out.exists()
    # In assemble: the sources before the Mac check; the icons written after the licences; and
    # the bundle's icons checked again, with the rest, before the tarball.
    assemble = _python_function(
        (build_app.TOOLS_DIR / "build_app.py").read_text(encoding="utf-8"), "assemble"
    )
    steps = (
        "check_host_wheel(",
        "check_icon_sources()",
        "require_apple_silicon(",
        "copy_licenses(",
        'icons.write_icons(contents / "Resources")',
        "icons.bundle_problems(app)",
        "write_tarball(",
    )
    places = [assemble.find(step) for step in steps]
    assert -1 not in places and places == sorted(places), dict(zip(steps, places, strict=True))
    plan = _python_function(
        (build_app.TOOLS_DIR / "build_app.py").read_text(encoding="utf-8"), "plan"
    )
    assert plan.index("check_icon_sources()") < plan.index("out.mkdir(")


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
# use Foundation alone; the app's other files add AppKit and SwiftUI, and nothing else but the
# one exception below.
_SWIFT_IMPORT = re.compile(
    r"^\s*(?:@\w+\s+)*import\s+(?:(?:typealias|struct|class|enum|protocol|let|var|func)\s+)?"
    r"(\w+)",
    re.MULTILINE,
)
_LOGIC_IMPORTS = {"Foundation", "Darwin", "Glibc"}
_APP_IMPORTS = _LOGIC_IMPORTS | {"AppKit", "SwiftUI"}
# One file may import one framework more, by name: the login item's (decisions/0002 §9).
_IMPORT_EXCEPTIONS: dict[str, frozenset[str]] = {
    "LoginItem.swift": frozenset({"ServiceManagement"}),
}


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
    # One named exception: LoginItem.swift, and no other file, imports ServiceManagement, for the
    # login item. It is not added to _APP_IMPORTS, so no other file may; and LoginItem.swift is
    # not a logic file, so the self-test, which runs without a screen, never registers anything.
    assert SWIFT_FILES
    names = {path.name for path in SWIFT_FILES}
    assert set(_IMPORT_EXCEPTIONS) <= names, sorted(set(_IMPORT_EXCEPTIONS) - names)
    for path in SWIFT_FILES:
        imports = _swift_imports(path.read_text(encoding="utf-8"))
        allowed = _IMPORT_EXCEPTIONS.get(path.name, frozenset())
        assert imports <= _APP_IMPORTS | allowed, path.name
        assert allowed <= imports, path.name
    assert _swift_imports(_swift("LoginItem.swift")) == {"Foundation", "ServiceManagement"}
    assert "LoginItem.swift" not in check_app.LOGIC_SOURCES
    assert "ServiceManagement" not in _APP_IMPORTS
    assert "ServiceManagement" not in _swift_imports(_SELF_TEST.read_text(encoding="utf-8"))


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


def test_the_abilities_menu_says_the_cores_words() -> None:
    # Each ability's words are the core's (decisions/0002 §8 and §13): its name in the menu is
    # `ops list --json`'s `summary`, and the sentence under it `sends`. The Swift tables that held
    # the app's own words are gone, so a catalog entry needs no change in the app to be named.
    text = (build_app.SWIFT_DIR / "HostText.swift").read_text(encoding="utf-8")
    for path in SWIFT_FILES:
        source = path.read_text(encoding="utf-8")
        assert "abilityTitles" not in source and "abilityDetails" not in source, path.name
    assert "    var title: String { summary ?? name }\n" in text
    assert "    var detail: String? { sends }\n" in text
    # The menu leaves out the operations meant for testing, by the core's `diagnostic`, and
    # host.describe, which is on from the start and is described in the menu instead.
    assert _swift_string_set(text, "unlistedAbilities") == {"host.describe"}
    assert (
        "    var isShown: Bool {\n"
        "        !diagnostic && state != .notInCatalog"
        " && !HostText.unlistedAbilities.contains(name)\n"
        "    }\n"
    ) in text
    listed = {
        name: op.about
        for name, op in OPERATIONS.items()
        if not op.about.diagnostic and name != "host.describe"
    }
    assert set(listed) == {"files.sha256"}
    assert OPERATIONS["host.sleep"].about.diagnostic
    # What the ui check looks for in the menu is the core's words for each listed ability.
    assert check_app.ABILITY_TITLES == {name: about.summary for name, about in listed.items()}
    assert check_app.ABILITY_DETAILS == {name: about.sends for name, about in listed.items()}
    # The one visible change: the sentence under "Check a file's fingerprint" is the core's.
    assert check_app.ABILITY_DETAILS["files.sha256"] == (
        "Lucille gets a code that identifies the file's exact contents, not the contents."
    )


def _op_states(name: str) -> list[str]:
    """The states a type's schema allows, in its order (spec/CLI.md section 6)."""
    schema = json.loads(
        (check_app.CLI_FIXTURES.parent / f"{name}.schema.json").read_text(encoding="utf-8")
    )
    return [
        schema["$defs"][branch["$ref"].rsplit("/", 1)[1]]["properties"]["state"]["const"]
        for branch in schema["oneOf"]
    ]


def test_every_type_the_menu_reads_is_decoded() -> None:
    # The menu reads `--version`, `ops` and `roots` as JSON (spec/CLI.md section 7): each object
    # type those verbs write is one the app knows, decoded by a case of its own, and every state
    # of the closed `state`s of `op` and `root_check` is read (section 12). A state this release
    # does not know makes the object one it cannot read, never a guess.
    host_json = _swift("HostJSON.swift")
    known = _swift_string_set(host_json, "knownTypes")
    menu_types = {"version", "op", "op_removed", "root", "root_removed", "root_check"}
    assert menu_types | {"alias_suggestion"} <= known, sorted(known)
    decoder = _function_body(host_json, "    init?(type: String, _ fields: JSONFields) {")
    for kind in menu_types | {"alias_suggestion"}:
        assert f'case "{kind}":' in decoder, kind
    ops = _function_body(host_json, "    private static func readOp(")
    assert _op_states("op") == [
        "enabled",
        "disabled",
        "disabled_by_upgrade",
        "unavailable",
        "not_in_catalog",
    ]
    for state in _op_states("op"):
        assert f'case "{state}":' in ops, state
    # The switch's position and the acknowledgement depend on fields, never on words: each is
    # read as a boolean, and an entry of the catalog without one is not read at all.
    for field in ("enabled", "sends_file_content", "diagnostic"):
        assert f'fields.bool("{field}")' in ops, field
    assert 'fields.string("missing_executable")' in ops
    assert "default:\n            return nil" in ops
    checks = _function_body(host_json, "    private static func readFolderCheck(")
    assert _op_states("root_check") == ["ok", "denied_by_macos", "denied", "missing", "error"]
    for state in _op_states("root_check"):
        assert f'case "{state}":' in checks, state
    assert 'fields.string("errno")' in checks
    assert "default:\n            return nil" in checks
    # A suggested alias is offered to the user as a name, so it is read only in the form the
    # host's aliases take.
    suggestion = decoder.split('case "alias_suggestion":', 1)[1].split("\n        case ", 1)[0]
    assert "Alias.isValid(" in suggestion
    # Set Up reads the policy a pairing set aside (`policy previous`): its two states, closed as
    # every state is (section 12), when it was set aside as a time, its operations as strings and
    # each folder whole, or not at all.
    assert "previous_policy" in known
    assert 'case "previous_policy":' in decoder
    previous = _function_body(host_json, "    private static func readPreviousPolicy(")
    assert _op_states("previous_policy") == ["none", "found"]
    for state in _op_states("previous_policy"):
        assert f'case "{state}":' in previous, state
    assert "default:\n            return nil" in previous
    for field in (
        'fields.time("set_aside_at")',
        'fields.strings("ops")',
        'fields.array("folders")',
    ):
        assert field in previous, field
    # A folder's alias becomes an argument of `roots add`: read only in the form the host's aliases
    # take, else nothing of the object is read.
    assert (
        'guard let alias = folder.string("alias"), Alias.isValid(alias),'
        ' let path = folder.string("path"),' in previous
    )
    # The accessors the menu reads.
    for accessor in (
        "    var hostVersion: String? {",
        "    var abilities: [Ability] {",
        "    var ability: Ability? {",
        "    var removedOp: String? {",
        "    var folders: [Folder] {",
        "    var folder: Folder? {",
        "    var removedFolder: String? {",
        "    var folderChecks: [FolderCheck] {",
        "    var aliasSuggestion: String? {",
        "    var previousPolicy: PreviousPolicy? {",
        "    var error: HostError? {",
    ):
        assert accessor in host_json, accessor


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


def test_the_menu_bar_icon_is_the_committed_glyph_with_the_symbol_behind_it() -> None:
    # HostModel.swift draws, so the self-test cannot run it: its text is pinned instead. The
    # glyph is a copy, so that its size and description are not set on the image NSImage caches;
    # without the files, the icon is the SF Symbol it was before.
    model = (build_app.SWIFT_DIR / "HostModel.swift").read_text(encoding="utf-8")
    icon = _swift_function(
        model, "    static func icon(paused: Bool, attention: Bool = false) -> NSImage {"
    )
    running, paused, attention = build_app.icons.GLYPHS
    assert f'let glyph = attention ? "{attention}" : (paused ? "{paused}" : "{running}")' in icon
    assert "if let found = NSImage(named: glyph)?.copy() as? NSImage {" in icon
    points = build_app.icons.GLYPH_POINTS
    assert f"found.size = NSSize(width: {points}, height: {points})" in icon
    assert (
        'let symbol = attention ? "exclamationmark.triangle" : (paused ? "pause.circle" : '
        '"desktopcomputer")'
    ) in icon
    assert (
        "image = NSImage(systemSymbolName: symbol, accessibilityDescription: name) ?? NSImage()"
    ) in icon
    # The description and the template flag are set after the branch, on the glyph and on the
    # symbol alike: the function ends so.
    assert icon.endswith(
        "        }\n"
        "        image.accessibilityDescription = name\n"
        "        image.isTemplate = true\n"
        "        return image\n"
        "    }\n"
    ), icon[-200:]
    # No other Swift file loads an image by its name.
    for path in [*SWIFT_FILES, build_app.MACOS_DIR / "Tests" / "SelfTest.swift"]:
        found = path.read_text(encoding="utf-8").count("NSImage(named:")
        assert found == (1 if path.name == "HostModel.swift" else 0), path.name
    menu = (build_app.SWIFT_DIR / "MenuBarApp.swift").read_text(encoding="utf-8")
    assert (
        "Image(nsImage: HostModel.icon(paused: model.paused, attention: connection.needsAttention))"
    ) in menu


def test_the_welcome_note_describes_the_capsule() -> None:
    text = (build_app.SWIFT_DIR / "HostText.swift").read_text(encoding="utf-8")
    literal = text.split('    static let welcomeText = """\n', 1)[1].split('        """\n', 1)[0]
    words = " ".join(re.sub(r"\\\n\s*", "", literal).split())
    assert "small computer" not in words and "pause sign" not in words
    assert (
        "by the clock: a capsule in two halves, with a pause badge while Lucille is paused."
        in words
    )


_BROAD_FLAG = '"--allow-home-or-root"'


def test_the_app_never_passes_the_broad_flag_for_the_whole_disk() -> None:
    text = (build_app.SWIFT_DIR / "HostText.swift").read_text(encoding="utf-8")
    model = (build_app.SWIFT_DIR / "HostModel.swift").read_text(encoding="utf-8")
    # One place builds the argument, and it leaves the flag out for the whole disk. The app never
    # reads the flag's name either: the host's refusal is `broad_folder`, a code.
    for path in SWIFT_FILES:
        source = path.read_text(encoding="utf-8")
        if path.name != "HostText.swift":
            assert _BROAD_FLAG not in source, path.name
    assert text.count(f"append({_BROAD_FLAG})") == 1
    assert text.count(_BROAD_FLAG) == 1
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


def test_roots_add_passes_double_dash_before_the_alias_and_path() -> None:
    # spec/CLI.md section 7: "A shell passes `--` before the alias and the path, so that neither
    # is read as an option", with `--json` and `--allow-home-or-root` before it (section 3).
    text = _swift("HostText.swift")
    adds = _swift_function(text, "    static func addFolderArguments(")
    assert '        var verb = ["--json", "roots", "add"]\n' in adds
    flag = adds.index(f"verb.append({_BROAD_FLAG})")
    dashes = adds.index('        verb += ["--", alias, path]\n')
    assert flag < dashes < adds.index("        return verb\n")
    assert adds.count('"--"') == 1 and adds.count("alias") == 2 and adds.count("verb +=") == 1
    # The host's golden fixture of `roots add` is the command line the app builds.
    fixture = json.loads((check_app.CLI_FIXTURES / "roots-add.json").read_text(encoding="utf-8"))
    assert fixture["argv"] == ["--json", "roots", "add", "--", "docs", "/home/me/Documents"]
    # The host's own parser reads each form the app builds as the alias and the path, a path that
    # starts with "-" included; without "--" it would be an option.
    parser = build_parser()
    for flags in ([], ["--allow-home-or-root"]):
        for folder in ("/Users/a/Documents", "-notes", "--json"):
            args = parser.parse_args(["--json", "roots", "add", *flags, "--", "docs", folder])
            assert (args.alias, args.path, args.json) == ("docs", folder, True)
            assert args.allow_home_or_root == bool(flags) and args.suggest_alias is None
    with pytest.raises(Exception, match="unrecognized arguments: -notes"):
        parser.parse_args(["--json", "roots", "add", "docs", "-notes"])
    # No other Swift file builds a `roots add` line, and the app removes a folder by its alias
    # after `--json`, which `roots remove` takes as its only argument.
    for path in SWIFT_FILES:
        source = path.read_text(encoding="utf-8")
        if path.name != "HostText.swift":
            assert '"roots", "add"' not in source, path.name
    assert '["--json", "roots", "remove", folder.alias]' in _swift("HostModel.swift")


def test_the_name_dialog_starts_from_the_hosts_suggestion() -> None:
    # spec/CLI.md section 9.1: the host suggests the alias (`roots add --suggest-alias <path>`,
    # one `alias_suggestion`), and a path that starts with "-" is written `--suggest-alias=<path>`
    # (section 7). The app still checks what the user types (`Alias.review`), and when the host's
    # answer can't be read, the dialog opens with the app's own suggestion, and says nothing.
    text = _swift("HostText.swift")
    model = _swift("HostModel.swift")
    asks = _swift_function(
        text, "    static func suggestAliasArguments(path: String) -> [String] {"
    )
    assert 'path.hasPrefix("-")' in asks
    assert '["--json", "roots", "add", "--suggest-alias=" + path]' in asks
    assert '["--json", "roots", "add", "--suggest-alias", path]' in asks
    parser = build_parser()
    for folder in ("/Users/a/Café Notes", "/", "-notes", "--json", "-"):
        if folder.startswith("-"):
            argv = ["--json", "roots", "add", "--suggest-alias=" + folder]
        else:
            argv = ["--json", "roots", "add", "--suggest-alias", folder]
        args = parser.parse_args(argv)
        assert (args.suggest_alias, args.alias, args.path, args.json) == (folder, None, None, True)
    with pytest.raises(Exception, match="expected one argument"):
        parser.parse_args(["--json", "roots", "add", "--suggest-alias", "-notes"])
    # The host's answer, when it is one the app can use; else the app's own suggestion.
    chosen = _swift_function(text, "    static func suggestion(")
    assert (
        "    static func suggestion(_ outcome: VerbOutcome, folderName: String, taken: Set<String>)"
        " -> String {\n"
    ) in chosen
    assert (
        "if let alias = outcome.aliasSuggestion, Alias.isValid(alias), !taken.contains(alias) {"
    ) in chosen
    assert "return Alias.suggest(forFolderName: folderName, taken: taken)" in chosen
    # The menu asks the host once a folder is chosen, off the main thread, and only then shows
    # the name dialog, in a run loop turn of its own; nothing on that path shows a note.
    assert model.count("HostText.suggestAliasArguments(path: path)") == 1
    assert model.count("HostText.suggestion(") == 1
    assert "Alias.suggest(" not in model
    naming = _swift_function(model, "    private func nameAndAdd(")
    asked = naming.index(
        "let asked = await run(HostText.suggestAliasArguments(path: path), timeout: suggestTimeout)"
    )
    turn = naming.index("RunLoopTurn.perform")
    assert asked < naming.index("HostText.suggestion(") < turn < naming.index("askForAlias(")
    # What the dialog starts from is the host's answer, through `suggestion`, and nothing else:
    # the `ui` check's folder gets the same alias from the host and from the app, so only these
    # lines and the self-test's choice between the two tell them apart.
    assert (
        "        let suggestion = HostText.suggestion(VerbOutcome(asked),"
        " folderName: url.lastPathComponent, taken: taken)\n"
    ) in naming
    assert naming.count("asked") == 2 and naming.count("suggestion: suggestion,") == 1
    assert "suggestion: suggestion, folderName: name, taken: taken, place: place, note: note" in (
        naming
    )
    dialog = _swift_function(model, "    private func askForAlias(")
    assert "        var current = suggestion\n" in dialog
    assert "            field.stringValue = current\n" in dialog
    for alarm in ("showFailure", "showNote", "Alerts.", "explain(", "problem"):
        assert alarm not in naming, alarm
    assert "private let suggestTimeout: TimeInterval = 10\n" in model
    assert model.count("askForAlias(") == 2
    assert "Task { await nameAndAdd(" in _swift_function(model, "    private func chooseAndAdd() {")
    # Only the fallback and the check of what the user typed make an alias in the app.
    for path in SWIFT_FILES:
        if path.name not in ("HostText.swift", "Alias.swift"):
            assert "Alias.suggest(" not in path.read_text(encoding="utf-8"), path.name
    assert (
        "switch Alias.review(typed: field.stringValue, suggestion: suggestion, taken: taken) {"
        in (model)
    )


def test_the_hosts_command_line_answers_each_suggestion_the_app_asks_for(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The command lines `HostText.suggestAliasArguments` builds (pinned above), run through the
    # host's own `main` on the app's profile, so that `_check_usage` and the verb itself read them
    # too, not only the parser: a path that starts with "-" is written `--suggest-alias=<path>`
    # and read as the path; written with a space, it is a usage error (spec/CLI.md section 7).
    def run(*argv: str) -> tuple[int, list[dict[str, Any]]]:
        capsys.readouterr()
        try:
            code = main(["--profile", "mac-app", *argv])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        return code, [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    folder = home / "My Project Files"
    folder.mkdir()
    for path, alias in ((str(folder), "my-project-files"), ("-notes", "notes"), ("--json", "json")):
        argv = ["--suggest-alias=" + path] if path.startswith("-") else ["--suggest-alias", path]
        answer = {"v": 1, "type": "alias_suggestion", "alias": alias}
        assert run("--json", "roots", "add", *argv) == (0, [answer]), path
    code, objects = run("--json", "roots", "add", "--suggest-alias", "-notes")
    assert code == 2 and [found["code"] for found in objects] == ["usage"]


def test_the_menu_reads_ops_roots_and_the_version_as_json() -> None:
    # The menu reads its version, its abilities and its folders from `--json` (spec/CLI.md
    # section 7), as it reads everything else, and no longer parses the text those verbs print
    # (section 2: "A shell never parses human output").
    text = _swift("HostText.swift")
    model = _swift("HostModel.swift")
    reload = _swift_function(model, "    private func reload() async {")
    for verb in (
        '["--json", "--version"]',
        '["--json", "ops", "list"]',
        '["--json", "roots", "list"]',
    ):
        assert f"await run({verb})" in reload, verb
    for read in (".hostVersion", ".abilities", ".folders"):
        assert read in reload, read
    for gone in ("fromVersionOutput", "fromList", "func pairs(", "result.stderr.contains"):
        assert gone not in text, gone
    # No Swift file, the self-test included, runs one of these verbs without --json first.
    text_verbs = re.compile(r'\[\s*"(?:ops|roots|--version)"')
    for path in [*SWIFT_FILES, _SELF_TEST]:
        found = text_verbs.findall(path.read_text(encoding="utf-8"))
        assert not found, (path.name, found)
    # A home folder is asked about when the host refuses `broad_folder`, a code, never when its
    # words name the flag.
    confirm = _swift_function(
        text, "    static func needsHomeConfirmation(_ result: CLIResult) -> Bool {"
    )
    assert "if case .refused(let error) = VerbOutcome(result) {" in confirm
    assert 'return error.code == "broad_folder"' in confirm
    assert "stderr" not in confirm


def test_a_switch_follows_the_hosts_answer() -> None:
    # spec/CLI.md section 7: "`ops enable` and `ops disable` write the operation's `op` object as
    # it is once the change is made, so a shell sets its switch from the host's answer, not from
    # what it asked for." The switch's position is the host's `enabled`.
    text = _swift("HostText.swift")
    model = _swift("HostModel.swift")
    assert "    var isOn: Bool { enabled }\n" in text
    turn = _swift_function(model, "    private func turn(_ ability: Ability, on: Bool) {")
    assert '["--json", "ops", on ? "enable" : "disable", ability.name]' in turn
    assert "self?.answered(VerbOutcome(result))" in turn
    answered = _swift_function(model, "    private func answered(_ outcome: VerbOutcome) {")
    assert "if let found = outcome.ability {" in answered
    assert "abilities = abilities.map { $0.name == found.name ? found : $0 }" in answered
    assert "} else if let gone = outcome.removedOp {" in answered
    assert "abilities.removeAll { $0.name == gone }" in answered
    assert "on:" not in answered and "isOn" not in answered
    # One place in the app runs `ops enable` and `ops disable`.
    for path in SWIFT_FILES:
        source = path.read_text(encoding="utf-8")
        expected = 1 if path.name == "HostModel.swift" else 0
        assert source.count('"ops", on ? "enable" : "disable"') == expected, path.name
        if path.name != "HostModel.swift":
            assert '"enable"' not in source and '"disable"' not in source, path.name


def test_a_refusal_that_means_done_shows_no_failure() -> None:
    # Turning off an ability that is already off (`not_enabled`) and removing a folder already
    # removed (`no_such_alias`) leave things as the user asked: the menu reads its set-up again and
    # says nothing. Every other refusal still shows its note.
    # The rule is HostText's, in a file the self-test compiles, and decided on the code alone.
    text = _swift("HostText.swift")
    model = _swift("HostModel.swift")
    assert _swift_string_set(text, "alreadyOff") == {"not_enabled"}
    assert _swift_string_set(text, "alreadyRemoved") == {"no_such_alias"}
    done = _swift_function(
        text, "    static func alreadyDone(_ result: CLIResult, codes: Set<String>) -> Bool {"
    )
    assert "if case .refused(let error) = VerbOutcome(result) {" in done
    assert "return codes.contains(error.code)" in done
    assert "stderr" not in done and "message" not in done
    perform = _swift_function(model, "    private func perform(")
    assert "        done: Set<String> = [],\n" in perform
    assert "let alreadyDone = HostText.alreadyDone(result, codes: done)" in perform
    shows = perform.index("showFailure(")
    assert "!alreadyDone" in perform[:shows]
    assert perform.index("await reload()") > shows
    turn = _swift_function(model, "    private func turn(_ ability: Ability, on: Bool) {")
    assert "done: on ? [] : HostText.alreadyOff" in turn
    remove = _swift_function(model, "    func removeFolder(_ folder: Folder) {")
    assert "done: HostText.alreadyRemoved" in remove
    assert '["--json", "roots", "remove", folder.alias]' in remove
    # The self-test runs the rule on the real host's refusals of both.
    selftest = _SELF_TEST.read_text(encoding="utf-8")
    assert "HostText.alreadyDone(off, codes: HostText.alreadyOff)" in selftest
    assert "HostText.alreadyDone(missing, codes: HostText.alreadyRemoved)" in selftest


def test_content_sending_ops_ask_first() -> None:
    # spec/CLI.md section 6: a shell "asks the user to acknowledge that before it enables such an
    # operation, and decides so on this field, never on the words of `sends`" (decisions/0002
    # §13). The question goes through Alerts, with Cancel the default; only its second button
    # turns the ability on. Turning one off, or on one that sends no file contents, asks nothing.
    text = _swift("HostText.swift")
    model = _swift("HostModel.swift")
    chosen = _swift_function(model, "    func setAbility(_ ability: Ability, on: Bool) {")
    assert (
        "        guard ability.asksFirst(turningOn: on) else {\n"
        "            turn(ability, on: on)\n"
        "            return\n"
        "        }\n"
    ) in chosen
    # The rule is Ability's, in a file the self-test compiles: only turning on, only the field.
    assert (
        "    func asksFirst(turningOn on: Bool) -> Bool {\n        on && sendsFileContent\n    }\n"
    ) in text
    assert "sendsFileContent" not in chosen
    assert "let question = HostText.turnOnQuestion(ability)" in chosen
    assert "Alerts.shared.show(" in chosen and "runModal" not in chosen
    cancel = chosen.index("alert.addButton(withTitle: ConnectionText.cancelButton)")
    assert cancel < chosen.index("alert.addButton(withTitle: HostText.turnOnButton)")
    assert (
        "if response == .alertSecondButtonReturn {\n                self?.turn(ability, on: true)\n"
    ) in chosen
    # turn is reached from setAbility alone: its definition and the two calls there.
    assert len(re.findall(r"\bturn\(", model)) == 3
    assert _swift("MenuBarApp.swift").count("model.setAbility(ability, on: $0)") == 1
    # The words: the core's name for the ability, and what Lucille gets, the core's `sends`.
    question = _swift_function(
        text,
        "    static func turnOnQuestion(_ ability: Ability) -> (title: String, text: String) {",
    )
    assert '"Turn on \\u{201C}\\(ability.title)\\u{201D}?"' in question
    assert 'ability.detail ?? ""' in question
    assert 'static let turnOnButton = "I Understand, Turn On"' in text
    # Decided on the field the host writes, never on the words.
    assert "    let sendsFileContent: Bool\n" in text
    for path in SWIFT_FILES:
        source = path.read_text(encoding="utf-8")
        for words in ("sends?.contains", "sends.contains", "detail?.contains", "detail.contains"):
            assert words not in source, (path.name, words)
    # The host's fixture that stubs an entry to send file contents is the one the self-test reads.
    stub = json.loads(
        (check_app.CLI_FIXTURES / "ops-list-sends-file-content.json").read_text(encoding="utf-8")
    )
    assert [op["sends_file_content"] for op in stub["stdout"]] == [False, False, True]
    selftest = _SELF_TEST.read_text(encoding="utf-8")
    assert "ops-list-sends-file-content" in selftest
    assert "let questionOn: [Bool] = content.map { $0.asksFirst(turningOn: true) }" in selftest
    assert "questionOn == [false, false, true] && questionOff == [false, false, false]" in selftest


def test_the_broad_question_follows_the_hosts_kind() -> None:
    # spec/CLI.md section 5: `broad_folder` carries `kind`, `home`, `holds_home` or `root`. When
    # the host refuses a folder the app did not see as broad from its path, such as the home
    # folder by another spelling of it, the question asked is the one for what the host says it
    # is. The whole disk (`root`) is refused before this is asked.
    text = _swift("HostText.swift")
    model = _swift("HostModel.swift")
    assert "| `kind` | `home`, `holds_home` or `root` |" in SPEC_CLI.read_text(encoding="utf-8")
    kind = _swift_function(
        text, "    static func refusedBroadFolder(_ result: CLIResult) -> BroadFolder {"
    )
    assert (
        'if case .refused(let error) = VerbOutcome(result), error.code == "broad_folder",'
        ' error.kind == "home" {\n            return .home\n        }\n        return .holdsHome\n'
    ) in kind
    assert "stderr" not in kind and "message" not in kind
    adds = _swift_function(model, "    private func addRoot(")
    asked = adds.index("let kind = HostText.refusedBroadFolder(result)")
    assert adds.index("if HostText.isWholeDisk(path: path) {") < asked
    confirm = adds.index("guard await RunLoopTurn.ask({ self.confirmBroadFolder(kind) }) else {")
    assert asked < confirm
    assert ".holdsHome" not in adds
    # The host gives `home` for the home folder by another path to it, which the app's own
    # check, a comparison of paths, does not see.
    selftest = _SELF_TEST.read_text(encoding="utf-8")
    assert "HostText.refusedBroadFolder(broad) == .home" in selftest
    assert "HostText.refusedBroadFolder(broadResult) == .home" in selftest


def test_the_broad_kind_the_host_gives_for_another_path_to_the_home_folder(
    tmp_path: Path,
) -> None:
    home = tmp_path / "Users" / "a"
    home.mkdir(parents=True)
    other = tmp_path / "link to a"
    other.symlink_to(home)
    assert policy.broad_kind(str(other), home=str(home)) == "home"
    assert policy.broad_kind(str(home) + "/", home=str(home)) == "home"
    assert policy.broad_kind(str(tmp_path / "Users"), home=str(home)) == "holds_home"


def test_a_name_taken_meanwhile_asks_again() -> None:
    # spec/CLI.md section 7: `roots add --suggest-alias` takes no lock, so another verb may take
    # the alias before `roots add` runs, "which then refuses with `alias_taken`, and the shell asks
    # again". The app opens the name dialog again, with its sentence for the refusal, which names
    # the alias the app asked for, above a name no folder has; no failure note.
    text = _swift("HostText.swift")
    model = _swift("HostModel.swift")
    taken = _swift_function(text, "    static func nameTaken(_ result: CLIResult) -> Bool {")
    assert "if case .refused(let error) = VerbOutcome(result) {" in taken
    assert 'return error.code == "alias_taken"' in taken
    assert "stderr" not in taken and "message" not in taken
    adds = _swift_function(model, "    private func addRoot(")
    assert (
        "    private func addRoot(alias: String, url: URL, place: FolderPlace, confirmedHome: Bool)"
        " async {\n"
    ) in adds
    # The sentence is said with the alias the app asked for, never one read from the host's words.
    worded = adds.index(
        "result, during: .addingFolder(HostText.access(of: path), alias: alias),"
        " earlierRunStuck: stuck"
    )
    again = adds.index("if HostText.nameTaken(result) {")
    failed = adds.index('showFailure(explanation, action: "add the folder")')
    assert adds.rindex("await reload()") < worded < again < failed
    asks = adds[again:failed]
    assert "await nameAndAdd(" in asks and "return" in asks
    assert "taken: Set(folders.map(\\.alias)).union([alias])" in asks
    assert "confirmedHome: confirmed, note: explanation.message" in asks
    # A folder confirmed after the host's refusal stays confirmed when the app asks again.
    confirmed = adds.index("confirmed = true")
    assert adds.index("guard await RunLoopTurn.ask({ self.confirmBroadFolder(kind) })") < confirmed
    assert confirmed < adds.index("confirmedHome: true),")
    # The note is the dialog's, above the name it starts from.
    naming = _swift_function(model, "    private func nameAndAdd(")
    assert "confirmedHome: Bool, note: String? = nil" in naming
    dialog = _swift_function(model, "    private func askForAlias(")
    assert "note first: String? = nil" in dialog and "        var note = first\n" in dialog
    assert "HostText.aliasPrompt(folderName: folderName, note: note, place: place)" in dialog
    assert "HostText.nameTaken(twice)" in _SELF_TEST.read_text(encoding="utf-8")


def test_a_name_this_release_lacks_is_set_aside_by_a_new_pairing() -> None:
    # spec/CLI.md section 6: `enabled` is false for `not_in_catalog`, yet the name is in the local
    # policy, which a new pairing sets aside. The pairing window's note still counts it.
    every = json.loads(
        (check_app.CLI_FIXTURES / "ops-list-every-state.json").read_text(encoding="utf-8")
    )
    lacking = [op for op in every["stdout"] if op["state"] == "not_in_catalog"]
    assert lacking and all(op["enabled"] is False for op in lacking)
    aside = _swift_function(
        _swift("Connection.swift"), "    private func settingsWouldBeSetAside() -> Bool {"
    )
    assert "!model.folders.isEmpty" in aside
    assert (
        "model.abilities.contains { ($0.isOn || $0.state == .notInCatalog)"
        ' && $0.name != "host.describe" }'
    ) in aside


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
        # The app's one opener (SystemPlaces.swift), which macos_lint keeps to one call of a closed
        # list of places (decisions/0002 §20).
        "_OBJC_CLASS_$_NSWorkspace",
    ]
    refused = [
        "_posix_spawn",
        "_posix_spawnp",
        "_socket",
        "_connect",
        "_nw_connection_create",
        "_OBJC_CLASS_$_NSURLSession",
        # Launch Services' and the Apple Event Manager's C calls, which open, register or drive
        # another application without the opener.
        "_LSOpenCFURLRef",
        "_LSOpenFromURLSpec",
        "_LSSetDefaultHandlerForURLScheme",
        "_LSSetDefaultRoleHandlerForContentType",
        "_LSRegisterURL",
        "_AESendMessage",
        # ... and the classes that send an Apple event or hand what they share to another
        # application, which the lint refuses by name too.
        "_OBJC_CLASS_$_NSAppleEventDescriptor",
        "_OBJC_CLASS_$_NSSharingService",
        "_OBJC_CLASS_$_NSSharingServicePicker",
        "_$s10Foundation10URLSessionC6sharedACvgZ",
        "_$s10Foundation4DataV10contentsOf7optionsAcA3URLV_So20NSDataReadingOptionsVtKcfC",
        "_OBJC_CLASS_$_NSPasteboard",
    ]
    assert check_app.forbidden_imports(allowed + refused) == refused
    assert "_OBJC_CLASS_$_NSWorkspace" not in check_app.FORBIDDEN_IMPORT_PREFIXES
    assert {
        "_LSOpen",
        "_LSSetDefault",
        "_LSRegister",
        "_AESend",
        "_OBJC_CLASS_$_NSAppleEventDescriptor",
        "_OBJC_CLASS_$_NSSharingService",
    } <= set(check_app.FORBIDDEN_IMPORT_PREFIXES)


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
        self.keywords: list[dict[str, object]] = []

    def __call__(self, argv: list[str], **keywords: object) -> object:
        import subprocess

        self.calls.append(argv)
        self.keywords.append(keywords)
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
    assert waits == list(build_app.HDIUTIL_WAITS)


def test_hdiutil_waits_two_minutes_in_all_and_says_each_wait_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On 2026-11-08 a macos-15 runner still refused `hdiutil verify` of a disk image it had just
    # made after 5, 10 and 15 s of waits. The waits now grow and add up to two minutes, one try
    # after each; and each wait is said, flushed, before it starts, so the job log's times for it
    # are true.
    assert build_app.HDIUTIL_WAITS == (5, 10, 15, 20, 30, 40)
    assert sum(build_app.HDIUTIL_WAITS) == 120
    assert build_app.HDIUTIL_TRIES == len(build_app.HDIUTIL_WAITS) + 1
    busy = build_app.HDIUTIL_BUSY[1]
    fake = _Hdiutil([(1, busy), (0, "")])
    monkeypatch.setattr("subprocess.run", fake)
    said: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        "builtins.print",
        lambda *parts, flush=False, **_: said.append((" ".join(map(str, parts)), flush)),
    )
    build_app.hdiutil(["verify", "x.dmg"], sleep=lambda seconds: None)
    told = [(line, flushed) for line, flushed in said if line.startswith("hdiutil is busy")]
    assert told == [(f"hdiutil is busy; trying again in 5 s (try 1 of 7): {busy}", True)]


def test_every_hdiutil_call_goes_through_the_retry() -> None:
    for script in ("build_app.py", "check_app.py"):
        source = (build_app.TOOLS_DIR / script).read_text(encoding="utf-8")
        assert '["hdiutil"' not in source.replace('argv = ["hdiutil", *args]', ""), script


def test_hdiutil_takes_waits_and_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    busy = build_app.HDIUTIL_BUSY[1]
    waits: list[float] = []
    fake = _Hdiutil([(1, busy)])
    monkeypatch.setattr("subprocess.run", fake)
    done = build_app.hdiutil(["verify", "x.dmg"], waits=(), sleep=waits.append)
    assert done.returncode == 1 and len(fake.calls) == 1 and waits == []
    said: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *parts, **_: said.append(" ".join(parts)))
    fake = _Hdiutil([(1, busy), (1, busy)])
    monkeypatch.setattr("subprocess.run", fake)
    done = build_app.hdiutil(["verify", "x.dmg"], waits=(1,), sleep=waits.append)
    assert done.returncode == 1 and len(fake.calls) == 2 and waits == [1]
    assert [line for line in said if line.startswith("hdiutil is busy")] == [
        f"hdiutil is busy; trying again in 1 s (try 1 of 2): {busy}"
    ]
    fake = _Hdiutil([(0, "")])
    monkeypatch.setattr("subprocess.run", fake)
    build_app.hdiutil(["info"], timeout=7)
    assert fake.keywords[0]["timeout"] == 7


# ---------------------------------------------------------------------------------------------
# The disk image. make_dmg hands on a verified copy of the file `hdiutil create` wrote, never that
# file. In 3 of 81 CI builds (2026-10-29 to 2026-11-09), hdiutil refused that file ("Resource
# temporarily unavailable") on every try the job made. The logs show a diskimages-helper still
# alive at the job's cleanup; that it was alive during the tries, and that the job's create started
# it, is inferred. That the helper held a lock on that one file, which a copy does not carry, is
# inferred, not seen. These tests hold files under real flock(2) locks, from
# real processes: on Linux against stand-ins for ditto and hdiutil, and on a Mac against both.

_HOLD_PROGRAM = """\
import fcntl, sys, time
handle = open(sys.argv[1], "rb")
how = {"ex": fcntl.LOCK_EX, "sh": fcntl.LOCK_SH}[sys.argv[2]]
try:
    fcntl.flock(handle.fileno(), how | (fcntl.LOCK_NB if sys.argv[4] == "now" else 0))
except BlockingIOError:
    open(sys.argv[3], "w").write("refused")
    sys.exit(0)
open(sys.argv[3], "w").write("held")
time.sleep(600)
"""


class _Holders:
    """Processes that each hold a file open under a flock(2) lock, ``"ex"`` or ``"sh"``, until the
    test ends. Never hold a file a second time while a holder has it ``"ex"``: the second holder
    would wait for ever."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.started: list[subprocess.Popen[bytes]] = []

    def hold(self, path: Path, mode: str, *, at_once: bool = False) -> subprocess.Popen[bytes]:
        """Start a holder of ``path`` and return it once it holds its lock. With ``at_once``, a
        lock another process already holds is not waited for: the holder then exits, and
        :meth:`refused` says so."""
        ready = self.folder / f"ready-{len(self.started)}"
        when = "now" if at_once else "wait"
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", _HOLD_PROGRAM, str(path), mode, str(ready), when],
            stdin=subprocess.DEVNULL,
        )
        self.started.append(process)
        deadline = time.monotonic() + 30
        while not self._said(process):
            ended = process.poll()
            assert ended is None or self._said(process), f"the holder of {path} ended ({ended})"
            assert time.monotonic() < deadline, f"the holder of {path} took no lock in 30 s"
            time.sleep(0.01)
        return process

    def _said(self, process: subprocess.Popen[bytes]) -> str:
        ready = self.folder / f"ready-{self.started.index(process)}"
        return ready.read_text(encoding="utf-8") if ready.exists() else ""

    def refused(self, process: subprocess.Popen[bytes]) -> bool:
        return self._said(process) == "refused"

    def all_alive(self) -> bool:
        return all(process.poll() is None for process in self.started)

    def stop(self) -> None:
        for process in self.started:
            if process.poll() is None:
                process.kill()
            process.wait()


@pytest.fixture
def holders(tmp_path: Path) -> Iterator[_Holders]:
    folder = tmp_path / "holders"
    folder.mkdir()
    held = _Holders(folder)
    try:
        yield held
    finally:
        held.stop()


def _koly(
    *, data: tuple[int, int], xml: tuple[int, int], header: int = 512, magic: bytes = b"koly"
) -> bytearray:
    """A 512-byte UDIF trailer that puts the data fork and the property list where it says."""
    trailer = bytearray(512)
    trailer[0:4] = magic
    struct.pack_into(">II", trailer, 4, 4, header)
    struct.pack_into(">QQ", trailer, 0x18, *data)
    struct.pack_into(">QQ", trailer, 0xD8, *xml)
    return trailer


def _udif(content: bytes, *, good: bool = True) -> bytes:
    """A stand-in for a UDIF disk image: a zlib data fork, a property list, and a trailer that
    says where both lie, with the stand-in's own CRC32 of the data fork at 0x168 (wrong when
    ``good`` is false). Its bytes depend on ``content`` alone."""
    data = zlib.compress(content)
    plist = b'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict/></plist>\n'
    trailer = _koly(data=(0, len(data)), xml=(len(data), len(plist)))
    struct.pack_into(">I", trailer, 0x168, zlib.crc32(data) ^ (0 if good else 1))
    return data + plist + bytes(trailer)


_BUSY_STDERR = (
    'hdiutil: verify: unable to recognize "{path}" as a disk image. '
    "(Resource temporarily unavailable)\n"
    "hdiutil: verify failed - Resource temporarily unavailable\n"
)
_FAKE_HELPER = "/System/Library/PrivateFrameworks/DiskImages2.framework/diskimages-helper"
# What the stand-in ps knows: a disk-image helper, and a shell whose arguments name disk images
# and set a made-up variable. Only the helper's command line may reach the log.
_EXECUTABLES = {"4242": _FAKE_HELPER, "777": "/bin/bash"}
_PROCESSES = {
    "4242": f"4242     1 runner Mon Nov  9 02:01:01 2026 02:10 Ss {_FAKE_HELPER}",
    "777": "777     1 runner Mon Nov  9 02:00:00 2026 03:11 S /bin/bash -c "
    "export LUCILLE_EXAMPLE=not-a-secret; ls diskimages",
}
_LOG_LINES = 100
_LOG_SHOW = [
    "log",
    "show",
    "--last",
    "3m",
    "--style",
    "compact",
    "--info",
    "--debug",
    "--predicate",
    'subsystem == "com.apple.diskimages"',
]


class _DiskTools:
    """A stand-in for subprocess.run, for make_dmg and what it runs. ``ditto`` copies. ``hdiutil
    create`` writes a stand-in disk image (:func:`_udif`) over its output path, as ``-ov`` may,
    keeping its inode, and then as ``plan`` says: ``"hold"`` leaves a process holding it under
    an exclusive flock(2) lock, ``"short"`` writes it with no trailer, ``"damaged"`` with a wrong
    checksum. ``hdiutil verify`` refuses, with the words CI saw, a file another process holds
    under a flock(2) lock (as DiskImages is thought to; inferred) and, with ``"refuse-all"``,
    every file; it fails at once, but not as busy, on a damaged one. ``ps``, ``lsof``, ``sudo -n
    lsof``, ``hdiutil info``, ``hdiutil verify -debug`` and ``log show`` give canned answers, or
    fail as ``broken`` says (``"missing"``, ``"hangs"``, ``"fails"``). Anything else fails the
    test."""

    def __init__(
        self, holders: _Holders, plan: str = "plain", broken: Mapping[str, str] | None = None
    ) -> None:
        import fcntl

        self.holders = holders
        self.plan = plan
        self.broken = dict(broken or {})
        self.calls: list[list[str]] = []
        self.timeouts: list[object] = []
        self.created: list[str] = []  # the sha256 of each image create wrote
        self.verified: list[tuple[Path, int, str]] = []  # (path, inode, sha256) of each pass
        self._flock = fcntl.flock  # kept, so that a test may break fcntl.flock for the build
        self._shared = fcntl.LOCK_SH | fcntl.LOCK_NB

    @staticmethod
    def tool(argv: Sequence[str]) -> str:
        match list(argv):
            case ["ditto", _, _]:
                return "ditto"
            case ["hdiutil", "create", *_]:
                return "create"
            case ["hdiutil", "verify", "-debug", _]:
                return "verify-debug"
            case ["hdiutil", "verify", _]:
                return "verify"
            case ["hdiutil", "info"]:
                return "info"
            case ["ps", "-axo", "pid=,comm="]:
                return "ps-list"
            case ["ps", "-o", _, "-p", _]:
                return "ps-show"
            case ["ps", *_]:
                return "ps-all"
            case ["lsof", "--", _]:
                return "lsof"
            case ["sudo", "-n", "lsof", "--", _]:
                return "sudo"
            case ["log", "show", *_]:
                return "log"
        raise AssertionError(f"the stand-in runs no {argv}")

    def _locked(self, path: Path) -> bool:
        with path.open("rb") as handle:
            try:
                self._flock(handle.fileno(), self._shared)
            except BlockingIOError:
                return True
        return False

    def __call__(self, argv: Sequence[str], **keywords: Any) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        self.timeouts.append(keywords.get("timeout"))
        tool = self.tool(argv)
        failure = self.broken.get(tool)
        if failure == "missing":
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", argv[0])
        if failure == "hangs":
            raise subprocess.TimeoutExpired(argv, keywords.get("timeout") or 0)
        if failure == "fails":
            return self._done(argv, 1, "", f"{argv[0]}: the stand-in fails\n")
        path = Path(argv[-1])
        if tool == "ditto":
            shutil.copytree(argv[1], argv[2], symlinks=True)
            return self._done(argv, 0, "", "")
        if tool == "create":
            return self._create(argv, path)
        if tool == "verify":
            return self._verify(argv, path)
        if tool == "verify-debug":
            if self.plan == "refuse-all" or self._locked(path):
                said = "DIBackingStoreOpenLockFriendly: open with lock returned 35\n"
                return self._done(argv, 1, "", "DIDiskImage: probing\n" + said + _BUSY_STDERR)
            return self._done(argv, 0, "DIDiskImage: probing\nverified   CRC32 $00000000\n", "")
        if tool == "info":
            return self._done(argv, 0, "framework       : 671\ndriver          : 671\n", "")
        if tool == "ps-list":
            listed = [f"{pid:>5} {executable}" for pid, executable in _EXECUTABLES.items()]
            return self._done(argv, 0, "\n".join(listed) + "\n", "")
        header = "  PID  PPID USER     STARTED                      ELAPSED STAT COMMAND"
        if tool == "ps-show":
            shown = [_PROCESSES[pid] for pid in argv[-1].split(",") if pid in _PROCESSES]
            return self._done(argv, 0, "\n".join([header, *shown]) + "\n", "")
        if tool == "ps-all":
            return self._done(argv, 0, "\n".join([header, *_PROCESSES.values()]) + "\n", "")
        if tool in ("lsof", "sudo"):
            said = f"COMMAND   PID   USER  FD  TYPE NAME\npython3  4343 runner  3r  REG {path}\n"
            return self._done(argv, 0, said, "")
        lines = [f"2026-11-09 02:01:01 diskimages line {n}" for n in range(_LOG_LINES)]
        return self._done(argv, 0, "\n".join(lines) + "\n", "")

    def _create(self, argv: list[str], out: Path) -> subprocess.CompletedProcess[str]:
        folder = Path(argv[argv.index("-srcfolder") + 1])
        names = "\n".join(sorted(path.name for path in folder.iterdir())).encode()
        # 8 KiB that zlib cannot shrink, so that the image is longer than its trailer.
        content = b"".join(hashlib.sha256(names + bytes([n])).digest() for n in range(256))
        image = _udif(content, good=self.plan != "damaged")
        if self.plan == "short":
            image = image[:-512]
        with out.open("wb") as handle:
            handle.write(image)
        self.created.append(hashlib.sha256(image).hexdigest())
        if self.plan == "hold":
            self.holders.hold(out, "ex")
        return self._done(argv, 0, f"created: {out}\n", "")

    def _verify(self, argv: list[str], path: Path) -> subprocess.CompletedProcess[str]:
        if not path.exists():
            return self._done(argv, 1, "", "hdiutil: verify failed - No such file or directory\n")
        if self.plan == "refuse-all" or self._locked(path):
            return self._done(argv, 1, "", _BUSY_STDERR.format(path=path))
        raw = path.read_bytes()
        trailer = raw[-512:]
        start, length = struct.unpack(">QQ", trailer[0x18:0x28])
        if trailer[:4] != b"koly" or struct.unpack(">I", trailer[0x168:0x16C])[0] != zlib.crc32(
            raw[start : start + length]
        ):
            return self._done(argv, 1, "", "hdiutil: verify failed - the stand-in's CRC differs\n")
        self.verified.append((path, path.stat().st_ino, hashlib.sha256(raw).hexdigest()))
        return self._done(argv, 0, "verified   CRC32 $C878A96B\n", "")

    @staticmethod
    def _done(argv: list[str], code: int, out: str, err: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr=err)


def _no_waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make every hdiutil call's waits entries of the list returned, made at once. make_dmg looks
    hdiutil up as a module global, so this reaches its calls whatever they pass."""
    waits: list[float] = []
    real = build_app.hdiutil
    monkeypatch.setattr(
        "tools.macos.build_app.hdiutil",
        lambda args, **given: real(args, **{**given, "sleep": waits.append}),
    )
    return waits


@dataclasses.dataclass
class _Disk:
    app: Path
    out: Path
    dmg: Path
    tools: _DiskTools
    waits: list[float]

    @property
    def work(self) -> Path:
        return self.out / "work"

    @property
    def made(self) -> Path:
        return self.work / "made.dmg"

    @property
    def copy(self) -> Path:
        return self.work / "copy.dmg"

    def make(self) -> None:
        build_app.make_dmg(self.app, self.dmg, "Capsule", self.work)

    def images(self) -> list[Path]:
        return build_app.disk_images(self.out, "Capsule")

    def verifies(self) -> list[Path]:
        """The paths of every `hdiutil verify` but the diagnostics' `verify -debug`."""
        return [Path(c[-1]) for c in self.tools.calls if self.tools.tool(c) == "verify"]

    def looks(self) -> list[list[str]]:
        """The diagnostics' calls, in order."""
        kept = ("ps-list", "ps-show", "ps-all", "lsof", "sudo", "info", "verify-debug", "log")
        return [call for call in self.tools.calls if self.tools.tool(call) in kept]


def _app(tmp_path: Path) -> tuple[Path, Path, Path]:
    """An app to put in a disk image, the build's out folder, and the disk image's path."""
    app = tmp_path / "Capsule.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(b"<plist/>\n")
    out = tmp_path / "out"
    return app, out, out / build_app.dmg_name("Capsule", "0.1.0", "abcdef123456")


def _disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    holders: _Holders,
    plan: str = "plain",
) -> _Disk:
    app, out, dmg = _app(tmp_path)
    tools = _DiskTools(holders, plan)
    monkeypatch.setattr("subprocess.run", tools)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return _Disk(app, out, dmg, tools, _no_waits(monkeypatch))


def _looks_at(image: Path, *, sudo: bool = False) -> list[list[str]]:
    """What show_image_holders runs about ``image``, in order, given _DiskTools' processes."""
    lsof = [["lsof", "--", str(image)]]
    if sudo:
        lsof.append(["sudo", "-n", "lsof", "--", str(image)])
    return [
        ["ps", "-axo", "pid=,comm="],
        ["ps", "-o", "pid,ppid,user,lstart,etime,stat,command", "-p", "4242"],
        *lsof,
        ["hdiutil", "info"],
        ["hdiutil", "verify", "-debug", str(image)],
        _LOG_SHOW,
    ]


def test_make_dmg_creates_in_work_and_hands_on_the_copy_it_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    holders: _Holders,
    capsys: pytest.CaptureFixture[str],
) -> None:
    disk = _disk(tmp_path, monkeypatch, holders)
    disk.make()
    staging = disk.work / "dmg"
    assert disk.tools.calls == [
        ["ditto", str(disk.app), str(staging / "Capsule.app")],
        ["hdiutil", "create", "-volname", "Capsule", "-srcfolder", str(staging)]
        + ["-format", "UDZO", "-ov", str(disk.made)],
        ["hdiutil", "verify", str(disk.copy)],
    ]
    # The image is made beside the folder it is made from, never inside it.
    assert staging not in disk.made.parents and disk.made.parent == disk.work
    assert disk.waits == []
    assert disk.images() == [disk.dmg]
    [(path, inode, digest)] = disk.tools.verified
    assert path == disk.copy
    assert disk.dmg.stat().st_ino == inode and build_app.sha256_of(disk.dmg) == digest
    assert disk.tools.created == [digest]
    assert not disk.made.exists() and not disk.copy.exists()
    out = capsys.readouterr().out
    assert "after hdiutil create: made.dmg has no lock on it\n" in out
    assert f"verified a copy of made.dmg; it is now {disk.dmg.name}\n" in out


def test_the_file_create_leaves_held_is_never_verified_or_handed_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    holders: _Holders,
    capsys: pytest.CaptureFixture[str],
) -> None:
    disk = _disk(tmp_path, monkeypatch, holders, "hold")
    disk.make()
    assert disk.verifies() == [disk.copy]
    assert disk.waits == []
    # The holder lives on, and the file it holds was unlinked under it.
    assert len(holders.started) == 1 and holders.all_alive()
    assert not disk.made.exists()
    assert disk.images() == [disk.dmg]
    assert [build_app.sha256_of(disk.dmg)] == disk.tools.created
    assert disk.looks() == _looks_at(disk.made)
    out = capsys.readouterr().out
    assert (
        "after hdiutil create: made.dmg is locked by another process; this build verifies and "
        "hands on a copy of it instead\n"
    ) in out
    assert "OpenLockFriendly" in out  # verify -debug's lock line reaches the log


@pytest.mark.parametrize("github_actions", ["true", "false", None])
def test_sudo_lsof_runs_only_on_github_actions(
    github_actions: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    holders: _Holders,
) -> None:
    disk = _disk(tmp_path, monkeypatch, holders, "hold")
    if github_actions is not None:
        monkeypatch.setenv("GITHUB_ACTIONS", github_actions)
    disk.make()
    assert disk.looks() == _looks_at(disk.made, sudo=github_actions == "true")


def test_a_file_without_a_whole_udif_trailer_is_refused_before_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, holders: _Holders
) -> None:
    disk = _disk(tmp_path, monkeypatch, holders, "short")
    with pytest.raises(build_app.BuildError, match="made no whole disk image: .*UDIF trailer"):
        disk.make()
    assert disk.verifies() == []
    assert disk.images() == []
    assert not disk.copy.exists() and disk.made.exists()


def test_a_file_that_changes_while_it_is_copied_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, holders: _Holders
) -> None:
    disk = _disk(tmp_path, monkeypatch, holders)
    real = shutil.copyfileobj

    def copy_then_write(source: Any, target: Any, *rest: Any) -> None:
        real(source, target, *rest)
        if str(getattr(source, "name", "")).endswith("made.dmg"):
            with disk.made.open("ab") as handle:
                handle.write(b"\0")

    monkeypatch.setattr("shutil.copyfileobj", copy_then_write)
    with pytest.raises(build_app.BuildError, match="changed while it was copied"):
        disk.make()
    assert disk.verifies() == []
    assert disk.images() == []
    assert not disk.copy.exists()


def test_a_refusal_of_every_file_fails_after_todays_waits_with_nothing_handed_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, holders: _Holders
) -> None:
    disk = _disk(tmp_path, monkeypatch, holders, "refuse-all")
    with pytest.raises(
        build_app.BuildError,
        match=r"^hdiutil verify failed: .*\(Resource temporarily unavailable\)",
    ):
        disk.make()
    assert disk.verifies() == [disk.copy] * build_app.HDIUTIL_TRIES
    assert disk.waits == list(build_app.HDIUTIL_WAITS)
    assert disk.looks() == _looks_at(disk.copy)
    assert disk.images() == []
    assert not disk.copy.exists()


def test_a_damaged_image_fails_at_once_with_nothing_handed_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, holders: _Holders
) -> None:
    disk = _disk(tmp_path, monkeypatch, holders, "damaged")
    with pytest.raises(build_app.BuildError, match="^hdiutil verify failed: .*CRC differs"):
        disk.make()
    assert disk.verifies() == [disk.copy]
    assert disk.waits == [] and disk.looks() == []
    assert disk.images() == []
    assert not disk.copy.exists()


def test_a_copy_that_fails_or_is_interrupted_leaves_no_copy_and_no_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, holders: _Holders
) -> None:
    # A disk that fills while the copy is written, a create that exits 0 without writing its
    # file, and Control-C during the waits after the copy's verify is refused as busy: none leaves
    # copy.dmg or a disk image. The first two end as a BuildError, which main() says in one line.
    unpatched = build_app.hdiutil  # before _disk wraps it
    disk = _disk(tmp_path / "full", monkeypatch, holders)
    real = shutil.copyfileobj

    def fill_the_disk(source: Any, target: Any, *rest: Any) -> None:
        if not str(getattr(source, "name", "")).endswith("made.dmg"):
            return real(source, target, *rest)
        target.write(source.read(100))
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("shutil.copyfileobj", fill_the_disk)
    with pytest.raises(build_app.BuildError, match=r"^could not copy .*No space left on device"):
        disk.make()
    assert disk.verifies() == [] and disk.images() == [] and not disk.copy.exists()
    monkeypatch.setattr("shutil.copyfileobj", real)

    disk = _disk(tmp_path / "unwritten", monkeypatch, holders)
    monkeypatch.setattr(disk.tools, "_create", lambda argv, _: disk.tools._done(argv, 0, "", ""))
    with pytest.raises(build_app.BuildError, match=r"^could not copy .*No such file"):
        disk.make()
    assert disk.verifies() == [] and disk.images() == [] and not disk.copy.exists()

    disk = _disk(tmp_path / "interrupted", monkeypatch, holders, "refuse-all")

    def control_c(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "tools.macos.build_app.hdiutil",
        lambda args, **given: unpatched(args, **{**given, "sleep": control_c}),
    )
    with pytest.raises(KeyboardInterrupt):
        disk.make()
    assert disk.verifies() == [disk.copy]
    assert disk.images() == [] and not disk.copy.exists()


@pytest.mark.parametrize("github_actions", ["true", "false", None])
@pytest.mark.parametrize("plan", ["plain", "hold", "refuse-all"])
def test_a_held_file_or_a_refused_copy_is_a_warning_on_github_actions(
    plan: str,
    github_actions: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    holders: _Holders,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A build the copy rescued is green, and the line that says so sits in a log of about 5,000
    # lines. On GitHub Actions it is also a warning annotation, which the run's page shows.
    disk = _disk(tmp_path, monkeypatch, holders, plan)
    if github_actions is not None:
        monkeypatch.setenv("GITHUB_ACTIONS", github_actions)
    if plan == "refuse-all":
        with pytest.raises(build_app.BuildError, match="^hdiutil verify failed"):
            disk.make()
    else:
        disk.make()
    annotations = [line for line in capsys.readouterr().out.splitlines() if line.startswith("::")]
    starts = {
        "plain": [],
        "hold": ["::warning title=disk image::made.dmg was locked by another process "],
        "refuse-all": ["::warning title=disk image::hdiutil verify refused copy.dmg, "],
    }[plan]
    if github_actions != "true":
        starts = []
    assert len(annotations) == len(starts), annotations
    for line, start in zip(annotations, starts, strict=True):
        assert line.startswith(start) and "%" not in line, line


def test_only_the_lines_of_verify_debug_that_name_a_lock_are_shown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    kept = [
        "DIBackingStoreOpenLockFriendly: open with lock returned 35",
        "flock(2) failed",
        "unlock",
        "LOCKED",
    ]
    dropped = ["read block 12", "clock skew", "blocked on I/O", "BlockingIOError"]
    last = "hdiutil: verify failed - Resource temporarily unavailable"
    said = "\n".join([*kept, *dropped]) + "\n"
    build_app._show(subprocess.CompletedProcess([], 1, stdout=said, stderr=last), only_locks=True)
    assert capsys.readouterr().out.splitlines() == [*kept, last]


def test_stale_held_files_are_replaced_by_new_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, holders: _Holders
) -> None:
    # A file opened "wb" over a path keeps its inode, and so any lock on it (seen on Linux); each
    # file make_dmg writes must be a new one.
    disk = _disk(tmp_path, monkeypatch, holders)
    disk.work.mkdir(parents=True)
    for stale in (disk.made, disk.copy, disk.dmg):
        stale.write_bytes(b"left by an earlier build")
        holders.hold(stale, "ex")
    stale_copy = disk.copy.stat().st_ino
    disk.make()
    [(path, inode, _)] = disk.tools.verified
    assert path == disk.copy and inode != stale_copy
    assert disk.waits == []
    assert holders.all_alive()
    assert disk.images() == [disk.dmg] and disk.dmg.stat().st_ino == inode


def test_image_lock_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    holders: _Holders,
    capsys: pytest.CaptureFixture[str],
) -> None:
    image = tmp_path / "image.dmg"
    assert isinstance(build_app.image_lock_held(image), FileNotFoundError)
    image.write_bytes(b"image")
    assert build_app.image_lock_held(image) is False
    holders.hold(image, "ex")
    assert build_app.image_lock_held(image) is True
    # A shared holder too: a probe that asked for a shared lock would be granted one, and blind.
    shared = tmp_path / "shared.dmg"
    shared.write_bytes(b"image")
    holders.hold(shared, "sh")
    assert build_app.image_lock_held(shared) is True
    # A probe that cannot be made says so, and the build goes on.
    disk = _disk(tmp_path, monkeypatch, holders)

    def no_locks(fd: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr("fcntl.flock", no_locks)
    error = build_app.image_lock_held(shared)
    assert isinstance(error, OSError) and error.errno == errno.ENOLCK
    capsys.readouterr()
    disk.make()
    assert disk.images() == [disk.dmg]
    out = capsys.readouterr().out
    assert "after hdiutil create: made.dmg could not be checked for a lock: " in out
    assert "No locks available" in out


def _sparse(path: Path, size: int, trailer: bytes) -> Path:
    with path.open("wb") as handle:
        handle.truncate(size)
        handle.seek(size - len(trailer))
        handle.write(trailer)
    return path


def test_udif_trailer_problem(tmp_path: Path) -> None:
    # The values of the trailer of a real image, built by CI (job 128888004295): 43,611,369 bytes,
    # the data fork at 0 and 43,597,364 bytes long, the property list right after it, 13,493 bytes.
    size, data, xml = 43_611_369, (0, 43_597_364), (43_597_364, 13_493)
    real = _sparse(tmp_path / "real.dmg", size, bytes(_koly(data=data, xml=xml)))
    assert build_app.udif_trailer_problem(real) is None
    short = tmp_path / "short.dmg"
    short.write_bytes(b"k" * 511)
    end = size - 512
    cases = {
        "too short": short,
        "do not start": _koly(data=data, xml=xml, magic=b"kolx"),
        "says it is 511 bytes": _koly(data=data, xml=xml, header=511),
        "data fork": _koly(data=(0, end + 1), xml=xml),
        "property list (13494 bytes": _koly(data=data, xml=(xml[0], xml[1] + 1)),
        "no length": _koly(data=data, xml=(end, 0)),
    }
    for words, case in cases.items():
        path = case if isinstance(case, Path) else _sparse(tmp_path / "x.dmg", size, bytes(case))
        problem = build_app.udif_trailer_problem(path)
        assert problem is not None and words in problem, (words, problem)


def test_show_image_holders_prints_and_never_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    holders: _Holders,
    capsys: pytest.CaptureFixture[str],
) -> None:
    image = tmp_path / "made.dmg"
    image.write_bytes(b"image")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _no_waits(monkeypatch)
    order = [_DiskTools.tool(call) for call in _looks_at(image, sudo=True)]
    # Nothing fails: only the matched process's own command line is shown, and long output is cut.
    tools = _DiskTools(holders)
    monkeypatch.setattr("subprocess.run", tools)
    build_app.show_image_holders(image)
    out = capsys.readouterr().out
    assert [tools.tool(call) for call in tools.calls] == order
    assert _PROCESSES["4242"] in out
    assert "LUCILLE_EXAMPLE" not in out and "/bin/bash" not in out
    cut = _LOG_LINES - build_app.DIAGNOSIS_LINES
    assert f"({cut} lines before these are not shown)\n" in out
    assert f"diskimages line {_LOG_LINES - 1}\n" in out
    assert f"diskimages line {cut - 1}\n" not in out and f"diskimages line {cut}\n" in out
    assert set(tools.timeouts) == {build_app.DIAGNOSIS_TIMEOUT}
    # Each tool in turn cannot start, does not end in time, or fails: one line says so, and every
    # later look still runs.
    for broken in order:
        for failure in ("missing", "hangs", "fails"):
            tools = _DiskTools(holders, broken={broken: failure})
            monkeypatch.setattr("subprocess.run", tools)
            build_app.show_image_holders(image)
            out = capsys.readouterr().out
            skipped = {"ps-show"} if broken == "ps-list" else set()  # no list, no pid to show
            ran = [tools.tool(call) for call in tools.calls]
            assert ran == [tool for tool in order if tool not in skipped], (broken, failure)
            words = {
                "missing": "could not be run",
                "hangs": f"did not end within {build_app.DIAGNOSIS_TIMEOUT} s",
                "fails": "exited 1",
            }[failure]
            assert words in out, (broken, failure, out)


_ON_A_MAC = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("hdiutil") is None,
    reason="needs macOS and its hdiutil",
)


@_ON_A_MAC
def test_make_dmg_hands_on_a_verified_image_while_a_process_holds_the_file_create_wrote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    holders: _Holders,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The real ditto and hdiutil, and a real process holding the file `hdiutil create` wrote under
    # an exclusive flock(2) lock from the moment create returns. The build must hand on a verified
    # image whatever hdiutil makes of a file held so. A pass does not say which that is, and the
    # code before the copy fails this test either way: if DiskImages refuses the file, with the
    # BuildError CI saw after seven refused tries; if it does not, with a TypeError on the `waits`
    # that its hdiutil() did not take. So only the way the old code fails tells the two apart. The
    # next test records, as a warning, what `hdiutil verify` answers for a file held so.
    #
    # The holder asks for its lock without waiting. If something already holds the file (a
    # lingering diskimages-helper, as in CI's three failures), it is refused and exits, and the
    # file is still held by another process, which is what this test needs.
    app, out, dmg = _app(tmp_path)
    work = out / "work"
    waits: list[float] = []
    real = build_app.hdiutil

    def hdiutil_then_hold(args: Sequence[str], **given: Any) -> subprocess.CompletedProcess[str]:
        done = real(args, **{**given, "sleep": waits.append})
        if args[0] == "create" and done.returncode == 0:
            holders.hold(Path(args[-1]), "ex", at_once=True)
        return done

    monkeypatch.setattr("tools.macos.build_app.hdiutil", hdiutil_then_hold)
    shown: list[Path] = []
    monkeypatch.setattr("tools.macos.build_app.show_image_holders", shown.append, raising=False)
    try:
        build_app.make_dmg(app, dmg, "lucille-host-test", work)
    except build_app.BuildError as error:
        if str(error).startswith("hdiutil create failed"):
            pytest.skip(f"hdiutil could not make a disk image here: {error}")
        raise
    assert dmg.is_file() and not (work / "made.dmg").exists()
    [holder] = holders.started
    assert holders.refused(holder) or holder.poll() is None
    assert build_app.hdiutil(["verify", str(dmg)], waits=()).returncode == 0
    assert build_app.udif_trailer_problem(dmg) is None
    assert shown == [work / "made.dmg"]
    assert "is locked by another process" in capsys.readouterr().out


@_ON_A_MAC
def test_show_image_holders_names_the_process_that_holds_a_real_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    holders: _Holders,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("a disk image for a test\n", encoding="utf-8")
    image = tmp_path / "image.dmg"
    create = ["create", "-volname", "lucille-host-test", "-srcfolder", str(source)]
    done = build_app.hdiutil([*create, "-format", "UDZO", "-ov", str(image)])
    if done.returncode != 0:
        pytest.skip(f"hdiutil could not make a disk image here: {done.stderr.strip()}")
    holder = holders.hold(image, "ex", at_once=True)
    if holders.refused(holder):
        pytest.skip(
            "another process held the new disk image under a flock(2) lock right after hdiutil "
            f"create returned: evidence for the reading of CI's refusals ({image})"
        )
    # The control, recorded and not asserted: whether hdiutil refuses a file that another process
    # holds under an exclusive flock(2) lock. This is the one look at that on a real Mac; the
    # pytest run's warnings summary shows it.
    control = build_app.hdiutil(["verify", str(image)], waits=())
    said = (control.stderr.strip() or control.stdout.strip()).splitlines()
    answer = (
        f"hdiutil verify of a file another process holds under flock(LOCK_EX): exit "
        f"{control.returncode}, refused as busy: {build_app.hdiutil_busy(control)}; its last line: "
        f"{said[-1] if said else '(none)'}"
    )
    print(answer)
    warnings.warn(answer, stacklevel=1)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    capsys.readouterr()
    build_app.show_image_holders(image)
    out = capsys.readouterr().out
    lsof = out.split("+ " + shlex.join(["lsof", "--", str(image)]) + "\n", 1)[1]
    lsof = lsof.split("\n+ ", 1)[0]
    assert re.search(rf"\b{holder.pid}\b", lsof), out
    assert "+ sudo" not in out, out


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
        (0, f"found the same bytes in 0 of {_ALL} .pyc files, {TAG}\n"),
        (0, f"found the same bytes in {_ALL} of {_ALL} .pyc files, cpython-0\n"),
        # The program counts no file only with exit 0 and no line before the count.
        (1, f"found the same bytes in 0 of 0 .pyc files, {TAG}\n"),
        (0, f"one\nfound the same bytes in 0 of 0 .pyc files, {TAG}\n"),
    ],
    ids=[
        "crash",
        "no-problem-named",
        "lines-do-not-add-up",
        "silent",
        "not-all",
        "none-matched",
        "another-tag",
        "none-counted-exit-1",
        "none-counted-after-a-line",
    ],
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


_NONE_CHECKED = f"found the same bytes in 0 of 0 .pyc files, {TAG}"
_NONE_SAID = f"the bytecode check counted no .py file: {_NONE_CHECKED}"


@pytest.mark.parametrize(
    "library",
    [{"README.txt": b"no module here\n"}, {}],
    ids=["no-py-in-the-library", "no-library-folder"],
)
def test_a_check_that_counts_no_source_is_a_problem(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], library: Mapping[str, bytes]
) -> None:
    # The program exits 0 on "0 of 0": nothing differed, because there was nothing to compare.
    # With no library folder at all, the layout check finds nothing wrong either. The build's own
    # gate refuses that count, from an app with no .py in its library, as check_app.py bytecode
    # refuses such an app.
    empty = _bytecode_app(tmp_path, library)
    assert _lib(empty).is_dir() == bool(library)
    done = build_app.run_bytecode_program(empty / build_app.RUNTIME_DIR, "check")
    assert (done.returncode, done.stdout) == (0, _NONE_CHECKED + "\n"), done
    assert build_app.bytecode_layout_problems(empty, tag=TAG) == []
    assert build_app.bytecode_problems(empty, tag=TAG) == [_NONE_SAID]
    with pytest.raises(build_app.BuildError, match=re.escape(_NONE_SAID)):
        build_app.compile_bytecode(empty, tag=TAG)
    assert f"wrote 0 of 0 .pyc files, {TAG}" in capsys.readouterr().out.splitlines()


def test_a_check_run_that_counts_no_file_is_a_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An app whose library is there, and a check run that reports it found none of it.
    app = _compiled_app(tmp_path)
    ended = subprocess.CompletedProcess(["program"], 0, _NONE_CHECKED + "\n", "")
    monkeypatch.setattr(build_app, "run_bytecode_program", lambda runtime, mode: ended)
    assert build_app.bytecode_problems(app, tag=TAG) == [_NONE_SAID]
    with pytest.raises(build_app.BuildError, match=re.escape(_NONE_SAID)):
        build_app.require_bytecode(app, tag=TAG)


def test_a_check_that_matches_no_file_names_each_and_one_of_one_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # What is refused above is a count of no .py file, not one where no .pyc matched: with one
    # byte of every body changed, each .pyc is named as it would be alone, and nothing else is.
    # And a library of one module passes.
    app = _compiled_app(tmp_path / "every-body-changed")
    runtime = app / build_app.RUNTIME_DIR
    sources = sorted(_lib(app).rglob("*.py"), key=lambda path: path.relative_to(runtime).as_posix())
    for source in sources:
        data = bytearray(_pyc_of(source).read_bytes())
        data[-1] ^= 1
        _pyc_of(source).write_bytes(bytes(data))
    capsys.readouterr()
    assert build_app.bytecode_problems(app, tag=TAG) == [
        f"{_pyc_of(source).relative_to(runtime).as_posix()} is not what this interpreter "
        f"compiles from {source.relative_to(runtime).as_posix()}"
        for source in sources
    ]
    assert capsys.readouterr().out.splitlines()[-1] == (
        f"found the same bytes in 0 of {_ALL} .pyc files, {TAG}"
    )
    one = _bytecode_app(tmp_path / "one-module", {"mod.py": _LIBRARY["mod.py"]})
    build_app.compile_bytecode(one, tag=TAG)
    assert (
        capsys.readouterr().out.splitlines()[-1]
        == f"found the same bytes in 1 of 1 .pyc files, {TAG}"
    )
    assert build_app.bytecode_problems(one, tag=TAG) == []


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


def test_the_bytecode_check_on_a_copy_refuses_a_check_run_that_counts_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # check_app.py bytecode reads a check run as the build's gate does: one that counts no .py
    # file fails, though the copy's library holds them and the layout and the loads are right.
    monkeypatch.setattr(build_app, "BYTECODE_TAG", TAG)
    app = _bytecode_app(tmp_path / "Downloaded Copy", {**_LIBRARY, **_STUB_HOST})
    stand_in = _BYTECODE_CHECK_RUNTIME.replace("@PYTHON@", shlex.quote(sys.executable))
    stand_in = stand_in.replace("@SITE@", _OWN_SITE).replace("@PREFIX@", "")
    check_app.python_in(app).write_text(stand_in, "utf-8")
    build_app.compile_bytecode(app, tag=TAG)
    ended = subprocess.CompletedProcess(["program"], 0, _NONE_CHECKED + "\n", "")
    monkeypatch.setattr(build_app, "run_bytecode_program", lambda runtime, mode: ended)
    capsys.readouterr()
    checker = check_app.Checker()
    check_app.check_bytecode(app, checker)
    assert len(checker.failures) == 1, checker.failures
    assert checker.failures[0].startswith(_PROGRAM_FAILS), checker.failures
    assert _NONE_SAID in capsys.readouterr().out.splitlines()


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
        # Without the hardened runtime, no option at all: codesign's `library` option would turn
        # library validation on by itself (from memory of codesign's manual, not read here).
        assert argv.count("--options") == int(hardened)


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


# The pairing screen, run supervision, Activity, the placement rule, Set Up and the login item's
# rules: the Foundation-only files they are built from, and the rules those keep. The self-test
# (macos/Tests/SelfTest.swift) checks their behaviour on a Mac.
SPEC_CLI = build_app.REPO_ROOT / "spec" / "CLI.md"
NEW_LOGIC = (
    "HostJSON.swift",
    "Pairing.swift",
    "Supervisor.swift",
    "ConnectionText.swift",
    "RequestLog.swift",
    "Placement.swift",
    "Setup.swift",
    "LoginRules.swift",
    "PlaceRules.swift",
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


# The verbs whose refusals ConnectionText words: pairing, `status`, `forget`, `run`, Stop What's
# Running (`pause` is the row of `pause`, `resume` and `kill`), Activity's log, and Reset Settings.
CONNECTION_VERBS = frozenset(
    {
        "status",
        "forget",
        "pair --code-stdin [--hub URL] [--store-file]",
        "pair status",
        "pair confirm",
        "pair reject",
        "run --json-events [--exit-on-stdin-eof] [--allow-downgrade]",
        "pause",
        "log [--limit N]",
        "policy reset",
    }
)
# The verbs the menu's own set-up runs, whose refusals HostText words: the version, the abilities
# and the folders, with the folder's suggested alias; and Set Up's own, the policy a pairing set
# aside and the check of each folder.
SETTINGS_VERBS = frozenset(
    {
        "--version",
        "ops list",
        "ops enable <op>",
        "ops disable <op>",
        "roots list",
        "roots add [--allow-home-or-root] [--] <alias> <path>",
        "roots add --suggest-alias <path>",
        "roots remove <alias>",
        "roots check [<alias>]",
        "policy previous",
    }
)


def _spec_verb_codes(wanted: frozenset[str] = CONNECTION_VERBS) -> dict[str, set[str]]:
    """The error codes each verb in ``wanted`` can give: its row in spec/CLI.md section 7's table,
    and the codes every verb can give, from the sentence before it. A row is named by the first
    verb in it, as the table writes it: ``pause`` is the row of ``pause``, ``resume`` and
    ``kill``, and ``--version`` the row of ``--version`` and of no verb."""
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
    # Reset Settings takes `not_invalid` as done (spec/CLI.md section 7): there was no invalid
    # file to set aside, and its words say that nothing changed, not that something failed.
    assert refusals["not_invalid"] == (
        "\\(appName)'s settings can be read again, so nothing was changed."
    )


def test_every_error_code_the_menus_own_verbs_give_has_words() -> None:
    # The menu reads its own verbs' refusals by `code` (spec/CLI.md section 10), never by their
    # words: one sentence for each code `--version`, `ops`, `roots` and `policy previous` can give,
    # in HostText.refusals; two that say more while a folder is added, in `refusal`'s own cases;
    # and the codes that can only be the app's mistake, which read as ConnectionText.appBug. Set
    # Up's `policy previous` is the one new code: a policy set aside that the host can't read,
    # which Set Up says in a note and offers nothing back for.
    text = _swift("HostText.swift")
    refusals = _swift_string_table(text, "refusals")
    app_bugs = _swift_string_set(_swift("ConnectionText.swift"), "appBugCodes")
    reader = _function_body(
        text, "    static func refusal(_ error: HostError, during activity: Activity"
    )
    own = set(re.findall(r'case "([a-z_]+)"', reader))
    assert own == {"alias_taken", "not_a_folder"}
    codes = _spec_verb_codes(SETTINGS_VERBS)
    assert codes["--version"] == {
        "usage",
        "internal",
        "bad_profile",
        "unsupported_platform",
        "os_error",
    }
    met = set().union(*codes.values())
    assert met - (set(refusals) | own | app_bugs) == set(), sorted(
        met - (set(refusals) | own | app_bugs)
    )
    assert not set(refusals) & app_bugs
    assert set(refusals) | own <= _spec_error_codes(), sorted(
        set(refusals) | own - _spec_error_codes()
    )
    # Today's sentences, keyed by the code, and previous_invalid's.
    assert refusals == {
        "policy_invalid": "\\(appName)'s settings file can't be read.",
        "policy_lock_failed": (
            "\\(appName) couldn't lock its settings file, so nothing was changed. Try again."
        ),
        "not_in_catalog": "This version of \\(appName) doesn't have that ability.",
        "unavailable": "This Mac lacks a program that ability needs.",
        "not_enabled": "That ability was already off.",
        "bad_alias": "That name can't be used.",
        "too_many_roots": (
            "\\(appName) can use at most \\(maxFolders) folders. Remove one before you add another."
        ),
        "not_a_folder": "That folder is no longer there.",
        "own_folder": "That folder holds \\(appName)'s own settings, so it can't be added.",
        "on_proc": "That folder can't be used.",
        "broad_folder": "That folder is your home folder, holds it, or is your whole disk.",
        "unstorable_path": "That folder's location can't be stored in \\(appName)'s settings.",
        "no_such_alias": "That folder was already removed.",
        "previous_invalid": (
            "\\(appName) can't read the folders and abilities you had before, so it can't offer"
            " them back."
        ),
    }
    # The most folders is the host's (spec/CLI.md section 10, `too_many_roots`), not read from its
    # words.
    assert f"    static let maxFolders = {policy.MAX_ROOTS}\n" in text
    alias_taken = reader.split('case "alias_taken":', 1)[1].split("\n        case ", 1)[0]
    assert 'return "You already have a folder named \\(alias). Choose another name."' in alias_taken
    # The host reads a folder macOS keeps from it as no folder at all: while the app itself can
    # see the folder, or is refused too, macOS is what stands in the way. A file error the host
    # names (`os_error`) is not read as macOS's: its code does not say which file it was.
    blocked = 'case "not_a_folder" where access != .missing:\n                return blockedByMacOS'
    assert blocked in reader
    assert "os_error" not in reader
    assert "return refusals[error.code] ?? ConnectionText.appBug" in reader
    # explain reads the refusal from the `error` object, and offers Reset Settings for its code.
    explain = _function_body(text, "    static func explain(")
    assert "if case .refused(let error) = VerbOutcome(result) {" in explain
    assert "message: refusal(error, during: activity)," in explain
    for gone in ("hasPrefix", "hasSuffix", "contains("):
        assert gone not in explain, gone
    # "explain(" itself holds "plain(": the word-matching helper is looked for as a call of its own.
    assert not re.search(r"\bplain\(", explain)
    assert "private static func plain(" not in text


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
    assert "await RunLoopTurn.ask({ self.confirmBroadFolder(kind) })" in adds
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


def _quote_lookalikes() -> set[int]:
    """Every character whose Unicode name holds QUOTATION MARK or APOSTROPHE, but the straight
    quote and apostrophe (U+0022, U+0027), which cannot end the app's curly quotes; then the primes
    U+2032 to U+2037 and U+2057, and the fullwidth quote and apostrophe, U+FF02 and U+FF07. This
    is the generator RequestText.quoteLookalikes's comment records."""
    import unicodedata

    named = {
        code
        for code in range(0x110000)
        if code not in (0x22, 0x27)
        and any(
            word in unicodedata.name(chr(code), "") for word in ("QUOTATION MARK", "APOSTROPHE")
        )
    }
    return named | set(range(0x2032, 0x2038)) | {0x2057, 0xFF02, 0xFF07}


def test_the_quote_lookalikes_are_every_quotation_mark_apostrophe_and_prime() -> None:
    # The review of #14 (non-blocking note 1): refusing Unicode's opening and closing quotation
    # marks (Pi, Pf) lets through quote look-alikes of other categories, such as U+201E (Ps),
    # U+FF02, U+2033 and U+2036 (Po), so a path like `a.txt„ in documents. Finished on this Mac.`
    # could imitate the app's own sentence inside its quotes. So `quoted` also refuses a fixed
    # list, written out in the Swift source rather than read from the platform's Unicode tables,
    # which this Python's unicodedata must give exactly (Unicode 14.0.0 and 15.1.0, Python 3.11
    # and 3.13, give the same 44 characters). A later Unicode that names more fails here, and the
    # list grows.
    log = _swift("RequestLog.swift")
    body = re.search(r"static let quoteLookalikes: Set<UInt32> = \[(.*?)\]", log, re.DOTALL)
    assert body is not None
    listed = [int(value, 16) for value in re.findall(r"\b0x([0-9A-F]{4,6})\b", body.group(1))]
    assert re.sub(r"\b0x[0-9A-F]{4,6}\b|[\s,]", "", body.group(1)) == "", body.group(1)
    wanted = _quote_lookalikes()
    assert set(listed) == wanted, [f"U+{code:04X}" for code in sorted(set(listed) ^ wanted)]
    assert listed == sorted(listed) and len(listed) == len(set(listed)) == 44
    assert 0x22 not in listed and 0x27 not in listed
    # The comment records where the list came from: the Python, its Unicode, and the generator.
    assert "Python 3.13.14's unicodedata, Unicode 15.1.0" in log
    assert '("QUOTATION MARK", "APOSTROPHE")' in log
    # Both checks hold: the categories, kept, and the list.
    quoted = _function_body(log, "    static func quoted(")
    assert (
        "case .control, .format, .lineSeparator, .paragraphSeparator, .initialPunctuation, "
        ".finalPunctuation:\n                return nil\n"
    ) in quoted
    # The list is tried first, for every scalar, inside the loop that tries the categories.
    assert (
        "for scalar in text.unicodeScalars {\n"
        "            if quoteLookalikes.contains(scalar.value) {\n"
        "                return nil\n"
        "            }\n"
        "            switch scalar.properties.generalCategory {\n"
    ) in quoted
    # Nothing reads the platform's Unicode names for it.
    assert "properties.name" not in log and "nameAlias" not in log


def test_activity_text_cannot_be_selected_or_copied() -> None:
    # Selected text could be copied, a pasteboard write the app does not make yet (Copy
    # Diagnostics, decisions/0002 §15, is later). So nothing in the Activity panel can be
    # selected or edited, and it has no Copy button.
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
    # The pairing window, the progress panel, the Activity panel and the Set Up window.
    assert made == 4
    assert "panel.hidesOnDeactivate = false" in _swift("ActivityPanel.swift")


# --- Reset Settings (decisions/0002 §14) -------------------------------------------------------

# `policy reset --json` as the app runs it: `--json` first, as every verb of the app has it.
_RESET_VERB = '["--json", "policy", "reset"]'


def test_reset_settings_is_offered_only_while_the_policy_is_invalid() -> None:
    # Reset Settings… is shown only while the settings file is invalid: in the note behind the
    # menu's problem line, when its explanation is the host's `policy_invalid` refusal
    # (`offersReset`, set in one place), in the note behind run's Needs attention line for
    # `policy` `invalid`, and, while connected, as an item of the Settings menu, behind the same
    # flag (decisions/0002 §14). No other item offers it, and no other note.
    words = _swift("ConnectionText.swift")
    text = _swift("HostText.swift")
    model = _swift("HostModel.swift")
    connection = _swift("Connection.swift")
    assert 'static let resetSettingsItem = "Reset Settings…"' in words
    for path in SWIFT_FILES:
        source = path.read_text(encoding="utf-8")
        if path.name not in (
            "ConnectionText.swift",
            "HostModel.swift",
            "Connection.swift",
            "MenuBarApp.swift",
        ):
            assert "resetSettingsItem" not in source, path.name
            assert "resetSettingsChosen" not in source, path.name
        if path.name != "ConnectionText.swift":
            assert '"Reset Settings' not in source, path.name
        if path.name != "HostText.swift":
            assert "offersReset:" not in source, path.name
    # The explanation offers it only when it says the settings file can't be read: false unless
    # set, and set in one place, from the refusal's code, `policy_invalid`, whose sentence that is
    # (spec/CLI.md section 10). Never from the host's words: they are for people (section 2).
    assert "    var offersReset = false\n" in text
    assert re.findall(r"offersReset: ([^\n]*)", text) == ['error.code == "policy_invalid"']
    assert '"policy_invalid": "\\(appName)\'s settings file can\'t be read.",' in text
    assert "settingsFileInvalid" not in text
    # The menu's problem line: the note has Reset Settings… only behind offersReset.
    problem = _swift_function(model, "    func showProblem() {")
    assert problem.count("ConnectionText.resetSettingsItem") == 1
    gated = problem.split("if problem.offersReset {", 1)
    assert len(gated) == 2 and "ConnectionText.resetSettingsItem" in gated[1]
    assert "ConnectionText.resetSettingsItem" not in gated[0]
    assert "self?.resetSettingsChosen()" in problem
    # Its first button is Reset Settings… only behind the same flag: every other note's first
    # button is OK, which must not ask to reset.
    assert "if problem.offersReset && response == .alertFirstButtonReturn {\n" in problem
    assert model.count("resetSettingsChosen()") == 2
    # run's Needs attention line for an invalid file: its own branch, as pairUnusable has its
    # Disconnect.
    attention = _swift_function(connection, "    func showAttention() {")
    branch = attention.split("        case .needsAttention(.policyInvalid):\n", 1)
    assert len(branch) == 2
    branch_body = branch[1].split("\n        case ", 1)[0].split("\n        default:", 1)[0]
    assert "buttons = [ConnectionText.resetSettingsItem, ConnectionText.okButton]" in branch_body
    assert "actions = [{ HostModel.shared.resetSettingsChosen() }, {}]" in branch_body
    assert connection.count("resetSettingsItem") == 1
    assert connection.count("resetSettingsChosen()") == 1
    # The Settings menu's item, with Open at Login: once, behind the problem line's own flag, and
    # running the same action (test_open_at_login_is_a_switch_in_a_settings_menu_while_connected).
    menu = _flat(_swift("MenuBarApp.swift"))
    assert menu.count("ConnectionText.resetSettingsItem") == 1
    assert menu.count("resetSettingsChosen()") == 1
    assert (
        "if model.problem?.offersReset == true {"
        " Button(ConnectionText.resetSettingsItem) { model.resetSettingsChosen() } }"
    ) in menu


def test_reset_settings_asks_first_then_runs_policy_reset() -> None:
    # The user's click starts it, and a question comes first, through Alerts (no modal loop of its
    # own), with Cancel the default (the first button added); only its Reset Settings runs
    # `policy reset --json`, with the progress window and its Cancel, on the queue of the other
    # short verbs (HostModel.run). Then a note says what came of it, the menu reads the host's
    # set-up again, and Connection reads `status` again. The verb takes the policy lock, so a run
    # left behind still counts (HostCLI.stuckRuns), and it never touches the policy a pairing set
    # aside (spec/CLI.md section 7), so no restore list follows.
    model = _swift("HostModel.swift")
    chosen = _swift_function(model, "    func resetSettingsChosen() {")
    assert "Alerts.shared.show(" in chosen and "runModal" not in chosen
    assert "ConnectionText.resetQuestionTitle" in chosen
    assert "ConnectionText.resetQuestionText" in chosen
    cancel = chosen.index("alert.addButton(withTitle: ConnectionText.cancelButton)")
    assert cancel < chosen.index("alert.addButton(withTitle: ConnectionText.resetButton)")
    assert "if response == .alertSecondButtonReturn {\n" in chosen
    assert "self?.resetSettings()" in chosen
    assert _RESET_VERB not in chosen
    total = sum(path.read_text(encoding="utf-8").count(_RESET_VERB) for path in SWIFT_FILES)
    assert total == 1 and model.count(_RESET_VERB) == 1
    reset = _swift_function(model, "    private func resetSettings() {")
    assert f"runWithProgress({_RESET_VERB}, message: ConnectionText.resetting)" in reset
    # Read from the run itself, so that one cancelled or timed out after the host had set the
    # file aside still says so.
    assert "let outcome = ResetOutcome(result: result)\n" in reset
    assert "VerbOutcome(" not in reset
    assert "ConnectionText.reset(outcome, earlierRunStuck: stuck)" in reset
    assert "let stuck = HostCLI.stuckRuns > 0" in reset
    assert "await reload()" in reset
    assert "Connection.shared.settingsReset()" in reset
    # Try Again only where the host could not take the policy lock, and it runs the verb again
    # without asking again: the user has already chosen Reset Settings.
    assert "let retry = outcome.offersTryAgain\n" in reset
    assert "ConnectionText.tryAgainButton" in reset
    # Only Try Again runs it again: a note with no Try Again has OK as its first button.
    assert "if retry && response == .alertFirstButtonReturn {\n" in reset
    assert model.count("self?.resetSettings()") == 2
    # Connection reads status again only while no child of the app runs, whose events say more.
    settled = _swift_function(_swift("Connection.swift"), "    func settingsReset() {")
    assert "runChild == nil, pairChild == nil" in settled and "refreshStatus()" in settled
    # Nothing else of the app, and nothing on its own, runs it.
    for path in SWIFT_FILES:
        if path.name != "HostModel.swift":
            assert '"policy", "reset"' not in path.read_text(encoding="utf-8"), path.name
    # The ResetOutcome reader: a policy_reset object is a reset, not_invalid nothing to reset, and
    # anything else a failure; only policy_lock_failed offers Try Again.
    host_json = _swift("HostJSON.swift")
    reader = _swift_function(host_json, "    init(_ outcome: VerbOutcome) {")
    assert "case .done(let objects) where objects.contains(.policyReset):" in reader
    assert 'case .refused(let error) where error.code == "not_invalid":' in reader
    assert "self = .failed(outcome)" in reader
    # A run that wrote policy_reset reset the settings, however it then ended: the host writes it
    # only once the fresh policy is in place (src/lucille_host/cli.py, `_policy_reset`), and a
    # Cancel or the time limit can come after that. So its output is read before how it ended.
    ran = _swift_function(host_json, "    init(result: CLIResult) {")
    read = ran.index("if HostJSON.objects(inOutput: result.stdout).contains(.policyReset) {")
    assert read < ran.index("self = .reset") < ran.index("self = ResetOutcome(VerbOutcome(result))")


# --- Set Up: Add back, Folders, Abilities, Ready (decisions/0002 §3.1 steps 8 to 10, §11.6) -----


def _code_of(text: str) -> str:
    """``text`` without its comment lines, which may name what the code may not call."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))


def test_set_up_is_a_flow_the_self_test_walks_and_a_panel_that_draws_it() -> None:
    # Set Up's rules are SetupFlow and RestoreOffer, in a file that draws nothing, which the
    # self-test compiles, runs over the host's fixtures (checkSetup) and walks through 40,000
    # seeded steps (checkSetupWalk). SetupPanel only draws the page the flow gives it and hands
    # back what the user did, as PairingPanel does for the pairing flow: it runs nothing.
    assert "Setup.swift" in check_app.LOGIC_SOURCES
    assert "SetupPanel.swift" not in check_app.LOGIC_SOURCES
    flow = _swift("Setup.swift")
    panel = _swift("SetupPanel.swift")
    assert _swift_imports(flow) == {"Foundation"}
    assert "AppKit" in _swift_imports(panel)
    for declared in (
        "enum PreviousPolicy: Equatable, Sendable {",
        "enum RestoreItem: Equatable, Sendable {",
        "struct RestoreRow: Equatable, Sendable {",
        "struct RestoreOffer: Equatable, Sendable {",
        "struct SetupFlow: Equatable, Sendable {",
        "enum SetupText {",
    ):
        assert declared in flow, declared
    for name in ("HostCLI", "VerbQueue", "cli.run", "HostChild", "runModal()", "HostModel"):
        assert name not in _code_of(flow), name
    for name in ("HostCLI", "VerbQueue", "cli.run", "HostChild", "runModal()"):
        assert name not in _code_of(panel), name
    # Every box is the user's own click: none starts checked, and nothing checks them all.
    for text in (flow, panel):
        assert "Select All" not in text and "selectAll" not in text
    selftest = _SELF_TEST.read_text(encoding="utf-8")
    main_body = _swift_function(selftest, "    static func main() {")
    assert main_body.index("checkPairing(fixtures, report)") < main_body.index(
        "checkSetup(fixtures, report)"
    )
    checks = _swift_function(selftest, "    static func checkSetup(")
    assert "checkSetupWalk(report)" in checks
    for fixture in ('"pair-code-stdin"', '"policy-previous"', '"policy-previous-none"'):
        assert f"fixtures[{fixture}]" in checks, fixture
    assert 'fixtures["ops-list-sends-file-content"]' in checks
    walk = _swift_function(selftest, "    static func checkSetupWalk(_ report: Report) {")
    assert "for step in 0..<40_000 {" in walk
    # A box is clicked only in an open window. The flow drops a click while it is closed;
    # checkSetup feeds one, then opens the window again and finds every box as it was; and the
    # walk's oracle counts a click only where the window was open before it.
    clicked = _swift_function(
        flow, "    private mutating func checked(_ rows: Set<Int>) -> [Effect] {"
    )
    assert "guard windowOpen, case .addBack(let offer, _)? = page else {" in clicked
    shut, after = checks.split("let clickedWhileClosed = flow.handle(.checked([2]))\n", 1)
    assert shut.rstrip().endswith("_ = flow.handle(.windowClosed)")
    assert after.index("let reopenedUnchecked = flow.handle(.reopen)") < after.index(
        "let addedAfterReopen = flow.handle(.addChecked)"
    )
    assert (
        "clickedWhileClosed.isEmpty && reopenedUnchecked == stillUnchecked"
        " && addedAfterReopen.isEmpty" in after
    )
    assert walk.index("let wasOpen = flow.windowOpen") < walk.index(
        "let effects = flow.handle(input)"
    )
    assert "if wasOpen, case .addBack? = before {" in walk
    # policy previous, on the profile no pairing has used, says none was set aside.
    cli = _swift_function(selftest, "    static func checkCLI(")
    assert '["--json", "policy", "previous"], timeout: SetupFlow.previousLimit' in cli
    assert "PreviousPolicy.nothingSetAside" in cli
    assert cli.index('"policy", "previous"') < cli.index("checkPairingVerbs(")


def test_the_restore_list_goes_through_the_same_gates() -> None:
    # A row the user checks on the Add back page becomes an ordinary `roots add` or `ops enable`
    # (decisions/0002 §11.6), through the menu's own gates and nothing else: a folder whose
    # spelling is the whole disk is refused before any run, whatever the set-aside policy marked
    # it; the home folder, or a folder that holds it by its path, is asked about first, as the
    # menu's chooseAndAdd asks, and confirmed only by that answer; then HostModel.addRoot, with its
    # whole-disk refusal, its home question after the host's broad_folder and its name dialog
    # again after alias_taken, then `roots check` on it; an ability through setAbility, with its
    # acknowledgement for one whose replies carry file contents. The folder's URL is made as a
    # folder's: without `isDirectory`, Foundation reads the disk, on the main thread, to tell.
    model = _swift("HostModel.swift")
    restore = _swift_function(model, "    func restore(_ item: RestoreItem) async {")
    folder = restore.split("case .folder(let folder):", 1)[1].split("case .ability(", 1)[0]
    refused = folder.index(
        "guard !HostText.namesWholeDisk(folder.path) else {\n                return\n"
    )
    asked = folder.index("if let kind = HostText.broadFolder(path: folder.path, home: cli.home) {")
    question = folder.index(
        "guard await RunLoopTurn.ask({ self.confirmBroadFolder(kind) }) else {\n"
        "                    addFolder()\n                    return\n"
    )
    confirmed = folder.index("confirmed = true")
    added = folder.index(
        "await addRoot(alias: folder.alias,"
        " url: URL(fileURLWithPath: folder.path, isDirectory: true),"
    )
    assert refused < asked < question < confirmed < added
    assert "var confirmed = false" in folder[:asked]
    assert folder.count("confirmed = true") == 1
    assert "confirmedHome: confirmed)" in folder[added:]
    assert '["--json", "roots", "check", ' in folder and "timeout: SetupFlow.checkLimit" in folder
    # chooseAndAdd asks the same question, the same way, before its run.
    chosen = _swift_function(model, "    private func chooseAndAdd() {")
    assert "if let kind = HostText.broadFolder(path: path, home: cli.home) {" in chosen
    assert "guard confirmBroadFolder(kind) else {" in chosen
    ability = restore.split("case .ability(let ability):", 1)[1]
    assert "setAbility(ability, on: true)" in ability
    for gone in (
        "addFolderArguments",
        '"roots", "add"',
        '"ops"',
        _BROAD_FLAG,
        "turn(",
        "confirmedHome: true",
        "folder.broad",
        "runWithProgress(",
    ):
        assert gone not in restore, gone
    # The command line that adds a folder still has addRoot's two call sites alone, and turn is
    # still reached from setAbility alone.
    assert model.count("HostText.addFolderArguments(") == 2
    assert len(re.findall(r"\bturn\(", model)) == 3
    # Set Up's own files add and turn on nothing: Connection hands each row the flow restores,
    # one at a time, to HostModel.restore, and says when it is done.
    for name in ("Setup.swift", "SetupPanel.swift"):
        code = _code_of(_swift(name))
        for gone in ('"roots"', '"ops"', "addFolderArguments", "addRoot(", "turn("):
            assert gone not in code, (name, gone)
    assert "HostModel.shared.restore(" not in _code_of(_swift("SetupPanel.swift"))
    connection = _swift("Connection.swift")
    effect = _swift_function(connection, "    private func setUpEffect(")
    restoring = effect.split("case .restore(let row, let item):", 1)[1].split("\n        case ", 1)[
        0
    ]
    assert "await HostModel.shared.restore(item)" in restoring
    assert "self.feedSetUp(.itemDone(row: row))" in restoring
    assert restoring.index("HostModel.shared.restore(") < restoring.index(".itemDone(")
    assert connection.count("HostModel.shared.restore(") == 1
    # The flow's only effect that adds anything back is `restore`, and it restores only a row
    # the user checked that has a box: checkSetup and checkSetupWalk hold it to that. Its one
    # other effect that widens anything, `registerLoginItem`, asks macOS to open the app at login,
    # only from Done with Ready's box checked
    # (test_ready_asks_to_open_at_login_and_only_done_registers).
    flow = _swift("Setup.swift")
    # The whole disk gets no box by its spelling, whatever the set-aside policy marked it, and
    # by the disk for a folder marked broad; an ordinary folder is not read.
    offer = _swift_function(
        flow, "    static func items(previous: PreviousPolicy, since start: Date,"
    )
    assert (
        "if HostText.namesWholeDisk(folder.path)"
        " || (folder.broad && HostText.isWholeDisk(path: folder.path)) {" in offer
    )
    assert offer.count("isWholeDisk(") == 1
    effects = re.search(
        r"    enum Effect: Equatable, Sendable \{\n(.*?)\n    \}\n", flow, re.DOTALL
    )
    assert effects is not None
    cases = re.findall(r"^        case (\w+)", effects.group(1), re.MULTILINE)
    assert cases == ["readPrevious", "show", "restore", "note", "registerLoginItem", "close"]


def test_set_up_opens_once_they_match_here_is_connected() -> None:
    # decisions/0002 §3.1 steps 8 to 10 and §11.6: the first time in this app session that the
    # status line says Connected (or Connected, waiting for Lucille) after They Match was clicked
    # here, not after a relaunch, nor for a pair confirmed in Terminal. Connection feeds SetupFlow
    # when this session's own pairing starts, with the pairing flow's epoch; a policy_reset from
    # that pairing's child, before the pairing flow reads the object; They Match's confirmation,
    # from the note that says so; Connected, from the status line; the end of the pair at
    # Disconnect; and the menu's Set Up Folders and Abilities…, while it is owed. PairingFlow, the
    # gate, is unchanged.
    connection = _swift("Connection.swift")
    assert "    private func feedSetUp(_ input: SetupFlow.Input) {" in connection
    start = _swift_function(connection, "    private func startPair(code: String, epoch: Int) {")
    assert start.index("feedSetUp(.pairStarted(at: Date(), epoch: epoch))") < start.index(
        "try child.start()"
    )
    assert "if object == .policyReset {" in start
    assert start.index("self.feedSetUp(.policySetAside(epoch: epoch))") < start.index(
        "self.feedFlow(.childObject(object), epoch: epoch)"
    )
    effect = _swift_function(connection, "    private func flowEffect(")
    said = effect.split("case .say(let note):", 1)[1].split("\n        case ", 1)[0]
    assert "if note == .confirmed {" in said
    assert "feedSetUp(.confirmedHere(epoch: epoch))" in said
    publish = _swift_function(connection, "    private func publish() {")
    assert "line == ConnectionText.connected || line == ConnectionText.connectedWaiting" in publish
    assert "feedSetUp(.connected)" in publish
    # The pair ended, whoever ended it: a confirmed pair seen, then `status` says there is none
    # and `run` is not alive. Never during a pairing, when no pair was confirmed yet.
    assert "if confirmed {\n            setUpSawPair = true\n" in publish
    ended = publish.split("} else if setUpSawPair, ", 1)[1]
    assert ended.startswith("case .noPair? = status?.pair, !supervisor.runAlive {")
    assert ended.index("setUpSawPair = false") < ended.index("feedSetUp(.pairEnded)")
    assert publish.count("feedSetUp(.pairEnded)") == 1
    assert "self.feedSetUp(.pairEnded)" in _swift_function(
        connection, "    private func forget() {"
    )
    assert "feedSetUp(.reopen)" in _swift_function(connection, "    func setUpChosen() {")
    menu = _swift("MenuBarApp.swift")
    assert "if connection.setUpOffered {" in menu
    assert "Button(SetupText.menuItem) { connection.setUpChosen() }" in menu
    pairing = _swift("Pairing.swift")
    assert "SetupFlow" not in pairing and "feedSetUp" not in pairing


def test_set_up_runs_policy_previous_and_roots_check_as_the_app_writes_them(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Set Up's own verbs, with --json first: `policy previous`, with 30 s, since it loads the
    # policy set aside, which can wait on a stalled mount; then `ops list` and `roots list`, which
    # the offer is filtered against; and `roots check`, for every folder or for one, with 600 s,
    # as adding a folder has, since macOS may first ask the user about a folder (spec/CLI.md
    # section 2). Each is answered as the app reads it, through the host's own `main`.
    connection = _swift("Connection.swift")
    model = _swift("HostModel.swift")
    flow = _swift("Setup.swift")
    assert "    static let previousLimit: TimeInterval = 30\n" in flow
    assert "    static let checkLimit: TimeInterval = 600\n" in flow
    previous_verb = '["--json", "policy", "previous"]'
    assert sum(path.read_text(encoding="utf-8").count(previous_verb) for path in SWIFT_FILES) == 1
    effect = _swift_function(connection, "    private func setUpEffect(")
    reading = effect.split("case .readPrevious:", 1)[1].split("\n        case ", 1)[0]
    assert previous_verb in reading and "timeout: SetupFlow.previousLimit" in reading
    for verb in ('["--json", "ops", "list"]', '["--json", "roots", "list"]', ".previousRead("):
        assert verb in reading, verb
    checks = _swift_function(model, "    func checkFolders() async {")
    assert '["--json", "roots", "check"]' in checks and "timeout: SetupFlow.checkLimit" in checks

    def run(*argv: str) -> tuple[int, list[dict[str, Any]]]:
        capsys.readouterr()
        try:
            code = main(["--profile", "mac-app", *argv])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        return code, [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert run("--json", "policy", "previous") == (
        0,
        [{"v": 1, "type": "previous_policy", "state": "none"}],
    )
    folder = home / "Documents"
    folder.mkdir()
    assert run("--json", "roots", "add", "--", "docs", str(folder))[0] == 0
    _, listed = run("--json", "roots", "list")
    held = listed[0]["path"]
    opened = {"v": 1, "type": "root_check", "alias": "docs", "path": held, "state": "ok"}
    assert run("--json", "roots", "check") == (0, [opened])
    assert run("--json", "roots", "check", "docs") == (0, [opened])


def test_only_runs_that_take_the_policy_lock_count_as_stuck(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # HostText.stuckRun says that until a run left behind ends, the app can't change its folders
    # or abilities: such a run may hold the host's policy lock (src/lucille_host/cli.py,
    # `_policy_lock`). Only a run that takes that lock can. One that only reads (the alias
    # suggestion, `roots list`, `ops list`, `--version`, `policy previous`, `roots check`,
    # `status`), left behind, holds nothing up, so it is not counted in HostCLI.stuckRuns, and the
    # next change never says that an earlier run holds it up when none does.
    source = _swift("HostCLI.swift")
    rows = _swift_string_set(source, "policyLockVerbs")
    assert rows == {"ops enable", "ops disable", "roots add", "roots remove", "policy reset"}
    # Read on the words before `--`, without `--json`; the alias suggestion takes no lock, written
    # with a space or with `=`.
    classify = _swift_function(
        source, "    static func takesPolicyLock(_ verb: [String]) -> Bool {"
    )
    assert 'prefix(while: { $0 != "--" })' in classify
    assert '"--json"' in classify
    assert 'hasPrefix("--suggest-alias")' in classify
    assert "policyLockVerbs.contains(" in classify
    # A run left behind is kept with whether it takes the lock, and only those count.
    ran = _function_body(source, "    func run(\n")
    assert "HostCLI.leftBehind.add(process, holdsPolicyLock: HostCLI.takesPolicyLock(verb))" in ran
    assert "    static var stuckRuns: Int { leftBehind.holdingPolicyLock }\n" in source
    left = source.split("private final class LeftBehind", 1)[1].split("\n}\n", 1)[0]
    assert "func add(_ process: Process, holdsPolicyLock: Bool) {" in left
    assert "var running: Int {" not in left
    holding = _function_body(source, "    var holdingPolicyLock: Int {")
    assert holding.index("processes.removeAll { !$0.process.isRunning }") < holding.index(
        "return HostCLI.lockHolders(runs)"
    )
    assert "(running: $0.process.isRunning, holdsPolicyLock: $0.holdsPolicyLock)" in holding
    assert "processes.count" not in holding
    # The count is a function of what was left behind, which the self-test runs.
    holders = _swift_function(
        source,
        "    static func lockHolders(_ runs: [(running: Bool, holdsPolicyLock: Bool)]) -> Int {",
    )
    assert "runs.filter { $0.running && $0.holdsPolicyLock }.count" in holders
    # Which verbs take the lock, measured on the host itself: each verb the app runs, through the
    # host's own `main` on the app's profile, with the lock recording each time it is taken.
    from lucille_host import cli as host_cli

    taken: list[bool] = []
    real = host_cli._policy_lock

    @contextlib.contextmanager
    def recording(dirs: Any) -> Any:
        taken.append(True)
        with real(dirs):
            yield

    monkeypatch.setattr(host_cli, "_policy_lock", recording)
    folder = home / "Documents"
    folder.mkdir()

    def takes(*argv: str) -> bool:
        taken.clear()
        capsys.readouterr()
        try:
            main(["--profile", "mac-app", *argv])
        except SystemExit:
            pass
        capsys.readouterr()
        return bool(taken)

    verbs = [
        ("--json", "--version"),
        ("--json", "ops", "list"),
        ("--json", "ops", "enable", "files.sha256"),
        ("--json", "ops", "--json", "disable", "files.sha256"),
        ("--json", "roots", "list"),
        ("--json", "roots", "add", "--suggest-alias", str(folder)),
        ("--json", "roots", "add", "--suggest-alias=" + str(folder)),
        ("--json", "roots", "add", "--", "docs", str(folder)),
        ("--json", "roots", "check"),
        ("--json", "roots", "check", "docs"),
        ("--json", "roots", "remove", "docs"),
        ("--json", "roots", "add", "--", "docs", "--suggest-alias"),
        ("--json", "policy", "previous"),
        ("--json", "policy", "reset"),
        ("--json", "status"),
        ("--json", "pause"),
        ("--json", "resume"),
        ("--json", "kill"),
        ("--json", "log", "--limit", "200"),
        ("--json", "pair", "status"),
        ("--json", "pair", "confirm"),
        ("--json", "pair", "reject"),
        ("--json", "forget"),
    ]
    measured = {argv: takes(*argv) for argv in verbs}
    locking = {argv for argv, took in measured.items() if took}
    assert locking == {
        ("--json", "ops", "enable", "files.sha256"),
        ("--json", "ops", "--json", "disable", "files.sha256"),
        ("--json", "roots", "add", "--", "docs", str(folder)),
        ("--json", "roots", "remove", "docs"),
        ("--json", "roots", "add", "--", "docs", "--suggest-alias"),
        ("--json", "policy", "reset"),
    }
    # The rule the Swift states, read as written: the verb and its action among the words before
    # `--`, but `--json`, and no alias suggestion among them. It agrees with the host on each.
    for argv, took in measured.items():
        before = argv[: argv.index("--")] if "--" in argv else argv
        words = [word for word in before if word != "--json"]
        says = " ".join(words[:2]) in rows and not any(
            word.startswith("--suggest-alias") for word in words
        )
        assert says == took, argv
    # The self-test holds the Swift function itself to the same verbs.
    selftest = _SELF_TEST.read_text(encoding="utf-8")
    assert "HostCLI.takesPolicyLock($0)" in selftest
    assert "let holding = HostCLI.lockHolders(leftRuns)" in selftest
    assert '["--json", "roots", "add", "--", "docs", "--suggest-alias"],' in selftest


def test_the_pairing_check_sets_up_after_connected() -> None:
    # On macos-15, after run reads the room and the menu says Connected, the Set Up window opens
    # by itself, within 60 s: with no Add back page, since the harness paired through the
    # command line, so the app saw no policy_reset; on Folders, which lists none; then on What
    # Lucille may do, files.sha256's switch shows it off, is turned on (`ops list --json` then says
    # it is enabled, and the switch shows it on), and off again; then Ready, whose box "Open
    # <name> when you log in" is checked and which says macOS's notice; and Done closes it, after
    # which the app says on standard error which state macOS gave its login item, and its words
    # agree with that state (login_after_done). A Set Up check that fails does not stop the
    # pairing check: it closes the window and any note about opening at login, and turns
    # files.sha256 off, and needs only those facts, so that the fingerprint step's request while
    # it is off still meets not_enabled (M18), and the later steps still run.
    source = _CHECK_APP.read_text(encoding="utf-8")
    go = _python_function(source, "go")
    assert (
        go.index("self.run_reads_the_room")
        < go.index("self.set_up_after_connected")
        < go.index("self.fingerprint")
    )
    outer = _python_function(source, "set_up_after_connected")
    walked = outer.index("self.set_up_pages()")
    closed = outer.index("_close_window(SETUP_TITLE)")
    noted = outer.index("_close_login_note()")
    disabled = outer.index('self.host("--json", "ops", "disable", "files.sha256")')
    needed = outer.index("self.need(")
    assert walked < closed < noted < disabled < needed
    assert outer.count("self.need(") == 1
    assert 'off = self.op_enabled("files.sha256") is False' in outer[disabled:needed]
    assert "clear = SETUP_TITLE not in _window_names() and _login_note() is None" in outer
    assert "self.need(\n            off and clear," in outer
    # Its detail is made only when it failed.
    assert 'detail = "" if off and clear else ' in outer
    step = _python_function(source, "set_up_pages")
    # Each Set Up fact is expected, and the walk returns at the first one the rest needs: none
    # stops the pairing check.
    assert "self.need(" not in step and "_Stop" not in step
    # Done's detail, which reads the app's windows through System Events, only when Done failed.
    assert (
        'closed, "Done closes the Set Up window", "" if closed else f"{said} {_window_names()}"'
        in step
    )
    opened = step.index("_wait_until(lambda: SETUP_TITLE in _window_names(), 60)")
    folders = step.index("_says(SETUP_FOLDERS)")
    assert opened < folders < step.index("not _says(SETUP_ADD_BACK)")
    assert folders < step.index("_says(SETUP_NO_FOLDERS)")
    first = step.index("_press(SETUP_CONTINUE)")
    assert folders < first < step.index("_says(SETUP_ABILITIES)")
    switch = 'ABILITY_TITLES["files.sha256"]'
    assert switch in step and step.count("_toggle(") == 2
    off = step.index('self.op_enabled("files.sha256") is False and _checked(switch) == "0"')
    on = step.index('self.op_enabled("files.sha256") is True')
    shown_on = step.index('_checked(switch) == "1"')
    back = step.rindex('self.op_enabled("files.sha256") is False')
    shown_off = step.rindex('_checked(switch) == "0"')
    assert off < step.index("_toggle(") < on < shown_on < step.rindex("_toggle(") < back < shown_off
    assert shown_off < step.rindex("_press(SETUP_CONTINUE)") < step.index("_says(SETUP_READY)")
    assert step.index("_says(SETUP_READY)") < step.index("_press(SETUP_DONE)")
    assert step.rindex("SETUP_TITLE not in _window_names()") > step.index("_press(SETUP_DONE)")
    for shot in ("p-setup-folders.png", "p-setup-abilities.png", "p-setup-ready.png"):
        assert shot in step, shot
    # Ready's box, checked, and macOS's notice in words; then Done, and what came of it.
    ready = step.index("_says(SETUP_READY)")
    box = step.index("box = _checked(OPEN_AT_LOGIN_BOX)")
    notice = step.index("_says(OPEN_AT_LOGIN_NOTICE)")
    mark = step.index("before = len(self.stderr_now())")
    done = step.index("_press(SETUP_DONE)")
    assert ready < box < notice < mark < done < step.index("self.login_after_done(before)")
    assert 'box == "1"' in step
    after = _python_function(source, "login_after_done")
    assert "_login_state(self.stderr_now()[before:])" in after
    assert "_dump_login_items()" in after and "LOGIN_NOTE_AFTER_DONE[state]" in after
    assert "p-setup-after-done.png" in after
    # Open Login Items Settings would open System Settings: the checks never press it.
    assert "_press(OPEN_LOGIN_ITEMS" not in source and "_choose(OPEN_LOGIN_ITEMS" not in source
    enabled = _python_function(source, "op_enabled")
    assert 'self.host("--json", "ops", "list")' in enabled
    assert '"enabled"' in enabled
    toggle = _python_function(source, "_toggle")
    assert '_target("checkbox", name)' in toggle and "click theTarget" in toggle
    checked = _python_function(source, "_checked")
    assert '_target("checkbox", name)' in checked and "value of theTarget" in checked


def test_the_pairing_check_reads_an_operation_as_the_host_writes_it(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The Set Up step decides on _PairingCheck.op_enabled, which reads `ops list --json` on the
    # app's profile. Run here on the host's own answers, before and after `ops enable`, with a
    # stand-in for the check whose `host` is the host's own `main`.
    def host(*argv: str) -> subprocess.CompletedProcess[str]:
        capsys.readouterr()
        try:
            code = main(["--profile", "mac-app", *argv])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        return subprocess.CompletedProcess(list(argv), code, capsys.readouterr().out, "")

    check = types.SimpleNamespace(host=host)
    assert check_app._PairingCheck.op_enabled(check, "files.sha256") is False
    assert check_app._PairingCheck.op_enabled(check, "host.describe") is True
    assert check_app._PairingCheck.op_enabled(check, "no.such") is None
    assert host("--json", "ops", "enable", "files.sha256").returncode == 0
    assert check_app._PairingCheck.op_enabled(check, "files.sha256") is True
    assert check_app._PairingCheck.op_enabled(check, "host.describe") is True

    # A line that is not JSON, or an `enabled` that is not a boolean, says nothing.
    def garbled(*argv: str) -> subprocess.CompletedProcess[str]:
        said = 'not json\n{"v":1,"type":"op","name":"files.sha256","enabled":"yes"}\n'
        return subprocess.CompletedProcess(list(argv), 0, said, "")

    stand_in = types.SimpleNamespace(host=garbled)
    assert check_app._PairingCheck.op_enabled(stand_in, "files.sha256") is None


# What the Set Up step needs once it has run, whatever failed in it.
_SET_UP_PUT_RIGHT = (
    "after Set Up, files.sha256 is off, and no Set Up window and no note about opening at login"
    " is open"
)


def _login_note_buttons() -> dict[str, str]:
    """The button that closes each note about opening at login and does nothing else, by the
    note's title."""
    return {check_app.NEEDS_APPROVAL_TITLE: check_app.NOT_NOW, check_app.LOGIN_FAILED_TITLE: "OK"}


class _SetUpOnAMac:
    """The Set Up window as System Events would see it, and the host's files.sha256, for running
    the pairing check's Set Up step here. Each flag makes one thing go wrong.

    Ready's box "Open <name> when you log in" is checked unless ``box_on`` is false. Done with it
    checked asks macOS to open the app at login: the app writes ``login item: <login_state>`` on
    standard error (nothing when ``login_state`` is None), and shows the note that goes with that
    state (none while ``enabled``), or, with ``login_words`` false, the wrong one. The note's Not
    Now, or OK, closes it unless ``note_closes`` is false. Ready says macOS's notice unless
    ``notice`` is false. Every body sent to System Events is kept in ``sent``. Its words are read
    only at run time: an instance is made when this module is imported."""

    def __init__(
        self,
        *,
        opens: bool = True,
        drawn_on: bool = False,
        host_turns_on: bool = True,
        host_turns_off: bool = True,
        done_closes: bool = True,
        disable_works: bool = True,
        switch_moves: bool = True,
        offered: bool = False,
        stderr: str = "",
        box_on: bool = True,
        login_state: str | None = "enabled",
        login_words: bool = True,
        note_closes: bool = True,
        notice: bool = True,
    ) -> None:
        self.page: str | None = "folders" if opens else None
        self.box_on = box_on
        self.notice = notice
        self.sent: list[str] = []
        self.login_state = login_state
        self.login_words = login_words
        self.note_closes = note_closes
        # The note about opening at login that is up, by its title; the app's standard error,
        # once the runner has made it; every button pressed; and how often the login items were
        # printed.
        self.note: str | None = None
        self.log: Path | None = None
        self.pressed: list[str] = []
        self.dumps = 0
        self.switch = drawn_on
        self.op_on = False
        self.host_turns_on = host_turns_on
        self.host_turns_off = host_turns_off
        self.done_closes = done_closes
        self.disable_works = disable_works
        self.switch_moves = switch_moves
        self.disabled_by_cli = 0
        self.closed_by_button = 0
        # The menu's items, which offer Set Up again when ``offered``; and the app's standard
        # error so far.
        self.menu = [check_app.CONNECTED, *([check_app.SETUP_ITEM] if offered else [])]
        self.stderr = stderr
        # How often the menu was read, and each screenshot taken, by name.
        self.menu_reads = 0
        self.shots: list[str] = []

    def window_names(self) -> list[str]:
        return [check_app.SETUP_TITLE] if self.page is not None else []

    def system_events(self, body: str, timeout: float = 30) -> tuple[bool, str]:
        """What the step reads through System Events beyond the names it reads above: the menu,
        and every window, which only a failure's detail reads."""
        self.sent.append(body)
        if "name of every menu item of" in body:
            self.menu_reads += 1
            return True, "\n".join(self.menu)
        if body == check_app.CLOSE_MENU:
            return True, "Escape sent"
        if check_app.WINDOWS_HOLD in body:
            return True, "frontmost true, 0 window(s):"
        return False, f"not on a Mac: {body!r}"

    def texts(self) -> list[str]:
        if self.page == "ready":
            notice = f"{check_app.OPEN_AT_LOGIN_NOTICE} That is expected."
            shown = [check_app.SETUP_READY, check_app.OPEN_AT_LOGIN_BOX]
            shown += [notice] if self.notice else []
        else:
            shown = {
                "folders": [check_app.SETUP_FOLDERS, check_app.SETUP_NO_FOLDERS],
                "abilities": [check_app.SETUP_ABILITIES],
                None: [],
            }[self.page]
        return shown + ([self.note] if self.note is not None else [])

    def says(self, words: str) -> bool:
        return any(words in text for text in self.texts())

    def press(self, name: str) -> tuple[bool, str]:
        self.pressed.append(name)
        if name == check_app.SETUP_CONTINUE and self.page in ("folders", "abilities"):
            self.page = {"folders": "abilities", "abilities": "ready"}[self.page]
            return True, "pressed"
        if name == check_app.SETUP_DONE and self.page == "ready":
            if self.done_closes:
                self.page = None
                self.done()
            return True, "pressed"
        if self.note is not None and _login_note_buttons()[self.note] == name:
            if self.note_closes:
                self.note = None
            return True, "pressed"
        return False, f"no button named {name}"

    def done(self) -> None:
        """Done, with Ready's box checked: the app asks macOS to open it at login, says on
        standard error which state macOS then gives, and shows the note for that state."""
        if not self.box_on or self.login_state is None:
            return
        assert self.log is not None
        said = self.log.read_text(encoding="utf-8")
        self.log.write_text(said + f"login item: {self.login_state}\n", encoding="utf-8")
        right = check_app.LOGIN_NOTE_AFTER_DONE[self.login_state]
        if self.login_words:
            self.note = right
        else:
            self.note = check_app.NEEDS_APPROVAL_TITLE if right is None else None

    def dump(self) -> None:
        self.dumps += 1

    def toggle(self, name: str) -> tuple[bool, str]:
        if self.page != "abilities" or name != check_app.ABILITY_TITLES["files.sha256"]:
            return False, "no checkbox"
        asked = not (self.switch if self.switch_moves else self.op_on)
        if self.switch_moves:
            self.switch = asked
        if asked and self.host_turns_on:
            self.op_on = True
        if not asked and self.host_turns_off:
            self.op_on = False
        return True, "clicked"

    def checked(self, name: str) -> str:
        if self.page == "ready" and name == check_app.OPEN_AT_LOGIN_BOX:
            return "1" if self.box_on else "0"
        if self.page != "abilities" or name != check_app.ABILITY_TITLES["files.sha256"]:
            return "absent"
        return "1" if self.switch else "0"

    def close_window(self, title: str) -> tuple[bool, str]:
        if title == check_app.SETUP_TITLE and self.page is not None:
            self.page = None
            self.closed_by_button += 1
            return True, ""
        return False, "no such window"

    def op_enabled(self, op: str) -> bool | None:
        return self.op_on if op == "files.sha256" else None

    def host(self, *args: str) -> subprocess.CompletedProcess[str]:
        if args == ("--json", "ops", "disable", "files.sha256"):
            self.disabled_by_cli += 1
            if self.disable_works:
                self.op_on = False
        return subprocess.CompletedProcess(list(args), 0, "", "")


def _set_up_step_on(
    mac: _SetUpOnAMac, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> check_app.Checker:
    for name, fake in (
        ("_window_names", mac.window_names),
        ("_texts", mac.texts),
        ("_says", mac.says),
        ("_press", mac.press),
        ("_toggle", mac.toggle),
        ("_checked", mac.checked),
        ("_close_window", mac.close_window),
        ("_system_events", mac.system_events),
    ):
        monkeypatch.setattr(check_app, name, fake)
    monkeypatch.setattr(check_app, "_screenshot", lambda path: mac.shots.append(path.name))
    monkeypatch.setattr(check_app, "_wait_until", lambda test, seconds: test())
    monkeypatch.setattr(check_app, "_value_within", lambda get, seconds: get())
    # Never sudo here. raising=False: the stand-in is set whether or not the check has the name.
    monkeypatch.setattr(check_app, "_dump_login_items", mac.dump, raising=False)
    check = object.__new__(check_app._PairingCheck)
    check.checker = check_app.Checker()
    check.screens = tmp_path
    stderr = tmp_path / "pairing-pairing-stderr.txt"
    stderr.write_text(mac.stderr, encoding="utf-8")
    mac.log = stderr
    check.logs = [stderr]
    check.op_enabled = mac.op_enabled  # type: ignore[method-assign]
    check.host = mac.host  # type: ignore[method-assign]
    check.set_up_after_connected()
    return check.checker


def test_the_set_up_step_passes_on_an_app_that_works(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The pairing check's Set Up step, run against a stand-in for the app and the host: on one
    # that does what Set Up should, nothing fails, and the cleanup has nothing to put right.
    mac = _SetUpOnAMac()
    checker = _set_up_step_on(mac, monkeypatch, tmp_path)
    assert checker.failures == []
    assert (mac.page, mac.op_on, mac.disabled_by_cli, mac.closed_by_button) == (None, False, 0, 0)
    # The missing window's detail is made only when the window is missing. Made on every run, it
    # took a missing window's screenshot and read the menu, Escape and all, while Set Up was open.
    assert mac.shots == [
        "p-setup-folders.png",
        "p-setup-abilities.png",
        "p-setup-ready.png",
        "p-setup-after-done.png",
    ]
    assert mac.menu_reads == 0
    # With the login item enabled, no note follows Done, and nothing is pressed after it; macOS's
    # login items are printed once.
    assert mac.note is None and mac.dumps == 1
    assert mac.pressed[-1] == check_app.SETUP_DONE
    # And no key goes anywhere: an Escape sent with no menu open closed the Set Up window in CI
    # run 42395410137 (_MenuOnAMac).
    assert not _keys_sent(mac.sent), mac.sent


def _keys_sent(bodies: list[str]) -> list[str]:
    """The bodies sent to System Events that press a key: Escape, Return, Space or any other."""
    return [
        body
        for body in bodies
        if body == check_app.CLOSE_MENU or "keystroke" in body or "key code" in body
    ]


@pytest.mark.parametrize(
    ("mac", "first_failure"),
    [
        (_SetUpOnAMac(opens=False), "once connected"),
        (_SetUpOnAMac(drawn_on=True), "is off before its switch is used"),
        (_SetUpOnAMac(host_turns_on=False), "turns files.sha256 on"),
        (_SetUpOnAMac(host_turns_off=False), "clicked again"),
        (_SetUpOnAMac(done_closes=False), "Done closes the Set Up window"),
        (_SetUpOnAMac(switch_moves=False), "shows it on"),
        (_SetUpOnAMac(box_on=False), "Ready's box"),
        (_SetUpOnAMac(login_state=None), "which state macOS gives its login item"),
        (_SetUpOnAMac(login_state="requiresApproval", login_words=False), "words agree"),
        (_SetUpOnAMac(login_state="notFound", login_words=False), "words agree"),
        (_SetUpOnAMac(login_words=False), "words agree"),
        (_SetUpOnAMac(notice=False), "and Ready says"),
        # A line from before Done is not what Done gave: only the lines after it count.
        (
            _SetUpOnAMac(stderr="login item: enabled\n", login_state=None),
            "which state macOS gives its login item",
        ),
    ],
)
def test_a_set_up_step_that_fails_is_counted_and_put_right(
    mac: _SetUpOnAMac, first_failure: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Each way Set Up can go wrong is the step's first failure, and the step goes on to close the
    # window and any note about opening at login, and turn files.sha256 off, the facts the later
    # steps need, so the pairing check still runs them.
    checker = _set_up_step_on(mac, monkeypatch, tmp_path)
    assert checker.failures and first_failure in checker.failures[0], checker.failures
    assert mac.page is None and mac.op_on is False and mac.note is None
    assert checker.failures[-1] != _SET_UP_PUT_RIGHT
    assert "Open Login Items Settings" not in mac.pressed


def test_ready_with_its_box_unchecked_is_one_failure_and_reads_no_login_item(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # With the box off, Done registers nothing and the app writes no line: the step counts the
    # box, once, and does not go on to look for a state Done never gave, which would be a second,
    # false failure, nor print macOS's login items.
    mac = _SetUpOnAMac(box_on=False)
    checker = _set_up_step_on(mac, monkeypatch, tmp_path)
    assert len(checker.failures) == 1 and "Ready's box" in checker.failures[0], checker.failures
    assert mac.dumps == 0 and mac.note is None
    assert mac.pressed[-1] == check_app.SETUP_DONE


def test_a_done_that_does_not_close_the_window_is_one_failure_and_reads_no_login_item(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Done that did not close the window asked macOS for nothing: the step counts that, once, and
    # does not go on to look for a state Done never gave, nor print macOS's login items.
    mac = _SetUpOnAMac(done_closes=False)
    checker = _set_up_step_on(mac, monkeypatch, tmp_path)
    assert len(checker.failures) == 1, checker.failures
    assert "Done closes the Set Up window" in checker.failures[0]
    assert mac.dumps == 0 and mac.note is None


def test_a_set_up_step_whose_cleanup_fails_stops_the_pairing_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # When files.sha256 cannot be turned off again, the later steps cannot run as written: the
    # cleanup's one need stops the check.
    mac = _SetUpOnAMac(host_turns_off=False, disable_works=False)
    with pytest.raises(check_app._Stop):
        _set_up_step_on(mac, monkeypatch, tmp_path)


@pytest.mark.parametrize(
    ("worked", "said", "seen"), [(True, "1", "1"), (True, "0", "0"), (False, "err", "absent")]
)
def test_the_set_up_step_reads_the_switch_as_system_events_says_it(
    worked: bool, said: str, seen: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies: list[str] = []

    def system_events(body: str, timeout: float = 30) -> tuple[bool, str]:
        bodies.append(body)
        return worked, said

    monkeypatch.setattr(check_app, "_system_events", system_events)
    assert check_app._checked("Check a file's fingerprint") == seen
    assert 'checkbox "Check a file\'s fingerprint" of theWindow' in bodies[0]
    assert bodies[0].endswith("return ((value of theTarget) as text)")


@pytest.mark.parametrize("offered", [True, False])
def test_a_missing_set_up_window_is_described(
    offered: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # CI run 42395410137 failed here with the detail "[]", which could not tell a window that never
    # opened from one that opened and was then closed, nor from a listing that failed. The detail
    # now says whether the menu offers Set Up again (only once the window opened and was closed as
    # a user closes it), every window of the app, the pair and run, and what the app has written on
    # standard error, where it says when Set Up was shown and closed.
    said = "Set Up shows Folders\nSet Up's window was closed, not by the app\n"
    mac = _SetUpOnAMac(opens=False, offered=offered, stderr=said)
    checker = _set_up_step_on(mac, monkeypatch, tmp_path)
    title = check_app.SETUP_TITLE
    assert checker.failures[0] == f"once connected, the app opens {title!r} by itself"
    detail = capsys.readouterr().out.split(f"FAIL: {checker.failures[0]}\n", 1)[1]
    offers = "offers" if offered else "does not offer"
    assert f"the menu {offers} {check_app.SETUP_ITEM!r}: {mac.menu}" in detail
    assert "the app's windows: frontmost true, 0 window(s):" in detail
    assert "pair None, run None" in detail
    assert f"the app's standard error so far: {said!r}" in detail
    assert mac.shots == ["p-setup-missing.png"] and mac.menu_reads == 1


class _CountingSetUpOnAMac(_SetUpOnAMac):
    """As _SetUpOnAMac, counting each read of every text the window and its notes show
    (``text_reads``), and how many there had been when the check read Ready's box
    (``reads_at_ready``). ``says`` reads them without counting."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.text_reads = 0
        self.reads_at_ready: int | None = None

    def texts(self) -> list[str]:
        self.text_reads += 1
        return super().texts()

    def says(self, words: str) -> bool:
        return any(words in text for text in super().texts())

    def checked(self, name: str) -> str:
        if name == check_app.OPEN_AT_LOGIN_BOX and self.reads_at_ready is None:
            self.reads_at_ready = self.text_reads
        return super().checked(name)


@pytest.mark.parametrize("state", ["enabled", "requiresApproval", "notRegistered", "notFound"])
def test_from_ready_on_a_set_up_step_that_passes_builds_no_failure_detail(
    state: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A failure's detail, which reads the screen through System Events, is built only when the
    # check failed. From Ready's box on (what Open at Login added: the notice, Done,
    # what came of opening at login, and the cleanup), a step that passes reads no text for one.
    mac = _CountingSetUpOnAMac(login_state=state)
    checker = _set_up_step_on(mac, monkeypatch, tmp_path)
    assert checker.failures == []
    assert mac.reads_at_ready is not None and mac.text_reads == mac.reads_at_ready


@pytest.mark.parametrize(
    ("state", "closed_with"),
    [
        ("enabled", []),
        ("requiresApproval", ["Not Now"]),
        ("notRegistered", ["OK"]),
        ("notFound", ["OK"]),
    ],
)
def test_after_done_the_set_up_step_reads_the_login_item_and_its_words(
    state: str,
    closed_with: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # What macOS answers an ad-hoc build is not known (decisions/0002 §9), so the step passes on
    # each state, as long as the app says on standard error which it was and its words agree:
    # no note while enabled; the note that macOS needs the user's OK, closed with Not Now, never
    # Open Login Items Settings; or the note that it couldn't, closed with OK. It says once which
    # state it read, and prints macOS's login items once.
    mac = _SetUpOnAMac(login_state=state)
    checker = _set_up_step_on(mac, monkeypatch, tmp_path)
    assert checker.failures == []
    assert mac.note is None and mac.dumps == 1
    after = mac.pressed[mac.pressed.index(check_app.SETUP_DONE) + 1 :]
    assert after == closed_with
    assert not _keys_sent(mac.sent), mac.sent
    said = capsys.readouterr().out
    assert said.count(f"after Done, macOS gives the app's login item the state {state}\n") == 1


def test_a_note_about_opening_at_login_left_up_stops_the_pairing_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A note left up is modal: the later steps' menu reads would find no menu. When the step
    # cannot close it, the cleanup's one need stops the check.
    mac = _SetUpOnAMac(login_state="requiresApproval", note_closes=False)
    made: list[check_app.Checker] = []
    real = check_app.Checker

    def checker() -> check_app.Checker:
        made.append(real())
        return made[-1]

    monkeypatch.setattr(check_app, "Checker", checker)
    with pytest.raises(check_app._Stop):
        _set_up_step_on(mac, monkeypatch, tmp_path)
    assert mac.note == check_app.NEEDS_APPROVAL_TITLE
    # The step pressed Not Now itself and counted that it did not close the note; the cleanup
    # pressed it once more, before its need stopped the check.
    assert made[0].failures[0] == "Not Now closes the note", made[0].failures
    after = mac.pressed[mac.pressed.index(check_app.SETUP_DONE) + 1 :]
    assert after == ["Not Now", "Not Now"]


def test_the_login_item_lines_are_read_as_the_app_writes_them() -> None:
    # The app writes one line, "login item: <state>", each time it acts on its login item or
    # checks it at launch (App/LoginItem.swift); the check reads the last one in what it is given.
    said = "Set Up shows Ready\nlogin item: requiresApproval\nrun exited 0 (drained, stdin_end)\n"
    assert check_app._login_state(said) == "requiresApproval"
    assert check_app._login_state(said + "login item: notRegistered\n") == "notRegistered"
    assert check_app._login_state("login item: enabled") == "enabled"
    for wrong in ("login item:enabled\n", "login item: Enabled\n", "login item: enabled now\n"):
        assert check_app._login_state(wrong) is None, wrong


@pytest.mark.parametrize("trouble", ["missing", "timeout", "refused", "works"])
def test_the_login_items_are_printed_and_never_checked(
    trouble: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `sudo -n sfltool dumpbtm` lists macOS's login and background items. The checks print the
    # records that name the app, for the record; nothing gates on it, and a machine with no
    # password-less sudo, no sfltool, or one that hangs, prints why and goes on.
    bundle_id = build_app.load_pins().bundle_id
    record = f" #3:\n                 UUID: x\n           Identifier: 2.{bundle_id}\n"
    # A record that names the app by its path only, as macOS's list may for the app itself.
    by_path = f" #5:\n                 URL: file:///Applications/{check_app.APP_NAME}.app/\n"
    other = " #4:\n                 UUID: y\n           Identifier: 2.com.example.other\n"
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if trouble == "missing":
            raise FileNotFoundError(2, "No such file or directory", "sudo")
        if trouble == "timeout":
            raise subprocess.TimeoutExpired(argv, 60)
        if trouble == "refused":
            return subprocess.CompletedProcess(argv, 1, "", "sudo: a password is required\n")
        return subprocess.CompletedProcess(argv, 0, f"========\n{record}\n{other}\n{by_path}\n", "")

    monkeypatch.setattr("subprocess.run", run)
    check_app._dump_login_items()
    said = capsys.readouterr().out
    assert calls == [["sudo", "-n", "sfltool", "dumpbtm"]]
    if trouble in ("missing", "timeout"):
        assert "sudo -n sfltool dumpbtm did not run" in said
    elif trouble == "refused":
        assert "exited 1: 0 record(s) name the app" in said and "a password is required" in said
    else:
        assert "exited 0: 2 record(s) name the app" in said
        assert bundle_id in said and "com.example.other" not in said
        assert f"/Applications/{check_app.APP_NAME}.app/" in said
    named = check_app._login_items_naming(f"{record}\n{other}", (bundle_id,))
    assert [block.strip() for block in named] == [record.strip()]
    source = _CHECK_APP.read_text(encoding="utf-8")
    dump = _python_function(source, "_dump_login_items")
    assert "checker" not in dump and "expect(" not in dump and "need(" not in dump
    assert dump.count("subprocess.run(") == 1 and "timeout=" in dump


class _LaunchOnAMac:
    """The app as System Events sees it once ``open`` has started it, for running the pairing
    check's launch here. It shows the note that says where its menu is at launch only while no
    confirmed pair is held (``paired`` false), or, with ``welcome_when_paired``, always. Paired,
    it writes ``login item: <state>`` on standard error once it has checked its login item
    (unless ``login_line`` is false), and, with the item in requiresApproval, shows the note that
    it won't start at login (unless ``login_note`` is false; ``note_anyway`` shows it whatever the
    state), whose Keep It Off closes it (unless ``keep_it_off_closes`` is false) and turns the
    item off, so that the app says ``login item: <keep_it_off_line>`` (nothing, and no change,
    when ``keep_it_off_works`` is false). Every body sent to System Events is kept in ``sent``.
    Its words are read only at run time: an instance is made when this module is imported."""

    def __init__(
        self,
        *,
        paired: bool,
        state: str = "enabled",
        welcome_when_paired: bool = False,
        login_line: bool = True,
        login_note: bool = True,
        note_anyway: bool = False,
        keep_it_off_works: bool = True,
        keep_it_off_closes: bool = True,
        keep_it_off_line: str = "notRegistered",
    ) -> None:
        self.paired = paired
        self.state = state
        self.welcome_when_paired = welcome_when_paired
        self.login_line = login_line
        self.login_note = login_note
        self.note_anyway = note_anyway
        self.keep_it_off_works = keep_it_off_works
        self.keep_it_off_closes = keep_it_off_closes
        self.keep_it_off_line = keep_it_off_line
        self.opened = False
        self.log: Path | None = None
        # The notes up, the front one first; every button pressed; every body sent to System
        # Events; each screenshot.
        self.notes: list[str] = []
        self.pressed: list[str] = []
        self.sent: list[str] = []
        self.shots: list[str] = []

    def open(self, err: Path) -> None:
        self.opened = True
        self.log = err
        if not self.paired or self.welcome_when_paired:
            self.notes.append(check_app.WELCOME_TITLE)
        if not self.paired:
            return
        if self.login_line:
            self.write(f"login item: {self.state}\n")
        if self.note_anyway or (self.state == "requiresApproval" and self.login_note):
            self.notes.append(check_app.LAUNCH_OFF_TITLE)

    def write(self, line: str) -> None:
        assert self.log is not None
        self.log.write_text(self.log.read_text(encoding="utf-8") + line, encoding="utf-8")

    def texts(self) -> list[str]:
        """The front note's words: Alerts shows one note at a time."""
        if not self.notes:
            return []
        if self.notes[0] == check_app.WELCOME_TITLE:
            return [
                check_app.WELCOME_TITLE,
                "Click it, up by the clock, to see it.",
                check_app.PURPOSE,
            ]
        return [check_app.LAUNCH_OFF_TITLE, "Opening at login is turned off in System Settings."]

    def says(self, words: str) -> bool:
        return any(words in text for text in self.texts())

    def window_list(self, what: str) -> tuple[bool, list[str]]:
        return True, self.texts()

    def system_events(self, body: str, timeout: float = 30) -> tuple[bool, str]:
        self.sent.append(body)
        if body == 'click button "OK" of window 1' and self.notes[:1] == [check_app.WELCOME_TITLE]:
            self.pressed.append("OK")
            self.notes.pop(0)
            return True, ""
        return False, f"not on a Mac: {body!r}"

    def press(self, name: str) -> tuple[bool, str]:
        self.pressed.append(name)
        if name == check_app.KEEP_IT_OFF and self.notes[:1] == [check_app.LAUNCH_OFF_TITLE]:
            if self.keep_it_off_closes:
                self.notes.pop(0)
            if self.keep_it_off_works:
                self.state = self.keep_it_off_line
                self.write(f"login item: {self.keep_it_off_line}\n")
            return True, "pressed"
        return False, f"no button named {name}"


def _launch_check(
    mac: _LaunchOnAMac, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> check_app._PairingCheck:
    """A _PairingCheck whose launches open ``mac``."""

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == "open":
            mac.open(Path(argv[argv.index("--stderr") + 1]))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(check_app, "_running", lambda name: ["4242"] if mac.opened else [])
    monkeypatch.setattr(check_app, "_can_drive", lambda: True)
    monkeypatch.setattr(check_app, "_wait_until", lambda test, seconds: test())
    monkeypatch.setattr(check_app, "_value_within", lambda get, seconds: get())
    monkeypatch.setattr(check_app.time, "sleep", lambda seconds: None)
    for name, fake in (
        ("_window_list", mac.window_list),
        ("_system_events", mac.system_events),
        ("_texts", mac.texts),
        ("_says", mac.says),
        ("_press", mac.press),
    ):
        monkeypatch.setattr(check_app, name, fake)
    monkeypatch.setattr(check_app, "_screenshot", lambda path: mac.shots.append(path.name))
    check = object.__new__(check_app._PairingCheck)
    check.checker = check_app.Checker()
    check.screens = tmp_path
    check.work = tmp_path
    check.app = tmp_path / f"{check_app.APP_NAME}.app"
    check.logs = []
    check.require_driven = True
    return check


def _launch_on(
    mac: _LaunchOnAMac,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    label: str,
    *,
    paired: bool,
) -> check_app.Checker:
    """Run _PairingCheck.launch(label, paired=paired) against ``mac``."""
    check = _launch_check(mac, monkeypatch, tmp_path)
    check.launch(label, paired=paired)
    return check.checker


class _CountingLaunchOnAMac(_LaunchOnAMac):
    """As _LaunchOnAMac, counting each read of every text the notes show (``text_reads``): a
    failure's detail reads them, and a launch that passes builds none. ``says`` reads the notes
    without counting, as System Events' own test would."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.text_reads = 0

    def texts(self) -> list[str]:
        self.text_reads += 1
        return super().texts()

    def says(self, words: str) -> bool:
        return any(words in text for text in super().texts())


@pytest.mark.parametrize("state", ["enabled", "requiresApproval", "notRegistered"])
def test_a_paired_launch_that_passes_builds_no_failure_detail(
    state: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A failure's detail, which reads the screen through System Events, is built only when the
    # check failed. A paired launch that passes reads no text for one, with
    # or without the note about opening at login.
    mac = _CountingLaunchOnAMac(paired=True, state=state)
    checker = _launch_on(mac, monkeypatch, tmp_path, "relaunched", paired=True)
    assert checker.failures == []
    assert mac.text_reads == 0


class _PausedLaunchOnAMac(_LaunchOnAMac):
    """As _LaunchOnAMac, with the host paused: the note that says so comes after the notes the
    app shows at launch (Alerts shows one at a time, in order), and its Resume resumes the host
    (``paused``)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.paused = True

    def open(self, err: Path) -> None:
        super().open(err)
        self.notes.append(check_app.PAUSED_TITLE)

    def texts(self) -> list[str]:
        if self.notes[:1] == [check_app.PAUSED_TITLE]:
            return [check_app.PAUSED_TITLE]
        return super().texts()

    def enabled(self, name: str) -> str:
        front = self.notes[:1] == [check_app.PAUSED_TITLE]
        return "true" if front and name == check_app.RESUME_BUTTON else "absent"

    def press(self, name: str) -> tuple[bool, str]:
        if name == check_app.RESUME_BUTTON and self.notes[:1] == [check_app.PAUSED_TITLE]:
            self.pressed.append(name)
            self.notes.pop(0)
            self.paused = False
            return True, "pressed"
        return super().press(name)


@pytest.mark.parametrize("state", ["enabled", "requiresApproval"])
def test_the_paused_launch_finds_the_paused_note_behind_the_note_about_opening_at_login(
    state: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # At a paired launch, the first status read checks the login item, and queues its note when
    # macOS waits for the user's OK, before afterWelcome queues the paused note; the app shows one
    # note at a time. The check chooses Keep It Off on the first, then finds the paused note, and
    # Resume resumes the host. It sends no key.
    mac = _PausedLaunchOnAMac(paired=True, state=state)
    check = _launch_check(mac, monkeypatch, tmp_path)
    monkeypatch.setattr(check_app, "_enabled", mac.enabled)
    check.status = lambda: {"paused": mac.paused}  # type: ignore[method-assign]
    check.paused_at_launch()
    assert check.checker.failures == []
    keep = ["Keep It Off"] if state == "requiresApproval" else []
    assert mac.pressed == [*keep, check_app.RESUME_BUTTON]
    assert mac.notes == [] and mac.paused is False and mac.sent == []


def test_an_unpaired_launch_reads_the_note_about_the_menu_bar_and_chooses_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # With no confirmed pair, the app says where its menu is at launch, as before
    # (decisions/0002, What is built today).
    mac = _LaunchOnAMac(paired=False)
    checker = _launch_on(mac, monkeypatch, tmp_path, "pairing", paired=False)
    assert checker.failures == []
    assert mac.pressed == ["OK"] and mac.notes == []
    assert "p-pairing-welcome.png" in mac.shots


@pytest.mark.parametrize("state", ["enabled", "notRegistered", "notFound"])
def test_a_paired_launch_shows_no_note_and_the_check_presses_nothing(
    state: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Paired, the app goes straight on: no note about its menu bar, and none about opening at
    # login unless macOS says it needs the user's OK. The check sends nothing to a note it has not
    # seen: no OK, no key. It says once which state it read.
    mac = _LaunchOnAMac(paired=True, state=state)
    checker = _launch_on(mac, monkeypatch, tmp_path, "relaunched", paired=True)
    assert checker.failures == []
    assert mac.pressed == [] and mac.notes == []
    # Nothing at all goes to System Events: no click, no key press. (What the check reads, it
    # reads through _says and _texts.)
    assert mac.sent == []
    said = capsys.readouterr().out
    line = f"at launch (relaunched), macOS gives the app's login item the state {state}\n"
    assert said.count(line) == 1


def test_a_paired_launch_with_opening_at_login_off_keeps_it_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The launch note (decisions/0002 §3.3, amended): "<name> won't start when you log in", with
    # Open Login Items Settings and Keep It Off. The check chooses Keep It Off, which takes the
    # item away, so the later launches show no note; it never opens System Settings.
    mac = _LaunchOnAMac(paired=True, state="requiresApproval")
    checker = _launch_on(mac, monkeypatch, tmp_path, "relaunched", paired=True)
    assert checker.failures == []
    assert mac.pressed == ["Keep It Off"] and mac.notes == [] and mac.state == "notRegistered"
    assert "p-relaunched-login-off.png" in mac.shots
    assert mac.sent == []


def test_keep_it_off_may_leave_the_login_item_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An item macOS no longer finds is gone too: the app writes the state macOS gives, and
    # notFound is one way it says there is none.
    mac = _LaunchOnAMac(paired=True, state="requiresApproval", keep_it_off_line="notFound")
    checker = _launch_on(mac, monkeypatch, tmp_path, "relaunched", paired=True)
    assert checker.failures == []
    assert mac.pressed == ["Keep It Off"] and mac.notes == [] and mac.state == "notFound"


def test_a_note_keep_it_off_leaves_up_stops_the_pairing_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The app shows one note at a time: left up, the launch note would hold the next step's note
    # behind it, so the steps after this one cannot run, and the check stops here, saying why.
    mac = _LaunchOnAMac(paired=True, state="requiresApproval", keep_it_off_closes=False)
    with pytest.raises(check_app._Stop, match="Keep It Off closes the note"):
        _launch_on(mac, monkeypatch, tmp_path, "relaunched", paired=True)
    assert mac.pressed == ["Keep It Off"] and mac.sent == []


def test_a_note_about_the_menu_bar_at_a_paired_launch_is_counted_then_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # That rule broken: the note shows at a paired launch. That is counted, and the check, which
    # has just read the note, closes it with OK, as at an unpaired launch: left up, it would hold
    # back the paused note and a pairing left unfinished, and every later step with them.
    mac = _LaunchOnAMac(paired=True, welcome_when_paired=True)
    checker = _launch_on(mac, monkeypatch, tmp_path, "relaunched", paired=True)
    assert len(checker.failures) == 1 and "shows no note about its menu bar" in checker.failures[0]
    assert mac.pressed == ["OK"] and mac.notes == []
    assert mac.sent == ['click button "OK" of window 1']


@pytest.mark.parametrize(
    ("mac", "first_failure"),
    [
        (_LaunchOnAMac(paired=True, welcome_when_paired=True), "shows no note about its menu bar"),
        (_LaunchOnAMac(paired=True, login_line=False), "which state macOS gives its login item"),
        (
            _LaunchOnAMac(paired=True, state="requiresApproval", login_note=False),
            "won't start when you log in",
        ),
        (_LaunchOnAMac(paired=True, note_anyway=True), "no note about opening at login"),
        (
            _LaunchOnAMac(paired=True, state="requiresApproval", keep_it_off_works=False),
            "Keep It Off turns",
        ),
        (
            _LaunchOnAMac(
                paired=True, state="requiresApproval", keep_it_off_line="requiresApproval"
            ),
            "Keep It Off turns",
        ),
    ],
)
def test_a_paired_launch_that_goes_wrong_is_counted(
    mac: _LaunchOnAMac, first_failure: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checker = _launch_on(mac, monkeypatch, tmp_path, "relaunched", paired=True)
    assert checker.failures and first_failure in checker.failures[0], checker.failures
    # A note the check did not expect is left alone: it presses only Keep It Off, on its note,
    # and OK only on the note about the menu bar it has just read; it sends no key.
    pressed = {"Keep It Off", "OK"} if mac.welcome_when_paired else {"Keep It Off"}
    assert set(mac.pressed) <= pressed
    assert set(mac.sent) <= {'click button "OK" of window 1'}


def test_each_launch_of_the_pairing_check_says_whether_it_is_paired() -> None:
    # Only the first launch holds no confirmed pair (the harness left the SAS pending): it alone
    # expects the note about the menu bar. Every later one is paired. Opened again while it runs,
    # the app shows that note paired or not (welcome_again, which backoff and quit_and_drain use).
    source = _CHECK_APP.read_text(encoding="utf-8")
    calls = re.findall(r"self\.launch\(([^)]*)\)", source)
    assert calls == [
        '"pairing", paired=False',
        '"relaunched", paired=True',
        '"stop-whats-running", paired=True',
        '"too-old", paired=True',
        '"stop-now", paired=True',
        '"paused", paired=True',
    ]
    launch = _python_function(source, "launch")
    assert "def launch(self, label: str, *, paired: bool) -> None:" in launch
    unpaired = launch.index("if not paired:")
    assert unpaired < launch.index("_welcome(") < launch.index("self.login_at_launch(label)")
    assert "_says(WELCOME_TITLE)" in launch
    at_launch = _python_function(source, "login_at_launch")
    assert "_press(KEEP_IT_OFF)" in at_launch and "LAUNCH_OFF_TITLE" in at_launch
    assert at_launch.index('if state != "requiresApproval":') < at_launch.index(
        "_press(KEEP_IT_OFF)"
    )
    again = _python_function(source, "welcome_again")
    assert "_says(WELCOME_TITLE)" in again
    paused = _python_function(source, "paused_at_launch")
    assert paused.index('self.launch("paused", paired=True)') < paused.index("PAUSED_TITLE")


class _DisconnectOnAMac:
    """The app, its menu and the host as the pairing check's Disconnect sees them, for running
    that step here. Before Disconnect the app has said ``login item: <state>`` (nothing when
    ``state`` is None). Disconnect drains run, forgets the pair, takes the login item away and
    says the state macOS then gives, as the app does: ``notFound`` when it had none to find,
    else ``notRegistered`` (nothing when ``line`` is false; ``still`` when unregistering failed,
    and then the note says how to turn it off by hand). Its note adds that the app won't open at
    login when ``sentence`` says so (by default, exactly when the item was enabled or waited for
    approval, and was taken away). While connected the menu holds Settings (unless ``settings``
    is false); once disconnected it does not (unless ``settings_after``)."""

    def __init__(
        self,
        *,
        state: str | None = "enabled",
        sentence: bool | None = None,
        line: bool = True,
        settings: bool = True,
        settings_after: bool = False,
        still: str | None = None,
    ) -> None:
        self.state = state
        registered = state in ("enabled", "requiresApproval") and still is None
        self.sentence = registered if sentence is None else sentence
        self.line = line
        self.still = still
        self.settings = settings
        self.settings_after = settings_after
        self.asked = False
        self.disconnected = False
        self.told = False
        self.log: Path | None = None
        self.pressed: list[str] = []
        self.dumps = 0

    def menu_until(
        self, test: Callable[[list[str]], bool], seconds: float, shot: Path | None = None
    ) -> tuple[bool, list[str]]:
        if self.disconnected:
            shown = [check_app.NOT_CONNECTED, check_app.CONNECT_ITEM]
            shown += [check_app.SETTINGS_MENU] if self.settings_after else []
        else:
            shown = [check_app.CONNECTED, check_app.DISCONNECT_ITEM]
            shown += [check_app.SETTINGS_MENU] if self.settings else []
        return test(shown), shown

    def choose(self, start: str) -> tuple[bool, str]:
        if not self.disconnected and check_app.DISCONNECT_ITEM.startswith(start):
            self.asked = True
            return True, "chosen"
        return False, f"no item {start}"

    def texts(self) -> list[str]:
        if self.asked:
            return [check_app.DISCONNECT_TITLE]
        if self.told:
            said = check_app.DISCONNECTED_TOLD
            if self.sentence:
                said += " " + check_app.OFF_AFTER_DISCONNECT
            return [check_app.DISCONNECTED_TOLD.removesuffix(" Lucille was told."), said]
        return []

    def says(self, words: str) -> bool:
        return any(words in text for text in self.texts())

    def press(self, name: str) -> tuple[bool, str]:
        self.pressed.append(name)
        if name == check_app.DISCONNECT_BUTTON and self.asked:
            self.asked = False
            self.disconnected = True
            self.told = True
            assert self.log is not None
            said = self.log.read_text(encoding="utf-8") + check_app.DRAINED_LINE + "\n"
            if self.line:
                gone = "notFound" if self.state == "notFound" else "notRegistered"
                said += f"login item: {self.still or gone}\n"
            self.log.write_text(said, encoding="utf-8")
            return True, "pressed"
        if name == "OK" and self.told:
            self.told = False
            return True, "pressed"
        return False, f"no button named {name}"

    def dump(self) -> None:
        self.dumps += 1


def _disconnect_on(
    mac: _DisconnectOnAMac, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> check_app.Checker:
    """Run _PairingCheck.disconnect against ``mac``."""
    for name, fake in (
        ("_menu_until", mac.menu_until),
        ("_choose", mac.choose),
        ("_texts", mac.texts),
        ("_says", mac.says),
        ("_press", mac.press),
    ):
        monkeypatch.setattr(check_app, name, fake)
    monkeypatch.setattr(check_app, "_screenshot", lambda path: None)
    monkeypatch.setattr(check_app, "_wait_until", lambda test, seconds: test())
    monkeypatch.setattr(check_app, "_value_within", lambda get, seconds: get())
    monkeypatch.setattr(check_app, "_alive", lambda pid: not mac.disconnected)
    monkeypatch.setattr(check_app, "_dump_login_items", mac.dump, raising=False)
    check = object.__new__(check_app._PairingCheck)
    check.checker = check_app.Checker()
    check.screens = tmp_path
    stderr = tmp_path / "pairing-paused-stderr.txt"
    stderr.write_text("" if mac.state is None else f"login item: {mac.state}\n", encoding="utf-8")
    mac.log = stderr
    check.logs = [stderr]
    check.run_pid = lambda: None if mac.disconnected else 42  # type: ignore[method-assign]
    check.pair_state = lambda: "none" if mac.disconnected else "confirmed"  # type: ignore[method-assign]
    check.disconnect()
    return check.checker


@pytest.mark.parametrize("state", ["enabled", "requiresApproval", "notRegistered", "notFound"])
def test_disconnect_takes_the_login_item_away_and_says_so_when_there_was_one(
    state: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # decisions/0002 §9: the pair gone, the login item goes. The app says
    # "login item: notRegistered" on standard error whatever it had (notFound, when macOS had
    # none to find); its note adds
    # "<name> won't open when you log in." exactly when it had one, by the last line it
    # wrote before (the paused launch's check). The menu holds Settings while connected, and not
    # after.
    mac = _DisconnectOnAMac(state=state)
    checker = _disconnect_on(mac, monkeypatch, tmp_path)
    assert checker.failures == []
    assert mac.pressed == [check_app.DISCONNECT_BUTTON, "OK"] and mac.dumps == 1


@pytest.mark.parametrize(
    ("mac", "first_failure"),
    [
        (_DisconnectOnAMac(state="enabled", sentence=False), "exactly when the login item"),
        (_DisconnectOnAMac(state="notRegistered", sentence=True), "exactly when the login item"),
        (_DisconnectOnAMac(line=False), "login item: notRegistered"),
        # The paused launch's own line is not what Disconnect gave: only the lines after it count.
        (_DisconnectOnAMac(state="notRegistered", line=False), "login item: notRegistered"),
        (_DisconnectOnAMac(state=None), "before Disconnect, the app has said"),
        (_DisconnectOnAMac(settings=False), "while connected, the menu holds"),
        (_DisconnectOnAMac(settings_after=True), "no longer holds"),
    ],
)
def test_a_disconnect_that_leaves_the_login_item_wrong_is_counted(
    mac: _DisconnectOnAMac, first_failure: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checker = _disconnect_on(mac, monkeypatch, tmp_path)
    assert checker.failures and first_failure in checker.failures[0], checker.failures


def test_a_disconnect_that_leaves_the_login_item_registered_is_counted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Unregistering failed: macOS still has the item, the app says so on standard error and adds
    # no "won't open" sentence. Both are counted.
    mac = _DisconnectOnAMac(state="enabled", still="enabled")
    checker = _disconnect_on(mac, monkeypatch, tmp_path)
    assert any("exactly when the login item" in failure for failure in checker.failures)
    assert any("login item: notRegistered" in failure for failure in checker.failures)


def test_the_disconnect_step_reads_the_login_item_before_and_after() -> None:
    source = _CHECK_APP.read_text(encoding="utf-8")
    step = _python_function(source, "disconnect")
    was = step.index("was = _login_state(self.stderr_now())")
    chosen = step.index('_choose(DISCONNECT_ITEM.removesuffix("…"))')
    mark = step.index("before = len(self.stderr_now())")
    pressed = step.index("_press(DISCONNECT_BUTTON)")
    sentence = step.index("_says(OFF_AFTER_DISCONNECT)")
    after = step.index("_login_state(self.stderr_now()[before:])")
    assert was < chosen < mark < pressed < sentence < after
    assert 'registered = was in ("enabled", "requiresApproval")' in step
    assert step.index("SETTINGS_MENU in shown") < chosen
    assert step.rindex("SETTINGS_MENU not in shown") > after
    assert "_dump_login_items()" in step[after:]


def _welcome_world(monkeypatch: pytest.MonkeyPatch, *, welcome_at_launch: bool) -> list[str]:
    """The app as the ui check sees it once check_ui has opened it: the note about its menu bar
    up when ``welcome_at_launch``, and up again whenever ``open`` opens it while it runs. Returns
    the notes read with _welcome, by when."""
    welcomed: list[str] = []
    up = {"welcome": welcome_at_launch}

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == "open":
            up["welcome"] = True
        return subprocess.CompletedProcess(argv, 0, "", "")

    def welcome(checker: check_app.Checker, shot: Path, when: str) -> None:
        checker.expect(up["welcome"], f"{when}, the app says where its menu is")
        up["welcome"] = False
        welcomed.append(when)

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(check_app, "_welcome", welcome)
    monkeypatch.setattr(
        check_app, "_says", lambda words: words == check_app.WELCOME_TITLE and up["welcome"]
    )
    monkeypatch.setattr(
        check_app, "_texts", lambda: [check_app.WELCOME_TITLE] if up["welcome"] else []
    )
    monkeypatch.setattr(check_app, "_wait_until", lambda test, seconds: test())
    monkeypatch.setattr(check_app, "_screenshot", lambda path: None)
    return welcomed


@pytest.mark.parametrize(("paired", "at_launch"), [(False, True), (True, False), (True, True)])
def test_the_ui_check_reads_the_note_at_launch_only_while_unpaired(
    paired: bool, at_launch: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The ui check opens the app with whatever pair the profile holds (none, on CI): unpaired, the
    # note about the menu bar comes at launch; paired, it must not. Opened again while it runs,
    # the note comes either way.
    welcomed = _welcome_world(monkeypatch, welcome_at_launch=at_launch)
    reads: list[int] = []
    texts = check_app._texts
    monkeypatch.setattr(check_app, "_texts", lambda: reads.append(1) or texts())
    checker = check_app.Checker()
    check_app._drive_welcome(checker, tmp_path, tmp_path / "x.app", paired=paired)
    # A failure's detail reads the screen only when it failed.
    assert len(reads) == (1 if paired and at_launch else 0)
    if paired:
        assert welcomed == ["when it is opened again"]
        assert bool(checker.failures) == at_launch, checker.failures
        if at_launch:
            assert "shows no note about its menu bar" in checker.failures[0]
    else:
        assert welcomed == ["when it opens", "when it is opened again"]
        assert checker.failures == []
    source = _CHECK_APP.read_text(encoding="utf-8")
    ui = _python_function(source, "check_ui")
    asked = ui.index('paired = _pair_state(python_in(app)) == "confirmed"')
    assert (
        asked
        < ui.index('["open", "--stdout"')
        < ui.index("_drive_welcome(checker, screens, app, paired=paired)")
    )


@pytest.mark.parametrize(("state", "paired"), [("confirmed", True), ("none", False), (None, False)])
def test_check_ui_reads_the_pair_before_it_opens_the_app_and_says_which(
    state: str | None,
    paired: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # check_ui itself, run against a stand-in Mac as far as the note about the menu bar: the
    # pair is read before ``open``, said once, and the note is driven for that pair state; a
    # status that can't be read counts as unpaired.
    events: list[str] = []
    running = {"app": False}

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        events.append(argv[0])
        if argv[0] == "open":
            running["app"] = True
        if argv[0] == "pkill":
            running["app"] = False
        return subprocess.CompletedProcess(argv, 0, "", "")

    def pair_state(python: Path) -> str | None:
        events.append("pair")
        return state

    driven: list[bool] = []

    def drive_welcome(
        checker: check_app.Checker, screens: Path, app: Path, *, paired: bool
    ) -> None:
        driven.append(paired)

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(check_app, "_pair_state", pair_state)
    monkeypatch.setattr(check_app, "_profile_folder", lambda: tmp_path / "profile")
    monkeypatch.setattr(check_app, "_stop_earlier_copies", lambda checker, name: True)
    monkeypatch.setattr(check_app, "_running", lambda name: ["4242"] if running["app"] else [])
    monkeypatch.setattr(check_app, "_wait_until", lambda test, seconds: test())
    monkeypatch.setattr(check_app.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(check_app, "_can_drive", lambda: True)
    monkeypatch.setattr(check_app, "_drive_welcome", drive_welcome)
    monkeypatch.setattr(check_app, "_screenshot", lambda path: None)
    monkeypatch.setattr(check_app, "_drive_menu", lambda checker, screens, flag: False)
    checker = check_app.Checker()
    check_app.check_ui(tmp_path / f"{check_app.APP_NAME}.app", checker, tmp_path)
    assert checker.failures == []
    assert driven == [paired]
    assert events.index("pair") < events.index("open")
    said = capsys.readouterr().out
    assert said.count("the app's profile holds a confirmed pair: ") == 1
    assert f"the app's profile holds a confirmed pair: {paired}\n" in said


@pytest.mark.parametrize(("paired", "welcomes"), [(False, True), (True, False), (True, True)])
def test_the_placement_control_expects_the_note_only_while_unpaired(
    paired: bool,
    welcomes: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The control copy, opened, says where its menu is only when the profile holds no confirmed
    # pair (decisions/0002, What is built today); paired, it says neither that nor where it
    # should be.
    installed = tmp_path / "Applications" / f"{check_app.APP_NAME}.app"
    monkeypatch.setattr(check_app, "installed_app_path", lambda: installed)
    _placement_world(tmp_path, monkeypatch, starts=True, writes=False)
    monkeypatch.setattr(check_app, "_can_drive", lambda: True)
    monkeypatch.setattr(check_app, "_pair_state", lambda python: "confirmed" if paired else "none")
    monkeypatch.setattr(
        check_app, "_says", lambda words: words == check_app.WELCOME_TITLE and welcomes
    )
    reads: list[int] = []

    def texts() -> list[str]:
        reads.append(1)
        return [check_app.WELCOME_TITLE] if welcomes else []

    monkeypatch.setattr(check_app, "_texts", texts)
    checker = check_app.Checker()
    assert check_app._placement_control(checker, tmp_path, True) is True
    assert bool(checker.failures) == (paired == welcomes), checker.failures
    # The failure's detail reads the screen only when it failed.
    assert len(reads) == len(checker.failures)
    # Said once, so the run's log shows which way the control went.
    said = capsys.readouterr().out
    assert said.count(f"the app's profile holds a confirmed pair: {paired}\n") == 1
    control = _python_function(_CHECK_APP.read_text(encoding="utf-8"), "_placement_control")
    asked = control.index('paired = _pair_state(python_in(installed)) == "confirmed"')
    assert asked < control.index('["open", "--stdout", str(out_log), "--stderr", str(err_log)')


@pytest.mark.parametrize("paired", [False, True])
def test_the_placement_control_fails_when_it_says_where_it_should_be(
    paired: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The copy in /Applications must never say where it should be, paired or not, though it
    # says, or not, where its menu is as it should.
    installed = tmp_path / "Applications" / f"{check_app.APP_NAME}.app"
    monkeypatch.setattr(check_app, "installed_app_path", lambda: installed)
    _placement_world(tmp_path, monkeypatch, starts=True, writes=False)
    monkeypatch.setattr(check_app, "_can_drive", lambda: True)
    monkeypatch.setattr(check_app, "_pair_state", lambda python: "confirmed" if paired else "none")
    shown = {check_app.PLACEMENT_TITLE} | (set() if paired else {check_app.WELCOME_TITLE})
    monkeypatch.setattr(check_app, "_says", lambda words: words in shown)
    monkeypatch.setattr(check_app, "_texts", lambda: sorted(shown))
    checker = check_app.Checker()
    check_app._placement_control(checker, tmp_path, True)
    assert len(checker.failures) == 1, checker.failures
    assert check_app.PLACEMENT_TITLE in checker.failures[0]


class _MenuOnAMac:
    """The status menu and Set Up's window as System Events drives them, as far as CI run
    42395410137 (macos-15) shows, for running the pairing check's menu read once Connected here.

    - A click on the status item opens its menu, or closes it while it is open. A click that comes
      right after an Escape closed the menu, with no wait between, opens nothing: every screenshot
      that run took so shows no menu.
    - An Escape while the menu is open closes it; with ``escape_closes_menu`` false it does
      nothing, the other reading of those screenshots. With no menu open it goes to the app's key
      window, Set Up's, and closes it, as that run's p05 screenshot and the empty window lists
      after it show. (That AppKit's NSPanel closes on Escape is believed, not checked on a Mac.)
    - With ``opens_on_menu``, Set Up's window opens only once the menu first opens, and taking the
      focus ends the menu: the order the check must survive if the app is slow to say Connected.
    """

    def __init__(self, *, escape_closes_menu: bool = True, opens_on_menu: bool = False) -> None:
        self.escape_closes_menu = escape_closes_menu
        self.opens_on_menu = opens_on_menu
        self.menu_open = False
        self.set_up_open = not opens_on_menu
        self.set_up_shown = self.set_up_open
        self.just_closed = False
        # Each Escape that went with no menu open, and what it met.
        self.stray: list[str] = []
        # Each screenshot: whether the menu, and Set Up's window, were open.
        self.shots: dict[str, tuple[bool, bool]] = {}

    def click(self, right_after_escape: bool) -> None:
        if self.menu_open:
            self.menu_open = False
        elif not right_after_escape:
            self.menu_open = True
            if not self.set_up_shown:
                self.set_up_shown = self.set_up_open = True
                self.menu_open = False

    def escape(self) -> None:
        if not self.menu_open:
            self.stray.append("Set Up's window, which it closed" if self.set_up_open else "none")
            self.set_up_open = False
        elif self.escape_closes_menu:
            self.menu_open = False
            self.just_closed = True

    def system_events(self, body: str, timeout: float = 30) -> tuple[bool, str]:
        right_after_escape, self.just_closed = self.just_closed, False
        lines = body.splitlines()
        if lines[0] == f"click {check_app.STATUS_ITEM}":
            self.click(right_after_escape)
            if not any("name of every menu item of" in line for line in lines):
                return True, ""
            if not self.menu_open:
                return False, "execution error: System Events got an error: Can't get menu 1."
            return True, f"{check_app.APP_NAME} 0.1.0\n{check_app.CONNECTED}"
        if "key code 53" in lines:
            # System Events still found the menu after every one of 11 Escapes in CI run
            # 42402770764, while the next click opened it as usual, so its ``exists`` is taken to
            # find the menu whether it is open or not: the script's guard never holds an Escape back
            # here, and only when the check sends it keeps it from Set Up's window.
            guarded = f"exists {check_app.MENU}" in lines[0]
            self.escape()
            if any(line.startswith("delay") for line in lines[lines.index("key code 53") :]):
                self.just_closed = False
            return True, "Escape sent" if guarded else ""
        if body == f"{check_app.LINES}return (name of every window) as text":
            return True, check_app.SETUP_TITLE if self.set_up_open else ""
        if check_app.WINDOWS_HOLD in body:
            return True, f"frontmost true, {int(self.set_up_open)} window(s):"
        raise AssertionError(f"not modelled: {body!r}")

    def screenshot(self, path: Path) -> None:
        self.shots[path.name] = (self.menu_open, self.set_up_open)

    def wait(self, seconds: float) -> None:
        self.just_closed = False


@pytest.mark.parametrize(
    "mac",
    [
        _MenuOnAMac(),
        _MenuOnAMac(escape_closes_menu=False),
        _MenuOnAMac(opens_on_menu=True),
    ],
    ids=["escape-closes-the-menu", "escape-leaves-the-menu", "set-up-opens-with-the-menu"],
)
def test_the_menu_read_once_connected_leaves_set_up_open(
    mac: _MenuOnAMac, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # CI run 42395410137 (macos-15): Set Up's window was up on Folders in the screenshot the check
    # took to show the menu saying Connected, which showed no menu, and it was gone at every look
    # for it in the next 60 seconds; between the two the check sent only the Escape after that
    # screenshot. Here run_reads_the_room runs against a Mac that does what that run's screenshots
    # show, with either reading of why the click showed no menu, and with Set Up opening only as
    # the menu opens. Afterwards Set Up's window is still open, no Escape went with no menu open,
    # and the screenshot shows the menu open over the window.
    import time

    clock = time.monotonic
    check = object.__new__(check_app._PairingCheck)
    check.checker = check_app.Checker()
    check.screens = tmp_path
    check.host_env = {}
    check.lucille = types.SimpleNamespace(offer=lambda room: None)
    check.run_pid = lambda: 4242  # type: ignore[method-assign]
    check.app_pid = lambda: 4241  # type: ignore[method-assign]
    check.reading = lambda: True  # type: ignore[method-assign]
    check.host_argv = lambda *args: list(args)  # type: ignore[method-assign]
    refused = [{"v": 1, "type": "stopped", "reason": "already_running"}]
    check.start = lambda argv, *, env, piped: types.SimpleNamespace(  # type: ignore[method-assign]
        wait=lambda: 21, objects=lambda: refused, describe=lambda: ""
    )
    command = (
        f"python -m lucille_host --profile {check_app.APP_PROFILE} run --json-events "
        "--exit-on-stdin-eof"
    )

    def lsof(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        held = "p4242\nf0\ntPIPE\n" if "-d" in argv else ""
        return subprocess.CompletedProcess(argv, 0, held, "")

    for name, fake in (
        ("_system_events", mac.system_events),
        ("_screenshot", mac.screenshot),
        ("_value_within", lambda get, seconds: get()),
        ("_wait_until", lambda test, seconds: test()),
        ("_parent", lambda pid: 4241),
        ("_ps", lambda pid, field: command),
        ("_alive", lambda pid: True),
        ("subprocess", types.SimpleNamespace(run=lsof)),
        ("time", types.SimpleNamespace(monotonic=clock, sleep=mac.wait)),
    ):
        monkeypatch.setattr(check_app, name, fake)
    check.run_reads_the_room()
    assert check.checker.failures == []
    assert mac.stray == []
    assert check_app._window_names() == [check_app.SETUP_TITLE]
    assert mac.shots == {"p05-connected.png": (True, True)}


@pytest.mark.parametrize(
    "read",
    [
        (True, f"{check_app.APP_NAME} 0.1.0\n{check_app.CONNECTED}"),
        (False, "execution error: no menu"),
    ],
)
def test_the_menu_is_closed_only_after_a_read_that_found_it(
    read: tuple[bool, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An Escape the check sends with no menu open goes to the app's key window and closes a panel:
    # that is how CI run 42395410137 lost Set Up's window, to the Escape after a menu screenshot
    # whose click had opened no menu. The screenshot is now taken in the read, with the menu open;
    # Escape follows only a read that worked, and only while System Events still finds the menu,
    # and then a second passes before anything clicks the status item again.
    sent: list[str] = []

    def system_events(body: str, timeout: float = 30) -> tuple[bool, str]:
        sent.append(body)
        return (
            read if body.startswith(f"click {check_app.STATUS_ITEM}\n") else (True, "Escape sent")
        )

    monkeypatch.setattr(check_app, "_system_events", system_events)
    monkeypatch.setattr(check_app, "_screenshot", lambda path: sent.append(f"shot {path.name}"))
    found = check_app._menu_until(lambda items: True, 0, shot=tmp_path / "menu.png")
    worked, said = read
    assert found == (worked, said.splitlines() if worked else [said])
    opened = (
        f"click {check_app.STATUS_ITEM}\ndelay 1\n{check_app.LINES}"
        f"return (name of every menu item of {check_app.MENU}) as text"
    )
    assert sent == [opened, "shot menu.png", *([check_app.CLOSE_MENU] if worked else [])]
    close = check_app.CLOSE_MENU.splitlines()
    escape = close.index("key code 53")
    assert close[:escape] == [
        f'if not (exists {check_app.MENU}) then return "no menu was open, so no Escape was sent"'
    ]
    assert close[escape + 1 :] == ["delay 1", 'return "Escape sent"']
    # Nothing after the Escape asks System Events whether the menu is still there: it found the
    # menu after every Escape in CI run 42402770764, so the answer said nothing.
    assert check_app.CLOSE_MENU.count("exists") == 1
    # No other Escape is sent to close a menu: there is no screenshot that clicks the status item
    # by itself any more, and the pairing check sends no Escape of its own.
    assert not hasattr(check_app, "_menu_shot")
    source = _CHECK_APP.read_text(encoding="utf-8")
    assert "key code 53" not in _python_function(source, "_menu_until")
    import ast

    pairing = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ClassDef) and node.name == "_PairingCheck"
    )
    assert "key code 53" not in (ast.get_source_segment(source, pairing) or "")


def test_the_app_says_on_standard_error_when_set_up_shows_and_closes() -> None:
    # So that a run that misses Set Up's window can tell, in the app's own words, whether it was
    # shown, closed by the app (Done, or the pair ended) or closed otherwise (its close button, or
    # Escape): the pairing check prints the app's standard error with that failure, and at its end.
    # Each line names a page at most, never what the page lists.
    connection = _swift("Connection.swift")
    effect = _swift_function(connection, "    private func setUpEffect(")
    assert (
        "        case .readPrevious:\n"
        '            Connection.log("Set Up reads the policy set aside before it shows a page")\n'
    ) in effect
    assert (
        "        case .show(let page):\n"
        '            Connection.log("Set Up shows \\(Connection.setUpPageName(page))")\n'
        "            setupPanel.show(page, with: setUpShown())\n"
    ) in effect
    assert (
        "        case .close:\n"
        '            Connection.log("Set Up closed by the app")\n'
        "            setupPanel.close()\n"
    ) in effect
    assert (
        "        setupPanel.onClosed = { [weak self] in\n"
        '            Connection.log("Set Up\'s window was closed, not by the app")\n'
        "            self?.feedSetUp(.windowClosed)\n"
        "        }\n"
    ) in connection
    named = _swift_function(connection, "    private static func setUpPageName(")
    assert re.findall(r'return "([^"\\]*)"\n', named) == [
        "Add back",
        "adding back",
        "Folders",
        "What Lucille may do",
        "Ready",
    ]
    assert named.count("return") == 5 and "\\(" not in named
    assert connection.count("Connection.setUpPageName(") == 1
    # The check reads those lines with that failure: what the app has written so far.
    missing = _python_function(_CHECK_APP.read_text(encoding="utf-8"), "set_up_missing")
    assert "self.stderr_now()" in missing


def test_the_set_up_check_looks_for_the_apps_own_words() -> None:
    # What the pairing check reads of Set Up, as App/Setup.swift spells it, and the words of each
    # page there.
    text = _swift("Setup.swift")
    name = check_app.APP_NAME
    words = {
        "windowTitle": check_app.SETUP_TITLE,
        "addBackTitle": check_app.SETUP_ADD_BACK,
        "foldersTitle": check_app.SETUP_FOLDERS,
        "noFolders": check_app.SETUP_NO_FOLDERS,
        "abilitiesTitle": check_app.SETUP_ABILITIES,
        "readyTitle": check_app.SETUP_READY,
        "continueButton": check_app.SETUP_CONTINUE,
        "doneButton": check_app.SETUP_DONE,
        "menuItem": check_app.SETUP_ITEM,
    }
    for constant, said in words.items():
        assert f'static let {constant} = "{_named_in_swift(said)}"' in text, constant
        assert check_app._quoted(said) == f'"{said}"'
    assert check_app.SETUP_TITLE == f"Set Up {name}"
    assert check_app.SETUP_ADD_BACK == "Add back what you had before?"
    assert check_app.SETUP_FOLDERS == "Folders Lucille may use"
    assert check_app.SETUP_NO_FOLDERS == "No folders yet"
    assert check_app.SETUP_ABILITIES == "What Lucille may do"
    assert check_app.SETUP_READY == f"{name} is ready"
    assert check_app.SETUP_ITEM == "Set Up Folders and Abilities…"
    pages = {
        "addBackText": (
            "Connecting started \\(appName)'s settings afresh. These are the folders and abilities"
            " you had before. Check the ones to add back. Nothing is added unless you check it."
        ),
        "addCheckedButton": "Add Checked",
        "skipButton": "Skip",
        "someNotOffered": (
            "Some of what you had before isn't shown: it is already set up, or \\(appName) doesn't"
            " offer it here."
        ),
        "wholeDiskNotOffered": "\\(appName) doesn't add your whole disk back, so it isn't offered.",
        "previousUnreadableTitle": "Couldn't read what you had before",
        "addFolderButton": "Add a Folder…",
        "removeButton": "Remove",
        "folderBlocked": "macOS is blocking \\(appName) from this folder.",
        "folderDenied": "This folder's permissions don't let \\(appName) open it.",
        "folderMissing": "This folder isn't there any more.",
        "folderUnopened": "\\(appName) couldn't open this folder.",
        "readyText": (
            "Lucille can now ask this Mac to do what is turned on, in the folders you added."
        ),
    }
    for constant, said in pages.items():
        assert f'static let {constant} = "{said}"' in text, constant
    assert "    private static let appName = HostText.appName\n" in text
    # The note for a set-aside policy the host can't read is HostText's refusal of
    # previous_invalid, which test_every_error_code_the_menus_own_verbs_give_has_words pins.
    assert "HostText.refusal(error)" in _swift_function(
        _swift("Connection.swift"), "    private func setUpEffect("
    )


# --- Open at Login (decisions/0002 §3.1 step 10, §3.3, §9, §14) --------------------------------

# Ways to register something to start at login other than the app itself, and the APIs before
# SMAppService: none of them is in the app's Swift, or in the self-test's.
_OTHER_LOGIN_ITEMS = (
    r"\.agent\s*\(",
    r"\.loginItem\s*\(\s*identifier\s*:",
    r"\.daemon\s*\(",
    r"\bSMLoginItemSetEnabled\b",
    r"\bSMJobBless\b",
    r"\bSMJobSubmit\b",
    r"\bLSSharedFileList",
    r"\bkLSSharedFileListSessionLoginItems\b",
)
_LOGIN_ITEM_FUNCTIONS = (
    "    static var state: LoginState {",
    "    static func checked() -> LoginState {",
    "    static func register() -> LoginOutcome {",
    "    static func unregisterIfRegistered() -> LoginRemoval {",
    "    static func openSettings() {",
)


def _flat(text: str) -> str:
    """``text`` with every run of white space one space, to compare Swift whatever its wrapping."""
    return re.sub(r"\s+", " ", text)


def test_the_login_item_is_the_main_app_only() -> None:
    # The app's one login item is the app itself (SMAppService.mainApp, decisions/0002 §9): not an
    # agent, a helper or a daemon, nor the APIs before SMAppService. Only LoginItem.swift names
    # SMAppService, and there only mainApp's register(), unregister() and status, and
    # openSystemSettingsLoginItems(). Every function there is synchronous, so unregister() is its
    # synchronous form, not the async one a call in an async context would take; and each refuses
    # unless this copy is the one at /Applications/<name>.app, since mainApp is keyed by the bundle
    # identifier, which a "<name> 2" shares. The app asks macOS to register it only from the
    # user's click: Ready's Done with its box checked, and the Settings menu's Open at Login.
    login = _swift("LoginItem.swift")
    code = _code_of(login)
    for path in [*SWIFT_FILES, _SELF_TEST]:
        text = _code_of(path.read_text(encoding="utf-8"))
        if path.name != "LoginItem.swift":
            assert "SMAppService" not in text, path.name
            assert "ServiceManagement" not in text, path.name
        for pattern in _OTHER_LOGIN_ITEMS:
            assert not re.search(pattern, text), (path.name, pattern)
    assert set(re.findall(r"SMAppService\s*\.\s*(\w+)", code)) == {
        "mainApp",
        "openSystemSettingsLoginItems",
    }
    assert set(re.findall(r"SMAppService\.mainApp\.(\w+)", code)) == {
        "register",
        "unregister",
        "status",
    }
    assert code.count("try SMAppService.mainApp.register()") == 1
    assert code.count("try SMAppService.mainApp.unregister()") == 1
    assert code.count("SMAppService.mainApp.status") == 1
    assert code.count("SMAppService.openSystemSettingsLoginItems()") == 1
    for word in ("async", "await", "Task", "completionHandler", "DispatchQueue", "@MainActor"):
        assert not re.search(rf"(?<![\w@]){re.escape(word)}\b", code), word
    assert "synchronous" in login
    assert "enum LoginItem {" in code
    for signature in _LOGIN_ITEM_FUNCTIONS:
        body = _swift_function(login, signature)
        assert "guard Placement.current.isPlaced else {" in body, signature
    # The status is read in one place, and every case of it is named, an unknown one too.
    state = _swift_function(login, "    static var state: LoginState {")
    assert "switch SMAppService.mainApp.status {" in state
    for case in ("notRegistered", "enabled", "requiresApproval", "notFound"):
        assert f"case .{case}:\n            return .{case}\n" in state, case
    assert "@unknown default:" in state
    # One line on standard error, the state's name and nothing else (decisions/0002 §15), from
    # one place, for each time it acts or checks: never from the plain read the menu makes.
    assert code.count('Connection.log("login item: \\(') == 1
    assert 'Connection.log("login item: \\(state.rawValue)")' in code
    assert "Connection.log" not in state
    for signature in _LOGIN_ITEM_FUNCTIONS[1:4]:
        assert "said(" in _swift_function(login, signature), signature
    # What came of a register or an unregister is decided from macOS's state afterwards, by
    # LoginRules, which the self-test checks.
    assert "LoginRules.afterRegister(state: " in _swift_function(
        login, "    static func register() -> LoginOutcome {"
    )
    assert "LoginRules.removal(before: " in _swift_function(
        login, "    static func unregisterIfRegistered() -> LoginRemoval {"
    )
    # register() is asked for only from a click: twice in Connection.swift, and nowhere else.
    counts = {
        path.name: _code_of(path.read_text(encoding="utf-8")).count("LoginItem.register()")
        for path in SWIFT_FILES
    }
    assert {name: count for name, count in counts.items() if count} == {"Connection.swift": 2}
    for path in SWIFT_FILES:
        text = _code_of(path.read_text(encoding="utf-8")).replace("LoginItem.register()", "")
        if path.name != "LoginItem.swift":
            assert ".register(" not in text, path.name
    connection = _swift("Connection.swift")
    effect = _swift_function(connection, "    private func setUpEffect(")
    done = effect.split("        case .registerLoginItem:\n", 1)
    assert len(done) == 2
    done_case = done[1].split("\n        case ", 1)[0]
    assert "showLoginOutcome(LoginItem.register())" in done_case
    assert done_case.index("guard mayRegisterLoginItem() else {") < done_case.index(
        "LoginItem.register()"
    )
    chosen = _swift_function(connection, "    func openAtLoginChosen(_ on: Bool) {")
    assert chosen.count("LoginItem.register()") == 1
    assert chosen.index("guard isConfirmed(), ladder == nil, !exiting else {") < chosen.index(
        "LoginItem.register()"
    )
    assert (
        chosen.index("if on {")
        < chosen.index("guard mayRegisterLoginItem() else {")
        < chosen.index("LoginItem.register()")
    )
    # Both go through one test: a confirmed pair, by status or by run, and not once run's exit has
    # said the Mac is not connected, when status may still say confirmed; not during a Disconnect
    # (its drain, or forget), nor while the app quits.
    may = _swift_function(connection, "    private func mayRegisterLoginItem() -> Bool {")
    assert "LoginRules.mayRegister(confirmed: confirmed, phase: supervisor.phase)" in may
    assert "let confirmed = isConfirmed()" in may
    assert "return ladder == nil && !exiting && disconnecting == nil && !forgetting" in may
    assert _code_of(connection).count("mayRegisterLoginItem()") == 3
    rule = _swift_function(
        _swift("LoginRules.swift"),
        "    static func mayRegister(confirmed: Bool, phase: RunSupervisor.Phase) -> Bool {",
    )
    assert (
        "        if case .halted(.notConnected) = phase {\n            return false\n        }\n"
        "        return confirmed\n"
    ) in rule
    # The Settings app is opened only from a button: the note after Done or the switch, and the
    # launch note.
    opened = sum(
        _code_of(path.read_text(encoding="utf-8")).count("LoginItem.openSettings()")
        for path in SWIFT_FILES
    )
    assert opened == 2 and _code_of(connection).count("LoginItem.openSettings()") == 2


def test_every_way_a_pair_ends_unregisters() -> None:
    # decisions/0002 §9: the login item is unregistered whenever the pair ends: run's exit or a
    # status read the supervisor asked for that says it is gone (notPaired, leaked, expired, ended,
    # forgotten; not noGate, whose pair is kept until Stop Connecting, nor a reason this release
    # does not know); Stop Connecting's pair reject; a status read that finds no pair once one was
    # seen; and Disconnect, whether or not a quit came during it, once forget is done. Taking it
    # away only narrows, so it needs no click (only widening needs the user's), and
    # LoginItem.unregisterIfRegistered calls unregister() only for an item that is enabled or
    # waits for approval.
    supervisor = _swift("Supervisor.swift")
    effect_cases = supervisor.split("    enum Effect: Equatable, Sendable {", 1)[1]
    assert "        case pairGone\n" in effect_cases.split("\n    }\n", 1)[0]
    exited = _swift_function(supervisor, "    private mutating func exited(")
    halted = exited.split("            case .notConnected(let why):\n", 1)[1]
    halted = halted.split("\n            case ", 1)[0]
    assert "phase = .halted(.notConnected(why))" in halted
    assert (
        "if LoginRules.pairGone(why) {\n                    effects.append(.pairGone)\n" in halted
    )
    read = _swift_function(supervisor, "    private mutating func statusRead(")
    assert read.count("LoginRules.pairGone(") == 1
    assert (
        "if case .halted(.notConnected(let why)) = phase, LoginRules.pairGone(why) {\n"
        "            return [.pairGone]\n"
    ) in read
    assert supervisor.count("LoginRules.pairGone(") == 2
    rules = _swift("LoginRules.swift")
    gone = _swift_function(rules, "    static func pairGone(_ why: NotConnected) -> Bool {")
    assert (
        "case .notPaired, .leaked, .expired, .ended, .forgotten:\n            return true\n" in gone
    )
    assert "case .noGate, .other:\n            return false\n" in gone
    assert "default" not in gone
    connection = _swift("Connection.swift")
    effect = _swift_function(connection, "    private func runEffect(")
    case = effect.split("        case .pairGone:\n", 1)
    assert len(case) == 2
    gone_case = case[1].split("\n        case ", 1)[0]
    # And status is read again, so the menu and a Set Up window follow an exit that ended it.
    assert gone_case.index("LoginItem.unregisterIfRegistered()") < gone_case.index(
        "refreshStatus()"
    )
    # Stop Connecting's pair reject forgot the pair, or the host found none: a pair whose gate
    # record was lost (no_gate) is ended only this way, as Disconnect is not offered for it.
    flow = _swift_function(connection, "    private func flowEffect(")
    said = flow.split("        case .say(let note):\n", 1)[1].split("\n        case ", 1)[0]
    assert (
        "if LoginRules.pairForgotten(note) {\n"
        "                // Stop Connecting forgot the pair, or there is none: the login item goes"
    ) in said
    assert said.index("if LoginRules.pairForgotten(note) {") < said.index(
        "_ = LoginItem.unregisterIfRegistered()"
    )
    forgotten = _swift_function(
        _swift("LoginRules.swift"), "    static func pairForgotten(_ note: PairingNote) -> Bool {"
    )
    assert "        case .rejected:\n            return true\n" in forgotten
    assert (
        '        case .refused(let code, _, _):\n            return code == "none"\n' in forgotten
    )
    assert "default" not in forgotten
    # Disconnect: done is decided before the early return of a quit that came during it, and the
    # login item goes before that return, so both ways out have it gone; the note says so.
    forget = _swift_function(connection, "    private func forget() {")
    early = forget.index("if self.ladder != nil || self.exiting {")
    decided = forget.index("var done = false")
    removed = forget.index("let removal = self.loginItemAfterForget(done)")
    assert decided < removed < early
    # Taken away by a method of its own, not by the task that waited for forget
    # (test_the_login_item_is_asked_from_synchronous_code_only).
    assert "LoginItem." not in _code_of(forget)
    after_forget = _swift_function(
        connection, "    private func loginItemAfterForget(_ done: Bool) -> LoginRemoval {"
    )
    assert "        done ? LoginItem.unregisterIfRegistered() : .nothingToRemove\n" in after_forget
    assert "LoginText.afterDisconnect(removal)" in forget[early:]
    # A confirmed pair seen, and then status finds none, whoever ended it.
    publish = _swift_function(connection, "    private func publish() {")
    ended = publish.split("} else if setUpSawPair, ", 1)[1].split("\n        }\n", 1)[0]
    assert ended.index("setUpSawPair = false") < ended.index("LoginItem.unregisterIfRegistered()")
    # Nothing else takes it away but the switch and the launch note's Keep It Off.
    assert _code_of(connection).count("LoginItem.unregisterIfRegistered()") == 6
    for path in SWIFT_FILES:
        if path.name not in ("Connection.swift", "LoginItem.swift"):
            assert "unregisterIfRegistered" not in path.read_text(encoding="utf-8"), path.name
    unregister = _swift_function(
        _swift("LoginItem.swift"), "    static func unregisterIfRegistered() -> LoginRemoval {"
    )
    guarded = unregister.index("guard before == .enabled || before == .requiresApproval else {")
    assert guarded < unregister.index("try SMAppService.mainApp.unregister()")
    # With nothing to take away, it still says the state macOS gives: after a Done that macOS
    # refused, or a Keep It Off, this is the only line Disconnect's check can read.
    assert (
        "        guard before == .enabled || before == .requiresApproval else {\n"
        "            _ = said(before)\n"
        "            return .nothingToRemove\n"
        "        }\n"
    ) in unregister


def _swift_blocks(code: str, opener: str) -> list[str]:
    """Each block of ``code`` that the regex ``opener`` opens (it ends at the block's ``{``),
    from where it matched to the brace that closes it. ``code`` holds no brace in a string."""
    blocks = []
    for found in re.finditer(opener, code):
        depth = 0
        for at in range(found.end() - 1, len(code)):
            if code[at] == "{":
                depth += 1
            elif code[at] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(code[found.start() : at + 1])
                    break
    return blocks


def test_the_login_item_is_asked_from_synchronous_code_only() -> None:
    # SMAppService's unregister() has an async form too, which a call in an async context would
    # take, and which would not compile without await. LoginItem's functions are synchronous, so
    # each call they make is the synchronous one; and they are called only from synchronous code,
    # never inside a task nor in an async function. Once forget is done, a method of its own
    # takes the item away, not the task that waited for forget.
    for path in SWIFT_FILES:
        if path.name not in ("Connection.swift", "LoginItem.swift"):
            assert "LoginItem." not in _code_of(path.read_text(encoding="utf-8")), path.name
    code = _code_of(_swift("Connection.swift"))
    strings = re.findall(r'"(?:[^"\\\n]|\\.)*"', code)
    assert strings and not [text for text in strings if "{" in text or "}" in text]
    tasks = _swift_blocks(code, r"\bTask(?:\.detached)?\s*(?:\([^)]*\))?\s*\{")
    asyncs = _swift_blocks(code, r"\bfunc [^{]*\basync\b[^{]*\{")
    assert len(tasks) >= 10 and len(asyncs) >= 4
    # forget's task among them, which once took the item away itself.
    forgetting = 'self.cli, ["--json", "forget"], on: VerbQueue.store'
    assert sum(forgetting in task for task in tasks) == 1
    for block in tasks + asyncs:
        assert "LoginItem." not in block, block[:300]
    assert code.count("LoginItem.") == 12
    login = _code_of(_swift("LoginItem.swift"))
    for word in ("async", "await", "Task"):
        assert not re.search(rf"\b{word}\b", login), word


def test_the_login_rules_draw_nothing_and_the_self_test_checks_them() -> None:
    # LoginRules.swift is Foundation only, compiled by the self-test (NEW_LOGIC), which runs
    # checkLoginRules over the host's fixtures: the pair gone or kept for each, the launch note,
    # what came of a register and of an unregister, and the words of each.
    assert "LoginRules.swift" in check_app.LOGIC_SOURCES
    rules = _swift("LoginRules.swift")
    for declared in (
        "enum LoginState: String, Equatable, Sendable {",
        "enum LoginOutcome: Equatable, Sendable {",
        "enum LoginRemoval: Equatable, Sendable {",
        "enum LoginRules {",
        "enum LoginText {",
        "    static func pairGone(_ why: NotConnected) -> Bool {",
        "    static func pairForgotten(_ note: PairingNote) -> Bool {",
        "    static func mayRegister(confirmed: Bool, phase: RunSupervisor.Phase) -> Bool {",
        "    static func isPaired(_ pair: StatusPair?) -> Bool {",
        "    static func launchNote(paired: Bool, state: LoginState) -> Bool {",
        "    static func afterRegister(state: LoginState, error: String?) -> LoginOutcome {",
        "    static func removal(before: LoginState, after: LoginState) -> LoginRemoval {",
        "    static func afterDisconnect(_ removal: LoginRemoval) -> String? {",
    ):
        assert declared in rules, declared
    states = rules.split("enum LoginState: String, Equatable, Sendable {", 1)[1].split("\n}\n")[0]
    assert re.findall(r"^    case (\w+)$", states, re.MULTILINE) == list(check_app.LOGIN_STATES)
    for name in ("SMAppService", "ServiceManagement", "LoginItem.", "NSAlert", "Alerts"):
        assert name not in _code_of(rules), name
    selftest = _SELF_TEST.read_text(encoding="utf-8")
    main_body = _swift_function(selftest, "    static func main() {")
    assert main_body.index("checkSupervisor(fixtures, report)") < main_body.index(
        "checkLoginRules(fixtures, report)"
    )
    checks = _swift_function(selftest, "    static func checkLoginRules(")
    for fixture in (
        "status-unpaired",
        "status-paired",
        "status-unreadable",
        "run-events-ended",
        "run-events-expired",
        "run-events-leaked",
        "run-events-not-paired",
        "run-events-no-gate",
        "run-events-several-pairs",
        "run-events-pair-unusable",
        "run-events-insecure-keyring",
    ):
        assert f'"{fixture}"' in checks, fixture
    for function in (
        "LoginRules.launchNote(",
        "LoginRules.afterRegister(",
        "LoginRules.removal(",
        "LoginRules.pairForgotten(",
        "LoginRules.mayRegister(",
    ):
        assert function in checks, function
    # And after each run-events exit that ends the pair, a click can't register on the status
    # read before the spawn.
    supervised = _swift_function(selftest, "    static func checkSupervisor(")
    assert "let mayAfter = LoginRules.mayRegister(confirmed: true, phase: sup.phase)" in supervised
    # The supervisor's checks: each run-events fixture's exit, and the walk.
    supervise = _swift_function(selftest, "    static func checkSupervisor(")
    assert "let gone: Set<String> = [" in supervise and ".pairGone" in supervise
    walk = _swift_function(selftest, "    static func checkSupervisorWalk(_ report: Report) {")
    assert "effects.contains(.pairGone)" in walk


def test_ready_asks_to_open_at_login_and_only_done_registers() -> None:
    # decisions/0002 §3.1 step 10: Ready has the box "Open <name> when you log in", on, and macOS's
    # notice in words; Done with the box checked asks the app to register (the effect
    # registerLoginItem, which Connection runs as LoginItem.register()). Nothing else does: not the
    # window closed on Ready, which ends Set Up as Done does, and not the box itself. Set Up is
    # owed, and Ready reached, only after They Match here (checkSetupWalk).
    flow = _swift("Setup.swift")
    handle = _swift_function(flow, "    mutating func handle(_ input: Input) -> [Effect] {")
    inputs = flow.split("    enum Input: Equatable, Sendable {", 1)[1].split("\n    }\n", 1)[0]
    effects = flow.split("    enum Effect: Equatable, Sendable {", 1)[1].split("\n    }\n", 1)[0]
    assert "        case openAtLogin(Bool)\n" in inputs
    assert "        case registerLoginItem\n" in effects
    assert "    private(set) var openAtLogin = true\n" in flow
    done = handle.split("        case .done:\n", 1)[1].split("\n        case ", 1)[0]
    assert "guard windowOpen, page == .ready else {" in done
    assert "return openAtLogin ? [.close, .registerLoginItem] : [.close]" in done
    assert _code_of(flow).count(".registerLoginItem") == 1
    box = handle.split("        case .openAtLogin(let on):\n", 1)[1].split("\n        case ", 1)[0]
    assert "guard windowOpen, page == .ready else {" in box and "openAtLogin = on" in box
    shown = _swift_function(flow, "    private mutating func next() -> [Effect] {")
    ready_again = (
        "case .abilities?:\n            openAtLogin = true\n            return showPage(.ready)"
    )
    assert ready_again in shown
    panel = _swift("SetupPanel.swift")
    ready = _flat(_swift_function(panel, "    private func readyPage() -> NSView {"))
    assert (
        "NSButton(checkboxWithTitle: SetupText.openAtLoginBox, target: self, action:"
        " #selector(openAtLoginChanged(_:)))"
    ) in ready
    assert "box.state = .on" in ready
    assert "label(SetupText.openAtLoginNotice, secondary: true)" in ready
    assert "    var onOpenAtLogin: ((Bool) -> Void)?\n" in panel
    assert "onOpenAtLogin?(sender.state == .on)" in panel
    connection = _swift("Connection.swift")
    assert (
        "        setupPanel.onOpenAtLogin = { [weak self] on in\n"
        "            self?.feedSetUp(.openAtLogin(on))\n"
        "        }\n"
    ) in connection
    selftest = _SELF_TEST.read_text(encoding="utf-8")
    walk = _swift_function(selftest, "    static func checkSetupWalk(_ report: Report) {")
    assert "case .registerLoginItem:" in walk and "registers" in walk


def test_open_at_login_is_a_switch_in_a_settings_menu_while_connected() -> None:
    # decisions/0002 §14: while connected, a Settings submenu holds Open at
    # Login, whose switch shows whether macOS will open the app at login (the state read again
    # from macOS, never kept by the app) and turns it on or off; and, while the settings file
    # can't be read, Reset Settings…, the action #16 added. Disconnect stays where it is.
    menu = _flat(_swift("MenuBarApp.swift"))
    items = menu.split("struct HostItems: View {", 1)[1]
    settings = items.split("if connection.connected { Menu(LoginText.settingsMenu) { ", 1)
    assert len(settings) == 2
    inside = settings[1]
    assert inside.startswith(
        "Toggle( LoginText.openAtLoginItem, isOn: Binding( get: { connection.openAtLogin },"
        " set: { connection.openAtLoginChosen($0) } ) ) if model.problem?.offersReset == true {"
        " Button(ConnectionText.resetSettingsItem) { model.resetSettingsChosen() } } } }"
    )
    assert menu.count("LoginText.settingsMenu") == 1
    assert menu.count("Button(ConnectionText.disconnectItem)") == 1
    connection = _swift("Connection.swift")
    assert "    @Published private(set) var openAtLogin = false\n" in connection
    refresh = _swift_function(connection, "    private func refreshLoginItem() {")
    assert "let on = LoginItem.state == .enabled" in refresh
    assert "refreshLoginItem()" in _swift_function(connection, "    private func menuOpened() {")
    chosen = _swift_function(connection, "    func openAtLoginChosen(_ on: Bool) {")
    assert "LoginItem.unregisterIfRegistered()" in chosen and "refreshLoginItem()" in chosen


def test_the_welcome_note_shows_at_launch_only_while_unpaired() -> None:
    # decisions/0002, What is built today: at launch, the note that says where the menu is
    # waits for the first status read, and shows only when it finds no confirmed pair; a paired
    # launch goes straight on (afterWelcome: the paused note, a pairing left unfinished). Opened
    # again while it runs, the app shows it, paired or not. The first status read also checks the
    # login item while paired, and shows the launch note before anything afterWelcome shows.
    menu = _swift("MenuBarApp.swift")
    finished = _swift_function(menu, "    func applicationDidFinishLaunching(")
    hook = finished.index("Connection.shared.onFirstStatusRead = { [weak self] paired in\n")
    assert hook < finished.index("Connection.shared.launch()")
    assert "self?.launchRead(paired: paired)" in finished
    assert "showWhereTheMenuIs()" not in finished
    assert "Task.sleep" not in finished.split("Connection.shared.launch()", 1)[1]
    read = _swift_function(menu, "    private func launchRead(paired: Bool) {")
    assert (
        "        guard !launchHandled else {\n            return\n        }\n"
        "        if paired {\n"
        "            launchHandled = true\n"
        "            Connection.shared.afterWelcome()\n"
        "        } else {\n"
        "            showWhereTheMenuIs()\n"
        "        }\n"
    ) in read
    reopen = _swift_function(menu, "    func applicationShouldHandleReopen(")
    assert "showWhereTheMenuIs()" in reopen
    # Its definition, launchRead and the reopen.
    assert menu.count("showWhereTheMenuIs()") == 3
    connection = _swift("Connection.swift")
    assert "    var onFirstStatusRead: ((Bool) -> Void)?\n" in connection
    status = _swift_function(connection, "    private func readStatus(")
    assert (
        "        if !firstStatusRead {\n"
        "            firstStatusRead = true\n"
        "            firstStatusArrived()\n"
        "        }\n"
    ) in status
    arrived = _swift_function(connection, "    private func firstStatusArrived() {")
    paired = arrived.index("let paired = LoginRules.isPaired(status?.pair)")
    # Only a paired launch checks the login item: an unpaired one writes no line about it, and
    # shows no note about opening at login (checkLoginItemAtLaunch takes the pair as given).
    guarded = arrived.index(
        "        if paired && ladder == nil && !exiting {\n"
        "            checkLoginItemAtLaunch()\n"
        "        }\n"
    )
    checked = arrived.index("checkLoginItemAtLaunch()")
    told = arrived.index("told?(paired)")
    assert paired < guarded < checked < told < arrived.index("afterWelcomeIfReady()")
    assert arrived.count("checkLoginItemAtLaunch()") == 1
    assert _code_of(connection).count("checkLoginItemAtLaunch()") == 2  # its definition, and here
    assert "onFirstStatusRead = nil" in arrived
    launch = _swift_function(connection, "    private func checkLoginItemAtLaunch() {")
    assert "let state = LoginItem.checked()" in launch
    assert "guard LoginRules.launchNote(paired: true, state: state) else {" in launch
    note = _flat(launch)
    assert 'Alerts.shared.show(key: "login-off", {' in note
    assert re.findall(r"addButton\(withTitle: ([\w.]+)\)", note) == [
        "LoginText.openSettingsButton",
        "LoginText.keepItOffButton",
    ]
    assert "LoginItem.openSettings()" in note and "LoginItem.unregisterIfRegistered()" in note
    assert _code_of(connection).count("LoginItem.checked()") == 1


def test_the_login_item_check_looks_for_the_apps_own_words() -> None:
    # What the pairing check reads of Open at Login, as App/LoginRules.swift and App/Setup.swift
    # spell it, and the rest of those words.
    rules = _swift("LoginRules.swift")
    setup = _swift("Setup.swift")
    words = {
        "settingsMenu": check_app.SETTINGS_MENU,
        "openSettingsButton": check_app.OPEN_LOGIN_ITEMS,
        "keepItOffButton": check_app.KEEP_IT_OFF,
        "needsApprovalTitle": check_app.NEEDS_APPROVAL_TITLE,
        "couldNotTurnOnTitle": check_app.LOGIN_FAILED_TITLE,
        "launchOffTitle": check_app.LAUNCH_OFF_TITLE,
        "offAfterDisconnect": check_app.OFF_AFTER_DISCONNECT,
    }
    for constant, said in words.items():
        assert f'static let {constant} = "{_named_in_swift(said)}"' in rules, constant
        assert check_app._quoted(said) == f'"{said}"'
    name = check_app.APP_NAME
    where = "System Settings › General › Login Items & Extensions"
    assert check_app.SETTINGS_MENU == "Settings"
    assert check_app.OPEN_LOGIN_ITEMS == "Open Login Items Settings"
    assert check_app.KEEP_IT_OFF == "Keep It Off"
    assert check_app.NEEDS_APPROVAL_TITLE == (
        f"macOS needs your OK before {name} can open when you log in"
    )
    assert check_app.LOGIN_FAILED_TITLE == f"{name} couldn't turn on opening at login"
    assert check_app.LAUNCH_OFF_TITLE == f"{name} won't start when you log in"
    # True whether the item was on or only waited for the user's approval.
    assert check_app.OFF_AFTER_DISCONNECT == f"{name} won't open when you log in."
    rest = {
        "openAtLoginItem": "Open at Login",
        "needsApprovalText": (
            f"Turn \\(appName) on in {where}. Until then, Lucille can reach this Mac only while"
            " \\(appName) is open."
        ),
        "couldNotTurnOnText": (
            "Open \\(appName) yourself after you log in: Lucille can reach this Mac only while"
            " \\(appName) is open. To try again, choose Settings, then Open at Login, in"
            " \\(appName)'s menu."
        ),
        "couldNotTurnOffTitle": "\\(appName) couldn't stop opening when you log in",
        "turnOffByHand": f"Turn it off in {where}.",
        "launchOffText": (
            "Opening at login is turned off in System Settings. Lucille can reach this Mac only"
            " while \\(appName) is open."
        ),
        "removeByHand": f"\\(appName) couldn't turn off opening at login. Turn it off in {where}.",
    }
    for constant, said in rest.items():
        assert f'static let {constant} = "{said}"' in rules, constant
    assert "    private static let appName = HostText.appName\n" in rules
    assert f'static let openAtLoginBox = "{_named_in_swift(check_app.OPEN_AT_LOGIN_BOX)}"' in setup
    assert check_app.OPEN_AT_LOGIN_BOX == f"Open {name} when you log in"
    notice = _named_in_swift(check_app.OPEN_AT_LOGIN_NOTICE)
    assert (
        f'static let openAtLoginNotice = "{notice} That is expected. You can change it later in'
        f' {where}."'
    ) in setup
    assert check_app.OPEN_AT_LOGIN_NOTICE == (
        f"macOS will show a notice that {name} can now open when you log in."
    )
    # Disconnect's note adds a sentence for what became of the login item, or none.
    after = _swift_function(
        rules, "    static func afterDisconnect(_ removal: LoginRemoval) -> String? {"
    )
    assert "case .nothingToRemove:\n            return nil\n" in after
    assert "case .removed:\n            return offAfterDisconnect\n" in after
    assert "case .failed:\n            return removeByHand\n" in after
    # The line on standard error, which the pairing check reads, and the note for each state
    # after Done.
    assert check_app.LOGIN_STATES == ("notRegistered", "enabled", "requiresApproval", "notFound")
    for state in check_app.LOGIN_STATES:
        assert check_app._login_state(f"x\nlogin item: {state}\ny\n") == state
    assert check_app._login_state("login item: moon\n") is None
    assert check_app._login_state("a login item: enabled\n") is None
    assert check_app._login_state("") is None
    said = "login item: notRegistered\nlogin item: enabled\n"
    assert check_app._login_state(said) == "enabled"
    assert check_app.LOGIN_NOTE_AFTER_DONE == {
        "enabled": None,
        "requiresApproval": check_app.NEEDS_APPROVAL_TITLE,
        "notRegistered": check_app.LOGIN_FAILED_TITLE,
        "notFound": check_app.LOGIN_FAILED_TITLE,
    }
    for said in (check_app.OPEN_AT_LOGIN_BOX, check_app.OPEN_AT_LOGIN_NOTICE, check_app.NOT_NOW):
        assert check_app._quoted(said) == f'"{said}"'


# --- Only the copy at /Applications/<name>.app acts; one update notice (0002 §13 and §16) -------

_SELF_TEST = build_app.MACOS_DIR / "Tests" / "SelfTest.swift"
# A copy that is not placed returns here, before anything that would start the host.
_NOT_PLACED_RETURNS = (
    "        guard Placement.current.isPlaced else {\n            return\n        }\n"
)


def test_nothing_starts_before_the_placement_check() -> None:
    # Every child starts through HostCLI.spawn, which starts nothing until allowSpawns has opened
    # its latch. allowSpawns takes a PlacedToken, which only Placement.swift can make, and only
    # Placement.judge does, for the copy at exactly /Applications/<name>.app; MenuBarApp.init is
    # the one place that opens it. A copy anywhere else does not even reach the code that would
    # start something: HostModel's first read and its menu observer, Connection.launch's signal
    # handler and status read, and the welcome note that leads to them.
    cli = _swift("HostCLI.swift")
    placement = _swift("Placement.swift")
    menu = _swift("MenuBarApp.swift")
    model = _swift("HostModel.swift")
    connection = _swift("Connection.swift")
    assert "struct PlacedToken: Equatable, Sendable {\n    fileprivate init() {}\n}" in placement
    assert placement.count("PlacedToken()") == 1
    assert "    static func judge(\n" in placement
    # The running copy is judged by the path it was opened from and the path that leads to once
    # links are followed, and by the name its Info.plist gives; the original is looked for as
    # another copy of this app, by its bundle identifier.
    current = placement.split("    static let current: Placement = {\n", 1)[1].split(
        "    }()\n", 1
    )[0]
    for line in (
        "let bundle = Bundle.main.bundleURL",
        "let resolved = bundle.resolvingSymlinksInPath().path",
        'let name = Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String',
        "runningAt: resolved,",
        "identifier: Bundle.main.bundleIdentifier",
        "bundlePath: bundle.path, resolvedPath: resolved, appName: name,",
        "originalPresent: original",
    ):
        assert line in current, line
    present = _swift_function(
        placement,
        "    static func originalPresent(at expected: String, runningAt running: String,"
        " identifier: String?) -> Bool {",
    )
    assert "guard let identifier, !identifier.isEmpty," in present
    assert "return Bundle(path: expected)?.bundleIdentifier == identifier" in present
    for path in [*SWIFT_FILES, _SELF_TEST]:
        if path.name != "Placement.swift":
            assert "PlacedToken()" not in path.read_text(encoding="utf-8"), path.name
    # The latch: closed until allowSpawns, and read first in spawn, before anything is made.
    assert "    static func allowSpawns(_ token: PlacedToken) {\n" in cli
    assert "        case notPlaced\n" in cli
    spawn = _function_body(cli, "    func spawn(\n")
    lock = spawn.index("HostCLI.spawnLock.lock()")
    latch = spawn.index(
        "guard HostCLI.latch.isOpen else {\n            throw SpawnError.notPlaced\n"
    )
    assert lock < latch < spawn.index("let process = Process()")
    # allowSpawns is called once in the app, from MenuBarApp.init, and only on a placed copy.
    calls = {
        path.name: path.read_text(encoding="utf-8").count("allowSpawns(") for path in SWIFT_FILES
    }
    assert {name: count for name, count in calls.items() if count} == {
        "HostCLI.swift": 1,
        "MenuBarApp.swift": 1,
    }
    init = _swift_function(menu, "    init() {")
    assert (
        "        if case .placed(let token) = Placement.current {\n"
        "            HostCLI.allowSpawns(token)\n"
        "        }\n"
    ) in init
    assert (
        menu.index("struct MenuBarApp: App {")
        < menu.index("    init() {")
        < menu.index("final class AppDelegate")
    )
    # HostModel's first read and its menu observer, and Connection.launch, need a placed copy.
    first = _swift_function(model, "    private init() {")
    assert first.index(_NOT_PLACED_RETURNS) < first.index("menuObserver = ")
    assert first.index(_NOT_PLACED_RETURNS) < first.index("refresh()")
    launch = _swift_function(connection, "    func launch() {")
    guard = (
        "        guard !launched, Placement.current.isPlaced else {\n"
        "            return\n        }\n"
    )
    assert launch.index(guard) < launch.index("SignalStop.install(children)")
    assert launch.index(guard) < launch.index("feedRun(.start)")
    # The app calls launch() once, and only once it knows the copy is placed; any other copy
    # shows its placement note instead of the welcome note.
    assert sum(path.read_text(encoding="utf-8").count(".launch()") for path in SWIFT_FILES) == 1
    finished = _swift_function(menu, "    func applicationDidFinishLaunching(")
    refused = finished.index("        guard Placement.current.isPlaced else {\n")
    assert refused < finished.index("PlacementNote.show()") < finished.index("            return\n")
    assert finished.index("            return\n") < finished.index("Connection.shared.launch()")
    # The self-test checks the latch before anything it runs starts a child.
    selftest = _SELF_TEST.read_text(encoding="utf-8")
    main = _swift_function(selftest, "    static func main() {")
    latch_checked = main.index("checkSpawnLatch(home: home, report)")
    assert latch_checked < main.index("checkChild(home: home, report)")
    assert latch_checked < main.index("checkCLI(app:")
    assert 'checkPlacement(home: options["home"], report)' in main


def test_a_copy_anywhere_else_says_how_to_fix_it_and_offers_only_its_place_and_quit() -> None:
    # A copy that is not at /Applications/<name>.app says, when it opens, when it is opened again
    # and from its menu's one line, why it can't run and how to fix that. A copy outside
    # Applications offers Show Applications Folder, the place PlaceRules gives it, before Quit; a
    # renamed copy, in Applications already, offers Quit alone. Show Applications Folder opens the
    # Finder there and quits, and Quit quits, through the app's one terminate (quitChosen). When
    # macOS could not open the folder, the note comes again with that said first, and Quit alone.
    # Its menu holds that line and Quit.
    menu = _swift("MenuBarApp.swift")
    note = _swift_function(menu, "    static func show(couldNotOpen failed: SystemPlace? = nil) {")
    assert "@MainActor\nenum PlacementNote {\n" in menu
    assert 'Alerts.shared.show(key: "placement", front: true, {' in note
    assert "guard let note = ConnectionText.placement(Placement.current) else {" in note
    assert "let place = failed == nil ? PlaceRules.place(for: Placement.current) : nil" in note
    assert re.findall(r"addButton\(withTitle: ([\w.()]+)\)", note) == [
        "PlaceText.button(place)",
        "ConnectionText.quitButton",
    ]
    assert (
        "if let place {\n                alert.addButton(withTitle: PlaceText.button(place))\n"
        in note
    )
    assert "PlaceText.couldNotOpen(" in note
    (done,) = _swift_blocks(note, r"done: \{")
    opened = done.index("if SystemPlaces.open(place) {")
    assert opened < done.index("Connection.shared.quitChosen()")
    assert done.index("PlacementNote.show(couldNotOpen: place)") > opened
    assert done.count("Connection.shared.quitChosen()") == 2
    assert menu.count("PlacementNote.show()") == 3
    reopen = _swift_function(menu, "    func applicationShouldHandleReopen(")
    refused = reopen.index(
        "        guard Placement.current.isPlaced else {\n            PlacementNote.show()\n"
    )
    assert refused < reopen.index("showWhereTheMenuIs()")
    host_menu = menu.split("struct HostMenu: View {", 1)[1].split("\n}\n", 1)[0]
    assert (
        "        if Placement.current.isPlaced {\n"
        "            HostItems(model: model, connection: connection)\n"
        "        } else if let note = ConnectionText.placement(Placement.current) {\n"
        "            Button(note.title) { PlacementNote.show() }\n"
        "        }\n"
        "        Divider()\n"
        "        Button(HostText.quitApp) { connection.quitChosen() }\n"
    ) in host_menu
    # Everything else the menu holds is in HostItems, which a copy that is not placed never shows.
    items = menu.split("struct HostItems: View {", 1)[1]
    for item in (
        'Button("Pause Lucille")',
        'Menu("Abilities")',
        "Button(ConnectionText.connectItem)",
        "Button(ConnectionText.activityItem)",
    ):
        assert item in items and item not in host_menu, item
    # The words, in ConnectionText: a note for every place but the right one, none for it.
    words = _swift("ConnectionText.swift")
    placement = _swift_function(
        words, "    static func placement(_ placement: Placement) -> ConnectionNote? {"
    )
    assert "        case .placed:\n            return nil\n" in placement
    for case in (".diskImage", ".insideApplications", ".homeApplications", ".elsewhere"):
        assert f"case .notInApplications({case}):" in placement, case
    assert "case .renamed(let copy, let originalPresent):" in placement


def test_prepare_to_update_goes_through_quit_chosen() -> None:
    # Prepare to Update drains like Quit, and quits (decisions/0002 §16), through the app's one
    # terminate: quitChosen, so the drain is the app's own .quit. It asks no Quit question, which
    # the app's own Quit keeps (D9); a quit by Apple event still never asks.
    connection = _swift("Connection.swift")
    chosen = _swift_function(connection, "    func prepareToUpdateChosen() {")
    assert chosen.index("skipQuitQuestion = true") < chosen.index("quitChosen()")
    assert "terminate(" not in chosen and "beginQuit(" not in chosen
    assert "    private var skipQuitQuestion = false\n" in connection
    should = _swift_function(
        connection, "    func shouldTerminate() -> NSApplication.TerminateReply {"
    )
    taken = should.index(
        "        let own = userAskedToQuit\n"
        "        userAskedToQuit = false\n"
        "        let skip = skipQuitQuestion\n"
        "        skipQuitQuestion = false\n"
    )
    assert taken < should.index("if exiting {")
    assert (
        "let kind: RunSupervisor.DrainKind = own ? .quit : Connection.quitKindOfCurrentEvent()"
    ) in should
    assert "        if own && !skip && supervisor.runAlive && disconnecting == nil {\n" in should
    assert should.count("askQuit()") == 1
    # The update notice's button and the menu's item are the only ways in.
    assert connection.count("prepareToUpdateChosen()") == 2
    assert connection.count("skipQuitQuestion = true") == 1
    menu = _swift("MenuBarApp.swift")
    assert menu.count("connection.prepareToUpdateChosen()") == 1
    assert (
        sum(path.read_text(encoding="utf-8").count("terminate(nil)") for path in SWIFT_FILES) == 1
    )


def test_the_update_notice_is_keyed() -> None:
    # host_too_old is said once (spec/CLI.md section 6, `host_too_old`). The supervisor tells the
    # glue once for each higher version Lucille asks for; the glue shows the notice by itself only
    # the first time, and after that only moves the menu's line to the highest version, so a
    # Lucille that raises its minimum on every request, even after Not Now, does not bring it back.
    # The one note, under one key, reads the highest version when it is drawn. Not Now is the
    # default. There is no Download button: the note says in words where to get the update.
    connection = _swift("Connection.swift")
    effect = connection.split("        case .tellHostTooOld(", 1)[1].split("\n        case .", 1)[0]
    assert "Alerts.shared.note(" not in effect
    assert effect.count("showUpdateNotice()") == 1
    first = effect.index("            let first = updateNotice == nil\n")
    assert first < effect.index("            updateNotice = ConnectionText.hostTooOldTitle(")
    assert (
        "            if first {\n                showUpdateNotice()\n            }\n"
        in effect + "\n"
    )
    # Besides the menu's line, which shows it on request, nothing else in the glue shows it.
    assert connection.replace("    func showUpdateNotice() {", "").count("showUpdateNotice()") == 1
    assert connection.count('key: "host-too-old"') == 1
    shown = _swift_function(connection, "    func showUpdateNotice() {")
    assert 'Alerts.shared.show(key: "host-too-old", {' in shown
    built = shown.split('Alerts.shared.show(key: "host-too-old", {', 1)[1].split("}, done:", 1)[0]
    assert "self.supervisor.highestMinVersion" in built
    assert "ConnectionText.hostTooOldTitle(minVersion:" in built
    assert "ConnectionText.hostTooOldText(hostVersion:" in built
    assert re.findall(r"addButton\(withTitle: ([\w.]+)\)", built) == [
        "ConnectionText.notNowButton",
        "ConnectionText.prepareToUpdateButton",
    ]
    done = shown.split("}, done:", 1)[1]
    assert "if response == .alertSecondButtonReturn {" in done
    assert "self?.prepareToUpdateChosen()" in done
    # While a notice is held, in memory only, the menu shows its line, which opens it again, and
    # Prepare to Update….
    assert "    @Published private(set) var updateNotice: String?\n" in connection
    menu = _swift("MenuBarApp.swift")
    assert (
        "        if let notice = connection.updateNotice {\n"
        "            Button(notice) { connection.showUpdateNotice() }\n"
        "            Button(ConnectionText.prepareToUpdateItem)"
        " { connection.prepareToUpdateChosen() }\n"
        "        }\n"
    ) in menu
    words = _swift("ConnectionText.swift")
    assert 'static let prepareToUpdateButton = "Prepare to Update"' in words
    assert 'static let prepareToUpdateItem = "Prepare to Update…"' in words
    assert "static func hostTooOld(minVersion: String, hostVersion: String)" not in words
    # Nothing offers a Download.
    for path in SWIFT_FILES:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r'addButton\(withTitle: "Download', text), path.name
        assert "downloadButton" not in text and "releasesPage" not in text, path.name
    # The minimum is Lucille's: HostJSON reads it only in the form the protocol gives it
    # (spec/PROTOCOL.md section 10), before anything shows it.
    host_json = _swift("HostJSON.swift")
    too_old = host_json.split('        case "host_too_old":\n', 1)[1].split('\n        case "', 1)[
        0
    ]
    assert "HostObject.isMinHostVersion(least)" in too_old
    assert "    static func isMinHostVersion(_ text: String) -> Bool {\n" in host_json


# Ways Swift code could pass run's --allow-downgrade, which only a person in Terminal may
# (decisions/0002 §16): the flag, and its name without its dashes.
_PASSES_ALLOW_DOWNGRADE = (r"allow-downgrade", r"(?i)allow_?downgrade")


@pytest.mark.parametrize(
    "line",
    [
        '["run", "--json-events", "--allow-downgrade"]',
        'verb.append("--allow-downgrade")',
        "let allowDowngrade = true",
    ],
)
def test_the_allow_downgrade_scan_sees_each_way(line: str) -> None:
    assert _matches(_PASSES_ALLOW_DOWNGRADE, line), line


def test_the_app_never_passes_allow_downgrade() -> None:
    # A run older than the newest that ran on the profile refuses to start (exit 25), and
    # `run --allow-downgrade` gets past that, from Terminal only: an older release can be a
    # vulnerable one. The app's run verb never has it, and no Swift file names it.
    connection = _swift("Connection.swift")
    assert 'private static let runVerb = ["run", "--json-events", "--exit-on-stdin-eof"]' in (
        connection
    )
    for path in SWIFT_FILES:
        text = path.read_text(encoding="utf-8")
        assert not _matches(_PASSES_ALLOW_DOWNGRADE, text), path.name


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
    # The code window comes after the whole disk; Reset Settings comes after it, last, before the
    # app is stopped (test_the_ui_check_resets_a_settings_file_the_host_cannot_read).
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
        "_drive_reset",
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


def test_the_placement_and_update_checks_look_for_the_apps_own_words() -> None:
    # What check_app.py placement and the pairing check's too_old_then_prepare read on the screen,
    # as App/ConnectionText.swift spells it: the copy's name and the version Lucille asks for are
    # interpolated there, as the app's name is.
    text = _swift("ConnectionText.swift")
    name = check_app.APP_NAME
    copy = check_app.RENAMED_COPY
    assert copy == f"{name} 2"

    def spelled(said: str) -> str:
        return _named_in_swift(said.replace(copy, "\\(copy)"))

    for constant, said in (
        ("notInApplicationsTitle", check_app.PLACEMENT_TITLE),
        ("somewhereElse", check_app.PLACEMENT_ELSEWHERE),
        ("movingSteps", check_app.MOVING_STEPS),
        ("quitButton", check_app.QUIT_BUTTON),
        ("prepareToUpdateButton", check_app.PREPARE_TO_UPDATE),
        ("prepareToUpdateItem", check_app.PREPARE_TO_UPDATE_ITEM),
        ("notNowButton", check_app.NOT_NOW),
    ):
        assert f'static let {constant} = "{spelled(said)}"' in text, constant
    for said in (check_app.RENAMED_TITLE, check_app.RENAMED_WITH_ORIGINAL, check_app.RENAMED_ALONE):
        assert f'"{spelled(said)}"' in text, said
    assert check_app.PLACEMENT_TITLE == f"{name} needs to be in your Applications folder"
    assert check_app.RENAMED_TITLE == f"This copy is named {copy}, so it can't run"
    for steps in (check_app.PLACEMENT_ELSEWHERE, check_app.MOVING_STEPS):
        assert steps.endswith(".") and '"' not in steps, steps
    # The note for a renamed copy says to put a copy in the Trash only while the original is
    # there too: never a user's only copy.
    assert "Trash" in check_app.RENAMED_WITH_ORIGINAL
    assert "Trash" not in check_app.RENAMED_ALONE and "rename" in check_app.RENAMED_ALONE
    # The update notice's title names the version Lucille asks for, its text says in words where
    # to get the update, and its versions are in the protocol's form, each higher than the last.
    minimum, higher = check_app.UPDATE_MIN_VERSION, check_app.UPDATE_HIGHER_VERSION
    highest = check_app.UPDATE_HIGHEST_VERSION
    assert check_app.UPDATE_TITLE == f"Lucille needs {name} {minimum} or newer"
    assert check_app.UPDATE_HIGHER_TITLE == f"Lucille needs {name} {higher} or newer"
    assert check_app.UPDATE_HIGHEST_TITLE == f"Lucille needs {name} {highest} or newer"
    title = check_app.UPDATE_TITLE.replace(minimum, "\\(minVersion)")
    assert f'"{_named_in_swift(title)}"' in text
    assert check_app.UPDATE_WHERE == f"Get the newer {name} from where you got this one."
    assert _named_in_swift(check_app.UPDATE_WHERE) in text
    triple = r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})"
    for version in (minimum, higher, highest):
        assert re.fullmatch(triple, version), version
    assert tuple(map(int, higher.split("."))) > tuple(map(int, minimum.split(".")))
    assert tuple(map(int, highest.split("."))) > tuple(map(int, higher.split(".")))
    for said in (
        check_app.PLACEMENT_TITLE,
        check_app.RENAMED_TITLE,
        check_app.UPDATE_TITLE,
        check_app.UPDATE_HIGHER_TITLE,
        check_app.UPDATE_HIGHEST_TITLE,
        check_app.PREPARE_TO_UPDATE,
        check_app.PREPARE_TO_UPDATE_ITEM,
        check_app.NOT_NOW,
    ):
        assert check_app._quoted(said) == f'"{said}"'


def test_the_pairing_check_asks_for_a_newer_release_then_prepares_to_update() -> None:
    # Between Stop What's Running and Stop now, the pairing check sends host.describe with a
    # minimum this build is older than (a development build is older than every minimum,
    # spec/PROTOCOL.md section 10): the update notice says so; a second request with a higher
    # minimum, sent while it is up, brings no second note; Not Now closes it, and a third, higher
    # still, sent once it is closed, does not bring it back; the menu then holds its line, with
    # the highest version, and Prepare to Update…, which asks no Quit question, drains run and
    # quits. Stop now's step opens the app again.
    source = _CHECK_APP.read_text(encoding="utf-8")
    go = _python_function(source, "go")
    assert (
        go.index("self.stop_whats_running")
        < go.index("self.too_old_then_prepare")
        < go.index("self.stop_now")
    )
    step = _python_function(source, "too_old_then_prepare")
    assert 'self.launch("too-old", paired=True)' in step
    first = step.index("min_host_version=UPDATE_MIN_VERSION")
    second = step.index("min_host_version=UPDATE_HIGHER_VERSION")
    shown = step.index("_says(UPDATE_TITLE)")
    assert first < shown < second < step.index("_press(NOT_NOW)")
    third = step.index("min_host_version=UPDATE_HIGHEST_VERSION")
    assert step.index("_press(NOT_NOW)") < third < step.index("_choose(UPDATE_HIGHEST_TITLE)")
    assert third < step.index("_says(UPDATE_HIGHEST_TITLE) or _says(UPDATE_TITLE)")
    assert step.index("_choose(UPDATE_HIGHEST_TITLE)") < step.index(
        "_choose(PREPARE_TO_UPDATE_ITEM"
    )
    for name in (
        '"host_too_old"',
        "UPDATE_HIGHER_TITLE",
        "PREPARE_TO_UPDATE_ITEM",
        "QUIT_QUESTION",
        "DRAINED_LINE",
        "_screenshot(",
    ):
        assert name in step, name
    assert 'self.launch("stop-now", paired=True)' in _python_function(source, "stop_now")


def test_the_placement_check_sees_its_control_before_it_reads_a_refusal() -> None:
    # check_app.py placement first opens the installed copy and sees the host start on the app's
    # profile: without that, seeing no host process later would prove nothing. Then, with no copy
    # running, the build copy, and a copy named "<name> 2" in /Applications with the original
    # there and then set aside, each say why they can't run, offer only Quit, start no host
    # process for ten seconds and change nothing in the profile, and quit when Quit is pressed.
    # Whatever happens, the original goes back and the renamed copy is removed.
    source = _CHECK_APP.read_text(encoding="utf-8")
    check = _python_function(source, "check_placement")
    assert check.index("_placement_control(") < check.index("_refusing_copy(app,")
    assert check.count("_refusing_copy(") == 3
    assert check.index("try:") < check.index("RENAMED_WITH_ORIGINAL") < check.index("RENAMED_ALONE")
    assert check.index("RENAMED_ALONE") < check.index("finally:") < check.rindex("make_writable(")
    assert '"ditto"' in check and "RENAMED_COPY" in check and "PLACEMENT_ELSEWHERE" in check
    control = _python_function(source, "_placement_control")
    assert "installed_app_path()" in control and "_watch_control(" in control
    assert "_watch_profile(" in _python_function(source, "_watch_control")
    refusing = _python_function(source, "_refusing_copy")
    for name in (
        "_watch_profile(",
        "_profile_folder()",
        "REFUSAL_WATCH",
        "QUIT_BUTTON",
        "SHOW_APPLICATIONS_FOLDER",
        "_screenshot(",
        "0-placement-",
    ):
        assert name in refusing, name
    assert "shows_folder=True, require_driven=require_driven" in check
    assert check_app.REFUSAL_WATCH == 10
    assert {"install", "placement"} <= set(check_app.CHECKS)


def _placement_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, starts: bool, writes: bool
) -> tuple[Path, list[str]]:
    """What the placement check sees of a copy it opens, made up: once ``open`` has run, a host
    process of the app's profile runs (``starts``) and a file appears in the profile's folder
    (``writes``), or not. The app runs once opened; nothing waits; the control looks for a host
    process for a fifth of a second at most, and its snapshot, unified log and the listers'
    libraries are stand-ins. Returns the profile's folder and what was opened."""
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "state").mkdir()
    opened: list[str] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == "open":
            opened.append(str(argv[-1]))
            if writes:
                (profile / "state" / "written").write_text("x", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(check_app, "_profile_folder", lambda: profile)
    monkeypatch.setattr(check_app, "_running", lambda name: [99] if opened else [])
    monkeypatch.setattr(
        check_app, "_profile_processes", lambda: [4242] if opened and starts else []
    )
    # raising=False: so that these tests also run against a check without the second lister, or
    # with it under its earlier name, which gave no third part.
    monkeypatch.setattr(check_app, "_ps_profile", lambda: ([], []), raising=False)
    monkeypatch.setattr(check_app, "_ps_processes", lambda: ([], [], []), raising=False)
    monkeypatch.setattr(check_app, "_lister_pool", _NowPool, raising=False)
    for name, seconds in (
        ("CONTROL_GATE", 0.2),
        ("CONTROL_AFTER", 0.1),
        ("CONTROL_SNAPSHOT_AT", 0.1),
        ("CONTROL_DIAGNOSIS", 0.3),
    ):
        monkeypatch.setattr(check_app, name, seconds, raising=False)
    monkeypatch.setattr(check_app, "_control_snapshot", lambda work, screens: None, raising=False)
    monkeypatch.setattr(
        check_app, "_save_control_log", lambda path, opened: "a stand-in", raising=False
    )
    monkeypatch.setattr(check_app, "_lister_libraries", lambda: "a stand-in", raising=False)
    monkeypatch.setattr(check_app, "_wait_until", lambda test, seconds: test())
    monkeypatch.setattr(check_app, "_stop_the_app", lambda checker, what: None)
    monkeypatch.setattr(check_app, "_stop_earlier_copies", lambda checker, name: True)
    monkeypatch.setattr(check_app, "_screenshot", lambda path: None)
    monkeypatch.setattr(check_app, "_can_drive", lambda: False)
    monkeypatch.setattr(check_app.time, "sleep", lambda seconds: None)
    return profile, opened


@pytest.mark.parametrize(
    ("starts", "writes"), [(False, False), (True, False), (False, True), (True, True)]
)
def test_a_refusing_copy_fails_the_check_if_it_starts_or_writes_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, starts: bool, writes: bool
) -> None:
    # The placement check's two detectors, run: a copy that is not the one in /Applications
    # passes only if, once opened, it starts no host process of the app's profile and nothing in
    # the profile's folder changes.
    _placement_world(tmp_path, monkeypatch, starts=starts, writes=writes)
    monkeypatch.setattr(check_app, "REFUSAL_WATCH", 0.2)
    checker = check_app.Checker()
    copy = tmp_path / "out" / f"{check_app.APP_NAME}.app"
    words = (check_app.PLACEMENT_TITLE, check_app.PLACEMENT_ELSEWHERE)
    check_app._refusing_copy(copy, checker, tmp_path, "elsewhere", words, False)
    failed = " ".join(checker.failures)
    assert ("starts no host process" in failed) == starts, checker.failures
    assert ("changed" in failed) == writes, checker.failures
    assert len(checker.failures) == starts + writes


@pytest.mark.parametrize("starts", [False, True])
def test_the_placement_control_fails_unless_the_installed_copy_starts_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, starts: bool
) -> None:
    # Without the control seeing the copy in /Applications start the host, seeing no host process
    # from a refusing copy later would prove nothing.
    installed = tmp_path / "Applications" / f"{check_app.APP_NAME}.app"
    monkeypatch.setattr(check_app, "installed_app_path", lambda: installed)
    _, opened = _placement_world(tmp_path, monkeypatch, starts=starts, writes=False)
    checker = check_app.Checker()
    assert check_app._placement_control(checker, tmp_path, False) is False
    assert opened == [str(installed)]
    assert bool(checker.failures) != starts, checker.failures
    if not starts:
        assert any("the control: it starts the host" in what for what in checker.failures)


# --- check_app.py placement: two listers, a watch that knows when it was blind, and the control's
# record of what it saw


def _done(
    argv: list[str], code: int, out: str = "", err: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, out, err)


# A host process on the app's profile, and a running copy of the app, as ps -o command shows them.
_HOST_COMMAND = (
    f"/Applications/{check_app.APP_NAME}.app/Contents/Resources/runtime/bin/lucille-host-runtime"
    f" -I -B -X utf8 -m lucille_host --profile {check_app.APP_PROFILE} --json status"
)
_APP_COMMAND = f"/Applications/{check_app.APP_NAME}.app/Contents/MacOS/{check_app.APP_NAME}"

# How pgrep answers: (exit, stdout, stderr, the pids it means, or None where it is blind).
_PGREP_ANSWERS = [
    (0, "4242\n4343\n", "", [4242, 4343]),
    (1, "", "", []),
    # Apple's pgrep, when its request to sysmond fails (pkill.c).
    (3, "", "pgrep: sysmon request failed with error: sysmond service not found\n", None),
    (3, "", "", None),
    (2, "", "", None),
    (1, "", "pgrep: cannot read the process table\n", None),
    (0, "4242\n", "a warning\n", None),
    (0, "", "", None),
    (0, "4242\nnot-a-pid\n", "", None),
]


@pytest.mark.parametrize(("code", "out", "err", "pids"), _PGREP_ANSWERS)
def test_the_profile_lister_never_reads_a_failed_pgrep_as_none(
    monkeypatch: pytest.MonkeyPatch, code: int, out: str, err: str, pids: list[int] | None
) -> None:
    # pgrep exits 0 with the pids it found and 1 with none. Any other exit, anything on standard
    # error, or an answer that is not pids, means it could not read the process table: the lister
    # is blind, never empty, and it has a time limit.
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return _done(argv, code, out, err)

    monkeypatch.setattr("subprocess.run", run)
    if pids is None:
        with pytest.raises(check_app.ListerBlind):
            check_app._profile_processes()
    else:
        assert check_app._profile_processes() == pids
    [(argv, kwargs)] = calls
    assert argv == ["pgrep", "-f", f"lucille_host --profile {check_app.APP_PROFILE}( |$)"]
    assert kwargs.get("timeout") == 5 and kwargs.get("errors") == "replace"


@pytest.mark.parametrize(("code", "out", "err", "pids"), _PGREP_ANSWERS)
def test_the_check_never_reads_a_failed_pgrep_as_the_app_not_running(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    code: int,
    out: str,
    err: str,
    pids: list[int] | None,
) -> None:
    # The pgrep -x that says whether the app runs follows the same rule. Where it is blind, a
    # wait for the app to stop, or to start, has not held; the check stops the app as if it ran;
    # and what the check prints says so.
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return _done(argv, code, out, err)

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(check_app.time, "sleep", lambda seconds: None)
    name = check_app.APP_NAME
    if pids is None:
        with pytest.raises(check_app.ListerBlind):
            check_app._running(name)
        assert check_app._running_now(name) is None
        assert check_app._may_run(name) is True
        assert "pgrep could not say whether" in capsys.readouterr().out
        assert check_app._wait_until(lambda: not check_app._running(name), 0) is False
        assert check_app._wait_until(lambda: bool(check_app._running(name)), 0) is False
    else:
        assert check_app._running(name) == [str(pid) for pid in pids]
        assert check_app._may_run(name) is bool(pids)
        assert check_app._wait_until(lambda: not check_app._running(name), 0) is not pids
    argv, kwargs = calls[0]
    assert argv == ["pgrep", "-x", name]
    assert kwargs.get("timeout") == 5


def test_the_check_stops_the_app_where_pgrep_cannot_say_whether_it_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # pgrep fails once, then reads none: the check sends SIGTERM anyway, and says that the copy
    # stopped only once a pgrep has read that nothing of that name runs.
    answers = iter([_done([], 3, "", "pgrep: sysmon request failed\n")] + [_done([], 1)] * 10)
    ran: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        ran.append(argv)
        return next(answers) if argv[:2] == ["pgrep", "-x"] else _done(argv, 0)

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(check_app.time, "sleep", lambda seconds: None)
    checker = check_app.Checker()
    assert check_app._stop_earlier_copies(checker, check_app.APP_NAME) is True
    assert ["pkill", "-TERM", "-x", check_app.APP_NAME] in ran
    assert checker.failures == []
    assert "pgrep could not say whether" in capsys.readouterr().out


@pytest.mark.parametrize("lister", ["pgrep", "ps"])
@pytest.mark.parametrize("raised", ["timeout", "missing"])
def test_a_lister_that_does_not_answer_in_time_is_blind(
    monkeypatch: pytest.MonkeyPatch, lister: str, raised: str
) -> None:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv[0] == lister and kwargs.get("timeout") == 5, (argv, kwargs)
        if raised == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr("subprocess.run", run)
    with pytest.raises(check_app.ListerBlind, match=lister):
        if lister == "pgrep":
            check_app._profile_processes()
        else:
            check_app._ps_processes()


def test_the_ps_lister_sees_the_profile_and_every_child_of_a_running_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The second lister reads ps, not pgrep: the host's processes on the app's profile, matched as
    # pgrep matches them, and every child of a running copy of the app, wherever that copy is and
    # whatever its argv says (ps may show a child by its name alone), with its state and command
    # line. Its own pgrep is never one, nor a host on another profile. A child whose arguments ps
    # could not read, which it shows as its name in parentheses, is also asked of lsof (below).
    rows = [
        "    1     0 Ss   /sbin/launchd",
        f"   99     1 S    {_APP_COMMAND}",
        "  100    99 U    (lucille-host-ru)",
        f"  101     1 S    {_HOST_COMMAND}",
        f"  102    77 S    /usr/bin/pgrep -f lucille_host --profile {check_app.APP_PROFILE} ",
        "  103     1 S    /x/python -m lucille_host --profile other --json status",
        f"  104    98 S    /x/{check_app.RENAMED_COPY}.app/Contents/MacOS/{check_app.APP_NAME}",
        "  105   104 S    /bin/sleep 5",
        "  106     1 Z    ",
        "  107   101 S    /bin/sleep 9",
    ]
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        if argv[0] == "lsof":
            return _done(argv, 1)
        return _done(argv, 0, "\n".join(rows) + "\n")

    monkeypatch.setattr("subprocess.run", run)
    hosts, children, unread = check_app._ps_processes()
    assert (hosts, children) == (
        [101],
        [(100, "U (lucille-host-ru)"), (105, "S /bin/sleep 5")],
    )
    assert [(u.pid, u.parent, u.said, u.child, u.texts) for u in unread] == [
        (100, 99, "U (lucille-host-ru)", True, ())
    ]
    [(argv, kwargs), (lsof, _)] = calls
    assert argv == ["ps", "-axww", "-o", "pid=,ppid=,stat=,command="]
    assert kwargs.get("timeout") == 5 and kwargs.get("errors") == "replace"
    assert lsof == ["lsof", "-w", "-a", "-p", "100", "-d", "txt", "-F", "pn"]


# Command lines a host on the app's profile may have, and some that are not one: whether the
# listers' pattern matches each.
_PROFILE_LINES = [
    (f"/x/python -m lucille_host --profile {check_app.APP_PROFILE} --json --version", True),
    (f"/x/python -m lucille_host --profile {check_app.APP_PROFILE} run --json-events", True),
    # A command line that ends at the profile's name: no verb after it.
    (f"/x/python -m lucille_host --profile {check_app.APP_PROFILE}", True),
    (f"/x/python -m lucille_host --profile {check_app.APP_PROFILE}2 --json status", False),
    (f"/x/python -m lucille_host --profile {check_app.APP_PROFILE}s", False),
    (f"/x/python -m lucille_host --profile {check_app.APP_PROFILE}-old", False),
    ("/x/python -m lucille_host --profile other --json status", False),
]


@pytest.mark.parametrize(("command", "matched"), _PROFILE_LINES)
def test_both_listers_match_a_host_whose_command_line_ends_at_the_profile(
    monkeypatch: pytest.MonkeyPatch, command: str, matched: bool
) -> None:
    # The pattern ends at the profile's name, then a space or the end of the command line: a host
    # started with no verb after its profile is still one, and a profile whose name only begins
    # with the app's is not. It is plain POSIX extended syntax, with no backslash, so that pgrep's
    # regcomp should read it as Python's re does (seen with Linux's pgrep; Apple's is not yet
    # seen, which the real-lister test below is for); ps matches with re.
    rows = f"   99     1 S    {_APP_COMMAND}\n  101     1 S    {command}\n"
    monkeypatch.setattr("subprocess.run", lambda argv, **kwargs: _done(argv, 0, rows))
    hosts, _, _ = check_app._ps_processes()
    assert hosts == ([101] if matched else [])
    pattern = check_app.PROFILE_PROCESS
    assert bool(re.search(pattern, command)) is matched
    assert pattern == f"lucille_host --profile {check_app.APP_PROFILE}( |$)" and "\\" not in pattern


@pytest.mark.parametrize("lister", ["pgrep", "ps"])
def test_the_real_listers_find_a_process_whose_command_line_ends_at_the_profile(
    lister: str,
) -> None:
    # The listers as they are on this machine, with this pattern: a process whose command line
    # ends at the profile's name is found, and one whose profile only begins with it is not. On a
    # Mac this is Apple's pgrep and ps. Where the lister is missing, or cannot read the process
    # table here at all, there is nothing to learn. So pgrep is asked first with a plain pattern,
    # no metacharacter in it: where that reads the table, pgrep must read it with the listers'
    # pattern too, and a pattern it cannot compile (pgrep exits 2) fails here, never skips.
    if shutil.which(lister) is None:
        pytest.skip(f"no {lister} here")
    sleeper = "import time; time.sleep(30)"
    tail = f"lucille_host --profile {check_app.APP_PROFILE}"
    ours = subprocess.Popen([sys.executable, "-c", sleeper, "-m", *tail.split()])
    other = subprocess.Popen([sys.executable, "-c", sleeper, "-m", *f"{tail}2".split()])
    try:
        if lister == "pgrep":
            try:
                check_app._pgrep(["-f", tail])
            except check_app.ListerBlind as exc:
                pytest.skip(f"pgrep cannot read the process table here: {exc}")
            hosts = check_app._profile_processes()
        else:
            try:
                hosts, _, _ = check_app._ps_processes()
            except check_app.ListerBlind as exc:
                pytest.skip(f"ps cannot read the process table here: {exc}")
        assert ours.pid in hosts and other.pid not in hosts, (hosts, ours.pid, other.pid)
    finally:
        for process in (ours, other):
            process.kill()
            process.wait()


@pytest.mark.parametrize(
    ("code", "out", "err"),
    [
        (1, "", ""),
        (0, "", ""),
        (0, f"   99     1 S    {_APP_COMMAND}\n", "ps: a warning\n"),
        (0, f"   99     1 S    {_APP_COMMAND}\n  x  y  z\n", ""),
    ],
)
def test_the_ps_lister_is_blind_when_ps_fails_or_says_what_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch, code: int, out: str, err: str
) -> None:
    monkeypatch.setattr("subprocess.run", lambda argv, **kwargs: _done(argv, code, out, err))
    with pytest.raises(check_app.ListerBlind, match="ps"):
        check_app._ps_processes()


def test_what_the_listers_and_diagnostics_read_need_not_be_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A process's command line can hold any bytes. Stand-ins for ps, pgrep and log, first on
    # PATH, each write a byte that is not UTF-8, and the check runs them as it runs the real
    # ones: none raises, and each reads what it can.
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    app = f"{check_app.APP_NAME}.app/Contents/MacOS/{check_app.APP_NAME}"
    scripts = {
        "ps": (
            'case "$3" in *wchan*) printf "  PID  PPID STAT WCHAN ELAPSED COMMAND\\n";; esac\n'
            f'printf "   99     1 S    /x/\\377/{app}\\n  100    99 S    /bin/sleep \\377\\n"\n'
        ),
        "pgrep": 'printf "4242\\n"\n',
        "log": 'printf "a line \\377\\n"\n',
    }
    for name, body in scripts.items():
        script = bin_ / name
        script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ.get('PATH', '')}")
    assert check_app._ps_processes() == ([], [(100, "S /bin/sleep \ufffd")], [])
    assert check_app._profile_processes() == [4242]
    assert check_app._save_app_processes(tmp_path / "control-ps.txt") == [99]
    said = check_app._save_control_log(tmp_path / "control-log.txt", time.time())
    assert "is in control-log.txt" in said, said
    assert "\ufffd" in (tmp_path / "control-log.txt").read_text(encoding="utf-8")
    capsys.readouterr()


@pytest.mark.parametrize(
    ("pgrep", "ps", "none"),
    [
        ([], ([], []), True),
        ("blind", ([], []), True),
        ([], "blind", True),
        ("blind", "blind", False),
        ([4242], "blind", False),
        ("blind", ([4242], []), False),
        ([], ([], [(100, "S /bin/sleep 5")]), True),
    ],
)
def test_no_host_process_is_said_only_after_a_lister_read_the_table(
    monkeypatch: pytest.MonkeyPatch, pgrep: Any, ps: Any, none: bool
) -> None:
    # The waits for no host process of the app's profile ("before the control", after a copy
    # stops) take a reading of none from either lister, never a blind one; a child of the app is
    # not a host process for them (the app is stopped first).
    def answer(given: Any) -> Callable[[], Any]:
        def lister() -> Any:
            if given == "blind":
                raise check_app.ListerBlind("could not read")
            return given

        return lister

    monkeypatch.setattr(check_app, "_profile_processes", answer(pgrep))
    # raising=False: so that this test also runs against a check whose ps lister had the earlier
    # name and gave no third part.
    monkeypatch.setattr(check_app, "_ps_profile", answer(ps), raising=False)
    monkeypatch.setattr(
        check_app, "_ps_processes", answer(ps if ps == "blind" else (*ps, [])), raising=False
    )
    assert check_app._no_profile_process() is none
    said = check_app._profile_processes_said()
    assert ("could not read" in said) == ("blind" in (pgrep, ps)), said


def _reading(start: float, end: float, read: bool, error: str = "", lister: str = "pgrep") -> Any:
    return check_app._Reading(
        lister, start, end, frozenset() if read else None, (), "" if read else error
    )


def _watched(*readings: Any, began: float = 0.0, ended: float | None = None) -> Any:
    watch = check_app._Watch(began=began, since=began)
    for reading in readings:
        watch.add(reading)
    if ended is not None:
        watch.ended = ended
    return watch


def test_a_watch_is_blind_where_no_lister_read_for_longer_than_the_limit() -> None:
    # Readings at 0 and 0.1 s, then two calls that failed, then one at 1.05 s: from the second
    # reading's start, 0.1 s, to the third's end, 2.3 s, the check could not see, longer than the
    # 2 s limit: a burst of host processes could have come and gone in it.
    watch = _watched(
        _reading(0.0, 0.05, True),
        _reading(0.1, 0.15, True, lister="ps"),
        _reading(0.2, 1.2, False, "pgrep did not answer in 5 s"),
        _reading(1.25, 2.2, False, "ps exited 1", lister="ps"),
        _reading(2.25, 2.3, True),
    )
    assert len(watch.readings) == 5 and watch.errors() == [
        (0.2, "pgrep did not answer in 5 s"),
        (1.25, "ps exited 1"),
    ]
    assert watch.longest_gap == pytest.approx(2.2)
    [(start, end, why)] = watch.blind()
    assert (start, end) == pytest.approx((0.1, 2.3))
    assert why == "pgrep did not answer in 5 s; ps exited 1"
    assert watch.read_by("pgrep") and watch.read_by("ps")
    # The same, a second shorter: within the limit, so not blind, and recorded.
    shorter = _watched(
        _reading(0.0, 0.05, True),
        _reading(0.1, 0.15, True, lister="ps"),
        _reading(0.2, 0.6, False, "pgrep did not answer in 5 s"),
        _reading(0.65, 1.0, False, "ps exited 1", lister="ps"),
        _reading(1.05, 1.1, True),
    )
    assert shorter.blind() == [] and shorter.longest_gap == pytest.approx(1.0)
    assert watch.recorded() == [
        "recorded, not a gate: no reading from 0.10 s to 2.30 s (2.20 s); lister errors in it: "
        "pgrep did not answer in 5 s; ps exited 1"
    ]
    assert check_app.BLIND_LIMIT == 2.0 and check_app.RECORDED_GAP == 0.35
    assert shorter.recorded() == [
        "recorded, not a gate: no reading from 0.10 s to 1.10 s (1.00 s); lister errors in it: "
        "pgrep did not answer in 5 s; ps exited 1"
    ]
    # No reading at all is blind from the watch's start to its end, however short.
    never = _watched(_reading(0.0, 0.05, False, "pgrep exited 3"))
    assert [(a, b) for a, b, _ in never.blind()] == [pytest.approx((0.0, 0.05))]
    assert not never.read_by("pgrep")
    # Readings 0.1 s long, starting 0.25 s apart: from one's start to the next one's end is
    # 0.35 s, which is not recorded, and so is the stretch after the last one's start.
    fine = _watched(*(_reading(t, t + 0.1, True) for t in (0.0, 0.25, 0.5, 0.75)), ended=1.0)
    assert fine.blind() == [] and fine.recorded() == []
    assert fine.longest_gap == pytest.approx(0.35)


def test_a_stretch_runs_from_one_readings_start_to_the_next_ones_end() -> None:
    # A lister reads the table somewhere within its call. Calls that start 0.31 s apart, the
    # second slow, leave 0.57 s in which nothing proves a read; the watch records it, and is not
    # blind for it.
    slow = _watched(_reading(0.0, 0.01, True), _reading(0.31, 0.57, True), ended=0.6)
    assert slow.blind() == []
    [(start, end, why)] = slow.stretches(check_app.RECORDED_GAP)
    assert (start, end) == pytest.approx((0.0, 0.57)) and why == ""
    assert slow.recorded() == ["recorded, not a gate: no reading from 0.00 s to 0.57 s (0.57 s)"]
    # Two listers' calls that overlap count together: a reading inside the stretch closes it.
    both = _watched(
        _reading(0.0, 0.3, True),
        _reading(0.05, 0.1, True, lister="ps"),
        _reading(0.3, 0.6, True),
        _reading(0.15, 0.2, True, lister="ps"),
        _reading(0.4, 0.45, True, lister="ps"),
        ended=0.62,
    )
    assert both.recorded() == [] and both.longest_gap == pytest.approx(0.3)
    # A watch that starts, or ends, with a long stretch with no reading is blind there: the
    # stretch before the first reading's end, and the stretch after the last one's start. Shorter
    # than the limit, the same stretches are recorded.
    start = _watched(_reading(2.5, 2.55, True), _reading(2.6, 2.65, True), ended=2.7)
    assert [(a, b) for a, b, _ in start.blind()] == [pytest.approx((0.0, 2.55))]
    end = _watched(_reading(0.0, 0.05, True), _reading(0.1, 0.15, True), ended=2.6)
    assert [(a, b) for a, b, _ in end.blind()] == [pytest.approx((0.1, 2.6))]
    start = _watched(_reading(0.5, 0.55, True), _reading(0.6, 0.65, True), ended=0.7)
    assert start.blind() == [] and start.recorded() == [
        "recorded, not a gate: no reading from 0.00 s to 0.55 s (0.55 s)"
    ]
    end = _watched(_reading(0.0, 0.05, True), _reading(0.1, 0.15, True), ended=0.6)
    assert end.blind() == [] and end.recorded() == [
        "recorded, not a gate: no reading from 0.10 s to 0.60 s (0.50 s)"
    ]


def test_stretches_that_overlap_are_each_said_with_their_own_length() -> None:
    # Both listers take 0.2 s a call, ps starting 0.05 s after pgrep: a reading ends every
    # 0.05 or 0.2 s, but from each ps call's start to the end of the next ps call is 0.4 s, and
    # those stretches overlap. Each is said on its own, with its own length; none is joined into
    # one span that no reading crossed, which would read as a real blind spell.
    readings = []
    for step in range(8):
        at = 0.25 * step
        readings += [_reading(at, at + 0.2, True), _reading(at + 0.05, at + 0.25, True, "", "ps")]
    watch = _watched(*readings, ended=2.0)
    assert watch.blind() == []
    said = watch.recorded()
    assert said == [
        f"recorded, not a gate: no reading from {0.25 * step + 0.05:.2f} s to "
        f"{0.25 * step + 0.45:.2f} s (0.40 s)"
        for step in range(7)
    ], said
    assert watch.longest_gap == pytest.approx(0.4) and watch.blind() == []
    # A stretch that lies within a longer one is not said again: from 0 s and from 0.1 s, where
    # a slow pgrep call starts, the next reading to end is ps's at 0.65 s, so the one stretch is
    # from 0 s.
    nested = _watched(
        _reading(0.0, 0.05, True),
        _reading(0.1, 1.0, True),
        _reading(0.6, 0.65, True, lister="ps"),
        _reading(0.7, 0.75, True, lister="ps"),
        ended=1.0,
    )
    assert nested.recorded() == [
        "recorded, not a gate: no reading from 0.00 s to 0.65 s (0.65 s)"
    ], nested.recorded()


def _fast_pgrep_slow_ps() -> Any:
    """A watch of 10 s, made up: pgrep answers none every 0.1 s in 0.02 s, as Apple's may where
    sysmond gives no command line, and each ps call takes 1.5 s, the next 0.05 s after it."""
    readings = [_reading(step / 10, step / 10 + 0.02, True) for step in range(100)]
    readings += [_reading(1.55 * call, 1.55 * call + 1.5, True, lister="ps") for call in range(6)]
    return _watched(*readings, ended=10.0)


def test_a_watch_gives_the_longest_stretch_with_no_reading_by_ps_alone() -> None:
    # pgrep's fast readings keep the stretches over both listers short, and a pgrep reading of
    # none can be wrong. From one ps reading's start to the next one's end is 3.05 s: a host
    # process could run that long with no ps reading inside its run. The watch's line gives that
    # stretch too, not as a gate, so that a run like this one shows.
    watch = _fast_pgrep_slow_ps()
    assert watch.longest_gap == pytest.approx(0.12) and watch.blind() == []
    assert watch.longest_gap_of("ps") == pytest.approx(3.05)
    assert watch.longest_gap_of("pgrep") == pytest.approx(0.12)
    assert (
        "longest gap between readings 0.12 s; between ps's own readings 3.05 s (not a gate); "
        "lister errors 0"
    ) in watch.summary(), watch.summary()
    # ps never reading the table: its own stretch is the whole watch.
    alone = _watched(_reading(0.0, 0.02, True), _reading(0.1, 0.12, False, lister="ps"), ended=10.0)
    assert alone.longest_gap_of("ps") == pytest.approx(10.0)


def test_a_span_longer_than_the_longest_stretch_holds_a_whole_reading() -> None:
    # What the docs rest on: in any span of the watch longer than its longest stretch with no
    # reading, a reading both started and ended, and with ps's readings alone, a ps reading did.
    # Made-up watches with calls of every length, overlapping, and some blind.
    rng = random.Random(20261109)
    for _ in range(300):
        readings = []
        for lister in check_app.LISTERS:
            at = rng.uniform(0.0, 0.5)
            while at < 10.0:
                took = rng.choice([0.01, 0.1, 0.3, 0.8, 1.6])
                readings.append(_reading(at, at + took, rng.random() > 0.15, "x", lister))
                at += took + rng.choice([0.05, 0.05, 0.2])
        watch = _watched(*readings, ended=max(r.ended for r in readings))
        for lister in (None, "ps"):
            read = [r for r in readings if r.hosts is not None and lister in (None, r.lister)]
            longest = watch.longest_gap if lister is None else watch.longest_gap_of(lister)
            for _ in range(40):
                length = longest + rng.uniform(1e-6, 1.0)
                if length > watch.ended:
                    continue
                start = rng.uniform(0.0, watch.ended - length)
                assert any(start <= r.started and r.ended <= start + length for r in read), (
                    lister,
                    longest,
                    start,
                    length,
                )


def test_the_two_listers_run_at_once_so_one_held_never_holds_back_the_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real threads and real time: pgrep is held for a second at each call; ps answers at once.
    # In half a second ps reads again and again, and the watch ends on time, not waiting for
    # pgrep.
    def held() -> list[int]:
        time.sleep(1.0)
        return []

    monkeypatch.setattr(check_app, "_profile_processes", held)
    monkeypatch.setattr(check_app, "_ps_profile", lambda: ([], []), raising=False)
    monkeypatch.setattr(check_app, "_ps_processes", lambda: ([], [], []), raising=False)
    # The stand-in worlds replace this pool with one that runs each call at once.
    assert "pool = _lister_pool()" in _python_function(
        _CHECK_APP.read_text(encoding="utf-8"), "_watch_profile"
    )
    started = time.monotonic()
    watch = check_app._watch_profile(0.5)
    took = time.monotonic() - started
    assert took < 0.9, took
    assert not watch.read_by("pgrep")
    assert sum(r.lister == "ps" for r in watch.readings) >= 4, watch.readings


class _NowPool:
    """The listers' threads, made up: each call runs at once, here, so that a made-up clock moves
    in a known order."""

    def submit(self, call: Callable[..., Any], *args: Any) -> concurrent.futures.Future[Any]:
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        try:
            future.set_result(call(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        return None


class _Clock:
    """Time in a stand-in world: it moves only when the code under test sleeps, or when a
    stand-in lister takes its time."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)


@dataclasses.dataclass
class _ListerWorld:
    """The process table as the placement check's two listers read it, made up. ``pgrep(t)`` and
    ``ps(t)`` give (exit, stdout, stderr) ``t`` seconds after ``open`` ran (None before), and each
    call takes ``took`` seconds (``ps_took`` for ps, where given). Asking whether the app runs
    takes ``asking`` seconds, so the open and the watch's start differ. Once ``open`` was given a
    file for the app's standard error, the app writes its login item line there ``marker_at``
    seconds after the open. ``lsof(t, pids)`` gives lsof's answer about ``pids`` (by default, it
    lists nothing and exits 1), each call taking ``took`` seconds too, and may raise as running
    it would. ``named(t)`` gives the answer of the control's lsof by name (by default, nothing,
    exit 1), each call taking ``named_took`` seconds, none by default, so that the listers' times
    stay what they were without it. A process runs now where ps would list it now. The control's
    snapshots are counted, and when its unified log was asked for is recorded."""

    pgrep: Callable[[float | None], tuple[int, str, str]]
    ps: Callable[[float | None], tuple[int, str, str]]
    took: float = 0.02
    ps_took: float | None = None
    asking: float = 0.25
    marker_at: float | None = None
    lsof: Callable[[float | None, list[int]], tuple[int, str, str]] | None = None
    named: Callable[[float | None], tuple[int, str, str]] | None = None
    named_took: float = 0.0
    named_calls: int = 0
    clock: _Clock = dataclasses.field(default_factory=_Clock)
    opened: list[list[str]] = dataclasses.field(default_factory=list)
    opened_at: float | None = None
    err: Path | None = None
    marked: bool = False
    snapshots: list[Path] = dataclasses.field(default_factory=list)
    logs: list[float | None] = dataclasses.field(default_factory=list)

    def since_open(self) -> float | None:
        return None if self.opened_at is None else self.clock.now - self.opened_at

    def running(self, name: str) -> list[str]:
        self.clock.now += self.asking
        return ["99"] if self.opened else []

    def runs_now(self, pid: int) -> bool:
        _, out, _ = self.ps(self.since_open())
        return any(line.split()[:1] == [str(pid)] for line in out.splitlines())

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == "open":
            self.opened.append(list(argv))
            self.opened_at = self.clock.now
            if "--stderr" in argv:
                self.err = Path(argv[argv.index("--stderr") + 1])
            return _done(argv, 0)
        if argv[0] in ("pgrep", "ps"):
            at = self.since_open()
            took = self.ps_took if argv[0] == "ps" and self.ps_took is not None else self.took
            self.clock.now += took
            if (
                self.err is not None
                and self.marker_at is not None
                and at is not None
                and at >= self.marker_at
                and not self.marked
            ):
                with self.err.open("a", encoding="utf-8") as file:
                    file.write("login item: notFound\n")
                self.marked = True
            return _done(argv, *(self.pgrep if argv[0] == "pgrep" else self.ps)(at))
        if argv[0] == "lsof" and "-c" in argv:
            self.named_calls += 1
            self.clock.now += self.named_took
            if self.named is None:
                return _done(argv, 1)
            return _done(argv, *self.named(self.since_open()))
        if argv[0] == "lsof":
            self.clock.now += self.took
            pids = [int(pid) for pid in argv[argv.index("-p") + 1].split(",")]
            if self.lsof is None:
                return _done(argv, 1)
            return _done(argv, *self.lsof(self.since_open(), pids))
        return _done(argv, 0)


def _in_lister_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: _ListerWorld) -> Path:
    """Run the placement check's control or a refusing copy in ``world``: nothing is driven, the
    app's profile is a folder of its own, and the copy in /Applications is under ``tmp_path``."""
    installed = tmp_path / "Applications" / f"{check_app.APP_NAME}.app"
    profile = tmp_path / "profile"
    (profile / "state").mkdir(parents=True)
    monkeypatch.setattr("subprocess.run", world.run)
    monkeypatch.setattr(check_app.time, "monotonic", world.clock.monotonic)
    monkeypatch.setattr(check_app.time, "sleep", world.clock.sleep)
    monkeypatch.setattr(check_app, "_lister_pool", _NowPool, raising=False)
    # raising=False: so that these tests also run against a check that never asked.
    monkeypatch.setattr(check_app, "_runs_now", world.runs_now, raising=False)
    monkeypatch.setattr(check_app, "installed_app_path", lambda: installed)
    monkeypatch.setattr(check_app, "_profile_folder", lambda: profile)
    monkeypatch.setattr(check_app, "_running", world.running)
    monkeypatch.setattr(check_app, "_pair_state", lambda python: "none")
    monkeypatch.setattr(check_app, "_stop_the_app", lambda checker, what: None)
    monkeypatch.setattr(check_app, "_stop_earlier_copies", lambda checker, name: True)
    monkeypatch.setattr(check_app, "_screenshot", lambda path: None)
    monkeypatch.setattr(check_app, "_can_drive", lambda: False)

    def snapshot(work: Path, screens: Path) -> None:
        # On its own thread, it takes a moment of real time: the control waits for it.
        threading.Event().wait(0.05)
        world.snapshots.append(work)

    monkeypatch.setattr(check_app, "_control_snapshot", snapshot, raising=False)

    def save_log(path: Path, opened: float) -> str:
        world.logs.append(world.since_open())
        return f"{path.name}: a stand-in"

    monkeypatch.setattr(check_app, "_save_control_log", save_log, raising=False)
    monkeypatch.setattr(
        check_app, "_lister_libraries", lambda: "the listers: stand-in", raising=False
    )
    return installed


def _none(at: float | None) -> tuple[int, str, str]:
    return 1, "", ""


def _ps_none(at: float | None) -> tuple[int, str, str]:
    return 0, f"   99     1 S    {_APP_COMMAND}\n" if at is not None else "    1     0 Ss   x\n", ""


def _blind_while(
    blind: Callable[[float], bool], lister: Callable[[float | None], tuple[int, str, str]]
) -> Callable[[float | None], tuple[int, str, str]]:
    def answer(at: float | None) -> tuple[int, str, str]:
        if at is not None and blind(at):
            return 3, "", "sysmon request failed\n"
        return lister(at)

    return answer


def _host_from(when: float) -> Callable[[float | None], tuple[int, str, str]]:
    def pgrep(at: float | None) -> tuple[int, str, str]:
        return (0, "4242\n", "") if at is not None and at >= when else (1, "", "")

    return pgrep


def _ps_with(*rows: tuple[float, float, str]) -> Callable[[float | None], tuple[int, str, str]]:
    """ps, with each row in its list from ``when`` to ``until`` seconds after the open."""

    def ps(at: float | None) -> tuple[int, str, str]:
        code, out, err = _ps_none(at)
        for when, until, row in rows:
            if at is not None and when <= at < until:
                out += row + "\n"
        return code, out, err

    return ps


_PS_HOST_ROW = f" 4242    99 S    {_HOST_COMMAND}"


def _ps_host_from(when: float) -> Callable[[float | None], tuple[int, str, str]]:
    return _ps_with((when, 1e9, _PS_HOST_ROW))


def _refusing_in(world: _ListerWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    _in_lister_world(tmp_path, monkeypatch, world)
    checker = check_app.Checker()
    copy = tmp_path / "out" / f"{check_app.APP_NAME}.app"
    words = (check_app.PLACEMENT_TITLE, check_app.PLACEMENT_ELSEWHERE)
    check_app._refusing_copy(copy, checker, tmp_path, "elsewhere", words, False)
    return checker


_NOT_JUDGED = "not judged: the check was blind (see above)"
_COULD_SEE_2S = (
    "the check could see for all 10 s: ps read the process table, and no stretch of more than "
    "2.0 s went without a reading"
)


def test_a_refusing_copy_fails_when_both_listers_are_blind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # pgrep's request to sysmond fails and ps fails too: the check saw nothing because it could
    # not look, so it may not say that the copy started nothing. One cause, one failure: whether
    # the copy started anything is not judged, and the check says so.
    world = _ListerWorld(
        pgrep=lambda at: (3, "", "pgrep: sysmon request failed\n"),
        ps=lambda at: (1, "", "ps: no\n"),
    )
    checker = _refusing_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [_COULD_SEE_2S], checker.failures
    assert f"{_NOT_JUDGED}: for 10 s it runs, and starts no host process" in said, said
    assert "FAIL: for 10 s it runs" not in said, said
    assert "the check was blind from 0.00 s to 10.0" in said, said
    assert "sysmon request failed" in said and "ps never read the process table" in said, said


def test_a_refusing_copy_passes_when_ps_reads_though_pgrep_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # pgrep fails throughout and ps reads the table at every call: the copy's ten seconds are
    # covered by ps, and the run says how often pgrep failed.
    world = _ListerWorld(pgrep=lambda at: (3, "", "pgrep: sysmon request failed\n"), ps=_ps_none)
    checker = _refusing_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [], checker.failures
    errors = re.search(r"lister errors (\d+)", said)
    assert errors and int(errors.group(1)) > 50, said
    assert "sysmon request failed" in said


def test_a_refusing_copy_fails_when_only_pgrep_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # ps fails throughout and pgrep reads none at every call: pgrep alone can read none where a
    # host process runs (pkill.c falls back to a process's name), so the check may not say that
    # the copy started nothing, and fails once for it.
    world = _ListerWorld(pgrep=_none, ps=lambda at: (1, "", "ps: no\n"))
    checker = _refusing_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [_COULD_SEE_2S], checker.failures
    assert "ps never read the process table" in said and _NOT_JUDGED in said, said
    assert "the check was blind from" not in said, said


@pytest.mark.parametrize(("took", "blind"), [(0.5, False), (1.1, True)])
def test_a_refusing_copy_fails_only_when_its_readings_are_more_than_the_limit_apart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    took: float,
    blind: bool,
) -> None:
    # Each lister's call takes half a second, and then 1.1 s, one after the other: from one
    # reading's start to the next one's end is twice that. A second is within the 2 s limit, and
    # each such stretch is recorded, not a gate; 2.2 s is not, and the watch is blind there: each
    # such stretch is said, with its length, where it fails the watch, and is not called "not a
    # gate".
    world = _ListerWorld(pgrep=_none, ps=_ps_none, took=took)
    checker = _refusing_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == ([_COULD_SEE_2S] if blind else []), checker.failures
    gap = re.search(r"longest gap between readings (\d\.\d\d) s", said)
    assert gap and float(gap.group(1)) == pytest.approx(2 * took, abs=0.01), said
    recorded = re.findall(r"recorded, not a gate: no reading from .* \((\d\.\d\d) s\)", said)
    failing = re.findall(r"the check was blind from .* \((\d\.\d\d) s\): no reading", said)
    each, neither = (failing, recorded) if blind else (recorded, failing)
    assert neither == [], said
    assert len(each) >= 8 and all(float(n) == pytest.approx(2 * took, abs=0.01) for n in each)


def test_a_refusing_copy_records_the_stretches_slow_calls_leave_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # pgrep is blind throughout and each ps call takes 0.2 s: ps's calls start 0.25 s apart, but
    # from one's start to the next one's end is 0.45 s, in which nothing proves a read. That is
    # within the limit: the copy passes, and each stretch is recorded with its own length, never
    # joined with the next into one span.
    world = _ListerWorld(
        pgrep=lambda at: (3, "", "pgrep: sysmon request failed\n"), ps=_ps_none, ps_took=0.2
    )
    checker = _refusing_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [], checker.failures
    assert re.search(r"longest gap between readings 0\.4\d s", said), said
    lengths = re.findall(r"recorded, not a gate: no reading from .* \((\d\.\d\d) s\)", said)
    assert len(lengths) > 20 and all(0.35 < float(n) < 0.5 for n in lengths), lengths


def _steady_watch(*gaps: float, host_at: float | None = None) -> Any:
    """A refusing copy's watch, made up: the two listers read in turn, each call 0.02 s long and
    0.1 s after the last one started, for 10 s, with no error; for each of ``gaps``, at 2 s, 4 s
    and so on, one stretch of that many seconds with no reading, from a reading's start to the
    next one's end. With ``host_at``, the reading then saw a host process."""
    took = 0.02
    starts = [step / 10 for step in range(100)]
    for number, gap in enumerate(gaps, start=1):
        hole = 2.0 * number
        starts = [start for start in starts if not hole < start < hole + gap]
        starts.append(hole + gap - took)
    readings = [
        check_app._Reading(
            check_app.LISTERS[index % 2],
            start,
            start + took,
            frozenset({4242}) if start == host_at else frozenset(),
        )
        for index, start in enumerate(sorted(starts))
    ]
    return _watched(*readings, ended=10.0)


@pytest.mark.parametrize("gaps", [(0.45, 0.95), (1.9,)])
def test_a_refusing_copy_passes_with_stretches_under_the_limit_and_records_each(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    gaps: tuple[float, ...],
) -> None:
    # Run 42681859860 measured stretches with no reading of up to 0.41 s in refusing copies'
    # watches, and 0.95 s in a control's, with no lister error: a passing app. Stretches like
    # these, within the 2 s limit, fail nothing; each is recorded on its own line, with its own
    # length.
    _in_lister_world(tmp_path, monkeypatch, _ListerWorld(pgrep=_none, ps=_ps_none))
    monkeypatch.setattr(check_app, "_watch_profile", lambda seconds: _steady_watch(*gaps))
    checker = check_app.Checker()
    copy = tmp_path / "out" / f"{check_app.APP_NAME}.app"
    words = (check_app.PLACEMENT_TITLE, check_app.PLACEMENT_ELSEWHERE)
    check_app._refusing_copy(copy, checker, tmp_path, "elsewhere", words, False)
    said = capsys.readouterr().out
    assert checker.failures == [], checker.failures
    recorded = [line for line in said.splitlines() if "recorded, not a gate" in line]
    assert recorded == [
        f"elsewhere: recorded, not a gate: no reading from {2.0 * number:.2f} s to "
        f"{2.0 * number + gap:.2f} s ({gap:.2f} s)"
        for number, gap in enumerate(gaps, start=1)
    ], recorded
    assert re.search(
        rf"longest gap between readings {max(gaps):.2f} s; between ps's own readings "
        r"\d\.\d\d s \(not a gate\); lister errors 0",
        said,
    ), said


@pytest.mark.parametrize(
    ("host_at", "running", "failures"),
    [
        (None, ["99"], 1),
        (None, None, 1),
        (5.0, ["99"], 2),
        (None, [], 2),
    ],
)
def test_a_refusing_copy_with_a_stretch_over_the_limit_fails_once_for_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host_at: float | None,
    running: list[str] | None,
    failures: int,
) -> None:
    # A stretch of 2.1 s with no reading, and no lister error: the watch is blind there, and the
    # copy fails for that, once. Whether it started anything is then not judged, unless what the
    # check did see says so on its own: a host process a reading saw, or a reading that the copy
    # no longer runs. Where pgrep cannot say whether it runs, that too is not judged.
    _in_lister_world(tmp_path, monkeypatch, _ListerWorld(pgrep=_none, ps=_ps_none))
    watch = _steady_watch(2.1, host_at=host_at)
    monkeypatch.setattr(check_app, "_watch_profile", lambda seconds: watch)
    monkeypatch.setattr(check_app, "_running_now", lambda name: running)
    checker = check_app.Checker()
    copy = tmp_path / "out" / f"{check_app.APP_NAME}.app"
    words = (check_app.PLACEMENT_TITLE, check_app.PLACEMENT_ELSEWHERE)
    check_app._refusing_copy(copy, checker, tmp_path, "elsewhere", words, False)
    said = capsys.readouterr().out
    assert len(checker.failures) == failures and checker.failures[0] == _COULD_SEE_2S, (
        checker.failures
    )
    assert "the check was blind from 2.00 s to 4.10 s (2.10 s): no reading" in said, said
    assert "recorded, not a gate: no reading from 2.00 s to 4.10 s" not in said, said
    assert (_NOT_JUDGED in said) == (failures == 1), said
    if failures == 2:
        assert checker.failures[1].startswith("for 10 s it runs"), checker.failures
        assert "the check was also blind" in said, said


def test_a_refusing_copy_records_a_short_stretch_and_fails_on_a_long_one_said_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two stretches with no reading, of 0.45 s and 2.1 s, and no lister error: the first is
    # recorded, not a gate; the second fails the watch as blind and is said there, once, with its
    # length, and never called "not a gate".
    _in_lister_world(tmp_path, monkeypatch, _ListerWorld(pgrep=_none, ps=_ps_none))
    monkeypatch.setattr(check_app, "_watch_profile", lambda seconds: _steady_watch(0.45, 2.1))
    checker = check_app.Checker()
    copy = tmp_path / "out" / f"{check_app.APP_NAME}.app"
    words = (check_app.PLACEMENT_TITLE, check_app.PLACEMENT_ELSEWHERE)
    check_app._refusing_copy(copy, checker, tmp_path, "elsewhere", words, False)
    said = capsys.readouterr().out
    assert checker.failures == [_COULD_SEE_2S], checker.failures
    recorded = [line for line in said.splitlines() if "recorded, not a gate" in line]
    assert recorded == [
        "elsewhere: recorded, not a gate: no reading from 2.00 s to 2.45 s (0.45 s)"
    ], recorded
    assert "the check was blind from 4.00 s to 6.10 s (2.10 s): no reading" in said, said
    assert said.count("4.00 s to 6.10 s") == 1, said


def test_a_refusing_copy_passes_on_fast_pgrep_readings_and_its_line_says_ps_was_slow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # pgrep answers none fast and often while ps takes 1.5 s a call: no stretch over both
    # listers reaches the limit, and the copy passes, as before. Its line now says that ps's own
    # longest stretch was 3.05 s, which is what a host process would have had to outlast for ps
    # to see it.
    _in_lister_world(tmp_path, monkeypatch, _ListerWorld(pgrep=_none, ps=_ps_none))
    monkeypatch.setattr(check_app, "_watch_profile", lambda seconds: _fast_pgrep_slow_ps())
    checker = check_app.Checker()
    copy = tmp_path / "out" / f"{check_app.APP_NAME}.app"
    words = (check_app.PLACEMENT_TITLE, check_app.PLACEMENT_ELSEWHERE)
    check_app._refusing_copy(copy, checker, tmp_path, "elsewhere", words, False)
    said = capsys.readouterr().out
    assert checker.failures == [], checker.failures
    line = next(line for line in said.splitlines() if "the watch of the app's profile" in line)
    assert "between ps's own readings 3.05 s (not a gate)" in line, line


@pytest.mark.parametrize(
    ("blind", "stretch"),
    [
        (lambda at: at < 3.0, r"blind from 0\.00 s to 2\.\d\d s"),
        (lambda at: at >= 7.0, r"blind from 6\.\d\d s to 10\.\d\d s"),
    ],
)
def test_a_refusing_copy_fails_when_it_was_blind_at_the_start_or_the_end_of_its_watch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    blind: Callable[[float], bool],
    stretch: str,
) -> None:
    # Both listers are blind for the first seconds of the watch, when a refusing copy's launch
    # burst would come, or for its last seconds: either way the check names the stretch, and the
    # errors in it, and fails once.
    world = _ListerWorld(pgrep=_blind_while(blind, _none), ps=_blind_while(blind, _ps_none))
    checker = _refusing_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [_COULD_SEE_2S], checker.failures
    assert re.search(stretch + r" \(\d\.\d\d s\): .*sysmon request failed", said), said
    assert _NOT_JUDGED in said, said


def test_a_refusing_copy_fails_when_ps_sees_a_child_of_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A child of the refusing copy, whatever its command line, is something it started.
    world = _ListerWorld(pgrep=_none, ps=_ps_with((2.0, 2.5, " 5555    99 U    (lucille-host-ru)")))
    checker = _refusing_in(world, tmp_path, monkeypatch)
    assert checker.failures == [
        f"for {check_app.REFUSAL_WATCH} s it runs, and starts no host process of the app's profile"
    ]


def test_a_refusing_copy_prints_how_its_watch_went(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _ListerWorld(pgrep=_none, ps=_ps_none)
    checker = _refusing_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [], checker.failures
    line = next(line for line in said.splitlines() if "lister calls" in line)
    assert "elsewhere" in line and "lister errors 0" in line, line
    assert re.search(r"lister calls \(\d{3} pgrep; \d{3} ps\) in 10\.0 s from 0\.00 s", line), line
    assert re.search(r"longest call 0\.02 s; longest gap between readings 0\.0[4-9] s", line), line


def _control_in(
    world: _ListerWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, Path]:
    installed = _in_lister_world(tmp_path, monkeypatch, world)
    checker = check_app.Checker()
    check_app._placement_control(checker, tmp_path, False)
    return checker, installed


def _case(said: str) -> str:
    return next(line for line in said.splitlines() if line.startswith("the case: "))


@pytest.mark.parametrize("marker_at", [1.0, None])
def test_a_failed_control_says_whether_the_app_had_read_its_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    marker_at: float | None,
) -> None:
    # Neither lister ever sees a host process. With the app's login item line at 1 s, its first
    # status read had ended inside the gate, so a host process ran that no reading saw; without
    # it, nothing by the end of the watch, 60 s after its start. Either way the gate fails, at
    # 5 s the check took its snapshot, and after the failure it saved the unified log; the app's
    # standard error went to a file it read.
    world = _ListerWorld(pgrep=_none, ps=_ps_none, marker_at=marker_at)
    checker, installed = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert any("the control: it starts the host" in what for what in checker.failures)
    assert "no host process of the app's profile was seen in 30 s" in said
    case = _case(said)
    if marker_at is None:
        assert re.search(r"no host process and no status read by 60\.\d s", case), case
    else:
        assert re.search(
            r"first status read had ended by 1\.\d s, inside the 30 s gate .*, but no reading saw "
            r"a host process by 60\.\d s",
            case,
        ), case
    [argv] = world.opened
    assert argv[0] == "open" and argv[-1] == str(installed)
    assert argv[argv.index("--stdout") + 1] == str(tmp_path / "control-app-stdout.txt")
    assert argv[argv.index("--stderr") + 1] == str(tmp_path / "control-app-stderr.txt")
    assert world.snapshots == [tmp_path]
    assert re.search(r"no host process seen 5\.[01]\d s after the open", said), said
    [log] = world.logs
    # The watch's 60 s count from its start, a quarter of a second after the open.
    assert log is not None and 60.2 <= log < 61, world.logs


def test_a_failed_control_says_when_the_host_came_after_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The first host process appears 30.5 s after the open, past the gate, which ends 30 s after
    # the watch's start: the gate still fails, and the check, watching on, says when it came.
    world = _ListerWorld(pgrep=_host_from(30.5), ps=_ps_host_from(30.5), marker_at=32.0)
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert any("the control: it starts the host" in what for what in checker.failures)
    case = _case(said)
    assert re.search(r"the first host process appeared at 30\.\d s, after the 30 s gate", case)
    assert "missed" not in case, case
    assert len(world.snapshots) == 1 and len(world.logs) == 1
    # Its watch's line records the burst too, as far as the watch saw it.
    line = next(line for line in said.splitlines() if line.startswith("the control's watch"))
    assert re.search(r"last seen by pgrep at 3\d\.\d\d s; ps at 3\d\.\d\d s", line), line
    assert "and pgrep and ps still saw one at its last reading" in line, line


def test_a_failed_control_names_a_late_start_though_a_reading_also_missed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Nothing until after the gate; then the app's status read ends at 30.6 s, and the first
    # reading that sees a host process is at 31.0 s. The start came after the gate: that is the
    # case, and the missed reading is said too.
    world = _ListerWorld(pgrep=_host_from(31.0), ps=_ps_host_from(31.0), marker_at=30.6)
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    case = _case(capsys.readouterr().out)
    assert re.search(r"the first host process appeared at 31\.\d s, after the 30 s gate", case)
    assert re.search(r"status read had ended by 30\.\d s, before any reading saw", case), case
    assert "inside the 30 s gate" not in case, case


def test_a_failed_control_says_the_status_read_came_before_the_first_sighting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The app's status read had ended by 1 s, and a host process is first seen only at 40 s: the
    # listers missed the first ones, which is the case it names, not a late start.
    world = _ListerWorld(pgrep=_host_from(40.0), ps=_ps_host_from(40.0), marker_at=1.0)
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert any("the control: it starts the host" in what for what in checker.failures)
    case = _case(said)
    assert re.search(
        r"status read had ended by 1\.\d s, inside the 30 s gate .*no reading saw a host "
        r"process until 40\.",
        case,
    ), case
    assert "after the 30 s gate" not in case, case


def test_the_control_fails_on_a_child_of_the_app_that_is_not_the_host_on_its_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The app starts its host on another profile: a child of the app, which neither lister's
    # pattern matches. The control proves the pattern matches the app's real host, so it fails,
    # and says what it saw.
    wrong = _HOST_COMMAND.replace(f"--profile {check_app.APP_PROFILE} ", "--profile default ")
    world = _ListerWorld(
        pgrep=_none, ps=_ps_with((0.3, 1e9, f" 5555    99 S    {wrong}")), marker_at=1.5
    )
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert any("the control: it starts the host" in what for what in checker.failures)
    case = _case(said)
    assert re.search(r"a child of the app from 0\.\d s \('5555 S .*--profile default", case), case
    assert re.search(r"no host process the gate matches by 60\.\d s", case), case
    assert re.search(r"status read had ended by 1\.\d s", case), case


def test_a_failed_control_names_a_child_seen_before_the_host_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A child of the app that ps shows only by its name, from 0.4 s, and the host on the app's
    # profile only from 30.5 s, past the gate (the set-up, not a cause). The control fails, takes
    # its snapshot and the unified log, and names that case.
    ps = _ps_with((0.4, 30.5, " 5555    99 U    (lucille-host-ru)"), (30.5, 1e9, _PS_HOST_ROW))
    world = _ListerWorld(pgrep=_host_from(30.5), ps=ps, marker_at=32.0)
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert any("the control: it starts the host" in what for what in checker.failures)
    assert len(world.snapshots) == 1 and len(world.logs) == 1
    case = _case(said)
    assert re.search(
        r"a child of the app from 0\.\d s \('5555 U \(lucille-host-ru\)'\), but no host process "
        r"the gate matches until 30\.\d s; the app's first status read had ended by 32\.\d s",
        case,
    ), case
    assert re.search(r"the first child of the app: '5555 U \(lucille-host-ru\)'", said), said


def test_the_control_passes_on_either_lister_and_says_which_saw_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Only ps sees the host's status, from 0.4 s after the open: the control passes, says pgrep
    # never saw it, and leaves a note a log search finds.
    world = _ListerWorld(pgrep=_none, ps=_ps_host_from(0.4), marker_at=1.5)
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [], checker.failures
    line = next(line for line in said.splitlines() if "the control's watch" in line)
    assert "pgrep never" in line and re.search(r"ps at 0\.4\d s", line), line
    assert re.search(r"status read had ended by 1\.[56]\d s", line), line
    assert "note: the control passed, but pgrep never saw a host process" in said, said
    assert world.snapshots == [] and world.logs == []


def test_a_passing_control_notes_a_lister_that_was_blind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # pgrep is blind for the first second and then sees the host, as ps does from the start.
    world = _ListerWorld(
        pgrep=_blind_while(lambda at: at < 1.0, _host_from(0.4)),
        ps=_ps_host_from(0.4),
        marker_at=1.5,
    )
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [], checker.failures
    assert re.search(
        r"note: the control passed, but pgrep was blind \d+ times, first: pgrep exited 3", said
    ), said
    assert "never saw" not in said, said


def test_a_passing_control_records_both_listers_and_the_status_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Times count from the open, not from the watch's start a quarter of a second later.
    world = _ListerWorld(pgrep=_host_from(0.4), ps=_ps_host_from(0.4), marker_at=1.5)
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [], checker.failures
    line = next(line for line in said.splitlines() if "the control's watch" in line)
    assert re.search(r"from 0\.2\d s;", line), line
    # The host on the app's profile is a child of the app too, and ps says so.
    assert re.search(r"pgrep at 0\.4\d s; ps at 0\.4\d s; ps, a child of the app at 0\.4\d s", line)
    assert "the first child of the app: '4242 S " in line, line
    assert "lister errors 0" in line and "status read had ended by 1." in line, line
    assert world.snapshots == [] and world.logs == []
    assert "the listers: stand-in" in said and "note: the control passed" not in said


def _host_between(when: float, until: float) -> Callable[[float | None], tuple[int, str, str]]:
    def pgrep(at: float | None) -> tuple[int, str, str]:
        return (0, "4242\n", "") if at is not None and when <= at < until else (1, "", "")

    return pgrep


def test_a_passing_control_records_the_last_sightings_and_the_burst(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The app's launch burst, as the listers see it: host processes from 0.4 s to 0.9 s after the
    # open by pgrep, and from 0.45 s to 0.85 s by ps, and its first status read ended at 0.8 s.
    # After the gate the watch goes on until both listers have seen a host process, the app has
    # written its line, and each lister's last reading saw none, and its line records when each
    # lister last saw one, and the burst from the first sighting to the last. Recorded, not a
    # gate: it is there to check the 2 s limit against real launches.
    world = _ListerWorld(
        pgrep=_host_between(0.4, 0.9), ps=_ps_with((0.45, 0.85, _PS_HOST_ROW)), marker_at=0.8
    )
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [], checker.failures
    line = next(line for line in said.splitlines() if line.startswith("the control's watch"))
    first = re.search(r"first seen by pgrep at (0\.\d\d) s; ps at (0\.\d\d) s", line)
    last = re.search(r"last seen by pgrep at (0\.\d\d) s; ps at (0\.\d\d) s", line)
    burst = re.search(r"the burst, from the first host sighting to the last, (0\.\d\d) s", line)
    assert first and last and burst, line
    assert 0.4 <= float(first.group(1)) < 0.5 and 0.8 < float(last.group(1)) < 0.9, line
    assert 0.45 <= float(first.group(2)) < 0.55 and 0.75 < float(last.group(2)) < 0.85, line
    assert float(burst.group(1)) == pytest.approx(
        max(float(last.group(1)), float(last.group(2))) - float(first.group(1)), abs=0.011
    )
    assert "still saw" not in line and "recorded, not a gate" in line, line
    # The watch stopped soon after the burst ended, not 5 s after the gate.
    took = re.search(r"in (\d+\.\d) s from", line)
    assert took and float(took.group(1)) < 1.0, line


def test_a_passing_control_whose_host_keeps_running_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A host process that runs on (``run``, while the profile holds a confirmed pair): the watch
    # goes on its 5 s after the gate, and its line says that both listers still saw one at their
    # last reading, so the burst is only as long as the watch.
    world = _ListerWorld(pgrep=_host_from(0.4), ps=_ps_host_from(0.4), marker_at=0.8)
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [], checker.failures
    line = next(line for line in said.splitlines() if line.startswith("the control's watch"))
    assert re.search(r"in 5\.\d s from 0\.2\d s", line), line
    assert re.search(r"last seen by pgrep at 5\.\d\d s; ps at 5\.\d\d s", line), line
    assert "and pgrep and ps still saw one at its last reading" in line, line


def test_a_passing_control_never_takes_a_blind_reading_for_the_end_of_the_burst(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # pgrep sees a host process from 0.4 s and is blind from 0.6 s on; ps sees it only until
    # 0.7 s. pgrep's last reading that read the table saw one, so the burst is not known to be
    # over: the watch goes on its 5 s, and its line says that pgrep still saw one.
    world = _ListerWorld(
        pgrep=_blind_while(lambda at: at >= 0.6, _host_from(0.4)),
        ps=_ps_with((0.4, 0.7, _PS_HOST_ROW)),
        marker_at=0.8,
    )
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [], checker.failures
    line = next(line for line in said.splitlines() if line.startswith("the control's watch"))
    assert re.search(r"in 5\.\d s from 0\.2\d s", line), line
    assert "and pgrep still saw one at its last reading" in line, line


@pytest.mark.parametrize(("starts", "took"), [(True, 0.35), (False, 0.35), (True, 1.1)])
def test_a_control_records_its_long_stretches_and_does_not_gate_on_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    starts: bool,
    took: float,
) -> None:
    # Calls of 0.35 s each, as in xcode-27's control in run 42681859860 (0.71 s at most): the
    # stretches with no reading, 0.7 s, are recorded, each on its own line, and the control
    # passes on its host process all the same. Where no host process comes, the control fails on
    # that alone, and its detail records the same stretches. The control has no gate on them:
    # calls of 1.1 s leave stretches of 2.2 s, past a refusing copy's limit, and these too are
    # recorded, not a gate.
    host = 0.4 if starts else 1e9
    world = _ListerWorld(pgrep=_host_from(host), ps=_ps_host_from(host), marker_at=1.5, took=took)
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == (
        []
        if starts
        else ["the control: it starts the host on the app's profile, and the check sees it"]
    ), checker.failures
    lengths = re.findall(
        r"^the control: recorded, not a gate: no reading from .* \((\d\.\d\d) s\)$",
        said,
        re.MULTILINE,
    )
    assert lengths and all(float(n) == pytest.approx(2 * took, abs=0.01) for n in lengths), said


def test_the_control_keeps_its_gate_and_opens_the_copy_with_its_output_kept() -> None:
    # The gate stays at 30 s and a refusing copy's watch at 10; the snapshot at 5 s and the watch
    # to 60 s are for the record. The control's open keeps the app's standard output and error.
    # Its gate takes a host process the listers match by its command line, or a child of the app
    # that runs that copy's bundled runtime where ps could read none of the children's arguments
    # (_Watch.passed_by), never a bare child of the app.
    source = _CHECK_APP.read_text(encoding="utf-8")
    control = _python_function(source, "_placement_control")
    assert '"--stdout"' in control and '"--stderr"' in control
    assert "_watch_control(work, err_log, opened, opened_wall, python_in(installed))" in control
    watch = _python_function(source, "_watch_control")
    assert "found.passed_by(runtime)" in watch and "watch.passed_by(runtime)" in watch
    assert "found.seen" not in watch and "watch.seen" not in watch
    assert check_app.CONTROL_GATE == 30 and check_app.REFUSAL_WATCH == 10
    assert check_app.CONTROL_SNAPSHOT_AT == 5 and check_app.CONTROL_DIAGNOSIS == 60
    # A stretch longer than 0.35 s with no reading is recorded; one longer than 2 s makes a
    # refusing copy's watch blind: more than twice the longest that run 42681859860 measured on
    # any image, 0.95 s, in xcode-27's control.
    assert check_app.RECORDED_GAP == 0.35 and check_app.BLIND_LIMIT == 2.0
    assert check_app.LISTER_LIMIT == 5 and check_app.POLL_PAUSE < check_app.RECORDED_GAP
    assert check_app.BLIND_LIMIT > 2 * 0.95


# --- check_app.py placement: a host process whose arguments ps cannot read, told by its
# executable. In run 42700559897 (xcode-27, macOS 27.0.1) ps showed the app's first child as
# '19072 R (lucille-host-run)', and neither lister matched a host process by its command line in
# 60 s, though the app's first status read had ended by 2.21 s.

_SHORT = "(lucille-host-run)"
# The bundled runtime's own name, as lsof gives it, read from the build, not from the check, so
# that the tests that give it run against a check that does not have it.
_RUNTIME_NAME = Path(build_app.INTERPRETER).name
# What Apple's lsof writes where a file's name goes when it cannot read a region of a process
# (dialects/darwin/libproc/dproc.c, process_text, read at 7a8a1b2), for an errno other than ESRCH
# or EINVAL: its own message, not a file.
_REGION_ERROR = "region info error: Operation not permitted"


def _runtime_of(app: Path) -> str:
    return str(check_app.python_in(app))


def _installed_in(tmp_path: Path) -> Path:
    """Where :func:`_in_lister_world` puts the copy in /Applications."""
    return tmp_path / "Applications" / f"{check_app.APP_NAME}.app"


def _lsof_giving(
    texts: Mapping[int, Sequence[str]],
) -> Callable[[float | None, list[int]], tuple[int, str, str]]:
    """lsof -F pn, made up: for each pid asked about, the text files ``texts`` gives it, and exit 1
    where any pid asked about has none, as lsof exits when it cannot find one."""

    def lsof(at: float | None, pids: list[int]) -> tuple[int, str, str]:
        out = "".join(
            f"p{pid}\n" + "".join(f"n{path}\n" for path in texts[pid])
            for pid in pids
            if texts.get(pid)
        )
        return (0 if all(texts.get(pid) for pid in pids) else 1), out, ""

    return lsof


def test_ps_shows_the_bundled_runtime_by_its_name_cut_to_sixteen_characters() -> None:
    # Where ps cannot read a process's arguments it prints its name in parentheses, the kernel's
    # p_comm, cut to MAXCOMLEN, 16 (adv_cmds' ps/print.c, getproclline). Seen in run 42700559897:
    # '(lucille-host-run)'. A copy's bundled runtime is told by its path's end, wherever the copy
    # is and whatever its folder is called, the copy the placement check sets aside included.
    assert check_app.RUNTIME_SHORT_NAME == _SHORT
    assert check_app.RUNTIME_SHORT_NAME == f"({Path(build_app.INTERPRETER).name[:16]})"
    runtime = check_app.RUNTIME_EXECUTABLE
    for path, matched in [
        (_runtime_of(Path("/Applications") / f"{check_app.APP_NAME}.app"), True),
        (_runtime_of(Path("/x") / f"{check_app.RENAMED_COPY}.app"), True),
        (_runtime_of(Path("/Applications") / f".{check_app.APP_NAME}.app.set-aside-by-x"), True),
        ("/opt/other/bin/lucille-host-runtime", False),
        (_runtime_of(Path("/x") / f"{check_app.APP_NAME}.app") + "-old", False),
        ("/usr/bin/python3", False),
    ]:
        assert bool(runtime.search(path)) is matched, path


def test_the_ps_lister_asks_lsof_for_what_runs_where_it_cannot_read_the_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # For each row whose arguments ps could not read, a child of a running copy of the app or a
    # process shown by the bundled runtime's short name, the ps lister asks lsof, once per
    # reading, for the files it maps (txt), with what is left of the reading's 5 s: lsof reads
    # those from the process's mapped regions, not from its arguments. A row whose arguments ps
    # read is judged by them, and lsof is not asked about it; nor about the check's own processes,
    # which are left out. Where lsof gives no file, why says whether the process still ran.
    own = os.getpid()
    installed = _runtime_of(Path("/Applications") / f"{check_app.APP_NAME}.app")
    renamed = _runtime_of(Path("/Applications") / f"{check_app.RENAMED_COPY}.app")
    rows = [
        "    1     0 Ss   /sbin/launchd",
        f"   99     1 S    {_APP_COMMAND}",
        f"  100    99 R    {_SHORT}",
        f"  101     1 S    {_HOST_COMMAND}",
        f"  102     1 S    {_SHORT}",
        f"  103     1 S    {_SHORT}",
        f"  104     1 S    {_SHORT}",
        "  105     1 S    (sleep)",
        f"  106 {own:>5} S    {_SHORT}",
        f"{own:>5}     1 S    {_SHORT}",
        "  108    99 S    /bin/sleep 5",
        "  109    99 U    (Capsule)",
        "  110    99 U    (Capsule)",
    ]
    texts = {
        100: ["/usr/lib/dyld", installed],
        102: [renamed],
        104: ["/opt/other/bin/lucille-host-runtime"],
        109: [f"/Applications/{check_app.APP_NAME}.app/Contents/MacOS/{check_app.APP_NAME}"],
    }
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        if argv[0] == "lsof":
            pids = [int(pid) for pid in argv[argv.index("-p") + 1].split(",")]
            return _done(argv, *_lsof_giving(texts)(None, pids))
        return _done(argv, 0, "\n".join(rows) + "\n")

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(check_app, "_runs_now", lambda pid: pid != 103)
    before = time.monotonic()
    hosts, children, unread = check_app._ps_processes()
    assert hosts == [101]
    assert [pid for pid, _ in children] == [100, 108, 109, 110]
    [_, (lsof, kwargs)] = calls
    assert lsof == ["lsof", "-w", "-a", "-p", "100,102,103,104,109,110", "-d", "txt", "-F", "pn"]
    assert 4 < kwargs["timeout"] <= 5 and kwargs.get("errors") == "replace", kwargs
    found = {u.pid: u for u in unread}
    assert all(before <= u.asked <= time.monotonic() and u.seen_by == "ps" for u in unread)
    assert sorted(found) == [100, 102, 103, 104, 109, 110]
    assert found[100].child and found[100].executable == installed and found[100].host
    assert found[100].said == f"R {_SHORT}" and found[100].parent == 99
    # Reparented, it runs another copy's runtime: a host by its executable, whatever its parent.
    assert not found[102].child and found[102].executable == renamed and found[102].host
    # lsof listed nothing for it: possibly a host, never taken for none.
    assert found[103].texts == () and found[103].maybe and not found[103].host
    assert "lsof listed no text file for it" in found[103].why, found[103].why
    assert found[103].why.endswith("; it had ended by the time lsof answered"), found[103].why
    assert found[110].why.endswith("; a process of that pid still ran once lsof had answered")
    # Another program of that name, outside any copy: neither.
    assert found[104].executable is None and not found[104].host and not found[104].maybe
    # A child of the app in parentheses that runs the app itself: neither, but still its child.
    assert found[109].child and not found[109].host and not found[109].maybe
    # One of which lsof says nothing is possibly a host only where ps shows the runtime's name.
    assert found[110].child and found[110].texts == () and not found[110].maybe


@pytest.mark.parametrize(
    ("failure", "said"),
    [
        ("missing", "lsof could not start"),
        ("timeout", "lsof did not answer in 5 s"),
        ("nothing", "lsof listed no text file for it (exit 1"),
        ("garbage", "lsof listed no text file for it (exit 0"),
    ],
)
def test_what_runs_is_unknown_where_lsof_fails_and_the_check_goes_on(
    monkeypatch: pytest.MonkeyPatch, failure: str, said: str
) -> None:
    # lsof missing (off a Mac where it is not installed), held, finding nothing, or answering what
    # is not its list: each pid gets no file and why, nothing raises, and a pid it did list keeps
    # its files. No pid, no call.
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "lsof" and kwargs.get("timeout") == 5, argv
        if failure == "missing":
            raise FileNotFoundError("lsof")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if failure == "nothing":
            return _done(argv, 1, "p42\nn/bin/sleep\n", "")
        return _done(argv, 0, "not\nits list\n", "")

    monkeypatch.setattr("subprocess.run", run)
    texts = check_app._texts_of([42, 43])
    assert sorted(texts) == [42, 43]
    if failure == "nothing":
        assert texts[42] == (("/bin/sleep",), "")
    else:
        assert texts[42][0] == () and said in texts[42][1], texts
    assert texts[43][0] == () and said in texts[43][1], texts
    monkeypatch.setattr("subprocess.run", lambda argv, **kwargs: pytest.fail(f"ran {argv}"))
    assert check_app._texts_of([]) == {}


def _names(path: str, expected: str | Path) -> bool:
    """Whether ``path`` names ``expected``, compared as strings first, as a file only where it is
    one, so that a name that is not a path is a failed match and not an error."""
    return path == str(expected) or (os.path.exists(path) and os.path.samefile(path, expected))


def test_the_real_lsof_names_the_program_a_process_runs() -> None:
    # lsof as it is on this machine: the files a sleeping /bin/sleep maps include /bin/sleep, or
    # the file /bin/sleep names. On a Mac this is Apple's lsof, whose -d txt lists the program and
    # the libraries it maps. A process that has ended gets none, and why says it had ended.
    if shutil.which("lsof") is None:
        pytest.skip("no lsof here")
    sleeper = subprocess.Popen(["/bin/sleep", "30"])
    try:
        texts, why = check_app._texts_of([sleeper.pid])[sleeper.pid]
        assert why == "" and any(_names(path, "/bin/sleep") for path in texts), (texts, why)
        assert all(path.startswith("/") for path in texts), texts
    finally:
        sleeper.kill()
        sleeper.wait()
    texts, why = check_app._texts_of([sleeper.pid])[sleeper.pid]
    assert texts == () and "lsof listed no text file for it" in why, why
    assert why.endswith("; it had ended by the time lsof answered"), why


def test_the_real_lsof_finds_a_process_by_the_runtimes_name_with_its_parent(
    tmp_path: Path,
) -> None:
    # lsof as it is on this machine, asked as the control's reader by name asks it: a copy of
    # /bin/sleep at a copy's runtime path, started by a shell, is found by its name, with the
    # shell as its parent and the copy among the files it maps. Its parent maps no copy of the
    # app, so it is no child of one. Seen here with Linux's lsof; on a Mac, Apple's.
    if shutil.which("lsof") is None:
        pytest.skip("no lsof here")
    runtime = Path(_runtime_of(tmp_path / f"{check_app.APP_NAME}.app"))
    runtime.parent.mkdir(parents=True)
    # copyfile and chmod, not copy2: copy2 also copies /bin/sleep's file flags, which macOS refuses
    # to set on a copy of a system binary (seen in CI, job 129545633292: PermissionError, errno 1).
    shutil.copyfile("/bin/sleep", runtime)
    runtime.chmod(0o755)
    shell = subprocess.Popen(
        ["/bin/sh", "-c", '"$0" 30 & wait', str(runtime)], start_new_session=True
    )
    try:
        found: list[Any] = []
        error = ""
        deadline = time.monotonic() + 10
        while not found and time.monotonic() < deadline:
            seen, error = check_app._named_processes()
            found = [each for each in seen if each.parent == shell.pid]
            time.sleep(0.1)
        assert error == "" and len(found) == 1, (found, error)
        [each] = found
        assert each.seen_by == check_app.NAMED and each.named and not each.child, each
        assert any(_names(path, runtime) for path in each.texts), each
        assert each.executable is not None and _names(each.executable, runtime), each
    finally:
        os.killpg(shell.pid, signal.SIGKILL)
        shell.wait()


def _child_world(
    tmp_path: Path, texts: Mapping[int, Sequence[str]], *rows: tuple[float, float, str]
) -> _ListerWorld:
    """pgrep never matches; ps shows ``rows``; lsof gives ``texts``; the app's login item line
    comes at 1.5 s."""
    return _ListerWorld(pgrep=_none, ps=_ps_with(*rows), lsof=_lsof_giving(texts), marker_at=1.5)


def test_the_control_passes_on_a_child_that_runs_its_runtime_where_ps_cannot_read_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # As in run 42700559897: from 0.4 s ps shows a child of the app only as '(lucille-host-run)',
    # and pgrep never matches. lsof says the child runs the copy's own bundled runtime: the gate
    # holds by its executable. The line says so, with the child's pid, what it runs and what ps
    # showed, and that this run does not prove the listers' pattern; the app's children, as ps
    # showed them, are recorded once. No snapshot or unified log: the gate held.
    runtime = _runtime_of(_installed_in(tmp_path))
    world = _child_world(tmp_path, {5555: [runtime]}, (0.4, 2.0, f" 5555    99 R    {_SHORT}"))
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [], checker.failures
    gate = next(line for line in said.splitlines() if line.startswith("the control's gate held"))
    assert re.fullmatch(
        r"the control's gate held by its executable, its arguments unreadable: pid 5555, a child "
        rf"of the app, has the runtime's name and maps {re.escape(runtime)} \(by lsof, asked at "
        rf"0\.4\d s\), and ps showed it as 'R {re.escape(_SHORT)}' at 0\.4\d s; ps read the "
        r"arguments of none of the app's children, so this run does not prove that the listers' "
        r"pattern matches the app's command line",
        gate,
    ), gate
    record = [line for line in said.splitlines() if "the app's children, as ps first" in line]
    assert len(record) == 1 and re.fullmatch(
        rf"the control: the app's children, as ps first showed them: 5555 'R {re.escape(_SHORT)}', "
        rf"its arguments unreadable, by lsof, asked at 0\.4\d s, it maps {re.escape(runtime)}",
        record[0],
    ), said
    named = [line for line in said.splitlines() if line.startswith("the control: lsof, by name")]
    assert len(named) == 1 and re.fullmatch(
        r"the control: lsof, by name, recorded, not a gate: \d+ calls, longest 0\.00 s, lsof "
        r"errors 0; it saw no process of the runtime's name",
        named[0],
    ), named
    line = next(line for line in said.splitlines() if "the control's watch" in line)
    assert re.search(r"ps, by executable \(its arguments unreadable\) at 0\.4\d s", line), line
    # The gate held at its first such reading, and the watch went on for the record for 5 s, not
    # to the gate's 30 s.
    assert re.search(r"lister calls \(.*\) in 5\.\d s from 0\.2\d s", line), line
    assert world.snapshots == [] and world.logs == []


@pytest.mark.parametrize(
    "child",
    ["elsewhere", "another copy", "unknown", "an error", "no lsof", "not a child", "none"],
)
def test_the_control_still_fails_unless_a_child_runs_that_copys_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    child: str,
) -> None:
    # A child whose arguments ps cannot read passes the gate only if lsof says it maps that copy's
    # own bundled runtime: not another program, not another copy's runtime, not where lsof lists
    # nothing, says only that it could not read the process (Apple's lsof writes that where a
    # file's name goes) or is not there, not one that maps it but is not the app's child, and not
    # with no child at all. The case says what lsof gave.
    other = _runtime_of(tmp_path / f"{check_app.RENAMED_COPY}.app")
    runtime = _runtime_of(_installed_in(tmp_path))
    texts = {
        "elsewhere": {5555: ["/usr/bin/python3"]},
        "another copy": {5555: [other]},
        "an error": {5555: [_REGION_ERROR]},
        "not a child": {5555: [runtime]},
    }
    parent = 1 if child == "not a child" else 99
    row = (0.4, 2.0, f" 5555 {parent:>5} R    {_SHORT}")
    world = _child_world(tmp_path, texts.get(child, {}), *([] if child == "none" else [row]))
    if child == "no lsof":

        def missing(at: float | None, pids: list[int]) -> tuple[int, str, str]:
            raise FileNotFoundError("lsof")

        world.lsof = missing
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [
        "the control: it starts the host on the app's profile, and the check sees it"
    ], checker.failures
    assert "the control's gate held" not in said, said
    assert len(world.snapshots) == 1 and len(world.logs) == 1
    case = _case(said)
    expected = {
        "not a child": "but no reading saw a host process by 60.",
        "elsewhere": "5555 maps /usr/bin/python3 and no copy's runtime (by lsof)",
        "another copy": f"5555 maps {other} (by lsof), not that copy's runtime",
        "unknown": "5555: lsof gave no executable (lsof listed no text file for it",
        "an error": f"5555: lsof gave no executable (lsof said {_REGION_ERROR!r}; ",
        "no lsof": "5555: lsof gave no executable (lsof could not start",
        "none": "but no reading saw a host process by 60.",
    }[child]
    assert expected in case, case
    if child in ("none", "not a child"):
        assert "the control: ps saw no child of the app" in said, said


def test_the_controls_gate_needs_every_child_ps_read_to_match_the_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Where ps could read the arguments of a child of the app, they are what the control proves
    # the pattern against: here one child runs the copy's runtime with arguments ps cannot read,
    # and another, on another profile, shows its arguments, which the pattern does not match. The
    # control fails, and says why the first did not pass it.
    runtime = _runtime_of(_installed_in(tmp_path))
    wrong = _HOST_COMMAND.replace(f"--profile {check_app.APP_PROFILE} ", "--profile default ")
    world = _child_world(
        tmp_path,
        {5555: [runtime]},
        (0.4, 2.0, f" 5555    99 R    {_SHORT}"),
        (0.6, 2.0, f" 5556    99 S    {wrong}"),
    )
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert any("the control: it starts the host" in what for what in checker.failures)
    case = _case(said)
    assert "5555 maps that copy's runtime (by lsof), from 0.4 s" in case, case
    assert re.search(
        r"ps read the arguments of a child of the app that the listers' pattern does not match "
        r"\(5556 'S .*--profile default .*'\), so what a child maps does not pass the gate",
        case,
    ), case


def test_the_control_fails_on_a_child_that_runs_its_runtime_only_after_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The gate is 30 s for a child told by its executable too: one first seen at 30.5 s does not
    # pass it.
    runtime = _runtime_of(_installed_in(tmp_path))
    world = _child_world(tmp_path, {5555: [runtime]}, (30.5, 32.0, f" 5555    99 R    {_SHORT}"))
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [
        "the control: it starts the host on the app's profile, and the check sees it"
    ], checker.failures
    assert "the control's gate held" not in said, said
    line = next(line for line in said.splitlines() if "the control's watch" in line)
    assert re.search(r"ps, by executable \(its arguments unreadable\) at 30\.\d\d s", line), line


def test_the_controls_record_keeps_what_lsof_said_once_it_said_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # lsof gives no file of the child at first, and from 0.6 s says that it maps the copy's
    # runtime: the gate holds at the reading that asked it then, and the record keeps what says
    # the most, the first time it was said, not lsof's first answer.
    runtime = _runtime_of(_installed_in(tmp_path))

    def lsof(at: float | None, pids: list[int]) -> tuple[int, str, str]:
        if at is None or at < 0.6:
            return 1, "", ""
        return 0, "".join(f"p{pid}\nn{runtime}\n" for pid in pids), ""

    world = _ListerWorld(
        pgrep=_none,
        ps=_ps_with((0.4, 2.0, f" 5555    99 R    {_SHORT}")),
        lsof=lsof,
        marker_at=1.5,
    )
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [], checker.failures
    assert re.search(
        r"\(by lsof, asked at 0\.[5-7]\d s\), and ps showed it as 'R \(lucille-host-run\)' at "
        r"0\.4\d s",
        said,
    ), said
    assert re.search(
        rf"its arguments unreadable, by lsof, asked at 0\.[5-7]\d s, it maps {re.escape(runtime)}",
        said,
    ), said
    assert "its executable unknown" not in said, said


def test_the_controls_record_shows_the_first_five_children_of_the_app() -> None:
    # Recorded, not a gate: the app's children as ps first showed them, five at most, with what
    # lsof said of each whose arguments ps could not read, and how many more there were.
    unread = check_app._Unread(106, 99, "R", _SHORT, True, (), "lsof listed no text file for it")
    reading = check_app._Reading(
        "ps",
        0.0,
        0.1,
        frozenset(),
        tuple((pid, f"S /bin/sleep {pid}") for pid in range(100, 106)) + ((106, f"R {_SHORT}"),),
        "",
        (unread,),
    )
    watch = _watched(reading)
    assert watch.children_said() == (
        "the app's children, as ps first showed them: "
        + "; ".join(f"{pid} 'S /bin/sleep {pid}'" for pid in range(100, 105))
        + "; and 2 more"
    )
    first = _watched(
        check_app._Reading("ps", 0.0, 0.1, frozenset(), ((106, f"R {_SHORT}"),), "", (unread,))
    )
    assert first.children_said() == (
        f"the app's children, as ps first showed them: 106 'R {_SHORT}', its arguments "
        "unreadable, its executable unknown: lsof listed no text file for it"
    )
    assert _watched().children_said() == "ps saw no child of the app"


def test_a_path_names_the_runtime_however_it_is_spelled(tmp_path: Path) -> None:
    # The same string, or the same file by another path (here a link to its folder), names it;
    # another file, a path that is not there, or none, does not.
    folder = tmp_path / "Contents"
    folder.mkdir()
    runtime = folder / "lucille-host-runtime"
    runtime.write_text("", encoding="utf-8")
    other = folder / "other"
    other.write_text("", encoding="utf-8")
    (tmp_path / "link").symlink_to(folder)
    assert check_app._same_file(str(runtime), runtime)
    assert check_app._same_file(str(tmp_path / "link" / "lucille-host-runtime"), runtime)
    assert not check_app._same_file(str(other), runtime)
    assert not check_app._same_file(str(tmp_path / "gone"), runtime)
    assert not check_app._same_file(None, runtime)


def test_the_controls_gate_takes_only_what_came_inside_it() -> None:
    # The gate, asked of a watch over made-up readings: inside its 30 s, a host a lister matched by
    # its arguments holds it by its arguments, even where a child told by its executable came first;
    # a child that runs the copy's runtime holds it by its executable; either one first seen past
    # the 30 s does not.
    runtime = _runtime_of(Path("/Applications") / f"{check_app.APP_NAME}.app")
    child = check_app._Unread(5555, 99, "R", _SHORT, True, (runtime,))

    def ps(at: float, hosts: frozenset[int] = frozenset(), unread: tuple[Any, ...] = ()) -> Any:
        return check_app._Reading("ps", at, at + 0.01, hosts, ((5555, f"R {_SHORT}"),), "", unread)

    assert _watched(ps(0.4, unread=(child,)), ended=40.0).passed_by(runtime)[0] == "executable"
    assert _watched(ps(30.5, unread=(child,)), ended=40.0).passed_by(runtime) == ("", "")
    both = _watched(ps(0.3, unread=(child,)), ps(0.5, frozenset({5555})), ended=40.0)
    assert both.passed_by(runtime)[0] == "arguments"
    assert _watched(ps(30.5, frozenset({5555})), ended=40.0).passed_by(runtime) == ("", "")
    assert _watched(ps(0.4, unread=(child,)), ended=40.0).passed_by("/x/other") == ("", "")


def test_a_control_that_sees_both_says_it_held_by_the_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # ps shows a child only by its name at 0.3 s (why is not known), and lsof says it maps the
    # copy's runtime; from 0.5 s ps reads its arguments and both listers match it. Inside the gate
    # a lister matched it by its command line, so the control held by its arguments, the proof it
    # had before, and the line says that and no more.
    runtime = _runtime_of(_installed_in(tmp_path))
    ps = _ps_with((0.3, 0.5, f" 4242    99 R    {_SHORT}"), (0.5, 1e9, _PS_HOST_ROW))
    world = _ListerWorld(
        pgrep=_host_from(0.5), ps=ps, lsof=_lsof_giving({4242: [runtime]}), marker_at=1.5
    )
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [], checker.failures
    gate = [line for line in said.splitlines() if line.startswith("the control's gate held")]
    assert len(gate) == 1 and re.fullmatch(
        r"the control's gate held by its arguments: (pgrep|ps) matched a host process by its "
        r"command line at 0\.5\d s",
        gate[0],
    ), gate
    assert "does not prove" not in said, said


def test_a_passing_control_by_the_arguments_still_prints_its_lines_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Where ps reads the arguments, nothing is asked of lsof by pid, and the control's lines are
    # the ones it printed before, with the gate's own line, the record of the app's children and
    # what the reader by name saw added. That reader saw the same child, which maps the copy's
    # runtime, but its arguments were readable: the record does not call them unreadable, and the
    # gate held by them.
    installed = _installed_in(tmp_path)
    runtime = _runtime_of(installed)
    world = _ListerWorld(
        pgrep=_host_from(0.4),
        ps=_ps_host_from(0.4),
        named=_named_giving(_app_listed(installed), (0.4, 1e9, 4242, 99, _RUNTIME_NAME, [runtime])),
        marker_at=1.5,
    )
    asked: list[list[int]] = []

    def lsof(at: float | None, pids: list[int]) -> tuple[int, str, str]:
        asked.append(pids)
        return 1, "", ""

    world.lsof = lsof
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [] and asked == [], (checker.failures, asked)
    assert re.search(
        r"^the control's gate held by its arguments: (pgrep|ps) matched a host process by its "
        r"command line at 0\.4\d s$",
        said,
        re.MULTILINE,
    ), said
    assert (
        f"the control: the app's children, as ps first showed them: 4242 'S {_HOST_COMMAND}'\n"
        in said
    ), said
    line = next(line for line in said.splitlines() if "the control's watch" in line)
    assert "by executable" not in line and "possibly a host" not in line, line
    assert re.search(
        r"the processes of the runtime's name it saw: 4242 \(parent 99, a running copy of the "
        rf"app\) from 0\.4\d s, by lsof, by name, asked at 0\.4\d s, it maps {re.escape(runtime)}$",
        said,
        re.MULTILINE,
    ), said


@pytest.mark.parametrize(
    ("texts", "starts"),
    [
        ([f"/x/{check_app.APP_NAME}.app/{build_app.RUNTIME_IN_APP}"], True),
        ([], True),
        ([_REGION_ERROR], True),
        (["/opt/other/bin/lucille-host-runtime"], False),
    ],
    ids=["a copy's runtime", "lsof says nothing", "lsof says only an error", "another program"],
)
def test_a_refusing_copy_fails_on_a_runtime_process_it_left_behind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    texts: list[str],
    starts: bool,
) -> None:
    # A process whose arguments ps cannot read, and whose parent is not the copy (its parent went,
    # and launchd took it): where lsof says it maps a copy's bundled runtime, the copy started it;
    # where lsof lists nothing, or only its own error where a file's name goes, it may have, and
    # the check fails safe. One that maps another program of that name is not counted.
    world = _ListerWorld(
        pgrep=_none,
        ps=_ps_with((2.0, 2.5, f" 7777     1 S    {_SHORT}")),
        lsof=_lsof_giving({7777: texts}),
    )
    checker = _refusing_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    started = (
        f"for {check_app.REFUSAL_WATCH} s it runs, and starts no host process of the app's profile"
    )
    assert checker.failures == ([started] if starts else []), checker.failures
    read = bool(texts) and texts[0].startswith("/")
    if read:
        how = (
            f"by its executable, its arguments unreadable: 7777 maps {texts[0]} (by lsof, asked at "
        )
    elif texts:
        how = (
            "possibly a host, its arguments and executable unreadable: 7777 "
            f"(lsof said {texts[0]!r}"
        )
    else:
        how = "possibly a host, its arguments and executable unreadable: 7777 (lsof listed no text"
    assert (how in said) is starts, said
    if read and starts:
        assert re.search(re.escape(how) + r"\d\.\d\d s; parent 1\)", said), said
    if starts:
        assert "seen: [7777]" in said, said
        line = next(line for line in said.splitlines() if "the watch of the app's profile" in line)
        name = check_app.BY_EXECUTABLE if read else check_app.MAYBE_HOST
        assert re.search(re.escape(name) + r" at \d\.\d\d s", line), line


def _ps_rows_then(*tables: list[str]) -> Callable[..., subprocess.CompletedProcess[str]]:
    """subprocess.run, made up: pgrep finds nothing; ps gives each of ``tables`` in turn, the last
    one from then on; lsof lists nothing (no text file for any pid)."""
    left = list(tables)

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == "pgrep":
            return _done(argv, 1)
        if argv[0] == "lsof":
            return _done(argv, 1)
        rows = left.pop(0) if len(left) > 1 else left[0]
        return _done(argv, 0, "".join(row + "\n" for row in rows))

    return run


@pytest.mark.parametrize(
    ("row", "texts", "none"),
    [
        # Possibly a host: its executable could not be read. Fail safe.
        (f" 7777     1 S    {_SHORT}", None, False),
        # A copy's runtime, by its executable.
        (
            f" 7777     1 S    {_SHORT}",
            [f"/x/{check_app.APP_NAME}.app/{build_app.RUNTIME_IN_APP}"],
            False,
        ),
        # Another program of that name.
        (f" 7777     1 S    {_SHORT}", ["/opt/other/bin/lucille-host-runtime"], True),
        # The check's own child, and the check itself, whatever they run: never a host here.
        ("OWN-CHILD", None, True),
        ("SELF", None, True),
        # A host process of the app's profile that ps can read, the check's own child or not, is
        # one, as before, by its arguments.
        (f" 7777     1 S    {_HOST_COMMAND}", None, False),
        ("OWN-HOST", None, False),
    ],
    ids=[
        "lsof says nothing",
        "a copy's runtime",
        "another program",
        "the check's child",
        "the check",
        "a host by its arguments",
        "the check's host by its arguments",
    ],
)
def test_the_waits_for_no_host_count_what_runs_where_ps_cannot_read_the_arguments(
    monkeypatch: pytest.MonkeyPatch, row: str, texts: list[str] | None, none: bool
) -> None:
    own = os.getpid()
    row = {
        "OWN-CHILD": f" 7777 {own:>5} S    {_SHORT}",
        "SELF": f"{own:>5}     1 S    {_SHORT}",
        "OWN-HOST": f" 7777 {own:>5} S    {_HOST_COMMAND}",
    }.get(row, row)
    base = _ps_rows_then(["    1     0 Ss   /sbin/launchd", row])
    lsof = None if texts is None else _lsof_giving({7777: texts})

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == "lsof" and lsof is not None:
            return _done(argv, *lsof(None, [7777]))
        return base(argv, **kwargs)

    monkeypatch.setattr("subprocess.run", run)
    assert check_app._no_profile_process() is none
    said = check_app._profile_processes_said()
    if not none and row.endswith(_SHORT):
        assert "7777" in said and ("possibly" in said) is (texts is None), said


def test_a_wait_for_no_host_holds_once_a_process_ps_cannot_read_has_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The wait does not pass while a '(lucille-host-run)' runs whose executable lsof cannot give,
    # and passes once it has gone; while it never goes, the wait fails.
    gone = ["    1     0 Ss   /sbin/launchd"]
    there = [*gone, f" 7777     1 S    {_SHORT}"]
    clock = _Clock()
    monkeypatch.setattr(check_app.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(check_app.time, "sleep", clock.sleep)
    monkeypatch.setattr("subprocess.run", _ps_rows_then(there, there, there, gone))
    assert check_app._wait_until(check_app._no_profile_process, 30) is True
    assert clock.now == pytest.approx(1001.5)
    monkeypatch.setattr("subprocess.run", _ps_rows_then(there))
    assert check_app._wait_until(check_app._no_profile_process, 30) is False


def _named_giving(
    *processes: tuple[float, float, int, int, str, Sequence[str]],
) -> Callable[[float | None], tuple[int, str, str]]:
    """The control's lsof by name, made up: each of ``processes`` (from, until, pid, parent, name,
    files) is listed from ``from`` to ``until`` seconds after the open, as lsof -F pcRn lists it,
    and lsof exits 1 where it lists none."""

    def named(at: float | None) -> tuple[int, str, str]:
        out = "".join(
            f"p{pid}\nc{name}\nR{parent}\n" + "".join(f"n{path}\n" for path in files)
            for when, until, pid, parent, name, files in processes
            if at is not None and when <= at < until
        )
        return (0 if out else 1), out, ""

    return named


def _app_listed(installed: Path) -> tuple[float, float, int, int, str, list[str]]:
    """The app itself, pid 99, as the control's lsof by name lists it."""
    app = f"{installed}/Contents/MacOS/{check_app.APP_NAME}"
    return (0.0, 1e9, 99, 1, check_app.APP_NAME, ["/usr/lib/dyld", app])


def test_lsof_s_own_error_where_a_files_name_goes_is_no_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Apple's lsof writes "region info error: ..." where a file's name goes when it cannot read a
    # region of the process, for an errno other than ESRCH or EINVAL. That is no file: the process
    # gets none, and why quotes what lsof said. Only an absolute path is a file.
    monkeypatch.setattr(check_app, "_runs_now", lambda pid: True)
    answer = f"p7777\nf txt\nn{_REGION_ERROR}\np7778\nf txt\nn/bin/sleep\nnnot a path\n"
    monkeypatch.setattr("subprocess.run", lambda argv, **kwargs: _done(argv, 0, answer))
    texts = check_app._texts_of([7777, 7778])
    assert texts[7777] == (
        (),
        f"lsof said {_REGION_ERROR!r}; a process of that pid still ran once lsof had answered",
    )
    assert texts[7778] == (("/bin/sleep",), "")
    listed = check_app._lsof_records(answer)
    assert listed[7777].files == [] and listed[7777].said == [_REGION_ERROR]
    assert listed[7778].files == ["/bin/sleep"] and listed[7778].said == ["not a path"]


def test_a_wait_for_no_host_does_not_pass_on_a_process_lsof_says_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A '(lucille-host-run)' whose parent went, of which lsof gives only its own error: possibly a
    # host, so the wait does not pass, and what the check says names lsof's words, not a file.
    base = _ps_rows_then(["    1     0 Ss   /sbin/launchd", f" 7777     1 S    {_SHORT}"])

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == "lsof":
            return _done(argv, 0, f"p7777\nf txt\nn{_REGION_ERROR}\n")
        return base(argv, **kwargs)

    monkeypatch.setattr("subprocess.run", run)
    # raising=False: so that this test also runs against a check that never asked.
    monkeypatch.setattr(check_app, "_runs_now", lambda pid: True, raising=False)
    assert check_app._no_profile_process() is False
    said = check_app._profile_processes_said()
    assert (
        "possibly a host, its arguments and executable unreadable: 7777 (lsof said "
        f"{_REGION_ERROR!r}; a process of that pid still ran once lsof had answered)"
    ) in said, said


@pytest.mark.parametrize(
    ("runs", "said"),
    [
        (False, "it had ended by the time lsof answered"),
        (True, "a process of that pid still ran once lsof had answered"),
        (None, "whether it still ran once lsof had answered is not known"),
    ],
)
def test_where_lsof_gives_no_file_the_check_says_whether_the_process_still_ran(
    monkeypatch: pytest.MonkeyPatch, runs: bool | None, said: str
) -> None:
    # A lookup that came after the process had ended finds nothing, which says nothing of what it
    # mapped: why says which it was, after lsof listed none, did not answer, or had no time left.
    monkeypatch.setattr(check_app, "_runs_now", lambda pid: runs)
    monkeypatch.setattr("subprocess.run", lambda argv, **kwargs: _done(argv, 1))
    assert check_app._texts_of([42]) == {
        42: ((), f"lsof listed no text file for it (exit 1); {said}")
    }

    def held(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == 2.5, kwargs
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr("subprocess.run", held)
    assert check_app._texts_of([42], 2.5) == {42: ((), f"lsof did not answer in 2.5 s; {said}")}
    monkeypatch.setattr("subprocess.run", lambda argv, **kwargs: pytest.fail(f"ran {argv}"))
    assert check_app._texts_of([42], 0.0) == {
        42: ((), f"no time was left for lsof within the reading's 5 s; {said}")
    }


def test_whether_a_process_runs_now_is_asked_of_the_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This process runs; a child that has been waited for does not. One the kernel will not let
    # this process signal (another user's) runs; any other error cannot tell.
    assert check_app._runs_now(os.getpid()) is True
    child = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
    child.wait()
    assert check_app._runs_now(child.pid) is False
    for error, runs in ((PermissionError, True), (OSError, None), (ProcessLookupError, False)):

        def kill(pid: int, sent: int, error: type[OSError] = error) -> None:
            raise error(pid)

        monkeypatch.setattr("os.kill", kill)
        assert check_app._runs_now(4242) is runs, error


def test_the_ps_lister_gives_lsof_what_is_left_of_its_five_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A ps call that took 4 s leaves lsof 1 s, so that the reading still ends within about 5 s.
    clock = _Clock()
    monkeypatch.setattr(check_app.time, "monotonic", clock.monotonic)
    asked: list[float] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == "lsof":
            asked.append(kwargs["timeout"])
            return _done(argv, 1)
        clock.now += 4.0
        return _done(argv, 0, f"    1     0 Ss   /sbin/launchd\n 7777     1 S    {_SHORT}\n")

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(check_app, "_runs_now", lambda pid: True)
    _, _, [unread] = check_app._ps_processes()
    assert asked == [pytest.approx(1.0)] and unread.asked == clock.now


def test_the_control_passes_where_its_reader_by_name_sees_a_child_ps_was_too_slow_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # As run 42700559897's record suggests (inferred, not seen): while the app launches a ps call
    # takes over a second, and the child, shown by ps only as '(lucille-host-run)', has gone by
    # the time the ps lister's own lookup asks lsof. The control's reader by name, on a thread of
    # its own, saw it while it ran, as a child of the app that maps the copy's runtime: the gate
    # holds by its executable. The ps lister's late lookup says the child had ended.
    installed = _installed_in(tmp_path)
    runtime = _runtime_of(installed)
    world = _ListerWorld(
        pgrep=_none,
        ps=_ps_with((1.0, 2.2, f" 5555    99 R    {_SHORT}")),
        ps_took=1.2,
        lsof=_lsof_giving({}),
        named=_named_giving(
            _app_listed(installed),
            (1.0, 2.2, 5555, 99, _RUNTIME_NAME, ["/usr/lib/dyld", runtime]),
        ),
        marker_at=2.0,
    )
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [], checker.failures
    gate = next(line for line in said.splitlines() if line.startswith("the control's gate held"))
    assert re.fullmatch(
        r"the control's gate held by its executable, its arguments unreadable: pid 5555, a child "
        rf"of the app, has the runtime's name and maps {re.escape(runtime)} \(by lsof, by name, "
        r"asked at 1\.\d\d s\), and ps showed it as 'R \(lucille-host-run\)' at 1\.\d\d s; ps read "
        r"the arguments of none of the app's children, so this run does not prove that the "
        r"listers' pattern matches the app's command line",
        gate,
    ), gate
    named = next(line for line in said.splitlines() if line.startswith("the control: lsof, by"))
    assert re.search(
        r"the processes of the runtime's name it saw: 5555 \(parent 99, a running copy of the "
        rf"app\) from 1\.\d\d s, by lsof, by name, asked at 1\.\d\d s, it maps "
        rf"{re.escape(runtime)}$",
        named,
    ), named
    children = next(line for line in said.splitlines() if "the app's children, as ps" in line)
    assert re.search(
        rf"5555 'R {re.escape(_SHORT)}', its arguments unreadable, by lsof, by name, asked at "
        rf"1\.\d\d s, it maps {re.escape(runtime)}",
        children,
    ), children
    assert world.named_calls > 0


def test_the_control_passes_on_a_child_its_reader_by_name_alone_saw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # ps never lists the child at all; the reader by name sees it, a child of the app that maps
    # the copy's runtime, from 0.4 s. The gate holds by its executable, and the line says ps never
    # read its arguments and did not list it, not that they were unreadable.
    installed = _installed_in(tmp_path)
    runtime = _runtime_of(installed)
    world = _ListerWorld(
        pgrep=_none,
        ps=_ps_none,
        named=_named_giving(_app_listed(installed), (0.4, 2.0, 5555, 99, _RUNTIME_NAME, [runtime])),
        marker_at=1.5,
    )
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [], checker.failures
    gate = next(line for line in said.splitlines() if line.startswith("the control's gate held"))
    assert gate.startswith(
        "the control's gate held by its executable, ps never read its arguments: pid 5555, a child "
        f"of the app, has the runtime's name and maps {runtime} (by lsof, by name, asked at 0.4"
    ), gate
    assert "and ps did not list it; ps read the arguments of none" in gate, gate
    assert "the control: ps saw no child of the app" in said, said
    assert re.search(
        r"^the control: lsof, by name, recorded, not a gate: \d+ calls, longest 0\.00 s, lsof "
        r"errors 0; the processes of the runtime's name it saw: 5555 \(parent 99, a running copy "
        r"of the "
        rf"app\) from 0\.4\d s, by lsof, by name, asked at 0\.4\d s, it maps {re.escape(runtime)}$",
        said,
        re.MULTILINE,
    ), said


@pytest.mark.parametrize("seen", ["not a child", "another copy", "no file"])
def test_the_control_still_fails_on_what_its_reader_by_name_alone_saw_otherwise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    seen: str,
) -> None:
    # The reader by name holds the gate only for a child of the app that maps that copy's
    # runtime: not one whose parent is launchd, not one that maps another copy's runtime, and not
    # one of which lsof gave only its error. The case says which, for a child of the app.
    installed = _installed_in(tmp_path)
    other = _runtime_of(tmp_path / f"{check_app.RENAMED_COPY}.app")
    parent, files = {
        "not a child": (1, [_runtime_of(installed)]),
        "another copy": (99, [other]),
        "no file": (99, [_REGION_ERROR]),
    }[seen]
    world = _ListerWorld(
        pgrep=_none,
        ps=_ps_none,
        named=_named_giving(_app_listed(installed), (0.4, 2.0, 5555, parent, _RUNTIME_NAME, files)),
        marker_at=1.5,
    )
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [
        "the control: it starts the host on the app's profile, and the check sees it"
    ], checker.failures
    case = _case(said)
    if seen == "not a child":
        assert "but no reading saw a host process by 60." in case, case
        assert "5555 (parent 1) from 0.4" in said, said
        return
    assert case.startswith(
        "the case: a child of the app from 0.4 s (5555, seen by lsof, by name alone), but no host "
        "process the gate matches by 60."
    ), case
    if seen == "another copy":
        assert f"5555 maps {other} (by lsof), not that copy's runtime" in case, case
    else:
        assert f"5555: lsof gave no executable (lsof said {_REGION_ERROR!r}; " in case, case
        assert re.search(r"; it had ended by the time lsof answered; asked at 0\.4\d s\)", case), (
            case
        )


def test_the_reader_by_name_takes_a_child_that_ps_saw_where_lsof_did_not_list_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # lsof by name lists the child, which maps the copy's runtime, but not the app itself, so its
    # own answer cannot say whose child it is; ps, slow, shows it as a child of the app only by its
    # name, later. A child of the app as ps saw it is one too, and the gate holds by its
    # executable, at the reader's sighting.
    runtime = _runtime_of(_installed_in(tmp_path))
    world = _ListerWorld(
        pgrep=_none,
        ps=_ps_with((1.0, 2.2, f" 5555    99 R    {_SHORT}")),
        ps_took=1.2,
        named=_named_giving((1.0, 2.2, 5555, 99, _RUNTIME_NAME, [runtime])),
        marker_at=2.0,
    )
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [], checker.failures
    gate = next(line for line in said.splitlines() if line.startswith("the control's gate held"))
    assert re.search(
        r"pid 5555, a child of the app, .*\(by lsof, by name, asked at 1\.4\d s\)", gate
    )
    assert "5555 (parent 99, a running copy of the app) from 1.4" in said, said


def test_the_controls_record_of_its_reader_by_name_says_what_went_wrong_and_how_many_it_saw() -> (
    None
):
    # Recorded, not a gate: how often the reader asked and what went wrong in it, and the first
    # five processes of the runtime's name it saw, with how many more; nothing where it never ran.
    runtime = _runtime_of(Path("/Applications") / f"{check_app.APP_NAME}.app")
    seen = tuple(
        check_app._Unread(pid, 1, "", "", False, (runtime,), "", None, check_app.NAMED)
        for pid in range(100, 107)
    )
    failed = check_app._Reading(check_app.NAMED, 0.0, 0.25, frozenset(), (), "lsof exited 2, x")
    found = check_app._Reading(check_app.NAMED, 0.5, 0.6, frozenset(), (), "", seen)
    said = _watched(failed, found).named_said()
    assert said.startswith(
        "lsof, by name, recorded, not a gate: 2 calls, longest 0.25 s, lsof errors 1 (lsof exited "
        "2, x); the processes of the runtime's name it saw: 100 (parent 1) from 0.50 s, by lsof, "
        f"by name, it maps {runtime}; 101 (parent 1)"
    ), said
    assert said.endswith("; and 2 more") and "105 (parent 1)" not in said, said
    assert _watched(failed).named_said().endswith("; it saw no process of the runtime's name")
    assert _watched().named_said() == ""


def test_the_controls_gate_needs_the_runtimes_name_as_well_as_what_a_child_maps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # lsof lists every file a process maps, not its program alone: a child of the app that ps
    # shows as another program's name, and that maps the copy's runtime among its files, does
    # not pass the gate. The case says it maps the runtime, but its name is not the runtime's.
    runtime = _runtime_of(_installed_in(tmp_path))
    world = _child_world(
        tmp_path,
        {5555: ["/usr/bin/codesign", runtime]},
        (0.4, 2.0, " 5555    99 R    (codesign)"),
    )
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert any("the control: it starts the host" in what for what in checker.failures)
    assert (
        "5555 maps that copy's runtime (by lsof), from 0.4 s, but its name is not the runtime's "
        "('R (codesign)')"
    ) in _case(said), said


def test_a_child_whose_arguments_ps_reads_later_takes_the_executables_pass_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # ps shows a child only by its name at 0.3 s, and lsof says it maps the copy's runtime, so the
    # gate holds at first; from 0.5 s ps reads its arguments, on another profile, which the
    # pattern does not match. Asked again over the whole watch, the gate does not hold: the
    # control fails, and the case names the child ps read.
    runtime = _runtime_of(_installed_in(tmp_path))
    wrong = _HOST_COMMAND.replace(f"--profile {check_app.APP_PROFILE} ", "--profile default ")
    world = _child_world(
        tmp_path,
        {5555: [runtime]},
        (0.3, 0.5, f" 5555    99 R    {_SHORT}"),
        (0.5, 2.0, f" 5555    99 S    {wrong}"),
    )
    checker, _ = _control_in(world, tmp_path, monkeypatch)
    said = capsys.readouterr().out
    assert checker.failures == [
        "the control: it starts the host on the app's profile, and the check sees it"
    ], checker.failures
    assert "the control's gate held" not in said, said
    assert re.search(
        r"ps read the arguments of a child of the app that the listers' pattern does not match "
        r"\(5555 'S .*--profile default .*'\), so what a child maps does not pass the gate",
        _case(said),
    ), said


def test_the_controls_gate_never_takes_what_a_child_maps_once_ps_has_read_its_arguments() -> None:
    # The reader by name sees a child that maps the copy's runtime at 0.4 s; ps reads its
    # arguments, which the pattern matches, only at 30.5 s, past the gate. Its arguments were
    # readable, so what it maps does not stand in for them, and the gate does not hold.
    runtime = _runtime_of(Path("/Applications") / f"{check_app.APP_NAME}.app")
    seen = check_app._Unread(5555, 99, "", "", True, (runtime,), "", 0.4, check_app.NAMED)
    named = check_app._Reading(check_app.NAMED, 0.4, 0.41, frozenset(), (), "", (seen,))
    ps = check_app._Reading("ps", 30.5, 30.51, frozenset({5555}), ((5555, f"S {_HOST_COMMAND}"),))
    assert _watched(named, ended=40.0).passed_by(runtime)[0] == "executable"
    assert _watched(named, ps, ended=40.0).passed_by(runtime) == ("", "")


def test_the_controls_record_keeps_the_first_of_two_answers_that_say_as_much() -> None:
    # Two answers of lsof that say as much, a file each and neither a copy's runtime: the record
    # keeps the first.
    def ps(at: float, path: str) -> Any:
        unread = check_app._Unread(5555, 99, "R", "(other)", True, (path,))
        return check_app._Reading(
            "ps", at, at + 0.01, frozenset(), ((5555, "R (other)"),), "", (unread,)
        )

    said = _watched(ps(0.1, "/a/first"), ps(0.2, "/b/second")).children_said()
    assert "it maps /a/first and no copy's runtime" in said and "/b/second" not in said, said


def test_the_reader_by_name_runs_in_the_controls_watch_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The control's watch asks lsof by name; a refusing copy's does not, so its stretches stay as
    # they were measured. The pool has a thread for it.
    pool = check_app._lister_pool()
    try:
        assert pool._max_workers == len(check_app.LISTERS) + 1
    finally:
        pool.shutdown()
    world = _ListerWorld(pgrep=_host_from(0.4), ps=_ps_host_from(0.4), marker_at=1.5)
    _control_in(world, tmp_path / "control", monkeypatch)
    assert world.named_calls > 0
    world = _ListerWorld(pgrep=_none, ps=_ps_none)
    _refusing_in(world, tmp_path / "refusing", monkeypatch)
    assert world.named_calls == 0
    source = _CHECK_APP.read_text(encoding="utf-8")
    assert _python_function(source, "_watch_control").count("named=True") == 3
    assert "named=True" not in _python_function(source, "_refusing_copy")


def test_the_reader_by_name_lists_the_runtimes_processes_with_their_parents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One call: lsof -a -c RUNTIME -c APP -d txt -R -F pcRn, each name cut to 15 characters. A
    # process of the runtime's name is a child of the app where its parent, in the same answer,
    # maps the app's executable; the check's own, and its children, are left out; a process of
    # another name is not one; lsof's own error is no file.
    own = os.getpid()
    app = f"/Applications/{check_app.APP_NAME}.app/Contents/MacOS/{check_app.APP_NAME}"
    runtime = _runtime_of(Path("/Applications") / f"{check_app.APP_NAME}.app")
    name = _RUNTIME_NAME
    answer = "".join(
        [
            f"p99\nc{check_app.APP_NAME}\nR1\nn/usr/lib/dyld\nn{app}\n",
            f"p100\nc{name}\nR99\nn{runtime}\n",
            f"p101\nc{name}\nR1\nn{_REGION_ERROR}\n",
            f"p{own}\nc{name}\nR1\nn{runtime}\n",
            f"p102\nc{name}\nR{own}\nn{runtime}\n",
            f"p103\nc{check_app.APP_NAME} Helper\nR99\nn/x/Helper\n",
            f"p104\nc{name[:15]}\nR98\nn{runtime}\n",
        ]
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return _done(argv, 0, answer)

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(check_app, "_runs_now", lambda pid: False)
    found, error = check_app._named_processes()
    [(argv, kwargs)] = calls
    assert argv == [
        "lsof",
        "-w",
        "-a",
        "-c",
        name[:15],
        "-c",
        check_app.APP_NAME[:15],
        "-d",
        "txt",
        "-R",
        "-F",
        "pcRn",
    ]
    assert kwargs.get("timeout") == 5 and kwargs.get("errors") == "replace" and error == ""
    by = {each.pid: each for each in found}
    assert sorted(by) == [100, 101, 104]
    assert by[100].child and by[100].host and by[100].named and by[100].seen_by == check_app.NAMED
    assert by[100].parent == 99 and by[100].command == "" and not by[100].maybe
    assert not by[101].child and by[101].texts == () and not by[101].host
    assert by[101].why == f"lsof said {_REGION_ERROR!r}; it had ended by the time lsof answered"
    assert not by[104].child and by[104].host
    assert all(each.asked is not None for each in found)


@pytest.mark.parametrize(
    ("failure", "said"),
    [
        ("missing", "lsof could not start"),
        ("timeout", "lsof did not answer in 5 s"),
        ("exit", "lsof exited 2, with 'no such option'"),
        ("stderr", "lsof exited 0, with 'a warning'"),
    ],
)
def test_the_reader_by_name_never_raises_and_says_what_went_wrong(
    monkeypatch: pytest.MonkeyPatch, failure: str, said: str
) -> None:
    # lsof missing, held, failing or writing on standard error: the reader says so and never
    # raises; what it could read it still gives. Nothing found and exit 1 is no error.
    runtime = _runtime_of(Path("/Applications") / f"{check_app.APP_NAME}.app")
    answer = f"p100\nc{_RUNTIME_NAME}\nR1\nn{runtime}\n"

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if failure == "missing":
            raise FileNotFoundError("lsof")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if failure == "exit":
            return _done(argv, 2, "", "no such option\n")
        return _done(argv, 0, answer, "a warning\n")

    monkeypatch.setattr("subprocess.run", run)
    found, error = check_app._named_processes()
    assert said in error, error
    assert [each.pid for each in found] == ([100] if failure == "stderr" else [])
    monkeypatch.setattr("subprocess.run", lambda argv, **kwargs: _done(argv, 1))
    assert check_app._named_processes() == ([], "")
    reading = check_app._read(check_app.NAMED)
    assert reading.hosts == frozenset() and reading.error == "" and reading.unread == ()


@pytest.mark.parametrize("failure", ["timeout", "missing", "exit"])
def test_the_controls_diagnostics_have_time_limits_and_never_raise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    # The screenshot, the app's processes, the sample, the unified log and the listers' libraries
    # each run with a time limit; whichever fails, the check goes on and says what stopped it.
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        if argv[0] == "ps":
            return _done(
                argv,
                0,
                f"  PID  PPID STAT WCHAN ELAPSED COMMAND\n   99     1 S - 00:05 {_APP_COMMAND}\n",
            )
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 0)
        if failure == "missing":
            raise FileNotFoundError(argv[0])
        return _done(argv, 1, "", "no")

    monkeypatch.setattr("subprocess.run", run)
    screens = tmp_path / "screens"
    screens.mkdir()
    check_app._control_snapshot(tmp_path, screens)
    opened = time.time()
    said = check_app._save_control_log(tmp_path / "control-log.txt", opened)
    libraries = check_app._lister_libraries()
    assert isinstance(said, str) and isinstance(libraries, str)
    ran = [argv[0] for argv, _ in calls]
    for program in ("screencapture", "ps", "sample", "log", "otool"):
        assert program in ran, ran
    for argv, kwargs in calls:
        assert kwargs.get("timeout"), argv
    assert ["sample", "99", "1", "-file", str(tmp_path / "control-sample.txt")] in [
        argv for argv, _ in calls
    ]
    assert (tmp_path / "control-ps.txt").read_text(encoding="utf-8").count(_APP_COMMAND) == 1
    # The unified log, from five seconds before the open, of the app, its interpreter and the
    # parts of macOS named.
    [log] = [argv for argv, _ in calls if argv[0] == "log"]
    start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(opened - 5))
    assert log[:-1] == [
        "log",
        "show",
        "--start",
        start,
        "--style",
        "compact",
        "--info",
        "--predicate",
    ]
    assert log[-1] == " OR ".join(
        f'process == "{name}"' for name in check_app.CONTROL_LOG_PROCESSES
    )
    capsys.readouterr()


def test_the_pairing_checks_tidy_kills_the_host_processes_still_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Host processes of the app's profile that outlive the wait are killed, whichever lister
    # saw them, so that none holds the profile's lock for the checks after it.
    pairing = object.__new__(check_app._PairingCheck)
    pairing.checker = check_app.Checker()
    pairing.owns_profile = True
    pairing.url = ""
    pairing.profile = tmp_path / "profile"
    pairing.children = []
    pairing.set_aside = None
    pairing.folder = tmp_path / "folder"
    pairing.app = tmp_path / f"{check_app.APP_NAME}.app"
    pairing.logs = []
    seen = (
        check_app._Reading("pgrep", 0.0, 0.0, frozenset({4242})),
        check_app._Reading("ps", 0.0, 0.0, frozenset({4242, 4343})),
    )
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(check_app, "_may_run", lambda name: False)
    monkeypatch.setattr(check_app, "_wait_until", lambda test, seconds: False)
    monkeypatch.setattr(check_app, "_look", lambda: seen)
    monkeypatch.setattr("os.kill", lambda pid, sent: killed.append((pid, sent)))
    monkeypatch.setattr(check_app, "_set_pasteboard", lambda text: None)
    monkeypatch.setattr("subprocess.run", lambda argv, **kwargs: _done(argv, 0))
    pairing.tidy()
    assert killed == [(4242, signal.SIGKILL), (4343, signal.SIGKILL)]
    assert "still ran, and were killed: [4242, 4343]" in capsys.readouterr().out


def test_the_pairing_checks_tidy_kills_a_runtime_process_and_names_one_it_cannot_tell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Where ps could not read a process's arguments: one that lsof says maps the bundled runtime
    # of the copy this check opened is killed with the host processes the listers matched; one
    # that maps another copy's runtime may be on another profile, and one of which lsof gave no
    # file the check cannot tell: each is named, and left.
    pairing = object.__new__(check_app._PairingCheck)
    pairing.checker = check_app.Checker()
    pairing.owns_profile = True
    pairing.url = ""
    pairing.profile = tmp_path / "profile"
    pairing.children = []
    pairing.set_aside = None
    pairing.folder = tmp_path / "folder"
    pairing.app = tmp_path / f"{check_app.APP_NAME}.app"
    pairing.logs = []
    runtime = _runtime_of(pairing.app)
    other = f"/x/{check_app.APP_NAME}.app/{build_app.RUNTIME_IN_APP}"
    unread = (
        check_app._Unread(7777, 1, "S", _SHORT, False, (runtime,)),
        check_app._Unread(7778, 1, "S", _SHORT, False, (), "lsof listed no text file for it"),
        check_app._Unread(7779, 1, "S", _SHORT, False, (other,)),
    )
    seen = (
        check_app._Reading("pgrep", 0.0, 0.0, frozenset({4242})),
        check_app._Reading("ps", 0.0, 0.0, frozenset(), (), "", unread),
    )
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(check_app, "_may_run", lambda name: False)
    monkeypatch.setattr(check_app, "_wait_until", lambda test, seconds: False)
    monkeypatch.setattr(check_app, "_look", lambda: seen)
    monkeypatch.setattr("os.kill", lambda pid, sent: killed.append((pid, sent)))
    monkeypatch.setattr(check_app, "_set_pasteboard", lambda text: None)
    monkeypatch.setattr("subprocess.run", lambda argv, **kwargs: _done(argv, 0))
    pairing.tidy()
    assert killed == [(4242, signal.SIGKILL), (7777, signal.SIGKILL)]
    said = capsys.readouterr().out
    assert "still ran, and were killed: [4242, 7777]" in said, said
    assert (
        "possibly host processes of the app's profile still ran, whose executables the check "
        "could not read, and were left: [7778]"
    ) in said, said
    assert (
        "processes that map another copy's bundled runtime, whose arguments ps could not read, "
        "still ran, and were left: [7779]"
    ) in said, said


def test_the_listers_libraries_are_read_from_otool() -> None:
    said = check_app._read_lister_libraries(
        "/bin/ps:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
        "/usr/bin/pgrep (architecture arm64e):\n\t/usr/lib/libsysmon.dylib (compatibility)\n"
        "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
    )
    assert "/bin/ps does not link libsysmon" in said
    assert "/usr/bin/pgrep links libsysmon" in said
    assert "was not listed" in check_app._read_lister_libraries("/bin/ps:\n")


@pytest.mark.parametrize("require_driven", [False, True])
@pytest.mark.parametrize("fails_at", [1, 2, 3, None])
def test_the_placement_check_puts_the_copy_in_applications_back_however_it_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails_at: int | None, require_driven: bool
) -> None:
    # The copy in /Applications is moved aside while the renamed copy runs alone. Whether a
    # refusing copy's check raises (System Events stops answering, say) before that, while the
    # renamed copy runs beside it, while it runs alone, or nothing raises, the check ends with
    # /Applications holding that copy, as it was, and nothing else.
    app = tmp_path / "out" / f"{check_app.APP_NAME}.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "built").write_text("the build copy", encoding="utf-8")
    applications = tmp_path / "Applications"
    installed = applications / app.name
    (installed / "Contents").mkdir(parents=True)
    (installed / "Contents" / "installed").write_text("the installed copy", encoding="utf-8")
    monkeypatch.setattr(check_app, "installed_app_path", lambda: installed)

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == "ditto":
            shutil.copytree(argv[1], argv[2], symlinks=True)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(check_app, "_stop_the_app", lambda checker, what: None)
    monkeypatch.setattr(check_app, "_placement_control", lambda checker, screens, driven: True)
    seen: list[tuple[str, list[str]]] = []
    given: list[dict[str, bool]] = []

    def refusing(
        copy: Path,
        checker: object,
        screens: Path,
        label: str,
        words: object,
        driven: bool,
        **options: bool,
    ) -> None:
        seen.append((label, sorted(path.name for path in applications.iterdir())))
        given.append(options)
        if len(seen) == fails_at:
            raise check_app.CheckError("System Events stopped answering")

    monkeypatch.setattr(check_app, "_refusing_copy", refusing)
    checker = check_app.Checker()
    if fails_at is None:
        check_app.check_placement(app, checker, tmp_path / "work", require_driven)
        assert not checker.failures, checker.failures
    else:
        with pytest.raises(check_app.CheckError):
            check_app.check_placement(app, checker, tmp_path / "work", require_driven)
    renamed = f"{check_app.RENAMED_COPY}.app"
    aside = f".{app.name}.set-aside-by-placement"
    assert (
        seen
        == [
            ("elsewhere", [app.name]),
            ("renamed", sorted([app.name, renamed])),
            ("renamed-alone", sorted([aside, renamed])),
        ][: fails_at or 3]
    )
    assert [path.name for path in applications.iterdir()] == [app.name]
    assert (installed / "Contents" / "installed").read_text(encoding="utf-8") == (
        "the installed copy"
    )
    # The build's copy, outside Applications, offers Show Applications Folder, whose Finder window
    # gates only with --require-driven; the renamed copy, in Applications already, does not.
    outside = {"shows_folder": True, "require_driven": require_driven}
    assert given == [outside, {}, {}][: fails_at or 3]


def test_the_ui_check_resets_a_settings_file_the_host_cannot_read(tmp_path: Path) -> None:
    # The ui check's last checked step: it writes a settings file the host cannot read (the
    # check may write the host's files; the app may not, spec/CLI.md section 2), sees the menu's
    # problem line, opens its note, chooses Reset Settings… and then the question's Reset
    # Settings, and finds the file set aside as policy.invalid.toml and only host.describe on.
    # Whatever happens, it puts the settings back in `finally`: ui leaves the host as it found it.
    # Only the Documents step comes after it, which gates nothing.
    source = _CHECK_APP.read_text(encoding="utf-8")
    ui = _python_function(source, "check_ui")
    assert "_drive_reset(" in ui
    assert ui.index("_drive_code_window(") < ui.index("_drive_reset(")
    assert ui.rindex("_drive_") == ui.index("_drive_documents_prompt(") > ui.index("_drive_reset(")
    reset = _python_function(source, "_drive_reset")
    for name in (
        "SETTINGS_UNREADABLE",
        "RESET_ITEM",
        "RESET_QUESTION",
        "RESET_BUTTON",
        "RESET_TITLE",
        "NOTHING_TO_RESET",
        "UNREADABLE_POLICY",
        "_profile_folder()",
        '"policy.toml"',
        '"policy.invalid.toml"',
        '["--json", "ops", "list"]',
        '"host.describe"',
        "_screenshot(",
    ):
        assert name in reset, name
    tidy = reset.split("    finally:\n", 1)
    assert len(tidy) == 2
    assert "write_bytes(" in tidy[1] and "unlink(missing_ok=True)" in tidy[1]
    # The words it looks for are the app's own.
    words = _swift("ConnectionText.swift")
    for name, said in {
        "resetSettingsItem": check_app.RESET_ITEM,
        "resetQuestionTitle": check_app.RESET_QUESTION,
        "resetButton": check_app.RESET_BUTTON,
        "resetTitle": check_app.RESET_TITLE,
        "nothingToResetTitle": check_app.NOTHING_TO_RESET,
    }.items():
        assert f'static let {name} = "{_named_in_swift(said)}"' in words, name
    unreadable = _named_in_swift(check_app.SETTINGS_UNREADABLE)
    assert f'        "policy_invalid": "{unreadable}",\n' in _swift("HostText.swift")
    assert check_app.RESET_ITEM not in check_app.MENU_ITEMS
    for said in (
        check_app.SETTINGS_UNREADABLE,
        check_app.RESET_ITEM,
        check_app.RESET_QUESTION,
        check_app.RESET_BUTTON,
        check_app.RESET_TITLE,
        check_app.NOTHING_TO_RESET,
    ):
        assert check_app._quoted(said) == f'"{said}"'
    # The note's OK only closes it. Had it run the verb again, the host would have found nothing
    # to set aside, and the app would say so: the check looks for that once the fresh policy has
    # been read.
    done = reset.split("    finally:\n", 1)[0]
    pressed = done.index('        _press("OK")\n')
    assert pressed < done.index("NOTHING_TO_RESET") > done.index('["--json", "ops", "list"]')
    # What it writes is a file the host refuses, as the host's own fixture for policy reset is.
    written = tmp_path / "policy.toml"
    written.write_bytes(check_app.UNREADABLE_POLICY)
    with pytest.raises(policy.PolicyError):
        policy.check_file(written)


@pytest.mark.parametrize("held", [True, False])
def test_the_reset_check_leaves_the_settings_as_it_found_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held: bool
) -> None:
    # Whatever the app did or failed to do, the settings file, and a file an earlier reset set
    # aside, are put back as they were, or taken away when there was none. Here System Events
    # answers nothing, so every step after the write fails.
    profile = tmp_path / "profile"
    profile.mkdir()
    settings = profile / "policy.toml"
    aside = profile / "policy.invalid.toml"
    if held:
        settings.write_bytes(b"# the settings the check found\n")
        aside.write_bytes(b"# what an earlier reset set aside\n")
    hosted: list[list[str]] = []

    def host(python: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        hosted.append(args)
        return subprocess.CompletedProcess(args, 1, "", "")

    def menu_once(
        test: Callable[[list[str]], bool], seconds: float, shot: Path | None = None
    ) -> tuple[bool, list[str]]:
        worked, shown = check_app._open_menu_and_read()
        return worked and test(shown), shown

    monkeypatch.setattr(check_app, "_profile_folder", lambda: profile)
    monkeypatch.setattr(check_app, "_system_events", lambda body, timeout=30: (False, "no"))
    monkeypatch.setattr(check_app, "_screenshot", lambda path: None)
    monkeypatch.setattr(check_app, "_app_host", host)
    monkeypatch.setattr(check_app, "_wait_until", lambda test, seconds: test())
    monkeypatch.setattr(check_app, "_menu_until", menu_once)
    checker = check_app.Checker()
    check_app._drive_reset(checker, tmp_path / "python", tmp_path)
    assert checker.failures
    if held:
        assert settings.read_bytes() == b"# the settings the check found\n"
        assert aside.read_bytes() == b"# what an earlier reset set aside\n"
    else:
        assert not settings.exists() and not aside.exists()
    assert sorted(path.name for path in profile.iterdir()) == (
        ["policy.invalid.toml", "policy.toml"] if held else []
    )


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


# --- The one opener: Open Date & Time Settings, Open Privacy Settings, Show Applications Folder
# (decisions/0002 §20) --------------------------------------------------------------------------

# Where the app's one opener is called, by file: the function, or the closure Set Up's window
# calls, whose body holds that file's call. Each is a click's: a note's button, or a button of
# Set Up's Folders page.
_OPEN_CALLS: dict[str, tuple[str, ...]] = {
    "Connection.swift": (r"    func showAttention\(\) \{", r"        setupPanel\.onOpenPlace = \{"),
    "HostModel.swift": (
        r"    private func showFailure\(_ explanation: Explanation, action: String\) \{",
    ),
    "MenuBarApp.swift": (r"    static func show\(couldNotOpen failed: SystemPlace\? = nil\) \{",),
}
# In each of those, the closure the call sits in: a note's button's action, or a note's done.
_OPEN_CLOSURES: dict[str, str] = {
    r"    func showAttention\(\) \{": r"actions = \[\{",
    r"        setupPanel\.onOpenPlace = \{": r"onOpenPlace = \{",
    r"    private func showFailure\(_ explanation: Explanation, action: String\) \{": r"done: \{",
    r"    static func show\(couldNotOpen failed: SystemPlace\? = nil\) \{": r"done: \{",
}
_SYSTEM_PLACE_CASES = ("dateAndTime", "filesAndFolders", "applicationsFolder")


def test_the_opener_opens_only_its_places() -> None:
    # One file, SystemPlaces.swift, names the class that opens things, once, in one call; the
    # URL it opens is made there, from a closed list of three places: the two System Settings
    # pages macos_lint's OPENABLE holds, which the places check opens too, and the Applications
    # folder. Every call of it is in a button's action (decisions/0002 §20, M20).
    opener = _swift("SystemPlaces.swift")
    assert opener.count("NSWorkspace") == 1
    assert len(re.findall(r"\bfunc\b", _code_of(opener))) == 1
    for path in [*SWIFT_FILES, _SELF_TEST]:
        if path.name != "SystemPlaces.swift":
            assert "NSWorkspace" not in path.read_text(encoding="utf-8"), path.name
    assert _swift_imports(opener) == {"AppKit"}
    assert "SystemPlaces.swift" not in check_app.LOGIC_SOURCES
    assert "@MainActor\nenum SystemPlaces {\n" in opener
    body = _swift_function(opener, "    static func open(_ place: SystemPlace) -> Bool {")
    assert re.findall(r"^        case \.(\w+):$", body, re.MULTILINE) == list(_SYSTEM_PLACE_CASES)
    assert "default:" not in body
    # The one call is the function's last statement, after the one guard; and each place opens
    # its own address: Open Date & Time Settings never opens Files & Folders.
    assert body.endswith(
        "        guard let url else {\n            return false\n        }\n"
        "        return NSWorkspace.shared.open(url)\n    }\n"
    ), body
    for case, made in (
        ("dateAndTime", f'URL(string: "{check_app.DATE_AND_TIME_PANE}")'),
        ("filesAndFolders", f'URL(string: "{check_app.FILES_AND_FOLDERS_PANE}")'),
        ("applicationsFolder", 'URL(fileURLWithPath: "/Applications", isDirectory: true)'),
    ):
        assert f"        case .{case}:\n            url = {made}\n" in body, case
    assert check_app.SETTINGS_PANES[check_app.DATE_AND_TIME_PANE] == "Date & Time"
    assert (check_app.DATE_AND_TIME_PANE, check_app.FILES_AND_FOLDERS_PANE) == (
        macos_lint.DATE_AND_TIME_PAGE,
        macos_lint.FILES_AND_FOLDERS_PAGE,
    )
    assert set(re.findall(r'URL\(string: "([^"]+)"\)', body)) == macos_lint.OPENABLE
    assert body.count("URL(string:") == len(macos_lint.OPENABLE) == 2
    assert body.count('URL(fileURLWithPath: "/Applications", isDirectory: true)') == 1
    assert set(check_app.SETTINGS_PANES) == macos_lint.OPENABLE
    assert len(check_app.SETTINGS_PANES) == len(macos_lint.OPENABLE)
    # The places: closed, and carrying no value, so nothing read at run time, and nothing Lucille
    # sends, chooses or makes where the opener goes (M19). Declared once, in a file that draws
    # nothing, and given nothing more anywhere.
    rules = _swift("PlaceRules.swift")
    cases = rules.split("enum SystemPlace: Equatable, Sendable {\n", 1)[1].split("\n}\n", 1)[0]
    assert [
        line.strip()
        for line in cases.splitlines()
        if line.strip() and not line.strip().startswith("//")
    ] == [f"case {place}" for place in _SYSTEM_PLACE_CASES]
    for path in [*SWIFT_FILES, _SELF_TEST]:
        text = path.read_text(encoding="utf-8")
        declared = len(re.findall(r"\benum SystemPlace\b", text))
        assert declared == (path.name == "PlaceRules.swift"), path.name
        assert not re.search(r"\bextension SystemPlace\b", text), path.name
    # Each call is in a button's action: a note's, by its done or its actions, or the closure Set
    # Up's window calls from its own button; and nowhere else.
    for path in SWIFT_FILES:
        text = path.read_text(encoding="utf-8")
        openers = _OPEN_CALLS.get(path.name, ())
        assert text.count("SystemPlaces.open(") == len(openers), path.name
        for start in openers:
            (block,) = _swift_blocks(text, start)
            assert block.count("SystemPlaces.open(") == 1, (path.name, start)
            held = [found for found in _swift_blocks(block, _OPEN_CLOSURES[start])]
            assert [found.count("SystemPlaces.open(") for found in held].count(1) == 1, start
    panel = _swift("SetupPanel.swift")
    assert "    var onOpenPlace: ((SystemPlace) -> Void)?\n" in panel
    assert panel.count("onOpenPlace?(") == 1
    pressed = _swift_function(
        panel, "    @objc private func openPlacePressed(_ sender: NSButton) {"
    )
    assert "onOpenPlace?(" in pressed
    # That closure is called only from that action, and set only once, in Connection; and that
    # action is named only by the buttons that send it. Anything else that called either would
    # open a place with no click, and no count of SystemPlaces.open( would see it.
    for path in [*SWIFT_FILES, _SELF_TEST]:
        code = macos_lint.swift_scan(path.read_text(encoding="utf-8")).code
        held = {"SetupPanel.swift": 2, "Connection.swift": 1}.get(path.name, 0)
        assert len(re.findall(r"\bonOpenPlace\b", code)) == held, path.name
        sent = 2 if path.name == "SetupPanel.swift" else 0
        assert len(re.findall(r"\bopenPlacePressed\b", code)) == sent, path.name
    assert len(re.findall(r"\bonOpenPlace\b", pressed)) == 1
    assert "action: #selector(openPlacePressed(_:))" in panel
    assert _swift("Connection.swift").count("        setupPanel.onOpenPlace = {") == 1
    # The place each call opens is the one PlaceRules gives, from the app's own state: the
    # problem `run` reports, macOS's refusal of a folder, where this copy is.
    connection = _swift("Connection.swift")
    attention = _swift_function(connection, "    func showAttention() {")
    assert "place = PlaceRules.place(for: problem)" in attention
    assert "buttons = [PlaceText.button(place), ConnectionText.okButton]" in attention
    failure = _swift_function(
        _swift("HostModel.swift"),
        "    private func showFailure(_ explanation: Explanation, action: String) {",
    )
    assert "let place = explanation.place" in failure
    assert "alert.addButton(withTitle: PlaceText.button(place))" in failure
    # Only the place's own button opens it, the note's first: OK opens nothing.
    assert re.findall(r"^ *alert\.addButton\(withTitle: (.+)\)$", failure, re.MULTILINE) == [
        "PlaceText.button(place)",
        '"OK"',
    ]
    assert "if let place, response == .alertFirstButtonReturn, !SystemPlaces.open(place) {" in (
        failure
    )
    placement = _swift_function(
        _swift("MenuBarApp.swift"),
        "    static func show(couldNotOpen failed: SystemPlace? = nil) {",
    )
    assert "PlaceRules.place(for: Placement.current)" in placement
    assert re.findall(r"^ *alert\.addButton\(withTitle: (.+)\)$", placement, re.MULTILINE) == [
        "PlaceText.button(place)",
        "ConnectionText.quitButton",
    ]
    # Quit quits, and opens nothing; the place's own button, the first, opens it.
    done = placement.split("}, done: { response in\n", 1)[1]
    assert done.startswith(
        "            if let place, response == .alertFirstButtonReturn {\n"
        "                if SystemPlaces.open(place) {\n"
    ), done
    assert done.count("SystemPlaces.open(") == 1
    row = _swift_function(
        panel, "    private func folderRow(_ folder: Folder, tag: Int) -> NSView {"
    )
    assert "PlaceRules.place(for: check.state)" in row and "PlaceText.button(" in row
    # When macOS could not open the place, the note says so first and keeps its words.
    assert "    func notOpened(_ place: SystemPlace, title: String, text: String) {\n" in connection
    said = _swift_function(
        connection, "    func notOpened(_ place: SystemPlace, title: String, text: String) {"
    )
    assert "PlaceText.couldNotOpen(place)" in said


def test_nothing_lucille_sends_reaches_the_opener() -> None:
    # M18 and M19 (decisions/0002 §22): no request, no text of Lucille's, and no refusal of a
    # request ever offers a place. The files that read or show what Lucille sends name none of the
    # opener's types, and the waiting page's clock sentence stays words only (the gate window is
    # left alone).
    for name in (
        "RequestLog.swift",
        "ActivityPanel.swift",
        "HostJSON.swift",
        "Pairing.swift",
        "PairingPanel.swift",
        "Supervisor.swift",
    ):
        text = _swift(name)
        for word in ("SystemPlace", "PlaceRules", "PlaceText"):
            assert word not in text, (name, word)
    assert "ConnectionText.waitingClockWrong" in _swift("PairingPanel.swift")
    rules = _code_of(_swift("PlaceRules.swift"))
    for word in ("Request", "NSWorkspace", "SystemPlaces.", "URL(", "x-apple", "NSAlert", "Alerts"):
        assert word not in rules, word
    # The words that said the app opens nothing are gone with it.
    assert "Words only: the app opens no settings itself." not in _swift("ConnectionText.swift")
    assert "Open Privacy Settings comes with the app's one opener" not in _swift("Setup.swift")


def test_the_place_rules_draw_nothing_and_the_self_test_checks_them() -> None:
    # PlaceRules.swift is Foundation only, compiled by the self-test (NEW_LOGIC), whose
    # checkPlaces holds each rule: clock skew offers Date & Time, macOS's privacy protection
    # Files & Folders, a copy outside Applications the Applications folder, and nothing else
    # offers anything; no refusal of a request ever does.
    assert "PlaceRules.swift" in check_app.LOGIC_SOURCES
    rules = _swift("PlaceRules.swift")
    for declared in (
        "enum SystemPlace: Equatable, Sendable {",
        "enum PlaceRules {",
        "enum PlaceText {",
        "    static func place(for problem: Problem) -> SystemPlace? {",
        "    static func place(for state: FolderCheck.State) -> SystemPlace? {",
        "    static func place(for placement: Placement) -> SystemPlace? {",
        "    static func place(for error: HostError, during activity: HostText.Activity)"
        " -> SystemPlace? {",
        "    static func button(_ place: SystemPlace) -> String {",
        "    static func couldNotOpen(_ place: SystemPlace) -> String {",
    ):
        assert declared in rules, declared
    # Every case of what each rule reads is named, so a new one must be decided there.
    for signature in (
        "    static func place(for problem: Problem) -> SystemPlace? {",
        "    static func place(for state: FolderCheck.State) -> SystemPlace? {",
        "    static func place(for placement: Placement) -> SystemPlace? {",
        "    static func button(_ place: SystemPlace) -> String {",
        "    static func couldNotOpen(_ place: SystemPlace) -> String {",
    ):
        assert "default:" not in _swift_function(rules, signature), signature
    # The explanation of a failed run carries its place, set in one place, from the refusal.
    text = _swift("HostText.swift")
    assert "    var place: SystemPlace? = nil\n" in text
    # Before offersReset, so that HostText.explain names offersReset last, as
    # test_reset_settings_is_offered_only_while_the_policy_is_invalid reads it.
    assert text.index("    var place: SystemPlace? = nil\n") < text.index(
        "    var offersReset = false\n"
    )
    assert re.findall(r"offersReset: ([^\n]*)", text) == ['error.code == "policy_invalid"']
    assert text.count("place: PlaceRules.place(for: error, during: activity)") == 1
    for path in SWIFT_FILES:
        if path.name != "HostText.swift":
            assert "place: PlaceRules." not in path.read_text(encoding="utf-8"), path.name
    selftest = _SELF_TEST.read_text(encoding="utf-8")
    main_body = _swift_function(selftest, "    static func main() {")
    assert main_body.index('checkPlacement(home: options["home"], report)') < main_body.index(
        "checkPlaces(report)"
    )
    checks = _swift_function(selftest, "    static func checkPlaces(_ report: Report) {")
    for needed in (
        "PlaceRules.place(for: Problem.clock(skew: ",
        "== .dateAndTime",
        ".deniedByMacOS",
        "== .filesAndFolders",
        ".notInApplications(",
        "== .applicationsFolder",
        ".renamed(",
        # Nothing else: every other case of each, by name.
        ".notResponding",
        ".policyInvalid",
        ".policyStuck",
        ".hubUnreachable(",
        ".readStuck",
        ".leftConnection(",
        ".halted(",
        # No refusal of a request, and no other refusal of a folder, ever offers one.
        "RequestText.refused.keys",
        "RequestText.failed.keys",
        "HostText.refusals.keys",
        "HostText.explain(",
        ".place == .filesAndFolders",
        # The words.
        'PlaceText.button(.dateAndTime) == "Open Date & Time Settings"',
        'PlaceText.button(.filesAndFolders) == "Open Privacy Settings"',
        'PlaceText.button(.applicationsFolder) == "Show Applications Folder"',
        "PlaceText.couldNotOpen(",
    ):
        assert needed in checks, needed
    # Every other state of a folder's check and every place a refused copy can be, each by its
    # own name in the arrays the self-test walks (a name inside another, .denied inside
    # .deniedByMacOS, would pin nothing).
    assert "let states: [FolderCheck.State] = [.ok, .denied, .missing, .error(errno: " in checks
    assert (
        "let wheres: [Placement.Where] = [.diskImage, .insideApplications, .homeApplications, "
        ".elsewhere]" in checks
    )


def test_the_place_checks_look_for_the_apps_own_words() -> None:
    # What check_app.py presses and reads, as PlaceRules.swift and HostText.swift spell it.
    rules = _swift("PlaceRules.swift")
    assert check_app.SHOW_APPLICATIONS_FOLDER == "Show Applications Folder"
    assert check_app.OPEN_PRIVACY_SETTINGS == "Open Privacy Settings"
    for said in (
        check_app.SHOW_APPLICATIONS_FOLDER,
        check_app.OPEN_PRIVACY_SETTINGS,
        "Open Date & Time Settings",
    ):
        assert f'"{said}"' in rules, said
        assert check_app._quoted(said) == f'"{said}"'
    name = check_app.APP_NAME
    assert check_app.BLOCKED_BY_MACOS == f"macOS is blocking {name} from this folder."
    assert f'static let blockedByMacOS = "{_named_in_swift(check_app.BLOCKED_BY_MACOS)} ' in _swift(
        "HostText.swift"
    )
    for could in (
        f"{name} couldn't open System Settings.",
        f"{name} couldn't open your Applications folder.",
    ):
        assert f'"{_named_in_swift(could)}"' in rules, could
    # The titles the places check expects, which only a Mac can show.
    assert list(check_app.SETTINGS_PANES.values()) == ["Date & Time", "Files & Folders"]
    assert check_app.SETTINGS_PANES[check_app.FILES_AND_FOLDERS_PANE] == "Files & Folders"
    for title in check_app.SETTINGS_PANES.values():
        assert check_app._quoted(title) == f'"{title}"'
    assert check_app.SYSTEM_SETTINGS == "System Settings"
    assert (check_app.FINDER, check_app.APPLICATIONS_WINDOW) == ("Finder", "Applications")


def _no_system_events_for_the_app(body: str, timeout: float = 30) -> tuple[bool, str]:
    raise AssertionError(f"this step sends nothing to the app through System Events: {body!r}")


# --- check_app.py places, run here against a stand-in for System Settings ----------------------


class _SettingsOnAMac:
    """System Settings as the places check sees it, made up. ``/usr/bin/open`` with a URL starts
    it, unless ``starts`` is false (open then exits 1), showing the window ``shows`` gives for
    that URL (by default the title SETTINGS_PANES expects; none for a URL it lacks).
    ``pkill -TERM -x "System Settings"`` quits it, unless ``quits`` is false; ``pkill -KILL``
    always does. With ``raises``, System Events raises when asked for its windows; with
    ``hangs``, open does not finish within its time limit; with ``running``, System Settings runs
    from the start, someone's own window open; ``exits`` is what open exits with when it starts
    it. Every argv run is kept in ``ran``, each one's time
    limit in ``limits``, each screenshot's name in ``shots``. Its words are read only at run time:
    an instance is made when this module is imported."""

    def __init__(
        self,
        *,
        shows: dict[str, str] | None = None,
        starts: bool = True,
        quits: bool = True,
        raises: bool = False,
        hangs: bool = False,
        running: bool = False,
        exits: int = 0,
    ) -> None:
        self.shows = shows
        self.starts = starts
        self.exits = exits
        self.quits = quits
        self.raises = raises
        self.hangs = hangs
        self.running = running
        self.window: str | None = "Wi-Fi" if running else None
        self.ran: list[list[str]] = []
        self.limits: list[float | None] = []
        self.shots: list[str] = []

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(arg) for arg in argv]
        self.ran.append(argv)
        self.limits.append(kwargs.get("timeout"))
        status = 0
        if argv[0] == "/usr/bin/open":
            if self.hangs:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 0)
            if self.starts:
                shows = dict(check_app.SETTINGS_PANES) if self.shows is None else self.shows
                self.running = True
                self.window = shows.get(argv[1])
                status = self.exits
            else:
                status = 1
        elif argv[:2] == ["pkill", "-KILL"] or (argv[:2] == ["pkill", "-TERM"] and self.quits):
            if argv[-2:] == ["-x", check_app.SYSTEM_SETTINGS]:
                self.running = False
                self.window = None
        return subprocess.CompletedProcess(argv, status, "", "")

    def names(self, name: str) -> list[str]:
        return ["777"] if name == check_app.SYSTEM_SETTINGS and self.running else []

    def windows(self, process: str) -> tuple[bool, list[str]]:
        if self.raises:
            raise check_app.CheckError("System Events stopped answering")
        if process != check_app.SYSTEM_SETTINGS or not self.running:
            return True, []
        return True, [self.window] if self.window else []


def _places_on(
    mac: _SettingsOnAMac,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    record_only: bool = False,
) -> check_app.Checker:
    monkeypatch.setattr("subprocess.run", mac.run)
    monkeypatch.setattr(check_app, "_running", mac.names)
    monkeypatch.setattr(check_app, "_process_windows", mac.windows, raising=False)
    monkeypatch.setattr(check_app, "_screenshot", lambda path: mac.shots.append(path.name))
    monkeypatch.setattr(check_app, "_wait_until", lambda test, seconds: test())
    monkeypatch.setattr(check_app.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(check_app, "_system_events", _no_system_events_for_the_app)
    checker = check_app.Checker()
    check_app.check_places(checker, tmp_path, record_only=record_only)
    return checker


@pytest.mark.parametrize("record_only", [False, True])
def test_the_places_check_opens_each_page_itself_and_quits_system_settings_by_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    record_only: bool,
) -> None:
    # The check, not the app, opens each System Settings page the opener may show, with
    # /usr/bin/open; reads its windows' names and prints them, so the first run on each macOS
    # says what the page is called; takes a screenshot; and quits System Settings by its name,
    # with no key sent: an Escape with no menu open closed a window in a check before.
    mac = _SettingsOnAMac()
    checker = _places_on(mac, monkeypatch, tmp_path, record_only=record_only)
    assert checker.failures == []
    assert mac.ran == [
        argv
        for url in check_app.SETTINGS_PANES
        for argv in (["/usr/bin/open", url], ["pkill", "-TERM", "-x", check_app.SYSTEM_SETTINGS])
    ]
    assert not mac.running
    assert len(mac.shots) == len(check_app.SETTINGS_PANES) == 2
    said = capsys.readouterr().out
    for url, title in check_app.SETTINGS_PANES.items():
        assert f"{url}: System Settings windows: {[title]}" in said, said


@pytest.mark.parametrize(
    ("mac", "failing"),
    [
        # Each page shows a window under another name: both are counted, each by the title.
        (_SettingsOnAMac(shows={}), ("Date & Time", "Files & Folders")),
        # System Settings never starts.
        (_SettingsOnAMac(starts=False), ("starts System Settings", "starts System Settings")),
        # It does not quit when asked by name: it is stopped, and that is counted.
        (_SettingsOnAMac(quits=False), ("quits", "quits")),
    ],
)
def test_the_places_check_gates_on_what_system_settings_shows(
    mac: _SettingsOnAMac,
    failing: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checker = _places_on(mac, monkeypatch, tmp_path)
    assert len(checker.failures) == len(failing), checker.failures
    for found, words in zip(checker.failures, failing, strict=True):
        assert words in found, checker.failures
    assert not mac.running


@pytest.mark.parametrize(
    "mac",
    [
        _SettingsOnAMac(shows={}),
        _SettingsOnAMac(starts=False),
        _SettingsOnAMac(quits=False),
        _SettingsOnAMac(raises=True),
    ],
)
def test_the_places_check_only_records_with_record_only(
    mac: _SettingsOnAMac,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # On macOS 26 and 27 the check prints what it saw and fails nothing, whatever it saw, and
    # whatever went wrong while it looked: those jobs record the pages, macos-15 gates them.
    checker = _places_on(mac, monkeypatch, tmp_path, record_only=True)
    assert checker.failures == []
    assert "recorded, not a gate" in capsys.readouterr().out
    assert not mac.running


def test_the_places_check_quits_system_settings_whatever_stops_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mac = _SettingsOnAMac(raises=True)
    with pytest.raises(check_app.CheckError):
        _places_on(mac, monkeypatch, tmp_path)
    assert ["pkill", "-TERM", "-x", check_app.SYSTEM_SETTINGS] in mac.ran
    assert not mac.running


@pytest.mark.parametrize("record_only", [False, True])
def test_the_places_check_quits_a_system_settings_that_started_though_open_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, record_only: bool
) -> None:
    # open exits 1, and System Settings starts anyway: the page is reported as not started, and
    # System Settings is still quit, by its name, before the next page and after the last.
    mac = _SettingsOnAMac(exits=1)
    checker = _places_on(mac, monkeypatch, tmp_path, record_only=record_only)
    assert len(checker.failures) == (0 if record_only else 2), checker.failures
    assert not mac.running
    assert mac.ran.count(["pkill", "-TERM", "-x", check_app.SYSTEM_SETTINGS]) == 2


def test_with_record_only_a_page_that_did_not_start_is_not_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Nothing started, so there is no window to read and nothing to take a screenshot of.
    mac = _SettingsOnAMac(starts=False)
    _places_on(mac, monkeypatch, tmp_path, record_only=True)
    assert mac.shots == []
    assert "System Settings windows" not in capsys.readouterr().out


def test_with_record_only_a_passing_page_prints_no_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A failure's detail is built only when the step failed: on a pass, each recorded line stands
    # alone, with no open's output and no list of windows under it.
    _places_on(_SettingsOnAMac(), monkeypatch, tmp_path, record_only=True)
    lines = capsys.readouterr().out.splitlines()
    recorded = [line for line in lines if line.startswith("recorded, not a gate:")]
    assert len(recorded) == 3 * len(check_app.SETTINGS_PANES), lines
    assert all(line.startswith("recorded, not a gate: yes, ") for line in recorded), recorded
    assert not any(line.startswith(("exit ", "stdout: ", "stderr: ")) for line in lines), lines
    for title in check_app.SETTINGS_PANES.values():
        assert f"{[title]}" not in lines, lines


def test_a_places_page_is_given_what_its_waits_can_take() -> None:
    # Quitting System Settings before the page and after it (SIGTERM, then 15 s, then SIGKILL,
    # then 10 s); /usr/bin/open; 30 s for System Settings to start; 30 s for its window, then
    # a System Events read of up to 30 s begun just before those end, and the one more read that
    # _wait_until makes after its time is up; and a screenshot of up to 30 s.
    quit_ = 15 + 10
    window = 30 + 30 + 30
    assert check_app.PLACES_PAGE_LIMIT == quit_ + check_app.OPEN_LIMIT + 30 + window + 30 + quit_
    asked: list[int] = []
    assert check_app._wait_until(lambda: bool(asked.append(1)), 0) is False
    assert asked == [1]


def test_the_places_check_needs_no_app_and_sends_no_key() -> None:
    source = _CHECK_APP.read_text(encoding="utf-8")
    check = _python_function(source, "check_places")
    for sent in ("key code", "keystroke", "_system_events(", "_press(", "Escape"):
        assert sent not in check, sent
    assert '"/usr/bin/open"' in check and "_quit_by_name(SYSTEM_SETTINGS)" in check
    assert "finally:" in check
    quit_by_name = _python_function(source, "_quit_by_name")
    assert '["pkill", "-TERM", "-x", name]' in quit_by_name
    assert "places" in check_app.CHECKS and "places" in check_app.NEEDS_NO_APP
    assert "places" not in check_app.OPENS_INSTALLED and "places" not in check_app.REQUIRED
    args = check_app._parser().parse_args(["places", "--record-only"])
    assert args.record_only is True
    assert check_app._parser().parse_args(["places"]).record_only is False


# --- check_app.py placement: Show Applications Folder, run here against a stand-in ------------


class _PlacementNoteOnAMac:
    """The note a copy that may not run shows, as System Events sees it, and the Finder's
    windows. The note says ``words`` and offers ``buttons`` (by default Show Applications Folder
    and Quit). Show Applications Folder opens a Finder window named ``opens_as`` (by default
    Applications), unless ``opens`` is false, and quits the app, unless ``quits`` is false; Quit
    quits it. ``before`` are the Finder's windows already open. Every press, window closed and
    screenshot is kept. Its words are read only at run time."""

    def __init__(
        self,
        *,
        buttons: list[str] | None = None,
        words: tuple[str, str] | None = None,
        opens: bool = True,
        opens_as: str | None = None,
        quits: bool = True,
        before: tuple[str, ...] = (),
        failing_reads: tuple[int, ...] = (),
    ) -> None:
        self.buttons = buttons
        self.words = words
        self.opens = opens
        self.opens_as = opens_as
        self.quits = quits
        self.finder = list(before)
        self.failing_reads = failing_reads
        self.finder_reads = 0
        self.app_running = True
        self.pressed: list[str] = []
        self.closed: list[tuple[str, str]] = []
        self.shots: list[str] = []
        self.text_reads: list[int] = []

    def offered(self) -> list[str]:
        if self.buttons is not None:
            return self.buttons
        return [check_app.SHOW_APPLICATIONS_FOLDER, check_app.QUIT_BUTTON]

    def texts(self) -> list[str]:
        words = self.words or (check_app.PLACEMENT_TITLE, check_app.PLACEMENT_ELSEWHERE)
        return list(words) if self.app_running else []

    def read_texts(self) -> list[str]:
        # The check's own read of the note's words, with how many presses came before it.
        self.text_reads.append(len(self.pressed))
        return self.texts()

    def says(self, words: str) -> bool:
        return any(words in text for text in self.texts())

    def window_list(self, what: str) -> tuple[bool, list[str]]:
        assert what == "name of every button", what
        return True, self.offered() if self.app_running else []

    def press(self, name: str) -> tuple[bool, str]:
        self.pressed.append(name)
        if not self.app_running or name not in self.offered():
            return False, f"no button named {name}"
        if name == check_app.QUIT_BUTTON:
            self.app_running = False
        else:
            # Show Applications Folder, the one other button such a note may offer.
            if self.opens:
                self.finder.append(self.opens_as or check_app.APPLICATIONS_WINDOW)
            self.app_running = not self.quits
        return True, "pressed"

    def windows(self, process: str) -> tuple[bool, list[str]]:
        if process != check_app.FINDER:
            return True, []
        self.finder_reads += 1
        if self.finder_reads - 1 in self.failing_reads:
            return False, ["(System Events: not allowed assistive access)"]
        return True, list(self.finder)

    def close_window_of(self, process: str, title: str) -> tuple[bool, str]:
        self.closed.append((process, title))
        if process == check_app.FINDER and title in self.finder:
            self.finder.remove(title)
            return True, "closed"
        return False, "no such window"


def _refusing_copy_on(
    mac: _PlacementNoteOnAMac,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    label: str = "elsewhere",
    checker: check_app.Checker | None = None,
    **options: bool,
) -> check_app.Checker:
    _, opened = _placement_world(tmp_path, monkeypatch, starts=False, writes=False)
    monkeypatch.setattr(check_app, "REFUSAL_WATCH", 0.2)
    monkeypatch.setattr(
        check_app, "_running", lambda name: [99] if opened and mac.app_running else []
    )
    for name, fake in (
        ("_texts", mac.read_texts),
        ("_says", mac.says),
        ("_window_list", mac.window_list),
        ("_press", mac.press),
        ("_process_windows", mac.windows),
        ("_close_window_of", mac.close_window_of),
    ):
        monkeypatch.setattr(check_app, name, fake, raising=False)
    monkeypatch.setattr(check_app, "_screenshot", lambda path: mac.shots.append(path.name))
    monkeypatch.setattr(check_app, "_system_events", _no_system_events_for_the_app)
    checker = checker or check_app.Checker()
    copy = tmp_path / "out" / f"{check_app.APP_NAME}.app"
    words = mac.words or (check_app.PLACEMENT_TITLE, check_app.PLACEMENT_ELSEWHERE)
    check_app._refusing_copy(copy, checker, tmp_path, label, words, True, **options)
    return checker


@pytest.mark.parametrize("require_driven", [True, False])
def test_a_copy_outside_applications_shows_the_applications_folder_then_quits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    require_driven: bool,
) -> None:
    # Its note offers Show Applications Folder and Quit, and the check presses the first, a
    # button it has just read: the app quits, and a Finder window named Applications opens, which
    # the check prints, and closes, since it opened it.
    mac = _PlacementNoteOnAMac()
    checker = _refusing_copy_on(
        mac, monkeypatch, tmp_path, shows_folder=True, require_driven=require_driven
    )
    assert checker.failures == []
    assert mac.pressed == [check_app.SHOW_APPLICATIONS_FOLDER]
    assert not mac.app_running
    assert mac.closed == [(check_app.FINDER, check_app.APPLICATIONS_WINDOW)]
    assert check_app.APPLICATIONS_WINDOW not in mac.finder
    assert "0-placement-elsewhere-folder.png" in mac.shots
    assert f"{[check_app.APPLICATIONS_WINDOW]}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("mac", "require_driven", "failing"),
    [
        # A note that offers only Quit, as before: counted, and nothing is pressed.
        (_PlacementNoteOnAMac(buttons=["Quit"]), True, ("its note offers",)),
        # The app does not quit once it has shown the folder.
        (_PlacementNoteOnAMac(quits=False), True, ("quits it",)),
        # No Finder window named Applications: macos-15 counts it; elsewhere it is printed.
        (_PlacementNoteOnAMac(opens=False), True, ("Finder window",)),
        (_PlacementNoteOnAMac(opens=False), False, ()),
    ],
)
def test_a_copy_outside_applications_that_does_not_show_the_folder_is_counted(
    mac: _PlacementNoteOnAMac,
    require_driven: bool,
    failing: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checker = _refusing_copy_on(
        mac, monkeypatch, tmp_path, shows_folder=True, require_driven=require_driven
    )
    assert len(checker.failures) == len(failing), checker.failures
    for found, words in zip(checker.failures, failing, strict=True):
        assert words in found, checker.failures
    # Only a button the check read is pressed, and a Finder window it opened is closed again.
    assert set(mac.pressed) <= {check_app.SHOW_APPLICATIONS_FOLDER}
    assert check_app.APPLICATIONS_WINDOW not in mac.finder
    if not failing:
        assert "recorded, not a gate" in capsys.readouterr().out


def test_the_notes_buttons_are_taken_in_either_order_and_printed_as_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # In which order System Events lists a note's two buttons is not yet seen on a Mac, and the
    # Swift's order is the build test's to hold: the check takes them in either order, prints
    # them as read so that the first runs record the order, and still counts a third button, or
    # one of the two listed twice.
    offered = [check_app.QUIT_BUTTON, check_app.SHOW_APPLICATIONS_FOLDER]
    mac = _PlacementNoteOnAMac(buttons=offered)
    checker = _refusing_copy_on(mac, monkeypatch, tmp_path, shows_folder=True, require_driven=True)
    assert checker.failures == []
    assert mac.pressed == [check_app.SHOW_APPLICATIONS_FOLDER]
    said = capsys.readouterr().out
    assert f"its note's buttons, as System Events lists them: {offered}" in said, said
    for number, more in enumerate(("OK", check_app.QUIT_BUTTON)):
        again = tmp_path / f"again-{number}"
        again.mkdir()
        mac = _PlacementNoteOnAMac(buttons=[*offered, more])
        checker = _refusing_copy_on(mac, monkeypatch, again, shows_folder=True, require_driven=True)
        assert any("its note offers" in failure for failure in checker.failures), checker.failures


def test_where_the_finders_windows_before_cannot_be_read_none_is_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Any window open then may be someone's, so none is closed, and the check says so.
    mac = _PlacementNoteOnAMac(before=("Downloads",), failing_reads=(0,))
    checker = _refusing_copy_on(mac, monkeypatch, tmp_path, shows_folder=True, require_driven=True)
    assert checker.failures == []
    assert mac.closed == [] and mac.finder == ["Downloads", check_app.APPLICATIONS_WINDOW]
    assert "the Finder's windows could not be read, so none was closed" in (capsys.readouterr().out)


def test_the_finders_windows_are_read_again_before_any_is_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The wait's last read may be System Events' error: what is closed is what a read after the
    # wait gives, never the error's words.
    mac = _PlacementNoteOnAMac(before=("Downloads",), failing_reads=(1,))
    _refusing_copy_on(mac, monkeypatch, tmp_path, shows_folder=True, require_driven=False)
    assert mac.closed == [(check_app.FINDER, check_app.APPLICATIONS_WINDOW)]
    assert mac.finder == ["Downloads"]


class _RecordingChecker(check_app.Checker):
    """A checker that keeps each expectation it is given: (ok, what, detail)."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[bool, str, str]] = []

    def expect(self, ok: bool, what: str, detail: str = "") -> bool:
        self.seen.append((ok, what, detail))
        return super().expect(ok, what, detail)


def test_a_passing_folder_step_builds_no_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A failure's detail is built only when the step failed: after the press that quits, the
    # check reads none of the app's words, and the Finder's gate is given no detail.
    mac = _PlacementNoteOnAMac()
    checker = _RecordingChecker()
    _refusing_copy_on(
        mac, monkeypatch, tmp_path, checker=checker, shows_folder=True, require_driven=True
    )
    assert checker.failures == []
    assert mac.pressed == [check_app.SHOW_APPLICATIONS_FOLDER]
    assert all(presses == 0 for presses in mac.text_reads), mac.text_reads
    folder = [
        (ok, detail)
        for ok, what, detail in checker.seen
        if "Finder window named" in what or "quits it" in what
    ]
    assert folder == [(True, ""), (True, "")], checker.seen


def test_a_finder_window_open_before_is_left_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mac = _PlacementNoteOnAMac(before=("Applications",))
    checker = _refusing_copy_on(mac, monkeypatch, tmp_path, shows_folder=True, require_driven=True)
    assert checker.failures == []
    assert mac.closed == [] and mac.finder.count(check_app.APPLICATIONS_WINDOW) >= 1


def test_a_renamed_copy_still_offers_only_quit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A renamed copy is in Applications already: its note offers Quit alone, which quits it, and
    # the Finder is not looked at.
    words = (check_app.RENAMED_TITLE, check_app.RENAMED_ALONE)
    mac = _PlacementNoteOnAMac(buttons=[check_app.QUIT_BUTTON], words=words)
    checker = _refusing_copy_on(mac, monkeypatch, tmp_path, "renamed-alone")
    assert checker.failures == []
    assert mac.pressed == [check_app.QUIT_BUTTON] and mac.closed == []


def test_the_places_check_quits_a_system_settings_it_finds_running_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # One someone had open is quit before the first page, so that the window read is the page's;
    # the check says so first.
    mac = _SettingsOnAMac(running=True)
    checker = _places_on(mac, monkeypatch, tmp_path)
    assert checker.failures == []
    assert mac.ran[:2] == [
        ["pkill", "-TERM", "-x", check_app.SYSTEM_SETTINGS],
        ["/usr/bin/open", check_app.DATE_AND_TIME_PANE],
    ]
    said = capsys.readouterr().out
    assert "was running before the places check, which quits it now" in said, said
    assert said.index("was running before") < said.index("System Settings windows")


@pytest.mark.parametrize("record_only", [False, True])
def test_an_open_that_does_not_finish_is_the_places_checks_to_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    record_only: bool,
) -> None:
    # /usr/bin/open is given OPEN_LIMIT seconds: one that hangs is reported, on macos-15 as a
    # failure, and the check goes on to the next page, rather than outliving its step.
    mac = _SettingsOnAMac(hangs=True)
    checker = _places_on(mac, monkeypatch, tmp_path, record_only=record_only)
    opens = [
        limit for argv, limit in zip(mac.ran, mac.limits, strict=True) if argv[0] == "/usr/bin/open"
    ]
    assert opens == [check_app.OPEN_LIMIT] * len(check_app.SETTINGS_PANES)
    assert f"/usr/bin/open did not finish within {check_app.OPEN_LIMIT} s" in (
        capsys.readouterr().out
    )
    if record_only:
        assert checker.failures == []
    else:
        assert len(checker.failures) == 2, checker.failures
        assert all("starts System Settings" in failure for failure in checker.failures)


@pytest.mark.parametrize("require_driven", [True, False])
def test_a_finder_window_under_another_name_is_closed_too(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    require_driven: bool,
) -> None:
    # The window's name is what this step is to find out: whatever the Finder calls it, the
    # window the press opened is closed by the name just read, and the check says so, and a
    # window open before is left; only on macos-15 does the name gate.
    mac = _PlacementNoteOnAMac(opens_as="Programme", before=("Downloads",))
    checker = _refusing_copy_on(
        mac, monkeypatch, tmp_path, shows_folder=True, require_driven=require_driven
    )
    assert mac.closed == [(check_app.FINDER, "Programme")]
    assert mac.finder == ["Downloads"]
    assert any("Finder window" in failure for failure in checker.failures) is require_driven
    assert "closing the Finder's window 'Programme', which the press opened" in (
        capsys.readouterr().out
    )


def test_a_finder_window_open_before_proves_nothing_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mac = _PlacementNoteOnAMac(before=("Applications",), opens=False)
    _refusing_copy_on(mac, monkeypatch, tmp_path, shows_folder=True, require_driven=True)
    assert "was open before the press, so its name proves nothing here" in (capsys.readouterr().out)
    assert mac.closed == [] and mac.finder == ["Applications"]


def test_when_show_applications_folder_does_not_quit_the_failure_shows_the_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The app's own words say why: the note it shows again names what went wrong.
    mac = _PlacementNoteOnAMac(quits=False)
    checker = _refusing_copy_on(mac, monkeypatch, tmp_path, shows_folder=True, require_driven=True)
    what = f"{check_app.SHOW_APPLICATIONS_FOLDER} quits it"
    assert what in checker.failures
    detail = capsys.readouterr().out.split(f"FAIL: {what}\n", 1)[1].splitlines()[0]
    assert check_app.PLACEMENT_ELSEWHERE in detail, detail


def test_a_renamed_copy_whose_note_lacks_quit_presses_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    words = (check_app.RENAMED_TITLE, check_app.RENAMED_ALONE)
    mac = _PlacementNoteOnAMac(buttons=[], words=words)
    checker = _refusing_copy_on(mac, monkeypatch, tmp_path, "renamed-alone")
    assert mac.pressed == []
    assert any("its note offers" in failure for failure in checker.failures), checker.failures


def test_the_helpers_for_other_applications_send_no_key_and_click_only_what_they_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Run here, as the stand-ins above do not: each helper's AppleScript tells the process it
    # names, never the app; the reads click nothing; the press clicks the button it names, last;
    # the close clicks that window's close button; and none of them sends a key, which goes to
    # whichever window has the focus.
    sent: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        sent.append([str(arg) for arg in argv])
        return subprocess.CompletedProcess(argv, 0, "Date & Time\nWi-Fi\n", "")

    monkeypatch.setattr("subprocess.run", run)
    prompt = check_app.TCC_PROMPT_PROCESS
    assert check_app._process_windows(check_app.SYSTEM_SETTINGS) == (
        True,
        ["Date & Time", "Wi-Fi"],
    )
    assert check_app._process_buttons(prompt) == (True, ["Date & Time", "Wi-Fi"])
    assert check_app._process_texts(prompt) == (True, ["Date & Time", "Wi-Fi"])
    assert check_app._windows_until(check_app.SYSTEM_SETTINGS, "Date & Time", 0) == (
        True,
        ["Date & Time", "Wi-Fi"],
    )
    check_app._press_in(prompt, "Don’t Allow")
    check_app._close_window_of(check_app.FINDER, check_app.APPLICATIONS_WINDOW)
    assert all(argv[:2] == ["osascript", "-e"] and len(argv) == 3 for argv in sent), sent
    scripts = [argv[2] for argv in sent]
    told = [check_app.SYSTEM_SETTINGS, prompt, prompt, check_app.SYSTEM_SETTINGS, prompt]
    for process, script in zip([*told, check_app.FINDER], scripts, strict=True):
        assert f'tell process "{process}"' in script, script
        assert f'tell process "{check_app.APP_NAME}"' not in script, script
    assert not _keys_sent(scripts), scripts
    for script in scripts[:4]:
        assert "click" not in script, script
    press, close = scripts[4], scripts[5]
    assert press.count("click") == 1 and 'button "Don’t Allow"' in press, press
    assert press.endswith('click theTarget\nreturn "pressed"\nend tell\nend tell'), press
    assert close.count("click") == 1, close
    assert 'click (first button of window "Applications" whose subrole is "AXCloseButton")' in close
    assert "value of every static text" in scripts[2] and "name of every button" in scripts[1]

    # What System Events says when it cannot read is not taken for a window's name.
    def fails(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "not allowed assistive access")

    monkeypatch.setattr("subprocess.run", fails)
    assert check_app._process_windows(check_app.FINDER) == (
        False,
        ["(System Events: not allowed assistive access)"],
    )
    assert check_app._process_buttons(prompt) == (False, [])
    assert check_app._windows_until(check_app.FINDER, "Applications", 0)[0] is False


# --- check_app.py ui: macOS's prompt for a folder in Documents, best effort --------------------


# The host's roots add on the app's profile, as the app starts it (App/HostCLI.swift and
# HostText.addFolderArguments): what the stand-in below runs while the app checks a folder.
_ADDING_A_FOLDER = f"lucille_host --profile {build_app.APP_PROFILE} --json roots add"


class _DocumentsOnAMac:
    """The ui check's last step as a Mac might answer it, made up. Choosing the folder in
    Documents opens the name dialog; its Add Folder runs `roots add`. With ``prompt``, macOS asks
    first, in a window of UserNotificationCenter, with ``prompt_buttons`` (by default Don’t Allow
    and OK) and ``question`` (by default words that name the app), while `roots add` waits behind
    the app's window that checks the folder, whose one button is Cancel. With ``answer_works``,
    Don’t Allow makes the host refuse, and the app's note says macOS is blocking it, with
    ``note_buttons`` (by default Open Privacy Settings and OK); without it, the press does nothing.
    With ``cancel_works``, the app's Cancel ends `roots add`; without it, only a signal does. With
    no prompt, the folder is added and the app's note says so, with OK. Open Privacy Settings
    opens System Settings on ``settings_title``; with ``settings_before``, System Settings runs
    from the start. The host has ``other_roots`` from the start too. With ``choose_raises``,
    choosing the folder raises, and with ``roots_raises``, reading the host's folders does. Add a
    Folder… opens the Open panel unless ``panel_opens`` is false. The question shows only from the
    read after ``prompt_after`` reads of its buttons, in ``prompt_windows`` windows; with
    ``term_ends`` false, only SIGKILL ends `roots add`. With ``blocks`` naming "ask" or "roots",
    choosing Add a Folder… or reading the host's folders waits, up to 10 s, for the check to stop
    it. Every press (in the app's front window, or wherever it is found), argv run (with its time
    limit), read of the app's buttons and System Events body is kept. Its words are read only at
    run time."""

    DONT_ALLOW = "Don’t Allow"

    def __init__(
        self,
        *,
        prompt: bool = False,
        prompt_buttons: list[str] | None = None,
        question: list[str] | None = None,
        answer_works: bool = True,
        cancel_works: bool = True,
        note_buttons: list[str] | None = None,
        settings_title: str = "Files & Folders",
        settings_before: bool = False,
        other_roots: dict[str, str] | None = None,
        choose_raises: bool = False,
        roots_raises: bool = False,
        panel_opens: bool = True,
        prompt_after: int = 0,
        prompt_windows: int = 1,
        term_ends: bool = True,
        blocks: str = "",
    ) -> None:
        self.prompt = prompt
        self.panel_opens = panel_opens
        self.prompt_after = prompt_after
        self.prompt_windows = prompt_windows
        self.term_ends = term_ends
        self.blocks = blocks
        self.panel = False
        self.went = 0
        self.blind = 0
        self.prompt_reads = 0
        self.button_reads = 0
        self.front: list[str] = []
        self.anywhere: list[str] = []
        self.prompt_buttons = prompt_buttons or [self.DONT_ALLOW, "OK"]
        self.question = question or [
            f"“{check_app.APP_NAME}” would like to access files in your Documents folder."
        ]
        self.answer_works = answer_works
        self.cancel_works = cancel_works
        self.note_buttons = note_buttons
        self.settings_title = settings_title
        self.choose_raises = choose_raises
        self.roots_raises = roots_raises
        self.folder: Path | None = None
        self.dialog = False
        self.asking = False
        self.adding = False
        self.note: str | None = None
        self.settings: str | None = "General" if settings_before else None
        self.roots: dict[str, str] = dict(other_roots or {})
        self.pressed: list[str] = []
        self.prompt_pressed: list[str] = []
        self.ran: list[list[str]] = []
        self.limits: dict[str, float | None] = {}
        self.removed: list[str] = []
        self.sent: list[str] = []
        self.shots: list[str] = []
        self.chosen = 0

    def alias(self) -> str:
        assert self.folder is not None
        return self.folder.name

    def blocked_buttons(self) -> list[str]:
        if self.note_buttons is not None:
            return self.note_buttons
        return [check_app.OPEN_PRIVACY_SETTINGS, "OK"]

    @staticmethod
    def _block() -> None:
        # Busy, as a System Events read that does not answer is, until the check stops it.
        end = time.monotonic() + 10
        while time.monotonic() < end:
            pass
        raise AssertionError("the check did not stop a step that outlived its time")

    def ask(self) -> tuple[bool, str]:
        self.chosen += 1
        if self.blocks == "ask":
            self._block()
        if self.choose_raises:
            raise check_app.CheckError("System Events stopped answering")
        self.panel = self.panel_opens
        return True, ""

    def choose_folder(self, folder: Path, screens: Path, shot: str) -> tuple[bool, str]:
        # The folder steps' helper, which types the folder's path whether or not the Open panel
        # came: kept, so that a step still using it shows here.
        self.ask()
        if not self.panel:
            self.blind += 1
            return False, "typed into no Open panel"
        return self.go_to_folder(folder, screens, shot)

    def panel_buttons(self) -> tuple[bool, list[str]]:
        return True, [check_app.OPEN_PROMPT, "Cancel"] if self.panel else []

    def go_to_folder(self, folder: Path, screens: Path, shot: str) -> tuple[bool, str]:
        self.went += 1
        assert self.panel, "keys sent to an Open panel the check had not read"
        assert folder.is_dir(), folder
        self.panel = False
        self.folder = folder
        self.dialog = True
        return True, "clicked"

    def name_dialog(self) -> tuple[bool, str]:
        return (True, self.alias()) if self.dialog else (False, "no name dialog")

    def texts(self) -> list[str]:
        if self.note == "added":
            return ["Folder added", "Once this Mac is connected to Lucille, Lucille can use it."]
        if self.note == "blocked":
            return [f"{check_app.APP_NAME} could not add the folder.", check_app.BLOCKED_BY_MACOS]
        if self.adding:
            return [f"Checking the folder “{self.alias()}”…"]
        return []

    def says(self, words: str) -> bool:
        return any(words in text for text in self.texts())

    def window_list(self, what: str) -> tuple[bool, list[str]]:
        if what == "value of every static text":
            return True, self.texts()
        assert what == "name of every button", what
        self.button_reads += 1
        if self.dialog:
            return True, ["Add Folder", "Cancel"]
        if self.note == "blocked":
            return True, self.blocked_buttons()
        if self.note == "added":
            return True, ["OK"]
        return True, ["Cancel"] if self.adding else []

    def press(self, name: str) -> tuple[bool, str]:
        # Wherever the button is found: not necessarily the window whose buttons were read.
        self.anywhere.append(name)
        return self._press(name)

    def press_front(self, name: str) -> tuple[bool, str]:
        self.front.append(name)
        return self._press(name)

    def _press(self, name: str) -> tuple[bool, str]:
        self.pressed.append(name)
        if self.dialog and name == "Add Folder":
            self.dialog = False
            if self.prompt:
                self.asking = True
                self.adding = True
            else:
                self.roots[self.alias()] = str(self.folder)
                self.note = "added"
            return True, "pressed"
        if self.adding and self.note is None and name == "Cancel":
            self.adding = not self.cancel_works
            return True, "pressed"
        if self.note == "added" and name == "OK":
            self.note = None
            return True, "pressed"
        if self.note == "blocked" and name in self.blocked_buttons():
            self.note = None
            if name == check_app.OPEN_PRIVACY_SETTINGS:
                self.settings = self.settings_title
            return True, "pressed"
        return False, f"no button named {name}"

    def process_buttons(self, process: str) -> tuple[bool, list[str]]:
        if process == check_app.TCC_PROMPT_PROCESS and self.asking:
            self.prompt_reads += 1
            if self.prompt_reads > self.prompt_after:
                return True, list(self.prompt_buttons)
        return True, []

    def window_count(self, process: str) -> int | None:
        if process == check_app.TCC_PROMPT_PROCESS and self.asking:
            self.prompt_reads += 1
            return self.prompt_windows if self.prompt_reads > self.prompt_after else 0
        return 0

    def process_texts(self, process: str) -> tuple[bool, list[str]]:
        if process == check_app.TCC_PROMPT_PROCESS and self.asking:
            return True, list(self.question)
        return True, []

    def press_in(self, process: str, name: str) -> tuple[bool, str]:
        self.prompt_pressed.append(name)
        if process == check_app.TCC_PROMPT_PROCESS and self.asking and name == self.DONT_ALLOW:
            if self.answer_works:
                self.asking = False
                self.adding = False
                self.note = "blocked"
            return True, "pressed"
        return False, f"no button named {name}"

    def windows(self, process: str) -> tuple[bool, list[str]]:
        if process == check_app.SYSTEM_SETTINGS and self.settings:
            return True, [self.settings]
        if process == check_app.TCC_PROMPT_PROCESS and self.asking:
            return True, [""]
        return True, []

    def names(self, name: str) -> list[str]:
        return ["777"] if name == check_app.SYSTEM_SETTINGS and self.settings else []

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(arg) for arg in argv]
        self.ran.append(argv)
        self.limits[" ".join(argv[:2])] = kwargs.get("timeout")
        out, code = "", 0
        if argv[:2] == ["pkill", "-TERM"] and argv[-2:] == ["-x", check_app.SYSTEM_SETTINGS]:
            self.settings = None
        elif argv[0] == "/bin/test" and argv[1] == "-e":
            code = 0 if Path(argv[2]).exists() else 1
        elif argv[0] == "/bin/mkdir":
            try:
                Path(argv[-1]).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return subprocess.CompletedProcess(argv, 1, "", str(exc))
        elif argv[0] == "/bin/rm":
            shutil.rmtree(argv[-1], ignore_errors=True)
        elif argv == ["pgrep", "-f", _ADDING_A_FOLDER]:
            out = "4242\n" if self.adding else ""
            code = 0 if self.adding else 1
        elif argv[0] == "pkill" and argv[-2:] == ["-f", _ADDING_A_FOLDER]:
            self.adding = self.adding and argv[1] == "-TERM" and not self.term_ends
        return subprocess.CompletedProcess(argv, code, out, "")

    def roots_of(self, python: Path) -> dict[str, str]:
        if self.blocks == "roots":
            self._block()
        if self.roots_raises:
            raise check_app.CheckError("the host did not answer")
        return dict(self.roots)

    def app_host(self, python: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["roots", "remove"]:
            self.roots.pop(args[2], None)
            self.removed.append(args[2])
        return subprocess.CompletedProcess(args, 0, "", "")

    def system_events(self, body: str, timeout: float = 30) -> tuple[bool, str]:
        self.sent.append(body)
        return False, f"not on a Mac: {body!r}"


def _documents_step_on(
    mac: _DocumentsOnAMac,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    tries: int = 1,
    deadline: float | None = None,
) -> Path:
    """Run the ui check's Documents step against ``mac``, each wait reading ``tries`` times at
    most, with ``deadline`` if one is given; return the folder it was to make."""
    folder = tmp_path / "Documents" / "lucille-host-ui-check-documents"
    monkeypatch.setattr(check_app, "_documents_check_folder", lambda: folder, raising=False)
    for name, fake in (
        ("_choose_folder", mac.choose_folder),
        ("_ask_to_add_a_folder", mac.ask),
        ("_panel_buttons", mac.panel_buttons),
        ("_go_to_folder", mac.go_to_folder),
        ("_press_in_front", mac.press_front),
        ("_process_window_count", mac.window_count),
        ("_name_dialog", mac.name_dialog),
        ("_texts", mac.texts),
        ("_says", mac.says),
        ("_window_list", mac.window_list),
        ("_press", mac.press),
        ("_process_buttons", mac.process_buttons),
        ("_process_texts", mac.process_texts),
        ("_press_in", mac.press_in),
        ("_process_windows", mac.windows),
        ("_running", mac.names),
        ("_roots", mac.roots_of),
        ("_app_host", mac.app_host),
        ("_system_events", mac.system_events),
    ):
        monkeypatch.setattr(check_app, name, fake, raising=False)
    monkeypatch.setattr("subprocess.run", mac.run)
    monkeypatch.setattr(check_app, "_screenshot", lambda path: mac.shots.append(path.name))
    monkeypatch.setattr(
        check_app, "_wait_until", lambda test, seconds: any(test() for _ in range(tries))
    )
    monkeypatch.setattr(check_app.time, "sleep", lambda seconds: None)
    if deadline is None:
        check_app._drive_documents_prompt(tmp_path / "python", tmp_path)
    else:
        check_app._drive_documents_prompt(tmp_path / "python", tmp_path, deadline)
    return folder


# How check_ui runs the Documents step: with the time left of the ui step's limit, less what its
# own end takes.
_DOCUMENTS_CALL = (
    "_drive_documents_prompt(python_in(app), screens, started + UI_STEP_LIMIT - UI_STEP_END)"
)


def _tcc_resets(mac: _DocumentsOnAMac) -> list[list[str]]:
    return [argv for argv in mac.ran if argv[:1] == ["tccutil"]]


def _ended_roots_add(mac: _DocumentsOnAMac) -> list[list[str]]:
    return [argv for argv in mac.ran if argv[0] == "pkill" and _ADDING_A_FOLDER in argv]


def test_the_documents_step_gates_nothing() -> None:
    # Runners may answer macOS's prompt for Documents before any app asks, so the step only
    # prints what it saw: it is given no checker to fail, and neither it nor its clean-up sends a
    # key.
    import inspect

    parameters = inspect.signature(check_app._drive_documents_prompt).parameters
    assert list(parameters) == ["python", "screens", "deadline"]
    source = _CHECK_APP.read_text(encoding="utf-8")
    for name in ("_drive_documents_prompt", "_tidy_documents_step", "_stop_adding_a_folder"):
        step = _python_function(source, name)
        for word in ("checker", "key code", "keystroke", "Escape"):
            assert word not in step, (name, word)
    ui = _python_function(source, "check_ui")
    assert ui.index("_drive_reset(") < ui.index(_DOCUMENTS_CALL) < ui.index("elif require_driven:")
    # What it waits on and ends is the app's own roots add, on the app's profile, as HostCLI
    # starts it (App/HostCLI.swift and HostText.addFolderArguments).
    assert check_app.ADDING_A_FOLDER == _ADDING_A_FOLDER
    assert '["--profile", profile] + verb' in _swift("HostCLI.swift")
    assert 'var verb = ["--json", "roots", "add"]' in _swift("HostText.swift")


def test_with_no_prompt_the_documents_step_says_so_and_leaves_things_as_they_were(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mac = _DocumentsOnAMac(other_roots={"work": "/Users/someone/work"})
    folder = _documents_step_on(mac, monkeypatch, tmp_path)
    assert "no macOS prompt on this runner" in capsys.readouterr().out
    # The folder the app added is removed through the command line, and no other; the app's note
    # closed by its OK, which the step read first; and the folder the step made is gone. Nothing
    # was asked of macOS's privacy settings, System Settings was not touched, and no roots add was
    # left to end.
    assert mac.pressed == ["Add Folder", "OK"] and mac.prompt_pressed == []
    assert mac.roots == {"work": "/Users/someone/work"} and mac.removed == [folder.name]
    assert not folder.exists()
    assert _tcc_resets(mac) == [] and not any(argv[0] == "pkill" for argv in mac.ran)
    assert not _keys_sent(mac.sent), mac.sent


def test_with_a_prompt_the_documents_step_refuses_it_and_opens_privacy_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mac = _DocumentsOnAMac(prompt=True)
    folder = _documents_step_on(mac, monkeypatch, tmp_path)
    said = capsys.readouterr().out
    # Don’t Allow, as macOS spells it, read in the prompt, whose words name the app and are
    # printed, and pressed there; the app's note says macOS is blocking it, and its Open Privacy
    # Settings, read first, opens Files & Folders, which the step prints; then System Settings is
    # quit by its name.
    assert mac.prompt_pressed == [_DocumentsOnAMac.DONT_ALLOW]
    assert mac.pressed == ["Add Folder", check_app.OPEN_PRIVACY_SETTINGS]
    assert f"macOS asked {mac.question}" in said and f"{['Files & Folders']}" in said, said
    assert f"{check_app.TCC_PROMPT_PROCESS} then shows the windows []" in said, said
    assert ["pkill", "-TERM", "-x", check_app.SYSTEM_SETTINGS] in mac.ran
    assert mac.settings is None
    # The answer given is taken back, for the app and its interpreter alone (which of the two
    # macOS asks for is not yet known, decisions/0002 §12), and what tccutil said is printed;
    # the folder is gone, and no roots add was left to end.
    pins = build_app.load_pins()
    assert _tcc_resets(mac) == [
        ["tccutil", "reset", "SystemPolicyDocumentsFolder", pins.bundle_id],
        ["tccutil", "reset", "SystemPolicyDocumentsFolder", pins.runtime_id],
    ]
    assert f"tccutil reset SystemPolicyDocumentsFolder {pins.runtime_id}: " in said
    assert mac.roots == {} and not folder.exists()
    assert not mac.adding and _ended_roots_add(mac) == []
    assert not _keys_sent(mac.sent), mac.sent


def test_the_documents_step_presses_dont_allow_wherever_the_prompt_lists_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mac = _DocumentsOnAMac(prompt=True, prompt_buttons=["OK", _DocumentsOnAMac.DONT_ALLOW])
    _documents_step_on(mac, monkeypatch, tmp_path)
    assert mac.prompt_pressed == [_DocumentsOnAMac.DONT_ALLOW]


@pytest.mark.parametrize("cancel_works", [True, False])
def test_a_dont_allow_that_does_nothing_leaves_no_roots_add_behind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cancel_works: bool,
) -> None:
    # The press answered nothing, so the app still checks the folder behind its window, and
    # roots add still waits on macOS, holding the profile's policy lock. The step presses the
    # app's Cancel, which it reads first; a roots add still running after that is ended by its
    # signal. Either way none is left for the checks after this one, and the answer macOS was
    # asked for is still taken back.
    mac = _DocumentsOnAMac(prompt=True, answer_works=False, cancel_works=cancel_works)
    folder = _documents_step_on(mac, monkeypatch, tmp_path)
    said = capsys.readouterr().out
    assert mac.prompt_pressed == [_DocumentsOnAMac.DONT_ALLOW]
    assert mac.pressed == ["Add Folder", "Cancel"]
    assert not mac.adding
    ended = _ended_roots_add(mac)
    assert ended == ([] if cancel_works else [["pkill", "-TERM", "-f", _ADDING_A_FOLDER]])
    assert ("roots add still runs" in said) is not cancel_works, said
    assert len(_tcc_resets(mac)) == 2 and not folder.exists() and mac.roots == {}
    assert not _keys_sent(mac.sent), mac.sent


def test_a_question_about_another_app_is_printed_and_not_answered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The step answers only a question it has read to be about the app or its interpreter; it
    # still leaves no roots add behind, and still takes back whatever macOS was asked.
    question = ["“Another App” would like to access files in your Documents folder."]
    mac = _DocumentsOnAMac(prompt=True, question=question)
    _documents_step_on(mac, monkeypatch, tmp_path)
    said = capsys.readouterr().out
    assert f"macOS asked {question}" in said and "names neither the app" in said, said
    assert mac.prompt_pressed == [] and mac.pressed == ["Add Folder", "Cancel"]
    assert not mac.adding and len(_tcc_resets(mac)) == 2


def test_a_question_naming_the_interpreter_is_about_the_app() -> None:
    pins = build_app.load_pins()
    for words in (
        f"“{check_app.APP_NAME}” would like to access files in your Documents folder.",
        "“lucille-host-runtime” would like to access files in your Documents folder.",
        f"{pins.runtime_id} would like to access files in your Documents folder.",
    ):
        assert check_app._names_the_app([words]), words
    assert not check_app._names_the_app(["“Another App” would like to access files."])
    assert not check_app._names_the_app([])


def test_the_documents_step_leaves_a_system_settings_it_did_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mac = _DocumentsOnAMac(prompt=True, settings_before=True)
    _documents_step_on(mac, monkeypatch, tmp_path)
    assert mac.pressed == ["Add Folder", check_app.OPEN_PRIVACY_SETTINGS]
    assert not any(argv[:1] == ["pkill"] for argv in mac.ran), mac.ran
    assert "ran before the Documents step, which leaves it" in capsys.readouterr().out


def test_a_note_without_open_privacy_settings_is_printed_and_closed_by_its_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mac = _DocumentsOnAMac(prompt=True, note_buttons=["OK"])
    folder = _documents_step_on(mac, monkeypatch, tmp_path)
    assert check_app.OPEN_PRIVACY_SETTINGS in capsys.readouterr().out
    assert mac.pressed == ["Add Folder", "OK"]
    assert mac.settings is None and not any(argv[0] == "pkill" for argv in mac.ran)
    assert len(_tcc_resets(mac)) == 2 and not folder.exists()


def test_the_documents_step_never_raises_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mac = _DocumentsOnAMac(choose_raises=True)
    folder = _documents_step_on(mac, monkeypatch, tmp_path)
    assert "System Events stopped answering" in capsys.readouterr().out
    assert not folder.exists() and _tcc_resets(mac) == []


def test_each_part_of_the_documents_steps_clean_up_runs_on_its_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Reading the host's folders fails: the folder is still removed, and macOS's answer still
    # taken back, and what stopped is printed.
    mac = _DocumentsOnAMac(prompt=True, roots_raises=True)
    folder = _documents_step_on(mac, monkeypatch, tmp_path)
    said = capsys.readouterr().out
    assert "could not remove the folder's alias: the host did not answer" in said, said
    assert not folder.exists() and len(_tcc_resets(mac)) == 2


def test_the_documents_step_acts_on_documents_in_a_child_with_a_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The check's own process never touches Documents: a question about it there would go
    # unanswered. Each test, mkdir and rm is a child given DOCUMENTS_FILE_LIMIT seconds.
    mac = _DocumentsOnAMac(prompt=True)
    _documents_step_on(mac, monkeypatch, tmp_path)
    for argv in ("/bin/test -e", "/bin/mkdir -p", "/bin/rm -rf"):
        assert mac.limits[argv] == check_app.DOCUMENTS_FILE_LIMIT, argv
    source = _python_function(_CHECK_APP.read_text(encoding="utf-8"), "_drive_documents_prompt")
    for word in (".exists()", ".mkdir(", "rmtree"):
        assert word not in source, word
    # A child that does not finish in time: the step says so, and adds nothing.

    def hangs(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 0)

    mac = _DocumentsOnAMac()
    monkeypatch.setattr(mac, "run", hangs)
    _documents_step_on(mac, monkeypatch, tmp_path)
    assert mac.chosen == 0
    assert "could not be looked for" in capsys.readouterr().out


@pytest.mark.parametrize("prompt", [False, True])
def test_the_documents_step_removes_its_own_folder_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prompt: bool
) -> None:
    # The ui check runs on the real HOME: whatever else is in Documents is someone's, and stays.
    documents = tmp_path / "Documents"
    documents.mkdir()
    (documents / "someone.txt").write_text("kept", encoding="utf-8")
    mac = _DocumentsOnAMac(prompt=prompt)
    folder = _documents_step_on(mac, monkeypatch, tmp_path)
    assert (documents / "someone.txt").read_text(encoding="utf-8") == "kept"
    assert not folder.exists()
    assert [argv for argv in mac.ran if argv[0] == "/bin/rm"] == [["/bin/rm", "-rf", str(folder)]]


def test_the_documents_steps_folder_is_its_own() -> None:
    assert check_app._documents_check_folder() == (
        Path.home() / "Documents" / "lucille-host-ui-check-documents"
    )


def test_the_documents_step_types_only_into_an_open_panel_it_has_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # It sends the folder's path as keys, which go to whatever has the focus: only once it has
    # read the Open panel's own button. No panel, no keys, and nothing added.
    mac = _DocumentsOnAMac(panel_opens=False)
    folder = _documents_step_on(mac, monkeypatch, tmp_path)
    assert mac.blind == 0 and mac.went == 0 and mac.pressed == []
    assert f"no Open panel with {check_app.OPEN_PROMPT!r} came" in capsys.readouterr().out
    assert not folder.exists() and mac.roots == {}
    mac = _DocumentsOnAMac()
    _documents_step_on(mac, monkeypatch, tmp_path)
    assert (mac.chosen, mac.went, mac.blind) == (1, 1, 0)
    step = _python_function(_CHECK_APP.read_text(encoding="utf-8"), "_drive_documents_prompt")
    assert "_choose_folder(" not in step
    assert (
        step.index("_ask_to_add_a_folder()")
        < step.index("OPEN_PROMPT in _panel_buttons()[1]")
        < step.index("_go_to_folder(")
    )


def test_dont_allow_is_pressed_only_when_the_question_is_the_one_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The question's words and buttons are read from every window of the process that draws it,
    # and the press finds its button in any of them: with two up, it could answer the other one.
    mac = _DocumentsOnAMac(prompt=True, prompt_windows=2)
    folder = _documents_step_on(mac, monkeypatch, tmp_path)
    said = capsys.readouterr().out
    assert mac.prompt_pressed == []
    assert f"{check_app.TCC_PROMPT_PROCESS} shows 2 windows, not one, so nothing was pressed" in (
        said
    ), said
    assert not mac.adding and len(_tcc_resets(mac)) == 2 and not folder.exists()


def test_the_documents_step_presses_each_button_in_the_window_it_read_it_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The app's buttons are read from its front window, and pressed there, never wherever a
    # button by that name is first found.
    for mac in (
        _DocumentsOnAMac(),
        _DocumentsOnAMac(prompt=True),
        _DocumentsOnAMac(prompt=True, answer_works=False),
        _DocumentsOnAMac(prompt=True, note_buttons=["OK"]),
    ):
        _documents_step_on(mac, monkeypatch, tmp_path)
        assert mac.anywhere == [] and mac.front == mac.pressed != [], (mac.front, mac.anywhere)


def test_a_question_that_comes_late_is_said_to_be_left_on_screen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # macOS asks after the step stopped waiting: nothing answers it here, and nothing closes it,
    # so the step says it is left on screen, and still takes back whatever it is answered.
    mac = _DocumentsOnAMac(prompt=True, prompt_after=1)
    folder = _documents_step_on(mac, monkeypatch, tmp_path)
    said = capsys.readouterr().out
    assert "no macOS prompt on this runner" in said
    assert "note: a question of macOS's is left on screen" in said, said
    assert mac.prompt_pressed == [] and not mac.adding
    assert len(_tcc_resets(mac)) == 2 and not folder.exists()


def test_a_question_that_comes_on_a_later_read_is_answered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mac = _DocumentsOnAMac(prompt=True, prompt_after=1)
    _documents_step_on(mac, monkeypatch, tmp_path, tries=2)
    assert mac.prompt_pressed == [_DocumentsOnAMac.DONT_ALLOW]
    assert mac.pressed == ["Add Folder", check_app.OPEN_PRIVACY_SETTINGS]


def test_a_roots_add_that_outlives_sigterm_is_killed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # One left waiting on macOS would hold the profile's policy lock for every later check.
    mac = _DocumentsOnAMac(prompt=True, answer_works=False, cancel_works=False, term_ends=False)
    _documents_step_on(mac, monkeypatch, tmp_path)
    assert _ended_roots_add(mac) == [
        ["pkill", "-TERM", "-f", _ADDING_A_FOLDER],
        ["pkill", "-KILL", "-f", _ADDING_A_FOLDER],
    ]
    assert not mac.adding


def test_once_its_note_is_closed_the_clean_up_reads_no_button(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The name dialog's buttons and the note's, before its OK: the app has nothing more to cancel.
    mac = _DocumentsOnAMac()
    _documents_step_on(mac, monkeypatch, tmp_path)
    assert mac.pressed == ["Add Folder", "OK"]
    assert mac.button_reads == 2


def test_with_too_little_of_the_ui_steps_time_left_the_documents_step_adds_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mac = _DocumentsOnAMac(prompt=True)
    least = check_app.DOCUMENTS_TIDY_TIME + check_app.DOCUMENTS_LEAST_TIME
    folder = _documents_step_on(mac, monkeypatch, tmp_path, deadline=time.monotonic() + least - 5)
    assert mac.ran == [] and mac.chosen == 0 and not folder.exists()
    assert "too little of the ui step's time is left for the Documents step" in (
        capsys.readouterr().out
    )


def test_the_documents_step_is_stopped_at_its_time_and_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Whatever it waits on, it ends DOCUMENTS_TIDY_TIME before the deadline, and its clean-up
    # runs; the timer and the handler it used are put back.
    monkeypatch.setattr(check_app, "DOCUMENTS_LEAST_TIME", 0.1)
    handler = signal.getsignal(signal.SIGALRM)
    mac = _DocumentsOnAMac(blocks="ask")
    started = time.monotonic()
    deadline = started + check_app.DOCUMENTS_TIDY_TIME + 0.5
    folder = _documents_step_on(mac, monkeypatch, tmp_path, deadline=deadline)
    assert time.monotonic() - started < 5
    said = capsys.readouterr().out
    assert "note: the Documents step stopped: it reached the end of the time it was given" in (
        said
    ), said
    assert not folder.exists() and ["/bin/rm", "-rf", str(folder)] in mac.ran
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) == handler


def test_the_documents_steps_clean_up_ends_by_the_deadline_and_says_what_it_left(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(check_app, "DOCUMENTS_TIDY_TIME", 0.5)
    monkeypatch.setattr(check_app, "DOCUMENTS_LEAST_TIME", 0.1)
    handler = signal.getsignal(signal.SIGALRM)
    mac = _DocumentsOnAMac(prompt=True, blocks="roots")
    started = time.monotonic()
    folder = _documents_step_on(mac, monkeypatch, tmp_path, deadline=started + 1.0)
    assert time.monotonic() - started < 5
    said = capsys.readouterr().out
    assert (
        "could not remove the folder's alias: it reached the end of the time it was given" in said
    ), said
    for left in ("remove the folder", "take macOS's answer back"):
        assert f"the Documents step's clean-up had no time left to {left}" in said, said
    assert _tcc_resets(mac) == [] and folder.exists()
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) == handler


def test_the_ui_check_gives_the_documents_step_the_rest_of_its_time() -> None:
    ui = _python_function(_CHECK_APP.read_text(encoding="utf-8"), "check_ui")
    assert ui.count("started = time.monotonic()") == 1
    assert ui.index("started = time.monotonic()") < ui.index("_stop_earlier_copies(")
    assert ui.count(_DOCUMENTS_CALL) == 1
    assert 0 < check_app.UI_STEP_END < check_app.DOCUMENTS_LEAST_TIME


def test_the_documents_steps_own_helpers_click_only_what_they_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Run here, as the stand-in above does not: the count reads, the press clicks the button it
    # names in the app's front window, the menu choice clicks Add a Folder…, and none sends a key.
    sent: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        sent.append([str(arg) for arg in argv])
        return subprocess.CompletedProcess(argv, 0, "2\n", "")

    monkeypatch.setattr("subprocess.run", run)
    prompt = check_app.TCC_PROMPT_PROCESS
    assert check_app._process_window_count(prompt) == 2
    check_app._press_in_front("Cancel")
    check_app._ask_to_add_a_folder()
    count, press, ask = (argv[2] for argv in sent)
    assert f'tell process "{prompt}"' in count and "count of windows" in count, count
    assert "click" not in count
    assert f'tell process "{check_app.APP_NAME}"' in press, press
    assert press.count("click") == 1 and 'click button "Cancel" of window 1' in press, press
    assert f'tell process "{check_app.APP_NAME}"' in ask, ask
    assert 'whose name starts with "Add a Folder"' in ask, ask
    assert not _keys_sent([count, press, ask])

    # What System Events says when it cannot count is not a count.
    def fails(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "not allowed assistive access")

    monkeypatch.setattr("subprocess.run", fails)
    assert check_app._process_window_count(prompt) is None


def test_the_documents_step_makes_its_own_folder_or_does_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A folder of that name already there is someone's: the step leaves it, and chooses nothing.
    folder = tmp_path / "Documents" / "lucille-host-ui-check-documents"
    folder.mkdir(parents=True)
    (folder / "kept").write_text("x", encoding="utf-8")
    mac = _DocumentsOnAMac()
    assert _documents_step_on(mac, monkeypatch, tmp_path) == folder
    assert mac.chosen == 0 and (folder / "kept").is_file()
    # And where the step cannot make one, it says so and chooses nothing.
    shutil.rmtree(folder.parent)
    folder.parent.write_text("not a folder", encoding="utf-8")
    mac = _DocumentsOnAMac()
    _documents_step_on(mac, monkeypatch, tmp_path)
    assert mac.chosen == 0
    assert "could not make" in capsys.readouterr().out
