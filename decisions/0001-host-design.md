# 0001: lucille-host design record (draft)

Author: lucille-host maintainers. First written 2026-10-15; revised 2026-10-19 and 2026-10-20; rewritten
for this public repository 2026-10-27; the protocol it describes was written out as
[spec/PROTOCOL.md](../spec/PROTOCOL.md) on 2026-10-28. Status: **draft.** Built so far: the package
skeleton, the guards, CI, the protocol spec (its prose, schemas and fixtures), and, on 2026-10-29: "The
closed operation set" without `files.search` or limits tightened in the policy; "Execution rules";
"Local policy and upgrades", except reloading on a local signal; "Audit log"; and, of "Pause, kill and
shutdown", `pause`, `resume`, `kill` and drain. On 2026-10-30: "Pairing" and its stores, "The read loop"
(with the envelope checks, dedupe, expiry, restarts and reply posting), what the host does on removal on
the Lucille side, and `status`, `log` and `forget`, tested against Switchboard's in-process hub and a
stand-in for Lucille; "Choices made in building pairing and the read loop" lists what that build chose.
Not built: the conformance suite, `files.search`, the release workflow and `lucille-host verify`.

This record describes the program in this repository, and it governs the host's code. A pull request that
changes the design updates this record in the same pull request.

**Conventions.**
- Where this record says what Lucille's backend does, it describes the other side of the protocol. Each
  such statement is marked "Lucille policy as of <date>; not proven by this repository". This repository
  can test only the host. Two kinds of statement are not marked: the protocol's definitions of its own
  message fields, Lucille's included (what `not_after` means, for example), which this repository's
  spec states for both sides; and what agent-switchboard 2.6.0 does, which is cited "sb" instead.
- "Not decided yet" marks a question this design leaves open. Each one is listed again at the end.
- Switchboard is cited at agent-switchboard 2.6.0 (source: github.com/gald33/switchboard, tag `v2.6.0`)
  and marked "sb". A name such as sb `sharing.py:30-33` means `src/switchboard/sharing.py`, lines 30 to
  33, at that tag. A path under `docs/` or `tests/` is that file in the same repository at that tag.
- "Measured locally" means run in a research container on 2026-10-19, against a private hub on 127.0.0.1
  (`switchboard serve`, or the in-process `switchboard.testing` hub where stated). Those runs are not part
  of this repository.

## What the host is

A small Python program a user installs on their own computer.
- It **pairs** with the user's Lucille account through Switchboard pairing.
- It waits in the work rooms Lucille mints and offers over that pair for requests to run one of a
  **closed set of named operations** that ship inside the release, and posts one reply per request.
  That Lucille mints and offers those rooms is Lucille policy as of 2026-10-27; not proven by this
  repository.
- The first operations call **command-line tools**.

The host:
- runs no LLM and opens no listening port;
- never runs code or a command line sent by Lucille;
- holds one long-lived credential toward Lucille, its side of the pair. It also keeps a local HMAC key for
  the policy digest, which never leaves the computer, and generates a throwaway signing key per client.
  It also keeps, in a share store of its own, the keys of the rooms it accepted. They can be recovered
  from the pair (sb `sharing.py:30-33`). Each computer has one standing work room, replaced only on
  re-pair or a suspected leak ([spec/PROTOCOL.md](../spec/PROTOCOL.md) §9), so those keys can live as
  long as the pair: they are a second long-lived secret (§18 there).

## Transport: work rooms offered over one pair

- **Library.** The runtime embeds `agent-switchboard` (MIT), pinned `==2.6.*`, base install with no extras.
  Its runtime closure is 11 packages, including `cryptography` and `httpx`.
- **Only the plain synchronous library is used:**
  - `pairing.accept`, `pairing.resolve` and `pairing.forget`. Not `pairing.status`, which resolves
    every pair over the hub: `status` reads the pair from the host's own store, with no hub call;
  - `switchboard.client.Client`: `post`, `inbox(since=...)` (a non-committing peek), and `read_channels`
    to step past a message it cannot open;
  - the part of `switchboard.sharing` that takes and uses a room Lucille offers: `sharing.touch` reads the
    offers, `sharing.accept` keeps one (sb `sharing.py:1541-1626`), and `sharing.resolve_room` opens a
    held room with no hub call (`:1842-1873`), and `sharing.leave` leaves a room the host abandons, or
    refuses right after accepting it (spec/PROTOCOL.md §6 and §9). Every call takes the host's own share
    store. Which calls are needed is this design's reading of agent-switchboard 2.6.0.

  Never the CLI, the MCP bridge, `sharing.share_new`, `register()`, an invite, or a hosted bridge. The
  host follows a heal the other side makes. It never starts one, and v0.1 has no heal verb.
- **Outbound HTTPS to one hub only.**
  - The host talks only to the hub its pairing code named. It refuses any code that names a hub other
    than the configured Lucille hub (`https://switchboard.lucille-ai.com`), and any code that carries a
    token.
  - The only exception is a loopback hub (`http://127.0.0.1:<port>` or `http://localhost:<port>`), and
    only when the local config names it (`--hub`, set by the conformance harness). `status` shows it. A
    code naming a loopback hub without that is refused.
  - The host opens no listening socket. A test with an audit hook sees none bound while the host
    pairs, runs and answers a request.
- **The hub token.** agent-switchboard 2.6.0 sends the managed hub's published token
  (`MANAGED_HUB_TOKEN`) itself to `https://switchboard.lucille-ai.com` when none is configured
  (sb `config.py:586-603`). The live hub admits it (Lucille policy as of 2026-10-19; not proven by this
  repository). So a code never needs to carry a token, and a code that carries one is refused.
- **Rooms.** Pairing derives three rooms from secrets both sides hold (sb `docs/pairing.md:46-79`):
  - the handshake room, used once;
  - the pair room, whose sealed records keep the pair in step;
  - the room Switchboard 2.6.0 calls the **work room**, `pair:<name>`. That is the library's label. In
    this record, "work room" means a room Lucille mints and offers over the pair, never `pair:<name>`.

  Lucille also mints rooms and offers them over the pair (Lucille policy as of 2026-10-27; not proven by
  this repository). An offer is one sealed record per room per writer in the pair room (sb
  `docs/pairing.md:62-70`, `sharing.py:35-41`; the host's accept writes the second, `:1546-1550`). All
  traffic between the host and Lucille is in those work rooms, on the channel `lucille.exec`, apart from
  what the Switchboard library itself writes to keep the pair and its offers in step. The host acts only
  on requests there (see "Host invariants and their tests") and posts its replies there. It posts
  nothing of its own in `pair:<name>`: from the host's side, that room carries only the library's own
  notes. Lucille posts every request on `lucille.exec` in those rooms, and nothing in `pair:<name>`
  beyond the library's notes (Lucille policy as of 2026-10-27; not proven by this repository). The host
  reads `pair:<name>` on its `pair` channel only, for those notes, the share wake `swbp1/shared` among
  them (sb `sharing.py:1175-1186`). The host reads them as wakes, never as instructions.
- **What the hub sees.** The hub cannot read message bodies. It sees:
  - room ids, blinded channel and agent ids;
  - the cleartext type, thread and TTL;
  - timing and sequence numbers;
  - sizes, padded to a power of two from 64 B up to 4 KiB and to a multiple of 4 KiB above that
    (sb `crypto.py:122-141`);
  - the computer's network address.
- **What else follows from that.**
  - Every job meaning stays inside the sealed body: type `note`, no thread.
  - `pair:<name>` is write-protected (`ws_`). Holders of the pair can write into it, and so could anyone
    either side invited (sb `docs/pairing.md:39-41`). The host invites nobody. A client with the read key
    and no write key got 403, measured locally.
  - Each room Lucille offers is write-protected too, and has no members: "access is knowing it" (sb
    `sharing.py:11-13`). Lucille, which minted it, and each pair it is offered over know its keys, and
    whoever holds the pair secret holds every room offered over the pair; a heal does not change that
    (`:30-33`). The host offers no room of its own; accepting one writes the library's offer of that same
    room back over the same pair, which is how Lucille sees "accepted" (`:1546-1550`).

## Pairing

1. The user adds the computer on Lucille's "Computers" page, which shows a `swbp1_` code once (Lucille
   policy as of 2026-10-27; not proven by this repository).
