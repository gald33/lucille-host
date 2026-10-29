# lucille.exec/1

The protocol between Lucille's backend and lucille-host: how Lucille asks a paired computer to run one
operation from the host's closed catalog, and how the computer answers.

**Version 1.0, draft.** No released host implements it yet. This document, the JSON Schemas and the golden
fixtures are the spec together. Changes are recorded in [PROTOCOL-CHANGELOG.md](PROTOCOL-CHANGELOG.md).

## Contents

1. [Conventions](#1-conventions)
2. [Files](#2-files)
3. [Parties and scope](#3-parties-and-scope)
4. [What Switchboard provides](#4-what-switchboard-provides)
5. [Rooms, channel and message form](#5-rooms-channel-and-message-form)
6. [Offers and the host's accept policy](#6-offers-and-the-hosts-accept-policy)
7. [Staying reachable](#7-staying-reachable)
8. [Agent ids](#8-agent-ids)
9. [Room lifetime, replacement and pair events](#9-room-lifetime-replacement-and-pair-events)
10. [Requests](#10-requests)
11. [Replies](#11-replies)
12. [Which refusal wins](#12-which-refusal-wins)
13. [Strings, numbers, sizes and output](#13-strings-numbers-sizes-and-output)
14. [Time: not_after, TTLs and late replies](#14-time-not_after-ttls-and-late-replies)
15. [Dedupe, restarts and transport failures](#15-dedupe-restarts-and-transport-failures)
16. [Local controls](#16-local-controls)
17. [What a reply may do on Lucille's side](#17-what-a-reply-may-do-on-lucilles-side)
18. [Keys and what they open](#18-keys-and-what-they-open)
19. [Not specified in v1](#19-not-specified-in-v1)
20. [Fixtures](#20-fixtures)
21. [Versioning](#21-versioning)

## 1. Conventions

- **The host** is lucille-host, running on one computer. **Lucille** is Lucille's backend, the other side.
- **Protocol rules** say what each side sends, accepts and does with a message. They bind both sides and
  are not marked. This repository can test only the host's half of each.
- **Lucille policy** is what Lucille does inside its own system, beyond the wire: how long it waits, what a
  job row holds, what its pages show. Each such statement is marked *(Lucille policy as of 2026-10-28; not
  proven by this repository)*. Nothing in this repository can test it.
- **"Not specified in v1"** marks a point this version leaves open, with what an implementation does
  meanwhile. Each one is listed again in [§19](#19-not-specified-in-v1).
- **"sb"** cites agent-switchboard 2.6.0 (github.com/gald33/switchboard, tag `v2.6.0`). sb `sharing.py:30-33`
  means `src/switchboard/sharing.py`, lines 30 to 33, at that tag. A path under `docs/` is that file at the
  same tag.
- **Sizes**: 1 KiB is 1024 bytes. **Times** are whole seconds since the Unix epoch, UTC, unless a field says
  otherwise. **"Now"** is the reading of the clock of the side that acts.

## 2. Files

| File | What it is |
|---|---|
| `spec/PROTOCOL.md` | This document. |
| `spec/PROTOCOL-CHANGELOG.md` | Every change to the protocol, and the versioning rule. |
| `src/lucille_host/spec/v1/request.schema.json` | The request, one closed branch per operation. |
| `src/lucille_host/spec/v1/reply.schema.json` | The reply, one closed branch per status. |
| `src/lucille_host/spec/v1/host.describe.schema.json` | The document `host.describe` returns in its reply's `stdout`. |
| `src/lucille_host/spec/v1/fixtures/` | The golden fixtures ([§20](#20-fixtures)). |
| `src/lucille_host/spec/__init__.py` | `lucille_host.spec`: finds and reads the files above, from wherever the package is installed. |

- The schemas and fixtures ship in the wheel as package data, at `lucille_host/spec/v1/`. The tests load
  them from an installed wheel (`tests/spec/test_packaged.py`).
- The schemas are JSON Schema draft 2020-12. Each schema's `$id` is
  `https://github.com/gald33/lucille-host/blob/main/src/lucille_host/spec/v1/<file>`. It names this
  repository, the spec's path and the major version. It identifies the schema; nothing fetches it.
- Every object in every schema sets `additionalProperties: false`, and `tests/spec/test_schemas.py` fails on
  any that does not.
- **Patterns** are ECMA-262 regular expressions, as JSON Schema says. Each one is anchored with `^` and one
  final `$`, which mean the start and the end of the string, and uses `[0-9]` rather than `\d`. Python's
  `re` reads `$` as "the end, or just before a final newline": a Python implementation matches with
  `re.fullmatch`, or ends the pattern with `\Z`.
- One spec, one home. Lucille keeps no copy of these files; it keeps its own message models and holds them
  to the fixtures, and a test on Lucille's side fails if one of these `$id`s appears in Lucille's tree
  *(Lucille policy as of 2026-10-28; not proven by this repository)*.

## 3. Parties and scope

- `lucille.exec/1` runs between Lucille and one lucille-host that Lucille paired with, for one computer.
- The host runs only operations from the catalog built into its release. Nothing in this protocol can add
  an operation, add a folder, change a limit or unpause a host: no field maps onto the host's local policy.
- Lucille uses this protocol only with lucille-host. It is not an interface Lucille offers to other node
  software *(Lucille policy as of 2026-10-28; not proven by this repository)*.

## 4. What Switchboard provides

Both sides use agent-switchboard 2.6. These are facts about that library and its hub that the rules below
rest on. They are not rules of this protocol.

- **What a message is.** A post carries a workspace, a channel, an agent id, a body, a type, a thread and a
  TTL. The client seals only the body and blinds the channel and the agent id. The type, thread and TTL
  travel and are stored in the clear (sb `client.py:386-408`, `:738`, `:1666-1673`; `server.py:133-140`).
- **Message TTLs.** A post with no TTL lives 3600 s. The hub's ceiling is 86400 s, and it clamps a larger
  TTL silently rather than refusing it. A TTL of 0 or less is refused. Expiry is computed on the hub's clock
  when the message is posted, and a read returns only messages that have not expired (sb `config.py:59-73`,
  `:1023-1031`; `server.py:140`; `store.py:699-718`).
- **Writes.** Every room this protocol uses is write-protected. A write must be signed with the room's
  write key within ±300 s of the hub's clock, and is refused 403 otherwise. The write key proves that the
  poster is a writer, never which one: every writer holds the same key (sb `writekey.py:38-42`,
  `:193-224`; `server.py:381-411`).
- **No hub size limit.** The 2.6.0 hub checks no message size (sb `server.py:706-724`). Every size bound
  below is this protocol's own, enforced by the two sides.
- **Padding.** The sealed body is padded before it is sent, so the hub sees a size bucket, not the exact
  size (sb `crypto.py:120-142`, `:604-612`).
- **Agent ids on the wire.** A client's local agent id never reaches the hub. The hub, and every reader,
  see a hub-form id in a message's `from` field, derived from the local id with the room's key, so one local
  id gives a different `from` in each room. Any holder of a room's key turns a local id into that room's
  hub-form id with `Client.peer_id(<local id>)`; a client's own is `Client.agent_id` (sb `crypto.py:386-402`;
  `client.py:715-738`, `:950-961`).
- **Reading.** A read that passes `since` is a peek that moves no cursor. Hub sequence numbers are one
  counter across every room on the hub (sb `store.py:76-77`, `:725-800`).

## 5. Rooms, channel and message form

**Three rooms, and the one this protocol uses.** Pairing derives three rooms from secrets both sides hold
(sb `docs/pairing.md:46-79`):
- the handshake room, used once;
- the **pair room**, whose sealed records keep the pair in step, offers of rooms among them;
- the room Switchboard 2.6 calls the pair's work room, **`pair:<name>`**.

This spec calls a **work room** a room Lucille mints with `sharing.share_new` and offers over a computer's
pair. In this spec, "work room" never means `pair:<name>`.

**Where requests and replies travel.**
- Every request and every reply travels on the channel **`lucille.exec`**, and nothing else travels there.
- Requests and replies travel only in a work room that Lucille minted and offered over that one computer's
  pair and no other. Never in `pair:<name>`, and never in any other room.
- Lucille may post a request in such a room before the host has accepted it
  ([§6](#6-offers-and-the-hosts-accept-policy)).
- The host acts only on a request in a room it accepted, that passed checks 3 and 4
  ([§6](#6-offers-and-the-hosts-accept-policy)), and whose list of pairs names its current pair
  ([§9](#9-room-lifetime-replacement-and-pair-events), Re-pair, says how it tells). It posts its reply in the
  request's room. It ignores a request anywhere else: on the `pair` channel, in `pair:<name>` on any
  channel, in a room it did not accept, or in a room not offered over its current pair.

**`pair:<name>` carries only the library's own notes.**
- Neither side's own code posts anything in `pair:<name>`. The library's own notes are allowed: `swbp1/paired`,
  `swbp1/moved`, `swbp1/ended` and the share wake `swbp1/shared` (sb `pairing.py:202-210`; `sharing.py:914-956`,
  `:1166-1186`). Either side's library posts the share wake, on `share_new`, on `accept`, on `leave` and on its
  daily refresh of its records, from a throwaway agent id, and its body is only the tag. A test that checks
  that nothing lands in `pair:<name>` excludes these notes by name.
- The host reads `pair:<name>` on its `pair` channel only, and reads the notes there as wakes, never as
  instructions: `swbp1/paired`, `swbp1/moved` and `swbp1/ended` only make it resolve the pair, and
  `swbp1/shared` only makes it read the pair's offers (a touch).

**Message form.**
- Every protocol message is a Switchboard message of type `note`, with no thread. Its body is one JSON
  object: a request ([§10](#10-requests)) or a reply ([§11](#11-replies)). Everything the protocol means is
  inside the sealed body; the cleartext type, thread and TTL carry nothing of it.
- Neither side calls `register()` in a computer's rooms, and the host never calls it at all.
- A message on `lucille.exec` whose type is not `note`, or that carries a thread, is logged locally by the
  side that reads it and otherwise ignored.

## 6. Offers and the host's accept policy

**The offer.**
- Lucille mints each work room with `sharing.share_new`, and offers it over that computer's pair only.
- Lucille offers nothing before its own SAS confirmation for that pair. `share_new` checks only the pair's
  `confirmed` flag, which is set before anyone compares the SAS (sb `docs/sharing.md:517-524`).
- An offer is a sealed record in the pair room (sb `sharing.py:35-48`). The host reads offers with
  `sharing.touch`, woken by the best-effort `swbp1/shared` note, and keeps one with `sharing.accept`, by the
  policy below. agent-switchboard 2.6.0 ships no accept policy (sb `docs/sharing.md:594-595`).

**Offers flow one way.**
- The host invites nobody, never calls `sharing.share_new` or `register()`, and offers no room of its own.
- Only Lucille offers rooms over a computer's pair, and Lucille never calls `sharing.accept` on one.
- The host's accept writes the same room's offer back over the same pair; that is how 2.6.0 records an
  acceptance (sb `sharing.py:1546-1550`). Lucille reads that record as "accepted", never as an offer to
  accept.

**The host's accept policy.** The host accepts a room only if all five hold:
1. the offer came over its Lucille pair;
2. its own local SAS check for that pair passed ([§16](#16-local-controls));
3. the room's hub is the hub its pairing code named (compared as URLs, after removing one trailing `/`);
4. the offer carries no hub token;
5. it holds fewer rooms than the cap below.

Anything else is refused and logged locally. What a refusal does depends on when its check runs.

**When each check runs.**
- Checks 1, 2 and 5 run before `sharing.accept`.
- Checks 3 and 4 run right after it, before any read or post in the room, against
  `sharing.resolve_room(<name>).config`: its `url` must equal the hub the pairing code named, and its
  `token_source` must not be `"shared"`. If either fails, the host calls `sharing.leave` on the room at once,
  logs the refusal locally, and never reads or posts in that room.
- Why: through the sharing calls the host uses (`touch`, `accept`, `resolve_room`, `leave`), a pending offer
  shows no hub URL and no token. 2.6.0 stores a pending offer without them (sb `sharing.py:853-855`), and
  `accept` itself re-reads the live offer and writes the offer-back above (`:1546-1550`, `:1612-1615`). So
  "accept only if" can hold only as: accept, check, and leave at once on a failure. Leaving is final for that
  room over that pair (sb `sharing.py:39-40`, `:824-835`), so only a new room can reach the host again, and
  in v1 Lucille offers one only when it replaces the work room
  ([§9](#9-room-lifetime-replacement-and-pair-events)).
- **Checks 3 and 4 hold for every held room, at every use**, not only right after the accept. `accept`
  stores the room before it returns, and can still raise after that (its second touch, sb
  `sharing.py:1612-1625`), and the host can stop between the accept and the checks. So the host runs checks 3
  and 4 again for each room it holds at start, and before it reads or posts in a room, and leaves any room
  that fails them, as above. They make no hub call: `resolve_room` uses only the host's own share store,
  which it reads and stamps with the time of use (sb `sharing.py:1842-1873`).

**A refusal before the accept is not final.** A failure of check 1, 2 or 5 refuses the offer without leaving
it: the offer stays pending, and the host considers every offer pending over its pair again at start, when
its local SAS gate opens, whenever it leaves a room, and whenever a touch reports a pending offer `left` or
`withdrawn` (below). Only a failure of check 3 or 4, after the accept, ends in a leave, and only that is
final for the room. 2.6.0 reports a pending offer to the host once, as an
`offered` note the first time a touch sees it (sb `sharing.py:842-846`), so the host keeps its own record of
the offers it was told of and has not decided; and it touches nothing before its SAS gate opens
([§7](#7-staying-reachable)), so that the first touch after the gate opens reports each offer waiting as new.
An offer whose note a crash hid is not specified in v1 ([§19](#19-not-specified-in-v1)).

**The cap (check 5).** The host holds at most 2 rooms whose list names its current pair (the current room,
plus its replacement while one is handed over), and at most 2 held rooms in all. A host at the cap accepts
nothing more until it has left a room.

**A newer room.** A room the host accepts over its current pair, and that then passes checks 3 and 4, is
the newer room: it replaces every other room the host holds over that pair
([§9](#9-room-lifetime-replacement-and-pair-events)). The host runs checks 3 and 4 on the new room before it
abandons anything. A room that fails them replaces nothing, and the host keeps its current room. After each
accept that passes, the host abandons the rooms the new one replaces before it considers the next offer.
Before it calls `sharing.accept`, the host records in its local log that the offered room, once it passes
checks 3 and 4, replaces the rooms it then holds over that pair; at start it completes any such replacement
whose room it holds and that passes them, so a stop between the accept and the abandon leaves no doubt
which room is newer.

**Several offers at once.** While more than one offer over its current pair is pending, the host accepts
none of them, and logs that locally. It cannot tell which is newer: 2.6.0 reports each pending offer as a
note stamped with the host's own time of reading (sb `sharing.py:389-393`, `:843-844`), and a room's name is
text the host never acts on (below). Lucille's leave of a room withdraws that room's pending offer, and a
touch then reports the offer as `left` or `withdrawn` (sb `sharing.py:759-763`, `:778-794`). So the wait ends
when Lucille leaves the rooms it no longer uses ([§9](#9-room-lifetime-replacement-and-pair-events)), and the
host then considers the one offer left. Before it judges how many are pending, the host drops from its own
record every offer whose room `sharing.resolve_room` reports with state `unknown`, which 2.6.0 does only when
its store holds no pending offer, held room or left room of that name (sb `sharing.py:1858-1868`). So a stop
that hid a `left` or `withdrawn` note does not leave a withdrawn offer blocking every accept.

**Offer text is untrusted.** A room's offered name and note are text the other side chose. The host shows
them only after sanitizing them, and never acts on them. 2.6.0 replaces control characters (C0 and C1) and
surrogates in a note with spaces and cuts it to 200 characters, and restricts room names to
`[a-z0-9][a-z0-9-]{0,31}` (sb `sharing.py:138`, `:210-214`, `:620`, `:636`; `pairing.py:197`). It leaves
other characters, such as bidirectional controls, as they are, so the host's own sanitizing still applies.

**No re-accept.** The host never accepts again a room it has left. It leaves a room only when the room failed
this policy, was replaced, or its pair is over, and a room that may have leaked never carries another job.

**Posting before the accept.** Lucille may post a request in a room as soon as it has offered the room, and
need not wait to see the host's accept. The host reads a newly accepted room from sequence number 0, so a
request posted before the accept still runs if it is still inside its `not_after` when the host decides it.

## 7. Staying reachable

- **The host is responsible for being reachable whenever it runs.** Lucille holds no long-poll on the
  computer's rooms *(Lucille policy as of 2026-10-28; not proven by this repository)*.
- **Listening.** While it runs, the host keeps one long-poll for each held work room on `lucille.exec`, and
  one on the `pair` channel of `pair:<name>`, running at the same time, so that neither waits on the other.
  It long-polls only rooms that passed checks 3 and 4 ([§6](#6-offers-and-the-hosts-accept-policy)) and
  whose list names its current pair.
- **Touching** (reading the pair's offers with `sharing.touch`, naming its pair). Once its local SAS gate for
  the pair is open ([§16](#16-local-controls)), the host touches when the gate opens, at start, on every
  `swbp1/shared` note, whenever it resolves the pair, on waking from sleep, and every 5 minutes, whether or
  not it holds a room. Before the gate opens it touches nothing; offers wait in the pair room, and the first
  touch after the gate opens reports each one as new ([§6](#6-offers-and-the-hosts-accept-policy)).
  - Why every 5 minutes, with a room or without: the share wake is best effort and never retried (sb
    `sharing.py:1166-1186`), and the library's `swbp1/moved` note is best effort too (sb
    `pairing.py:2738-2747`). After a heal, the wake for a replacement room goes to the new generation of
    `pair:<name>`, which the host is not yet reading. Without the 5-minute touch, the host could miss a
    replacement until its next 24-hour resolve, longer than any job waits
    ([§14](#14-time-not_after-ttls-and-late-replies)).
  - A touch resolves the pair itself (sb `sharing.py:1082`), so a touch every 5 minutes also finds a move or
    the end of the pair within 5 minutes while the host runs. It costs a few hub requests. That resolve is
    part of the touch and calls for no touch of its own: "whenever it resolves the pair" above, and "a touch
    follows every resolve" below, mean the host's own `pairing.resolve` calls, other than one made because
    a touch failed: after that one, the next touch comes on the rhythm above, so that a hub error that lasts
    does not keep touches and resolves calling each other.
- **Resolving** (`pairing.resolve`). The host resolves at start, on every `swbp1/paired`, `swbp1/moved` or
  `swbp1/ended` note, on a 403 or another hub error, and at least every 24 hours. That keeps its side record
  in the pair room, which lives 7 days, fresh. Once the host's SAS gate is open, a touch follows every
  resolve the host makes, which also keeps fresh the records the library writes back for each accepted room
  (it rewrites them at most daily, sb `sharing.py:110-114`).

## 8. Agent ids

- **The host's local agent id is the literal `lucille-host`**, the same on every computer and in every room.
  The hub sees agent ids only in their hub form, derived with each room's key ([§4](#4-what-switchboard-provides)),
  so one shared value links nothing across rooms or computers. A fixed literal is something both sides and
  the fixtures can name.
- **Lucille's local agent id may differ per Lucille instance.** The host never pins it and never checks it.
  It is never `lucille-host`: under that id Lucille's posts would carry the host's hub-form id, and the host
  would ignore them.
- **Comparing ids.** A message's `from` is a hub-form id. A side compares it with its own `Client.agent_id`,
  or with `Client.peer_id("lucille-host")` for the room the message is in.
- **The host's rule.** The host ignores every post under its own id. It treats any body with
  `dir: "request"` in a room it accepted, not under its own id, as Lucille's. That rests on a rule both sides
  are bound by: Lucille never shares a computer's work room with any other agent, and the host invites
  nobody ([§6](#6-offers-and-the-hosts-accept-policy)), so the room's holders are Lucille and that one
  computer. This repository can test only the host's half. The rule does not rest on the keys excluding
  anyone else, which they do not ([§18](#18-keys-and-what-they-open)).
- **Lucille's rule.** Lucille applies a reply only if it is under the host's id, and never applies its own
  post as a reply. It reads its own requests only to adopt one that a crash left unrecorded *(Lucille
  policy as of 2026-10-28; not proven by this repository)*.
- **Agent ids are not authentication.** Anyone who holds a room's keys can post under any id in it:
  Lucille, the host, and whoever holds the pair secret, which opens every room offered over the pair (sb
  `sharing.py:27-33`). The host never calls `register()`, so it publishes no signing key, and it uses a fresh
  signing identity for each client it builds; to Lucille, its signatures read as unknown (sb
  `client.py:1054-1097`). The id rules keep each side's own posts apart. They authenticate nothing, and no
  rule of this protocol rests on a signature.

## 9. Room lifetime, replacement and pair events

**One standing work room per computer.** Each computer has one work room at a time. It is replaced only
when the pair is re-paired or when a key leak is suspected, never on a schedule. A room the host leaves on
its own is not replaced (below).

**A suspected leak.** In v1, each of these counts as a suspected leak of the work room's keys: a heal of
`pair:<name>`, and a lapse of `pair:<name>` (below). Each replaces the work room.

**When a room counts as replaced, on the host.** Any one of these:
- the host accepts a newer room over the same pair, one that has passed checks 3 and 4
  ([§6](#6-offers-and-the-hosts-accept-policy), A newer room);
- a touch shows Lucille's `left` record for the room;
- `pair:<name>` moves (a heal or a lapse);
- the room's list no longer names the host's current pair (the pair was paired again);
- the pair ends, or the user forgets it.

**What the host does with a replaced room ("abandon").** It first records in its local log that it abandons
the room. Then it starts no request from that room. If it is running one, it kills that process group and
posts no reply. It stops reading the room, and calls `sharing.leave` on it. After a restart, the host posts
nothing for a request in a room it abandoned or left, `interrupted` included
([§15](#15-dedupe-restarts-and-transport-failures)). Why: no reply in a replaced room is applied
([section 17](#17-what-a-reply-may-do-on-lucilles-side)); output posted into a room suspected leaked would go
to whoever leaked it; and leaving removes the room's keys from the host's share store.

**What Lucille does with a replaced room.**
- It posts nothing more there. Jobs it had queued but not yet posted go to the replacement room. It never
  knowingly posts a job again, in any room (how Lucille adopts a request after a crash is in
  [§8](#8-agent-ids)), and the host's dedupe covers a re-post it misses
  ([§15](#15-dedupe-restarts-and-transport-failures)).
- No reply in a replaced room is applied ([section 17](#17-what-a-reply-may-do-on-lucilles-side)). Lucille
  drops and tallies any reply that arrives there *(Lucille policy as of 2026-10-28; not proven by this
  repository)*.

**Who leaves, and when.**
- Lucille leaves a room it no longer uses (a replaced room, or a room offered over a pair that is over) once
  every job it posted there is past its `not_after` + 900 s, after which no reply there can still be read
  in time ([§14](#14-time-not_after-ttls-and-late-replies)). Until then it keeps reading the room, so that a
  reply arriving there is seen and tallied, and it applies nothing from it. The replacement, or the end of
  the pair, starts that wait; a reply never triggers a leave.
- If the pair is gone by then, Lucille's leave tells nobody, and still removes the room and its keys from
  Lucille's share store, which `leave` does first (sb `sharing.py:1704`).
- The host leaves a room at once when it abandons it, as above, or when the room fails check 3 or 4 (below).
- Outside these, neither side leaves the current room.
- Leaving revokes nothing in Switchboard: every other holder still holds the room (sb `sharing.py:16-23`).
  It stops the library's daily re-offer and drops the room's keys from the store of the side that leaves.

**A room the host leaves on its own.** With no pair event and no newer room to explain it, the host leaves
Lucille's current room only when the room failed check 3 or 4 ([§6](#6-offers-and-the-hosts-accept-policy)).
Such a room is not replaced, and it carries nothing more: the host never reads it again
([§6](#6-offers-and-the-hosts-accept-policy), no re-accept), and Lucille, which sees the host's `left`
record on a touch, posts nothing more there. The computer then runs no jobs until Lucille offers it a new
room, which in v1 happens only on a re-pair or a suspected leak (above): in practice, until the user pairs
the computer again. What the user sees:
- On the computer, the host's local log names the check that failed, and `lucille-host status` shows that
  the host holds no work room.
- In Lucille, each of the computer's unfinished jobs fails when its wait ends: as not posted if Lucille had
  not posted it, and as no reply if it was posted before Lucille saw the `left` record
  ([§14](#14-time-not_after-ttls-and-late-replies)) *(Lucille policy as of 2026-10-28; not proven by this
  repository)*.

**Re-pair.** Pairing the computer again makes a new pair. The library takes the old pair off every room's
list (sb `sharing.py:449-457`, `:499-508`), and the host acts only in rooms whose list names its current pair.
Lucille offers a new room over the new pair, and the old room is replaced, as above.
- **How the host tells.** A pair made again under the same name keeps that name, and `resolve_room` lists
  a room's pairs by name only (sb `sharing.py:1871-1873`). 2.6.0 takes the old pair's entries off every
  list on a touch of the new pair, which compares each entry's transcript with the pair record (sb
  `sharing.py:499-508`, `:1074-1075`). So the host judges whether a room's list names its current pair only
  after a touch of that pair has run since the pair was made, never by the name alone. Before its SAS gate
  for the pair opens, the host acts in no room ([§16](#16-local-controls)), and it touches when the gate
  opens and at every start ([§7](#7-staying-reachable)), so the touch comes first.

**Heal.** A heal moves `pair:<name>` to a new generation, and does not move a room offered over the pair (sb
`sharing.py:27-33`; `docs/sharing.md:526-528`).
- The host follows it for the library's notes, reading the new `pair:<name>` from sequence number 0.
- A heal counts as a suspected leak: Lucille replaces the work room, and the host abandons the old one.
  Lucille fails the jobs in flight in the old room *(Lucille policy as of 2026-10-28; not proven by this
  repository)*.
- In v1 neither side's code starts a heal; the host only follows one. So a heal in v1 came from whoever
  holds the pair secret, or from a later Lucille release, and either way it is treated as a suspected leak.

**Lapse.** 2.6.0 also moves `pair:<name>` with no heal: a confirmed side that has not read the pair room for
6 days, and finds the other side's record gone, moves to a fresh generation, and the other side follows (sb
`pairing.py:2357-2365`; `docs/pairing.md:657-660`). No key need have leaked. But the side that lapsed cannot
tell whether a heal was made while it was away and lost with the expired record, and the library says so
(sb `pairing.py:2360-2364`): a lapse may hide a heal, and a heal counts as a suspected leak. So in v1 a lapse
is handled exactly as a heal: Lucille replaces the work room, and the host abandons the old one.

**The pair ends, by Lucille.** The host learns it on a resolve (`ended_by_peer`). Before it stops, it
abandons and leaves every room it held over the ended pair; then it calls `pairing.forget` to delete its
own copy of the pair, says so, and stops. When Lucille ends the pair because the computer was removed, or
its owner rejected the SAS, Lucille fails the computer's unfinished jobs as pair-ended *(Lucille policy as
of 2026-10-28; not proven by this repository)*. A re-pair also ends the old pair on Lucille's side; Re-pair,
above, covers what that does to the old room. If Lucille's forget did not reach the
hub, the host is not told, and keeps its half until the user runs `lucille-host forget`; Lucille's page for
the computer says that the computer was not told *(Lucille policy as of 2026-10-28; not proven by this
repository)*.

**Rooms at the end of a pair.** 2.6.0 keeps a room, and its keys, held after its pair is forgotten or ended;
only the room's list changes (sb `sharing.py:449-508`), and `pairing.forget` does not touch the share store.
So the host leaves every room it held over a pair before that pair is gone from its store, so that its share
store keeps no key of a pair that is over. This also applies to `lucille-host forget`.

**`lucille-host forget`.** It abandons and leaves the pair's rooms, then calls `pairing.forget`, which deletes
the local record first and then tells Lucille, best effort. The command prints whether Lucille was told. If
Lucille was not told, Lucille keeps its half until the computer is removed there *(Lucille policy as of
2026-10-28; not proven by this repository)*.

## 10. Requests

A request goes from Lucille to the host. Its body is exactly:

`{v, dir: "request", job_id, op, args, not_after, caps?, min_host_version?}`

| Field | Rule |
|---|---|
| `v` | Exactly `"lucille.exec/1"`. |
| `dir` | Exactly `"request"`. |
| `job_id` | Lucille's id for the job: a canonical lowercase UUID string, 8-4-4-4-12 hex digits. The schema checks that form, not the version bits. |
| `op` | The operation: a string of at most 64 characters matching `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`. |
| `args` | A JSON object: the operation's arguments, held to that operation's own closed schema (below). |
| `not_after` | When Lucille stops waiting for the job: an integer, whole seconds since the Unix epoch, UTC, from 0 to 4294967295. |
| `caps` | Optional. The object `{stdout_bytes, stderr_bytes}`, both keys required, each an integer from 2 to 2147483647: a cap on each output stream, measured as in [§13](#13-strings-numbers-sizes-and-output). The empty stream measures 2 bytes, so a cap below 2 could not be met. |
| `min_host_version` | Optional. The oldest host release that may run this request: `MAJOR.MINOR.PATCH`, digits only, no suffix, each part at most 9 digits with no leading zeros. |

**Envelope rules.**
- Any other key in the envelope, or in `caps`, is refused `bad_request` and never interpreted. That includes
  keys such as `argv`, `exe`, `env`, `cwd` and `timeout`, and any key shaped like policy.
- A missing required key, or a value of the wrong type or out of range, is `bad_request`. That includes
  `args` that is not a JSON object.
- `op`: a well-formed op that is not in the host release's catalog is refused `unknown_op`. One that is in
  the catalog but not enabled by the host's local policy is refused `not_enabled`. A malformed op is refused
  `bad_request`.
- `not_after`: the host refuses `bad_request` any request whose `not_after` is more than 86400 s after the
  moment it received the request (rule **not-after-range**). No hub message outlives 86400 s, and the bound
  keeps the host's dedupe state from holding an entry indefinitely. Lucille sends, as `not_after`, the floor
  of its own deadline for the job, written as a JSON integer. Lucille caps a job's wait at 85500 s
  ([§14](#14-time-not_after-ttls-and-late-replies)) *(Lucille policy as of 2026-10-28; not proven by this
  repository)*, which leaves 900 s for the difference between Lucille's clock and the host's. A side whose
  clock is more than 300 s from the hub's cannot write at all ([§4](#4-what-switchboard-provides)).
- A **valid `not_after`** is one these rules accept: a JSON integer from 0 to 4294967295, at most 86400 s
  after the moment the host received the request. One in the past is valid, and draws `expired`
  ([§14](#14-time-not_after-ttls-and-late-replies)). A request whose `not_after` is missing, not a JSON
  integer (`1.0` included, rule `integers` in [§13](#13-strings-numbers-sizes-and-output)), or out of range
  has no valid `not_after`; [§14](#14-time-not_after-ttls-and-late-replies) and
  [§15](#15-dedupe-restarts-and-transport-failures) say what the host uses instead.
- `caps`: for each stream of a child's output the host applies the lower of the request's cap and its own,
  and never refuses because a request cap exceeds its own. `host.describe` runs no child, and its document
  is not capped by a request's `caps` ([§11](#11-replies)). Lucille sends no `caps` in v1 *(Lucille policy as
  of 2026-10-28; not proven by this repository)*.
- `min_host_version`: a host older than it refuses `host_too_old`. The host compares its own release's
  version as a tuple of three numbers: the numeric triple its version starts with. A host version with
  anything after that triple (a pre-release, dev, post-release or local suffix) counts as older than the
  same triple, and one that does not start with a numeric triple counts as older than every
  `min_host_version`. Erring older is the safe direction for a cut-off meant to stop a bad release. A
  well-formed `min_host_version` is checked before the rest of the envelope
  ([§12](#12-which-refusal-wins)), so that an older host refuses `host_too_old` even a request that uses a
  field added after its release. A `min_host_version` that is not in the form above is `bad_request`.

**Operations and their arguments.** Each operation's `args` schema is part of this spec, and so binds Lucille
as well as the host. Each is a closed object (`additionalProperties: false`), and the request schema picks it
by `op`. The host's catalog entry for each operation must take exactly the parameter schema stated here;
`tests/test_catalog.py` (`test_catalog_params_equal_spec_args`) asserts it. Adding an operation is a minor
change ([§21](#21-versioning)).

| `op` | `args` | Notes |
|---|---|---|
| `host.describe` | `{}` | No arguments. Runs no process. Its reply is in [§11](#11-replies). |
| `host.sleep` | `{n}`: an integer from 1 to 240 | Sleeps `n` seconds. Diagnostic. Its catalog timeout is `n` + 30 s. |
| `files.sha256` | `{root, path}` | The SHA-256 digest of one file. `root` is a folder alias; `path` is a string of 1 to 1024 characters with no U+0000. |
| `files.search` | `{root, pattern, fixed_strings, max_count}` | Searches the files under `root`. `pattern` is a string of 1 to 256 characters with no U+0000; `fixed_strings` is a boolean; `max_count` is an integer from 1 to 200, the most matches per file. All four are required. Its reply carries lines of the files it searched. |

- A **folder alias** (`root`) is a name the host's local policy maps to a folder: 1 to 32 characters matching
  `^[a-z][a-z0-9_-]{0,31}$`. A request never carries a folder's path.
- An argument that breaks its operation's schema (an extra key, a missing key, a wrong type, a value out of
  range, a malformed alias, a path too long or empty) is refused `bad_args`.
- **Paths and roots.** The schema checks a path's type, length and characters. Containment is the host's
  check, decided before anything runs, and every escape gets the one code `path_outside_root`: an absolute
  path, a path with a `..` segment, a path whose realpath is outside the root, a path into the host's own
  configuration or state folders, a path through Linux's `/proc`, a path naming something other than a
  file or a folder (a FIFO, a socket or a device), and a `root` alias the host's local policy does not hold. A root that is `$HOME`, holds it,
  or is `/` needs an explicit local flag; a host may not recognise every way a folder can hold `$HOME`
  (through another filesystem's view of it, say).

**Messages the host does not answer.** A reply must name a `job_id`, so the host posts nothing for a body that
is not a JSON object, has no `dir: "request"`, or has no well-formed `job_id`. It logs such a message
locally. It ignores every post under its own id ([§8](#8-agent-ids)).

## 11. Replies

A reply goes from the host to Lucille, in the request's work room. Its body is exactly:

`{v, dir: "reply", job_id, status, code?, exit_code?, stdout?, stderr?, stdout_truncated?, stderr_truncated?, duration_ms?, policy_digest, host_version}`

The host posts one reply per `job_id`, with two exceptions, both in
[§15](#15-dedupe-restarts-and-transport-failures), where it may post more than one reply for the same job:
- a restart between posting a reply and recording that it was sent: the host then posts `failed` with
  `interrupted` for a decision to run, which can follow a different reply it had already posted (a `done`,
  say), or the same refusal again;
- a retried post whose earlier attempt reached the hub although the host saw it fail, which can leave one or
  more copies of the same reply.

The first counting reply for a job decides it, and a later one is never applied
([section 17](#17-what-a-reply-may-do-on-lucilles-side)).

**Status.**
- `done`: the operation ran. A non-zero exit is data, not a failure.
- `refused`: it never started. `code` is one of `unknown_op`, `not_enabled`, `bad_request`, `bad_args`,
  `path_outside_root`, `expired`, `paused`, `host_too_old`.
- `failed`: the host decided to run it, and it did not finish. `code` is one of `timeout` (it ran past its
  time limit), `killed_locally` (the user killed it) and `interrupted` (the host did not carry out its
  decision: a restart came between the decision and the reply, [§15](#15-dedupe-restarts-and-transport-failures),
  the user paused the host between the decision and the start, or the process it decided to run could
  not be started: its executable or its folder went away in between, say, or starting it failed).

These lists are closed. A receiver refuses a reply with any other status or code.

**Three shapes.** The reply schema is a `oneOf` of three closed objects. Every shape carries `v`, `dir`,
`job_id`, `status`, `policy_digest` and `host_version`.

| Status | Also required | Also allowed | Never present |
|---|---|---|---|
| `done` | `exit_code`, `stdout`, `stderr`, `stdout_truncated`, `stderr_truncated`, `duration_ms` | | `code` |
| `refused` | `code` | | `exit_code`, `stdout`, `stderr`, either truncation flag, `duration_ms` |
| `failed` | `code`, `duration_ms` | `exit_code`; `stdout` with `stdout_truncated`; `stderr` with `stderr_truncated` (each stream with its flag, or neither) | |

**Fields.**
- `policy_digest`: 64 lowercase hex digits, the HMAC-SHA256 of the host's canonical local policy under a
  random key that never leaves the computer. The user can match a result to a policy, and Lucille can tell
  when the policy changed. A plain hash would let anyone holding it confirm a guessed folder path.
- `host_version`: the host release's version string, at most 32 characters.
- `exit_code`: the integer the OS reported, from -255 to 255; negative when the child was killed by that
  signal. A child that died of a signal the host did not send is still `done`.
- `duration_ms`: an integer from 0 to 2147483647, the milliseconds from when the host started the operation
  to when it finished or was stopped, by the host's own clock. For `interrupted`, it is what the host's own
  records show, or 0.
- `stdout`, `stderr`: the child's output, decoded and capped as in [§13](#13-strings-numbers-sizes-and-output),
  or, for `host.describe`, its document (below), which is neither.
- `stdout_truncated`, `stderr_truncated`: true when capping dropped the end of that stream's output
  ([§13](#13-strings-numbers-sizes-and-output)), and false otherwise. Removing U+0000 and redaction do not set
  it. Output the host did not keep (rule `raw-ceiling`) counts as dropped by capping, and sets it.

**Error text never crosses the wire.** A refused reply carries a code and nothing else. The host's own
exception or error text never appears in any reply, under any status. (A child's own `stderr` is output, not
the host's error text.)

**`host.describe`.** It replies `done` with `exit_code` 0, an empty `stderr`, and both truncation flags
false. Its document goes, JSON-encoded, in `stdout`. The reply has no structured result field, and the
closed v1 schema allows none.
- The document is not a child's output: [§13](#13-strings-numbers-sizes-and-output)'s decoding, redaction
  and capping do not apply to it, and neither do a request's `caps`. It is never cut short, so `stdout`
  always parses. Its schema bounds it: the largest document the schema allows, written with no
  insignificant whitespace, measures under 8 KiB as a JSON-encoded `stdout` string, well under the host's
  own 64 KiB (rule `stream-size`, which still holds).
- The document is a closed object, held to `host.describe.schema.json`:

| Key | Value |
|---|---|
| `host_version` | as in the reply |
| `protocol` | `"lucille.exec/1"` |
| `os_family` | `"linux"` or `"macos"` |
| `catalog_version` | identifies the release's catalog; it changes whenever any catalog entry changes. At most 64 characters. |
| `enabled_ops` | the names of the operations the local policy enables, at most 64 |
| `root_aliases` | the folder aliases the local policy holds, at most 64; never paths |
| `policy_digest` | as in the reply |

## 12. Which refusal wins

Each reply carries one code, so the checks run in a fixed order, and the first that fails decides the reply:

1. **Dedupe.** A request whose `job_id` the host has already decided gets nothing at all
   ([§15](#15-dedupe-restarts-and-transport-failures)).
2. **`host_too_old`**: the request carries a `min_host_version` in the form [§10](#10-requests) states, and
   the host is older. This comes before the envelope check, so that an older host refuses `host_too_old` a
   request that uses a field a later minor version added ([§21](#21-versioning)), whatever else the request
   holds, rather than `bad_request`.
3. **`bad_request`**: the envelope ([§10](#10-requests)), the request's size, the strings in it, the range of
   `not_after`, and the form of `min_host_version`.
4. **`expired`**.
5. **`paused`**.
6. **`unknown_op`**.
7. **`not_enabled`**.
8. **`bad_args`**.
9. **`path_outside_root`**.

The numbered order governs. A request that passes all nine runs.

## 13. Strings, numbers, sizes and output

These are the rules a JSON Schema cannot express. Each has a name. The fixtures use the names of the rules
a message can break ([§20](#20-fixtures)); `raw-ceiling` binds only what the host keeps of a child's output.

**strings.** No string anywhere in a message, `args` included, holds U+0000 or an unpaired surrogate.
- In a request, either makes the request `bad_request`. This request-wide check runs before any check of an
  operation's own arguments.
- Every string in the schemas has a fixed value, or a pattern that cannot match U+0000: free text carries
  `^[^\u0000]*$`, and a formatted string a pattern built from ASCII classes. The unpaired-surrogate half of the
  rule is not in the schemas: a character class over U+D800 to U+DFFF means different things in ECMA-262
  (UTF-16 code units, which would refuse valid pairs) and in Python's `re` (code points). It is enforced by
  fixtures, and by a test in each implementation.

**integers.** Every number in a v1 message is an integer, written as a JSON integer: digits only, no fraction
and no exponent. JSON Schema's `integer` type also accepts a number such as `1.0`; this protocol does not. In
the envelope that is `bad_request`; in `args`, `bad_args`. A boolean is never an integer.

**How size is measured.** The size of a value is the byte length of the value serialized as JSON with every
non-ASCII character escaped (`\uXXXX`, and a surrogate pair for a character beyond the Basic Multilingual
Plane) and no insignificant whitespace, as Python's `json.dumps(value, ensure_ascii=True,
separators=(",", ":"))` writes it. Each side computes a size with exactly this serialization, never with
its own encoder's output: encoders differ (some escape `<`, `>`, `&` or `/`, some add spaces after `,` and
`:`), so their byte counts for the same value differ.

**request-size.** A request whose body measures more than 16 KiB is refused `bad_request`. The largest
arguments in v1 are a 1024-character path and a 256-character pattern, so the per-field bounds already keep
a request under this; the size check is a backstop.

**stream-size.** Each stream's JSON-encoded string, both quotes included, measures at most 64 KiB (65536
bytes). That is the host's own cap on each stream. A request's `caps` are measured the same way, and the
host applies, for each stream of a child's output, the lower of the request's cap and its own
(`host.describe`'s document: [§11](#11-replies)). The empty stream, `""`, measures
2 bytes, so a cap is at least 2 ([§10](#10-requests)), and every cap allowed can be met. 65534 characters is
the most any stream can hold; the schema states that bound, and this rule the byte bound.

**Decoding.** The host decodes each stream of a child's output as UTF-8 with `errors="replace"`, as Python's
decoder does it (each invalid sequence becomes U+FFFD; the decode fixtures pin the result), and then removes
every U+0000. So no reply string holds U+0000
or an unpaired surrogate. Lucille rejects any reply string holding either *(Lucille policy as of 2026-10-28;
not proven by this repository)*. The decode fixtures cover output that is not UTF-8 and output that holds NUL.

**Redaction.** Before anything is sent, a best-effort redaction pass runs over each stream of a child's
output: PEM blocks
(an unterminated `BEGIN ... PRIVATE KEY` block counts as running to the end of the stream) and common key
prefixes. It is a convenience, not a guarantee, and nothing may claim that secrets are removed from output.

**Order of the steps.** For each stream of a child's output: decode, remove U+0000, redact, then cap last.
Capping drops whole characters from
the end of the stream until its JSON-encoded string fits the cap, and then sets that stream's truncation
flag. Only capping sets it ([§11](#11-replies)).

**raw-ceiling.** The host keeps at most the first 4 MiB (4194304 bytes) of each stream's raw output. It reads
and drops the rest, so that a child never blocks on a full pipe and the host's memory stays bounded. It may
also stop reading a stream before its end once the operation's process group is gone and the stream stays
open for 2 s more (a process that left the group still holds it). Either way, the steps above run on what
the host kept, as if the stream had ended there, and that stream's truncation flag is set: what the host did
not keep counts as dropped by capping. So output that a later part of the stream would have reached (after
a long run of U+0000, say, or a long redacted block) is not in the reply, and the flag says the stream was
cut short.

**No paths in replies.** No reply contains the realpath of a root or the user's `$HOME`. `host.describe`
returns folder aliases, never paths.

**Reply size on Lucille's side.** Lucille bounds a reply at 256 KiB, measured as above *(Lucille policy as of
2026-10-28; not proven by this repository)*. A conforming host cannot exceed it: two streams at the cap and
the other fields measure well under 256 KiB.

## 14. Time: not_after, TTLs and late replies

**Never start late.** The host never starts a request after its `not_after` by its own clock. When the host
decides a request and its clock reads later than `not_after` (now > `not_after`), it refuses `expired`, and
nothing runs. At now ≤ `not_after` the request is not expired.

**Request TTL.** Lucille posts each request with `ttl = ceil(not_after − now)`, computed from the integer
`not_after` it sends ([§10](#10-requests)), so the hub drops the request within 1 s after its `not_after`
when Lucille's clock and the hub's agree.
Lucille does not post a request whose TTL would be 0 or less. It fails that job as not posted *(Lucille
policy as of 2026-10-28; not proven by this repository)*.

**Reply TTL.** The host posts each reply with `ttl = (not_after − now) + 900`, at most 86400 s, and always
passes it explicitly, never relying on the hub's 1-hour default. If that TTL is under 1 s, the host does not
post the reply, and logs that locally: the hub refuses a TTL of 0 or less, and Lucille drops any reply it
reads after the grace (below).
- A request with no valid `not_after` ([§10](#10-requests)) still gets a reply (`bad_request`, or
  `host_too_old` when that check comes first, [§12](#12-which-refusal-wins)). For it the host uses, in
  place of `not_after`, the time it received the request: `ttl = (received − now) + 900`, so at most 900 s,
  passed explicitly and not posted when under 1 s, as above. The same holds for a re-post after a restart
  ([§15](#15-dedupe-restarts-and-transport-failures)).

**The wait cap and the grace** *(Lucille policy as of 2026-10-28; not proven by this repository)*. Lucille
caps a job's wait at 85500 s, so that the 900-second grace always fits under the hub's 86400-second ceiling.
The grace is a constant 900 s.

**Expiry.** The hub drops a request at the end of its TTL, so a request that expired on the hub while the
computer slept is simply gone; the host never sees it. The host can still reach `expired` in these cases:
- its clock runs ahead of the hub's;
- inside the up to 1 s of slack that `ceil()` adds to the request's TTL;
- a request it read in time waited past its `not_after` before the host decided it: behind an earlier request
  ([§15](#15-dedupe-restarts-and-transport-failures): one at a time, in sequence order; one read can return
  many requests, and one operation may run up to 300 s), or across a sleep that began after the read.

**Late replies** *(Lucille policy as of 2026-10-28; not proven by this repository)*. Lucille measures a
reply's lateness when it reads the reply, not by the time the hub stamped on it. A reply Lucille reads after
its job's `not_after` + 900 s is tallied and never applied
([section 17](#17-what-a-reply-may-do-on-lucilles-side)), even one the hub stamped earlier. A job with no
applied reply by then fails: as not posted if Lucille never posted it, and as no reply otherwise.

The outcome of a job that got no reply is unknown to Lucille: the computer may have done the work. A
conforming host never started it late.

## 15. Dedupe, restarts and transport failures

**Dedupe.** The host acts on the first request per `job_id` it sees, and never again. The hub can deliver a
message twice, and a crashed Lucille can post the same request again.
- The host keeps its dedupe state in its own file of entries, each a `job_id` and when to prune it. It writes
  an entry before it records its decision, and prunes it 15 minutes after the later of the request's
  `not_after` and the time the host received it (so an entry for a request that was already expired on
  arrival is not pruned at once). It never derives that state from its rotating audit log.
- There is one dedupe state per host profile, across all its rooms. So a request is posted in one room
  only: a `job_id` posted again in a second room gets no reply. Lucille never knowingly posts a job again, in
  any room ([§9](#9-room-lifetime-replacement-and-pair-events)).
- Every request with a well-formed `job_id` that reaches a decision is recorded, refusals included, so a
  duplicate never gets a second reply. An entry whose request had no valid `not_after`
  ([§10](#10-requests)) is pruned at the time the host received it + 86400 s + 15 minutes.

**Restarts.** The host's local log records, for each request, when it was received, what was decided, and
what was sent.
- A request with a decision to run but no record that a reply was sent gets `failed` with `interrupted` after
  a restart, never a second run.
- A refusal with no record that it was sent is posted again, as the same refusal.
- A request that was received but has no decision is decided afresh only if the host can recover it from its
  own durable state. Otherwise it gets no reply, and the job fails when its wait ends
  ([§14](#14-time-not_after-ttls-and-late-replies)).
- None of these applies to a request in a room the host abandoned or left: after a restart the host posts
  nothing there ([§9](#9-room-lifetime-replacement-and-pair-events)). It records the abandonment before it
  kills anything, so a restart never finds a decision to run in an abandoned room without that record.

**Transport failures.** A transport failure is never a `failed` reply.
- A hub error while a child runs does not stop the child.
- A failed post of a reply is retried until the reply's TTL would already have run out
  ([§14](#14-time-not_after-ttls-and-late-replies)).
- A post can fail on the host's side after the hub has stored it (a timeout, or a proxy error after the
  hub's commit), and 2.6.0's posts carry no idempotency key (sb `client.py:1666-1673`). So each retry can
  post another copy of a reply. The first counting reply decides the job
  ([section 17](#17-what-a-reply-may-do-on-lucilles-side)).

**One at a time.** The host handles requests one at a time, in sequence order.

## 16. Local controls

**Both SAS gates.** Until the user confirms the pair's short authentication string (SAS) on the computer, the
host touches nothing, posts nothing, accepts nothing and runs nothing. Until Lucille's own SAS confirmation, Lucille posts,
offers and applies nothing. Pairing's own `confirmed` flag counts for nothing here, because 2.6.0 sets it
when the pair is collected, before anyone compares the SAS.

**Pause.** While the host's pause flag is set, it starts nothing. If it is online, it replies `refused` with
`paused` to each new request, so the job fails fast. It reads the flag again just before it starts a process
it decided to run, and a pause set in between turns that decision into `failed` with `interrupted`. If it is
offline, nothing happens and the requests expire. Nothing Lucille sends can unpause a host.

**Kill and timeout.**
- `lucille-host kill`, or a second Ctrl-C, kills the process group of the operation in flight, and the host
  replies `failed` with `killed_locally`. It also sets the pause flag, so later requests are refused `paused`
  until the user runs `lucille-host resume`.
- A child that runs past its catalog timeout (30 s by default, 300 s at most; `host.sleep` allows `n` + 30 s)
  has its process group killed, and the host replies `failed` with `timeout`.

**Drain.** The first SIGTERM, SIGHUP or Ctrl-C drains: the running operation finishes and its reply is
posted, and nothing new starts. A signal the host was started with ignored (SIGHUP under `nohup`) stays
ignored. Only a running host enforces a time limit or a kill.

## 17. What a reply may do on Lucille's side

**Which reply decides a job.** This is a protocol rule, and the host relies on it when a restart or a
retried post makes it post more than one reply for a job ([§11](#11-replies)): **the first counting reply
decides the job, and a later one is never applied.**
- A reply counts only if it is under the host's id, in the computer's current work room, names a job Lucille
  posted in that room, and is in time (below).
- Read in sequence order within the job's own room, the first counting reply that names the job decides it,
  whether or not it passes Lucille's size and content checks. A reply that fails them fails the job as too
  large or invalid; a later reply never overtakes it. Within one room, sequence order is arrival order, so
  "first" and "lowest sequence number" agree.

**When a reply is in time** *(Lucille policy as of 2026-10-28; not proven by this repository)*. Lucille
judges it when it reads the reply, not by the time the hub stamped on it: a reply is in time if Lucille reads
it before its job's `not_after` + 900 s. A reply Lucille reads later, for example after an outage on
Lucille's side, is tallied and dropped. If no reply for the job was read in time, the job fails as no reply
([§14](#14-time-not_after-ttls-and-late-replies)), though the computer may have done the work.

**What that reply may do** *(Lucille policy as of 2026-10-28; not proven by this repository)*.
- It may move the job to done or failed, and leave its capped output on the job row until the output is
  blanked 24 hours later, shown on the computer's page in Lucille. Nothing else: it posts nothing back,
  writes no memory, adds nothing to a chat and touches no other row.
- Later replies for the job, replies in a replaced room, and anything else are dropped and tallied.
- Lucille keeps no host version on the computer's record, and decides nothing about posting from a reply.

## 18. Keys and what they open

- **A pair secret opens every room offered over the pair**, in both directions, because the offer records
  that carry the rooms' keys are sealed under the pair room's key (sb `sharing.py:27-33`). A heal does not
  change that; a fresh pairing and a new room do.
- **A room's raw key opens every message still on the hub in that room.** A room's content key changes every
  900 s by default, but each epoch's key is derived from the room's raw key, and a reader honours whatever
  epoch a message names (sb `crypto.py:61-86`, `:236-241`, `:281-295`, `:353-362`). Epoch rotation does not
  limit what a leaked raw key exposes, and nothing about this protocol may claim it does. What bounds the
  exposure is each message's TTL (at most 86400 s on the hub), plus whatever the holder captured live.
- **The host holds a second long-lived secret.** Besides its side of the pair, it holds the keys of each
  room it accepted, for as long as it holds the room; with one standing room per computer, that is as long
  as the pair lasts, unless a replacement comes first.
- **The host's share store.** Room keys get the same protection as the pair secret: the host keeps them in
  its OS-keyring store, beside the pair. A file of mode 0600 is used only under `--store-file`, with the
  same warning that it is weaker. Never under `~/.switchboard`. 2.6.0's own `ShareStore` keeps room keys in
  the clear, owner-only, and asks an embedder to keep its store encrypted at rest (sb `sharing.py:229-249`;
  `docs/sharing.md:533-536`). Why: the pair secret already opens every room offered over the pair, so keeping
  room keys more weakly than the pair would make them the weak link.
- **A room key is not a credential Lucille mints.** A room key opens one room's messages and grants nothing
  in Lucille: authority stays with the job row Lucille wrote *(Lucille policy as of 2026-10-28; not proven by
  this repository)*.
- **Lucille's room store.** Lucille keeps the rooms it mints for computers in its per-owner share store,
  encrypted at rest and deleted with the account *(Lucille policy as of 2026-10-28; not proven by this
  repository)*.

## 19. Not specified in v1

Each point below is open in this version. Each says what an implementation does meanwhile.

- **A leak of a room's key alone.** Neither side detects a leak of a work room's key without the pair secret
  (2.6.0's share store holds room keys in the clear unless the embedder protects them), and a heal does not
  answer it, because a heal moves only `pair:<name>` (sb `docs/sharing.md:526-536`). The room is replaced
  only at the next re-pair or suspected leak ([§9](#9-room-lifetime-replacement-and-pair-events)), so, as far
  as this spec can tell, such a leak lasts until then. Replacing the room on a schedule would answer it, and
  v1 deliberately has no schedule. **Meanwhile:** the host keeps room keys as strongly as the pair secret
  ([§18](#18-keys-and-what-they-open)); a user who suspects a leak removes the computer in Lucille, or runs
  `lucille-host forget`, and pairs again, which replaces the room.
- **Re-finding an offer a crash hid.** 2.6.0 reports a pending offer to the host once, as an `offered` note
  the first time a touch sees it, and a touch returns only notes newer than its own start (sb
  `sharing.py:842-846`, `:1246`). None of the sharing calls the host uses lists the offers waiting. So a host
  that stops between a touch that reported an offer and its own record of that offer
  ([§6](#6-offers-and-the-hosts-accept-policy)) cannot find the offer again through those calls.
  **Meanwhile:** the host records each offer a touch reports before it decides on it, which leaves only a
  stop inside that one step. An offer lost there is found again only when Lucille offers a new room (a
  re-pair, heal or lapse, [§9](#9-room-lifetime-replacement-and-pair-events)); a user whose computer runs
  no jobs runs `lucille-host forget` and pairs again.
- **A body with a duplicate key.** v1 does not say how a receiver treats a JSON object that names the same
  key twice. **Meanwhile:** a sender never sends one. The host receives each body already parsed by the
  Switchboard library, which keeps the last value of a repeated key, so it cannot tell.

## 20. Fixtures

The golden fixtures are in `src/lucille_host/spec/v1/fixtures/`, one JSON file each, all ASCII, with every
non-ASCII character escaped. Their job ids are obviously fake: `00000000-0000-4000-8000-NNNNNNNNNNNN`,
where `NNNNNNNNNNNN` is the fixture's number in 12 decimal digits.

**`request/*.json` and `reply/*.json`.** Each holds one message body and labels it:

| Key | Meaning |
|---|---|
| `description` | What the fixture shows. |
| `valid` | Whether a conforming sender may send this body: it passes the schema and every named rule (`not-after-range` in [§10](#10-requests), and those of [§13](#13-strings-numbers-sizes-and-output)). |
| `schema_valid` | What the JSON Schema alone says. A `valid` fixture is always `schema_valid`. |
| `prose_rule` | Present only when the schema accepts the body and a named rule refuses it: one of `strings`, `integers`, `request-size`, `stream-size`, `not-after-range`. |
| `host_state` | Requests only, optional: the host state the expected reply assumes, as overrides of `fixtures/host-state.json`. |
| `expect` | Requests only: `{"reply": null}` when the host posts nothing, or `{"reply": {"status", "code"?}}`; and, where it matters, `effective_caps`. |
| `op` | Replies only, optional: the operation the reply answers, where its `stdout` has a schema (`host.describe`). |
| `body` | The message body. |

**`fixtures/host-state.json`** is the state a request fixture assumes unless its `host_state` says otherwise.
It is a test state, not a host's defaults:

| Key | Meaning |
|---|---|
| `received_at` | When the host read the request (2026-01-01T00:00:00Z). "Now", for `expired` and `not-after-range`. |
| `host_version` | The host's version, for `min_host_version`. |
| `enabled_ops` | The operations its local policy enables. |
| `roots` | The folder aliases its local policy holds. |
| `resolves_outside_root` | Paths whose realpath leaves their root, as a symbolic link out of it would. |
| `paused` | Whether the pause flag is set. |
| `seen_job_ids` | Job ids the host has already decided. |
| `caps` | The host's own cap on each stream: 65536 bytes. |
| `event` | What happens once the operation starts: `runs_past_timeout`, `killed_locally`, `restarted_after_decision`, or null. |

**`decode/*.json`.** Each holds a child's raw output, base64-encoded in `raw_base64`, and the `text` the host
decodes from it ([§13](#13-strings-numbers-sizes-and-output)).

**What the fixtures cover.** A valid request for each operation; a request that draws each refused and each
failed code; a duplicate that draws no reply; the order of [§12](#12-which-refusal-wins), by fixtures with
two faults; the messages the host does not answer; invalid requests with extra keys (`argv`, `exe`, `env`,
`cwd`, `timeout`, policy-shaped keys), a wrong `v`, oversize fields, a string holding U+0000 and one holding
an unpaired surrogate; a valid reply of each shape and with each code; invalid replies for each way a reply
can break the schema or a named rule; and decoding.

**Who is held to them.**
- `tests/spec/test_fixtures.py` checks that each fixture validates, or fails, as labelled, and that each
  request fixture's expected reply is the one the order of [§12](#12-which-refusal-wins) gives.
- The host's own message checks, when they are written, are held to every fixture. The runtime imports no
  JSON Schema library; its runtime dependencies are agent-switchboard and keyring only.
- Lucille's contract test holds Lucille's own request and reply models to the fixtures shipped in a released
  wheel: they must accept every valid fixture and refuse every invalid one *(Lucille policy as of
  2026-10-28; not proven by this repository)*.

## 21. Versioning

- `v` names only the major version: `lucille.exec/1`. Minor versions are recorded in
  [PROTOCOL-CHANGELOG.md](PROTOCOL-CHANGELOG.md), with the host release that first accepts each.
- **A minor change is additive**: a new field, a new code or a new operation. Because every schema here is
  closed, an older receiver refuses anything new, so a sender uses a minor addition only once the receiver is
  known to accept it:
  - Lucille sends a new request field or a new operation only to hosts that accept it, by setting
    `min_host_version` on such a request to the first host release that does. An older host refuses such a
    request `host_too_old`, since that check comes before the envelope's ([§12](#12-which-refusal-wins)).
  - A host sends a new reply field or code only in a release made after Lucille accepts it. The changelog
    records the date Lucille's side reported accepting it; this repository cannot prove that date.
- **An editorial change** only corrects a fixture to agree with the prose and the schemas (where a fixture
  disagrees with them, the fixture is wrong), or makes the prose clearer, and changes nothing either side may
  send or must do. It is recorded in the changelog, names no host release, and changes no version.
- **A major change ships only after Lucille supports both majors.** Anything that is neither additive nor
  editorial is a major change. A new major gets its own schemas, under its own `v<major>/` directory, and
  its own `v` value.
