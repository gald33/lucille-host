"""Synthetic in-process operations for tests/test_in_process.py: each is a module-level function, as
the catalog requires, and none of them touches anything outside the call it is given."""

from __future__ import annotations

import threading
import time

from lucille_host.ops import Call, Result, Stopped, TimedOut

# Set by a test to see, from outside, that a call has begun.
began = threading.Event()


def echo(call: Call) -> Result:
    """Reports back the one argument it was given."""
    return Result(stdout=f"said {call.args['word']}", stderr="")


def exits_two(call: Call) -> Result:
    return Result(exit_code=2, stdout="", stderr="no such thing")


def loud(call: Call) -> Result:
    """More output than any cap, with a token the redaction pass knows."""
    return Result(stdout="x" * 200 + " ghp_" + "a" * 36, stderr="")


def raises(call: Call) -> Result:
    raise ValueError("a message that may hold a secret: hunter2")


def not_a_result(call: Call) -> Result:
    return "done"  # type: ignore[return-value]


def bad_exit_code(call: Call) -> Result:
    return Result(exit_code=-9)


def bytes_out(call: Call) -> Result:
    return Result(stdout=b"raw")  # type: ignore[arg-type]


def times_out(call: Call) -> Result:
    raise TimedOut


def waits_for_stop(call: Call) -> Result:
    """Runs until its call says stop, then says so, as a well-behaved operation does."""
    began.set()
    limit = time.monotonic() + 10
    while not call.stop():
        if time.monotonic() > limit:
            return Result(stdout="never stopped")
        time.sleep(0.01)
    raise Stopped


# Every call `record` was given, for a test to read.
calls: list[Call] = []


def record(call: Call) -> Result:
    calls.append(call)
    return Result()
