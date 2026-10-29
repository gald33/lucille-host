"""Switchboard-surface lint: the host uses a small, fixed part of agent-switchboard.

The host reaches Lucille through agent-switchboard 2.6. It pairs, reads and posts messages, and
takes the rooms Lucille mints and offers over the pair. It must never make itself discoverable,
bring anyone else into its rooms, start a pairing or a heal, hand out its pair secret, or mint a
room of its own. These rules keep that visible in the source. The rule name of each finding is in
brackets.

- [switchboard-import] **One site.** In the runtime package (``src/lucille_host/`` except
  ``src/lucille_host/conformance/``), ``switchboard`` is imported or used only in
  ``src/lucille_host/_switchboard.py`` (that exact path), as far as this lint resolves names:
  through the file's imports, and through the wrapper. Reaching it through the wrapper counts:
  ``from lucille_host._switchboard import client``, ``_switchboard.pairing.accept``, or
  ``from lucille_host._switchboard import *``, is a use of ``switchboard`` in the module that
  writes it. A name taken from the wrapper is presumed to be
  part of ``switchboard`` unless the wrapper shows it is not (:func:`wrapper_bindings`; the
  whole-tree check reads the wrapper): a function or class the wrapper defines, or a constant it
  assigns, whose decorators, bases or value use no part of ``switchboard``, directly or through
  another such name; or a name the wrapper imports from another module, which stands for that
  module's name. So ``CLIENT = client.Client`` in the wrapper is ``switchboard``, and so is any
  name the wrapper binds in a way this lint does not classify (inside an ``if`` or a ``try``, say).
  The wrapper module used as a value counts too.
- [register] **No register, anywhere.** No reference to anything named ``register``, as a name or
  as an attribute (one a ``match`` class pattern reads, or a ``__match_args__`` entry names,
  included), called or not, and no string ``"register"`` passed to ``getattr``,
  ``setattr``, ``delattr``, ``hasattr``, ``operator.attrgetter`` or ``operator.methodcaller``, or
  used as a key of a ``**{...}`` literal. This is deliberately broad: it also catches unrelated
  calls such as ``atexit.register``, and code that needs one of those must find another way. A
  ``register`` call is how a Switchboard client announces itself on a hub, which the host never
  does.
- [invite] **No invites, anywhere.** No identifier, attribute (one a ``match`` class pattern reads
  included), import, keyword argument, parameter or ``except ... as`` name whose name contains
  ``invite`` (in any case), no such string
  in the places above, and no string literal containing the prefix of a Switchboard invite code.
  Function and class names are not checked, so a test may be named for what it checks. A test
  that must replace an invite entry point to prove it is never called patches it by its dotted
  path, as a string (``monkeypatch.setattr("switchboard.invite.from_env", fake)``), which this
  rule allows.

Outside ``src/lucille_host/conformance/`` and ``tests/`` (so in the runtime, the wrapper
included, and in ``tools/``):

- [share-new] **No minting rooms.** No ``share_new``, as a call, an attribute, an import or a
  string in the places above.
- [sharing-member] **Only the sharing calls that take an offered room.** No member of
  ``switchboard.sharing`` other than the calls ``touch``, ``accept``, ``resolve_room`` and
  ``leave``, and the types they take and return, which make no hub call: ``ShareStore``,
  ``Store``, ``SharingError``, ``Touched``, ``Kept``, ``Left`` and ``SharedRoom``. That holds
  however ``sharing`` is imported (``import switchboard.sharing as s``, ``from switchboard import
  sharing``, ``from switchboard.sharing import touch``). The module itself may not be used as a
  value (``getattr(sharing, ...)``), and an attribute chain with a ``sharing`` component counts as
  the module, so a re-export (``_switchboard.sharing.x``) is checked the same way; do not name
  unrelated things ``sharing``.
- [switchboard-surface] **Only the parts of Switchboard the host needs.** Of ``switchboard``,
  only the submodules and members in :data:`SURFACE` may be imported or used: ``pairing``
  (decoding a code, accepting, resolving, forgetting and reading the status of a pair; the store,
  record and result types, ``PendingHandshake`` and ``VERDICT_KEEP`` among them, which a store
  of the host's own must name; ``CHANNEL``, the name of the pair room's channel the library posts
  its notes on, which the read loop reads; and ``message_tag``, the ``TAG_PAIRED``,
  ``TAG_MOVED``, ``TAG_ENDED`` and ``TAG_SHARED`` constants and ``hub_errors``, which tell the
  library's notes and the hub's errors apart without a hub call; never ``start``, ``collect``,
  ``heal``, ``export`` or ``from_secret``),
  ``client`` (``Client`` and its errors), ``sharing`` (above), ``signing``
  (``SigningIdentity``), ``crypto`` (its errors) and ``config`` (``ClientConfig``). The CLI, the
  MCP bridge, invites, rendezvous, the hub server and every other submodule are refused. No
  submodule, nor the package, may be used as a value.

The conformance suite and the tests are exempt from these three rules, and only these, because
their fake Lucille mints and offers rooms the way Lucille does.

In the runtime package:

- [private-member] No attribute whose name starts with a single underscore, except directly on
  ``self`` or ``cls`` (``self._data``, not ``self.hub._call``) and directly on one of this
  package's own modules (``lucille_host._exec._run``, not ``_switchboard.CLIENT._call``), and no
  such name imported from a module outside this package (``from tempfile import _os``). A
  private member of a library object, such as ``Client._call``, which posts to any hub path,
  would get past every rule above.

What this lint cannot see. It checks names written in the source, resolved through the file's
imports and through the wrapper; a module re-exported by another module is not followed (the AST
lint's import allowlist keeps ``switchboard`` out of every other runtime module, and this lint's
one-site rule reports it there). It does not follow a method
called on an object, such as a ``Client`` the wrapper built, or a ``ShareStore``'s ``save``: of
those it refuses only ``register``, anything with ``invite`` in its name, and private members.
It checks a member of a Switchboard submodule (``pairing.PairingCode``), not the members of that
member (``PairingCode.decode``), because the same method on an instance would not be seen
either. A value a wrapper function returns is not followed, so a wrapper function that returns a
``Client`` hands the runtime a ``Client``. A name built at run time and passed to ``getattr`` is
not seen either. Review covers the rest.
"""

