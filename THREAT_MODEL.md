# Threat model

**Status: partly built.** Built: the guards CI runs and the lock it installs from; the catalog, argv-only
execution with user values after `--`, path containment, the environment allowlist, the local policy, the
audit log, `pause` and `kill`, and drain. Not built: pairing, its hub check and SAS gate; the read loop, and with
it the envelope checks, dedupe and expiry; the private `XDG_RUNTIME_DIR` against a planted signer socket;
the conformance suite; the release workflow and its provenance (protected tags, the `pypi` environment,
attestations); the check of Switchboard's release provenance; `files.search`; and `forget`. No other
protection below is built yet. Each protection not yet built is one the first release must have. The full design is
[decisions/0001-host-design.md](decisions/0001-host-design.md).

## What the host protects

- Files and folders you have not enabled.
- Your passwords, keys and browser sessions, unless they are inside a file an enabled operation reads.
- Your computer, from running anything other than the operations built into the release, with the
  arguments their schemas allow.

## Who the host trusts

- **Your user account and your OS.** Anything running as your user is outside this model (see "Out of
  scope").
- **The release you installed:** its operation catalog, which is reviewed in public, and its
  dependencies, among them agent-switchboard 2.6.x and keyring. CI runs against the exact versions
  pinned in `uv.lock`. The package itself declares ranges (`agent-switchboard==2.6.*`, `keyring>=25`),
  and `pipx` or `uv tool` resolve those on the day they run, without reading the lock. How a release
  carries exact pins to your install is not decided yet.
- **Not Lucille's backend.** The host treats every request as possibly hostile. Lucille's backend is
  bounded only by what you enable, on the folders you add.

## Threats and what bounds them

| Threat | What bounds it | What is left |
|---|---|---|
| A compromised or prompt-injected Lucille backend | The release catalog, the operations you enable and the folders you add. Every v0.1 operation is read-only. Only `host.describe` is on after pairing. `pause` and `kill` work offline. | Anything enabled operations can read under enabled folders, while the host runs. |
| An intercepted or phished pairing code | The code must name Lucille's hub (`https://switchboard.lucille-ai.com`) and carry no token. Nothing else is accepted, except a loopback hub that the local config names, for the conformance suite. The short code (SAS) is compared and confirmed on both screens (what Lucille's screen shows is Lucille policy as of 2026-10-27; not proven by this repository). | A user who confirms without comparing. |
| Someone else's pairing code: an attacker pairing your computer to themselves | The hub check; the local SAS gate; only `host.describe` enabled after pairing. | A user who confirms a mismatched SAS, then enables operations. |
| A leaked `pair:<name>` key, offered work-room key, or pair secret | Only two places hold the pair, and both hold it with the `pair:<name>` key: the host's store (local, owner-only) and Lucille's database (encrypted with Lucille's master key: Lucille policy as of 2026-10-27; not proven by this repository). Signing requests with a key pinned at pairing would not help, because every leak path found exposes that key with the pair. The response is to remove the computer in Lucille, or run `lucille-host forget`, and pair again. v0.1 has no heal command. That analysis was made for `pair:<name>`'s key. It has not been repeated for the work rooms Lucille offers over the pair, whose keys also sit in each side's share store and in the pair room's offer records. The full analysis is in the design record's threats table. | Whoever holds the key can read and forge traffic, and ask for enabled operations, until the pair is replaced. |
| A malicious payload: shell metacharacters, path traversal, flag or environment injection | Argument schemas; argv only, never a shell; user values after `--`; paths resolved one name at a time beneath their folder, never through `/proc`, never into the host's own files, and never to a FIFO, socket or device; an environment built from an allowlist; the conformance suite. | None known. |
| A malicious release, or a compromised maintainer account | Protected release tags; a `pypi` environment that needs the repository owner's approval; PEP 740 attestations (they prove where a release was built, not what it does); a public diff of the catalog and of `CLAIMS.md`; exact pins in the lock CI uses; no auto-update. | A validly attested malicious release from an account that holds both tag and environment rights. |
| A malicious or careless pull request | The guards in CI, which catch accidental and casual violations in the forms they list ([VERIFYING.md](VERIFYING.md) says what each cannot see); a maintainer's review of every pull request before it merges. | A change crafted to get past both the guards and review. |
| A compromised dependency, `agent-switchboard` included | Few dependencies, pinned exactly in the lock CI uses; Switchboard's own release provenance checked. | Dependency code runs with the host's privileges. An install with `pipx` or `uv tool` resolves the declared ranges on the day it runs, so it can get versions CI never ran. |
| Malware running as your user | Out of scope, and stated. | It can read the host's stores, edit the installed package and rewrite the log. |
| Another local user planting a signer socket for the Switchboard client | A private `XDG_RUNTIME_DIR` for the host's process, set before it builds any Switchboard client, pairing's included. That is the measure that covers every client: none finds a socket at its signer path. A freshly generated signing identity is also set on the clients built for `pair:<name>` and for each accepted room. | Not tested across users. The planned conformance test runs as one user. |
| Hazards in the external programs an operation runs (config files, flags) | Each catalog entry is reviewed; executables by absolute path; the environment allowlist; `rg --no-config`. | Each new operation widens this surface. |

## Out of scope

- Malware or any other process running as your user.
- What Lucille's backend, the people who run it, or its AI provider do with a reply.
- The AI provider's own retention.
- Someone with physical access to an unlocked computer.

## Known gaps

- Switchboard's signer-socket hazard is shown for one user. That another local user could exploit it on
  Linux with `XDG_RUNTIME_DIR` unset is inferred, not tested.
- The leak-path analysis above has not been repeated for the offered work rooms.
- How long a reply can be late after a lid close, and whether Lucille has already failed the job by then,
  has not been measured.
- Redaction of secrets from output is best effort. It is not a protection this model relies on.
- A path is checked before the operation runs, and the operation then opens it by the name the request
  gave. A process that swaps a folder inside an enabled folder for a symbolic link in between could send
  the operation outside it, and one that swaps a file for a FIFO could make it block. That takes a
  process able to write inside the enabled folder while the host runs, which "Out of scope" already
  covers when it runs as your user.
- A root is stored as the path you typed, so a root that is a symbolic link is followed to wherever it
  points when a request comes. A link repointed while the host runs (at your home folder, say) is
  checked against the flag for home folders only when the policy is next loaded.
- Whether a folder holds your home folder (and so needs the explicit flag) is decided by its path, by
  device and inode, through any other mount of the same filesystem that Linux's mount table lists (the
  source of a bind mount, a btrfs top level), and through macOS's Data volume; when the folder is added,
  the host also looks below it for your home folder's own path. A folder that reaches your home folder
  any other way is not recognised: through another filesystem (a FUSE or overlay view of it, say), a
  mount this process cannot see, or, on macOS, anything but the Data volume. A stalled network mount
  where that path would be below a folder hangs `roots add`; interrupt it.