2. `lucille-host pair <code>`:
   - **refuses while a pair is held** ("Disconnect first"): a profile keeps one pair;
   - **decodes** the code (`pairing.PairingCode.decode`). It refuses the wrong hub or a carried token, and
     shows the pair's note only after sanitizing it. With `--code-stdin`, the code is read from stdin,
     never from the command line;
   - **resets the local policy** to the fresh one (only `host.describe`, no folder) right before it
     answers a new code. The earlier policy is set aside, unused, and `lucille-host policy previous`
     lists it;
   - **accepts** it with the host's own Store and `base=None`, so nothing is written under `~/.switchboard`
     and no subprocess runs. Tests with an audit hook find no process started, and nothing written
     outside the profile's own folders and its keyring item;
   - **waits for Lucille to collect.**
     - While waiting it calls `pairing.resolve` every 5 s, and at once on any `swbp1/*` note. The note is
       only a wake: 2.6.0 posts `paired` best effort, and a failed post is not retried on its own
       (sb `pairing.py:2015-2019`).
     - Resolve also resends an answer that has not reached the hub (`unsent`).
     - It stops waiting when the state is `paired`. The SAS reaches the responder on its first resolve
       after the initiator collects.
     - On AlreadyRedeemed, `aborted` or `superseded`, it says the code leaked, and stops.
     - On `expired`, `never_confirmed`, `wrong_hub` or a void `unsent`, it says to pair again, and stops.
     - Stopping so, it forgets whatever is left of the pair on the computer.
   - **shows the SAS** and asks the user to type `y` if it matches the one Lucille's page shows (that
     Lucille's page shows it is Lucille policy as of 2026-10-27; not proven by this repository). `y` or
     `yes` confirms, `n` or `no` rejects, and any other answer is asked again. The end of stdin, or
     Ctrl-C, leaves the answer to `pair confirm` or `pair reject`. With `--code-stdin`, `pair` stops once
     the SAS is shown (`sas_pending`, with the deadline).
   - **What was typed before the question does not count.** When stdin is a terminal, `pair` discards
     whatever was typed there before it asks (`termios.tcflush`), so a `y` typed while it waited for
     Lucille, before anyone could compare, answers nothing; where that cannot be done, it asks nothing,
     and leaves the answer to `pair confirm` or `pair reject`. Input that is not a terminal (a pipe, a
     file) is read as given: whoever wrote it chose the answer.
3. **The host's gate.**
   - Until the user confirms locally, the host touches nothing, accepts nothing, posts nothing and runs
     nothing.
   - The gate opens only on a SAS the host has shown. Until a command has shown it, the gate stays shut
     and has no deadline: `run` resolves the pair but shows no SAS, and `pair confirm` refuses a SAS the
     host has not shown (`sas_ready`).
   - The gate's deadline counts from when the SAS is first shown on the host, by `pair` or `pair status`.
     It is 24 h, chosen to match the deadline of Lucille's own gate (that deadline is Lucille policy as of
     2026-10-27; not proven by this repository).
   - `lucille-host pair confirm` and `pair reject` answer later, after the interactive `pair` has exited.
     `pair status` reports `none`, `waiting`, `sas_pending` or `confirmed`, and in `sas_pending` shows the
     SAS again, with the deadline. A pair with no gate record cannot be confirmed on the host: `pair`,
     `pair status` and `pair confirm` each refuse it (`refused`), showing no SAS.
   - `n`, `pair reject`, or the deadline passing with the gate still shut forgets the pair. A gate opened
     in time has no deadline.
   - The host writes no copy of the SAS of its own: neither the gate's record nor the audit log holds it
     (the pair record the library keeps in the store does). `lucille-host status` sends the user to
     `pair status` rather than showing it, and says `sas_ready` while the SAS has reached the host and no
     command has shown it.
   - Lucille has its own gate on the same comparison, and sends no request before it opens (Lucille
     policy as of 2026-10-27; not proven by this repository). The host's gate does not depend on it:
     nothing runs on the host before the host's own gate opens.

**The Store.**
- The host implements Switchboard's `pairing.Store` Protocol over the OS keyring: one item per profile,
  plus file locks ("Choices made in building pairing and the read loop" says which).
- **Tested against the template.** The host's store passes the eight cases of v2.6.0's template file
  that exercise the store under test (vendored as `tests/vendored/test_pairing_store.py`).
  - The ninth case, the parametrized `directory` one, tests `PairStore`.
  - It runs on the in-process `switchboard.testing` hub, which needs starlette 1.9 or later (the
    `[conformance]` extra). Measured locally, it failed with starlette 1.6.0.
  - The test reaches into the store's instrumentation, so a thin wrapper exposes `held`, `locks`, `broken`
    and `enabled` for it.
- With `--store-file`, the host uses `PairStore(<state dir>/pairs, env={})` (mode 0600) and says that this
  is weaker. Its storage failures (a full disk, a folder it cannot write) reach the rest of the host as
  the keyring store's do, as a failure of the store: 2.6.0 counts a plain `OSError` among the hub's errors
  (`pairing.hub_errors()`), and the host never reads a store it cannot write as a hub out of reach.
- **Never `~/.switchboard/pairs`.** A user's own Switchboard CLI or MCP listener would then park in the
  host's pair, and could act in it.
- **The share store follows the same rule.** 2.6.0's `ShareStore` defaults to `~/.switchboard/pairs`
  (sb `sharing.py:263-281`), so the host passes a store of its own to every sharing call. It holds room
  keys. 2.6.0 asks for encryption at rest where the store is the only copy of a room's keys, for a room
  the machine created (`:237-240`); its own `ShareStore` keeps them in the clear, owner-only
  (sb `docs/sharing.md:533-536`). The host keeps room keys as it keeps the pair: in its OS-keyring Store,
  beside the pair, or in a 0600 file only under `--store-file`, with the same warning
  (spec/PROTOCOL.md §18).
- The host refuses insecure keyring backends it can recognise, and refuses to run as uid 0. It recognises
  keyring's `fail` and `null` backends, a chain with nothing in it, and a backend named `PlaintextKeyring`,
  as keyrings.alt names the one that writes secrets unencrypted to a file of its own (keyrings.alt 5.0.2),
  and a chain that holds any of them. `pair --store-file` pairs where the keyring stores nothing, since such
  a keyring holds no pair.

**Hazards neutralized,** each with a test:
- **The signer socket.**
  - Every Switchboard client attaches to any unix socket at its signer path when it is built. It sends
    `{op: pubkey}` at construction, and then the plaintext of each message it signs
    (sb `client.py:753`, `signing.py:392-406`). Shown same-user. From this it is inferred, unchecked, that
    on Linux with `XDG_RUNTIME_DIR` unset another local user could plant one under the shared
    `/tmp/switchboard/`.
  - So, before building any Switchboard client, pairing's included, the host sets `XDG_RUNTIME_DIR` for
    its own process to a private 0700 directory under its state dir. `signing.attach` then finds no
    socket.
  - It also sets `client.signing = SigningIdentity.generate()` on every client it builds, pairing's
    included, and builds none unless `XDG_RUNTIME_DIR` names that folder.
  - The variable is set for the whole process. Read from the source, not tried on a desktop: keyring,
    and the libraries it uses for the Secret Service on Linux, never read `XDG_RUNTIME_DIR` (jeepney
    finds the session bus from `DBUS_SESSION_BUS_ADDRESS` alone). From this it is inferred, unchecked,
    that the private folder does not disturb that lookup. Other keyring backends were not checked.
  - A test plants a socket at the signer path for the host's agent id, both under the `XDG_RUNTIME_DIR`
    the host was started with and, with that variable unset, under the system temporary folder, where
    2.6.0 looks then (sb `signing.py:245-247`). It runs pair, request and reply, and asserts the socket
    is never connected to.
- **`SWITCHBOARD_*` in the environment.** The host refuses to start if any is set.
- **Clock skew.** Writes fail with 403 beyond ±300 s of the hub's clock. `status` shows that as its own
  state (`clock: skewed`), not as a generic error, once `run` has met it: on a reply it posted, or on a
  write the library made for it in a touch, an accept or a leave. `pair` warns when the hub refuses its
  answer for it. `status` reads what `run` wrote, so it shows nothing of the kind before `run` has run.

