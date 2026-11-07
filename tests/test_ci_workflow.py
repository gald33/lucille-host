"""ci.yml's macOS jobs, and the parts of tools/macos/check_app.py they run that work anywhere.

The jobs: ``macos-app`` builds the app on macos-15, signs it ad hoc and runs every check;
``macos-smoke`` runs the same bytes on macOS 26 and 27. These tests read ci.yml: no signing
secret anywhere, nothing uploaded but logs and UI snapshots except the ad-hoc app on a manual
run, the order of the checks, how the disk image reaches the smoke jobs, and that each
tools/macos command a step runs gets past the script's own argument parsing. The checks
themselves run only on a Mac; their parsers and plans are tested here, on any system.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from lucille_host import paths
from tools.macos import build_app, check_app, payload

CI = build_app.REPO_ROOT / ".github" / "workflows" / "ci.yml"
PINS = build_app.load_pins()
ALLOWED_SECRET = "${{ secrets.LUCILLE_HOST_PRIVATE_STRINGS }}"
APP_UPLOAD_IF = "${{ success() && github.event_name == 'workflow_dispatch' }}"
LOGS_UPLOAD_IF = "${{ !cancelled() }}"
LOG_PATH = re.compile(r"build/macos/check/(?:screens/)?\*\.(?:png|txt|json)")
HANDOFF = "build/macos/handoff"
# The actions ci.yml may use, each at one commit.
PINNED_ACTIONS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "astral-sh/setup-uv": "1c37ad07a6a961277cf70c0d37d6f313000f5884",
    "actions/upload-artifact": "cf430e030ddbb5b0abf93d22962f4752f3646cd9",
    "actions/cache/save": "caa296126883cff596d87d8935842f9db880ef25",
    "actions/cache/restore": "caa296126883cff596d87d8935842f9db880ef25",
}
# Words of a signing or notarization step, or of an Apple credential, in any case.
SIGNING_WORDS = (
    "notarytool",
    "stapler",
    "security import",
    "security create-keychain",
    "--keychain",
    ".p12",
    "developer id",
    "app store connect",
    "apple_",
    "ac_password",
    "altool",
)


def _ci() -> dict[Any, Any]:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def _on(doc: dict[Any, Any]) -> Any:
    # YAML 1.1 reads the key ``on`` as the boolean True.
    return doc["on"] if "on" in doc else doc.get(True)


def _walk(value: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[tuple[Any, ...], str]]:
    """Every string in a parsed YAML document, keys included, with where it is."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield (*path, key), key
            yield from _walk(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, (*path, index))


def signing_secret_problems(doc: dict[Any, Any]) -> list[str]:
    """Why ``doc`` might reach a signing secret, sign with anything but an ad-hoc signature, or
    hand secrets to code it did not review. Empty when none of that is possible."""
    problems = []
    uses = []
    for path, text in _walk(doc):
        if re.search(r"(?i)secrets", text):
            uses.append((path, text))
        low = text.lower()
        problems += [f"{path}: {word!r}" for word in SIGNING_WORDS if word in low]
        for signed in re.findall(r"codesign\b[^\n]*?\s(?:--sign|-s)\s+(\S+)", text):
            if signed != "-":
                problems.append(f"{path}: codesign signs with {signed}, not ad hoc")
    allowed = [
        (path, text)
        for path, text in uses
        if text == ALLOWED_SECRET
        and path[:2] == ("jobs", "guards")
        and path[-2:] == ("env", "LUCILLE_HOST_PRIVATE_STRINGS")
    ]
    if uses != allowed or len(allowed) != 1:
        problems.append(f"secrets are named at {[path for path, _ in uses]}")
    on = _on(doc)
    triggers = set(on) if isinstance(on, dict) else set(on if isinstance(on, list) else [on])
    problems += [f"trigger {name}" for name in {"pull_request_target", "workflow_run"} & triggers]
    if doc.get("permissions") != {"contents": "read"}:
        problems.append(f"top-level permissions are {doc.get('permissions')!r}")
    for job_id, job in doc.get("jobs", {}).items():
        problems += [f"{job_id} has {key}" for key in ("permissions", "environment") if key in job]
    return problems


def _steps(doc: dict[Any, Any], job: str) -> list[dict[str, Any]]:
    return doc["jobs"][job]["steps"]


def _paths(step: dict[str, Any]) -> list[str]:
    return [line.strip() for line in str(step["with"]["path"]).splitlines() if line.strip()]


def upload_problems(doc: dict[Any, Any]) -> list[str]:
    """Why an upload in ``doc`` might carry more than logs and UI snapshots, except the one
    upload of the ad-hoc app, on a manual run only, kept three days."""
    problems = []
    apps = []
    for job_id, job in doc["jobs"].items():
        for step in job.get("steps", []):
            if not str(step.get("uses", "")).startswith("actions/upload-artifact@"):
                continue
            paths = _paths(step)
            if any(re.search(r"\.dmg|\.app|\.tar|\.zip|\.whl|selftest|handoff", p) for p in paths):
                apps.append((job_id, step, paths))
                continue
            if step.get("if") != LOGS_UPLOAD_IF:
                problems.append(f"{job_id}: the logs upload runs if {step.get('if')!r}")
            if step["with"].get("retention-days") != 7:
                problems.append(f"{job_id}: logs kept {step['with'].get('retention-days')} days")
            for path in paths:
                if LOG_PATH.fullmatch(path) is None or "library" in path:
                    problems.append(f"{job_id}: uploads {path}")
    if len(apps) != 1:
        problems.append(f"{len(apps)} uploads of the app")
        return problems
    job_id, step, paths = apps[0]
    if job_id != "macos-app" or paths != [f"build/macos/*{build_app.DMG_SUFFIX}"]:
        problems.append(f"{job_id} uploads {paths}")
    if step.get("if") != APP_UPLOAD_IF:
        problems.append(f"the app is uploaded if {step.get('if')!r}")
    if step["with"].get("retention-days") != 3:
        problems.append(f"the app is kept {step['with'].get('retention-days')} days")
    if step["with"].get("if-no-files-found") != "error":
        problems.append("a manual run without the app passes")
    return problems


def _runs(doc: dict[Any, Any], job: str) -> list[str]:
    return [str(step["run"]) for step in _steps(doc, job) if "run" in step]


def _checks(doc: dict[Any, Any], job: str) -> list[str]:
    """The check_app.py checks a job runs, in order."""
    found = []
    for run in _runs(doc, job):
        found += re.findall(r"tools/macos/check_app\.py ([a-z-]+)", run)
    return found


# ---------------------------------------------------------------------------------------------
# ci.yml


def test_ci_references_no_signing_secret() -> None:
    doc = _ci()
    assert {"macos-app", "macos-smoke"} <= set(doc["jobs"])
    assert signing_secret_problems(doc) == []
    for job in ("macos-app", "macos-smoke"):
        text = yaml.safe_dump(doc["jobs"][job])
        assert "secrets" not in text.lower()


def _add_step_env(doc: dict[Any, Any], value: str) -> None:
    _steps(doc, "macos-app")[0].setdefault("env", {})["X"] = value


def _add_run(doc: dict[Any, Any], run: str) -> None:
    _steps(doc, "macos-app").append({"run": run})


def _set_trigger(name: str) -> Callable[[dict[Any, Any]], None]:
    def change(doc: dict[Any, Any]) -> None:
        _on(doc)[name] = {"types": ["completed"]}

    return change


@pytest.mark.parametrize(
    "change",
    [
        lambda doc: _add_step_env(doc, "${{ secrets.DEVELOPER_ID_P12 }}"),
        lambda doc: _add_step_env(doc, "${{ secrets[format('{0}', 'KEY')] }}"),
        lambda doc: _add_step_env(doc, "${{ toJSON(secrets) }}"),
        lambda doc: doc["jobs"]["macos-app"].update({"secrets": "inherit"}),
        lambda doc: doc["jobs"]["macos-smoke"].update({"environment": "macos-release"}),
        lambda doc: doc["jobs"]["macos-app"].update({"permissions": {"contents": "write"}}),
        lambda doc: doc.update({"permissions": "write-all"}),
        lambda doc: doc.update({"permissions": {"contents": "read", "actions": "write"}}),
        _set_trigger("pull_request_target"),
        _set_trigger("workflow_run"),
        lambda doc: _add_run(doc, "xcrun notarytool submit build/macos/x.dmg"),
        lambda doc: _add_run(doc, "xcrun stapler staple build/macos/x.dmg"),
        lambda doc: _add_run(doc, 'codesign --sign "Developer ID Application: X" x.app'),
        lambda doc: _add_run(doc, "codesign --force -s ABCDEF0123 x.app"),
        lambda doc: _add_run(doc, "security import cert.p12 -k build.keychain"),
        lambda doc: _add_step_env(doc, "$APPLE_ID"),
        lambda doc: doc["jobs"]["guards"]["steps"].append(
            {"env": {"OTHER": "${{ secrets.LUCILLE_HOST_PRIVATE_STRINGS }}"}}
        ),
    ],
)
def test_a_signing_secret_or_a_wider_grant_is_refused(
    change: Callable[[dict[Any, Any]], None],
) -> None:
    doc = copy.deepcopy(_ci())
    change(doc)
    assert signing_secret_problems(doc)


def test_ci_uploads_the_app_only_on_a_manual_run() -> None:
    doc = _ci()
    assert upload_problems(doc) == []
    on = _on(doc)
    assert "workflow_dispatch" in on
    # The disk image reaches the smoke jobs as a cache entry, never an artifact: saved by
    # macos-app under the key it outputs, checked to exist, and restored by that key alone.
    saves = [
        step
        for step in _steps(doc, "macos-app")
        if str(step.get("uses", "")).startswith("actions/cache/save@")
    ]
    assert len(saves) == 1
    assert saves[0]["with"] == {"path": HANDOFF, "key": "${{ steps.handoff.outputs.cache_key }}"}
    restores = [
        step
        for job in ("macos-app", "macos-smoke")
        for step in _steps(doc, job)
        if str(step.get("uses", "")).startswith("actions/cache/")
        and not str(step["uses"]).startswith("actions/cache/save@")
    ]
    assert [step["uses"].split("@")[0] for step in restores] == ["actions/cache/restore"] * 2
    assert restores[0]["with"] == {
        "path": HANDOFF,
        "key": "${{ steps.handoff.outputs.cache_key }}",
        "lookup-only": True,
        "fail-on-cache-miss": True,
    }
    assert restores[1]["with"] == {
        "path": HANDOFF,
        "key": "${{ needs.macos-app.outputs.cache_key }}",
        "fail-on-cache-miss": True,
    }
    assert doc["jobs"]["macos-app"]["outputs"] == {
        "built": "${{ steps.built.outputs.built }}",
        "cache_key": "${{ steps.handoff.outputs.cache_key }}",
        "dmg_sha256": "${{ steps.handoff.outputs.dmg_sha256 }}",
        "selftest_sha256": "${{ steps.handoff.outputs.selftest_sha256 }}",
    }
    # Nothing a check writes about ~/Library is under check/, the only folder uploaded.
    runs = "\n".join(_runs(doc, "macos-app") + _runs(doc, "macos-smoke"))
    for path in re.findall(r"library-(?:snapshot --to|diff --before) (\S+)", runs):
        assert path.startswith("build/macos/library/"), path


def _upload(job: str, path: str, condition: str, days: int) -> Callable[[dict[Any, Any]], None]:
    def change(doc: dict[Any, Any]) -> None:
        _steps(doc, job).append(
            {
                "if": condition,
                "uses": f"actions/upload-artifact@{PINNED_ACTIONS['actions/upload-artifact']}",
                "with": {"name": "x", "path": path, "retention-days": days},
            }
        )

    return change


