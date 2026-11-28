"""Scenario: an operation whose catalog definition changed since it was enabled does not run.

The invariant "An upgrade never runs a changed op under old consent" (decisions/0001, "Host
invariants and their tests"). The policy file stores, for each enabled operation, the hash of the
catalog definition the user enabled. When a release changes that definition, the host must treat
the operation as disabled until the user enables it again, and must not rewrite the file, so that
the user can see why.

The suite cannot install a second release with a changed entry, and it must not edit the
installed catalog it is testing. It changes the other side of the comparison instead: it rewrites
the stored hash of the enabled ``files.sha256``, which is what the host sees after an upgrade that
changed the entry. That the two look the same to the host is an inference from
``lucille_host.policy.effective``, which compares the stored hash with the catalog's; the suite
does not check it by installing a changed release.
"""

from __future__ import annotations

import re

from lucille_host.conformance._restore import (
    refused_here,
    restore,
    scenario,
    until_describe,
    write_policy,
)
from lucille_host.conformance._scenarios import ROOT, Context, Failed, _short, check

OP = "files.sha256"
OTHER_HASH = "0" * 63 + "1"
_TABLE = re.compile(rb'(\[ops\."files\.sha256"\]\s*\ndefinition\s*=\s*")([0-9a-f]{64})(")')


def rewrite_definition(data: bytes, new_hash: str) -> bytes:
    """``data``, a policy file, with ``files.sha256``'s stored definition hash replaced."""
    match = _TABLE.search(data)
    if match is None:
        raise Failed(f"the policy file holds no definition for {OP}")
    if match.group(2) == new_hash.encode("ascii"):
        raise Failed("the stored definition is already the replacement hash")
    return data[: match.start(2)] + new_hash.encode("ascii") + data[match.end(2) :]


@scenario
def upgrade_disables_changed_op(ctx: Context) -> None:
    """With files.sha256's stored definition hash no longer the catalog's, as after an upgrade
    that changed the entry: host.describe stops listing it, a request for it is refused
    not_enabled and starts nothing, the policy digest is unchanged by that refusal, the policy
    file is left as it is, and `ops list` shows it disabled_by_upgrade; once the user enables it
    again, it runs."""
    assert ctx.baseline is not None
    named = ctx.host.policy_file
    check(named is not None, "the host names no policy file")
    assert named is not None
    args = {"root": ROOT, "path": "hello.txt"}
    try:
        changed = rewrite_definition(named.read_bytes(), OTHER_HASH)
        write_policy(named, changed)
        until_describe(ctx, lambda d: OP not in d["enabled_ops"], f"disabling the changed {OP}")
        refused_here(ctx, ctx.body(OP, args), "not_enabled")
        check(named.read_bytes() == changed, "the host rewrote the policy file")
        listed = [o for o in ctx.host.cli("ops", "list") if o.get("name") == OP]
        check(
            [o.get("state") for o in listed] == ["disabled_by_upgrade"],
            f"ops list does not tell the user why {OP} is off: {listed!r}",
        )

        ctx.host.enable(OP)
        until_describe(ctx, lambda d: OP in d["enabled_ops"], f"enabling {OP} again")
        spawns = ctx.spawn_count()
        again = ctx.request(ctx.body(OP, args))
        check(
            again.get("status") == "done", f"{OP} did not run once enabled again: {_short(again)}"
        )
        check(ctx.spawn_count() == spawns + 1, "run did not count the process it started")
    finally:
        restore(ctx)
