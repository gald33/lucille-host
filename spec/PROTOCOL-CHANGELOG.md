# lucille.exec protocol changelog

Every change to the protocol in [PROTOCOL.md](PROTOCOL.md), its JSON Schemas and its fixtures is recorded
here, newest first, in the same pull request as the change.

## The versioning rule

- A version is `MAJOR.MINOR`. The `v` field of every message names only the major: `lucille.exec/1`.
- **A minor change is additive.** It adds a field, a code or an operation, and changes or removes nothing.
- **An editorial change** only corrects a fixture to agree with the prose and the schemas (where a fixture
  disagrees with them, the fixture is wrong), or makes the prose clearer, and changes nothing either side may
  send or must do. It is recorded here and changes no version.
- Every schema is closed, so an older receiver refuses anything a minor change adds. A sender therefore uses
  an addition only once the receiver is known to accept it:
  - Lucille sends a new request field or a new operation only with `min_host_version` set to the first host
    release that accepts it.
  - A host sends a new reply field or a new code only in a release made after Lucille accepts it.
- **A major change ships only after Lucille supports both majors.** Anything that is neither additive nor
  editorial is a major change: a new `v`, and its own schemas under its own `v<major>/` directory, beside
  the old ones.
- Each additive or major entry names the host release that first accepts the change, and, for a change
  Lucille must accept first, the date Lucille's side reported accepting it; an editorial entry names none.
  This repository cannot prove that date.

## 1.0 (draft, unreleased)

The first version.

- **Transport.** Requests and replies travel on the channel `lucille.exec`, as Switchboard notes with no
  thread, only in a work room that Lucille minted with `sharing.share_new` and offered over that one
  computer's pair; the host acts only on requests in a room it accepted. `pair:<name>` carries only the
  Switchboard library's own notes, which the host reads as wakes.
- **Offers.** The host's accept policy (five checks; the two that can run only after `sharing.accept` hold
  for every held room at every use and end in an immediate leave on a failure; a refusal by the other three
  is not final), a cap of two rooms, which room is newer (one that has passed the two later checks), no
  accept while several offers are pending, no re-accept, and the host's listening, touching and resolving
  rhythm (no touch before the host's SAS gate opens, then one every 5 minutes).
- **Rooms.** One standing work room per computer, replaced only on re-pair or a suspected leak (a heal or a
  lapse of `pair:<name>`), never on a schedule; what each side does with a replaced room, at re-pair, at the
  end of a pair, and with a room the host leaves on its own, which is not replaced; who leaves a room, and
  when.
- **Agent ids.** The host's local agent id is `lucille-host`, and Lucille's is never that. Agent ids are
  not authentication.
- **Messages.** The request envelope and the four operations of the v1 catalog (`host.describe`,
  `host.sleep`, `files.sha256`, `files.search`), each with its own closed `args` schema; the reply's three
  shapes; the closed status and code lists; the document `host.describe` returns in `stdout`, which no
  decoding, redaction or capping touches.
- **Rules.** The refusal order (`host_too_old` before the envelope check); which reply decides a job; the
  named rules `strings`, `integers`, `request-size`, `stream-size` and `not-after-range`; how size is
  measured; decoding, redaction and capping, and the host's rule `raw-ceiling` (at most 4 MiB of each
  stream's raw output is kept, and what is not kept sets the truncation flag); containment, which keeps
  paths out of the host's own folders and Linux's `/proc`, and off FIFOs, sockets and devices; `not_after`, TTLs and expiry;
  dedupe, restarts and transport failures; pause (read again just before a start), kill, timeout and
  drain.
- **Files.** JSON Schemas (draft 2020-12) for the request, the reply and the `host.describe` document, closed
  at every level; golden fixtures for requests, replies and decoding. Both ship in the wheel under
  `lucille_host/spec/v1/`.
- **Not specified in v1.** Detecting a leak of a room's key alone; re-finding an offer a crash hid; how a
  receiver treats a body with a duplicate key ([PROTOCOL.md §19](PROTOCOL.md#19-not-specified-in-v1)).
- First accepted by: no host release yet. Lucille: not yet.
