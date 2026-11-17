"""In-process catalog operations: a reviewed function the host calls in its own process, run
under the same rules as a child (pause, drain, kill, abandonment, caps, the audit order), with a
definition hash that covers each source file its code path names.

The operations here are synthetic (``tests/_in_process.py``): this release's catalog has none.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from lucille_host import jobs, spec
from lucille_host.jobs import Abandoned, Job, Ready, check, execute, process, reply_body
from lucille_host.ops import CATALOG_VERSION, OPERATIONS, About, InProcess, Operation
from tests import _in_process
from tests._host import MakeContext

JOB_ID = "00000000-0000-4000-8000-000000000002"
OWN = (Path(_in_process.__file__),)
WORD_SCHEMA = json.dumps(
    {
        "additionalProperties": False,
        "properties": {
            "word": {"maxLength": 16, "minLength": 1, "pattern": "^[a-z]+$", "type": "string"}
        },
        "required": ["word"],
        "type": "object",
    },
    separators=(",", ":"),
    sort_keys=True,
)
EMPTY_SCHEMA = '{"additionalProperties":false,"properties":{},"required":[],"type":"object"}'
ABOUT = About(summary="A test operation", sends="Lucille gets a word back.")


def _op(function: Any, *, name: str = "test.echo", schema: str = EMPTY_SCHEMA) -> Operation:
    return Operation(
        name=name,
        params_json=schema,
        argv=None,
        timeout_s=30,
        timeout_plus_arg=None,
        default_enabled=False,
        about=ABOUT,
        in_process=InProcess(function, OWN),
    )


def _ready(op: Operation, args: dict[str, Any] | None = None) -> Ready:
    job = Job(job_id=JOB_ID, op=op.name, args=args or {})
    return Ready(job=job, operation=op, argv=None, cwd=None, timeout_s=op.timeout(job.args))


def _lines(ctx: jobs.Context) -> list[dict[str, Any]]:
    return [json.loads(line) for line in ctx.audit.path.read_text().splitlines()]


def _validate_reply(reply: dict[str, Any]) -> None:
    jsonschema.Draft202012Validator(spec.load_schema("reply")).validate(reply)


# --- what the entry is ----------------------------------------------------------------------------


def test_an_in_process_entry_runs_no_child_and_is_available_everywhere() -> None:
    op = _op(_in_process.echo, schema=WORD_SCHEMA)
    assert not op.runs_child
    assert op.executable("linux") is None
    assert op.available("linux") and op.available("macos")


def test_the_definition_hash_names_the_function_and_its_files() -> None:
    op = _op(_in_process.echo)
    definition = op._definition()["in_process"]
    assert definition["function"] == "tests._in_process:echo"
    digest = hashlib.sha256(OWN[0].read_bytes()).hexdigest()
    assert definition["sources"] == [["_in_process.py", digest]]
    # Never where the file is: the hash is the same on every computer.
    assert str(OWN[0].parent) not in json.dumps(definition)
    # Another function of the same module is another definition.
    assert _op(_in_process.exits_two).definition_hash != op.definition_hash


def test_the_definition_hash_covers_the_code_path(tmp_path: Path) -> None:
    """Changing the operation's own module, or a file it names as on its path, changes the hash:
    an upgrade that changes what an enabled in-process entry runs turns it off. The same bytes in
    another folder are the same hash."""
    own = tmp_path / "_in_process.py"
    helper = tmp_path / "helper.py"
    shutil.copy(OWN[0], own)
    helper.write_text("def shout(word):\n    return word.upper()\n")

    def entry_hash() -> str:
        entry = InProcess(_in_process.echo, (own, helper))
        return dataclasses.replace(_op(_in_process.echo), in_process=entry).definition_hash

    first = entry_hash()
    assert first != _op(_in_process.echo).definition_hash  # helper.py is on the path
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    shutil.copy(own, moved / own.name)
    shutil.copy(helper, moved / helper.name)
    entry = InProcess(_in_process.echo, (moved / own.name, moved / helper.name))
    assert dataclasses.replace(_op(_in_process.echo), in_process=entry).definition_hash == first
    own.write_text(own.read_text() + "\n# changed\n")
    second = entry_hash()
    assert second != first
    helper.write_text("def shout(word):\n    return word.lower()\n")
    assert entry_hash() != second


def test_only_a_named_module_level_function_can_be_an_entry(tmp_path: Path) -> None:
    def nested(call: Any) -> Any:
        return None

    for function in (nested, lambda call: None):
        with pytest.raises(ValueError, match="module-level function"):
            InProcess(function, OWN)
    # Its own module must be among the files: print's is not.
    with pytest.raises(ValueError, match="do not include its module"):
        InProcess(print, OWN)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="do not include its module"):
        InProcess(_in_process.echo, (tmp_path / "other.py",))
    with pytest.raises(ValueError, match="cannot be read"):
        InProcess(_in_process.echo, (*OWN, tmp_path / "missing.py"))


def test_an_entry_is_a_child_or_in_process_and_never_takes_a_folder() -> None:
    sha = OPERATIONS["files.sha256"]
    with pytest.raises(ValueError, match="both"):
        dataclasses.replace(sha, in_process=InProcess(_in_process.echo, OWN))
    with pytest.raises(ValueError, match="folder"):
        dataclasses.replace(
            sha, argv=None, file_operands=(), in_process=InProcess(_in_process.echo, OWN)
        )


def test_this_release_catalog_is_unchanged() -> None:
    """The kind is new; no entry of this release uses it, and no hash moved (test_catalog pins
    them)."""
    assert all(op.in_process is None for op in OPERATIONS.values())
    assert "in_process" not in OPERATIONS["host.describe"]._definition()
    assert CATALOG_VERSION == "06fe2f327b0916227970e377eaad94b064a7631bd7ff2696b3cef675bd607f35"


# --- running it -----------------------------------------------------------------------------------


def test_the_function_runs_and_its_result_is_the_reply(make_context: MakeContext) -> None:
    ctx = make_context()
    op = _op(_in_process.echo, schema=WORD_SCHEMA)
    outcome = execute(_ready(op, {"word": "hello"}), ctx)
    assert (outcome.status, outcome.exit_code, outcome.stdout) == ("done", 0, "said hello")
    assert (outcome.stderr, outcome.stdout_truncated, outcome.stderr_truncated) == (
        "",
        False,
        False,
    )
    _validate_reply(reply_body(JOB_ID, outcome, ctx.policy_digest))
    start, execution = _lines(ctx)
    assert start["kind"] == "start" and start["op"] == "test.echo"
    assert start["in_process"] == "tests._in_process:echo"
    assert start["definition_hash"] == op.definition_hash
    assert "executable" not in start and "argv" not in start
    assert (execution["kind"], execution["child"], execution["ended"]) == (
        "execution",
        False,
        "exited",
    )
    assert (execution["stdout_bytes"], execution["stderr_bytes"]) == (len("said hello"), 0)
    # Results go in the audit log only when the policy says so.
    assert "stdout" not in execution


def test_a_nonzero_exit_code_is_done_with_it(make_context: MakeContext) -> None:
    ctx = make_context(log_results=True)
    outcome = execute(_ready(_op(_in_process.exits_two)), ctx)
    assert (outcome.status, outcome.exit_code, outcome.stderr) == ("done", 2, "no such thing")
    assert _lines(ctx)[-1]["stderr"] == "no such thing"


def test_output_is_redacted_and_capped_as_a_child_s_is(make_context: MakeContext) -> None:
    ctx = make_context()
    job = Job(job_id=JOB_ID, op="test.loud", args={}, caps={"stdout_bytes": 64})
    op = _op(_in_process.loud, name="test.loud")
    outcome = execute(Ready(job=job, operation=op, argv=None, cwd=None, timeout_s=30), ctx)
    assert outcome.stdout_truncated is True
    assert len(json.dumps(outcome.stdout)) <= 64
    full = execute(_ready(op), ctx)
    assert "ghp_" not in (full.stdout or "") and "[redacted]" in (full.stdout or "")


def test_an_exception_is_exit_code_1_naming_only_its_type(make_context: MakeContext) -> None:
    ctx = make_context(log_results=True)
    for function, kind in (
        (_in_process.raises, "ValueError"),
        (_in_process.not_a_result, "TypeError"),
        (_in_process.bad_exit_code, "TypeError"),
        (_in_process.bytes_out, "TypeError"),
    ):
        outcome = execute(_ready(_op(function)), ctx)
        assert (outcome.status, outcome.exit_code) == ("done", 1)
        assert outcome.stderr == f"the operation raised {kind}"
        execution = _lines(ctx)[-1]
        assert (execution["ended"], execution["raised"]) == ("raised", kind)
        # The exception's message can hold anything the operation held: it goes nowhere.
        assert "hunter2" not in ctx.audit.path.read_text()


def test_timed_out_is_failed_timeout(make_context: MakeContext) -> None:
    ctx = make_context()
    outcome = execute(_ready(_op(_in_process.times_out)), ctx)
    assert (outcome.status, outcome.code) == ("failed", "timeout")
    _validate_reply(reply_body(JOB_ID, outcome, ctx.policy_digest))
    assert _lines(ctx)[-1]["ended"] == "timeout"


def test_the_call_carries_its_limit_and_read_only_args(make_context: MakeContext) -> None:
    ctx = make_context()
    op = dataclasses.replace(_op(_in_process.record, schema=WORD_SCHEMA), timeout_s=7)
    _in_process.calls.clear()
    before = time.monotonic()
    execute(_ready(op, {"word": "a"}), ctx)
    (call,) = _in_process.calls
    assert call.timeout_s == 7
    assert before + 7 <= call.deadline <= time.monotonic() + 7
    assert dict(call.args) == {"word": "a"}
    with pytest.raises(TypeError):
        call.args["word"] = "b"  # type: ignore[index]
    assert call.stop() is False


@pytest.mark.parametrize("control", ["pause", "drain", "abandon"])
def test_a_pause_drain_or_abandonment_after_the_decision_stops_the_call(
    make_context: MakeContext, control: str
) -> None:
    _in_process.calls.clear()
    ctx = make_context()
    op = _op(_in_process.record)
    if control == "pause":
        ctx.controls.pause()
    elif control == "drain":
        ctx.controls.draining.set()
    else:
        ctx = dataclasses.replace(ctx, stop=lambda: True)
    if control == "abandon":
        with pytest.raises(Abandoned):
            execute(_ready(op), ctx)
    else:
        outcome = execute(_ready(op), ctx)
        assert (outcome.status, outcome.code, outcome.duration_ms) == ("failed", "interrupted", 0)
    assert _in_process.calls == []
    (execution,) = _lines(ctx)
    stopped = {"pause": "paused", "drain": "draining", "abandon": "abandoned"}[control]
    assert (execution["started"], execution["stopped"]) == (False, stopped)


def test_a_kill_reaches_the_call_and_is_killed_locally(
    make_context: MakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_context()
    kills: list[bool] = []
    monkeypatch.setattr(ctx.controls, "kill_requested", lambda: _in_process.began.is_set())
    monkeypatch.setattr(ctx.controls, "kill_done", lambda: kills.append(True))
    _in_process.began.clear()
    outcome = execute(_ready(_op(_in_process.waits_for_stop)), ctx)
    assert (outcome.status, outcome.code) == ("failed", "killed_locally")
    assert kills == [True]
    assert _lines(ctx)[-1]["ended"] == "killed"


def test_an_abandoned_room_stops_a_running_call_with_no_reply(make_context: MakeContext) -> None:
    stop = threading.Event()
    _in_process.began.clear()
    ctx = dataclasses.replace(make_context(), stop=stop.is_set)
    done: list[Any] = []

    def run() -> None:
        try:
            done.append(execute(_ready(_op(_in_process.waits_for_stop)), ctx))
        except Abandoned as exc:
            done.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert _in_process.began.wait(10)
    stop.set()
    thread.join(10)
    assert len(done) == 1 and isinstance(done[0], Abandoned)
    execution = _lines(ctx)[-1]
    assert (execution["ended"], execution["abandoned"]) == ("killed", True)


def test_a_request_for_one_goes_through_every_check(
    make_context: MakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From the refusal order to the reply: unknown, not enabled and bad args as for any entry."""
    op = _op(_in_process.echo, schema=WORD_SCHEMA)
    monkeypatch.setattr(jobs, "OPERATIONS", {**OPERATIONS, op.name: op})
    ctx = make_context()
    assert check(Job(job_id=JOB_ID, op=op.name, args={"word": "a"}), ctx) == "not_enabled"
    ctx = dataclasses.replace(
        ctx, policy=dataclasses.replace(ctx.policy, enabled_ops=(*ctx.policy.enabled_ops, op.name))
    )
    assert check(Job(job_id=JOB_ID, op=op.name, args={"word": "A"}), ctx) == "bad_args"
    ready = check(Job(job_id=JOB_ID, op=op.name, args={"word": "a"}), ctx)
    assert isinstance(ready, Ready) and ready.argv is None and ready.cwd is None
    reply = json.loads(process(Job(job_id=JOB_ID, op=op.name, args={"word": "hi"}), ctx))
    _validate_reply(reply)
    assert (reply["status"], reply["stdout"]) == ("done", "said hi")
    kinds = [line["kind"] for line in _lines(ctx)]
    assert kinds == ["received", "decision", "start", "execution", "sent"]
