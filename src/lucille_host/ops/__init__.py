"""The closed set of operations this release of the host can run.

:data:`OPERATIONS` is the complete list. Lucille can ask the host to run an operation only if it is
named here; nothing Lucille sends can add an entry, and the host never runs a command line or code
it was sent. Each entry is a reviewed catalog definition: an argv template per OS whose first
element is an absolute executable, the operation's parameter schema, and its time limit. A new
operation means a new public release. The local policy file can only enable entries and map folder
aliases to paths.

The parameter schema of each entry is the operation's ``args`` schema in ``spec/PROTOCOL.md``
(section 10), with ``$ref`` resolved and descriptions left out; ``tests/test_catalog.py`` holds the
two equal. :func:`args_valid` checks a request's ``args`` against that schema by interpreting it,
so the check and the schema cannot drift apart. The interpreter knows only the keywords these
schemas use, and refuses at import any schema that uses another.

An argv template is a tuple of literal strings and placeholders. A placeholder is an element that
is exactly ``{name}`` for an argument ``name``; it is replaced by that argument's value as one
whole argv element, never spliced into a string. A string argument comes after ``--``. An integer
argument may come before it: it is rendered with ``str()`` from an integer the schema bounds below
by 1, so it cannot begin with ``-``. (``host.sleep`` needs that: macOS's ``/bin/sleep`` takes no
``--``.)

Each entry's :attr:`Operation.definition_hash` covers everything that decides what the entry does
when it is enabled. The local policy stores it beside each enabled entry, and an upgrade that
changes an enabled entry disables it until the user enables it again.

An entry is one of three kinds: an argv template (it runs a child), :class:`InProcess` (it calls a
reviewed module-level function in the host's own process), or neither (``host.describe``, whose
reply the host builds itself). An in-process entry is for an operation whose inputs cannot go in an
argv, such as a token held by the process that embeds the host. Its definition hash covers the
function's module and qualified name, and the name and SHA-256 of each source file the entry names
as on its path, its own module's among them, read when the entry is defined. A release that
changes any of those files turns an enabled entry off, as a changed argv does. What the hash does
NOT cover is any code the entry does not name: a module it imports and leaves out of ``files``, an
installed library and its version, ``lucille_host`` itself, the interpreter. Naming every file on
the path is the review's job; the host checks only that the function's own module is among them.

The function gets a :class:`Call` (read-only arguments, its time limit and deadline, and a ``stop``
it should poll) and returns a :class:`Result`; it raises :class:`TimedOut` when it ran past its
deadline and :class:`Stopped` when ``stop`` said so. The host runs it on a thread of its own and
bounds it whether or not it polls (``jobs._call_in_process``): past its deadline, or once killed or
abandoned, it gets a short grace to return, and then the host replies without it. A thread cannot
be killed, so a function that ignores ``stop`` runs on after that reply, and what it does then
still happens. No in-process entry takes a folder alias.

Each entry also carries what a shell shows of it (:class:`About`, ``spec/CLI.md`` section 8): a few
plain words for what it does, what Lucille gets when it runs, whether a reply can carry the
contents of a file, and whether it is meant for testing. The words, and whether it is meant for
testing, describe the entry and decide none of it, so they are outside the hash and outside
:data:`CATALOG_VERSION`: a release that only changes them turns no operation off. Whether a reply
can carry file contents is what a shell asks the user to acknowledge, so it is in the hash when
it is true.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from lucille_host._exec import CHILD_ENV

__all__ = [
    "CATALOG_VERSION",
    "About",
    "Call",
    "DEFAULT_TIMEOUT_S",
    "InProcess",
    "MAX_TIMEOUT_S",
    "OPERATIONS",
    "OS_FAMILIES",
    "Operation",
    "Result",
    "Stopped",
    "TimedOut",
    "args_valid",
    "catalog_version",
]

# The operating systems this release runs on, by the names host.describe reports.
OS_FAMILIES: Final = ("linux", "macos")

# A child's time limit: this by default, and never more than MAX_TIMEOUT_S.
DEFAULT_TIMEOUT_S: Final = 30
MAX_TIMEOUT_S: Final = 300

# The schema keywords args_valid interprets. A schema using any other keyword is refused at import.
_OBJECT_KEYWORDS = frozenset({"type", "additionalProperties", "required", "properties"})
_VALUE_KEYWORDS = frozenset({"type", "minimum", "maximum", "minLength", "maxLength", "pattern"})

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}", re.ASCII)


@dataclass(frozen=True)
class About:
    """What a shell shows of a catalog entry (``spec/CLI.md`` section 8)."""

    # A few plain words for what it does, at most 60 characters.
    summary: str
    # What Lucille gets when it runs, at most 300 characters.
    sends: str
    # Whether a reply can carry the contents of a file. A shell asks the user to acknowledge it
    # before it enables the entry. In the definition hash when true.
    sends_file_content: bool = False
    # Whether it is meant for testing; a shell may hide it.
    diagnostic: bool = False


@dataclass(frozen=True)
class Call:
    """What an in-process operation's function is given."""

    # The request's arguments, already checked against the entry's schema. Read-only.
    args: Mapping[str, Any]
    # The time limit in seconds, and when it runs out, by time.monotonic().
    timeout_s: int
    deadline: float
    # True once the user kills the operation or its room is abandoned: the function stops and
    # raises Stopped.
    stop: Callable[[], bool]