## The read loop

- **Two kinds of room** (the split is this design's reading of agent-switchboard 2.6.0). One
  `Client.inbox` call reads one room (sb `client.py:1716-1731`), so the host reads:
  - `pair:<name>`, channel `pair` only, for the library's notes;
  - each room it accepted whose list still names its current pair, channel `lucille.exec` only, for
    requests. A pair forgotten, ended or paired again is only taken off the lists; the room and its keys
    stay held (sb `sharing.py:449-457`, `:499-508`), so the host skips it (this design's reading of the
    invariant "offered over the host's pair", under "Host invariants and their tests").

  It keeps one long-poll per held room that passed the hub and token checks (below), and one on
  `pair:<name>`'s `pair` channel, running at the same time (spec/PROTOCOL.md §7).
- **Taking an offer.** Once its local SAS gate is open, and never before, the host runs
  `sharing.touch(pairs=[name])` with its own stores when the gate opens, at start, on a `swbp1/shared`
  note, whenever it resolves (except after a failed touch), on waking from sleep, and every 5 minutes,
  whether or not it holds a room (spec/PROTOCOL.md §7). The wake is best effort and not retried (sb
  `sharing.py:1175-1186`), a heal's `swbp1/moved` note is best effort too, and the next resolve can be
  24 h away; a touch resolves the pair itself (sb `sharing.py:1082`), so the 5-minute touch also finds a
  move or an end within 5 minutes. With no pairs named, a touch reads every pair in the store (sb
  `sharing.py:1250-1266`), so the host always names its pair. It keeps an offer with `sharing.accept`, by
  its own policy, which 2.6.0 does not ship (sb `docs/sharing.md:594-595`). The policy is in
  spec/PROTOCOL.md §6: the offer came over the Lucille pair; the local SAS gate is open, since the library
  checks only the pair's `confirmed` flag (sb `sharing.py:1316`); the room's hub is the one the pairing
  code named, since the library checks only for an http(s) URL with a host (`:671-678`); the offer carries
  no token; and the host holds fewer than two rooms. The hub and token checks can run only right after
  `sharing.accept`, so a failure there leaves the room at once; they are run again for every held room at
  start and before each read or post in it. A refusal by the other three checks is not final: the offer
  stays pending and is considered again at start, when the SAS gate opens, whenever the host leaves a
  room, and whenever a touch reports a pending offer `left` or `withdrawn`; an accept that failed without
  refusing the offer (the hub or the pair room could not be read) is tried again after the next touch that
  reads the pair room. A newly accepted room replaces the old one only once it has passed the hub and token
  checks, and the host records that replacement before it accepts, so a restart completes it. While more
  than one offer over the pair is pending the host accepts none of them, since nothing it may read orders
  them; it first drops from its record any offer its share store no longer holds (spec/PROTOCOL.md §6). The
  host never takes back a room it has left: one offered again is accepted and left at once, unused, since a
  pending offer shows no workspace (spec/PROTOCOL.md §6, No re-accept).
- **Read with a since-cursor peek:** `Client.inbox(channels=[...], since=cursor, wait=25, limit=200)`,
  with a client timeout of 40 s, and one cursor per room (this design's reading: hub sequence numbers
  are global across rooms, below, so one shared cursor could skip a room's messages).
  - Never call `inbox()` without `since`. Without it the hub advances the cursor before the client
    decrypts, and one message it cannot open loses the batch.
  - **If the peek raises `DecryptionError`,** read the same channels with
    `Client.read_channels([client.cipher.blind_channel(c) for c in CHANNELS], limit=1000)`.
    - That call takes no `since`. It pages from seq 0 and returns the newest `limit` messages per channel,
      own posts included, with an unopenable row as `body: None, unreadable: True` and its seq
      (sb `client.py:1778-1823`).
    - The host handles, in seq order, every returned row with seq above the cursor up to the first
      unreadable one. It logs that one's seq, sets the cursor to it, and resumes the peek.
    - If no unreadable row above the cursor is in the window, it reports `read_stuck` and does not
      advance.
  - **A body that opens with the room's key but cannot be read** (not JSON, or nested more than 64 levels
    deep) is handled the same way. 2.6.0 raises `DecryptionError` only for a body that does not open; its
    JSON parser raises anything else past it (sb `crypto.py:365`, `client.py:1473-1481`), so the peek
    fails with an error the loop does not expect, and so does the recovery read, which marks only a
    `DecryptionError` unreadable. The host's own readers, in the work rooms and in pair:<name>, give the
    room's cipher a wrapper that raises `DecryptionError` for such a body (spec/PROTOCOL.md §19).
- **How the cursor advances.** `limit` is per channel on the hub, and hub sequence numbers are global
  across rooms (sb `store.py:76-77`). So:
  - the cursor advances only to the lowest last seq among channels whose page came back full (the rule
    `switchboard.client._Tail` uses), otherwise to the highest seq returned;
  - it is persisted only after the audit log records the message (at-least-once);
  - a newly accepted room's cursor starts at 0, and so does `pair:<name>`'s after a heal. Dedupe makes
    the re-read harmless.
- **Back off** on hub errors and timeouts, doubling to 30 s with jitter. On the hub's own 429, wait
  `exc.payload['retry_after']`. A proxy's `Retry-After` header is not visible through agent-switchboard
  2.6.0 (sb `client.py:366-378`). The loop never dies on one timeout.
- **Resolve** on start, on every `swbp1/paired`, `moved` or `ended` note, on any 403 or hub error, and at
  least every 24 hours. That keeps the side record (7-day TTL) fresh.
  - **Pair ended by Lucille** (`ended_by_peer`): the host calls `pairing.forget` to delete its own copy,
    says so, and stops. Resolve alone leaves the record in place (sb `pairing.py:2321-2325`).
  - **`pair:<name>` moved** (a heal the other side made, or a lapse): the host follows it and reads the
    new room from 0. A heal moves `pair:<name>`, not a room offered over the pair (sb `sharing.py:30-33`,
    `docs/sharing.md:526-528`), so the host follows the move for the library's notes. A move also counts
    as a suspected leak: the host abandons its work room (records that it does, then kills the request in
    flight, posts no reply, and leaves the room; after a restart it posts nothing there), and Lucille
    replaces the room (spec/PROTOCOL.md §9).
- **One request at a time.** Requests are handled in sequence order.

## The protocol: `lucille.exec/1` (spec in `spec/`)

[spec/PROTOCOL.md](../spec/PROTOCOL.md) states the protocol, with its JSON Schemas and golden fixtures
in `src/lucille_host/spec/v1/`. This section summarizes it. Where the two differ, the spec governs.

**Request** (Lucille → host), a JSON object in the sealed body:
- `{v: "lucille.exec/1", dir: "request", job_id, op, args, not_after, caps?, min_host_version?}`.
- `not_after` is when Lucille stops waiting.
- `caps`, when present, is `{stdout_bytes, stderr_bytes}`. For a child's output the host applies the lower
  of each request cap and its own, and never refuses because a request cap exceeds its own; a request's
  caps do not apply to `host.describe`'s document (spec/PROTOCOL.md §11). Lucille sends none in v1 (Lucille
  policy as of 2026-10-27; not proven by this repository).
- `min_host_version`, when present, makes an older host refuse with `host_too_old`.
- Anything else is refused with `bad_request` and never interpreted (inside `args`, `bad_args`).
- Each operation's `args` schema is part of the spec, and so binds Lucille as well as the host. The
  catalog's parameter schema for each operation must equal the spec's (spec/PROTOCOL.md §10).

**Reply** (host → Lucille), one per job id, except that a restart between posting a reply and recording it
as sent, or a retried post whose earlier attempt reached the hub unseen, can post more than one (after a
restart, a `failed interrupted` can follow a `done` already posted); the first counting reply for a job
decides it (spec/PROTOCOL.md §11, and its section 17):
- `{v, dir: "reply", job_id, status, code?, exit_code?, stdout?, stderr?, stdout_truncated?,
  stderr_truncated?, duration_ms?, policy_digest, host_version}`.
- `status` is `done` when the operation ran. A non-zero exit is data.
- `status` is `refused`, with a code, when it never started: `unknown_op`, `not_enabled`,
  `bad_request`, `bad_args`, `path_outside_root`, `expired`, `paused` or `host_too_old`.