from __future__ import annotations

import ast
import functools
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make ``tools.guards`` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.guards._common import (  # noqa: E402
    OWN_PACKAGE,
    REPO_ROOT,
    Ref,
    Violation,
    absolute_module,
    anchored,
    import_alias_conflicts,
    matches,
    own_modules,
    package_of,
    parse,
    python_files,
    references,
    run_guard,
)

RUNTIME_PREFIX = "src/lucille_host/"
CONFORMANCE_PREFIX = "src/lucille_host/conformance/"
SWITCHBOARD_MODULE = "src/lucille_host/_switchboard.py"
WRAPPER = "lucille_host._switchboard"
# Where the fake Lucille lives: exempt from the share-new, sharing-member and surface rules only.
SURFACE_EXEMPT_PREFIXES = (CONFORMANCE_PREFIX, "tests/")

SHARING = "switchboard.sharing"
# The parts of agent-switchboard 2.6 the host may use: submodule -> the members it may name.
SURFACE: Mapping[str, frozenset[str]] = {
    "pairing": frozenset(
        {
            "PairingCode",
            "accept",
            "resolve",
            "forget",
            "status",
            "PairStore",
            "Store",
            "PairRecord",
            "PendingHandshake",
            "VERDICT_KEEP",
            "Resolution",
            "Accepted",
            "Forgotten",
            "PairingError",
            "AlreadyRedeemed",
            "message_tag",
            "CHANNEL",
            "TAG_PAIRED",
            "TAG_MOVED",
            "TAG_ENDED",
            "TAG_SHARED",
            "hub_errors",
        }
    ),
    "client": frozenset({"Client", "SwitchboardError", "ReadOnlyRoom"}),
    "sharing": frozenset(
        {
            "touch",
            "accept",
            "resolve_room",
            "leave",
            "ShareStore",
            "Store",
            "SharingError",
            "Touched",
            "Kept",
            "Left",
            "SharedRoom",
        }
    ),
    "signing": frozenset({"SigningIdentity"}),
    "crypto": frozenset({"CryptoError", "DecryptionError"}),
    "config": frozenset({"ClientConfig"}),
}
# Submodules and subpackages of agent-switchboard 2.6.0, so that a finding can name the submodule
# when the wrapper is not read. The list need not be complete: a name taken from the wrapper that is
# not here, and that the wrapper does not show to be its own, is presumed to be switchboard anyway.
SWITCHBOARD_SUBMODULES = (
    "auth",
    "bridge_links",
    "cli",
    "client",
    "config",
    "crypto",
    "desktop",
    "drill",
    "guidance",
    "handoff",
    "holds",
    "invite",
    "knownrooms",
    "load",
    "mcp_server",
    "notify",
    "pairing",
    "peers",
    "rendezvous",
    "rooms",
    "scripts",
    "server",
    "sharing",
    "signing",
    "skill",
    "spec",
    "stash",
    "store",
    "testing",
    "timing",
    "writekey",
)
DEFAULT_WRAPPER_BINDINGS: Mapping[str, str] = {
    "switchboard": "switchboard",
    **{name: f"switchboard.{name}" for name in SWITCHBOARD_SUBMODULES},
}
# What a name taken from the wrapper stands for when the wrapper does not show it is its own.
PRESUMED = "switchboard"
WATCHED = {"switchboard": "switchboard", "sharing": SHARING}