@pytest.mark.parametrize(
    "change",
    [
        _upload("macos-app", "build/macos/*.dmg", LOGS_UPLOAD_IF, 3),
        _upload("macos-app", "build/macos/*.dmg", "${{ success() }}", 3),
        _upload("macos-smoke", "build/macos/*.dmg", APP_UPLOAD_IF, 3),
        _upload("macos-app", "build/macos/Capsule.app", APP_UPLOAD_IF, 3),
        _upload("macos-app", f"{HANDOFF}/selftest", LOGS_UPLOAD_IF, 7),
        _upload("macos-app", "build/macos/check/**", LOGS_UPLOAD_IF, 7),
        _upload("macos-app", "build/macos/library/before.json", LOGS_UPLOAD_IF, 7),
        _upload("macos-app", "build/macos/check/*.json", LOGS_UPLOAD_IF, 30),
        _upload("macos-smoke", "build/macos/check/*.txt", "${{ always() }}", 7),
        lambda doc: [
            step["with"].update({"retention-days": 7})
            for step in _steps(doc, "macos-app")
            if "dmg" in str(step.get("with", {}).get("path", ""))
        ],
    ],
)
def test_an_upload_beyond_logs_and_snapshots_is_refused(
    change: Callable[[dict[Any, Any]], None],
) -> None:
    doc = copy.deepcopy(_ci())
    change(doc)
    assert upload_problems(doc)


