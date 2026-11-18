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


# Set by a test to let `blocks` return.
release = threading.Event()


def blocks(call: Call) -> Result:
    """Never polls ``stop``: stands in for a call stuck in a read with no timeout."""
    release.wait(30)
    return Result(stdout="ran on")


def ignores_stop(call: Call) -> Result:
    time.sleep(0.1)
    return Result(stdout="ran on")


def sleeps_then_returns(call: Call) -> Result:
    time.sleep(1.3)
    return Result(stdout="late")


def exits_the_interpreter(call: Call) -> Result:
    raise SystemExit(3)


# What `big` writes, and the source of a child that writes the same: a token the redaction pass
# knows ahead of far more output than any cap, on both streams.
BIG_SIZE = 20 * 1024 * 1024
BIG_STDOUT = "ghp_" + "a" * 36 + " " + "x" * BIG_SIZE
BIG_STDERR = "sk-" + "b" * 30 + "\n" + "y" * BIG_SIZE
BIG_CHILD_SOURCE = (
    "import sys\n"
    f"sys.stdout.write('ghp_' + 'a' * 36 + ' ' + 'x' * {BIG_SIZE})\n"
    f"sys.stderr.write('sk-' + 'b' * 30 + chr(10) + 'y' * {BIG_SIZE})\n"
)


def big(call: Call) -> Result:
    """Output of 20 MiB on each stream, with a token in clear at the front of each."""
    return Result(stdout=BIG_STDOUT, stderr=BIG_STDERR)


HUGE = 1024 * 1024


def raises_named_for_a_token(call: Call) -> Result:
    """Raises a class whose own name is a token ahead of a huge run, with a message to match."""
    name = "ghp_" + "a" * 36 + "_" + "n" * HUGE
    raise type(name, (ValueError,), {})("sk-" + "b" * 30 + "m" * HUGE)


SECRET = "ghp_" + "c" * 36


class _ClassRaises(ValueError):
    """An exception whose ``__class__`` raises, with a token, when something asks the object."""

    @property  # type: ignore[misc]
    def __class__(self) -> type:  # type: ignore[override]
        raise RuntimeError(SECRET)


class _GetattributeRaises(ValueError):
    """An exception whose every attribute lookup raises, with a token."""

    def __getattribute__(self, name: str) -> object:
        raise RuntimeError(SECRET)


def raises_with_a_hostile_class(call: Call) -> Result:
    raise _ClassRaises("message")


def raises_with_a_hostile_getattribute(call: Call) -> Result:
    raise _GetattributeRaises("message")