# Calls that turn a string into an attribute lookup.
ATTRIBUTE_BY_STRING = frozenset(
    {"getattr", "setattr", "delattr", "hasattr"}
    | {f"builtins.{name}" for name in ("getattr", "setattr", "delattr", "hasattr")}
    | {"operator.attrgetter", "operator.methodcaller"}
)

# The prefix of a Switchboard invite code, built from two literals so this file passes its own
# check.
CODE_PREFIX = "swb1" + "_"


def _is_runtime(rel: str) -> bool:
    return rel.startswith(RUNTIME_PREFIX) and not rel.startswith(CONFORMANCE_PREFIX)


def _module_scope(tree: ast.Module) -> tuple[Counter[str], dict[str, set[str]], bool]:
    """The module's own scope (not inside a function, class or lambda body): how many times each
    name is bound, what each name an import binds stands for, and whether there is a ``*`` import.
    A ``global`` declaration anywhere counts as one more binding of its names."""
    bound: Counter[str] = Counter()
    imported: dict[str, set[str]] = {}
    star = False
    package = package_of(SWITCHBOARD_MODULE)

    def visit(node: ast.AST) -> None:
        nonlocal star
        if isinstance(node, ast.Import | ast.ImportFrom):
            module = (
                absolute_module(node.module, node.level, package)
                if isinstance(node, ast.ImportFrom)
                else ""
            )
            for alias in node.names:
                if alias.name == "*":
                    star = True
                    continue
                if isinstance(node, ast.ImportFrom):
                    name, target = alias.asname or alias.name, f"{module}.{alias.name}"
                elif alias.asname:
                    name, target = alias.asname, alias.name
                else:
                    name = target = alias.name.split(".")[0]
                bound[name] += 1
                imported.setdefault(name, set()).add(target)
            return
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bound[node.name] += 1
            for field, value in ast.iter_fields(node):
                if field != "body":
                    for child in value if isinstance(value, list) else [value]:
                        if isinstance(child, ast.AST):
                            visit(child)
            return
        if isinstance(node, ast.Lambda):
            visit(node.args)
            return
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            bound[node.id] += 1
        elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar) and node.name:
            bound[node.name] += 1
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound[node.rest] += 1
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            bound.update(node.names)
    return bound, imported, star


def _loaded(nodes: Iterable[ast.AST]) -> set[str]:
    """Every name read anywhere inside ``nodes``."""
    return {
        sub.id
        for node in nodes
        for sub in ast.walk(node)
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
    }