- `status` is `failed`, with a code, when the host decided to run it and it did not finish: `timeout`,
  `killed_locally` or `interrupted`.
- The underlying error text never crosses the wire.
- `stdout` and `stderr` are decoded from the child's bytes as UTF-8 with `errors="replace"`, and U+0000
  is removed, before capping. A fixture covers non-UTF-8 and NUL output. Lucille rejects any string
  holding U+0000 or an unpaired surrogate (Lucille policy as of 2026-10-27; not proven by this
  repository).

**Agent ids.**
- The host's agent id is the literal `lucille-host`, fixed by the spec. Lucille's may differ per Lucille
  instance.
- The host ignores posts under its own id, and treats any `dir: request` in a room Lucille offered it
  over its pair that is not under its own id as Lucille's. That rests on a rule both sides are bound by:
  Lucille never shares a computer's work room with any other agent, and the host invites nobody. This
  repository can test only the host's half. It does not rest on the keys excluding anyone else, which
  they do not. Agent ids authenticate nothing (spec/PROTOCOL.md §8).
- Lucille applies a reply only if it is under the host's id, never applies its own post as a reply, and
  reads its own requests only to adopt one a crash left unrecorded (Lucille policy as of 2026-10-27; not
  proven by this repository).

**Rules both sides follow.** This repository can test only the host's half of each rule. Lucille's half
is Lucille policy as of 2026-10-27; not proven by this repository.
- **Dedupe.** The host acts on the first request per `job_id` it sees, and never again.
  - Its dedupe state is a separate file of job ids, each with when to prune it, written before the decision
    line. An entry is pruned 15 min after the later of `not_after` and the time the request was received,
    since an expired request is refused anyway.
  - It never depends on the rotating audit log.
  - The hub can deliver a genuine row twice, and a crashed Lucille can re-post.
- **Expiry.** A request past `not_after` is refused with `expired`, and never started.
- **TTLs.**
  - Lucille's request TTL ends at `not_after`.
  - The host's reply TTL is `(not_after − now) + 15 min`, at most 86400 s, and never the 1 h default.
  - Lucille caps the wait at 85,500 s, so the full 15 minutes always fits under the hub's ceiling.
- **Size.** Caps are measured on the serialized JSON, not on raw bytes, with every non-ASCII character
  escaped and no insignificant whitespace (spec/PROTOCOL.md §13).
  - Measured locally: 64 KiB of ASCII on each stream seals to 176 KiB, and the same raw bytes as 2-byte
    UTF-8 text to 518 KiB.
  - The host's 64 KiB-per-stream cap on the JSON-encoded string keeps a reply under the 256 KiB cap
    Lucille applies to a reply.

## The closed operation set

**The release defines the operations.**
- Each operation is a reviewed catalog entry in `src/lucille_host/ops/`: the executable, an argv
  template per OS, a typed parameter schema, and limits. A new operation means a new public release.
- The local policy file only enables entries, maps folder aliases to paths, and tightens limits
  (tightening limits is not built yet).
- User-written command templates are rejected. A template such as `["/bin/sh", "-c", "{x}"]` would turn
  the host into a remote shell, which is what this design exists not to be.
- Ops are namespaced: `host.*` and `files.*` first, later `git.*`.

**The v0.1 catalog** (the sha256 proof first, then file search):
- `host.describe`. No subprocess. Returns the host version, protocol, OS family, catalog version, enabled
  op names, root *aliases* (never paths), and the policy digest.
- `host.sleep {n: int 1..240}`. Diagnostic, off by default. It runs `sleep` through the executor, with a
  catalog timeout of n + 30 s. It exists to test waits, deploys and lid close.
- `files.sha256 {root: alias, path: relative string, at most 1024 chars, no NUL}`. Off by default.
  - argv `[/usr/bin/shasum, -a, 256, --, <path>]` on macOS, and `[/usr/bin/sha256sum, --, <path>]` on
    Linux, with `cwd` set to the root's realpath.
  - It shells out on purpose, because the first end-to-end run has to prove the exec path.
  - The digest leaves the computer. The content does not.
- `files.search {root: alias, pattern: string, at most 256 chars, fixed_strings: bool, max_count: int
  1..200}`. Off by default.
  - It runs ripgrep with `cwd` set to the root's realpath and no path argument, so every path in its
    output is relative.
  - Flags: `--no-config` and `--no-follow`; no hidden files unless the policy allows them; the pattern
    after `--`.
  - The `rg` executable is taken only from a catalog-listed set of absolute paths per OS. The policy may
    pick one of them, and the op is unavailable if none exists. It is never a `PATH` lookup.
  - **Its output carries lines of file content.** The claim says so.

**The policy digest.** `policy_digest` is an HMAC-SHA256 of the canonical local policy, under a random key
that never leaves the computer. The user can match a result to a policy, and Lucille can tell when the
policy changed. A plain hash would let anyone holding it confirm a guessed folder path.

## Execution rules (`_exec.py`, the only process-spawning call site in the runtime package)