@dataclass(frozen=True)
class Result:
    """What an in-process operation returns: as a child's exit code and streams."""

    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


class TimedOut(Exception):  # noqa: N818 - the name is the outcome it reports
    """Raised by an in-process function that ran past its deadline: the reply is failed timeout."""


class Stopped(Exception):  # noqa: N818 - the name is the outcome it reports
    """Raised by an in-process function that stopped because its call's ``stop`` said so."""


@dataclass(frozen=True)
class InProcess:
    """A reviewed function the host calls in its own process, and the source files of its code path.

    ``function`` is a module-level function, so its module and qualified name pin it. ``files`` are
    the source files its code runs through, its own module's among them (a module gives its own as
    ``Path(__file__)``). Each is read and hashed here, when the entry is defined, so the hash
    describes the code the process loaded; the hash holds each file's name and digest, never where
    it is, so it is the same on every computer. Which files are on the path is the review's to
    check. The host checks that one file's path ends with the function's dotted module path
    (``a/b/c.py`` or ``a/b/c/__init__.py`` for ``a.b.c``); it cannot see the module's file itself
    without the reflection the runtime does not use."""

    function: Callable[[Call], Result]
    files: tuple[Path, ...]
    # The function's "module:qualname", and each file's name with the SHA-256 of its bytes.
    name: str = field(init=False)
    sources: tuple[tuple[str, str], ...] = field(init=False)

    def __post_init__(self) -> None:
        module = getattr(self.function, "__module__", None)
        qualname = getattr(self.function, "__qualname__", None)
        if not isinstance(module, str) or not isinstance(qualname, str):
            raise ValueError("an in-process operation is a named module-level function")
        if not qualname.isidentifier():
            raise ValueError("an in-process operation is a named module-level function")
        parts = module.split(".")
        endings = ((*parts[:-1], parts[-1] + ".py"), (*parts, "__init__.py"))
        if not any(
            path.parts[-len(ending) :] == ending for path in self.files for ending in endings
        ):
            raise ValueError(
                f"the files of {module}:{qualname} do not include its module, "
                f"{'/'.join(endings[0])}"
            )
        sources = []
        for path in self.files:
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                raise ValueError(f"{path.name} cannot be read to hash ({exc.strerror})") from None
            sources.append((path.name, digest))
        object.__setattr__(self, "name", f"{module}:{qualname}")
        object.__setattr__(self, "sources", tuple(sorted(sources)))