def _stored_names(target: ast.expr) -> list[str]:
    """The names an assignment target binds (none for an attribute or an item)."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Tuple | ast.List):
        return [name for element in target.elts for name in _stored_names(element)]
    if isinstance(target, ast.Starred):
        return _stored_names(target.value)
    return []


def _own_candidates(tree: ast.Module) -> dict[str, list[set[str]]]:
    """Each name a top-level ``def``, ``class`` or assignment binds, with the names that decide
    what it is: decorators, bases and keywords, or the assigned value."""
    candidates: dict[str, list[set[str]]] = {}
    for stmt in tree.body:
        names: list[str] = []
        deciding: set[str] = set()
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            names, deciding = [stmt.name], _loaded(stmt.decorator_list)
        elif isinstance(stmt, ast.ClassDef):
            keywords = [keyword.value for keyword in stmt.keywords]
            names = [stmt.name]
            deciding = _loaded([*stmt.decorator_list, *stmt.bases, *keywords])
        elif isinstance(stmt, ast.Assign):
            names = [name for target in stmt.targets for name in _stored_names(target)]
            deciding = _loaded([stmt.value])
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            names, deciding = _stored_names(stmt.target), _loaded([stmt.value])
        elif isinstance(stmt, ast.AugAssign):
            names = _stored_names(stmt.target)
            deciding = _loaded([stmt.value]) | set(names)
        for name in names:
            candidates.setdefault(name, []).append(deciding)
    return candidates


def wrapper_bindings(source: str | bytes) -> dict[str, str]:
    """What each name the wrapper binds stands for, on top of :data:`DEFAULT_WRAPPER_BINDINGS`.

    - A name only imports bind stands for what it imports: a part of ``switchboard``, or another
      module's name.
    - A function or class the wrapper defines, or a constant it assigns, at top level, stands for
      itself (``lucille_host._switchboard.<name>``) when every binding of the name is such a
      statement and none of their decorators, bases, keywords or values reads a name that is
      ``switchboard`` or presumed to be.
    - Every other name the wrapper binds is presumed to be ``switchboard``, as is a name it does
      not bind at all (see :func:`_names`).
    """
    bindings = dict(DEFAULT_WRAPPER_BINDINGS)
    tree = parse(source, SWITCHBOARD_MODULE)
    if not isinstance(tree, ast.Module):
        return bindings
    bound, imported, star = _module_scope(tree)
    candidates = _own_candidates(tree)
    other_modules = {
        name
        for name, targets in imported.items()
        if bound[name] == len(targets) == 1
        and not any(matches(target, "switchboard") for target in targets)
    }
    own = {name for name, decided in candidates.items() if len(decided) == bound[name]}

    def presumed(name: str) -> bool:
        if bound[name] == 0:
            return star  # a builtin, unless a * import could have bound it
        return name not in own and name not in other_modules

    changed = True
    while changed:
        changed = False
        for name in sorted(own):
            if any(presumed(read) for deciding in candidates[name] for read in deciding):
                own.discard(name)
                changed = True
    for name in bound:
        targets = imported.get(name, set())
        if name in own:
            bindings[name] = f"{WRAPPER}.{name}"
        elif bound[name] == len(targets) == 1:
            bindings[name] = next(iter(targets))
        else:
            bindings[name] = PRESUMED
    return bindings


@functools.cache
def _default_modules() -> frozenset[str]:
    return own_modules(REPO_ROOT)


def _names(ref: Ref, bindings: Mapping[str, str]) -> list[str]:
    """Every dotted name a reference can stand for: as written, through ``sharing``, and through
    the wrapper's exports."""
    names = anchored(ref.qualified, WATCHED)
    if ref.qualified == WRAPPER and ref.kind in ("use", "star"):
        # The wrapper as a value (any of its names, by a string), or ``from ._switchboard import
        # *``, which binds every public name it has.
        names.append(PRESUMED)
    elif ref.qualified.startswith(WRAPPER + "."):
        rest = ref.qualified[len(WRAPPER) + 1 :].split(".")
        target = bindings.get(rest[0], PRESUMED)
        names += anchored(".".join([target, *rest[1:]]), WATCHED)
    return list(dict.fromkeys(names))