- An escape from an enabled folder is defined by real path (spec/PROTOCOL.md §10). A bind mount, or a
  hard link to a file, made inside an enabled folder has a real path inside it, so it can reach a file
  outside it, and the host allows that (its own files aside, below). Only the local user can set either
  up.
- The host's own files are recognised by device and inode, so a second mount of one of its folders or
  a hard link to one of its files is refused too. A hard link made to an earlier version of a file the
  host has since replaced (it replaces the policy file whole on each change) is not.
- Only a running host enforces an operation's time limit and `kill`. If the host itself is killed (by
  SIGKILL, say) or crashes while an operation runs, the operation keeps running without its time limit,
  and `kill` cannot reach it; the audit log then ends at that operation's `start` line. Closing the
  terminal drains instead (SIGHUP is handled as SIGTERM is), unless the host was started with SIGHUP
  ignored, as `nohup` does, when it keeps running.
- A stalled network or FUSE mount inside an enabled folder can hang the host: checking a path there, or
  waiting for an operation stuck on it, blocks in the kernel. The host gives up waiting for a killed
  operation after 10 s, but checking a path has no time limit, and `kill` is not seen while it waits.
- A process an operation starts that leaves the operation's process group (by calling `setpgid` or
  `setsid` itself) is not killed with the operation. The v0.1 operations start no such process.
- Exact dependency pins reach CI through `uv.lock`, but not yet a user's install (see "Who the host
  trusts").
