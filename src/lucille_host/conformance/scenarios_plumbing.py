"""The scenario that proves the plumbing: an allowed operation runs, end to end.

``host.describe`` is the one operation the fresh policy enables after pairing. Lucille asks for it
in the work room it offered, and the host answers ``done``, with a policy digest, and with no
process started for it: the host answers ``host.describe`` itself. The spawn count is read before
and after, from the host's audit log, so that every later scenario can count the same way.
"""

from __future__ import annotations

import re

from lucille_host.conformance._scenarios import Context, Failed, scenario

DIGEST = re.compile(r"[0-9a-f]{64}")


@scenario("allowed-op-runs")
def allowed_op_runs(ctx: Context) -> None:
    spawns = ctx.spawn_count()
    reply = ctx.request("host.describe")
    ctx.expect_status(reply, "done")
    if reply.get("dir") != "reply":
        raise Failed(f"the answer is not a reply: {reply}")
    if not DIGEST.fullmatch(str(reply.get("policy_digest", ""))):
        raise Failed(f"the reply carries no well-formed policy digest: {reply}")
    if ctx.policy_digest() != reply["policy_digest"]:
        raise Failed("the policy digest changed between two host.describe requests")
    jobs = {line.get("job_id") for line in ctx.audit_lines() if line.get("kind") == "decision"}
    if reply["job_id"] not in jobs:
        raise Failed("the audit log records no decision for the request")
    if ctx.spawn_count() != spawns:
        raise Failed("the host started a process for host.describe")