def _surface_problem(name: str, ref: Ref) -> tuple[str, str] | None:
    """(rule, message) when ``name``, a part of switchboard, is outside the host's surface."""
    parts = name.split(".")
    if parts[0] != "switchboard":
        return None
    as_value = ref.kind == "use" and not ref.annotation
    if len(parts) == 1:
        if as_value:
            return "switchboard-surface", "switchboard is used as a value; name the member you use"
        return None
    sub = parts[1]
    rule = "sharing-member" if sub == "sharing" else "switchboard-surface"
    allowed = SURFACE.get(sub)
    if allowed is None:
        parts_allowed = ", ".join(sorted(SURFACE))
        return rule, f"switchboard.{sub} is not part of the host's surface ({parts_allowed})"
    if len(parts) == 2:
        if as_value:
            return rule, f"switchboard.{sub} may be used only through an allowed member"
        return None
    member = parts[2]
    if member not in allowed:
        return rule, f"switchboard.{sub}.{member} is not one of: {', '.join(sorted(allowed))}"
    return None


def _named_by_string(text: str) -> list[tuple[str, str]]:
    """(rule, message) for each banned name a string spells."""
    found: list[tuple[str, str]] = []
    for part in text.split("."):
        if part == "register":
            found.append(("register", "'register' by string: no register, anywhere"))
        if "invite" in part.lower():
            found.append(("invite", f"{part!r} by string: no invites, anywhere"))
        if part == "share_new":
            found.append(("share-new", "'share_new' by string: the host mints no room"))
    return found


