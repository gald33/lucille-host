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
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from lucille_host._exec import CHILD_ENV

__all__ = [
    "CATALOG_VERSION",
    "DEFAULT_TIMEOUT_S",
    "MAX_TIMEOUT_S",
    "OPERATIONS",
    "OS_FAMILIES",
    "Operation",
    "args_valid",
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
    # Arguments that name a file to the program. "-" is rendered "./-": most programs read
    # standard input for an operand "-", even after "--".
    file_operands: tuple[str, ...] = ()

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
        child runs (the folder's real path, or ``/``), and the child's environment."""
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
        file_operands=file_operands,
    )


_ENTRIES = (
    # Runs no process. Its reply is the document of spec/PROTOCOL.md section 11.
    _entry("host.describe", _object([], {}), None, default_enabled=True),
    # Diagnostic: sleeps n seconds, to test waits, deploys and a closed lid. Its limit is n + 30 s.
    _entry(
        "host.sleep",
        _object(["n"], {"n": {"type": "integer", "minimum": 1, "maximum": 240}}),
        {"linux": ("/bin/sleep", "{n}"), "macos": ("/bin/sleep", "{n}")},
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
        file_operands=("path",),
    ),
)

OPERATIONS: Final[Mapping[str, Operation]] = MappingProxyType({op.name: op for op in _ENTRIES})

# Identifies this release's catalog: it changes whenever any entry does, its definition or whether a
# fresh install enables it.
CATALOG_VERSION: Final = _sha256_json(
    {
        op.name: {"definition": op.definition_hash, "default_enabled": op.default_enabled}
        for op in _ENTRIES
    }
)


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


_COMPILED: dict[str, dict[str, re.Pattern[str]]] = {}
for _op in _ENTRIES:
    _schema = _op.params()
    _check_schema(_op.name, _schema)
    _COMPILED[_op.name] = {
        key: _compile(sub["pattern"])
        for key, sub in _schema["properties"].items()
        if "pattern" in sub
    }


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
    patterns = _COMPILED[op.name]
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