- **argv only.**
  - `shell=False`, and the executable is an absolute path from the catalog.
  - Its sha256 is recorded in the audit log, but not enforced, because OS updates change it. For a
    script, the hash does not cover the interpreter (macOS's `/usr/bin/shasum` is a Perl script).
- **User values come after `--`.**
- **Paths.**
  - Absolute paths and `..` are refused.
  - Each path must resolve inside its root, walked one component at a time beneath a descriptor on
    the root's real path, following symbolic links as the kernel would (`containment.py`), never by
    `realpath` on the joined path, which skips a component it cannot `lstat` past PATH_MAX. A symlink
    escape fails with `path_outside_root`, and so does a link the walk cannot check.
  - No path goes through Linux's `/proc`, and a root on it holds nothing: a link there
    (`<pid>/fd/0`, `<pid>/root`) takes the kernel to an object its text does not name, and `self`,
    `<pid>/cwd` or a process not yet started mean the host when the host checks and the child when
    it opens the path, so no lookup the host makes can stand in for the child's. On Linux, where
    the mount table lists no `/proc` (not mounted, or covered), every path is refused. After the
    walk the host also looks the whole path up itself and refuses unless that finds the object the
    walk ended on (or nothing, where the walk found nothing).
  - A path naming something other than a file or a folder (a FIFO, a socket, a device) fails with
    `path_outside_root`: opening a FIFO blocks, or takes data another program was waiting for.
  - The host's own files (its config and state folders, for every profile, and everything in them) are
    outside every root. They are compared by device and inode, with links in them followed and not, so
    a second mount of one of its folders, a hard link to one of its files, or a folder a link in them
    points to is caught: a path reaching one fails with `path_outside_root`,
    and a root inside one cannot be added. A hard link to an earlier version of a file the host has
    since replaced is not caught.
    Otherwise a root holding `~/.config` would let Lucille hash `policy.toml` and confirm a guessed
    folder path, which the policy digest's key exists to prevent.
  - A file operand of `-` is passed as `./-`: most programs read standard input for `-`, even after
    `--`.
  - A root that is `$HOME`, holds it (`/home`, `/Users`, macOS's `/System/Volumes/Data`), or is `/`
    needs an explicit flag. `policy.holds` decides "holds it" by path, by device and inode, through
    any other mount of the same filesystem that Linux's mount table lists, and through macOS's Data
    volume; `roots add` also looks below the folder for `$HOME`'s own path (only then, since a stalled
    mount there would hang every policy load). A folder reaching `$HOME` any other way (another
    filesystem's view of it, a mount this process cannot see) is not recognised.
- **Child processes.**
  - stdin is `/dev/null`.
  - The environment is built from an allowlist (a fixed `PATH`, `LANG`, `LC_ALL`), never copied from
    `os.environ`.
  - Each child runs in its own session, and the whole process group is killed on timeout (30 s by
    default, 300 s at most) or on `kill`.
- **Output.**
  - stdout and stderr are read incrementally, decoded as the protocol says, and capped at 64 KiB each on
    the JSON-encoded string, with truncation flags.
  - A best-effort redaction pass (PEM blocks, common key prefixes) runs before anything is sent. It
    treats an unterminated `BEGIN … PRIVATE KEY` block as running to the end of the buffer.
  - Redaction is a convenience, not a guarantee.

## Local policy and upgrades

- **One file.** The policy is one TOML file in the platform config dir, read with stdlib `tomllib`. Only
  the local CLI verbs change it.
  - An invalid file stops the host (fail closed).
  - The file is reloaded only on restart or on a local signal.
  - No protocol message has a field that maps onto policy.
- **Upgrades.** Each enabled entry is stored with the hash of its catalog definition. If an upgrade
  changes an enabled entry, the host disables it until the user re-enables it.
- **No confirmations yet.** Every v0.1 op is read-only. Confirm-before-mutation and "allow for N minutes"
  grants arrive with the first mutating op.

## Audit log

- Append-only JSONL, mode 0600, in the platform state dir.
- Each line is written before the host acts on what it records:
  - **received:** job id, hub sequence number, op, and args as received;
  - **decision:** run, or refused with its code;
  - **start:** binary path and its sha256, and resolved argv, before the process starts, so that a crash
    mid-run still leaves a record of what ran;
  - **execution:** how it ended, exit code, duration, output sizes, with the binary, its sha256 and the
    argv again;
  - **sent:** the sha256 and size of the exact bytes about to be posted.
- Output bodies are kept only when the user sets `log_results = true`.
- Files rotate by size and are never edited. No key is ever written.
- The log is user-writable. It records; it proves nothing against someone with the user's privileges.

## Pause, kill and shutdown

- **`pause`** writes a flag file. While it is set, the host starts nothing: the flag is read when a
  request is decided and again just before its process starts, and a pause that lands in between
  turns the decision into `failed` with `interrupted`.
  - If it is online, it replies `refused` with `paused` to each new request, so the job fails fast.
  - If it is offline, nothing happens and the requests expire.
- **`kill`** (or a second Ctrl-C) also sets the flag. It kills the in-flight process group, and replies
  `failed` with `killed_locally`.
- **The first SIGTERM, SIGHUP or Ctrl-C drains** (a signal ignored when the host started, as SIGHUP is
  under `nohup`, stays ignored). The running op finishes and replies, and nothing new starts, an op
  decided but not yet started included (`failed` with `interrupted`). Only a running host enforces a
  time limit or a kill: if the host is killed or crashes while an op runs, the op keeps running without
  either (THREAT_MODEL.md, "Known gaps").
- **Removal on the Lucille side** ends the pair there (Lucille policy as of 2026-10-27; not proven by
  this repository).
  - If Lucille's forget reached the hub, the host learns it on its next resolve, forgets its own copy,
    and stops.
  - If not, the host keeps its half until `lucille-host forget`. Lucille's page says the computer was not
    told (Lucille policy as of 2026-10-27; not proven by this repository).
- **Nothing Lucille sends can unpause a host.**

## Sleep and wake

- **No leases, no heartbeats.** A request waits on the hub until its `not_after`. The host is the only
  reader that matters.
- **When the computer wakes,** the host reads each room it accepted over its current pair from that
  room's own cursor ("The read loop") and runs each request still inside its `not_after`. A wake from
  sleep also touches the pair, for offers that arrived meanwhile (spec/PROTOCOL.md §7).
  - The hub drops a request at its `not_after`, so a request that expired while the computer slept is
    simply gone.
  - The host can still refuse `expired`: when its clock runs ahead of the hub's; inside the up to 1 s of
    slack that rounding the request's TTL up adds; or when a request it read in time waited past its
    `not_after` behind an earlier request, or across a sleep that began after the read
    (spec/PROTOCOL.md §14).
- **A lid closed mid-operation** suspends the child with the machine. The reply goes out when it finishes.
  If that is after Lucille's grace, Lucille drops it, and the job has already failed with "no reply"
  (Lucille policy as of 2026-10-27; not proven by this repository). That window is the cost of keeping
  no lease. It has not been measured yet; the first end-to-end run is to measure it.
- **A host restart mid-operation.** A request with a decision to run but no sent line gets
  `failed interrupted`, and never a second run. A refusal with no sent line is posted again, unchanged.
- **A transport failure is never a `failed` reply.** A hub error does not stop a running child, and a
  failed post of a reply is retried until the reply's TTL would already have run out. 2.6.0's posts carry
  no idempotency key, so a retry after a post that reached the hub unseen posts another copy.

## CLI verbs (v0.1)

`pair [CODE] [--code-stdin] [--hub URL] [--store-file]`, `pair status`, `pair confirm`, `pair reject`,
`run`, `status`, `log [--limit N]`, `forget`, `ops list|enable|disable`, `roots list|add|remove`,
`policy previous`, `pause`, `resume`, `kill`. They print text, with the state word first where there is
one (`waiting`, `sas_pending`, `confirmed`, `leaked`, `expired`, ...).
- `status` makes no hub call. It shows the version, the policy check, the enabled operations and folder
  aliases, whether the host is paused, where the pair is kept, the hub and a loopback hub if one is
  configured, the pair's state (never its SAS: `pair status` shows that), each work room held or that it
  holds none (with the check its last room failed, when it left that room on its own), and what `run`
  last saw: whether it runs, its last contact with the hub, and the clock skew, which shows as its own
  state past ±300 s.
- `log` prints the last lines of the audit log, oldest first, across its rotated files.
- `run` holds `state/.run.lock` (`os.lockf`, taken without waiting) for as long as it runs: a second `run`
  of the profile, in another shell or the same process, refuses at once (exit 1) and changes nothing,
  since two would each read and answer every request. `run` refuses a pair with no gate record, which
  can never be confirmed on the host, as `pair status` and `pair confirm` do.
- `forget` leaves the pair's work rooms, then calls `pairing.forget`, which deletes the pair from the
  store and then tells Lucille, best effort. It prints whether Lucille was told. It marks every room
  abandoned in the room record before the first leave, and holds the record lock until it is done,
  through hub calls that can each take up to the client's timeout. A `run` going at the time does not
  wait for that lock to stop its work in those rooms: its worker, readers and poster read the room
  record and the gate's record themselves, with no lock, before each request, read and post and every
  0.05 s while a child runs. So a request running from a marked room is killed then, with no reply, and
  nothing more starts from it. `run` itself stops at its first step once `forget` is done (until then a
  drain waits too), by the gate's record: `run` notes which record it started with (the pair's name, and
  when the record was made), and a record that is gone, names another pair, or was made for another
  pairing under the same name stops it, whether or not Lucille was told. Stopping so, it touches nothing
  of what the profile holds, which may already be a new pair's.
- `--profile <name>` selects a separate config dir, state dir and store, so that two computers can be
  simulated on one machine.

## Packaging, releases and verification

**Packaging.**
- A pure-Python wheel (`py3-none-any`) for Python 3.11 or later, installed with
  `pipx install lucille-host==X` or `uv tool install lucille-host==X`.
- Runtime dependencies are `agent-switchboard==2.6.*` (base) and `keyring`, with their own dependencies,
  all pinned exactly in the release's lock. The wheel's metadata declares the ranges, not the lock's
  pins, so an install with `pipx` or `uv tool` can resolve other versions. How a release carries the
  exact pins to an install is not decided yet. The build of Capsule, the Mac app (`macos/`), is one
  channel: it installs the lock's exact pins, by hash, into the Python runtime it embeds. No Mac
  release exists yet.
- An extra, `[conformance]`, adds `agent-switchboard[server]` at the runtime pin and `starlette>=1.9`, for
  the conformance suite only. A test asserts that the runtime import graph never reaches `fastapi` or
  `uvicorn`.
- An extra, `[verify]`, adds `pypi-attestations` for `lucille-host verify`, which a later release adds.
- There is no auto-update.

**`release.yml`** runs on protected `v*` tags, on GitHub-hosted runners only, with every action pinned by
commit SHA:
1. **Build**, with a tag-equals-version guard, an exact-pinned build backend, and `SOURCE_DATE_EPOCH`.
2. **Publish**, in environment `pypi`, which needs the repository owner's approval. It uses PyPI Trusted
   Publishing through `pypa/gh-action-pypi-publish`, which uploads PEP 740 attestations.
3. **Post-publish:** download from PyPI, install `lucille-host[conformance]` into a fresh venv, and run
   the conformance suite.
4. **GitHub Release**, with `SHA256SUMS`.

A workflow-lint test rejects any `self-hosted` label, and any action not pinned by SHA.

**Before anyone other than the repository owner pairs a computer,** a later release adds:
- a rebuild job that compares sha256s;
- `lucille-host verify`.

That nobody else pairs until then is Lucille policy as of 2026-10-27; not proven by this repository.

**A bad release.**
- Yank it on PyPI, and publish a GitHub security advisory.
- Lucille sets its minimum host version, and its requests then carry `min_host_version` (Lucille policy
  as of 2026-10-27; not proven by this repository). An older host refuses them with `host_too_old`.
- The bad code stays on a user's computer until they upgrade, and `SECURITY.md` says so.
- **A managed-hub upgrade that breaks 2.6 clients needs a host release first,** because hosts pin 2.6
  and do not auto-update.

## How a third party verifies the host (`VERIFYING.md`)

1. **Read.**
   - `src/lucille_host/ops/` is the complete list of operations Lucille can ask the host to run.
   - `CLAIMS.md` maps each sentence of the privacy claim to the code and test behind it.
   - CI fails on `shell=True`, `os.system`, `eval` and `exec`, and on dynamic imports, anywhere in the
     repository.
   - In the runtime package, CI fails on any process-spawning call outside `_exec.py`, on any import of
     a module outside a short, reviewed allowlist, and on any `switchboard` import outside the one module
     that wraps it.
   - Everywhere, CI fails on any use of `register()` or an invite. Outside `src/lucille_host/conformance/`
     and the tests, whose fake Lucille must mint and offer rooms as Lucille does, it also fails on any
     `sharing.share_new`, on any other `switchboard.sharing` call outside the ones "Transport" lists for
     taking Lucille's offers, and on any part of `switchboard` outside the surface the
     Switchboard-surface lint lists.
   - Those are the rules. The guards that enforce them catch the forms they list (the AST lint catches
     `compile` as well), and they do not see every indirection. The AST lint is a denylist of known
     forms, not a sandbox. The Switchboard-surface lint does not follow a method called on a
     Switchboard object, such as a client, other than `register`, invites and private members, nor a
     value a wrapper function returns. Review is the backstop. `VERIFYING.md` says what each guard
     catches and what it cannot see.
2. **Check provenance.** Every file on PyPI carries a PEP 740 attestation:
   - its publisher names repository `gald33/lucille-host`, workflow `release.yml` and environment `pypi`;
   - its signing certificate's Runner Environment extension reads `github-hosted`.

   The same check works for the pinned `agent-switchboard` release: its publisher is `gald33/switchboard`,
   `publish.yml`, environment `pypi`, `refs/tags/v2.6.0`. That certificate was decoded on 2026-10-19,
   not cryptographically verified.
3. **Run** the suite: install `lucille-host[conformance]==X` into a fresh venv, then run
   `python -m lucille_host.conformance` offline, against a local Switchboard hub it starts. It attempts
   each widening listed under "Host invariants".
4. **Watch.** `lucille-host log` shows every request, every local decision, and a fingerprint of
   everything sent; `status` shows the pair, its work rooms, and when the host last reached the hub.

**What verification cannot establish:**
- anything about what Lucille's backend does with a reply;
- whether installed files are still the verified ones after any process running as the same user has
  touched them;
- the provenance of dependencies beyond the ones checked;
- tag protection and the `pypi` environment's required reviewer, which are repository settings.

## Repository contents and guards

**`spec/`**
- `PROTOCOL.md` for `lucille.exec/1`: channel, envelopes, agent ids, TTLs, dedupe, expiry, decoding,
  refusal codes, and what each side does on heal, end and expiry.
- `PROTOCOL-CHANGELOG.md`. A minor change must be additive. A major ships only after Lucille supports both
  majors.

**`src/lucille_host/spec/`**
- JSON Schemas (draft 2020-12) with `additionalProperties: false` at every level, in `v1/`.
- Golden fixtures in `v1/fixtures/`: one per message shape and per code, invalid ones for each refusal
  reason, and decoding fixtures, with obviously fake UUIDs.
- `lucille_host.spec`, which reads them from wherever the package is installed.

The schemas and fixtures ship in the wheel as package data; `tests/spec/test_packaged.py` loads them from
an installed wheel. The prose stays in `spec/` at the root and is not in the wheel.

**`src/lucille_host/conformance/`** is a hostile fake Lucille. It drives a real local hub: `switchboard
serve` on 127.0.0.1 for the scenarios, and the in-process `switchboard.testing` hub for the vendored Store
template test, which needs that hub's injected clock. It may spawn only the local hub and the installed
`lucille-host`, under a lint of its own. The runtime package's lints exempt this directory explicitly.

**Docs:** `PRIVACY.md` (the claim plus the must-not-claim list), `CLAIMS.md`, `THREAT_MODEL.md`,
`VERIFYING.md`, `SECURITY.md` (GitHub private vulnerability reporting), `decisions/`.

**CI guards:**
- the workflow lint and the AST lint above;
- the Switchboard-surface lint above;
- a private-string check for strings private to Lucille that must never appear in this repository. Its
  list lives in a repository secret, because a committed denylist would publish the strings it protects.
  It reads every path git tracks (a gitlink's path included) and every tracked file's contents, as UTF-8
  lines and again as UTF-16.

**`LICENSE`:** MIT.

## Must not claim (`PRIVACY.md`)

- "Your data never leaves your device", "private" with no qualifier, or "Lucille cannot see your files".
- **"End-to-end encrypted"** without saying where the far end is. The traffic is sealed between the
  computer and Lucille's backend, and Lucille reads it.
- "Sandboxed", or "safe to enable anything". Operations run with the user's own permissions. An allowlist
  is not a sandbox.
- "Read-only" as a guarantee. It describes catalog entries, checked by review.
- "A hacked Lucille cannot read your files". A compromised or prompt-injected backend can request any
  enabled operation, with any valid argument, whenever the host runs. Only the local policy bounds it.
- "Passwords and keys never leave". They leave if they are inside a file an enabled operation reads.
- "Tamper-proof log", "only lucille-host can read its key", or "secrets are removed from output".
- "Removing it ends the pairing on both sides", without the caveat that the other side is told only when
  the hub is reachable.
- "Deleted", without the caveats: the blank is Lucille policy, not host code, and it does not reach
  Lucille's database backups, which this repository cannot audit. Requests and replies also sit sealed on
  the hub until their TTL.
- "Verified" for a user who has not verified, "reproducible" for a release whose rebuild was not green,
  or "audited" before anyone has audited it.
- Anything about the AI provider's own retention.
- That open source, or a verified release, proves anything about Lucille's backend.

## Host invariants and their tests

| Invariant | Test |
|---|---|
| Nothing Lucille sends can widen what the host permits | Conformance tries each of these: an unknown op, a not-enabled op, extra keys (`exe`, `argv`, `env`, `cwd`, `timeout`), policy- and update-shaped fields, traversal, an absolute path, a symlink escape, a file named `--version`, and oversize args. Each is refused with the right code, the policy digest is unchanged, and the spawn count is 0. `caps` above the host's own are not refused, but the effective cap never exceeds the host's own |
| No shell; one spawn site; children get a built environment and no key | The AST lint. A fixture that dumps its environment shows only allowlisted keys |
| Nothing runs before the local SAS confirmation | A fake Lucille offers a room and posts a request before the user confirms the SAS: no touch, no accept, no spawn, no reply; `pair confirm` before the host has shown the SAS confirms nothing; once the SAS is shown and confirmed, the request runs. At `pair`'s prompt on a terminal, a `y` typed before the question is discarded. `pair confirm` after the code has expired still opens the gate. `n`, `pair reject` and the deadline each forget the pair, and a confirmation made in time is never expired by `run` or `pair status` judging the gate beside it |
| One run per job id, never after `not_after` | A duplicated request runs once. A second `run` of the profile refuses while one runs, and two processes writing the journal at once never both take a job id. An expired request is refused with no spawn. A restart between a decision to run and sent gives `failed interrupted`, with no second run; a refusal not yet sent is re-posted unchanged |
| Only `lucille.exec`, in a room Lucille offered over the host's pair and the host accepted, is acted on | A request on `pair`, in `pair:<name>` on any channel, or in a room not offered over the host's pair or not accepted, is ignored. A `swbp1/paired`, `moved` or `ended` note only triggers a resolve, and a `swbp1/shared` note only a share read |
| No request is skipped | A test fills `lucille.exec` past one page: none skipped. A heal mid-stream does not move the work room, but counts as a suspected leak (spec/PROTOCOL.md §9): the request in flight in the old room is killed with no reply, the host leaves that room, and a request Lucille posts in the replacement room before the host accepts it still runs |
| The host never invites anyone into its pair's rooms, and offers no room of its own | No invite or `share_new` call exists, and no verb makes one (lint plus test). It only accepts Lucille's offers. 2.6.0's accept writes the host's record back to Lucille carrying that same room's offer, the sharer's "accepted" (sb `sharing.py:1546-1550`), so no test asserts that the host writes no offer record |
| Failures cross the wire as codes only | A sentinel in stderr from a refused op never appears in a sent body |
| Output is bounded, decodable and path-free | A 1 MiB output comes back truncated and flagged, and the serialized reply stays under 256 KiB. Non-UTF-8 and NUL output decode without U+0000 or surrogates. No reply contains a root's realpath or `$HOME` |
| The host's stores and Switchboard footprint are its own | No write under `~/.switchboard`, from the pair store or the share store, no subprocess from pairing, a planted signer socket is never connected to, and the Store template test passes |
| An upgrade never runs a changed op under old consent | Change an enabled entry's definition, then upgrade: the entry is disabled |
| No listening socket | An audit hook sees no socket bound while the host pairs, runs and answers a request |
| Every release comes from a hosted runner and environment `pypi` | The workflow lint, and a post-publish provenance check |

## Threats the host bounds

| Threat | What bounds it | What is left |
|---|---|---|
| Compromised or prompt-injected Lucille backend | the release catalog, enabled ops and roots; v0.1 is read-only; only `host.describe` is on after pairing; `pause` and `kill` work offline | anything enabled ops can read under enabled roots, while the host runs |
| Intercepted or phished pairing code | the code must name Lucille's hub and carry no token; nothing else is accepted except a loopback hub the local config names (for conformance); the SAS is compared and confirmed on both screens: the host shows it and asks for `y` if Lucille's page shows the same (Lucille's screen and its own confirmation: Lucille policy as of 2026-10-27; not proven by this repository) | a user who confirms without comparing |
| Someone else's pairing code (an attacker pairing the computer to themselves) | the hub check; the local SAS gate, which catches this only when the user compares against Lucille's own page; only `host.describe` enabled after pairing | a user who confirms the SAS the attacker shows (it matches the host's), or confirms without comparing, then enables ops |
| Leaked `pair:<name>` key, offered work-room key, or pair secret | Only two places hold the pair, and both hold it with the `pair:<name>` key: the host's store (local, owner-only, same-user malware is out of scope) and Lucille's database (encrypted with Lucille's master key: Lucille policy as of 2026-10-27; not proven by this repository). Signing requests with a key pinned at pairing would not help, because every leak path found exposes that key with the pair. The response is to remove the computer, or `forget`, and pair again. v0.1 has no heal verb. This analysis was written for `pair:<name>`'s derived key. That key opens nothing shared (sb `docs/sharing.md:526-528`). The offered work rooms' keys sit in each side's share store as well as in the pair room's offer records, and of a share store, "A copy of it opens every room in it" (`:533-536`). That every leak path exposes those keys with the pair was not re-checked for the offered rooms. spec/PROTOCOL.md §18 states what a room's key opens (every message still on the hub in that room; epoch rotation does not bound it), and §19 that nothing in v1 detects a leak of a room's key alone | the holder can read and forge traffic, and ask for enabled ops until then |
| Malicious payload (metacharacters, traversal, flag or env injection) | schemas, argv only, `--`, paths resolved one name at a time beneath their folder, the built environment, conformance | none known |
| Malicious release, or compromised maintainer account | protected tags, the `pypi` environment, which needs the repository owner's approval, attestations (they prove where a release was built, not what it does), a public diff of the catalog and `CLAIMS.md`, exact pins in the lock CI uses (see "Packaging"), no auto-update | a validly attested malicious release from an account that holds both tag and environment rights |
| Compromised dependency, including `agent-switchboard` | minimal dependencies, exactly pinned in the lock CI uses (an install with `pipx` or `uv tool` resolves the declared ranges; see "Packaging"); Switchboard's own provenance checked | dependency code runs with the host's privileges |
| Same-user malware | out of scope, and stated | it can read the store, edit site-packages and rewrite the log |
| Another local user planting a signer socket | the private `XDG_RUNTIME_DIR` and the generated signing identity | not tested cross-user; the test is same-user |
| Hazards in external binaries (config files, flags) | each catalog entry is reviewed; absolute paths; the env allowlist; `rg --no-config` | each new op widens this surface |

## Choices made in building pairing and the read loop

The spec and the sections above leave these to the host. Each was made with the code on 2026-10-30, and
each could be made another way in a later pull request that updates this list.

**The stores.**
- **One keyring item per profile.** Service `lucille-host`, the profile's name as user name. It holds one
  JSON document: the pair store's records, pending handshakes and verdicts, and the share store's own
  document under `shared`. The pair store and the share store are two views of it (`KeyringPairs` and
  `KeyringRooms` in `_switchboard.py`). One item is one keychain entry to unlock and to remove.
- **Three lock files** in the state folder: the pair store's lock, the share store's (2.6.0 asks for a
  second one, sb `sharing.py:241-244`), and one around each read-modify-write of the item, never held
  across a hub call. Each is `os.lockf` beside a `threading.RLock`, one lock object per file per process:
  `lockf` keeps other processes out, not other threads. The kernel sees processes, so it can refuse a lock
  with `EDEADLK` when one thread of `run` is inside the item's lock while another waits for a lock that a
  command holds as it waits for the item's. No lock is ever taken while the item's is held, so that is no
  deadlock: the host asks again after a pause, 10 ms doubling to 0.5 s.
- **A fourth lock file**, `.record.lock`, is held while the record of offers and rooms is read, changed
  and written whole, so that `run` and another command (`forget`, say) never change it at once. Without it,
  `forget` could write over a room `run` had just recorded, and that room and its keys would stay in the
  share store after the pair was gone. `run` holds it through each step of its session, hub calls
  included, so a command waits for that step to end; and `run`'s main thread waits for a command's
  (`forget`'s leaves and its note to Lucille). Its other threads never take it: they only read the room
  record and the gate's record, each written whole, to stop the work of a room another command marked.
- **Two more**: `.run.lock`, held by `run` for as long as it runs, so that a profile runs one `run`; and
  `.journal.lock`, held around each read and each read-modify-write of the journal.
- **Keyring writes are throttled, not measured.** A save that changes only when the pair or a room was
  last read or used is skipped while the item was written less than an hour ago; every other change, and
  every delete, is written before the call returns. In a simulated day of 5-minute touches and a room
  look-up before each read, the item is written 24 times (`tests/test_rooms.py` holds it to at most 30);
  without the throttle the same day wrote it 4032 times, measured once. The stamps it delays decide only
  whether a pair has gone unread for days, and what the library shows about a room. What a keychain write
  costs on macOS, and whether it prompts, is not measured.
- **`--hub` and `--store-file` are options of `pair`,** saved in `config/host.json`, which every later
  verb reads; a file that cannot be read stops the verb. `--hub` takes only `http://127.0.0.1:<port>` or
  `http://localhost:<port>`. Under `--store-file` the host warns, wherever the store is opened, that the
  file store keeps the pair secret and the rooms' keys unencrypted, in files only the user can read, and
  that this is weaker than the OS keyring.
- **`forget` leaves the keyring item in place,** holding no pair and no room key (what 2.6.0 keeps of a
  room left holds none). It also drops how far each room was read, and keeps the journal: a job id is
  answered once per profile, whatever pair it came over.

**Pairing and the gate.**
- **The gate's record** is `state/pair.json`: the pair's name, the hub its code named (check 3 compares
  against it), when the record was made, when the SAS was first shown on the host and the deadline that
  sets, when the gate opened, and when the first touch after that ran. It holds no SAS, no code and no
  key. It is written before the code is answered and deleted whenever the pair is forgotten: only the
  record of the pair forgotten, made when that pair was, so a record another pairing wrote since stays. A
  record that is missing, or names another pair, keeps the gate shut. The record is written, changed,
  deleted and its deadline judged only under the record lock (`state/.record.lock`) that `run` holds
  through each step and forgetting holds throughout: so a `pair confirm` made in time is never expired by
  a `run` or a `pair status` that read the record before it, no showing or confirmation writes a record
  back for a pair being forgotten, and `pair reject` (or `n`) beside a `pair confirm` either finds the gate
  open, and refuses, or forgets the pair before the confirm finds it gone. Forgetting leaves the work
  rooms only while the record of rooms names that pair, or a pair no longer held.
- **The policy is reset right before a new code is answered,** once its hub and token checks pass: a
  policy that is not the fresh one moves to `config/policy.previous.toml`. A fresh or missing one is never
  set aside over an earlier one. Before the answer, not after, so that a stop between the two never leaves
  the old folders enabled for the new pair; a code the library then refuses has still reset the policy.
- **Exit codes:** 0 when `pair` stops at `sas_pending` (with `--code-stdin`, when stdin ends, or when
  what was typed ahead at the terminal cannot be discarded) or the gate opens, and when `run` is drained
  or the pair ends; 1 for every refusal and every pairing that ends early, `n` at `pair`'s prompt among
  them (`rejected`), with the state word first on stderr.
- **The startup checks** (not root, no `SWITCHBOARD_*` variable, a keyring backend that stores, a private
  runtime folder) run for the pairing verbs, `run`, `status` and `forget`; the other verbs touch no store
  and no hub.

**The work rooms.**
- **The record of offers and rooms** is `state/rooms.json` (`rooms.py`): each offer as the library first
  notes it, before anything is decided; the accept about to be made and the rooms it replaces; the rooms
  held, under names the host chooses (`room-<n>`, never reused, so no name Lucille chose collides with one
  the host holds); every room this profile left, by workspace, kept across pairs; and the check its last
  room failed when it holds none. 2.6.0 reports a pending offer once, as an `offered` note written only
  when the offer is first seen (sb `sharing.py:842-846`), and `sharing.status`, which lists the offers
  waiting, is not among the calls "Transport" lists, so the record is the host's own. A call returns only
  the notes it made, and an accept that refuses returns none, though its own read of the pair room may
  have noted a new offer (sb `sharing.py:1583-1627`). So the host reads the notes from its share store,
  past the last id it read (ids only grow, `:389-393`), after every sharing call, refused ones included.
  It keeps that id in memory: a stop between a note and the record is the case spec/PROTOCOL.md §19
  leaves open.
- **A room left is never taken back.** None of the four sharing calls shows a pending offer's workspace,
  and 2.6.0 makes an offer pending again when a room this computer left is offered over a new pair. So such
  a room is accepted and left at once, as a room failing check 3 or 4 is (spec/PROTOCOL.md §6 says so).
- **An accept that fails in passing is tried again.** When the hub, or the pair room, could not be read
  inside an accept, the offer is considered again after the next touch that reads the pair room: the
  library notes an offer once, so no later touch would report it again. When the local name the host chose
  is taken, the next consideration chooses another.
- **An offer is dropped from the record when `resolve_room` says `unknown`,** and not on `left`: it looks a
  name up as a held room, then a room left, then an offer, so an offer named like a room left reads `left`
  while it is still pending.
- **A move of pair:<name> is found by its generation,** compared with the one the record holds, never by
  the library's `moved_from`, which only the resolve that made the move sees. A touch that read the pair
  room compares the generation the store keeps too, and asks for a resolve when it differs: a move
  another command followed first (`pair status` resolves) leaves that touch nothing to move and no note
  to write. The rooms are abandoned before the new generation is recorded, the room of an accept a stop
  interrupted among them, unjudged: it was accepted over the pair as it stood before the move.
- **Resolves back off** after a failure: the hub's `retry_after` on a 429, otherwise 1 s doubling to 30 s
  plus up to a quarter more, drawn from `secrets`; a resolve that could not read the pair room counts as
  a failure. 2.6.0 does not raise when the hub turns away its read of the pair room: `pairing.resolve`
  returns what the computer last saw, unchecked, with the hub's error in `Resolution.hub_error`
  (sb `pairing.py:2278-2290`), and the host takes `retry_after` from that error. `pair`, waiting for
  Lucille, waits that long too, when it is longer than its own 5 s. A hub error another part of the host
  meets asks for a resolve only when none ran in the last 30 s.

**The read loop and replies.**
- **The journal** is `state/journal.json` (`journal.py`): for each job id, its room, sequence number,
  receipt time, prune time, the request's body (kept only up to 16 KiB, and only until it is decided), the
  decision, the refusal's bytes (until they are sent), whether the reply was sent, and whether its room was
  abandoned. The entry is written before the request is decided, the decision right after the audit log's
  `decision` line and before anything runs, and `sent` once the hub accepted the post. Writing the entry
  takes the job id: the look for an entry and the write are one step, so a job id another thread or
  process took first is a duplicate. Each read and each read-modify-write holds `.journal.lock`
  (`os.lockf` beside a thread lock), so neither another thread nor another process writes in between.
  A change to an entry another write has pruned changes nothing: past its prune time the request is
  settled. A request received
  and not decided before a restart is decided afresh from that body: the range of its `not_after` from the
  time it was first received, its expiry at the time of the new decision.
- **Cursors** are kept per room workspace in `state/cursors.json`, each written after its message's audit
  lines and journal entry. pair:<name>'s is not kept: its notes are wakes, and a start resolves and
  touches anyway. A new room, or pair:<name> after a move, is a new workspace, so it is read from 0.
- **Replies are posted by a thread of their own,** so the next request does not wait while a reply is
  retried, and a write outage (a skewed clock, say) does not expire every request behind it. Each reply
  retries with its own back-off until its TTL would have run out; it is then dropped and logged.
- **Waking from sleep** is a wall clock that ran more than 30 s further than the monotonic clock between
  two of `run`'s once-a-second looks (inferred: monotonic clocks stop in suspend on macOS and Linux; not
  measured on either).
- **The clock skew** `status` shows is measured on the host's own posts: the hub's `created_at` minus the
  local time halfway through the call. It shows as `skewed` once the hub refused a write with a signature
  outside its time window (sb `writekey.py:223-224`): one of the host's posts, or one the library made for
  it in a touch, an accept or a leave, where the library keeps the hub's error as text (sb
  `sharing.py:991-1001`, `:1583-1589`, `:1624-1631`, `:1733-1737`). It stays so until a post succeeds or a
  touch reads the pair room and writes what it owes. The hub publishes no time of its own to read
  otherwise.
- **Abandoning a room** waits up to 10 s for the killed child of a request from it to end, so that the
  leave comes after the kill. When the pair ends while `run` runs, every room it still reads is abandoned
  so, including when another command forgot the pair. A room another command marked abandoned is
  stopped in `run` as soon as one of its threads reads the mark, without the leave, which is that
  command's.
- **Draining** drops the pages queued, lets the running operation finish, and gives the reply thread 30 s;
  what it has not posted by then is left to the next start.
- **A thread of `run` that meets an error it does not expect stops `run`,** which exits 1.

**Output.** Text only. The state words (`accepted`, `waiting`, `sas_ready`, `sas_pending`, `confirmed`,
`leaked`, `expired`) are the ones a machine-readable form, not built here, would use. `sas_ready` is a SAS
that has reached the host and that no command has shown; `sas_pending`, one shown and not yet confirmed.

## Not decided yet

Each of these is open. The questions this list held about work rooms (how long a room lives, who leaves
it and when, what the accept policy checks, how often the host touches the pair, what a heal means for
the rooms the host holds, whether a room may admit other agents, and where the share store lives) are
answered in [spec/PROTOCOL.md](../spec/PROTOCOL.md) §6 to §9 and §18; how the host keeps its record of
the offers it has not decided is under "Choices made in building pairing and the read loop".
- The points the spec leaves open in v1 (spec/PROTOCOL.md §19): detecting a leak of a room's key alone,
  re-finding an offer a crash hid, and how a receiver treats a body with a duplicate key.
- How a release carries exact dependency pins to a user's install. The wheel declares
  `agent-switchboard==2.6.*` and `keyring>=25`, and `pipx` and `uv tool` resolve those on the day they
  run, without reading the lock.