def _strings_as_attributes(tree: ast.Module, refs: list[Ref]) -> list[tuple[int, str]]:
    """(line, text) of each string that becomes an attribute or a keyword name."""
    found: list[tuple[int, str]] = []
    for ref in refs:
        call = ref.call
        if call is None or ref.qualified not in ATTRIBUTE_BY_STRING:
            continue
        candidates = call.args[:1] if ref.qualified == "operator.methodcaller" else call.args
        for argument in candidates:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                found.append((ref.line, argument.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg is None and isinstance(keyword.value, ast.Dict):
                    for key in keyword.value.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            found.append((node.lineno, key.value))
    return found


def _private_member(ref: Ref, names: list[str], modules: frozenset[str]) -> str | None:
    """The private attribute this reference reads on something not its own, or None.

    Its own: an attribute directly on ``self`` or ``cls``, or directly on one of this package's
    modules. Any later private attribute in the chain is on some other object.
    """
    if ref.kind != "use":
        return None
    attrs = ref.written[1:]
    private = [
        (index, attr)
        for index, attr in enumerate(attrs, start=1)
        if attr.startswith("_") and not attr.startswith("__")
    ]
    if not private:
        return None
    if any(matches(name, "switchboard") for name in names):
        return private[0][1]
    parts = ref.qualified.split(".")
    root = parts[: len(parts) - len(attrs)]
    for index, attr in private:
        if index == 1 and ref.written[0] in ("self", "cls"):
            continue
        if ref.rooted and ".".join([*root, *attrs[: index - 1]]) in modules:
            continue
        return attr
    return None


def check_source(
    source: str | bytes,
    rel: str,
    bindings: Mapping[str, str] | None = None,
    modules: frozenset[str] | None = None,
) -> list[Violation]:
    """Check one file's source as if it were at ``rel`` (a root-relative POSIX path).

    ``bindings`` maps the names the wrapper binds to what they stand for (:func:`wrapper_bindings`);
    by default, as if the wrapper were not read: each submodule name stands for that submodule,
    and any other name is presumed to be ``switchboard``. ``modules`` is this package's modules
    (:func:`own_modules`); by default, this repository's.
    """
    tree = parse(source, rel)
    if isinstance(tree, Violation):
        return [tree]
    bindings = DEFAULT_WRAPPER_BINDINGS if bindings is None else bindings
    modules = _default_modules() if modules is None else modules
    surface_rules = not rel.startswith(SURFACE_EXEMPT_PREFIXES)
    found: list[Violation] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str | bytes):
            text = node.value if isinstance(node.value, str) else node.value.decode("latin-1")
            if CODE_PREFIX in text:
                found.append(
                    Violation(rel, node.lineno, "invite", "string holds an invite-code prefix")
                )
        elif isinstance(node, ast.keyword) and node.arg and "invite" in node.arg.lower():
            found.append(Violation(rel, node.lineno, "invite", f"keyword {node.arg}= is an invite"))
        elif isinstance(node, ast.arg) and "invite" in node.arg.lower():
            found.append(
                Violation(rel, node.lineno, "invite", f"parameter {node.arg} is an invite")
            )
        elif isinstance(node, ast.ExceptHandler) and node.name and "invite" in node.name.lower():
            found.append(
                Violation(rel, node.lineno, "invite", f"{node.name}: no invites, anywhere")
            )
        elif isinstance(node, ast.ImportFrom) and _is_runtime(rel):
            module = absolute_module(node.module, node.level, package_of(rel))
            for alias in node.names:
                private = alias.name.startswith("_") and not alias.name.startswith("__")
                if private and not matches(module, OWN_PACKAGE):
                    found.append(
                        Violation(
                            rel,
                            node.lineno,
                            "private-member",
                            f"{module}.{alias.name}: {alias.name} is private to the module that "
                            "defines it",
                        )
                    )

    found.extend(import_alias_conflicts(tree, package_of(rel), rel))
    refs = references(tree, rel)
    for line, text in _strings_as_attributes(tree, refs):
        for rule, message in _named_by_string(text):
            if rule != "share-new" or surface_rules:
                found.append(Violation(rel, line, rule, message))

    for ref in refs:
        words = ref.words()
        names = _names(ref, bindings)
        uses_switchboard = any(matches(name, "switchboard") for name in names)
        if _is_runtime(rel) and rel != SWITCHBOARD_MODULE and uses_switchboard:
            found.append(
                Violation(
                    rel,
                    ref.line,
                    "switchboard-import",
                    f"{ref.qualified}: in the runtime package only {SWITCHBOARD_MODULE} "
                    "imports or uses switchboard",
                )
            )
        if _is_runtime(rel):
            private = _private_member(ref, names, modules)
            if private is not None:
                found.append(
                    Violation(
                        rel,
                        ref.line,
                        "private-member",
                        f"{ref.qualified}: {private} is private to the object that defines it",
                    )
                )
        if "register" in words:
            found.append(
                Violation(rel, ref.line, "register", f"{ref.qualified}: no register, anywhere")
            )
        named = sorted(word for word in words if "invite" in word.lower())
        if named:
            found.append(Violation(rel, ref.line, "invite", f"{named[0]}: no invites, anywhere"))
        if not surface_rules:
            continue
        if "share_new" in words:
            found.append(
                Violation(rel, ref.line, "share-new", f"{ref.qualified}: the host mints no room")
            )
            continue
        if ref.kind == "star" and uses_switchboard:
            found.append(
                Violation(
                    rel,
                    ref.line,
                    "sharing-member",
                    f"from {ref.qualified} import * hides which parts of switchboard are used",
                )
            )
            continue
        for name in names:
            problem = _surface_problem(name, ref)
            if problem is not None:
                found.append(Violation(rel, ref.line, *problem))
    return sorted(set(found))


def check(root: Path) -> list[Violation]:
    """Check every Python file under ``root``, following the names its wrapper exports."""
    wrapper = root / SWITCHBOARD_MODULE
    bindings = wrapper_bindings(wrapper.read_bytes()) if wrapper.is_file() else None
    modules = own_modules(root)
    found: list[Violation] = []
    for rel in python_files(root):
        found.extend(check_source((root / rel).read_bytes(), rel, bindings, modules))
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    """Script entry point: ``python tools/guards/switchboard_lint.py [ROOT]``."""
    return run_guard(
        "switchboard_lint", sys.argv[1:] if argv is None else argv, python_files, check
    )


if __name__ == "__main__":
    raise SystemExit(main())