@dataclass(frozen=True)
class Operation:
    """One catalog entry."""

    name: str
    # The parameter schema, as canonical JSON text (read it with params()).
    params_json: str
    # The argv template per OS family, or None for an operation that runs no process.
    argv: Mapping[str, tuple[str, ...]] | None
    # The child's time limit: timeout_s, plus the value of the argument timeout_plus_arg, if named.
    timeout_s: int
    timeout_plus_arg: str | None
    # Whether a fresh install enables it.
    default_enabled: bool
    # What a shell shows of it. Only ``sends_file_content``, when true, is in the hash.
    about: About
    # Arguments that name a file to the program. "-" is rendered "./-": most programs read
    # standard input for an operand "-", even after "--".
    file_operands: tuple[str, ...] = ()
    # The function it calls in the host's own process, for an entry with no argv.
    in_process: InProcess | None = None

    def __post_init__(self) -> None:
        if self.in_process is None:
            return
        if self.argv is not None:
            raise ValueError(f"{self.name}: an entry runs a child or calls a function, not both")
        if self.file_operands or "root" in self.params()["properties"]:
            raise ValueError(f"{self.name}: an in-process operation takes no folder")

    def params(self) -> dict[str, Any]:
        """The parameter schema, as a fresh dict."""
        value: dict[str, Any] = json.loads(self.params_json)
        return value

    @property
    def runs_child(self) -> bool:
        return self.argv is not None

    @property
    def uses_root(self) -> bool:
        """Whether the operation works inside a folder alias: it takes ``root`` and ``path``."""
        return "root" in self.params()["properties"]

    def executable(self, os_family: str) -> str | None:
        """The absolute executable this operation runs on that OS, or None if it runs none."""
        if self.argv is None:
            return None
        return self.argv[os_family][0]

    def available(self, os_family: str) -> bool:
        """Whether this computer can run it: True for an operation with no process, else whether
        its executable is an executable regular file."""
        exe = self.executable(os_family)
        if exe is None:
            return True
        return os.path.isfile(exe) and os.access(exe, os.X_OK)

    def build_argv(self, os_family: str, args: Mapping[str, Any]) -> tuple[str, ...]:
        """The argv for valid ``args`` (see :func:`args_valid`), from this OS's template."""
        if self.argv is None:
            raise ValueError(f"{self.name} runs no process")
        built = []
        for element in self.argv[os_family]:
            match = _PLACEHOLDER.fullmatch(element)
            if match is None:
                built.append(element)
                continue
            key = match.group(1)
            value = args[key]
            if key in self.file_operands and value == "-":
                value = "./-"
            built.append(value if isinstance(value, str) else str(value))
        return tuple(built)

    def timeout(self, args: Mapping[str, Any]) -> int:
        """The child's time limit in seconds for valid ``args``."""
        extra = int(args[self.timeout_plus_arg]) if self.timeout_plus_arg is not None else 0
        return min(self.timeout_s + extra, MAX_TIMEOUT_S)

    @property
    def definition_hash(self) -> str:
        """SHA-256 over what decides what this entry does when enabled: its name, parameter
        schema, argv templates and file operands, its time limit and the cap on it, where its
        child runs (the folder's real path, or ``/``), and the child's environment; for an
        in-process entry, its function and each source file on its code path; and, when a
        reply can carry the contents of a file, that it can. The words a shell shows are not in
        it."""
        return _sha256_json(self._definition())

    def _definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "params": self.params(),
            "argv": None if self.argv is None else {k: list(v) for k, v in self.argv.items()},
            "file_operands": list(self.file_operands),
            "timeout_s": self.timeout_s,
            "timeout_plus_arg": self.timeout_plus_arg,
            "max_timeout_s": MAX_TIMEOUT_S,
            "cwd": None if self.argv is None else ("root" if self.uses_root else "/"),
            "child_env": None if self.argv is None else dict(CHILD_ENV),
            # Written only when true, so that the hashes of entries that send no file contents
            # are the ones they had before the field existed.
            **({"sends_file_content": True} if self.about.sends_file_content else {}),
            # Written only for an in-process entry, for the same reason.
            **(
                {}
                if self.in_process is None
                else {
                    "in_process": {
                        "function": self.in_process.name,
                        "sources": [list(pair) for pair in self.in_process.sources],
                    }
                }
            ),
        }


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("ascii")).hexdigest()


# The argument schemas, from spec/PROTOCOL.md section 10 (request.schema.json, $defs args.*).
_ROOT = {"type": "string", "minLength": 1, "maxLength": 32, "pattern": "^[a-z][a-z0-9_-]{0,31}$"}
_RELATIVE_PATH = {"type": "string", "minLength": 1, "maxLength": 1024, "pattern": "^[^\\u0000]*$"}


def _object(required: list[str], properties: dict[str, Any]) -> str:
    return _canonical(
        {
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": properties,
        }
    )


def _entry(
    name: str,
    params_json: str,
    argv: dict[str, tuple[str, ...]] | None,
    about: About,
    *,
    timeout_plus_arg: str | None = None,
    default_enabled: bool = False,
    file_operands: tuple[str, ...] = (),
) -> Operation:
    return Operation(
        name=name,
        params_json=params_json,
        argv=None if argv is None else MappingProxyType(argv),
        timeout_s=DEFAULT_TIMEOUT_S,
        timeout_plus_arg=timeout_plus_arg,
        default_enabled=default_enabled,
        about=about,
        file_operands=file_operands,
    )


