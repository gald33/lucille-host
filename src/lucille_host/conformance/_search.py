"""Scenarios: ``files.search`` cannot be widened by traversal or by flag injection.

``files.search`` takes no path. It runs ripgrep in the root's real path with no path operand,
with ``--no-config``, ``--no-ignore`` and ``--no-follow``, hidden files skipped, and the pattern
after ``--`` (decisions/0001, "The closed operation set"; spec/PROTOCOL.md section 10). So the ways
Lucille could try to widen it are its ``root`` alias, keys added to ``args``, a pattern shaped like
a flag, and what sits in the folder: links out of it, hidden files, files named like flags. The
host's own environment is another way in: ripgrep reads ``RIPGREP_CONFIG_PATH`` unless told not
to, so one scenario plants a configuration that would follow links and search hidden files. The
child's built environment never carries that variable, so that scenario shows the environment
holding; it would pass without ``--no-config`` too, and does not show that flag.

Each refused case checks the exact code, that nothing started, and that the policy digest did
not change. Each case that runs checks the exact matches the reply's document reports, so that a
flag that took effect (``--files`` lists files instead of matching lines) shows up as a
difference.

The scenarios add a folder of their own as alias ``search`` and enable ``files.search``, then put
the policy (and ``run``, where restarted) back as the baseline found them. Where this computer has
no ripgrep at the catalog's path, they are skipped with that reason, never a pass; a host that
reports ``files.search`` unavailable while ripgrep is there fails them
(:func:`~lucille_host.conformance._restore.search_runs_here`).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from lucille_host.conformance._restore import (
    refused_here,
    restore,
    scenario,
    search_runs_here,
    until_describe,
)
from lucille_host.conformance._scenarios import Context, Failed, _short, check

OP = "files.search"
ALIAS = "search"
SENTINEL = "conformance-outside-7e02"
# Patterns that are flags ripgrep knows, each of which would widen or change the search if it were
# read as one: list every file, run a program on each file, follow links, search hidden and
# ignored files, print the version, read patterns from a file.
FLAG_PATTERNS = (
    "--files",
    "--pre=/bin/sh",
    "--follow",
    "-uuu",
    "--hidden",
    "--version",
    "-f/etc/passwd",
)
PLAIN = "nothing to see"


def _text(value: Any) -> str:
    """A path or line of the document: its text, or its bytes (base64) decoded with any byte
    that is not UTF-8 replaced."""
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    if isinstance(value, dict) and isinstance(value.get("bytes"), str):
        return base64.b64decode(value["bytes"]).decode("utf-8", "replace")
    raise Failed(f"a match holds neither text nor bytes: {value!r}")


def matches(reply: dict[str, Any]) -> list[str]:
    """The matches a ``files.search`` reply reports, one ``path:line:text`` string each (the
    line without its ending), in the order given. Its stdout is the document
    ``files.search.schema.json`` describes; a document with an error, or that says it is
    truncated, fails the scenario, since none of these searches should come near a cap."""
    stdout = str(reply.get("stdout", ""))
    try:
        document = json.loads(stdout)
    except ValueError as error:
        raise Failed(f"files.search's stdout is not its document: {stdout[:200]!r}") from error
    check(isinstance(document, dict), f"files.search's document is {document!r}")
    check(document.get("error") is None, f"files.search's document has error {document!r}")
    check(document.get("truncated") is False, f"files.search's document is truncated: {document!r}")
    found = []
    for match in document.get("matches", []):
        line = _text(match.get("lines")).removesuffix("\n").removesuffix("\r")
        found.append(f"{_text(match.get('path'))}:{match.get('line_number')}:{line}")
    return found


def plant(workdir: Path) -> tuple[Path, Path]:
    """A folder to search and one outside it, filled as the scenarios below expect."""
    root = workdir / "search-root"
    outside = workdir / "search-outside"
    root.mkdir(exist_ok=True)
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text(SENTINEL + "\n", encoding="utf-8")
    # A ripgrep configuration that would widen the search, for RIPGREP_CONFIG_PATH.
    (outside / "ripgreprc").write_text("--follow\n--hidden\n", encoding="utf-8")
    (root / "flags.txt").write_text("".join(p + "\n" for p in FLAG_PATTERNS), encoding="utf-8")
    (root / "plain.txt").write_text(PLAIN + "\n", encoding="utf-8")
    (root / "--version").write_text("named like a flag\n", encoding="utf-8")
    (root / ".hidden").write_text(SENTINEL + "\n", encoding="utf-8")
    for link, target in (("out", outside), ("secret-link.txt", outside / "secret.txt")):
        if not (root / link).is_symlink():
            (root / link).symlink_to(target)
    return root, outside


def args(pattern: str, *, fixed_strings: bool = True, **extra: Any) -> dict[str, Any]:
    return {
        "root": ALIAS,
        "pattern": pattern,
        "fixed_strings": fixed_strings,
        "max_count": 200,
        **extra,
    }


def _set_up(ctx: Context, env: dict[str, str] | None = None) -> tuple[Path, Path]:
    """Plant the folders, add the root, enable the operation (restarting ``run`` with ``env``
    if given), and check that a plain search finds the one plain line: the control."""
    search_runs_here(ctx)
    root, outside = plant(ctx.workdir)
    ctx.host.add_root(ALIAS, root)
    ctx.host.enable(OP)
    if env is not None:
        ctx.host.restart(env=env)
    until_describe(
        ctx,
        lambda d: OP in d["enabled_ops"] and ALIAS in d["root_aliases"],
        f"enabling {OP} with root {ALIAS}",
    )
    spawns = ctx.spawn_count()
    reply = ctx.request(ctx.body(OP, args(PLAIN)))
    check(reply.get("status") == "done", f"control: a plain search was not done: {_short(reply)}")
    check(matches(reply) == [f"plain.txt:1:{PLAIN}"], f"control: matches {matches(reply)!r}")
    check(ctx.spawn_count() == spawns + 1, "control: run did not count the search it started")
    return root, outside


@scenario
def search_root_traversal(ctx: Context) -> None:
    """files.search with a root that is a path, a root the policy does not hold, or a path,
    paths, cwd or glob key added to its arguments is refused (bad_args, path_outside_root and
    bad_args) and starts nothing."""
    try:
        _set_up(ctx)
        for root in ("..", "../search", "/", "/etc", "search/..", "search/../.."):
            refused_here(ctx, ctx.body(OP, args("x", root=root)), "bad_args")
        refused_here(ctx, ctx.body(OP, args("x", root="elsewhere")), "path_outside_root")
        for key, value in (("path", "../.."), ("paths", ["/"]), ("cwd", "/"), ("glob", "!*")):
            refused_here(ctx, ctx.body(OP, args("x", **{key: value})), "bad_args")
    finally:
        restore(ctx)


@scenario
def search_flag_injection(ctx: Context) -> None:
    """files.search for a pattern shaped like a ripgrep flag (--files, --pre=/bin/sh, --follow,
    -uuu, --hidden, --version, -f/etc/passwd), as a fixed string and as a regular expression,
    reports exactly the one line holding that text; a file named --version is matched as a
    file."""
    try:
        _set_up(ctx)
        for fixed in (True, False):
            for number, pattern in enumerate(FLAG_PATTERNS, start=1):
                reply = ctx.request(ctx.body(OP, args(pattern, fixed_strings=fixed)))
                want = [f"flags.txt:{number}:{pattern}"]
                check(
                    reply.get("status") == "done" and matches(reply) == want,
                    f"pattern {pattern!r} (fixed_strings={fixed}) was not searched as text: "
                    f"{_short(reply)} matches {matches(reply)!r}, not {want!r}",
                )
        reply = ctx.request(ctx.body(OP, args("named like a flag")))
        want = ["--version:1:named like a flag"]
        check(matches(reply) == want, f"a file named --version: {matches(reply)!r}, not {want!r}")
    finally:
        restore(ctx)


@scenario
def search_no_escape(ctx: Context) -> None:
    """files.search reaches nothing outside its root and no hidden file: not through a link to
    a folder or a file outside it, and not when run's own environment names a ripgrep
    configuration that would follow links and search hidden files."""
    try:
        _root, outside = plant(ctx.workdir)
        _set_up(ctx, env={"RIPGREP_CONFIG_PATH": str(outside / "ripgreprc")})
        for fixed in (True, False):
            reply = ctx.request(ctx.body(OP, args(SENTINEL, fixed_strings=fixed)))
            check(reply.get("status") == "done", f"the search did not run: {_short(reply)}")
            check(not matches(reply), f"{OP} reached outside its root: {matches(reply)!r}")
            check(str(outside) not in str(reply), "the reply names the folder outside the root")
    finally:
        restore(ctx, restart=True)
