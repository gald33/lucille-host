"""Deciding a request (steps 5 to 9 of the refusal order) and the replies the host builds."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from lucille_host import __version__, spec
from lucille_host.jobs import (
    Draining,
    Job,
    Outcome,
    Ready,
    check,
    describe_document,
    encode,
    execute,
    process,
    reply_body,
)
from lucille_host.ops import CATALOG_VERSION
from tests._host import MakeContext

JOB_ID = "00000000-0000-4000-8000-000000000001"


def _validate_reply(reply: dict[str, Any]) -> None:
    jsonschema.Draft202012Validator(spec.load_schema("reply")).validate(reply)


def test_host_describe_document_in_stdout(tmp_path: Path, make_context: MakeContext) -> None:
    root = tmp_path / "private-folder-name"
    root.mkdir()
    ctx = make_context(enabled=["host.describe", "host.sleep"], roots={"docs": root})
    # A request's caps do not apply to the document, however small.
    job = Job(
        job_id=JOB_ID, op="host.describe", args={}, caps={"stdout_bytes": 2, "stderr_bytes": 2}
    )
    data = process(job, ctx)
    reply = json.loads(data)
    _validate_reply(reply)
    assert reply["status"] == "done" and reply["exit_code"] == 0
    assert reply["stderr"] == ""
    assert reply["stdout_truncated"] is False and reply["stderr_truncated"] is False
    document = json.loads(reply["stdout"])
    jsonschema.Draft202012Validator(spec.load_schema("host.describe")).validate(document)
    assert document == {
        "host_version": __version__,
        "protocol": "lucille.exec/1",
        "os_family": ctx.os_family,
        "catalog_version": CATALOG_VERSION,
        "enabled_ops": ["host.describe", "host.sleep"],
        "root_aliases": ["docs"],
        "policy_digest": ctx.policy_digest,
    }
    # Never a path: not the root's, nor its real path, nor $HOME.
    text = data.decode()
    for path in (str(root), os.path.realpath(root), str(Path.home())):
        assert path not in text


def test_unheld_root_is_path_outside_root(make_context: MakeContext) -> None:
    ctx = make_context(enabled=["files.sha256"], roots=["docs"])
    job = Job(job_id=JOB_ID, op="files.sha256", args={"root": "photos", "path": "a.txt"})
    assert check(job, ctx) == "path_outside_root"
    reply = json.loads(process(job, ctx))
    _validate_reply(reply)
    assert (reply["status"], reply["code"]) == ("refused", "path_outside_root")


def test_a_root_whose_folder_is_gone_is_path_outside_root(
    tmp_path: Path, make_context: MakeContext
) -> None:
    ctx = make_context(enabled=["files.sha256"], roots={"docs": tmp_path / "gone"})
    job = Job(job_id=JOB_ID, op="files.sha256", args={"root": "docs", "path": "a.txt"})
    assert check(job, ctx) == "path_outside_root"


@pytest.mark.parametrize(
    ("paused", "op", "enabled", "args", "roots", "expected"),
    [
        # Each case has two faults; the earlier step wins.
        (True, "files.delete", [], {}, [], "paused"),
        (True, "files.sha256", [], {"root": "x", "path": "/a"}, [], "paused"),
        (False, "files.delete", [], {"bogus": 1}, [], "unknown_op"),
        (False, "files.search", [], {"bogus": 1}, [], "unknown_op"),
        (False, "files.sha256", [], {"root": "docs", "path": ""}, ["docs"], "not_enabled"),
        (False, "files.sha256", ["files.sha256"], {"root": "nope", "path": ""}, [], "bad_args"),
        (
            False,
            "files.sha256",
            ["files.sha256"],
            {"root": "nope", "path": "a"},
            [],
            "path_outside_root",
        ),
        (
            False,
            "files.sha256",
            ["files.sha256"],
            {"root": "docs", "path": "../a"},
            ["docs"],
            "path_outside_root",
        ),
        (False, "host.sleep", ["host.sleep"], {"n": 1.0}, [], "bad_args"),
        (False, "host.describe", ["host.describe"], [], [], "bad_args"),
    ],
)
def test_steps_5_to_9_in_order(
    make_context: MakeContext,
    paused: bool,
    op: str,
    enabled: list[str],
    args: Any,
    roots: list[str],
    expected: str,
) -> None:
    ctx = make_context(enabled=enabled, roots=roots)
    if paused:
        ctx.controls.pause()
    assert check(Job(job_id=JOB_ID, op=op, args=args), ctx) == expected


def test_a_refusal_from_the_read_loop_is_passed_through(make_context: MakeContext) -> None:
    ctx = make_context(enabled=["host.describe"])
    job = Job(job_id=JOB_ID, op="host.describe", args={})
    reply = json.loads(process(job, ctx, earlier_code="expired"))
    _validate_reply(reply)
    assert (reply["status"], reply["code"]) == ("refused", "expired")


def test_replies_of_each_shape_hold_to_the_schema(make_context: MakeContext) -> None:
    digest = make_context().policy_digest
    shapes = [
        Outcome(status="refused", code="bad_args"),
        Outcome(
            status="done",
            exit_code=1,
            stdout="",
            stdout_truncated=False,
            stderr="x",
            stderr_truncated=False,
            duration_ms=3,
        ),
        Outcome(status="failed", code="timeout", duration_ms=30000),
        Outcome(
            status="failed",
            code="killed_locally",
            exit_code=-9,
            stdout="partial",
            stdout_truncated=False,
            stderr="",
            stderr_truncated=False,
            duration_ms=10,
        ),
    ]
    for outcome in shapes:
        body = reply_body(JOB_ID, outcome, digest)
        _validate_reply(body)
        assert json.loads(encode(body)) == body
        assert encode(body).isascii()
        assert body["host_version"] == __version__ and body["policy_digest"] == digest


def test_draining_starts_nothing_and_records_nothing(make_context: MakeContext) -> None:
    ctx = make_context(enabled=["host.sleep"])
    ctx.controls.draining.set()
    with pytest.raises(Draining):
        process(Job(job_id=JOB_ID, op="host.sleep", args={"n": 1}), ctx)
    assert not ctx.audit.path.exists()


def test_an_operation_this_computer_cannot_run_now_is_not_enabled(
    make_context: MakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_context(enabled=["host.describe", "host.sleep"])
    assert "host.sleep" in ctx.policy.enabled_ops
    monkeypatch.setattr(
        "lucille_host.ops.Operation.available", lambda self, family: self.name == "host.describe"
    )
    assert check(Job(job_id=JOB_ID, op="host.sleep", args={"n": 1}), ctx) == "not_enabled"
    # host.describe says the same: it lists what would run now, not what the policy loaded with.
    assert describe_document(ctx)["enabled_ops"] == ["host.describe"]


def test_a_start_that_fails_after_the_decision_is_failed_interrupted(
    tmp_path: Path, make_context: MakeContext
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("a")
    ctx = make_context(enabled=["files.sha256"], roots={"docs": root})
    job = Job(job_id=JOB_ID, op="files.sha256", args={"root": "docs", "path": "a.txt"})
    ready = check(job, ctx)
    assert isinstance(ready, Ready)
    # The folder goes away between the decision and the start.
    (root / "a.txt").unlink()
    root.rmdir()
    outcome = execute(ready, ctx)
    assert (outcome.status, outcome.code, outcome.duration_ms) == ("failed", "interrupted", 0)
    reply = reply_body(JOB_ID, outcome, ctx.policy_digest)
    _validate_reply(reply)
    execution = json.loads(ctx.audit.path.read_text().splitlines()[-1])
    assert (execution["kind"], execution["started"]) == ("execution", False)


def test_a_pause_after_the_decision_stops_the_start(
    tmp_path: Path, make_context: MakeContext
) -> None:
    marker = tmp_path / "the-child-ran"
    ctx = make_context(enabled=["host.sleep"])
    ready = check(Job(job_id=JOB_ID, op="host.sleep", args={"n": 1}), ctx)
    assert isinstance(ready, Ready)
    ready = dataclasses.replace(
        ready, argv=(sys.executable, "-c", f"open({str(marker)!r}, 'w').close()")
    )
    ctx.controls.pause()
    outcome = execute(ready, ctx)
    assert (outcome.status, outcome.code, outcome.duration_ms) == ("failed", "interrupted", 0)
    _validate_reply(reply_body(JOB_ID, outcome, ctx.policy_digest))
    assert not marker.exists()
    lines = [json.loads(line) for line in ctx.audit.path.read_text().splitlines()]
    assert [line["kind"] for line in lines] == ["execution"]
    assert (lines[0]["started"], lines[0]["paused"]) == (False, True)


def test_an_operation_with_no_root_runs_in_the_filesystem_root(make_context: MakeContext) -> None:
    ready = check(Job(job_id=JOB_ID, op="host.sleep", args={"n": 1}), make_context(["host.sleep"]))
    assert isinstance(ready, Ready) and ready.cwd == "/"


def test_received_is_written_before_deciding(
    make_context: MakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_context(enabled=["host.sleep"])
    seen: list[list[str]] = []
    real_check = check

    def spy(job: Job, context: Any) -> Any:
        seen.append([json.loads(line)["kind"] for line in ctx.audit.path.read_text().splitlines()])
        return real_check(job, context)

    monkeypatch.setattr("lucille_host.jobs.check", spy)
    process(Job(job_id=JOB_ID, op="host.sleep", args={"n": 1}), ctx)
    assert seen == [["received"]]