_ENTRIES = (
    # Runs no process. Its reply is the document of spec/PROTOCOL.md section 11.
    _entry(
        "host.describe",
        _object([], {}),
        None,
        About(
            summary="Describe this computer's host",
            sends=(
                "Lucille gets the host's version and protocol, the operating system family, "
                "which release's catalog it has, which operations are on, the names you gave "
                "your folders (never where they are), and a fingerprint of the local policy."
            ),
        ),
        default_enabled=True,
    ),
    # Diagnostic: sleeps n seconds, to test waits, deploys and a closed lid. Its limit is n + 30 s.
    _entry(
        "host.sleep",
        _object(["n"], {"n": {"type": "integer", "minimum": 1, "maximum": 240}}),
        {"linux": ("/bin/sleep", "{n}"), "macos": ("/bin/sleep", "{n}")},
        About(
            summary="Wait up to 4 minutes, for testing",
            sends="Lucille gets how long the wait took and how it ended.",
            diagnostic=True,
        ),
        timeout_plus_arg="n",
    ),
    # The SHA-256 digest of one file under a folder alias. It shells out on purpose: the first
    # end-to-end run has to prove the exec path. The digest leaves the computer; the content does
    # not. The child runs with the root's realpath as its working directory.
    _entry(
        "files.sha256",
        _object(["root", "path"], {"root": _ROOT, "path": _RELATIVE_PATH}),
        {
            "linux": ("/usr/bin/sha256sum", "--", "{path}"),
            "macos": ("/usr/bin/shasum", "-a", "256", "--", "{path}"),
        },
        About(
            summary="Check a file's fingerprint",
            sends=(
                "Lucille gets a code that identifies the file's exact contents, not the contents."
            ),
        ),
        file_operands=("path",),
    ),
)

OPERATIONS: Final[Mapping[str, Operation]] = MappingProxyType({op.name: op for op in _ENTRIES})


def catalog_version(entries: Iterable[Operation]) -> str:
    """Identifies a catalog: it changes whenever any entry's definition, or whether a fresh install
    enables it, changes. The words a shell shows for an entry are outside it."""
    return _sha256_json(
        {
            op.name: {"definition": op.definition_hash, "default_enabled": op.default_enabled}
            for op in entries
        }
    )


# Identifies this release's catalog.
CATALOG_VERSION: Final = catalog_version(_ENTRIES)


def _check_schema(name: str, schema: Mapping[str, Any]) -> None:
    if set(schema) - _OBJECT_KEYWORDS or schema.get("type") != "object":
        raise RuntimeError(f"{name}: the parameter schema is not a closed object args_valid reads")
    if schema.get("additionalProperties") is not False:
        raise RuntimeError(f"{name}: the parameter schema must set additionalProperties false")
    for key, sub in schema["properties"].items():
        if set(sub) - _VALUE_KEYWORDS or sub.get("type") not in ("integer", "string", "boolean"):
            raise RuntimeError(f"{name}.{key}: a keyword or type args_valid does not interpret")
        pattern = sub.get("pattern")
        if pattern is not None and not (pattern.startswith("^") and pattern.endswith("$")):
            raise RuntimeError(f"{name}.{key}: a pattern must be anchored at both ends")
        if pattern is not None and "$" in pattern[:-1]:
            raise RuntimeError(f"{name}.{key}: a pattern may hold '$' only at its end")


def _compile(pattern: str) -> re.Pattern[str]:
    # JSON Schema patterns are ECMA-262, where '$' (no 'm' flag) matches only at the very end.
    # Python's '$' also matches before a final newline, so it becomes '\Z'.
    return re.compile(pattern[:-1] + r"\Z")


@functools.lru_cache(maxsize=64)
def _patterns(name: str, params_json: str) -> Mapping[str, re.Pattern[str]]:
    schema = json.loads(params_json)
    _check_schema(name, schema)
    return MappingProxyType(
        {
            key: _compile(sub["pattern"])
            for key, sub in schema["properties"].items()
            if "pattern" in sub
        }
    )


for _op in _ENTRIES:
    _patterns(_op.name, _op.params_json)


def args_valid(op: Operation, args: Any) -> bool:
    """Whether ``args`` holds to the operation's parameter schema.

    ``args`` is the value as JSON decoding gave it. Integers must be ``int``, never ``bool`` or
    ``float`` (spec rule ``integers``: ``1.0`` is not an integer here). String lengths count code
    points, as JSON Schema does.
    """
    if type(args) is not dict:
        return False
    schema = op.params()
    properties: dict[str, Any] = schema["properties"]
    if set(args) - set(properties) or set(schema["required"]) - set(args):
        return False
    patterns = _patterns(op.name, op.params_json)
    for key, value in args.items():
        sub = properties[key]
        kind = sub["type"]
        if kind == "boolean":
            if type(value) is not bool:
                return False
        elif kind == "integer":
            if type(value) is not int:
                return False
            if value < sub.get("minimum", value) or value > sub.get("maximum", value):
                return False
        elif kind == "string":
            if type(value) is not str:
                return False
            length = len(value)
            if length < sub.get("minLength", 0) or length > sub.get("maxLength", length):
                return False
            pattern = patterns.get(key)
            if pattern is not None and pattern.search(value) is None:
                return False
    return True