def test_ci_macos_jobs() -> None:
    doc = _ci()
    on = _on(doc)
    assert set(on) == {"push", "pull_request", "workflow_dispatch"}
    assert on["push"] == {"branches": ["main"]}
    # Every pull request builds the app: no path filter anywhere (decisions/0002 §18).
    assert not on["pull_request"]
    assert "paths" not in yaml.safe_dump(on)
    assert doc["concurrency"] == {
        "group": "ci-${{ github.event.pull_request.number || github.run_id }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }
    app = doc["jobs"]["macos-app"]
    smoke = doc["jobs"]["macos-smoke"]
    assert app["runs-on"] == "macos-15"
    # M10 holds MANIFEST.json's host wheel to the sha256 the wheel step output.
    (record,) = [step for step in app["steps"] if "check_app.py record" in step.get("run", "")]
    assert record["env"] == {"HOST_WHEEL_SHA256": "${{ steps.wheel.outputs.sha256 }}"}
    assert '--host-wheel-sha256 "$HOST_WHEEL_SHA256"' in record["run"]
    assert smoke["runs-on"] == "${{ matrix.runner }}"
    assert smoke["strategy"] == {"fail-fast": False, "matrix": {"runner": ["macos-26", "xcode-27"]}}
    assert smoke["needs"] == "macos-app"
    assert smoke["if"] == "${{ !cancelled() && needs.macos-app.outputs.built == 'true' }}"

    app_checks = _checks(doc, "macos-app")
    smoke_checks = _checks(doc, "macos-smoke")
    assert set(app_checks) | set(smoke_checks) <= set(check_app.CHECKS)
    assert set(app_checks) >= {
        "selftest",
        "handoff",
        "bundle",
        "bytecode",
        "record",
        "version",
        "links",
        "signature",
        "wrapper",
        "host",
        "environment",
        "readonly",
        "swift",
        "relocate",
        "library-snapshot",
        "install",
        "placement",
        "launch",
        "ui",
        "library-diff",
        "gatekeeper",
        "dmg",
        "startup",
        "suite",
        "probe",
        "pairing",
    }
    # The smoke jobs run the same bytes: they build nothing and repeat no check that depends only
    # on the bytes (M10, M11, the libraries, the suite), and they do not run the pairing check,
    # which needs the suite's copy of the runtime for its harness. The probe prints what the
    # verbs do on each macOS, and gates nothing. They put the app in /Applications, as macos-app
    # does, and open it only there once placement has seen every other copy refuse.
    assert smoke_checks == [
        "handoff-check",
        "unpack-dmg",
        "dmg",
        "bundle",
        "bytecode",
        "signature",
        "wrapper",
        "host",
        "probe",
        "environment",
        "readonly",
        "swift",
        "library-snapshot",
        "install",
        "placement",
        "launch",
        "ui",
        "library-diff",
        "signature",
    ]
    for job, checks in (("macos-app", app_checks), ("macos-smoke", smoke_checks)):
        runs = _runs(doc, job)
        order = [run for run in runs if "chmod" in run or "check_app.py" in run]
        made_readonly = next(i for i, run in enumerate(order) if "chmod -R a-w" in run)
        assert order[made_readonly].strip() == "chmod -R a-w build/macos/*.app"
        for name in (
            "host",
            "environment",
            "readonly",
            "swift",
            "install",
            "placement",
            "launch",
            "ui",
        ):
            first = next(i for i, run in enumerate(order) if f"check_app.py {name}" in run)
            assert first > made_readonly, (job, name)
        wrapper = next(i for i, run in enumerate(order) if "check_app.py wrapper" in run)
        assert wrapper < made_readonly
        assert checks[-1] == "signature", job
        # The bytecode check runs the bundled interpreter right after the bundle check, before
        # anything else runs it (0002 §6).
        assert checks.index("bytecode") == checks.index("bundle") + 1, job
        assert checks.index("library-snapshot") < checks.index("launch") < checks.index("ui")
        assert checks.index("ui") < checks.index("library-diff")
        # The copy every later check opens is put in /Applications from the read-only bundle,
        # once ~/Library has been walked, and placement sees every other copy refuse before
        # launch opens it (decisions/0002 §13).
        assert checks.count("install") == 1 and checks.count("placement") == 1, job
        assert (
            checks.index("library-snapshot")
            < checks.index("install")
            < checks.index("placement")
            < checks.index("launch")
        ), job
        assert not any("build_app.py" in run for run in runs if job == "macos-smoke")
    # macos-app requires the menu to be driven; on the newer images that is recorded, not required.
    ui = [run.strip() for run in _runs(doc, "macos-app") if "check_app.py ui" in run]
    assert ui == ["uv run --no-project python tools/macos/check_app.py ui --require-driven"]
    ui = [run.strip() for run in _runs(doc, "macos-smoke") if "check_app.py ui" in run]
    assert ui == ["uv run --no-project python tools/macos/check_app.py ui"]
    # The same for placement, which reads the note a misplaced copy shows; install drives nothing.
    for job, driven in (("macos-app", " --require-driven"), ("macos-smoke", "")):
        runs = [run.strip() for run in _runs(doc, job)]
        assert [run for run in runs if "check_app.py install" in run] == [
            "uv run --no-project python tools/macos/check_app.py install"
        ], job
        assert [run for run in runs if "check_app.py placement" in run] == [
            f"uv run --no-project python tools/macos/check_app.py placement{driven}"
        ], job
    diff = [run for run in _runs(doc, "macos-smoke") if "library-diff" in run]
    assert len(diff) == 1 and "--control-run" in diff[0]
    # The smoke jobs check the restored bytes before anything else uses them, and run no check
    # unless those bytes were the ones macos-app built.
    steps = _steps(doc, "macos-smoke")
    restore = next(i for i, step in enumerate(steps) if "cache/restore" in str(step.get("uses")))
    verify = steps[restore + 1]
    assert "check_app.py handoff-check" in verify["run"] and "if" not in verify
    assert verify["env"] == {
        "DMG_SHA256": "${{ needs.macos-app.outputs.dmg_sha256 }}",
        "SELFTEST_SHA256": "${{ needs.macos-app.outputs.selftest_sha256 }}",
    }
    unpack = steps[restore + 2]
    assert "check_app.py unpack-dmg" in unpack["run"] and "if" not in unpack
    for step in steps[restore + 3 :]:
        if not str(step.get("uses", "")).startswith("actions/upload-artifact@"):
            assert step["if"] == "${{ !cancelled() && steps.unpacked.outcome == 'success' }}"
    # In macos-app, each check runs whatever the one before it found, once the build succeeded.
    for step in _steps(doc, "macos-app"):
        run = str(step.get("run", ""))
        if "check_app.py" in run and not re.search(r"check_app\.py (selftest|handoff) ", run):
            assert step["if"] == "${{ !cancelled() && steps.build.outcome == 'success' }}", run
    # The second build is recorded, never a gate, and says SAME, DIFFERENT or FAILED.
    repeat = [step for step in _steps(doc, "macos-app") if "build/macos-repeat" in str(step)]
    assert len(repeat) == 3 and all(step.get("continue-on-error") is True for step in repeat)
    assert "compare build/macos build/macos-repeat" in repeat[-1]["run"]
    assert '>> "$GITHUB_STEP_SUMMARY"' in repeat[-1]["run"]
    # One version flag, never both: --tag on a tag ref, else the run number.
    for run in _runs(doc, "macos-app"):
        if "--build-number" in run or "--tag" in run:
            assert (
                'if [ "$REF_TYPE" = tag ]; then set -- --tag "$REF_NAME"; '
                'else set -- --build-number "$RUN_NUMBER"; fi'
            ) in run
    assert not (build_app.REPO_ROOT / ".github" / "workflows" / "macos-app.yml").exists()


def test_only_the_xcode_27_leg_may_fail_without_failing_the_run() -> None:
    # The maintainer's decision of 2026-11-02 (decisions/0002 §18): the xcode-27 leg still runs and
    # shows its result, but a failure there does not fail the run. Every other job gates.
    doc = _ci()
    smoke = doc["jobs"]["macos-smoke"]
    assert smoke["continue-on-error"] == "${{ matrix.runner == 'xcode-27' }}"
    runners = smoke["strategy"]["matrix"]["runner"]
    assert "xcode-27" in runners and len(runners) > 1
    for job_id, job in doc["jobs"].items():
        if job_id != "macos-smoke":
            assert "continue-on-error" not in job, job_id


def test_the_pairing_check_runs_with_the_suites_copy_of_the_runtime() -> None:
    doc = _ci()
    steps = _steps(doc, "macos-app")
    runs = [str(step.get("run", "")) for step in steps]
    pairing = [index for index, run in enumerate(runs) if "check_app.py pairing" in run]
    assert len(pairing) == 1
    step = steps[pairing[0]]
    # It needs Switchboard's hub and the test harness, which the suite step installed into its
    # copy of the runtime (and nothing else on the runner holds), so this script runs with that
    # copy's interpreter, with the flags the suite runs pytest with; the step refuses to go on
    # unless exactly one such interpreter is there.
    suite = [index for index, run in enumerate(runs) if "check_app.py suite" in run]
    assert len(suite) == 1 and suite[0] < pairing[0]
    assert '--copy-to "$RUNNER_TEMP/suite"' in runs[suite[0]]
    assert step["run"].splitlines() == [
        f'set -- "$RUNNER_TEMP"/suite/*.app/{build_app.RUNTIME_IN_APP}',
        'if [ "$#" -ne 1 ] || [ ! -x "$1" ]; then echo "the suite step made no copy of the '
        'runtime: $*"; exit 1; fi',
        '"$1" -I -B tools/macos/check_app.py pairing --require-driven',
    ]
    assert step["if"] == "${{ !cancelled() && steps.build.outcome == 'success' }}"
    assert "env" not in step
    # After the ui check, before the last signature check, which then sees whether the pairing
    # check wrote into the bundle.
    ui = runs.index("uv run --no-project python tools/macos/check_app.py ui --require-driven")
    signatures = [index for index, run in enumerate(runs) if "check_app.py signature" in run]
    assert ui < pairing[0] < signatures[-1]
    # Its own time limit, well inside the job's: a hang must not use up the job and cancel the
    # upload of the screenshots that show where it hung, which comes after it.
    limit = step["timeout-minutes"]
    job = doc["jobs"]["macos-app"]
    assert isinstance(limit, int) and 0 < limit <= job["timeout-minutes"] // 2
    uploads = [
        index
        for index, found in enumerate(steps)
        if str(found.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert uploads and min(uploads) > pairing[0]
    # Every step that drives the app or runs a long check has a limit of its own too, in both
    # jobs, at most half the job's as this one's is: a hang in any of them must leave the job the
    # time to upload what shows it.
    for job_id, names in (
        ("macos-app", ("swift", "relocate", "placement", "ui", "suite", "pairing")),
        ("macos-smoke", ("swift", "placement", "ui")),
    ):
        job = doc["jobs"][job_id]
        for name in names:
            (found,) = [
                found
                for found in _steps(doc, job_id)
                if re.search(rf"check_app\.py {name}\b", str(found.get("run", "")))
            ]
            limit = found.get("timeout-minutes")
            assert isinstance(limit, int), (job_id, name)
            assert 0 < limit <= job["timeout-minutes"] // 2, (job_id, name)
    assert not any("check_app.py pairing" in run for run in _runs(doc, "macos-smoke"))


def test_the_pairing_step_finds_the_interpreter_the_suite_check_installs_into(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The pairing step's glob holds only if check_suite puts its copy at <--copy-to>/<the app>;
    # a mismatch would show only on the Mac, as "made no copy". Here check_suite runs with its
    # programs stubbed up to its first install, then the step's own lines look for the
    # interpreter it installed into.
    doc = _ci()
    runs = _runs(doc, "macos-app")
    (suite,) = [shlex.split(run) for run in runs if "check_app.py suite" in run]
    (pairing,) = [run for run in runs if "check_app.py pairing" in run]
    runner_temp = tmp_path / "runner"
    copy_to = Path(suite[suite.index("--copy-to") + 1].replace("$RUNNER_TEMP", str(runner_temp)))
    app = check_app.app_path(tmp_path / "build")
    runtime = check_app.python_in(app)
    runtime.parent.mkdir(parents=True)
    runtime.write_text("#!/bin/sh\n", encoding="ascii")
    runtime.chmod(0o755)
    installed_into: list[str] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == "ditto":
            shutil.copytree(argv[1], argv[2], symlinks=True)
        elif argv[:3] == ["uv", "pip", "install"]:
            installed_into.append(argv[argv.index("--python") + 1])
            return subprocess.CompletedProcess(argv, 1, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with monkeypatch.context() as patched:
        patched.setattr(check_app, "_manifest", lambda app: {})
        patched.setattr(check_app, "find_reference_wheels", lambda manifest, folders: ({}, []))
        patched.setattr("subprocess.run", run)
        check_app.check_suite(app, tmp_path / "build", copy_to, check_app.Checker())
    (python,) = installed_into

    def step() -> subprocess.CompletedProcess[str]:
        lines = [line for line in pairing.splitlines() if "check_app.py pairing" not in line]
        return subprocess.run(
            ["sh", "-c", "\n".join([*lines, 'printf "%s\\n" "$1"'])],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "RUNNER_TEMP": str(runner_temp)},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    done = step()
    assert (done.returncode, done.stdout) == (0, f"{python}\n"), done.stderr
    # Two copies, and the step refuses rather than pick one.
    shutil.copytree(copy_to / app.name, copy_to / "Other.app", symlinks=True)
    assert step().returncode == 1


def test_the_probe_runs_after_the_host_check_on_the_read_only_bundle() -> None:
    # In both jobs the probe prints what the verbs do on the bundle as the app ships it, read-only,
    # once the host check has run, and runs as every check but pairing does, without the project.
    doc = _ci()
    for job in ("macos-app", "macos-smoke"):
        runs = [run.strip() for run in _runs(doc, job)]
        probe = [index for index, run in enumerate(runs) if "check_app.py probe" in run]
        assert len(probe) == 1, job
        assert runs[probe[0]] == "uv run --no-project python tools/macos/check_app.py probe", job
        made_readonly = runs.index("chmod -R a-w build/macos/*.app")
        host = next(i for i, run in enumerate(runs) if re.search(r"check_app\.py host\b", run))
        assert made_readonly < host < probe[0], job


def test_ci_steps_take_github_values_only_through_the_environment() -> None:
    doc = _ci()
    for job_id, job in doc["jobs"].items():
        for step in job.get("steps", []):
            assert "${{" not in str(step.get("run", "")), (job_id, step.get("name"))


def test_ci_actions_are_pinned_to_the_known_commits() -> None:
    doc = _ci()
    found = set()
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if "uses" in step:
                action, _, sha = str(step["uses"]).partition("@")
                assert PINNED_ACTIONS.get(action) == sha, step["uses"]
                found.add(action)
    assert found == set(PINNED_ACTIONS)


# ---------------------------------------------------------------------------------------------
# Signatures and the Mach-O files

CODESIGN_GOOD = """\
Executable=/Users/runner/work/x/build/macos/Capsule.app/Contents/Resources/runtime/bin/lucille-host-runtime
Identifier=com.lucille-ai.capsule.runtime
Format=Mach-O thin (arm64)
CodeDirectory v=20500 size=137447 flags=0x10002(adhoc,runtime) hashes=4290+2 location=embedded
Signature=adhoc
TeamIdentifier=not set
Runtime Version=15.0.0
Sealed Resources=none
Internal requirements count=0 size=12
"""


def _codesign(old: str, new: str) -> str:
    assert old in CODESIGN_GOOD
    return CODESIGN_GOOD.replace(old, new)


def test_codesign_problems() -> None:
    runtime = PINS.runtime_id
    assert check_app.codesign_problems(CODESIGN_GOOD, identifier=runtime, hardened=True) == []
    assert check_app.codesign_problems(CODESIGN_GOOD, identifier=runtime, hardened=None) == []
    assert check_app.codesign_problems(CODESIGN_GOOD, identifier=runtime, hardened=False)
    plain = _codesign("(adhoc,runtime)", "(adhoc)")
    assert check_app.codesign_problems(plain, identifier=runtime, hardened=False) == []
    assert check_app.codesign_problems(plain, identifier=runtime, hardened=True)
    assert check_app.codesign_problems(CODESIGN_GOOD, identifier=PINS.bundle_id, hardened=None)


@pytest.mark.parametrize(
    ("old", "new", "said"),
    [
        # What a wheel's extension or swiftc's output carries before the build signs it.
        ("flags=0x10002(adhoc,runtime)", "flags=0x20002(adhoc,linker-signed)", "linker"),
        ("flags=0x10002(adhoc,runtime)", "flags=0x30002(adhoc,runtime,linker-signed)", "linker"),
        ("Signature=adhoc", "Authority=Developer ID Application: X", "ad hoc"),
        ("flags=0x10002(adhoc,runtime)", "flags=0x10000(runtime)", "ad hoc"),
        ("TeamIdentifier=not set", "TeamIdentifier=ABCDE12345", "Team"),
        ("TeamIdentifier=not set\n", "", "Team"),
        ("Identifier=com.lucille-ai.capsule.runtime", "Identifier=python3.13", "identifier"),
        ("Identifier=com.lucille-ai.capsule.runtime\n", "", "identifier"),
        ("flags=0x10002(adhoc,runtime) ", "", "flags"),
    ],
)
def test_codesign_problems_refuse(old: str, new: str, said: str) -> None:
    problems = check_app.codesign_problems(
        _codesign(old, new), identifier=PINS.runtime_id, hardened=None
    )
    assert problems and any(said in problem for problem in problems), problems


def test_codesign_problems_read_no_line_of_another_file() -> None:
    # A line that merely contains a key, such as an Executable path naming one, is not that key.
    text = (
        _codesign(
            "Executable=/Users/runner/work/x/",
            "Executable=/Users/Signature=adhoc/TeamIdentifier=not set/",
        )
        .replace("Signature=adhoc\n", "")
        .replace("TeamIdentifier=not set\n", "")
    )
    assert len(check_app.codesign_problems(text, identifier=PINS.runtime_id, hardened=None)) == 2


def _runtime(rel: str) -> str:
    return f"{build_app.RUNTIME_DIR}/{rel}"


EXPECTED_MACHO = {
    f"Contents/MacOS/{PINS.app_name}",
    build_app.RUNTIME_IN_APP,
    _runtime(f"{build_app.STDLIB}/lib-dynload/_dbm.cpython-313-darwin.so"),
    _runtime(f"{build_app.SITE_PACKAGES}/_cffi_backend.cpython-313-darwin.so"),
    _runtime(f"{build_app.SITE_PACKAGES}/cryptography/hazmat/bindings/_rust.abi3.so"),
}


def test_the_mach_o_files_are_exactly_the_expected_ones() -> None:
    assert check_app.expected_macho_files(PINS) == frozenset(EXPECTED_MACHO)
    assert check_app.macho_set_problems(sorted(EXPECTED_MACHO), PINS) == []
    assert check_app.macho_set_problems([], PINS)
    for missing in EXPECTED_MACHO:
        problems = check_app.macho_set_problems(sorted(EXPECTED_MACHO - {missing}), PINS)
        assert any(missing in problem for problem in problems)
    extra = _runtime(f"{build_app.SITE_PACKAGES}/other/_speedups.cpython-313-darwin.so")
    problems = check_app.macho_set_problems([*EXPECTED_MACHO, extra], PINS)
    assert any(extra in problem for problem in problems)


def test_each_mach_o_file_has_the_identifier_the_build_gave_it(tmp_path: Path) -> None:
    app = tmp_path / f"{PINS.app_name}.app"
    for rel in EXPECTED_MACHO:
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        (app / rel).write_bytes(b"\xcf\xfa\xed\xfe")
    assert check_app.expected_identifier(app, app, PINS) == PINS.bundle_id
    main = app / "Contents" / "MacOS" / PINS.app_name
    assert check_app.expected_identifier(app, main, PINS) == PINS.bundle_id
    interpreter = app / build_app.RUNTIME_IN_APP
    assert check_app.expected_identifier(app, interpreter, PINS) == PINS.runtime_id
    rust = app / _runtime(f"{build_app.SITE_PACKAGES}/cryptography/hazmat/bindings/_rust.abi3.so")
    identifier = check_app.expected_identifier(app, rust, PINS)
    assert identifier == build_app.signing_identifier(app, rust, PINS)
    assert identifier.startswith(PINS.bundle_id + ".")


def test_the_checks_run_the_host_with_the_apps_flags(tmp_path: Path) -> None:
    python = Path("/x/lucille-host-runtime")
    assert check_app.host_argv(python, ["status", "--json"]) == [
        str(python),
        "-I",
        "-B",
        "-X",
        "utf8",
        "-m",
        "lucille_host",
        "status",
        "--json",
    ]
    for flag in ("isolated", "dont_write_bytecode", "utf8_mode"):
        assert f"assert sys.flags.{flag} == 1" in check_app.NATIVE_PROBE
    # The app's flags leave --check-hash-based-pycs at default, so the interpreter checks each
    # checked-hash .pyc's source hash against its .py when it imports it, and leave no pycache
    # prefix, so it reads .pyc files only beside their sources (0002 §6). The host and
    # environment checks run the probe with the bundled interpreter; here its lines run with this
    # one, with PYTHONPYCACHEPREFIX set, which -I ignores.
    lines = [
        line
        for line in check_app.NATIVE_PROBE.splitlines()
        if "_imp" in line or "pycache_prefix" in line or line == "import sys"
    ]
    assert "import _imp" in lines
    assert any(line.startswith('assert _imp.check_hash_based_pycs == "default"') for line in lines)
    assert any(line.startswith("assert sys.pycache_prefix is None") for line in lines)
    env = {"PATH": "/usr/bin:/bin", "PYTHONPYCACHEPREFIX": str(tmp_path / "elsewhere")}
    for flags, returncode in ((build_app.RUNTIME_FLAGS, 0), (("-B", "-X", "utf8"), 1)):
        done = subprocess.run(
            [sys.executable, *flags, "-c", "\n".join(lines)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
        assert done.returncode == returncode, (flags, done.stderr)
    # The control above, without -I, fails on the prefix, not on another line.
    assert "AssertionError: " + str(tmp_path / "elsewhere") in done.stderr


# ---------------------------------------------------------------------------------------------
# ~/Library (M12)


def test_the_library_allowlist_holds_only_the_hosts_folder_and_the_apps_defaults() -> None:
    text = check_app.LIBRARY_ALLOWLIST.read_text(encoding="utf-8")
    rules = check_app.parse_library_allowlist(
        text, bundle_id=PINS.bundle_id, app_name=PINS.app_name
    )
    assert [(rule.pattern, rule.prefix) for rule in rules] == [
        ("Application Support/lucille-host", True),
        (f"Preferences/{PINS.bundle_id}.plist", False),
    ]
    assert all(rule.reason for rule in rules)


def _rules(text: str) -> tuple[check_app.AllowRule, ...]:
    return check_app.parse_library_allowlist(
        text, bundle_id="com.lucille-ai.capsule", app_name="Capsule"
    )


@pytest.mark.parametrize(
    ("rel", "allowed"),
    [
        ("Application Support/lucille-host", True),
        ("Application Support/lucille-host/default/state/paused", True),
        ("Application Support/lucille-host-other", False),
        ("Application Support", False),
        ("Preferences/com.lucille-ai.capsule.plist", True),
        ("Preferences/com.lucille-ai.capsule.other.plist", False),
        ("Preferences/com.lucille-ai.capsule.plist/x", False),
        ("Caches/com.lucille-ai.capsule", True),
        ("Caches/com.lucille-ai.capsule.other", True),
        # "*" never crosses a "/", as fnmatch's does.
        ("Caches/other/com.lucille-ai.capsule", False),
        ("Logs/x", True),
        ("Logs/x/y", False),
        ("logs/x", False),
    ],
)
def test_the_library_allowlist_matches_one_segment_at_a_time(rel: str, allowed: bool) -> None:
    rules = _rules(
        "# a comment\n"
        "Application Support/lucille-host/**  # the host's folders\n"
        "Preferences/{bundle_id}.plist  # the app's defaults\n"
        "Caches/{bundle_id}*  # a pattern\n"
        "Logs/*  # one level\n"
    )
    assert check_app.library_allowed(rel, rules) is allowed


@pytest.mark.parametrize(
    "line",
    [
        "Preferences/x.plist",
        "Preferences/x.plist  #",
        "/Preferences/x.plist  # absolute",
        "Preferences/../x  # up",
        "Preferences/./x  # dot",
        "Preferences//x  # empty",
        "Caches/**/x  # deep",
        "Caches/a**  # deep",
        "Caches/*/**  # a pattern before the folder rule",
        "Caches/{team_id}  # unknown",
        "Caches\\x  # backslash",
        "**  # everything",
    ],
)
def test_a_bad_library_allowlist_line_is_refused(line: str) -> None:
    with pytest.raises(check_app.AllowlistError):
        _rules(line + "\n")


def test_a_library_allowlist_line_given_twice_is_refused() -> None:
    with pytest.raises(check_app.AllowlistError):
        _rules("Logs/x  # one\nLogs/x  # two\n")


def _entry(kind: str = "file", size: int = 1, mtime: int = 1) -> list[Any]:
    return [kind, size, mtime]


def test_library_changes() -> None:
    before = {"a": _entry(), "b": _entry(), "c": _entry("dir", 0, 1)}
    after = {"a": _entry(), "b": _entry(size=2), "c": _entry("dir", 0, 2), "d": _entry()}
    assert check_app.library_changes(before, after) == [
        ("b", "changed"),
        ("c", "changed"),
        ("d", "new"),
    ]
    assert check_app.library_changes(after, before) == [
        ("b", "changed"),
        ("c", "changed"),
        ("d", "removed"),
    ]


def test_library_problems() -> None:
    rules = check_app.parse_library_allowlist(
        check_app.LIBRARY_ALLOWLIST.read_text(encoding="utf-8"),
        bundle_id=PINS.bundle_id,
        app_name=PINS.app_name,
    )
    control = check_app.LIBRARY_CONTROL
    changes = [
        ("Application Support", "changed"),
        ("Application Support/lucille-host", "new"),
        (control, "new"),
        (f"Preferences/{PINS.bundle_id}.plist", "new"),
        ("Caches/com.apple.Safari/x", "changed"),
        (f"Caches/{PINS.bundle_id}/Cache.db", "new"),
        (f"Logs/DiagnosticReports/{PINS.app_name}-2026-11-02-101010.ips", "new"),
        ("Saved Application State/x.LUCILLE.y", "removed"),
        ("HTTPStorages/" + PINS.app_name.lower(), "new"),
    ]
    verdict = check_app.library_problems(
        changes, rules, bundle_id=PINS.bundle_id, app_name=PINS.app_name
    )
    assert verdict.control_seen
    assert verdict.failures == [
        f"new: Caches/{PINS.bundle_id}/Cache.db",
        f"new: Logs/DiagnosticReports/{PINS.app_name}-2026-11-02-101010.ips",
        "removed: Saved Application State/x.LUCILLE.y",
        "new: HTTPStorages/" + PINS.app_name.lower(),
    ]
    assert verdict.allowed == [
        "new: Application Support/lucille-host",
        f"new: {control}",
        f"new: Preferences/{PINS.bundle_id}.plist",
    ]
    assert verdict.noise == [
        "changed: Application Support",
        "changed: Caches/com.apple.Safari/x",
    ]
    # A walk that saw none of the host's own writes proves nothing: no control, a failure.
    quiet = check_app.library_problems(
        [("Caches/com.apple.Safari/x", "changed")],
        rules,
        bundle_id=PINS.bundle_id,
        app_name=PINS.app_name,
    )
    assert not quiet.control_seen and quiet.failures == []
    # An empty diff is the case the control is for: a walk of the wrong folder, or a "before"
    # taken after the run, sees no change at all.
    empty = check_app.library_problems([], rules, bundle_id=PINS.bundle_id, app_name=PINS.app_name)
    assert empty == check_app.LibraryVerdict([], [], [], False)
    # A sibling of the state folder is not the state folder.
    sibling = check_app.library_problems(
        [(control + "-x", "new"), (control + "x/y", "new")],
        rules,
        bundle_id=PINS.bundle_id,
        app_name=PINS.app_name,
    )
    assert not sibling.control_seen


def _mac_state(home: Path) -> Path:
    """The state folder of the app's profile, where the host keeps it on macOS."""
    return paths.dirs(check_app.APP_PROFILE, family="macos", environ={"HOME": str(home)}).state


def test_the_library_control_is_the_state_folder_of_the_apps_profile(tmp_path: Path) -> None:
    assert tmp_path / "Library" / check_app.LIBRARY_CONTROL == _mac_state(tmp_path)


# A stand-in for the app's runtime, for the two controls: the last --profile given, or default,
# and the pause flag in that profile's state folder, which both verbs make, as the host does.
_CONTROLS_RUNTIME = """\
#!/bin/sh
profile=default
while [ "$#" -gt 1 ]; do
    [ "$1" = --profile ] && profile=$2
    shift
done
state="$HOME/Library/Application Support/lucille-host/$profile/state"
mkdir -p "$state" || exit 1
case $1 in
    pause) : > "$state/paused" ;;
    resume) rm -f "$state/paused" ;;
    *) exit 2 ;;
esac
"""


def _controls_app(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(home))
    app = home / "Capsule.app"
    runtime = check_app.python_in(app)
    runtime.parent.mkdir(parents=True)
    runtime.write_text(_CONTROLS_RUNTIME, encoding="ascii")
    runtime.chmod(0o755)
    return app


@pytest.mark.parametrize("paused", [False, True])
def test_the_control_run_changes_the_apps_profile_and_leaves_it_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paused: bool
) -> None:
    app = _controls_app(tmp_path, monkeypatch)
    flag = _mac_state(tmp_path) / "paused"
    if paused:
        flag.parent.mkdir(parents=True)
        flag.touch()
    checker = check_app.Checker()
    check_app._control_writes(app, checker)
    assert checker.failures == []
    assert flag.is_file() == paused
    # Both verbs ran on the app's profile: no other profile appeared.
    hosts = tmp_path / "Library" / "Application Support" / "lucille-host"
    assert [folder.name for folder in hosts.iterdir()] == [check_app.APP_PROFILE]


def test_the_library_diff_sees_its_own_control_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _controls_app(tmp_path, monkeypatch)
    library = tmp_path / "Library"
    library.mkdir()
    before = tmp_path / "before.json"
    before.write_text(json.dumps(check_app.library_snapshot(library)), encoding="utf-8")
    checker = check_app.Checker()
    check_app.check_library_diff(app, before, True, checker)
    assert checker.failures == []
    # From here on nothing writes, so a diff without the control run has no control.
    before.write_text(json.dumps(check_app.library_snapshot(library)), encoding="utf-8")
    quiet = check_app.Checker()
    check_app.check_library_diff(app, before, False, quiet)
    assert len(quiet.failures) == 1


def test_library_snapshot(tmp_path: Path) -> None:
    library = tmp_path / "Library"
    (library / "Preferences").mkdir(parents=True)
    (library / "Preferences" / "a.plist").write_bytes(b"abc")
    (library / "Developer" / "Xcode").mkdir(parents=True)
    (library / "Developer" / "Xcode" / "big").write_bytes(b"x")
    (library / "Android" / "sdk").mkdir(parents=True)
    (library / "Caches" / "Developer").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    (library / "Caches" / "link").symlink_to(outside, target_is_directory=True)
    snapshot = check_app.library_snapshot(library)
    entries = snapshot["entries"]
    assert set(entries) == {
        "Preferences",
        "Preferences/a.plist",
        "Caches",
        "Caches/Developer",
        "Caches/link",
    }
    assert entries["Preferences/a.plist"][:2] == ["file", 3]
    assert entries["Caches/link"][0] == "link"
    assert entries["Caches/Developer"][0] == "dir"
    assert snapshot["root"] == str(library)
    assert snapshot["unreadable"] == []
    json.dumps(snapshot)


# ---------------------------------------------------------------------------------------------
# The hand-off from macos-app to the smoke jobs


def test_handoff_key() -> None:
    assert check_app.handoff_key("Capsule", "123456789", "2") == (
        "capsule-adhoc-macos-arm64-123456789-2"
    )
    assert check_app.handoff_key("My App", "1", "1") == "my-app-adhoc-macos-arm64-1-1"
    for run_id, attempt in (("", "1"), ("1", ""), ("0", "1"), ("1x", "1"), ("1\n2", "1")):
        with pytest.raises(check_app.CheckError):
            check_app.handoff_key("Capsule", run_id, attempt)


def test_handoff_outputs() -> None:
    a, b = "a" * 64, "b" * 64
    assert check_app.handoff_outputs("capsule-adhoc-macos-arm64-1-1", a, b) == (
        f"cache_key=capsule-adhoc-macos-arm64-1-1\ndmg_sha256={a}\nselftest_sha256={b}\n"
    )
    for key, dmg, selftest in (
        ("x\ny=1", a, b),
        ("ok", "A" * 64, b),
        ("ok", a, b + "\n"),
        ("", a, b),
    ):
        with pytest.raises(check_app.CheckError):
            check_app.handoff_outputs(key, dmg, selftest)


def _handoff(folder: Path) -> tuple[str, str]:
    folder.mkdir(parents=True, exist_ok=True)
    dmg = folder / build_app.dmg_name(PINS.app_name, "0.1.0.dev0", "abcdef123456")
    dmg.write_bytes(b"disk image")
    (folder / check_app.SELFTEST_NAME).write_bytes(b"self-test")
    return (
        hashlib.sha256(b"disk image").hexdigest(),
        hashlib.sha256(b"self-test").hexdigest(),
    )


def test_handoff_problems(tmp_path: Path) -> None:
    folder = tmp_path / "handoff"
    dmg, selftest = _handoff(folder)
    assert check_app.handoff_problems(folder, PINS.app_name, dmg, selftest) == []
    assert check_app.handoff_problems(folder, PINS.app_name, selftest, selftest)
    assert check_app.handoff_problems(folder, PINS.app_name, dmg, dmg)
    assert check_app.handoff_problems(folder, PINS.app_name, "", selftest)
    assert check_app.handoff_problems(folder, PINS.app_name, dmg.upper(), selftest)
    (folder / "extra").write_bytes(b"")
    assert check_app.handoff_problems(folder, PINS.app_name, dmg, selftest)
    (folder / "extra").unlink()
    second = folder / build_app.dmg_name(PINS.app_name, "0.1.0.dev0", "abcdef000000")
    second.write_bytes(b"disk image")
    assert check_app.handoff_problems(folder, PINS.app_name, dmg, selftest)
    second.unlink()
    (folder / check_app.SELFTEST_NAME).unlink()
    assert check_app.handoff_problems(folder, PINS.app_name, dmg, selftest)
    assert check_app.handoff_problems(tmp_path / "none", PINS.app_name, dmg, selftest)


# ---------------------------------------------------------------------------------------------
# The record check on the signed bundle (M10)


def _wheel(folder: Path, name: str, version: str, files: dict[str, bytes]) -> Path:
    info = f"{name}-{version}.dist-info"
    entries = {
        **files,
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n".encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: cp313-cp313-x\n",
    }
    rows = [f"{path},{payload.record_hash(data)},{len(data)}" for path, data in entries.items()]
    rows.append(f"{info}/RECORD,,")
    folder.mkdir(parents=True, exist_ok=True)
    wheel = folder / f"{name}-{version}-cp313-cp313-x.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, data in entries.items():
            archive.writestr(path, data)
        archive.writestr(f"{info}/RECORD", "\n".join(rows) + "\n")
    return wheel


def _install(wheel: Path, site: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
        info = next(name.split("/")[0] for name in archive.namelist() if ".dist-info/" in name)
    record = site / info / "RECORD"
    data = b"uv"
    (site / info / "INSTALLER").write_bytes(data)
    record.write_text(
        record.read_text(encoding="utf-8")
        + f"{info}/INSTALLER,{payload.record_hash(data)},{len(data)}\n",
        encoding="utf-8",
    )


def test_split_record_problems_on_a_signed_mach_o(tmp_path: Path) -> None:
    macho = b"\xcf\xfa\xed\xfe extension"
    wheel = _wheel(
        tmp_path / "wheels", "demo", "1.0", {"demo/__init__.py": b"", "demo/_x.so": macho}
    )
    site = tmp_path / "site"
    _install(wheel, site)
    assert payload.record_problems(site, [wheel]) == []
    assert check_app.split_record_problems([], ["demo/_x.so"]) == ([], [])
    # codesign rewrites a Mach-O file in place: reported, not failed.
    (site / "demo" / "_x.so").write_bytes(macho + b" signed")
    found = payload.record_problems(site, [wheel])
    failing, signed = check_app.split_record_problems(found, ["demo/_x.so"])
    assert failing == [] and len(signed) == 2 and all("demo/_x.so" in p for p in signed)
    # Any other file that differs fails, and so does a Mach-O file the check was not told of.
    (site / "demo" / "__init__.py").write_bytes(b"planted = 1\n")
    found = payload.record_problems(site, [wheel])
    failing, signed = check_app.split_record_problems(found, ["demo/_x.so"])
    assert len(failing) == 2 and all("demo/__init__.py" in p for p in failing)
    failing, signed = check_app.split_record_problems(found, [])
    assert len(failing) == 4 and signed == []
    # A file no RECORD lists fails even when it is a Mach-O file.
    (site / "demo" / "_y.so").write_bytes(macho)
    found = payload.record_problems(site, [wheel])
    failing, _ = check_app.split_record_problems(found, ["demo/_x.so", "demo/_y.so"])
    assert any("demo/_y.so" in p for p in failing)


# A stand-in for codesign: --remove-signature cuts a file at its first b"SIG", or fails on a file
# whose name ends with FAKE_CODESIGN_REFUSES.
FAKE_CODESIGN = """\
import os, sys
assert sys.argv[1:2] == ["--remove-signature"] and len(sys.argv) == 3, sys.argv
path = sys.argv[2]
refuses = os.environ.get("FAKE_CODESIGN_REFUSES")
if refuses and path.endswith(refuses):
    sys.stderr.write(path + ": invalid or unsupported format for signature\\nsecond line\\n")
    sys.exit(3)
with open(path, "rb") as handle:
    data = handle.read()
with open(path, "wb") as handle:
    handle.write(data.split(b"SIG", 1)[0])
"""


def _fake_codesign(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "codesign"
    fake.write_text(f"#!{sys.executable}\n{FAKE_CODESIGN}", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def _stripped(tmp_path: Path, ours: bytes, theirs: bytes) -> list[tuple[str, str]]:
    wheel = _wheel(tmp_path / "wheels", "demo", "1.0", {"demo/_x.so": theirs})
    site = tmp_path / "site"
    (site / "demo").mkdir(parents=True)
    (site / "demo" / "_x.so").write_bytes(ours)
    return check_app._stripped_comparison(site, ["demo/_x.so", "demo/_none.so"], [wheel])


def test_stripped_comparison_same_and_different(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_codesign(tmp_path, monkeypatch)
    same = _stripped(tmp_path / "same", b"codeSIG ours", b"codeSIG theirs")
    assert same == [("demo/_x.so", "SAME"), ("demo/_none.so", "in no wheel")]
    different = _stripped(tmp_path / "different", b"code+SIG ours", b"codeSIG theirs")
    assert different[0] == (
        "demo/_x.so",
        "DIFFERENT (without signatures, the bundle's copy is 5 bytes and the wheel's 4)",
    )


@pytest.mark.parametrize("refused", ["bundle", "wheel"])
def test_stripped_comparison_names_a_copy_codesign_could_not_strip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refused: str
) -> None:
    # A removal that failed used to read as DIFFERENT, so DIFFERENT could not show that the
    # signatures had been removed at all.
    _fake_codesign(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_CODESIGN_REFUSES", f"-{refused}")
    rel, verdict = _stripped(tmp_path, b"codeSIG ours", b"codeSIG theirs")[0]
    assert rel == "demo/_x.so"
    assert verdict.startswith(
        f"not compared: `codesign --remove-signature` failed on the {refused}'s copy (exit 3: "
    )
    assert verdict.endswith(": invalid or unsupported format for signature)")
    assert "second line" not in verdict and "SAME" not in verdict and "DIFFERENT" not in verdict


def test_stripped_comparison_strips_even_copies_that_are_already_equal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Equal bytes before stripping would read SAME either way; what codesign said must not be
    # skipped for them.
    _fake_codesign(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_CODESIGN_REFUSES", "-bundle")
    _, verdict = _stripped(tmp_path, b"codeSIG", b"codeSIG")[0]
    assert verdict.startswith("not compared: `codesign --remove-signature` failed on the bundle's")


def _manifest(wheels: list[tuple[str, str, Path]], host: Path) -> dict[str, Any]:
    return {
        "host": {
            "version": "0.1.0.dev0",
            "wheel": {"file": host.name, "sha256": payload.sha256_of(host)},
        },
        "wheels": [
            {
                "name": name,
                "version": version,
                "file": path.name,
                "url": f"https://files.example/{path.name}",
                "sha256": payload.sha256_of(path),
            }
            for name, version, path in wheels
        ],
    }


def test_reference_wheels_are_hashed_against_the_manifest(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    demo = _wheel(cache, "demo", "1.0", {"demo/__init__.py": b""})
    other = _wheel(cache, "other", "2.0", {"other/__init__.py": b""})
    host = _wheel(tmp_path / "host-wheel", "lucille_host", "0.1.0.dev0", {"x/__init__.py": b""})
    manifest = _manifest([("demo", "1.0", demo), ("other", "2.0", other)], host)
    folders = [cache, tmp_path / "work" / "host-wheel", tmp_path / "host-wheel"]
    found, problems = check_app.find_reference_wheels(manifest, folders)
    assert problems == []
    assert found == {demo.name: demo, other.name: other, host.name: host}
    other.write_bytes(other.read_bytes() + b"x")
    found, problems = check_app.find_reference_wheels(manifest, folders)
    assert len(problems) == 1 and other.name in problems[0] and "sha256" in problems[0]
    other.unlink()
    found, problems = check_app.find_reference_wheels(manifest, folders)
    assert len(problems) == 1 and other.name in problems[0]
    bad = copy.deepcopy(manifest)
    bad["wheels"][0]["file"] = "../demo-1.0-cp313-cp313-x.whl"
    assert check_app.find_reference_wheels(bad, folders)[1]
    bad = copy.deepcopy(manifest)
    bad["wheels"] = []
    assert check_app.find_reference_wheels(bad, folders)[1]


def test_distribution_set_problems() -> None:
    manifest = {
        "host": {"version": "0.1.0.dev0"},
        "wheels": [{"name": "demo", "version": "1.0"}, {"name": "other", "version": "2.0"}],
    }
    installed = {"demo": "1.0", "other": "2.0", "lucille-host": "0.1.0.dev0"}
    assert check_app.distribution_set_problems(installed, manifest) == []
    assert check_app.distribution_set_problems({**installed, "extra": "1"}, manifest)
    assert check_app.distribution_set_problems({**installed, "demo": "1.1"}, manifest)
    without = {name: v for name, v in installed.items() if name != "lucille-host"}
    assert check_app.distribution_set_problems(without, manifest)
    assert check_app.distribution_set_problems({}, {"host": {"version": "x"}, "wheels": []})


def _a_lock_wheel() -> tuple[dict[tuple[str, str], tuple[payload.LockWheel, ...]], dict[str, str]]:
    """uv.lock as the build reads it, and one of its wheels as MANIFEST.json names a wheel."""
    lock = payload.parse_lock(build_app.LOCK_FILE.read_text(encoding="utf-8"))
    wheel = next(wheels[0] for _, wheels in sorted(lock.items()) if wheels)
    entry = {
        "name": wheel.name,
        "version": wheel.version,
        "file": wheel.filename,
        "url": wheel.url,
        "sha256": wheel.sha256,
    }
    return lock, entry


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "not-in-the-lock"),
        ("version", "6.6.6"),
        ("file", "other-1.0-py3-none-any.whl"),
        ("url", "https://example.invalid/other-1.0-py3-none-any.whl"),
        ("sha256", "0" * 64),
    ],
)
def test_lock_wheel_problems(field: str, value: str) -> None:
    lock, entry = _a_lock_wheel()
    host = {"version": "0.1.0.dev0", "wheel": {"file": "lucille_host.whl", "sha256": "a" * 64}}
    manifest = {"host": host, "wheels": [entry]}
    assert check_app.lock_wheel_problems(manifest, lock, host_wheel_sha256="a" * 64) == []
    # Each wheel must be one uv.lock pins, exactly: name, version, file, URL and sha256.
    bad = copy.deepcopy(manifest)
    bad["wheels"][0][field] = value
    problems = check_app.lock_wheel_problems(bad, lock, host_wheel_sha256="a" * 64)
    assert len(problems) == 1 and "uv.lock" in problems[0]
    # The host wheel must be the one the wheel step built.
    problems = check_app.lock_wheel_problems(manifest, lock, host_wheel_sha256="b" * 64)
    assert len(problems) == 1 and "b" * 64 in problems[0]
    # An empty output from the wheel step is a mismatch too.
    assert check_app.lock_wheel_problems(manifest, lock, host_wheel_sha256="")


def test_record_refuses_a_wheel_the_lock_does_not_pin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A site-packages and MANIFEST.json that agree with each other, and with the wheels beside
    # them, on a distribution uv.lock does not hold, and on a host wheel no wheel step built.
    app = check_app.app_path(tmp_path)
    site = app / build_app.RUNTIME_DIR / build_app.SITE_PACKAGES
    site.mkdir(parents=True)
    planted = _wheel(tmp_path / "cache" / "wheels", "not_in_the_lock", "6.6.6", {"n.py": b""})
    host = _wheel(tmp_path / "host-wheel", "lucille_host", "0.1.0.dev0", {"x/__init__.py": b""})
    _install(planted, site)
    _install(host, site)
    manifest = _manifest([("not_in_the_lock", "6.6.6", planted)], host)
    (app / build_app.MANIFEST_PATH).write_text(json.dumps(manifest), encoding="utf-8")
    checker = check_app.Checker()
    check_app.check_record(app, tmp_path, checker, None, host_wheel_sha256="c" * 64)
    shown = capsys.readouterr().out
    assert any("uv.lock" in failure for failure in checker.failures), checker.failures
    assert "not-in-the-lock==6.6.6" in shown and "c" * 64 in shown
    # The same bundle with only lock wheels in its MANIFEST.json and the built host wheel passes
    # that step; the RECORD steps after it are what fail then.
    lock, entry = _a_lock_wheel()
    assert (
        check_app.lock_wheel_problems(
            {"host": manifest["host"], "wheels": [entry]},
            lock,
            host_wheel_sha256=payload.sha256_of(host),
        )
        == []
    )


def test_pth_problems(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path / "wheels", "demo", "1.0", {"demo.pth": b"demo\n", "demo/a.py": b""})
    site = tmp_path / "site"
    _install(wheel, site)
    assert check_app.pth_problems(site) == []
    (site / "planted.pth").write_text("import os\n", encoding="utf-8")
    (site / "demo" / "deep.pth").write_text("x\n", encoding="utf-8")
    assert check_app.pth_problems(site) == [
        "demo/deep.pth is in no RECORD",
        "planted.pth is in no RECORD",
    ]


# ---------------------------------------------------------------------------------------------
# The version check (M11)


def _plist(versions: build_app.BundleVersions) -> dict[str, Any]:
    return build_app.info_plist(PINS, versions)


def _version_manifest(versions: build_app.BundleVersions, **build: Any) -> dict[str, Any]:
    return {
        "app": {
            "name": PINS.app_name,
            "bundle_id": PINS.bundle_id,
            "runtime_id": PINS.runtime_id,
            "deployment_target": PINS.deployment_target,
            "short_version": versions.short,
            "bundle_version": versions.build,
        },
        "build": {"tag": None, "build_number": None, **build},
        "host": {"version": "0.1.0.dev0"},
    }


def test_version_problems() -> None:
    versions = build_app.bundle_versions("0.1.0.dev0", None, "523")
    good = {
        "versions": versions,
        "host_version": "0.1.0.dev0",
        "bundled_version": "lucille-host 0.1.0.dev0\n",
        "pins": PINS,
        "tag": None,
        "build_number": "523",
    }
    manifest = _version_manifest(versions, build_number="523")
    assert check_app.version_problems(_plist(versions), manifest, **good) == []
    other = build_app.bundle_versions("0.1.0.dev0", None, "524")
    assert check_app.version_problems(_plist(other), manifest, **good)
    assert check_app.version_problems(_plist(versions), _version_manifest(other), **good)
    assert check_app.version_problems(
        _plist(versions), manifest, **{**good, "bundled_version": "lucille-host 0.1.0\n"}
    )
    assert check_app.version_problems(
        _plist(versions), manifest, **{**good, "host_version": "0.1.0"}
    )
    changed = copy.deepcopy(manifest)
    changed["app"]["runtime_id"] = "com.example.runtime"
    assert check_app.version_problems(_plist(versions), changed, **good)
    changed = copy.deepcopy(manifest)
    changed["build"]["tag"] = "v0.1.0"
    assert check_app.version_problems(_plist(versions), changed, **good)
    changed = copy.deepcopy(manifest)
    changed["build"]["build_number"] = "524"
    assert check_app.version_problems(_plist(versions), changed, **good)
    plist = {**_plist(versions), "LSMinimumSystemVersion": "14.0"}
    assert check_app.version_problems(plist, manifest, **good)

    tagged = build_app.bundle_versions("0.2.0", "v0.2.0", None)
    tag_manifest = _version_manifest(tagged, tag="v0.2.0")
    tag_manifest["host"]["version"] = "0.2.0"
    tag_args = {
        **good,
        "versions": tagged,
        "host_version": "0.2.0",
        "bundled_version": "lucille-host 0.2.0\n",
        "tag": "v0.2.0",
        "build_number": None,
    }
    assert check_app.version_problems(_plist(tagged), tag_manifest, **tag_args) == []


# ---------------------------------------------------------------------------------------------
# The environment check (M6), on this machine's own CPython


def _base_python() -> Path:
    """The interpreter running these tests, or, in a virtual environment, the one it was made
    from: the user site is off in a virtual environment, so the user-site controls need the base
    one. (On a copy of the app's runtime, which check_app.py suite runs, it is that runtime.)"""
    if sys.prefix == sys.base_prefix:
        return Path(sys.executable)
    base = Path(sys.base_prefix) / "bin" / f"python{sys.version_info[0]}.{sys.version_info[1]}"
    assert base.is_file(), f"no base interpreter at {base}"
    return base


def _stdlib(python: Path) -> Path:
    return python.parent.parent / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}"


def test_environment_vectors_each_have_a_control(tmp_path: Path) -> None:
    vectors = check_app.environment_vectors(
        tmp_path,
        stdlib=tmp_path / "stdlib" / "python3.13",
        home_user_site=tmp_path / "home" / ".local" / "lib" / "python3.13" / "site-packages",
        userbase=tmp_path / "userbase",
        userbase_user_site=tmp_path / "userbase" / "lib" / "python3.13" / "site-packages",
    )
    names = [vector.name for vector in vectors]
    assert names == [
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "the user site",
        "PYTHONUSERBASE",
        "the working folder",
    ]
    markers = [marker for vector in vectors for marker in vector.markers]
    assert len(markers) == len(set(markers)) >= len(vectors)
    for vector in vectors:
        assert vector.control and "-I" not in vector.control, vector.name
        assert vector.files, vector.name
        for path in vector.files:
            assert path.is_relative_to(tmp_path), path
    by_name = dict(zip(names, vectors, strict=True))
    assert set(by_name["PYTHONPATH"].env) == {"PYTHONPATH"}
    assert set(by_name["PYTHONHOME"].env) == {"PYTHONHOME"}
    assert set(by_name["PYTHONSTARTUP"].env) == {"PYTHONSTARTUP"}
    assert set(by_name["PYTHONUSERBASE"].env) == {"PYTHONUSERBASE"}
    assert by_name["the user site"].env == {}
    # The shadow package is found from the working folder only under -m, the way the app runs.
    assert by_name["the working folder"].cwd is not None
    assert by_name["the working folder"].control[:2] == ("-m", "lucille_host")
    # PYTHONSTARTUP is read only by an interactive interpreter; its control is one.
    assert by_name["PYTHONSTARTUP"].control == ("-i",)
    assert "-m" in by_name["PYTHONSTARTUP"].note
    assert any(path.name == "usercustomize.py" for path in by_name["the user site"].files)
    assert any(path.suffix == ".pth" for path in by_name["the user site"].files)
    assert any(path.suffix == ".pth" for path in by_name["PYTHONUSERBASE"].files)


def test_the_environment_check_on_a_real_interpreter(tmp_path: Path) -> None:
    python = _base_python()
    checker = check_app.Checker()
    check_app.environment_check(
        python, _stdlib(python), tmp_path, checker, [("nothing", ("-c", "pass"))]
    )
    assert checker.failures == []


def test_a_control_that_cannot_fire_fails_the_environment_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = _base_python()
    real = check_app.environment_vectors

    def misplaced(scratch: Path, **where: Path) -> list[check_app.EnvironmentVector]:
        vectors = real(scratch, **where)
        # The user site's files, planted where Python never looks.
        nowhere = scratch / "nowhere"
        vectors[3] = check_app.EnvironmentVector(
            name=vectors[3].name,
            env=vectors[3].env,
            files={nowhere / path.name: text for path, text in vectors[3].files.items()},
            links=vectors[3].links,
            markers=vectors[3].markers,
            control=vectors[3].control,
            cwd=vectors[3].cwd,
            note=vectors[3].note,
        )
        return vectors

    monkeypatch.setattr(check_app, "environment_vectors", misplaced)
    checker = check_app.Checker()
    check_app.environment_check(
        python, _stdlib(python), tmp_path, checker, [("nothing", ("-c", "pass"))]
    )
    assert len(checker.failures) == 1 and "the user site" in checker.failures[0]


def test_the_environment_check_sees_a_pycache_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PYTHONPYCACHEPREFIX would make the importer read .pyc files from another tree than the
    # bundle's. Run as the app runs it, the interpreter ignores it; this check sees the difference,
    # and it is set in the host runs, where the native probe asserts there is no prefix.
    python = _base_python()
    probe = ("the prefix", ("-c", "import sys; assert sys.pycache_prefix is None"))
    checker = check_app.Checker()
    check_app.environment_check(python, _stdlib(python), tmp_path / "a", checker, [probe])
    assert checker.failures == []
    # Without -I, and with no other vector, the check fails on the prefix, and so does the host
    # run that asserts there is none; the control still sees the variable in force.
    monkeypatch.setattr(build_app, "RUNTIME_FLAGS", ("-B", "-X", "utf8"))
    monkeypatch.setattr(check_app, "environment_vectors", lambda scratch, **where: [])
    checker = check_app.Checker()
    check_app.environment_check(python, _stdlib(python), tmp_path / "b", checker, [probe])
    assert checker.failures == [
        "PYTHONPYCACHEPREFIX: with -B -X utf8, the interpreter reads no .pyc file from it",
        "the prefix: with -B -X utf8, every variable set and the planted folder as the working "
        "folder, it runs and loads nothing planted",
    ]


# ---------------------------------------------------------------------------------------------
# The suite on a copy of the bundled runtime, start-up times, and the command line


def test_suite_reinstall_text() -> None:
    manifest = {
        "host": {"version": "0.1.0.dev0", "wheel": {"file": "h.whl", "sha256": "a" * 64}},
        "wheels": [
            {"name": "keyring", "version": "25.7.0", "sha256": "b" * 64},
            {"name": "agent-switchboard", "version": "2.6.0", "sha256": "c" * 64},
        ],
    }
    assert check_app.suite_reinstall_text(manifest) == (
        f"lucille-host==0.1.0.dev0 --hash=sha256:{'a' * 64}\n"
        f"agent-switchboard==2.6.0 --hash=sha256:{'c' * 64}\n"
    )
    del manifest["wheels"][1]
    with pytest.raises(check_app.CheckError):
        check_app.suite_reinstall_text(manifest)
    # The two console scripts the suite needs beside the interpreter (tests/test_cli.py,
    # tests/test_run_process.py).
    assert check_app.SUITE_SCRIPTS == ("lucille-host", "switchboard")


def test_suite_install_argv(tmp_path: Path) -> None:
    python = tmp_path / "lucille-host-runtime"
    first = check_app.suite_install_argv(python, tmp_path / "dev.txt")
    assert first[:3] == ["uv", "pip", "install"]
    assert first[first.index("--python") + 1] == str(python)
    # The copy is the suite's own, and not a virtual environment.
    assert "--break-system-packages" in first
    for flag in ("--require-hashes", "--no-deps", "--no-config", "--no-python-downloads"):
        assert flag in first
    # A pin with no wheel fails rather than being built by a backend nothing pins.
    assert first[first.index("--only-binary") + 1] == ":all:"
    assert "--no-index" not in first and "--reinstall-package" not in first
    # uv export reads no uv.toml either.
    assert "--no-config" in check_app.suite_export_argv()
    second = check_app.suite_install_argv(
        python,
        tmp_path / "again.txt",
        find_links=tmp_path / "wheels",
        reinstall=("lucille-host", "agent-switchboard"),
    )
    assert "--no-index" in second
    assert second[second.index("--find-links") + 1] == str(tmp_path / "wheels")
    assert [second[i + 1] for i, arg in enumerate(second) if arg == "--reinstall-package"] == [
        "lucille-host",
        "agent-switchboard",
    ]
    assert second[-2:] == ["-r", str(tmp_path / "again.txt")]


def _check_app_function(name: str) -> str:
    """The source of the function ``name`` in tools/macos/check_app.py."""
    import ast

    source = (build_app.TOOLS_DIR / "check_app.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            found = ast.get_source_segment(source, node)
            assert found is not None, name
            return found
    raise AssertionError(f"no function {name}")


def test_the_startup_check_times_the_bundle_and_a_copy_without_pyc() -> None:
    # The bundle as shipped, with the .pyc files the build wrote, then a writable copy with every
    # .pyc removed. Nothing is compiled on the runner (0002 §6).
    check = _check_app_function("check_startup")
    shipped = check.find('"checked-hash (as shipped)"')
    removed = check.find('"none (a copy with every .pyc removed)"')
    assert -1 < shipped < removed, check
    assert "rmtree" in check and "compileall" not in check


def test_the_record_check_admits_the_bytecode_the_build_writes() -> None:
    import ast

    calls = [
        node
        for node in ast.walk(ast.parse(_check_app_function("check_record")))
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "payload.record_problems"
    ]
    assert len(calls) == 1
    keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in calls[0].keywords}
    assert keywords == {"bytecode_tag": "build_app.BYTECODE_TAG"}


def test_startup_timing() -> None:
    assert check_app.timing([0.3, 0.1, 0.2]) == (200.0, 100.0)
    assert check_app.timing([0.4, 0.1, 0.2, 0.3]) == (250.0, 100.0)
    table = check_app.startup_table(
        {"no .pyc": {"--version": (200.0, 100.0), "status --json": (300.0, 250.0)}}
    )
    assert "| no .pyc | 200 | 100 | 300 | 250 |" in table


def test_every_new_check_is_a_choice() -> None:
    assert set(check_app.CHECKS) >= {
        "bundle",
        "bytecode",
        "record",
        "version",
        "wrapper",
        "environment",
        "readonly",
        "library-snapshot",
        "library-diff",
        "startup",
        "suite",
        "unpack-dmg",
        "selftest",
        "handoff",
        "handoff-check",
    }
    # The bytecode check needs the app and no option: a person can run it as it is, on a copy.
    assert "bytecode" not in check_app.NEEDS_NO_APP and "bytecode" not in check_app.REQUIRED


def _as_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")


@pytest.mark.parametrize(
    "args",
    [
        ["version"],
        # M10 compares MANIFEST.json's host wheel with the one the wheel step built.
        ["record"],
        ["library-diff"],
        ["library-snapshot"],
        ["suite"],
        ["selftest"],
        ["handoff"],
        ["handoff", "--run-id", "1", "--run-attempt", "1"],
        ["handoff-check", "--dmg-sha256", "a" * 64],
    ],
)
def test_a_check_without_its_arguments_is_a_usage_error(
    args: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_mac(monkeypatch)
    assert check_app.main([*args, "--out", str(tmp_path)]) == 2


def test_version_flags_are_exclusive_for_the_version_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        check_app.main(
            ["version", "--tag", "v0.1.0", "--build-number", "1", "--out", str(tmp_path)]
        )
    assert exited.value.code == 2
    assert "--build-number: not allowed with argument --tag" in capsys.readouterr().err


def test_unpack_dmg_and_handoff_check_need_no_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _as_mac(monkeypatch)
    out = tmp_path / "out"
    out.mkdir()
    assert check_app.main(["bundle", "--out", str(out)]) == 1
    assert f"{PINS.app_name}.app exists" in capsys.readouterr().out
    # unpack-dmg makes the app; with no disk image to unpack it says so, not that the app is
    # missing.
    assert check_app.main(["unpack-dmg", "--out", str(out), "--from", str(tmp_path / "x")]) == 1
    said = capsys.readouterr().out
    assert "exists" not in said and "disk image" in said
    dmg, selftest = _handoff(out / check_app.HANDOFF_DIR)
    args = ["handoff-check", "--out", str(out), "--dmg-sha256", dmg, "--selftest-sha256", selftest]
    assert check_app.main(args) == 0
    assert "exists" not in capsys.readouterr().out
    args[-1] = dmg
    assert check_app.main(args) == 1


def test_check_app_runs_on_a_mac_only(tmp_path: Path) -> None:
    if sys.platform == "darwin":
        pytest.skip("this is the refusal off a Mac")
    assert check_app.main(["handoff-check", "--out", str(tmp_path)]) == 2


def test_relocate_and_suite_leave_their_copies_removable(tmp_path: Path) -> None:
    tree = tmp_path / "copy"
    (tree / "a" / "b").mkdir(parents=True)
    (tree / "a" / "b" / "f").write_text("x", encoding="utf-8")
    for path in (tree / "a" / "b" / "f", tree / "a" / "b", tree / "a"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    link = tree / "link"
    link.symlink_to(tmp_path / "elsewhere")
    check_app.make_writable(tree)
    for path in (tree, tree / "a", tree / "a" / "b", tree / "a" / "b" / "f"):
        assert path.stat().st_mode & 0o200, path
    shutil.rmtree(tree)
    assert not tree.exists()


# ---------------------------------------------------------------------------------------------
# The copy in /Applications: install, and the checks that open it (decisions/0002 §13)


def test_the_checks_that_open_the_app_open_the_installed_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the copy at /Applications/<name>.app starts anything, so from install on every check
    # that opens the app opens that copy; placement is given the build copy, which it opens to see
    # it refuse, and finds the installed one itself.
    out = (tmp_path / "out").resolve()
    opened: list[tuple[str, Path]] = []
    for name in ("check_launch", "check_ui", "check_pairing", "check_placement"):

        def record(app: Path, *rest: Any, _name: str = name, **options: Any) -> None:
            opened.append((_name, app))

        monkeypatch.setattr(check_app, name, record)
    for check in ("launch", "ui", "pairing", "placement"):
        args = check_app._parser().parse_args([check, "--out", str(out)])
        check_app._run_check(args, out, check_app.Checker())
    installed = Path("/Applications") / f"{PINS.app_name}.app"
    assert check_app.installed_app_path() == installed
    assert opened == [
        ("check_launch", installed),
        ("check_ui", installed),
        ("check_pairing", installed),
        ("check_placement", check_app.app_path(out)),
    ]


def test_the_signature_check_covers_the_installed_copy_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The last signature step sees whether launch, ui or pairing wrote into the copy they opened
    # (M8): it checks the installed copy as well as the build copy, once install has put it there.
    # A copy another build left there, which no check opened, is only named.
    out = (tmp_path / "out").resolve()
    app = check_app.app_path(out)
    _a_build(app, "this build")
    installed = tmp_path / "Applications" / f"{PINS.app_name}.app"
    monkeypatch.setattr(check_app, "installed_app_path", lambda: installed)
    checked: list[Path] = []
    monkeypatch.setattr(check_app, "check_signature", lambda app, checker: checked.append(app))
    args = check_app._parser().parse_args(["signature", "--out", str(out)])
    check_app._run_check(args, out, check_app.Checker())
    assert checked == [app]
    assert str(installed) in capsys.readouterr().out
    shutil.copytree(app, installed)
    _a_build(installed, "an earlier build")
    check_app._run_check(args, out, check_app.Checker())
    assert checked == [app, app]
    assert f"{installed} is another build" in capsys.readouterr().out
    shutil.rmtree(installed)
    shutil.copytree(app, installed)
    check_app._run_check(args, out, check_app.Checker())
    assert checked == [app, app, app, installed]


def _a_build(app: Path, made: str) -> None:
    """Give ``app`` the files that tell one build of the app from another
    (``check_app.BUILD_IDENTITY``), each saying it is of the build ``made``."""
    for name in check_app.BUILD_IDENTITY:
        (app / name).parent.mkdir(parents=True, exist_ok=True)
        (app / name).write_text(f"{name}, of {made}", encoding="utf-8")


def _install_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, list[list[str]]]:
    """A build copy, an Applications folder and a folder to set a copy aside in, all under
    ``tmp_path``; ditto copies, and every other program it runs only says it ran. The copies of
    the app it stops, the folders it moves and the copies it removes are said too."""
    app = check_app.app_path(tmp_path / "out")
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "built").write_text("the build copy", encoding="utf-8")
    _a_build(app, "this build")
    installed = tmp_path / "Applications" / app.name
    installed.parent.mkdir()
    aside = tmp_path / "tmp" / f"set-aside-{app.name}"
    aside.parent.mkdir()
    monkeypatch.setattr(check_app, "installed_app_path", lambda: installed)
    monkeypatch.setattr(check_app, "set_aside_path", lambda: aside)
    ran: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        ran.append([str(word) for word in argv])
        if argv[0] == "ditto":
            shutil.copytree(argv[1], argv[2], symlinks=True)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("subprocess.run", run)

    def stop(checker: check_app.Checker, name: str) -> bool:
        ran.append(["stop", name])
        return True

    monkeypatch.setattr(check_app, "_stop_earlier_copies", stop)
    monkeypatch.setattr(
        check_app, "check_readonly", lambda app, checker: ran.append(["readonly", str(app)])
    )
    move, remove = check_app._move_folder, check_app._remove_copy

    def moved(source: Path, target: Path) -> None:
        ran.append(["move", str(source), str(target)])
        move(source, target)

    def removed(path: Path) -> None:
        ran.append(["remove", str(path)])
        remove(path)

    monkeypatch.setattr(check_app, "_move_folder", moved)
    monkeypatch.setattr(check_app, "_remove_copy", removed)
    return app, installed, aside, ran


def test_install_puts_the_read_only_bundle_in_applications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A copy of the read-only bundle, made read-only in its turn and then held to M8 (the read-only
    # check), at the one place the app runs from.
    app, installed, aside, ran = _install_world(tmp_path, monkeypatch)
    checker = check_app.Checker()
    check_app.check_install(app, checker)
    assert not checker.failures, checker.failures
    assert (installed / "Contents" / "built").read_text(encoding="utf-8") == "the build copy"
    copied = ran.index(["ditto", str(app), str(installed)])
    locked = ran.index(["chmod", "-R", "a-w", str(installed)])
    assert copied < locked < ran.index(["readonly", str(installed)])
    assert not aside.exists()


def test_install_refuses_a_copy_already_there_unless_asked_to_replace_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # On the owner's Mac /Applications may hold their own copy. install leaves it alone and says
    # how to go on; --replace sets it aside, says how to put it back, and installs; --restore puts
    # it back. Nothing is ever set aside over a copy set aside before.
    app, installed, aside, ran = _install_world(tmp_path, monkeypatch)
    (installed / "Contents").mkdir(parents=True)
    owners = installed / "Contents" / "owners"
    owners.write_text("the owner's copy", encoding="utf-8")
    checker = check_app.Checker()
    check_app.check_install(app, checker)
    assert checker.failures
    assert owners.read_text(encoding="utf-8") == "the owner's copy"
    assert not any(argv[0] == "ditto" for argv in ran) and not aside.exists()
    assert "--replace" in capsys.readouterr().out
    del ran[:]
    checker = check_app.Checker()
    check_app.check_install(app, checker, replace=True)
    assert not checker.failures, checker.failures
    assert (aside / "Contents" / "owners").read_text(encoding="utf-8") == "the owner's copy"
    assert (installed / "Contents" / "built").is_file() and not owners.exists()
    assert "--restore" in capsys.readouterr().out
    # No copy of the app runs while one is moved.
    set_aside = ran.index(["move", str(installed), str(aside)])
    assert (
        ran.index(["stop", PINS.app_name])
        < set_aside
        < ran.index(["ditto", str(app), str(installed)])
    )
    checker = check_app.Checker()
    check_app.check_install(app, checker, replace=True)
    assert checker.failures
    assert (aside / "Contents" / "owners").read_text(encoding="utf-8") == "the owner's copy"
    del ran[:]
    checker = check_app.Checker()
    check_app.check_install(app, checker, restore=True)
    assert not checker.failures, checker.failures
    assert owners.read_text(encoding="utf-8") == "the owner's copy"
    assert not (installed / "Contents" / "built").exists() and not aside.exists()
    assert ran == [
        ["stop", PINS.app_name],
        ["remove", str(installed)],
        ["move", str(aside), str(installed)],
    ]
    checker = check_app.Checker()
    check_app.check_install(app, checker, restore=True)
    assert checker.failures and owners.is_file()


def test_install_holds_this_build_again_and_restore_removes_only_this_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A hand run made again finds this build already in /Applications: install copies nothing,
    # and holds it to M8 again. With nothing set aside, install --restore puts back what was there
    # before install, which is nothing: it removes this build's copy, and no other copy.
    app, installed, aside, ran = _install_world(tmp_path, monkeypatch)
    checker = check_app.Checker()
    check_app.check_install(app, checker)
    assert not checker.failures, checker.failures
    del ran[:]
    checker = check_app.Checker()
    check_app.check_install(app, checker)
    assert not checker.failures, checker.failures
    assert ran == [["readonly", str(installed)]]
    assert "already holds this build" in capsys.readouterr().out
    del ran[:]
    checker = check_app.Checker()
    check_app.check_install(app, checker, restore=True)
    assert not checker.failures, checker.failures
    assert not installed.exists() and not aside.exists()
    assert ran == [["stop", PINS.app_name], ["remove", str(installed)]]
    checker = check_app.Checker()
    check_app.check_install(app, checker, restore=True)
    assert checker.failures
    shutil.copytree(app, installed)
    _a_build(installed, "an earlier build")
    for flags in ({}, {"restore": True}):
        checker = check_app.Checker()
        check_app.check_install(app, checker, **flags)
        assert checker.failures, flags
        assert (
            (installed / check_app.BUILD_IDENTITY[0])
            .read_text(encoding="utf-8")
            .endswith("of an earlier build")
        ), flags
    assert "only when it is this build" in capsys.readouterr().out


def test_a_copy_is_this_build_only_by_its_signed_files(tmp_path: Path) -> None:
    # A copy of the build is this build. One whose executable or signature differs is another
    # build, and so is one without them, or a link to the build.
    assert check_app.BUILD_IDENTITY == (
        f"Contents/MacOS/{PINS.app_name}",
        "Contents/_CodeSignature/CodeResources",
    )
    app = check_app.app_path(tmp_path / "out")
    _a_build(app, "this build")
    copy = tmp_path / "Applications" / app.name
    shutil.copytree(app, copy)
    assert check_app.is_this_build(copy, app)
    for number, name in enumerate(check_app.BUILD_IDENTITY):
        other = tmp_path / f"other-{number}" / app.name
        shutil.copytree(app, other)
        (other / name).write_text("another build", encoding="utf-8")
        assert not check_app.is_this_build(other, app), name
        (other / name).unlink()
        assert not check_app.is_this_build(other, app), name
    link = tmp_path / "link" / app.name
    link.parent.mkdir()
    link.symlink_to(app)
    assert not check_app.is_this_build(link, app)
    assert not check_app.is_this_build(tmp_path / "nothing" / app.name, app)


def test_a_read_only_copy_moves_to_another_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A copy an earlier install made read-only may not move to another folder, which rewrites its
    # "..": macOS refuses, and so does Linux for anyone but root, as this stand-in does for root
    # too. install --replace and --restore move it all the same, and it stays read-only.
    real_rename = Path.rename

    def rename(self: Path, target: Any) -> Path:
        if self.is_dir() and not self.is_symlink() and not self.stat().st_mode & stat.S_IWUSR:
            raise PermissionError(13, "Permission denied", str(self))
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    app, installed, aside, ran = _install_world(tmp_path, monkeypatch)
    (installed / "Contents").mkdir(parents=True)
    (installed / "Contents" / "owners").write_text("the owner's copy", encoding="utf-8")
    for path in (installed / "Contents" / "owners", installed / "Contents", installed):
        path.chmod(0o444 if path.is_file() else 0o555)
    try:
        checker = check_app.Checker()
        check_app.check_install(app, checker, replace=True)
        assert not checker.failures, checker.failures
        assert (aside / "Contents" / "owners").is_file()
        assert stat.S_IMODE(aside.stat().st_mode) == 0o555
        checker = check_app.Checker()
        check_app.check_install(app, checker, restore=True)
        assert not checker.failures, checker.failures
        assert (installed / "Contents" / "owners").is_file() and not aside.exists()
        assert stat.S_IMODE(installed.stat().st_mode) == 0o555
    finally:
        for path in (installed, aside):
            if path.exists():
                check_app.make_writable(path)


@pytest.mark.parametrize("check", sorted(check_app.OPENS_INSTALLED))
def test_the_checks_that_open_the_installed_copy_need_it_to_be_this_build(
    check: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # launch, ui, pairing and placement open /Applications/<name>.app. With no copy there, or the
    # copy an earlier build left there, they refuse before they open anything: what that copy did
    # would otherwise be reported as this build's.
    assert check_app.OPENS_INSTALLED == {"launch", "ui", "pairing", "placement"}
    _as_mac(monkeypatch)
    out = (tmp_path / "out").resolve()
    app = check_app.app_path(out)
    _a_build(app, "this build")
    installed = tmp_path / "Applications" / app.name
    monkeypatch.setattr(check_app, "installed_app_path", lambda: installed)
    ran: list[str] = []
    monkeypatch.setattr(check_app, "_run_check", lambda args, out, checker: ran.append(args.check))
    assert check_app.main([check, "--out", str(out)]) == 1
    assert "check_app.py install puts one there" in capsys.readouterr().out and not ran
    shutil.copytree(app, installed)
    _a_build(installed, "an earlier build")
    assert check_app.main([check, "--out", str(out)]) == 1
    assert "install --replace" in capsys.readouterr().out and not ran
    shutil.rmtree(installed)
    shutil.copytree(app, installed)
    assert check_app.main([check, "--out", str(out)]) == 0
    assert ran == [check]


def test_install_takes_replace_or_restore_not_both(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for flag in ("--replace", "--restore"):
        args = check_app._parser().parse_args(["install", flag, "--out", str(tmp_path)])
        assert (args.replace, args.restore) == (flag == "--replace", flag == "--restore")
    with pytest.raises(SystemExit) as exited:
        check_app._parser().parse_args(["install", "--replace", "--restore"])
    assert exited.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


# What a step's shell expands besides its job's and its own env: the files and folder GitHub sets
# for every step.
RUNNER_VARIABLES = frozenset({"GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY", "RUNNER_TEMP"})
TOOL_SCRIPTS = {"tools/macos/build_app.py": "build_app", "tools/macos/check_app.py": "check_app"}
# What a step that sets ``"$@"`` from the ref can pass: one flag or the other.
VERSION_CHOICES = (("--tag", "v1.2.3"), ("--build-number", "7"))
_VARIABLE = re.compile(r"\$\{?(\w+)\}?")


def _stand_in(word: str, known: set[str]) -> str:
    """``word`` with each variable in ``known`` replaced by a stand-in value."""
    return _VARIABLE.sub(lambda m: f"/stand-in/{m[1]}" if m[1] in known else m[0], word)


def tool_commands(doc: dict[Any, Any]) -> list[tuple[str, str, str, list[str]]]:
    """Each run of a tools/macos script in ``doc``'s ``run:`` blocks, as (job, step, script, argv).

    A variable the step defines (in its env, its job's env, or :data:`RUNNER_VARIABLES`) becomes
    a stand-in value; one it does not define stays as written, with its ``$``. ``"$@"`` becomes
    each of :data:`VERSION_CHOICES` when the step sets both, and stays as written otherwise.
    """
    commands = []
    for job_id, job in doc["jobs"].items():
        for number, step in enumerate(job.get("steps", [])):
            text = step.get("run")
            if not isinstance(text, str):
                continue
            name = step.get("name", f"step {number}")
            known = {*job.get("env", {}), *step.get("env", {}), *RUNNER_VARIABLES}
            sets_versions = 'set -- --tag "$REF_NAME"' in text and (
                'set -- --build-number "$RUN_NUMBER"' in text
            )
            for line in text.replace("\\\n", " ").splitlines():
                words = shlex.split(line, comments=True)
                scripts = [index for index, word in enumerate(words) if word in TOOL_SCRIPTS]
                if not scripts:
                    continue
                argv: list[str] = []
                for word in words[scripts[0] + 1 :]:
                    if word.startswith((">", "<", "|", "&", ";")):
                        break
                    argv.append(_stand_in(word, known))
                variants = [argv]
                if "$@" in argv and sets_versions:
                    at = argv.index("$@")
                    variants = [[*argv[:at], *flag, *argv[at + 1 :]] for flag in VERSION_CHOICES]
                script = TOOL_SCRIPTS[words[scripts[0]]]
                commands += [(job_id, name, script, variant) for variant in variants]
    return commands


def command_problems(doc: dict[Any, Any]) -> list[str]:
    """Why a tools/macos command in ``doc`` would stop at its own argument parsing, or run with
    an empty value. Empty when every one parses with every value the step can give it."""
    problems = []
    for job, step, script, argv in tool_commands(doc):
        where = f"{job} / {step}: {script} {' '.join(argv)}"
        empty = [word for word in argv if "$" in word]
        if empty:
            problems.append(f"{where}: {empty} would be empty or unexpanded")
            continue
        said = io.StringIO()
        try:
            with contextlib.redirect_stderr(said):
                if script == "build_app":
                    build_app._parser().parse_args(argv)
                else:
                    args = check_app._parser().parse_args(argv)
        except SystemExit:
            last = (said.getvalue().strip().splitlines() or ["refused"])[-1]
            problems.append(f"{where}: {last}")
            continue
        if script == "check_app" and check_app.missing_options(args):
            problems.append(f"{where}: needs {check_app.missing_options(args)}")
    return problems


def test_every_command_ci_runs_parses() -> None:
    doc = _ci()
    assert command_problems(doc) == []
    commands = tool_commands(doc)
    ran = {(script, argv[0]) for _, _, script, argv in commands}
    # Every check check_app.py has is a step of ci.yml, and every build_app.py command is used.
    assert {name for script, name in ran if script == "check_app"} == set(check_app.CHECKS)
    assert {name for script, name in ran if script == "build_app"} == {
        "toolchain",
        "plan",
        "wheel",
        "build",
        "assemble",
        "compare",
    }
    # Each step that passes "$@" was read both ways.
    versioned = [argv for _, _, _, argv in commands if "--tag" in argv or "--build-number" in argv]
    assert sum("--tag" in argv for argv in versioned) == sum(
        "--build-number" in argv for argv in versioned
    )
    assert len(versioned) == 2 * 4  # plan, build, version, the repeat assemble


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # A flag the command does not have.
        ('--host-wheel-sha256 "$HOST_WHEEL_SHA256"', '--wheel-sha256 "$HOST_WHEEL_SHA256"'),
        # A check that does not exist.
        ("check_app.py record --host", "check_app.py records --host"),
        # A check without an option it needs.
        ('record --host-wheel-sha256 "$HOST_WHEEL_SHA256" ', "record "),
        ('check_app.py handoff --run-id "$RUN_ID" ', "check_app.py handoff "),
        ("library-diff --before build/macos/library/before.json --control-run", "library-diff"),
        ("build_app.py wheel --out build/macos/host-wheel ", "build_app.py wheel "),
        # A variable the step does not define.
        ("          HOST_WHEEL: ${{ steps.wheel.outputs.path }}\n", ""),
        ('--copy-to "$RUNNER_TEMP/suite"', '--copy-to "$RUNNER_TMP/suite"'),
        # Both version flags at once, and "$@" in a step that never sets it.
        ('build_app.py plan "$@"', 'build_app.py plan --tag v1.2.3 "$@"'),
        ("check_app.py links", 'check_app.py version "$@"'),
    ],
)
def test_a_command_ci_cannot_run_is_reported(old: str, new: str) -> None:
    text = CI.read_text(encoding="utf-8")
    assert old in text
    assert command_problems(yaml.safe_load(text.replace(old, new)))
