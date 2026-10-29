# Security policy

## Reporting a vulnerability

Report it privately, through GitHub's private vulnerability reporting on this repository: open the
**Security** tab of `gald33/lucille-host` and choose **Report a vulnerability**. Only the maintainers can
read the report.

If that button is not there, private reporting is not switched on. Open a public issue titled
"security contact" that says nothing about the problem, and a maintainer will switch it on and answer
there.

Do not describe a vulnerability in a public issue or pull request. There is no email address for
reports.

Useful things to include:
- the version, or the commit, you tested;
- your OS and Python version;
- the steps that show the problem, and what you expected instead.

## Supported versions

No release exists yet. Version `0.1.0.dev0` is a skeleton and is not meant to be installed. Which
versions get fixes will be stated here with the first release.

## What counts

In scope, for example:
- any way for Lucille, or anyone holding a pairing code or a room key, to make the host run something
  outside its catalog, read outside an enabled folder, or loosen the local policy;
- any way for a child process to receive the host's keys or your environment variables;
- anything that makes a sentence of the claim in [PRIVACY.md](PRIVACY.md) false;
- a way to get a violation past a guard and past review into a release (report a gap in a guard on its
  own as an ordinary issue or pull request; the guards' known limits are listed in
  [VERIFYING.md](VERIFYING.md)).

Out of scope: malware already running as your user, and anything about Lucille's backend itself. See
[THREAT_MODEL.md](THREAT_MODEL.md).

## What a bad release means for you

If a release turns out to be harmful:
1. It is yanked on PyPI. An installer then skips it, unless it is asked for that exact version, and then
   it warns.
2. A GitHub security advisory is published on this repository. It names the affected versions, and the
   fixed version once there is one.
3. Lucille raises the minimum host version it accepts. Its requests then carry `min_host_version`, and a
   host older than that refuses them with `host_too_old`. That step is Lucille policy as of 2026-10-27;
   not proven by this repository. The refusal is host code (not written yet).

**The bad code stays on your computer until you upgrade.** lucille-host has no auto-update. Yanking a
release does not remove it from computers where it is installed. If an advisory names your version,
stop `lucille-host` and upgrade. You can also remove the computer in Lucille.

**A Switchboard hub upgrade can need a host release first.** Each release pins agent-switchboard 2.6.x
and does not update itself. If the managed hub ever changes in a way that breaks 2.6 clients, a host
release that supports the change must ship first.
