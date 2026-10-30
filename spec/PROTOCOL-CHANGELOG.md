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
  paths out of the host's own folders and Linux's `/proc`, and off FIFOs, sockets and devices;
  `not_after`, TTLs and expiry; dedupe, restarts and transport failures; pause (read again just before a
  start), kill, timeout and drain.
- **Revised in the draft, with the host's first code.** These change what a side may send or must do:
  - `failed` means the host decided to run the operation and it did not finish; it was "it started and did
    not finish". `interrupted` now covers, besides a restart between the decision and the reply, a decision
    the host did not carry out because a pause or a drain came before the start, or because the process
    could not be started. So `interrupted` does not say whether the operation ran, and Lucille must not
    read it as either (§11).
  - SIGHUP drains as SIGTERM and Ctrl-C do, unless the host was started with it ignored; an operation
    decided but not yet started when the drain begins is not started (§16).
  - The pause flag is read again just before a start, and a pause set in between turns the decision into
    `failed` with `interrupted` (§16).
  - A stream's truncation flag is also set by output the host did not keep under `raw-ceiling` (§11, §13).
  - `path_outside_root` also covers a path into the host's own configuration or state folders, through
    Linux's `/proc`, or naming a FIFO, socket or device (§10).
  The reply schema's descriptions changed to match; no field, status or code was added or removed.
- **Files.** JSON Schemas (draft 2020-12) for the request, the reply and the `host.describe` document, closed
  at every level; golden fixtures for requests, replies, `host.describe` documents and decoding. Both ship
  in the wheel under `lucille_host/spec/v1/`.
- **Not specified in v1.** Detecting a leak of a room's key alone; re-finding an offer a crash hid; how a
  receiver treats a body with a duplicate key; a bound on how deeply a body nests
  ([PROTOCOL.md §19](PROTOCOL.md#19-not-specified-in-v1)).
- **Revised in the draft, after review.**
  - Check 3 of the accept policy compares the two hub URLs as strings once every `/` at the end of each
    is removed ([PROTOCOL.md §6](PROTOCOL.md#6-offers-and-the-hosts-accept-policy)). That is exactly how
    agent-switchboard 2.6.0 stores both: it removes them when it decodes a pairing code (sb
    `pairing.py:599`) and when it keeps an offered room (`sharing.py:1613`). The reviewed text removed one
    trailing `/` and compared the two "as URLs", which left open whether case, a default port or a query
    counted. Now a code or a room from any writer that adds `/` at the end compares equal, and the rest of
    each URL must match as written: case, a port, a query, a fragment, and a `/` before a query or a
    fragment, which is not at the end. 2.6.0 itself writes no `/` at the end of either URL
    (`pairing.py:1430`, `sharing.py:1433`), so between two 2.6.0 sides only the second half matters (see
    the next entry).
  - Lucille mints each work room with the same hub URL as the pairing code it gave that computer, every
    trailing `/` aside ([PROTOCOL.md §6](PROTOCOL.md#6-offers-and-the-hosts-accept-policy)). Check 3
    compares the two as strings, so a room minted on the same hub under another spelling of its URL, such as
    another case or a port, would fail it, and the host would leave the room for good.
  - Before the checks of the refusal order, `host_too_old` included, the host sets aside a body that is not
    a JSON object, has no `dir: "request"` or has no well-formed `job_id`, and posts nothing for it
    ([PROTOCOL.md §12](PROTOCOL.md#12-which-refusal-wins)). §10 already said the host posts nothing for
    such a body; §12 now says that this comes before the checks. Dedupe, the first check, can still end in
    no reply.
  - The prose now states the forms the schemas already held `host_version` and `catalog_version` to: a
    digit first for `host_version`, a letter or digit first for `catalog_version`, then letters, digits,
    `.`, `+`, `_` or `-`; and that neither list in the `host.describe` document names an entry twice
    ([PROTOCOL.md §11](PROTOCOL.md#11-replies)). This changes nothing either side may send.
  - Fixtures ([PROTOCOL.md §20](PROTOCOL.md#20-fixtures)). A fixture kind for the `host.describe`
    document, `fixtures/host.describe/`. Two fake job ids with a letter in every group join the numbered
    ones. §20 now lists what the fixtures are written to pin, and says that a change to a pattern that
    none of its lists names may pass every test, with examples. With the fixtures added, they now hold: a
    request with two faults for each two neighbouring checks of §12 that one request can fail together,
    and for each of four bodies the host does not answer with a `min_host_version` the host is older than;
    a body at and one past each numeric and length bound no pattern implies; and, for each pattern but the
    one that refuses only U+0000, in each message and document, the bodies `tools/spec/gen_fixtures.py` writes: an empty string, one character of each
    refused kind in each class's place, each separator left out, doubled or replaced, each counted
    repetition one past its bound, and each part of `min_host_version` empty or `00`. A test checks that
    the script writes exactly the committed files, and that each of their bodies has that one fault and no
    other. Some fixtures are renamed, so that the members of each family are named alike, and some are
    replaced by the script's.
  - Editorial: three citations of agent-switchboard 2.6.0 are corrected (the share wake at
    `sharing.py:1175-1186`, in §5; `ShareStore`'s owner-only text at `:263-273` and the `Store`
    interface's request to keep the store encrypted at rest at `:237-240`, in §18), and §10 gives `1.2`
    and `1.2-beta` as host versions that do not start with a numeric triple.
- **Revised in the draft, with the host's pairing and read loop.**
  - No re-accept ([PROTOCOL.md §6](PROTOCOL.md#6-offers-and-the-hosts-accept-policy)). The text said the
    host never accepts again a room it has left. Through the sharing calls the host uses, a pending offer
    shows no workspace, so the host can tell a room it left only once it has accepted it: it then leaves the
    room at once and never uses it, as it does a room that fails check 3 or 4. Lucille therefore sees an
    "accepted" record for such a room, and then the host's `left` record.
  - An accept that fails without refusing the offer, because the hub or the pair room could not be read, is
    tried again after the next touch that reads the pair room
    ([PROTOCOL.md §6](PROTOCOL.md#6-offers-and-the-hosts-accept-policy)). The text listed no such time, and
    the library reports an offer once, so such an offer waited for an unrelated event.
  - The host reads the library's notes from its own share store after every sharing call, a refused one
    included, so that an offer noted inside an accept that then refused is not lost
    ([PROTOCOL.md §6](PROTOCOL.md#6-offers-and-the-hosts-accept-policy) and
    [§19](PROTOCOL.md#19-not-specified-in-v1)).
  - A deeply nested body is a point v1 leaves open; meanwhile the host reads a body nested more than 64
    levels deep as one it cannot open, and posts nothing for it
    ([PROTOCOL.md §19](PROTOCOL.md#19-not-specified-in-v1)).
- First accepted by: no host release yet. Lucille: not yet.
