"""Workflow lint: CI runs on GitHub-hosted runners only, with every action and image pinned.

A release is only as trustworthy as the machine that built it and the code that ran there. These
rules apply to every workflow (``.github/workflows/*.yml`` and ``*.yaml``) and to every action
defined in this repository (any ``action.yml`` or ``action.yaml``). The rule name of each finding
is in brackets.

- [self-hosted] No string anywhere in a workflow or an action may contain ``self-hosted``, in any
  case: not in ``runs-on`` (a string or a list), and not in a matrix value ``runs-on`` could
  expand to.
- [runner] Every job that runs steps names exactly one ``runs-on`` label, and it is one of the
  GitHub-hosted image labels in :data:`HOSTED_LABELS`, written literally or as
  ``${{ matrix.<key> }}`` whose values, ``include`` entries among them, are all such labels. A
  runner group, any other expression, a custom label, or a list of several labels is refused,
  because each of those can select a self-hosted runner without saying ``self-hosted``. The list
  is exact: extend it when GitHub adds an image.
- [unpinned] Every action a step uses is ``<owner>/<repo>[/<path>]@<sha>``, where ``<sha>`` is a
  full 40-character lower-case hexadecimal commit SHA. A tag or a branch can be moved to different
  code after review; a commit cannot. Anything else a step ``uses:`` is refused, a local action
  (``./...``) and a Docker image (``docker://...``) included: no workflow here uses either, and
  allowing one is a change that brings its own check. A job's ``container`` and its ``services``
  are pinned by ``sha256`` digest. An action defined in this repository (an ``action.yml``) is
  still read and held to the same rules, for whoever uses it: its steps are pinned the same way,
  and a Docker action's image is pinned by ``sha256`` digest (a ``Dockerfile`` is refused,
  because it builds from whatever its ``FROM`` names).
- [reusable-workflow] A job may call a reusable workflow only from this repository
  (``./.github/workflows/...``), which is itself a workflow these rules check. A reusable
  workflow from elsewhere chooses its own runner, which this lint cannot see, and runs in this
  repository's context.

Files are parsed with PyYAML. A file that does not parse is a violation [parse].

What this lint cannot see: what a pinned action does when it runs, a JavaScript action's
dependencies, and repository settings such as which runners are registered.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

if __package__ in (None, ""):  # run as a script: make ``tools.guards`` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.guards._common import Violation, repo_files, run_guard  # noqa: E402

WORKFLOW_DIR = ".github/workflows/"
ACTION_FILES = ("action.yml", "action.yaml")
# The GitHub-hosted runner image labels this lint accepts. A label is either GitHub-hosted or
# custom; only an exact list keeps a custom label that looks hosted out.
HOSTED_LABELS = frozenset(
    {
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
)
SHA_PINNED = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+(/[^@\s]+)?@[0-9a-f]{40}$")
DIGEST_PINNED = re.compile(r"^(docker://)?[^@\s]+@sha256:[0-9a-f]{64}$")
MATRIX_VALUE = re.compile(r"^\$\{\{\s*matrix\.([A-Za-z0-9_\-]+)\s*\}\}$")
LOCAL_WORKFLOW = "./.github/workflows/"


def workflow_files(root: Path) -> list[str]:
    """The workflow files GitHub reads: YAML files directly in ``.github/workflows/``."""
    return [
        rel
        for rel in repo_files(root, untracked=True)
        if rel.startswith(WORKFLOW_DIR)
        and "/" not in rel[len(WORKFLOW_DIR) :]
        and rel.endswith((".yml", ".yaml"))
    ]


def action_files(root: Path) -> list[str]:
    """The actions defined in this repository: every ``action.yml`` or ``action.yaml``."""
    return [rel for rel in repo_files(root, untracked=True) if rel.split("/")[-1] in ACTION_FILES]


def lint_files(root: Path) -> list[str]:
    """Every file this lint reads."""
    return sorted({*workflow_files(root), *action_files(root)})


def _line_of(text: str, needle: str) -> int:
    """The first line holding ``needle`` (1-based), or 1. YAML parsing drops line numbers."""
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    return 1


def _strings(value: Any) -> Iterator[str]:
    """Every string in a parsed YAML document, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _matrix_values(job: dict[str, Any], key: str) -> list[Any] | None:
    """Every value ``matrix.<key>`` can take in this job, or None if that cannot be read."""
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if not isinstance(matrix, dict):
        return None
    values: list[Any] = []
    listed = matrix.get(key)
    if listed is not None:
        if not isinstance(listed, list):
            return None
        values.extend(listed)
    include = matrix.get("include") or []
    if not isinstance(include, list):
        return None
    for entry in include:
        if not isinstance(entry, dict):
            return None
        if key in entry:
            values.append(entry[key])
    return values or None


def _runner_problems(job: dict[str, Any]) -> list[str]:
    """Why this job's ``runs-on`` might not be a GitHub-hosted runner (empty if it must be)."""
    runs_on = job.get("runs-on")
    if runs_on is None:
        return [] if "uses" in job else ["the job has no runs-on"]
    if isinstance(runs_on, list):
        if len(runs_on) != 1:
            return [
                "runs-on lists several labels, which selects a runner carrying all of them; "
                "name one hosted image"
            ]
        runs_on = runs_on[0]
    if not isinstance(runs_on, str):
        return ["runs-on selects a runner group or labels by mapping; name a hosted image"]
    expanded: list[Any] = [runs_on]
    reference = MATRIX_VALUE.match(runs_on)
    if reference is not None:
        values = _matrix_values(job, reference.group(1))
        if values is None:
            return [f"runs-on {runs_on} does not expand to a literal matrix list"]
        expanded = values
    elif "${{" in runs_on:
        return [f"runs-on {runs_on} is an expression this lint cannot check"]
    return [
        f"runs-on {value!r} is not a GitHub-hosted runner image this lint knows"
        for value in expanded
        if not (isinstance(value, str) and value in HOSTED_LABELS)
    ]


