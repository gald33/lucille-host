"""Scenarios for what a reply may carry: its size, the paths it never holds, and the text a
failure never sends.

Each claim is one row of "Host invariants and their tests" in decisions/0001:

- "Output is bounded, decodable and path-free": a 1 MiB output comes back truncated and flagged,
  and the serialized reply stays under 256 KiB (:func:`output_bounded`); no reply contains a
  root's realpath or ``$HOME`` (:func:`replies_path_free`);
- "Failures cross the wire as codes only": a sentinel in what a refused request carries never
  appears in a sent body (:func:`failures_codes_only`).

They are registered after the scenarios of ``_scenarios.py``, all but its last,
``policy-unchanged``, which checks the policy after every other scenario and so is moved back to
the end once these are registered. :func:`output_bounded` and :func:`replies_path_free` turn
``files.search`` on and put the policy back as they found it, checked against the baseline digest,
before they return.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from lucille_host.conformance._scenarios import (
    REGISTRY,
    ROOT,
    STREAM_CAP,
    Context,
    check,
    expect_refused,
    measure,
    scenario,
)

# The reply cap of spec/PROTOCOL.md section 13: a serialized reply stays under it.
REPLY_CAP = 256 * 1024
# The output a search must exceed, as the invariant names it.
MIB = 1024 * 1024
SEARCH = "files.search"
# Text no request or file of the suite holds but the ones that carry it on purpose.
SENTINEL = "SENTINEL-c0nf0rmance-7f3a91"
# The same, in an operation name's form, so that the request reaches the catalog lookup.
OP_SENTINEL = "host.sentinel_c0nf0rmance_7f3a91"
# What to look for in a body: either, in any case.
MARKS = (SENTINEL.lower(), OP_SENTINEL)
FILL = "conformance-fill"


def _text(reply: dict[str, Any]) -> str:
    """A reply as text, every string in it as written (no escapes), to search it."""
    return json.dumps(reply, ensure_ascii=False)


@contextlib.contextmanager
def _search_on(ctx: Context) -> Iterator[None]:
    """``files.search`` on for the block, then off again, with the baseline digest back."""
    assert ctx.baseline is not None
    ctx.host.enable(SEARCH)
    try:
        yield
    finally:
        ctx.host.cli("ops", "disable", SEARCH)
    check(ctx.policy_digest() == ctx.baseline.digest, "the policy did not come back to baseline")


@contextlib.contextmanager
def _planted(folder: Path, files: dict[str, bytes]) -> Iterator[None]:
    """``files`` written in ``folder`` for the block, then removed."""
    folder.mkdir()
    try:
        for name, content in files.items():
            (folder / name).write_bytes(content)
        yield
    finally:
        for name in files:
            with contextlib.suppress(FileNotFoundError):
                (folder / name).unlink()
        folder.rmdir()


@scenario
def output_bounded(ctx: Context) -> None:
    """A files.search whose output passes 1 MiB comes back done, its stdout truncated, flagged
    and within the host's 64 KiB stream cap, and the serialized reply measures under 256 KiB:
    asked with no caps, and asked with caps far above the host's own, which must not raise it."""
    line = (FILL + " " + "x" * 100 + "\n").encode("ascii")
    # files.search returns at most 200 lines a file, so the output is spread over many files.
    files = {f"fill-{index:03}.txt": line * 200 for index in range(60)}
    check(sum(len(content) for content in files.values()) > MIB, "the planted output is small")
    args = {"root": ROOT, "pattern": FILL, "fixed_strings": True, "max_count": 200}
    high = {"stdout_bytes": 2147483647, "stderr_bytes": 2147483647}
    with _search_on(ctx), _planted(ctx.root / "bounded", files):
        replies = {
            "no caps": ctx.request(ctx.body(SEARCH, args)),
            "caps above the host's": ctx.request(ctx.body(SEARCH, args, caps=high)),
        }
    for asked, reply in replies.items():
        check(reply.get("status") == "done", f"{asked}: expected done, got {reply.get('status')}")
        check(reply.get("stdout_truncated") is True, f"{asked}: 1 MiB is not flagged truncated")
        for stream in ("stdout", "stderr"):
            size = measure(reply[stream])
            check(size <= STREAM_CAP, f"{asked}: {stream} measures {size}, past the host's cap")
        size = measure(reply)
        check(size < REPLY_CAP, f"{asked}: the reply measures {size} bytes, not under 256 KiB")


def _forbidden(ctx: Context) -> list[str]:
    """The paths no reply may hold: the root's and the host's home, as given and as real paths."""
    found = {str(ctx.root), os.path.realpath(ctx.root)}
    home = ctx.host.home
    found |= {str(home), os.path.realpath(home)}
    return sorted(found)


@scenario
def replies_path_free(ctx: Context) -> None:
    """No reply holds the root's path, its real path, or the host's `$HOME`: host.describe, a
    files.sha256 and a files.search that run in the root, and refusals of requests that name
    those paths themselves."""
    forbidden = _forbidden(ctx)
    replies = [
        ctx.request(ctx.body("host.describe")),
        ctx.request(ctx.body("files.sha256", {"root": ROOT, "path": "hello.txt"})),
    ]
    with _search_on(ctx):
        args = {"root": ROOT, "pattern": "hello", "fixed_strings": True, "max_count": 10}
        searched = ctx.request(ctx.body(SEARCH, args))
        check(searched.get("status") == "done", f"files.search was {searched.get('status')}")
        check("hello.txt" in searched.get("stdout", ""), "files.search found no hello.txt")
        replies.append(searched)
    for path in forbidden:
        named = os.path.join(path, "hello.txt")
        replies.append(
            expect_refused(
                ctx, ctx.body("files.sha256", {"root": ROOT, "path": named}), "path_outside_root"
            )
        )
    for reply in replies:
        text = _text(reply)
        for path in forbidden:
            check(path not in text, f"a {reply.get('status')} reply holds {path!r}")


@scenario
def failures_codes_only(ctx: Context) -> None:
    """Refused requests that carry a sentinel (in an op's name, of an op name's form and not,
    a path, a folder alias, an extra envelope key and an extra argument) are each refused with a
    code alone: the sentinel is in no body the host posted."""

    def holds(reply: dict[str, Any]) -> bool:
        text = _text(reply).lower()
        return any(mark in text for mark in MARKS)

    refusals = [
        (ctx.body(OP_SENTINEL), "unknown_op"),
        (ctx.body(f"host.{SENTINEL}"), "bad_request"),
        (ctx.body("host.sleep", {"n": 1}, note=SENTINEL), "bad_request"),
        (ctx.body("files.sha256", {"root": ROOT, "path": f"../{SENTINEL}"}), "path_outside_root"),
        (ctx.body("files.sha256", {"root": ROOT, "path": "hello.txt", SENTINEL: 1}), "bad_args"),
        (
            ctx.body("files.sha256", {"root": SENTINEL.lower(), "path": "hello.txt"}),
            "path_outside_root",
        ),
        (ctx.body("files.sha256", {"root": SENTINEL, "path": "hello.txt"}), "bad_args"),
    ]
    for body, code in refusals:
        reply = expect_refused(ctx, body, code)
        check(not holds(reply), f"a {code} refusal holds the sentinel")
    sent = ctx.lucille.replies(ctx.room)
    check(sent, "the host posted nothing")
    leaked = [r.get("job_id") for r in sent if holds(r)]
    check(not leaked, f"bodies the host posted hold the sentinel: {leaked}")


# policy-unchanged checks after every other scenario: keep it last.
LAST = "policy-unchanged"
REGISTRY.sort(key=lambda found: found.name == LAST)
