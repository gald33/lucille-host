"""tools/guards/workflow_lint.py reports each planted violation and passes the repo's workflows."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tools.guards import workflow_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "guards" / "workflow_lint.py"
REL = ".github/workflows/example.yml"
ACTION_REL = ".github/actions/setup/action.yml"

SHA = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "0" * 64

GOOD = textwrap.dedent(
    f"""\
    name: example
    on: [push, pull_request]
    permissions:
      contents: read
    jobs:
      test:
        runs-on: ${{{{ matrix.os }}}}
        strategy:
          matrix:
            os: [ubuntu-latest, macos-latest]
            include:
              - os: windows-2025
        steps:
          - uses: actions/checkout@{SHA} # v1.2.3
          - uses: github/codeql-action/init@{SHA}
          - run: echo hello
      lint:
        runs-on: [ubuntu-24.04]
        container: node@sha256:{DIGEST}
        services:
          db:
            image: postgres@sha256:{DIGEST}
        steps:
          - run: echo hello
      reuse:
        uses: ./.github/workflows/check.yml
    """
)

GOOD_ACTION = textwrap.dedent(
    f"""\
    name: setup
    runs:
      using: composite
      steps:
        - uses: actions/setup-python@{SHA} # v5.0.0
        - run: echo hello
          shell: bash
    """
)


def rules(text: str) -> set[str]:
    return {violation.rule for violation in workflow_lint.check_workflow(text, REL)}


def action_rules(text: str) -> set[str]:
    return {violation.rule for violation in workflow_lint.check_action(text, ACTION_REL)}


def job(runs_on: str, extra: str = "", uses: str = f"actions/checkout@{SHA}") -> str:
    """A one-job workflow with the given runs-on (a YAML fragment) and step action."""
    return (
        "on: push\n"
        "jobs:\n"
        "  build:\n"
        f"    runs-on: {runs_on}\n"
        f"{extra}"
        "    steps:\n"
        f"      - uses: {uses}\n"
    )


def test_good_workflow_is_clean() -> None:
    assert rules(GOOD) == set()


def test_good_action_is_clean() -> None:
    assert action_rules(GOOD_ACTION) == set()


SELF_HOSTED = [
    job("self-hosted"),
    job("[Self-Hosted, linux, x64]"),
    job("SELF-HOSTED"),
    job("[ubuntu-latest, self-hosted]"),
    job(
        "${{ matrix.os }}",
        "    strategy:\n      matrix:\n        os: [ubuntu-latest, self-hosted]\n",
    ),
    # Every form runs-on can take: a runner group and its labels, by mapping.
    job("{group: builders, labels: [self-hosted, linux]}"),
    job("{group: builders, labels: self-hosted}"),
    job("{group: self-hosted-pool}"),
    job("\n      group: builders\n      labels:\n        - self-hosted\n"),
]


@pytest.mark.parametrize("text", SELF_HOSTED)
def test_self_hosted_label_is_reported(text: str) -> None:
    assert "self-hosted" in rules(text)


NOT_HOSTED = [
    job("build-box"),
    job("[linux, x64]"),
    job("${{ vars.RUNNER }}"),
    job("{group: builders}"),
    job("${{ matrix.os }}"),
    job("${{ matrix.os }}", "    strategy:\n      matrix:\n        os: [ubuntu-latest, gpu-box]\n"),
    # A custom label shaped like a hosted image, alone or beside a hosted one.
    job("ubuntu-mybox"),
    job("windows-selfhost-box"),
    job("macos-99"),
    job("[ubuntu-latest, ubuntu-gpu]"),
    job("[ubuntu-latest, macos-latest]"),
    # A custom label added only through the matrix's include list.
    job(
        "${{ matrix.os }}",
        "    strategy:\n      matrix:\n        os: [ubuntu-latest]\n        include:\n"
        "          - os: big-box\n",
    ),
    "on: push\njobs:\n  build:\n    steps:\n      - run: echo hello\n",
    # A matrix value that is not a list, or an include that is not a list of mappings, cannot be
    # read, whatever else the matrix holds.
    job(
        "${{ matrix.os }}",
        "    strategy:\n      matrix:\n        os: gpu-box\n        include:\n"
        "          - os: ubuntu-latest\n",
    ),
    job(
        "${{ matrix.os }}",
        "    strategy:\n      matrix:\n        os: [ubuntu-latest]\n        include:\n"
        "          os: gpu-box\n",
    ),
    job(
        "${{ matrix.os }}",
        "    strategy:\n      matrix:\n        os: [ubuntu-latest]\n        include:\n"
        "          - gpu-box\n",
    ),
]


@pytest.mark.parametrize("text", NOT_HOSTED)
def test_runner_that_may_not_be_github_hosted_is_reported(text: str) -> None:
    assert "runner" in rules(text)


UNPINNED = [
    job("ubuntu-latest", uses="actions/checkout@v4"),
    job("ubuntu-latest", uses="actions/checkout@v4.2.2"),
    job("ubuntu-latest", uses="actions/checkout@main"),
    job("ubuntu-latest", uses="actions/checkout@0123456"),
    job("ubuntu-latest", uses="actions/checkout"),
    job("ubuntu-latest", uses="docker://alpine:3.20"),
    job("ubuntu-latest", "    container: node:latest\n"),
    job("ubuntu-latest", "    container:\n      image: node:22\n"),
    job("ubuntu-latest", "    services:\n      db:\n        image: postgres:latest\n"),
    # A digest must be a whole sha256: 64 hex digits.
    job("ubuntu-latest", uses="docker://alpine@sha256:abc"),
    job("ubuntu-latest", f"    container: node@sha256:{DIGEST[:-1]}\n"),
    job("ubuntu-latest", f"    container: node@sha256:{DIGEST}0\n"),
    # Forty characters that are not a commit SHA: not hexadecimal, a branch name, upper case.
    job("ubuntu-latest", uses="actions/checkout@" + "g" * 40),
    job("ubuntu-latest", uses="actions/checkout@release-2026-10-28-branch-for-the-pin-v1"),
    job("ubuntu-latest", uses="actions/checkout@feature/forty-characters-of-branch-name1"),
    job("ubuntu-latest", uses="actions/checkout@" + SHA.upper()),
    job("ubuntu-latest", uses=f"actions/checkout@{SHA[:-1]}"),
    job("ubuntu-latest", uses=f"actions/checkout@{SHA}0"),
    # A local action and a Docker image are refused outright, pinned or not.
    job("ubuntu-latest", uses="./.github/actions/local"),
    job("ubuntu-latest", uses="./"),
    job("ubuntu-latest", uses=f"docker://alpine@sha256:{DIGEST}"),
]


def test_the_branch_names_above_are_forty_characters() -> None:
    for ref in (
        "release-2026-10-28-branch-for-the-pin-v1",
        "feature/forty-characters-of-branch-name1",
    ):
        assert len(ref) == 40


@pytest.mark.parametrize("text", UNPINNED)
def test_action_or_image_not_pinned_is_reported(text: str) -> None:
    assert "unpinned" in rules(text)


REMOTE_REUSABLE = [
    "on: push\njobs:\n  reuse:\n    uses: example/workflows/.github/workflows/check.yml@v1\n",
    f"on: push\njobs:\n  reuse:\n    uses: example/workflows/.github/workflows/check.yml@{SHA}\n",
]


@pytest.mark.parametrize("text", REMOTE_REUSABLE)
def test_reusable_workflow_from_elsewhere_is_reported(text: str) -> None:
    assert "reusable-workflow" in rules(text)


UNPINNED_ACTIONS = [
    "runs:\n  using: composite\n  steps:\n    - uses: actions/setup-python@v5\n",
    "runs:\n  using: composite\n  steps:\n    - uses: some/thing@main\n",
    "runs:\n  using: composite\n  steps:\n    - uses: ./.github/actions/other\n",
    f"runs:\n  using: composite\n  steps:\n    - uses: docker://alpine@sha256:{DIGEST}\n",
    "runs:\n  using: docker\n  image: Dockerfile\n",
    "runs:\n  using: docker\n  image: docker://alpine:3.20\n",
]


@pytest.mark.parametrize("text", UNPINNED_ACTIONS)
def test_local_action_with_unpinned_step_or_image_is_reported(text: str) -> None:
    assert "unpinned" in action_rules(text)


def test_local_action_naming_self_hosted_is_reported() -> None:
    text = "runs:\n  using: composite\n  steps:\n    - run: echo self-hosted\n      shell: bash\n"
    assert "self-hosted" in action_rules(text)


@pytest.mark.parametrize("text", ["jobs: [unclosed\n", "- just\n- a list\n", "on: push\n"])
def test_unreadable_workflow_is_reported(text: str) -> None:
    assert "parse" in rules(text)


def test_repository_workflows_are_clean() -> None:
    files = workflow_lint.workflow_files(REPO_ROOT)
    assert ".github/workflows/ci.yml" in files
    assert workflow_lint.check(REPO_ROOT) == []


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def test_script_exit_codes(tmp_path: Path) -> None:
    assert _run_script(str(tmp_path)).returncode == 2  # no workflows: nothing to check
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(GOOD)
    (workflows / "check.yml").write_text(job("ubuntu-latest"))
    action = tmp_path / ".github" / "actions" / "local" / "action.yml"
    action.parent.mkdir(parents=True)
    action.write_text(GOOD_ACTION)
    result = _run_script(str(tmp_path))
    assert result.returncode == 0, result.stdout

    (workflows / "release.yaml").write_text(job("self-hosted"))
    result = _run_script(str(tmp_path))
    assert result.returncode == 1
    assert ".github/workflows/release.yaml:4: [self-hosted]" in result.stdout


def test_the_hosted_labels_are_pinned() -> None:
    assert workflow_lint.HOSTED_LABELS == {
        "ubuntu-latest",
        "ubuntu-24.04",
        "ubuntu-22.04",
        "ubuntu-24.04-arm",
        "ubuntu-22.04-arm",
        "macos-latest",
        "macos-15",
        "macos-14",
        "macos-15-intel",
        "macos-latest-large",
        "macos-15-large",
        "macos-14-large",
        "macos-latest-xlarge",
        "macos-15-xlarge",
        "macos-14-xlarge",
        "windows-latest",
        "windows-2025",
        "windows-2022",
        "windows-11-arm",
    }


@pytest.mark.parametrize("label", sorted(workflow_lint.HOSTED_LABELS))
def test_each_hosted_label_is_accepted(label: str) -> None:
    assert rules(job(label)) == set()


def test_script_reads_the_actions_defined_here(tmp_path: Path) -> None:
    # A local action may not be used, but an action.yml in the repository is still checked.
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(job("ubuntu-latest"))
    action = tmp_path / ".github" / "actions" / "setup" / "action.yml"
    action.parent.mkdir(parents=True)
    action.write_text("runs:\n  using: composite\n  steps:\n    - uses: some/thing@main\n")
    result = _run_script(str(tmp_path))
    assert result.returncode == 1
    assert ".github/actions/setup/action.yml:4: [unpinned]" in result.stdout