def _uses_problem(uses: Any) -> str | None:
    """Why a step's ``uses:`` value is not an action pinned to a commit, or None if it is."""
    if not isinstance(uses, str):
        return f"uses: {uses!r} is not a string"
    if uses.startswith("./"):
        return f"{uses} is a local action; none is allowed yet"
    if uses.startswith("docker://"):
        return f"{uses} is a Docker image; none is allowed as a step yet"
    if SHA_PINNED.match(uses):
        return None
    return f"{uses} is not pinned to a full 40-character commit SHA"


def _image_problem(image: Any, where: str) -> str | None:
    """Why a container image is not pinned by digest, or None if it is."""
    if isinstance(image, str) and DIGEST_PINNED.match(image):
        return None
    return f"{where} image {image!r} is not pinned by sha256 digest"


def _job_images(job: dict[str, Any]) -> list[tuple[str, Any]]:
    """(where, image) for the job's container and each of its services."""
    images: list[tuple[str, Any]] = []
    container = job.get("container")
    if container is not None:
        image = container.get("image") if isinstance(container, dict) else container
        images.append(("container", image))
    services = job.get("services")
    if isinstance(services, dict):
        for name, service in services.items():
            image = service.get("image") if isinstance(service, dict) else service
            images.append((f"service {name}", image))
    elif services is not None:
        images.append(("services", services))
    return images


def _load(text: str, rel: str) -> dict[str, Any] | Violation:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark is not None else 1
        return Violation(rel, line, "parse", "the file is not valid YAML")
    if not isinstance(document, dict):
        return Violation(rel, 1, "parse", "the file is not a YAML mapping")
    return document


def _self_hosted(document: dict[str, Any], text: str, rel: str) -> list[Violation]:
    return [
        Violation(rel, _line_of(text, value), "self-hosted", f"{value!r} names self-hosted")
        for value in _strings(document)
        if "self-hosted" in value.lower()
    ]


def _steps_uses(steps: Any) -> list[Any]:
    if not isinstance(steps, list):
        return []
    return [step["uses"] for step in steps if isinstance(step, dict) and "uses" in step]


def check_workflow(text: str, rel: str) -> list[Violation]:
    """Check one workflow's text as if it were at ``rel``."""
    document = _load(text, rel)
    if isinstance(document, Violation):
        return [document]
    found = _self_hosted(document, text, rel)
    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        return sorted(set(found)) + [Violation(rel, 1, "parse", "the workflow has no jobs")]
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            found.append(Violation(rel, 1, "parse", f"job {job_id!r} is not a mapping"))
            continue
        job_line = _line_of(text, f"{job_id}:")
        for problem in _runner_problems(job):
            found.append(Violation(rel, job_line, "runner", f"{job_id}: {problem}"))
        if "uses" in job:
            uses = job["uses"]
            if not (isinstance(uses, str) and uses.startswith(LOCAL_WORKFLOW)):
                found.append(
                    Violation(
                        rel,
                        _line_of(text, str(uses)),
                        "reusable-workflow",
                        f"{job_id}: {uses!r} is a reusable workflow from outside this repository; "
                        "it chooses its own runner",
                    )
                )
        for uses in _steps_uses(job.get("steps")):
            problem = _uses_problem(uses)
            if problem is not None:
                found.append(Violation(rel, _line_of(text, str(uses)), "unpinned", problem))
        for where, image in _job_images(job):
            problem = _image_problem(image, f"{job_id}: {where}")
            if problem is not None:
                found.append(Violation(rel, _line_of(text, str(image)), "unpinned", problem))
    return sorted(set(found))


def check_action(text: str, rel: str) -> list[Violation]:
    """Check one action definition's text as if it were at ``rel``."""
    document = _load(text, rel)
    if isinstance(document, Violation):
        return [document]
    found = _self_hosted(document, text, rel)
    runs = document.get("runs")
    if not isinstance(runs, dict):
        return sorted(set(found)) + [Violation(rel, 1, "parse", "the action has no runs")]
    for uses in _steps_uses(runs.get("steps")):
        problem = _uses_problem(uses)
        if problem is not None:
            found.append(Violation(rel, _line_of(text, str(uses)), "unpinned", problem))
    if "image" in runs:
        problem = _image_problem(runs["image"], "the action's")
        if problem is not None:
            found.append(Violation(rel, _line_of(text, "image"), "unpinned", problem))
    return sorted(set(found))


def check(root: Path) -> list[Violation]:
    """Check every workflow and action under ``root``."""
    found: list[Violation] = []
    for rel in workflow_files(root):
        found.extend(check_workflow((root / rel).read_text(encoding="utf-8"), rel))
    for rel in action_files(root):
        found.extend(check_action((root / rel).read_text(encoding="utf-8"), rel))
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    """Script entry point: ``python tools/guards/workflow_lint.py [ROOT]``."""
    return run_guard("workflow_lint", sys.argv[1:] if argv is None else argv, lint_files, check)


if __name__ == "__main__":
    raise SystemExit(main())
