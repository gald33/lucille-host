# The lucille-host command line, contract version 1

What a program that drives lucille-host (a "shell": the Mac app, a later Linux or Windows app, a script)
can rely on: the verbs it calls, the JSON objects each one writes, and the exit codes.

**Version 1.1, draft.** lucille-host `0.1.0` implements 1.0; the release after it implements 1.1. It stays a draft, and can still change, until the
release that opens pairing to anyone other than the repository owner. This document, the JSON Schemas and the golden
fixtures are the contract together. It is separate from the protocol between Lucille and the host
([PROTOCOL.md](PROTOCOL.md)), and has its own version.

## 1. Files

| File | What it is |
|---|---|
| `spec/CLI.md` | This document. |
| `src/lucille_host/spec/cli/v1/<type>.schema.json` | One JSON Schema per object type (section 6). |
| `src/lucille_host/spec/cli/v1/fixtures/<case>.json` | The golden fixtures: a command line, its exit code, and the objects it writes (section 11). |
| `src/lucille_host/spec/__init__.py` | `lucille_host.spec`: finds and reads the files above, and the protocol's, from wherever the package is installed. |

- The schemas and fixtures ship in the wheel as package data, at `lucille_host/spec/cli/v1/`, beside the
  protocol's. The tests load them from an installed wheel (`tests/spec/test_packaged.py`).
- The schemas are JSON Schema draft 2020-12. Each schema's `$id` is
  `https://github.com/gald33/lucille-host/blob/main/src/lucille_host/spec/cli/v1/<file>`. It identifies the
  schema; nothing fetches it.
- Every object in every schema sets `additionalProperties: false`. Where a type has a `state`, the schema is
  a `oneOf` with one closed branch per state, and each branch lists every field it allows as required.
- Patterns are ECMA-262, anchored with `^` and one final `$`, as in the protocol's schemas.
- The host does not validate its own output against these schemas at run time. It holds no JSON Schema
  library. Its tests hold every verb's output to them.

## 2. The rules for a shell

- **A shell never parses human output.** Everything it needs to decide is a field of an object below. The
  words on standard error, and the words in an object's `message`, `summary`, `sends` and `text`, are for
  people. A shell shows them; it never branches on them.
- **A shell never writes the host's files.** Every change goes through a verb: `ops enable`, `roots add`,
  `pause`, `pair confirm`, and the rest. It does not read them either for anything a verb reports.
- **A secret is never passed in argv.** A pairing code is never given to the host at all: it arrives in a
  Connect request's drop room, from Lucille (`pair begin`). `pair` refuses anything on its command line that
  is not an action (`code_in_argv`), and does not repeat it. A secret a later version takes is read from
  standard input. The host never writes a pairing code it was delivered, in JSON or in text. A word given on
  the command line may be quoted on standard error by a usage error; with `--json`, the `usage` object never
  quotes it.
- **The user compares the SAS.** A shell shows the user the `sas` the host wrote, and runs `pair confirm`
  only when the user says Lucille's page shows the same. It never confirms on its own, and never before it
  has shown that SAS. It calls `pair status` only while it shows the user the `sas` that call returns. To
  learn where the pair is without showing the SAS, it calls `status` (section 7, "The SAS in JSON").
  `status` makes no hub call, so its `waiting` cannot tell a pair whose answer Lucille has not collected
  from one whose answer it has collected since a command last asked the hub: a shell that finds `waiting`
  shows its pairing screen, where it calls `pair status`.
- **A reader ignores a `type` it does not know**, and a field it does not know. Section 12 says what may be
  added within version 1, and what a reader does with a value it does not know.
- **A reader checks `v`.** Every object carries `"v": 1`. An object with another `v` is from another major
  version of this contract; a reader that does not know it stops. (A shell supervising `run` keeps reading
  instead, and stops `run`: section 13.2.)
- **A shell validates in its tests, not at run time.** It holds its decoders to the schemas and fixtures of
  the host release it ships with.
- **A shell runs every verb with a time limit**, except `run`, which it supervises (section 13). Any verb
  that reads the local policy may wait on a folder of it that is a stalled network mount, since loading the
  policy looks at each folder; `roots add` and `roots check` also open the folder, and on macOS
  `roots check` may wait while macOS asks the user about a protected folder. A shell stops a verb that runs
  too long with `SIGINT` or `SIGTERM`.

## 3. Asking for JSON

- `--json` asks for JSON. It may come before the verb, after it, or after the verb's action:
  `lucille-host --json ops list`, `lucille-host ops --json list` and `lucille-host ops list --json` are the
  same. It must come before a `--`: after `--`, every word is an argument (`roots add --json -- docs ~/Docs`).
  A shell writes it in full; the parser also takes a unique prefix of it, as it does of every long option,
  and that is not part of this contract.
- `--profile NAME` selects a separate config folder, state folder and store, so that one computer can hold
  more than one host. It comes before the verb: `lucille-host --profile mac-app --json pause`.
- `--json` with no verb, `--json --version` and `--version --json` write one `version` object and exit 0. As
  without `--json`, `--version` ends the command line at once: nothing after it runs.
- `-h` and `--help` print help for people on standard output, also with `--json`, and exit 0: they are the
  one exception to section 4.1. A shell never asks for help.
- `run`'s JSON form is its event stream, `run --json-events` (section 13). `--json` beside it adds nothing:
  `lucille-host --json run --json-events` and `lucille-host run --json-events` are the same.
  `lucille-host run --json` alone is a usage error (exit 2), so that a shell never reads `run`'s text as
  JSON. `--json-events`, `--exit-on-stdin-eof` and `--allow-downgrade` are options of `run` only; with
  another verb they are a usage error, and `--json-events` asks for the `error` object as `--json` does.

## 4. The output

### 4.1 Standard output

- With `--json`, standard output holds only JSON objects, one per line. Each line is one object, written
  whole with its newline and flushed before the host does the next thing, so a reader can act on each line
  as it arrives. A verb that streams (`pair begin`, `roots check`) writes each object when it is known.
- Each object has `"v": 1` and a `"type"` (section 6), then that type's fields.
- The output is ASCII: every character outside ASCII is written as a JSON `\u` escape. Every string is valid
  Unicode. A file name that is not valid UTF-8 reaches the host as unpaired surrogates; each is written as
  U+FFFD, so such a name can be shown but not passed back.
- Objects are written compactly, with their fields in the order section 6 lists them. A reader depends on
  neither the order nor the spacing.
- Times are UTC in ISO 8601 to the second: `2026-11-01T12:00:00Z`. Durations and skews are numbers of seconds.
- Every number is finite: the host never writes `NaN` or an infinity, which JSON does not have.

### 4.2 Standard error

Standard error stays human, and carries the same text with `--json` as without it: warnings
(`lucille-host: warning: ...`), notes, the failure line (`lucille-host: ...`, `<word>: ...` or
`error: ...`), the parser's usage text, and the traceback of an unexpected error. The lines that a verb
prints on standard output without `--json` are not printed with it: the objects take their place. With
`--json`, `pair` asks nothing, so it writes no prompt.

### 4.3 Exit codes

They are the same with `--json` as without it.

| Exit | Meaning | Standard output with `--json` |
|---|---|---|
| 0 | The verb did what was asked. | Its objects (section 7). |
| 1 | The verb failed or refused. | What it wrote before it stopped, then one `error` object, last. |
| 2 | Usage: the command line could not be parsed, or names a form version 1 does not have. | One `error` object with `code` `usage`, when the line holds `--json` before any `--`. |
| Ended by a signal | Ctrl-C outside a pairing wait, `SIGTERM`, `SIGKILL`. | What was written before; nothing more. |

- With `--json`, every exit of 1 or 2 ends standard output with an `error` object. Ctrl-C while `pair`
  waits for Lucille is the exception to the last row: it ends with an `error` object with `code`
  `interrupted`, exit 1, and the pair is kept, as without `--json`.
- **A shell tells a signal from an exit by how the process ended, never by the number.** A host ended by
  Ctrl-C has exit status "ended by signal 2", which some APIs report as the number 2 beside a separate
  reason (Foundation's `Process.terminationReason` is `.uncaughtSignal`). With `--json`, a 1 or a 2 with
  no `error` object at the end of standard output was a signal.
- **How a shell stops a verb.** `SIGINT` (Ctrl-C) stops `pair` while it waits for Lucille with
  `interrupted`, and keeps the pair. `SIGTERM` ends any verb with nothing more written; the pair is then in
  whatever state the host had reached. After any stop, a shell calls `status` to learn where things are.
- `run` has exit codes of its own, and ends with a `stopped` object rather than an `error` (section 13.3).
  The table above holds for every other verb.

## 5. The error object

```json
{"v":1,"type":"error","code":"not_in_catalog","message":"host.nothing is not in this release's catalog"}
```

| Field | Type | |
|---|---|---|
| `code` | string | What failed, from the list in section 10. A shell branches on this. |
| `message` | string | The sentence standard error shows, without its `lucille-host: `, `<word>: ` or `error: ` prefix. For people only. |
| `kind` | `home`, `holds_home` or `root` | For `broad_folder` only: the folder is the home folder, holds it, or is `/`. |

- For `usage`, the `message` is always "the command line could not be parsed; standard error says why":
  the parser's own words, which can quote the command line, are on standard error only.
- For `answer_failed` and `pair_unusable`, the `message` is the host's words, the pairing library's state in
  parentheses, then a colon and why, in the host's words: ``the code was not answered (error): this computer
  could not load the encryption library pairing needs (`cryptography`)``. The host repeats no text of the
  library's: the library's sentence can name the file that holds the pair secret, or quote what the other
  side wrote. The state is one of the library's own words (or the host's `invalid_sas`), and `error` for none
  or any other. The reason is one of the sentences below, each standing for one sentence of switchboard
  2.6.0's, which must match it whole, its variable parts (an absolute path ending in a folder named `pairs`,
  a pair name, a short value) bounded; any other sentence of the library's, a sentence of a later release
  included, gives the last. A refusal with no sentence ends after the state. `status` gives a message of the
  same form in `pair.message` when its `code` is `pair_unusable`.

  | The library refused because | Why, as the host says it |
  |---|---|
  | `cryptography` did not load | this computer could not load the encryption library pairing needs (`cryptography`) |
  | the pair's file of the file store is not JSON | the pair's file in the host's store folder is not valid JSON; it holds the pair secret, so it is left as it is: move it aside |
  | another of its files is not JSON | a file the pairing library keeps in the host's store folder is not valid JSON; it is left as it is: move it aside |
  | the pair's record is not a record | the pair's record in the host's store is damaged: it is not a pair record |
  | the record is of another protocol | the pair's record in the host's store is for a pairing protocol this release does not speak |
  | the record lacks a field | the pair's record in the host's store is missing a field |
  | the record names a role or strategy it does not know | the pair's record in the host's store names a role or a healing strategy this release does not know |
  | an unfinished pairing in the store is damaged | an unfinished pairing in the host's store is damaged |
  | the record's name is not one it allows | the pair's record in the host's store holds a name pairing does not allow |
  | the record's work-room number cannot be used | the pair's record in the host's store holds a work-room number that cannot be used |
  | the code's handshake key is a low-order point | the code's handshake key cannot be used: the code was tampered with, or was not made by a pairing library |
  | the pair room holds a record neither side wrote (`tampered`) | the pair room holds a record that is not what either side wrote: the hub is serving something it was not given, and nothing was changed |
  | a record in the pair room names a healing strategy it does not know (`upgrade`) | the pair was moved on with a healing strategy this release does not know: update the host before using the pair again |
  | the host's store holds a SAS of another shape (`invalid_sas`, the host's own) | the store holds a SAS of another shape, which the host never shows |
  | the profile keeps no pairs (`disabled`, the host's own) | this profile keeps no pairs |
  | anything else | the pairing library refused it for a reason this host does not repeat |
- An unexpected error (a bug) writes its traceback to standard error and an `error` object with `code`
  `internal`, and exits 1.

## 6. Types

Fields are listed in the order the host writes them, after `v` and `type`. A field is in an object exactly
when the table lists it for that object's state; there are no optional fields. "or null" marks a value that
can be unknown. Arrays are written in the order each type states.

### `version`

| Field | Type | |
|---|---|---|
| `host_version` | string | This release's version, as `lucille-host --version` prints it. |
| `contract` | string, `1.<minor>` | The version of this contract this release writes, `1.1` here. |

### `op`: one catalog operation, or a policy entry this release does not have

| Field | Type | In states |
|---|---|---|
| `name` | string | all |
| `state` | `enabled`, `disabled`, `disabled_by_upgrade`, `unavailable` or `not_in_catalog` | all |
| `enabled` | boolean | all |
| `summary` | string, at most 60 characters | all but `not_in_catalog` |
| `sends` | string, at most 300 characters | all but `not_in_catalog` |
| `sends_file_content` | boolean | all but `not_in_catalog` |
| `diagnostic` | boolean | all but `not_in_catalog` |
| `missing_executable` | string: an absolute path | `unavailable` only |

- `state`:
  - `enabled`: the local policy enables it, and this computer can run it.
  - `disabled`: the local policy does not enable it, and this computer could run it.
  - `disabled_by_upgrade`: the local policy enabled it under a definition this release changed, so it is off
    until enabled again (`ops enable` accepts the new definition). This computer can run it.
  - `unavailable`: this computer cannot run it: `missing_executable` is not an executable file here. The
    policy may enable it or not; `enabled` says which. This state comes first: an operation this computer
    cannot run is `unavailable` even when an upgrade changed its definition, because `ops enable` would
    refuse it.
  - `not_in_catalog`: the local policy names an operation this release does not have.
- `enabled` is the switch's position: true exactly when the local policy enables the operation with this
  release's definition, whether or not this computer can run it. So an `unavailable` operation the policy
  enables has `enabled` true, and one it does not, or enables under an earlier definition, has `enabled`
  false. `disabled_by_upgrade` and `not_in_catalog` are always false.
- `summary` is a few plain words for what it does. `sends` says what Lucille gets when it runs.
- `sends_file_content` is true when a reply can carry the contents of a file, as a search's matching lines
  do. A shell asks the user to acknowledge that before it enables such an operation, and decides so on this
  field, never on the words of `sends`. In this release, `files.search` is the one operation that does.
- `diagnostic` is true for an operation meant for testing (`host.sleep`). A shell may hide it.
- Section 8 says where `summary`, `sends`, `sends_file_content` and `diagnostic` come from.

### `op_removed`

| Field | Type |
|---|---|
| `name` | string |

`ops disable` took out of the local policy an operation this release's catalog does not have. The next
`ops list` has no row for it, so a shell drops the row.

### `root`: one folder alias

| Field | Type | |
|---|---|---|
| `alias` | string, `^[a-z][a-z0-9_-]{0,31}$` | The name Lucille uses. |
| `path` | string: an absolute path | The folder, as the local policy holds it. |
| `broad` | boolean | It is the home folder, holds it, or is `/`, and was added with `--allow-home-or-root`. |

### `root_removed`

| Field | Type |
|---|---|
| `alias` | string |

### `root_check`: whether the host can open one folder

| Field | Type | In states |
|---|---|---|
| `alias` | string | all |
| `path` | string | all |
| `state` | `ok`, `denied_by_macos`, `denied`, `missing` or `error` | all |
| `errno` | string, `^E[A-Z0-9]{1,15}$` | `error` only |

Section 9 says how each state is decided. The text line shows the alias and the state; the path is the one
`roots list` shows.

### `alias_suggestion`

| Field | Type |
|---|---|
| `alias` | string, `^[a-z][a-z0-9_-]{0,31}$` |

### `controls`

| Field | Type | |
|---|---|---|
| `paused` | boolean | Whether the host is paused, once the verb is done: true after `pause` and `kill`, false after `resume`. |

### `status`: what `lucille-host status` shows, as one object

| Field | Type | |
|---|---|---|
| `host_version` | string | |
| `policy` | object, below | The local policy check. |
| `paused` | boolean | |
| `store` | `keyring` or `file` | Where the pair is kept: the OS keyring, or files under `--store-file`. |
| `hub` | string | Lucille's hub. |
| `loopback_hub` | string or null | The loopback hub `pair --hub` named, when one is configured. |
| `pair` | object, below | Where the pair is. **Never the SAS.** |
| `work_rooms` | array of objects, below | The work rooms held, by name, then the room of an interrupted accept. |
| `last_left_because` | `check_3`, `check_4`, `left_before` or null | When no room is held and the host left its last one on its own: the check that room failed ([PROTOCOL.md](PROTOCOL.md) section 6). |
| `offers_waiting` | integer | Offers of a room that wait to be judged. |
| `run` | object, below | What the latest `lucille-host run` last wrote. |
| `last_hub_contact` | string (a time) or null | When `run` last reached the hub. |
| `clock` | object, below | |

`status` makes no hub call. It reads this computer's files and its store, and writes one thing: its line in
the run record, as every command does (section 16). It reports what it can:
when the store that keeps the pair cannot be read, `pair` says so (`unreadable`), and every other field is
still filled in. A file of the host's own that holds something else still stops it (`state_invalid`), and so
does one that holds a value the host never writes there: a time or a skew that is not a finite number in
its range, or a pid out of range.

The text of `lucille-host status` ends with the claim of [PRIVACY.md](../PRIVACY.md), as this release ships
it. The object leaves it out: it is the same in every `status` of a release, and is not a state to read.

`policy`:
- `{"state": "ok", "file": <path or null>, "enabled": [...], "disabled": [...], "folders": [...]}`. `file`
  is null when there is no policy file yet, and the policy is the fresh one. `enabled` lists the operations
  that run here, by name. `disabled` lists `{"name": ..., "why": ...}` for each the policy enables that does
  not run here, `why` being `disabled_by_upgrade`, `unavailable` or `not_in_catalog`, as `ops list` gives its
  `state`, in that order of `why` and then by name. `folders` lists the aliases, by alias.
- `{"state": "invalid", "error": <why>}`: the policy file is invalid, and the host acts on nothing.

`pair`, by `state`. It shares state words with the `pair` type, not its fields: a decoder keeps the two
apart.

| `state` | Other fields | |
|---|---|---|
| `none` | | This computer holds no pair. |
| `waiting` | `name` | No command has seen Lucille collect this computer's answer yet. `status` makes no hub call, so Lucille may have collected it since: `pair status` asks the hub. |
| `sas_ready` | `name` | The SAS has reached this computer, and no command has shown it yet. `pair status` shows it. |
| `sas_pending` | `name`, `deadline` | The SAS was shown, and is not confirmed. |
| `confirmed` | `name`, `generation` (integer or null) | The local gate is open. `generation` is the generation of the pair room this computer last followed. It is null when the store does not say, or holds one above 2^53 - 1, which Lucille's side could set and a JSON reader may not keep exactly. |
| `refused` | `name` | The pair has no local gate record, so it can never be confirmed here; `pair reject` forgets it. |
| `expired` | `name`, `deadline` | The SAS was not confirmed in time; the next command that resolves the pair forgets it. |
| `several_pairs` | `message` | The profile holds more than one pair, and the host keeps one. |
| `unreadable` | `code`, `message` | The store that keeps the pair could not be read: `code` is `insecure_keyring`, `store_error` or `pair_unusable` (section 10). |

Each of `work_rooms`, by `state`. With `pair` `unreadable`, whether a pair is held is not known, so no room
is reported as `pair_gone` or `accept_interrupted`.

| `state` | Other fields | |
|---|---|---|
| `reading` | `name`, `accepted_at` | `run` reads it. |
| `not_read` | `name`, `accepted_at` | Held, and `run` is not reading it now. |
| `read_stuck` | `name`, `accepted_at` | A message there cannot be opened, and `run` reads no further there. |
| `abandoned` | `name`, `accepted_at`, `why` | Nothing is taken from it; its leave is tried again, or `forget` leaves it. |
| `pair_gone` | `name`, `accepted_at` | The pair it was taken over is gone from this computer; `forget` leaves it. |
| `accept_interrupted` | `name` | An accept that a stop interrupted may have kept it, over a pair that is gone; `forget` leaves it. |

`why` is one of `replaced` (a newer room replaced it), `left_by_lucille`, `pair_moved` (pair:<name> was
healed or lapsed), `not_over_current_pair` (its list no longer names the pair), `pair_over`, `gone` (it left
the share store without this host leaving it), `check_3`, `check_4` and `left_before`. These words, and
those of `last_left_because`, are for branching; the text says them in words.

`run`, by `state`: `none` (no record); `running` with `pid` and `as_of`; `not_seen` with `pid` and `as_of`
(it stopped without saying so); `stopped` with `as_of`.

`clock`, by `state`: `ok` with `skew_s`; `skewed` with `skew_s` (a number or null); `unknown`. `skew_s` is
positive when this computer's clock is behind the hub's. `skewed` means the hub refuses what the host writes,
because this computer's clock is more than 300 s from the hub's.

### `log_line`

| Field | Type | |
|---|---|---|
| `text` | string | One line of the audit log, as `lucille-host log` prints it: printable ASCII, any other byte shown as `?`. |

The audit log's own format is not part of this contract. A line is one record the host wrote, but the log
is writable by the user's account and proves nothing, and its records change as the host grows. A shell
shows the text as it is, or not at all; it decides nothing from it.

### `forget`

| `state` | Other fields | |
|---|---|---|
| `forgotten` | `name`, `lucille_told` (boolean) | The pair's work rooms were left, then the pair was forgotten here. `lucille_told` says whether Lucille was told; if not, Lucille keeps its half until the computer is removed there. |
| `left` | `name` | No pair was held. The work rooms still held over a pair already gone from this computer were left. Lucille was told nothing. |

### `pair_code`: what a pairing code says, before it is answered

| Field | Type | |
|---|---|---|
| `name` | string, `^[a-z0-9][a-z0-9-]{0,31}$` | The pair's name. |
| `hub` | string | The hub the code names: Lucille's, or the loopback hub `--hub` named. |
| `note` | string, at most 200 characters | The code's note, made safe as the text output makes it: control, reordering and separator characters, and unpaired surrogates, become spaces, and it is trimmed; `""` when it has none. Whoever made the code chose it. |

### `policy_reset`

No other fields. The local policy was set aside, and the fresh policy put in its place: only `host.describe`
is enabled, and no folder is held.
- `pair` writes it when it sets a policy aside for the new pair, as `config/policy.previous.toml`, which
  `policy previous` lists. It is written only when a policy was moved; a pairing from the fresh policy moves
  nothing. It comes before the code is answered, so a `pair` that then fails may already have written it;
  the policy was set aside all the same.
- `policy reset` writes it when it sets an invalid policy file aside, as `config/policy.invalid.toml`, which
  the host never reads.

### `pair`

| `state` | Other fields | Written by |
|---|---|---|
| `none` | | `pair status` |
| `accepted` | `name`, `clock_skewed` (boolean) | `pair begin`, `pair resume` |
| `waiting` | `name` | `pair begin`, `pair resume`, `pair status` |
| `sas_pending` | `name`, `sas` (`^[0-9]{4}-[0-9]{4}$`), `deadline` | `pair begin`, `pair resume`, `pair status` |
| `confirmed` | `name` | `pair status`, `pair confirm` |
| `rejected` | `name`, `lucille_told` (boolean) | `pair reject` |

`clock_skewed` is true when the hub refused the answer because this computer's clock is more than 300 s from
its own: the answer was kept, and is sent again while `pair` waits, but the clock must be set right first.

### `connect`: a Connect request from Lucille's chat

[decisions/0002-macos-app.md](../decisions/0002-macos-app.md) §11.4: the user sends Lucille the connect code
in their chat, and Lucille answers into a room only the two of them can find.

| `state` | Other fields | Written by |
|---|---|---|
| `code` | `code` (`^[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}$`), `expires_at`, `service`, `username` | `pair begin`, `pair resume`, `pair status` |
| `delivered` | `service` | `pair status` |
| `connecting` | `service`, `from`, `handle` | `pair begin`, `pair resume` |
| `cancelled` | | `pair cancel` |

- `code` is the request: the user sends `connect <code>` to Lucille before `expires_at`. It works once.
  `service` is the chat service (`telegram`), and `username` the user's own username there, as the host
  normalized it: lower case, with no `@` (`^[a-z][a-z0-9_]{0,63}$`).
- `delivered`: Lucille's answer has arrived, and nothing was answered; `pair resume` connects.
- `connecting`: Lucille's answer passed every check, and names the account the user typed; the code it
  carries is answered next, and `pair_code`, `policy_reset` if a policy was set aside, and the `pair`
  objects follow.
  `from` is the chat account Lucille signed, its service and stable id (`^[a-z]{1,16}:[0-9A-Za-z_-]{1,64}$`),
  and `handle` the handle Lucille signed beside it (`^@[A-Za-z][A-Za-z0-9_]{0,63}$`): the app shows
  "Connecting as @ada on Telegram".
- `cancelled`: the request was dropped, and its code connects nothing from now on.

### `previous_policy`

| `state` | Other fields | |
|---|---|---|
| `none` | | No pairing has set a policy aside. |
| `found` | `set_aside_at`, `ops` (array of names, by name), `folders` (array of `{"alias", "path", "broad"}`, by alias) | The last policy a pairing set aside. Nothing uses it. `set_aside_at` is when it was set aside, to the second, and listing it never changes it. A pairing sets the policy aside in the second the pairing began or a later one, so a shell offers it back only when `set_aside_at` is at or after the second in which the shell started the pairing it is showing. |

### `ready`

| Field | Type | |
|---|---|---|
| `host_version` | string | This release's version. |
| `pair` | string, `^[a-z0-9][a-z0-9-]{0,31}$` | The pair `run` serves. |

`run` passed its start checks (section 13.3) and serves the pair. It is written once, first, before `run`
first calls the hub, and a `state` follows it, unless `run` stops at once: a state file of the profile that
cannot be read (`rooms.json`, say) makes `stopped` `failed` follow `ready` directly, but for those the start
checks read, which stop `run` before `ready`. After it, `run` ends only with a `stopped` reason of exit 0, 1
or 20.

### `state`: what a running `run` sees now

| Field | Type | |
|---|---|---|
| `pair` | string | The pair `run` serves, as in `ready`. |
| `confirmed` | boolean | The local gate is open: the SAS was confirmed on this computer. Until then `run` touches nothing, accepts nothing, posts nothing and runs nothing. |
| `paused` | boolean | The pause flag is set. |
| `policy` | `ok`, `invalid` or `checking` | The local policy: valid; invalid, so `run` acts on nothing until it is valid again (section 13.5), and `status` says why; or not checked yet, when `run` accepts nothing either: just after `run` started; after a valid file changed, until a load of it says it is still valid; and when `run` would accept a room under a load that began 30 s ago or more, until a new load answers. An invalid file that changed stays `invalid` until a load of it says it is valid. |
| `last_hub_contact` | string (a time) or null | When `run` last reached the hub. |
| `clock` | object | As in `status`: `ok` with `skew_s`, `skewed` with `skew_s` (a number or null), or `unknown`. |
| `rooms` | array of objects, by name, at most 2 | The work rooms `run` reads now: `{"name", "state"}`, `state` being `reading`, or `read_stuck` when a message there cannot be opened and `run` reads no further there. Empty while it reads none. A room held and not read is not listed: while the policy is not `ok`, `run` starts reading no room, so one held when `run` starts, or whose reading stopped, is listed only once the policy is `ok`; and none is listed once `run` drains. `status` says which rooms are held. |
| `offers_waiting` | integer | Offers of a room that wait to be judged. |
| `last_left_because` | `check_3`, `check_4`, `left_before` or null | As in `status`: when no room is held and the host left its last one on its own, the check that room failed. |

A computer has one standing work room ([PROTOCOL.md](PROTOCOL.md) §9), so `rooms` is empty before Lucille
first offers one, and after the host left one on its own, which Lucille does not replace: then
`last_left_because` says why.

### `request`: one request, at each step

Every `request` has `phase` and `job_id` (Lucille's id for the job, a lowercase UUID), then the fields of its
phase. A request read before a restart and decided after it is written again from `received`.

| `phase` | Other fields | When |
|---|---|---|
| `received` | `op`, `root`, `path` | The request was written to the audit log, before it is decided. |
| `decided` | `decision` `run` | It is to run. |
| `decided` | `decision` `refused`, `code` | It is refused. `code` is the protocol's refusal code. |
| `started` | | The process it runs is about to start. Only for an operation that starts a process. |
| `finished` | `status` `done`, `exit_code`, `duration_ms`, `stdout_bytes`, `stderr_bytes`, `reply_bytes` | It ran, and its reply is built. |
| `finished` | `status` `refused`, `code`, `reply_bytes` | Its refusal is built. |
| `finished` | `status` `failed`, `code`, `exit_code`, `duration_ms`, `stdout_bytes`, `stderr_bytes`, `reply_bytes` | It was decided to run and did not finish: `timeout`, `killed_locally` or `interrupted`. `exit_code`, `stdout_bytes` and `stderr_bytes` are null when the reply has none. |
| `finished` | `status` `abandoned` | Its work room was given up while it was decided or ran (the pair moved, or `forget`): it was stopped, and gets no reply. |

- `op` is the request's op when it has the form of an operation name and the request is within the
  protocol's size limit; otherwise null.
- `root` and `path` are the request's folder alias and path when its op is in this release's catalog, works
  in a folder, and its arguments hold to that operation's schema; otherwise null. `path` is null too when it
  starts with `/`, or holds a control, reordering or separator character (those `pair_code`'s `note` writes
  as spaces). A path is what Lucille asked for, not a file on this computer: a shell shows it, and never uses
  it to find a file.
- `exit_code` is as the reply carries it, from -255 to 255. `duration_ms` is from 0 to 2147483647.
- `stdout_bytes` and `stderr_bytes` are each stream's size in the reply, measured as the protocol measures a
  stream (its JSON-encoded string, both quotes included): from 2 to 65536. `reply_bytes` is the size of the
  reply as posted, from 1 to 262144.
- `finished` comes once the reply is built and handed on to be posted: it ran on this computer. Whether the
  hub took the reply is in the audit log, not in an event.
- A request gets these objects only from the step at which it is written down. A message that is not a
  request, a duplicate, a request read while `run` drains, and one waiting while the policy is invalid get
  none. A reply posted again after a restart (`failed interrupted`, or the same refusal) gets none: its
  objects were written by the `run` that decided it.
- No object carries a key, a pairing code, the SAS, a room's workspace, an absolute path, or anything an
  operation returned.

### `host_too_old`

| Field | Type | |
|---|---|---|
| `job_id` | string | The request's. |
| `min_host_version` | string, `MAJOR.MINOR.PATCH` | The oldest release the request allows. |
| `host_version` | string | This release's version. |

Lucille asked for a newer release: the request is refused `host_too_old`. It comes right after that
request's `decided` object. A shell tells the user once that a newer release is needed, keeping the highest
`min_host_version` it saw, however many requests say so.

### `stopped`: why `run` ended

| `reason` | Other fields | Exit |
|---|---|---|
| `drained` | `cause`: `signal`, `stdin_end` or `stdout_gone` | 0 |
| `forgotten`, `ended` | | 0 |
| `not_paired`, `no_gate`, `several_pairs`, `pair_unusable`, `leaked`, `expired` | | 20 |
| `already_running` | | 21 |
| `insecure_keyring`, `store_error` | | 23 |
| `startup_refused` | | 24 |
| `downgrade` | `last_version` | 25 |
| `failed` | `code` | 1 |

It is the last object `run` writes. Section 13.3 says what each reason means. `cause` says what began a
drain: a signal (`SIGTERM`, `SIGHUP` or Ctrl-C), the end of standard input, or a standard output nobody reads
(section 13.4). `last_version` is the release recorded on this profile, which this one is older than.
`code` is one of the codes of section 10.

### `credential`: one credential kept on this computer, never its value

| Field | Type | |
|---|---|---|
| `service` | string, `^[a-z0-9][a-z0-9._-]{0,63}$` | The service it is for, such as `gmail`. |
| `name` | string, `^[a-z0-9][a-z0-9._-]{0,63}$` | Its name, such as `default`. |
| `hosts` | array of strings | The hosts it may be sent to (section 15), in the order they were given; at most 16. Empty only for one saved without hosts, which nothing sends. |
| `saved_at` | string (a time) | When it was saved. |

### `credential_removed`

| Field | Type |
|---|---|
| `service` | string |
| `name` | string |

### `credential_test`: a credential that signed in

| Field | Type | |
|---|---|---|
| `service` | string | `gmail`, the one service with a test. |
| `name` | string | |
| `host` | string | The host it signed in to: `imap.gmail.com`. |

### `credential_ask`: one of Lucille's asks for a credential, waiting for the user's answer

| Field | Type | |
|---|---|---|
| `id` | string, 16 digits of `0`-`9` and `a`-`f` | The ask's id, which `credential answer` takes. |
| `service` | `github` or `gmail` | A service the host has a template for (section 15). |
| `title` | string | What a person calls the service: `GitHub`, `Gmail`. |
| `name` | string, as `credential`'s | The credential's name. |
| `hosts` | array of strings, at least 1 | The only hosts the credential will be sent to: the service's template's, never Lucille's. |
| `reason` | string or null | Lucille's own words, at most 300 characters, or null when it sent none. A shell shows them apart from the rest, under "Lucille says:", and never as its own. |
| `replaces` | boolean | A credential of that service and name is saved here now, and saving replaces it. |
| `value_words` | string | What the value is, in the host's words. |
| `make_at` | string, an `https://` URL | The service's page where the user makes one, the narrowest it offers. |
| `asked_at` | string (a time) | When Lucille asked. |
| `until` | string (a time) | When the ask lapses, declined, unless it is answered. |

### `credential_answer`: the user's answer to an ask

| Field | Type | |
|---|---|---|
| `id` | string | The ask's id. |
| `service` | `github` or `gmail` | |
| `name` | string | |
| `answer` | `saved` or `declined` | What Lucille is told, and all it is told. |

### `profile_check`: a folder or file of the profile that is not yours

| Field | Type | In states |
|---|---|---|
| `state` | `ok`, `not_yours`, `outside_home` or `unreadable` | all |
| `config_folder` | string, an absolute path | `ok` only |
| `state_folder` | string, an absolute path | `ok` only |
| `path` | string, an absolute path | all but `ok` |
| `kind` | `folder` or `file` | `not_yours`, `outside_home` |
| `uid` | integer, 0 to 4294967295 | `not_yours`, `outside_home` |
| `look` | string | `not_yours` only |
| `repair` | string | `not_yours` only |

`profile check` writes one of these for each folder or file it finds, by path, or one `ok` when it finds none
(section 17). `not_yours` is inside the home folder and owned by another user (`uid`; root is 0); `look`
and `repair` are command lines for a person to run as root, with the path in single quotes: `look` shows
what is there, and `repair` gives it to the user. A shell shows them, and never runs them. `outside_home`
is the same outside the home folder, where the host suggests no repair. `unreadable` cannot be looked at:
a folder above it refuses the user (above a profile folder, it is written only when no folder above it is
another user's). `ok` names the profile's two folders, as the host resolves them; on macOS the state
folder is inside the config folder.

### `verify`: the running release passed every check

| Field | Type | |
|---|---|---|
| `version` | string, `X.Y.Z` | The release checked: the version of the host that ran the command. |
| `repository` | `gald33/lucille-host` | The repository every attestation's publisher and certificate name. |
| `workflow` | `release.yml` | The workflow every attestation's publisher and certificate name. |
| `environment` | `pypi` | The environment PyPI records for every attestation bundle's publisher. |
| `files` | array of 2 | The release's files on PyPI, the wheel then the sdist: each `{name, sha256, attestations}`, the sha256 of the bytes PyPI serves, and how many attestations of them verified, 1 to 100. |
| `site` | string, an absolute path | The folder the running package was imported from, whose files were compared with the wheel's. |
| `installed` | integer, 1 to 100,000 | How many installed files are the wheel's: every file the wheel holds but its `RECORD`. |
| `bytecode` | integer, 0 to 100,000 | How many bytecode files (`.pyc`) in the package's `__pycache__` folders were counted, and not checked. |

## 7. Verbs

Each verb writes the objects below on success, and an `error` object (section 5) on failure, with one of
the codes listed for it, or one of the codes every verb can give: `usage` (exit 2), `internal`,
`bad_profile`, `unsupported_platform`, `os_error` and, as root, `startup_refused` (section 10; `--version`, `--help` and a
line with no verb never give it).

| Verb | On success | Codes |
|---|---|---|
| `--version`, or no verb | one `version` | |
| `ops list` | one `op` per catalog entry, in catalog order, then one per policy entry the catalog does not have, by name | `policy_invalid` |
| `ops enable <op>` | the `op`, as it is now | `policy_invalid`, `policy_lock_failed`, `not_in_catalog`, `unavailable` |
| `ops disable <op>` | the `op`, as it is now; `op_removed` for a name this release's catalog does not have | `policy_invalid`, `policy_lock_failed`, `not_enabled` |
| `roots list` | one `root` per alias, by alias | `policy_invalid` |
| `roots add [--allow-home-or-root] [--] <alias> <path>` | the `root` added | `policy_invalid`, `policy_lock_failed`, `bad_alias`, `alias_taken`, `too_many_roots`, `not_a_folder`, `own_folder`, `on_proc`, `broad_folder`, `unstorable_path` |
| `roots add --suggest-alias <path>` | one `alias_suggestion` (section 9) | `policy_invalid` |
| `roots remove <alias>` | one `root_removed` | `policy_invalid`, `policy_lock_failed`, `no_such_alias` |
| `roots check [<alias>]` | one `root_check` per alias, by alias, or for the alias named | `policy_invalid`, `no_such_alias` |
| `pause`, `resume`, `kill` | one `controls` | |
| `status` | one `status` | `startup_refused`, `runtime_folder`, `state_invalid` |
| `log [--limit N]` | one `log_line` per line, oldest first; none when the log is empty | `bad_limit` |
| `forget` | one `forget` | `none`, `several_pairs`, `room_not_left`, `pair_unusable`, `startup_refused`, `insecure_keyring`, `store_error`, `runtime_folder`, `state_invalid` |
| `pair status` | one `pair`: `none`, `waiting`, `sas_pending` or `confirmed`; or, with no pair and a Connect request, one `connect`: `code` or `delivered` | `interference`, `connect_expired`, `newer_capsule`, `key_misused`, `hub_error`, `bad_options`, `several_pairs`, `leaked`, `expired`, `ended`, `none`, `no_gate`, `pair_unusable`, `room_not_left`, `startup_refused`, `insecure_keyring`, `store_error`, `runtime_folder`, `state_invalid` |
| `pair begin [--hub URL [--test-key KEY]] [--store-file]`, or `pair` with no action | `connect` `code`; once Lucille's answer has passed, `connect` `connecting`, `pair_code`, `policy_reset` if a policy was set aside, `pair` `accepted`, `pair` `waiting`, `pair` `sas_pending` | `code_in_argv`, `bad_options`, `bad_hub`, `startup_refused`, `busy`, `lock_failed`, `held`, `bad_service`, `bad_username`, `hub_error`, `interference`, `connect_expired`, `newer_capsule`, `key_misused`, `other_account`, `not_this_mac`, `bad_code`, `wrong_hub`, `code_has_token`, `policy_lock_failed`, `answer_failed`, `leaked`, `expired`, `ended`, `none`, `no_gate`, `interrupted`, `pair_unusable`, `room_not_left`, `insecure_keyring`, `store_error`, `runtime_folder`, `state_invalid` |
| `pair resume` | as `pair begin`, on the request it kept | `bad_options`, `none`, and those of `pair begin` but `code_in_argv`, `bad_hub`, `bad_service`, `bad_username` |
| `pair cancel` | one `connect` `cancelled` | `bad_options`, `none`, `busy`, `lock_failed`, `startup_refused`, `runtime_folder`, `state_invalid` |
| `pair confirm` | one `pair` `confirmed` | `bad_options`, `none`, `several_pairs`, `waiting`, `sas_ready`, `no_gate`, `expired`, `leaked`, `ended`, `pair_unusable`, `room_not_left`, `startup_refused`, `insecure_keyring`, `store_error`, `runtime_folder`, `state_invalid` |
| `pair reject` | one `pair` `rejected` | `bad_options`, `none`, `several_pairs`, `already_confirmed`, `pair_unusable`, `room_not_left`, `startup_refused`, `insecure_keyring`, `store_error`, `runtime_folder`, `state_invalid` |
| `policy previous` | one `previous_policy` | `previous_invalid` |
| `policy reset` | one `policy_reset` | `policy_lock_failed`, `not_invalid` |
| `profile check` | one `profile_check` per folder or file found, or one `ok` (section 17) | |
| `run --json-events [--exit-on-stdin-eof] [--allow-downgrade]` | `ready`; `state`, `request` and `host_too_old` as they come; `stopped`, last (section 13) | `usage` |
| `credential set <service> <name> --host <host> [--host <host> ...] --value-stdin [--replace]` | the `credential` saved | `credential_exists`, `bad_credential`, `no_value`, `bad_value`, `interrupted`, `insecure_keyring`, `store_error` |
| `credential list` | one `credential` per credential, by service then name; none when none is saved | `insecure_keyring`, `store_error` |
| `credential remove <service> <name>` | one `credential_removed` | `no_such_credential`, `bad_credential`, `insecure_keyring`, `store_error` |
| `credential test <service> <name>` | one `credential_test` | `no_test`, `no_such_credential`, `paused`, `bad_credential`, `unreachable`, `limit_reached`, `sign_in_refused`, `insecure_keyring`, `store_error` |
| `credential asks` | one `credential_ask` per ask waiting for an answer, oldest first; none when none waits | `insecure_keyring`, `store_error` |
| `credential answer <id> --decline`, or `--value-stdin` | one `credential_answer` | `no_such_ask`, `no_value`, `bad_value`, `values_differ`, `interrupted`, `insecure_keyring`, `store_error` |
| `verify` | one `verify` | `not_a_release`, `no_verifier`, `unreachable`, `yanked`, `not_attested`, `files_differ` |

Notes:
- **`pair`** takes no pairing code. `pair` alone is `pair begin`; a word that is not an action (`begin`,
  `resume`, `cancel`, `status`, `confirm`, `reject`) is refused (`code_in_argv`), and is not repeated. There
  is no `--code-stdin` (a usage error). With `--json`, `pair begin` never asks anything, and stops once the
  SAS is shown (`sas_pending`, exit 0). Once it has answered the code, it
  resolves the pair every 5 s, and at once when a note of the pairing library arrives, sending an unsent
  answer again, until Lucille has collected the answer. It may write `waiting` more than once; a reader
  takes the latest. A pairing that ends early writes an `error` with `leaked` (the code leaked: start again
  with a fresh code, and share it with nobody), `expired` (it did not finish in time: start again), `ended`
  or `none`, and exits 1. `pair` refuses while a pair is held (`held`: disconnect first) and while another
  `pair` of the profile runs (`busy`). `--hub` and `--store-file` are for testing against a local hub, and
  for a computer with no keyring; both are saved for the profile. With `--store-file`, a keyring the host
  refuses (`insecure_keyring`) is never read, and the pair is kept in the file store. So is a keyring whose
  Secret Service cannot be reached or started at the first read (one the session bus lists and that fails
  to start, or whose program does not exist, or a bus that refuses the connection, or one whose service the
  bus gives up on with `org.freedesktop.DBus.Error.NoReply`), whether the Secret Service's own libraries
  say so or keyring does, from them: the host then warns on stderr that the keyring could not be read,
  naming the error's class and D-Bus error name, and that the pair is kept in a file instead. It does not
  fall back on any other `store_error`: a read that did not answer within the host's own 60 s deadline (a
  `NoReply` the bus itself sends, after its own reply timeout, which can come sooner, is not that: it falls
  back, as above), a locked keyring, an unlock prompt the user dismissed, or any other error of a Secret
  Service that runs; nor once that process has tried to write the pair to the keyring, which could leave one
  pair there and another in the file.
- **`pair status`** resolves the pair once, sending an unsent answer again. A shell that was relaunched
  learns where the pair is from `status`, and calls `pair status` once its pairing screen is up; it calls it
  every 5 s while it reports `waiting`. `status` cannot tell its `waiting` from a pair whose answer Lucille
  has collected since a command last asked the hub, so a shell that finds `waiting` there shows its pairing
  screen too. `pair status` writes `waiting` also when the hub could not be reached, and this computer holds
  no SAS: whether Lucille has collected the answer is then not known, its text says so, and the object does
  not (section 13). The next call 5 s later asks again.
- **`pair begin`** starts a Connect request (decisions/0002 §11.4). Standard input is two lines: the chat
  service (`telegram`), then the user's own username there, in any spelling the host takes (`ada`, `@Ada`,
  `telegram:@ada`). It may exit before it reads anything (`code_in_argv`, `bad_hub`, `busy`, `held`, ...),
  so a shell writes both lines at once, closes standard input, and ignores a broken pipe. It draws a connect
  code, posts the host's hello where Lucille will look, keeps the request in the state folder, writes
  `connect` `code`, and reads Lucille's answer every 5 s. Without `--json`, it asks at the SAS for `y` or
  `n`; the end of standard input leaves the answer to `pair confirm` or `pair reject`. A hub that cannot be
  reached when it posts the hello gives `hub_error`, and no request is kept. One request is kept at a time:
  a new `begin` replaces it. Ctrl-C gives `interrupted` and keeps the request, which `pair resume` takes
  up again. The request ends, and is spent, with one of these codes:
  - `interference`: Capsule could not read Lucille's answer, and the code may be known to someone else.
    It is given for a read of the room that fails other than at the hub, for a room that lost the host's
    hello or holds a second one, or 50 posts or more, and, at the expiry, when the last read failed at the
    hub or an answer was read and not yet answered;
  - `connect_expired`: the 20 minutes passed;
  - `newer_capsule`: the 20 minutes passed, and the only answers were signed with a key this release does
    not know;
  - `key_misused`: two answers passed, which Lucille never sends;
  - `other_account`: the answer names another account than the one typed, judged before the expiry. No
    screen names that account;
  - `not_this_mac`: the answer's code names another request, or carries no hello of the pairing library,
    or the request was used already;
  - `bad_code`, `wrong_hub` or `code_has_token`: the answer's code is not one the host answers.
- **`--test-key`** is for the end-to-end tests and the conformance suite, which play Lucille against a local
  hub and sign with a key of their own: 32 bytes in unpadded base64url. `pair begin` takes it only with
  `--hub`, and saves it for the profile, as it saves `--hub`. An answer to a request on that loopback hub may
  then be signed with it, as well as with the release's own keys; a request on Lucille's hub takes the
  release's keys only. The app never passes it. Any other use is `bad_options`. A key is base64url, so it
  may start with `-`: a harness writes it joined to the flag, `--test-key=KEY`, as it writes a path that
  starts with `-` to `--suggest-alias`.
- **`pair resume`** takes up the request `pair begin` kept, as `begin` would have gone on. A shell that was
  relaunched calls `pair status`, then `pair resume` if it reports a `connect`.
- **`pair cancel`** drops the request. It refuses `busy` while `begin` or `resume` waits on it: the shell
  stops that first.
- **`pair status`**, with no pair held and a Connect request kept, reads Lucille's room once and reports the
  request: `code`, or `delivered` when Lucille's answer has arrived. It records what it saw, but answers
  nothing, posts nothing and spends nothing: a request that has ended is reported with its code, and left
  for `pair resume` to spend. While `begin` or `resume` waits, it reports from the state folder, with no
  read.
- **`pair confirm`** is the user's "yes, Lucille's page shows the same SAS". `pair reject` is "no".
- **`ops enable`** and **`ops disable`** write the operation's `op` object as it is once the change is made,
  so a shell sets its switch from the host's answer, not from what it asked for.
- **`roots add`** refuses a folder that is the home folder, holds it, or is `/`, unless
  `--allow-home-or-root` is given (`broad_folder`, with `kind`). A shell asks the user before it passes that
  flag. A shell passes `--` before the alias and the path, so that neither is read as an option.
- **`roots add --suggest-alias <path>`** takes no alias, no folder argument and no other option. A path that
  starts with `-` is written `--suggest-alias=<path>`. It writes nothing but its line in the run record
  (section 16), and takes no lock: another verb may
  take the alias before `roots add` runs, which then refuses with `alias_taken`, and the shell asks again.
- **`roots check`** exits 0 whatever it found: a folder that cannot be opened is a result, not a failure.
  With no folder in the policy it writes no object and exits 0; empty standard output then means there is
  no folder to check, not that the verb failed.
- **`forget`** with no pair held, and no work room left over, refuses with `none`: there is nothing to
  forget. A shell disconnecting takes that as done.
- **`log`** writes no object, and exits 0, when the audit log has no lines yet; standard error says so.
- **`policy reset`** sets aside a policy file that is invalid, and only such a file. With a valid file, or
  none, it changes nothing but its line in the run record (section 16), and refuses `not_invalid`; a shell
  resetting takes that as done. It never touches
  the policy a pairing set aside.
- **`credential` with `--json`** never asks anything: `credential set --json` reads the value from
  standard input, and only with `--value-stdin` (without it, a usage error). No object holds a value.
  `credential list` writes no object, and exits 0, when none is saved; standard error says so.
  `credential test` exits 1 with an `error` whose `message` is the child's own fixed line (section 15).
- **`run --json-events`** writes no `error` object but for a usage error. Every other way it ends is a
  `stopped` object and an exit code of section 13.3.
- **`verify`** checks the release whose code is running, as
  [decisions/0001-host-design.md](../decisions/0001-host-design.md) ("Packaging, releases and
  verification") says, and stops at the first check that fails: the host is a release, not a
  development build (`not_a_release`); `pypi-attestations`, which the `[verify]` extra installs, can be
  imported (`no_verifier`); PyPI lists exactly the release's wheel and sdist (`not_attested`), and marks
  neither, nor the release, yanked (`yanked`); the bytes PyPI serves for each have the sha256 it lists,
  and each is attested by repository `gald33/lucille-host`, workflow `release.yml` and environment
  `pypi`, at tag `v<version>`, from a GitHub-hosted runner, as sigstore verifies (`not_attested`); and
  every file the wheel holds but its `RECORD` is installed in the folder the package was imported from,
  with the same sha256, and the package folder holds no other file but bytecode in `__pycache__`
  (`files_differ`). PyPI or sigstore's trust root that cannot be reached, or that answers with anything
  but what was asked, gives `unreachable`; nothing was verified, and the shell may ask again. It asks
  `pypi.org`, `files.pythonhosted.org` and sigstore's trust root, and nothing else; it reads no file of
  the profile, takes no lock, and writes nothing but its line in the run record (section 16) and
  sigstore's cache of its trust root. Its text is one line for the release, one per file, and one for the
  installed files. It can take a minute or more: a shell runs it with a longer time limit than the other
  verbs.

### The SAS in JSON

This release's pairing gate is the one in
[decisions/0001-host-design.md](../decisions/0001-host-design.md), "Pairing": the computer shows the SAS,
the user compares it with Lucille's page, and confirms on the computer. So `sas_pending` carries the SAS
(`sas`) and the deadline, from `pair begin --json` and from `pair status --json`, as their text does.

Writing the SAS in JSON counts as showing it: the first time either verb writes it, the deadline starts, as
when the text shows it. The program that reads it shows it to the user, and the user's `y` (`pair confirm`)
must come after that (section 2). `lucille-host status` never reports the SAS, in JSON too: its `pair`
object has no field that could hold it, and it starts no deadline.

[decisions/0002-macos-app.md](../decisions/0002-macos-app.md) §11.2 records a gate, decided on 2026-11-21
and not built, in which the computer shows its SAS with the last four digits hidden. The first entry is
those four digits, once, from Lucille's number; every entry after a wrong one is all eight digits; three wrong
entries in all forget the pair. When it is built, `sas_pending`
carries the first four digits only, in place of its `sas` field, `pair status` shows those four until a wrong
entry and none after it, and that is a new major version of this contract (section 12).

## 8. The catalog's words

Each catalog entry carries four things for shells beside its definition, in `src/lucille_host/ops/`:
`summary`, `sends`, `sends_file_content` and `diagnostic`. `summary`, `sends` and `diagnostic` are outside
the entry's definition hash and outside `CATALOG_VERSION`: they describe what the entry does, and decide
none of it. So a release that only changes them turns no operation off, and leaves the catalog version as
it was. `sends_file_content` is part of the definition hash when it is true, so a release that makes an
entry send file contents turns that operation off until the user enables it again, and with it sees the
acknowledgement a shell asks for. Their words must not make any claim on [PRIVACY.md](../PRIVACY.md)'s
list of what this project must never claim; a test checks them against it.

| Operation | `summary` | `sends` | `sends_file_content` | `diagnostic` |
|---|---|---|---|---|
| `host.describe` | Describe this computer's host | Lucille gets the host's version and protocol, the operating system family, which release's catalog it has, which operations are on, the names you gave your folders (never where they are), and a fingerprint of the local policy. | false | false |
| `host.sleep` | Wait up to 4 minutes, for testing | Lucille gets how long the wait took and how it ended. | false | true |
| `files.sha256` | Check a file's fingerprint | Lucille gets a code that identifies the file's exact contents, not the contents. | false | false |
| `files.search` | Search the files in a folder | Lucille gets the lines that match its search in the files of one folder you added, with each file's path inside that folder, up to 200 lines a file. Hidden files are never searched. | true | false |
| `github.read` | Read from GitHub with a token kept here | Lucille gets GitHub's reply to one read it names: your repositories, issues, pull requests, commits or a README. The token goes only to api.github.com; Lucille learns only its name. | false | false |
| `gmail.check` | Check that Gmail can be read | Lucille learns whether the app password kept here still signs in to Gmail. | false | false |
| `gmail.mailboxes` | List your Gmail mailboxes | Lucille gets the names of your Gmail mailboxes and labels. | false | false |
| `gmail.search` | Search your Gmail | Lucille gets the sender, recipients, subject, date and size of up to 50 messages that match its search, never their text. The app password goes only to imap.gmail.com. | false | false |
| `gmail.read` | Read one Gmail message | Lucille gets one message it names: its headers, up to 32 KB of its text, and its attachments' names, types and sizes, never their contents. | false | false |
| `credential.ask` | Ask you to save a credential here | Lucille learns only whether you saved the credential it asked for or declined. You type it on this computer; it is never sent to Lucille. | false | false |

## 9. Folder aliases and folder checks

### 9.1 `roots add --suggest-alias`

It suggests an alias made from the folder's name, which `roots add` will take, and which no folder of the
local policy has. It reads the policy and nothing else, and writes nothing but its line in the run record
(section 16).

1. The name is the last part of the folder's absolute path (`""` for `/`).
2. Each character becomes:
   - an ASCII letter: itself, lowercase; an ASCII digit, `-` or `_`: itself;
   - a full-width form (U+FF01 to U+FF5E): the ASCII character it stands for, then as above;
   - a Latin letter with a mark, or a Latin ligature, from U+00C0 to U+017F: its plain letters, lowercase
     (`é` is `e`, `ß` is `ss`, `æ` is `ae`, `œ` is `oe`, `ø` is `o`, `þ` is `th`, `ł` is `l`);
   - a combining mark (U+0300 to U+036F), which a name in decomposed form carries after its letter:
     nothing. So a letter up to U+017F gives the same decomposed as composed (`e` then U+0301 is `e`, as
     `é` is). A Latin letter past U+017F does not: composed, it is `-`, as below; decomposed, its marks
     are dropped and its plain letter stays (`Brașov` gives `bra-ov` composed and `brasov` decomposed);
   - anything else, letters of other scripts and Latin letters past U+017F (`ș`, say) included: `-`.
3. Each run of `-` becomes one `-`. Every `-` and `_` at either end is removed.
4. An empty result becomes `folder`. A result that starts with a digit gets `folder-` in front.
5. It is cut to 32 characters, and every `-` and `_` at its end is removed again.
6. If a folder of the policy already has that alias, `-2` is added, or `-3`, and so on, to the first that is
   free: the alias is cut first so that the whole stays within 32 characters, and every `-` and `_` at the
   end of what is left is removed, then the suffix is added.

`Documents` gives `documents`, `2026 Taxes` gives `folder-2026-taxes`, `Café Notes` gives `cafe-notes`, and
a second `Taxes` gives `taxes-2`. A name in another script, such as `Документы`, gives `folder`. A shell may
offer the user an alias of its own instead (one written in Latin letters, say); `roots add` checks it as it
checks any alias.

### 9.2 `roots check`

For each folder, it opens it and reads one entry, as an operation working there would need to. It sends
nothing to Lucille or the hub, and writes nothing but its line in the run record (section 16).

| State | When |
|---|---|
| `ok` | The folder opened, and one entry could be read (an empty folder is `ok`). |
| `denied_by_macos` | On macOS, `EPERM`: macOS's privacy protection refuses the host this folder. |
| `denied` | `EACCES`, or `EPERM` on Linux: the folder's permissions refuse it. |
| `missing` | `ENOENT` or `ENOTDIR`: nothing is there, or not a folder. |
| `error` | Any other error; `errno` names it (`EIO`, `ELOOP`, `ESTALE`, ...). |

It checks one folder after another, so a stalled mount holds back the folders after it. Before it checks
any, it loads the local policy, and loading the policy looks at each folder (section 2). So a stalled mount
anywhere in the policy holds back every `roots check`, whether or not it names an alias: running
`roots check <alias>` once per folder, side by side, does not get around it. A shell runs it with a time
limit, as it runs every verb, and stops it when the limit passes.

## 10. Error codes

| Code | What failed |
|---|---|
| `usage` | The command line could not be parsed, or names a form version 1 does not have (exit 2). |
| `internal` | An unexpected error; the traceback is on standard error. |
| `bad_profile` | A `--profile` name is lowercase letters, digits, `-` and `_`, starting with a letter, at most 32. |
| `unsupported_platform` | This is not Linux or macOS. |
| `os_error` | A file the host needed could not be read or written; the message names it. |
| `startup_refused` | The host does not start here: it runs as root (every verb but `--version`, `--help` and a command with no verb), or `SWITCHBOARD_*` variables are set. |
| `insecure_keyring` | The keyring backend the host keeps the pair in cannot be used here, and the message says it has no keyring to store in: keyring's Secret Service backend on Linux (no session bus, or no Secret Service on it), its Keychain backend on macOS. The host builds that backend itself and takes no other, so a Linux desktop whose only keyring is KWallet, with no Secret Service on the session bus, now sees that it has no keyring to store in, and one that runs both KWallet and a Secret Service gets the Secret Service. Or the session bus did not answer the Secret Service's check within 5 s (it took the connection and never replied), and the message says it has no keyring to store in: the session bus did not answer within 5 s; the check is not asked again in that process. Or keyring's own off switch is set, and the message says the keyring backend stores nothing: `PYTHON_KEYRING_BACKEND`, or the `default-keyring` of keyring's `keyringrc.cfg`, names keyring's null backend, `keyring.backends.null.Keyring` (what `keyring --disable` writes), or its fail backend, `keyring.backends.fail.Keyring`. Both messages point to `--store-file`. Any other value of either is not consulted, and nothing either names is imported. |
| `store_error` | The store that keeps the pair could not be used. On Linux that includes a read or a write of the keyring item that the Secret Service did not answer within 60 s, and the message says so: the keyring did not answer a read (or a write) within 60 s. For a write it also says that whether it was saved is not known: it may still land before the process exits, so the host removes or rewrites none of its own files on the assumption that it failed, and the next start reads the item again. That process then asks the keyring nothing more, and every later read and write in it fails at once, with a message that says to try again in a new process. It also includes a read or a write that the Secret Service's own libraries fail, past keyring: a service the session bus lists as one it can start, which then fails to start, or a bus that went away. The message names the error's class and, for a D-Bus error, its name, and below a keyring error the Secret Service error it was raised from, for example: the keyring could not be read (DBusErrorResponse: org.freedesktop.DBus.Error.Spawn.ChildExited), or (InitError, from DBusErrorResponse: org.freedesktop.DBus.Error.Spawn.ExecFailed). |
| `runtime_folder` | The host's private runtime folder is missing, or is not private. |
| `state_invalid` | A file of the host's own holds something else. |
| `policy_invalid` | The local policy file is invalid. Nothing changes it but a verb. |
| `policy_lock_failed` | The policy lock could not be taken on this filesystem, so the policy was not changed. |
| `not_invalid` | The local policy is valid, or there is no file: `policy reset` sets aside only an invalid file. |
| `not_in_catalog` | That operation is not in this release's catalog. |
| `unavailable` | That operation cannot run here: its executable is missing. |
| `not_enabled` | That operation is not enabled. |
| `bad_alias` | An alias is lowercase letters, digits, `-` and `_`, starting with a letter, at most 32. |
| `alias_taken` | A folder already has that alias. |
| `too_many_roots` | The policy holds at most 64 folder aliases. |
| `not_a_folder` | That path is not a folder. |
| `own_folder` | That folder is where lucille-host keeps its own files. |
| `on_proc` | That folder is on `/proc`. |
| `broad_folder` | That folder is the home folder, holds it, or is `/`, and `--allow-home-or-root` was not given. `kind` says which. |
| `unstorable_path` | That path cannot be stored in the policy (its name is not valid UTF-8). |
| `no_such_alias` | No folder has that alias. |
| `previous_invalid` | The policy a pairing set aside is invalid, or the time it was set aside (its file's modification time) is not from 1970 to the end of 9999. |
| `bad_limit` | `--limit` takes 1 or more. |
| `bad_options` | `pair status`, `pair confirm` and `pair reject` take no options. |
| `code_in_argv` | `pair` was given something other than an action; a pairing code is never taken. |
| `bad_hub` | `--hub` names a hub that is not `http://127.0.0.1:<port>` or `http://localhost:<port>`. |
| `busy` | Another `pair` of this profile is running. |
| `lock_failed` | The lock that keeps one `pair` per profile could not be taken on this filesystem. |
| `held` | This computer already holds a pair: disconnect first. |
| `bad_code` | That is not a pairing code. |
| `wrong_hub` | The code is for another hub, not Lucille's (or for a loopback hub `--hub` did not name). |
| `code_has_token` | The code carries a hub token, and the host takes only codes that do not. |
| `no_gate` | The pair has no local gate record, so it can never be confirmed here: `pair reject`, then start again. |
| `already_confirmed` | `pair reject` of a pair that is confirmed: `forget` ends it. |
| `answer_failed` | The pairing library refused to answer the code. Nothing was answered. |
| `hub_error` | The hub could not be reached, or answered with an error. Nothing was decided; the same command can be tried again, with the same code. |
| `leaked` | The pairing code leaked: another computer answered it, or somebody interfered. Start again with a fresh code. |
| `expired` | The pairing, or the SAS, was not finished in time. The pair is forgotten. Start again. |
| `ended` | The pair was ended, by Lucille or on this computer. |
| `none` | This computer holds no pair, or another command forgot it meanwhile. |
| `waiting` | No resolve has seen Lucille collect this computer's answer, so there is no SAS to confirm: Lucille has not collected it, or the hub could not be reached to ask. The message says which. |
| `sas_ready` | This computer has not shown the SAS yet: `pair status` shows it. |
| `interrupted` | Ctrl-C while `pair` waited. The pair is kept; `pair status` resumes. Or Ctrl-C, or the end of standard input, while `credential set` or `credential answer` asked for a value: nothing was stored. |
| `pair_unusable` | The pairing library refused the pair here, or the store holds a SAS for it that is not four digits, `-`, four digits. |
| `room_not_left` | A work room could not be left, so its keys would stay behind; the command can be tried again, or `forget` leaves it. |
| `several_pairs` | The profile holds more than one pair, and the host keeps one. |
| `bad_credential` | `credential`: a service or credential name, a host, or a value the store does not take (section 15). Nothing was stored. |
| `credential_exists` | `credential set`: a credential is stored under that service and name already, and `--replace` was not given. Nothing changed. |
| `no_such_credential` | `credential remove`: no credential is stored under that service and name. |
| `no_value` | `credential set`, `credential answer`: no value was given, or standard input is of the wrong kind to give one: not a terminal without `--value-stdin`, or a terminal with it. Nothing was stored. |
| `values_differ` | `credential set`, or `credential answer` at a terminal: the value typed twice differed. Nothing was stored, and an ask still waits. |
| `bad_value` | `credential set`, `credential answer`: the value holds a control character, or is not text in standard input's encoding (a line that does not decode, or one holding a lone surrogate); for `credential answer`, also a value not in the form its service's template says. Nothing was stored. |
| `no_test` | `credential test`: the service has no test; only `gmail` has one. Nothing was read. |
| `paused` | `credential test`: the host is paused, and so starts nothing. Nothing was read. |
| `sign_in_refused` | `credential test`: Gmail refused the app password (wrong or revoked), or did within the last 15 minutes and was not asked again. |
| `no_such_ask` | `credential answer`: no ask with that id waits for an answer: none was made, it was answered, or it lapsed. Or it lapsed while the value was typed, and nothing was saved; or just after the credential was saved, which the message says, and Lucille was told it was declined. |
| `unreachable` | `credential test`: `imap.gmail.com` did not resolve, resolved to an address the host does not allow, could not be connected to, or the connection broke. `verify`: PyPI or sigstore's trust root could not be reached, the connection broke, or PyPI answered with a status but 200 and 404, more than 64 MiB, or a listing or provenance that is not JSON; nothing was verified. |
| `limit_reached` | `credential test`: the credential was used for more than 60 sign-ins in 10 minutes, or Gmail sent more than the host reads. |
| `bad_service` | `pair begin`: that is not a chat service Capsule connects through (`telegram`). A pairing code given there, as the paste form took one, is refused with it too, saying that pasting a code is no longer supported. |
| `bad_username` | `pair begin`: that is not a username: it starts with a letter, and holds letters, digits and `_` only, 64 at most. |
| `interference` | Capsule couldn't read Lucille's answer, and the code may be known to someone else. Start again. |
| `connect_expired` | The connect code expired. Start again. |
| `newer_capsule` | The connect code expired, and Lucille's answers were signed with a key this release does not know: Lucille may need a newer Capsule. |
| `key_misused` | Lucille's answer came twice, which Lucille never does. Nothing was connected. |
| `other_account` | The connect code was sent to Lucille from a different account than the one typed. Nothing was connected. |
| `not_this_mac` | Lucille's answer is for a request this computer did not make (its code names another request, or carries no hello of the pairing library), or for one that was used already. Nothing was connected. |
| `not_a_release` | `verify`: this host is a development build, not a final release `X.Y.Z`, so PyPI holds nothing to check it against. Nothing was fetched. |
| `no_verifier` | `verify`: `pypi-attestations` is not installed; the `[verify]` extra installs it. Nothing was fetched. |
| `yanked` | `verify`: PyPI marks the release, or one of its files, yanked: its maintainers withdrew it. |
| `not_attested` | `verify`: PyPI holds no such release, lists other files than its wheel and sdist, serves bytes that are not the sha256 it lists, or holds no provenance for a file; or an attestation names another publisher, does not verify, or its certificate names another repository, tag or runner. The message says which file and which check. |
| `files_differ` | `verify`: an installed file of the release is missing, cannot be read or is not the wheel's, or the package folder holds a file the release does not ship. The message names up to five. |

**As root.** Every verb but `--version`, `--help` and a command with no verb (which shows the version) is
refused when the effective user id is 0: first thing after the command line parses, before the profile's
paths are resolved, so a profile name that is not valid is not looked at either. The refused command
creates nothing: no folder, no lock file, no run record. It writes the line `lucille-host: <message>` on
standard error and exits 1 (24 for `run`); `--json` writes one `error` object with the code
`startup_refused` and that message, and `run --json-events` writes one `stopped` object with the reason
`startup_refused`. The message says the host does not run as root (uid 0), and points to `profile check`
(section 17), run as the user, for a profile an earlier run as root left unreadable. A command line that
does not parse is a `usage` error first, as for any user. The library's own refusal
(`lucille_host.library.start`) is unchanged.

**A profile that refuses the user.** When a verb other than `run` fails because the permissions on one of
the profile's folders, a folder above one, or a file inside one refuse it, its message ends with a pointer
to `profile check`: "if an earlier run as root left it so, `lucille-host profile check` names what to
repair". The code is the one the failure has without it (`os_error`, `policy_invalid`, `state_invalid`).

## 11. Golden fixtures

Each fixture is one command line and what it writes:

```json
{
  "description": "ops enable, with --json after the action",
  "argv": ["ops", "enable", "host.sleep", "--json"],
  "exit": 0,
  "stdout": [
    {"v": 1, "type": "op", "name": "host.sleep", "state": "enabled", "enabled": true, "...": "..."}
  ]
}
```

- `argv` is what follows `lucille-host`. `stdout` is the objects in order. Each fixture's `description`
  says what was set up before the command ran.
- Paths in a fixture are under `/home/me`, as Linux's folders name them; the version is a sample, and so is
  each pair name, SAS, time, generation, connect code and request nonce in a code's note. The tests put the
  real values in their place before they compare: the profile's folders, the home folder, the version, the
  pair's name, Lucille's SAS, a time, the connect code, the nonce, and the executable this computer's family
  names for an operation. A run record's pid and skew are set by
  the test.
- Every object in every fixture validates against its type's schema, and the tests run every fixture's
  command line and compare. They also run it without `--json`, from the same set-up, and check that the
  exit code and standard error are the same.
- The `verify` fixtures are written with a release, `1.2.3`, installed in a folder under the home, and
  PyPI and sigstore stood in for. Each `sha256` of a `verify` object is a sample: the stand-in wheel's
  bytes depend on how each Python writes a zip file. `verify-dev-build` stubs the running host's
  version to a development build's.
- `ops-list-sends-file-content` is written with a catalog entry stubbed to send file contents, so that a
  shell's decoder sees `sends_file_content` true on an entry whose words it knows.
- Every `stopped` reason has a fixture of `run`, except `forgotten`. It comes only while `run` serves the
  pair, when another command forgets it, or when a touch of `run`'s, coming after a resolve, finds that this
  computer's side ended the pair (section 13.3): at a moment one command line does not fix. A pair forgotten
  before `run` starts leaves none to serve, and one this computer's side ended before `run` starts is found
  by the first resolve of `run`'s that reads the pair room, which says the pair ended (if the hub turns
  `run`'s first resolve away, a touch can find it first, as above). The tests make it another way.
- `run-events-leaked` is written with the pairing library's answer at `run`'s first hub call stubbed to say
  the handshake was interfered with (`aborted`), which a set-up cannot do on the hub.
- A fixture of `run` has `--json-events` in its `argv`, and its last object is `stopped`, whose reason gives
  the exit code. `run` writes a `state` object when what it sees changes, at moments the test does not set,
  so the tests check each `state` against its schema and compare the other objects, in order. The sizes and
  the duration a `request` object reports are samples too. A fixture's set-up says how standard input ends:
  "closed at once" is a standard input with no file descriptor behind it. The same line without
  `--json-events` must give the same exit code and standard error.

## 12. Versioning

- `v` names the major version of this contract, independent of the host's version and of the protocol's.
  `version`'s `contract` names the minor too.
- **Within version 1**, a later release may add a type, add a field to a type (to one of its states), add
  an `error` code, add a verb, an action or a flag, or add an event to `run`'s stream. Each of these is a
  minor version. The schemas of each release describe exactly what that release writes. A reader ignores a
  type and a field it does not know.
- **Closed and open values.** Every `state` is closed: a new state is a new major version. So are `store`,
  `policy.disabled[].why`, `root_check`'s `state`, `broad_folder`'s `kind`, `status.pair`'s `code`,
  `request`'s `decision` and `status`, the protocol's refusal codes (closed in its version 1) and the codes
  of a `failed` request, and `state`'s `rooms[].state` and `clock`'s `state`. These are open, and may gain
  values within version 1:
  - `error`'s `code`: a reader treats one it does not know as a failure: the exit code says so, and it
    shows the `message`.
  - `work_rooms[].why`: a reader treats one it does not know as "abandoned for another reason".
  - `last_left_because`, in `status` and in `state`: a reader treats one it does not know as "left by the
    host on its own".
  - `stopped`'s `reason`: a reader that does not know it goes by the exit code; `stopped`'s `code`, as
    `error`'s; and `drained`'s `cause`, which a reader that does not know it treats as a drain it did not
    ask for.
  - `request`'s `phase`: a reader ignores a `request` object whose phase it does not know.
  - `state`'s `policy`: a reader treats a value it does not know as `invalid`.
- A field a reader must act on for safety is never added within version 1.
- **Anything else is a new major version:** removing or renaming a type or a field, changing a field's JSON
  type or what it means, or adding a value to a closed value. A new major gets its own schemas, under
  `cli/v2/`.
- Changes are recorded at the end of this document, by contract version, with the host release that first
  writes them.

## 13. Supervising `run`

### 13.1 What a shell starts

```
lucille-host --profile <name> run --json-events --exit-on-stdin-eof
```

- Standard input: a pipe whose write end the shell holds, close-on-exec, gives to no other process, and never
  writes to. `run` reads and discards what is written, and its end comes only once every holder has closed
  the write end.
- Standard output and standard error: pipes the shell reads, each on a thread or queue that does nothing
  else, until the process has exited, during a quit and a logout too.
- A shell starts `run` only when the computer is paired and its gate is open (`status`: `pair`
  `confirmed`). A `run` started before that does not stop at start: it writes `ready` and `state`
  (`confirmed` false), takes no room and runs nothing, and goes on once `pair confirm` opens the gate.
  Meanwhile it stops with 20 only as `expired` once the SAS deadline passes, as `leaked` if the pairing ends
  early, or as `pair_unusable` if the pairing library refuses the pair (section 13.3).
- `run` writes its events from a queue of its own, so a shell that reads slowly holds up no thread of `run`:
  nothing waits for it, a drain and a `SIGTERM` included. A shell that stops reading altogether lets 1000
  objects wait, and then counts as a standard output nobody reads (section 13.4). Standard error is written
  as it comes: a shell reads it continuously.
- Only at its very end does `run` wait for the shell: once it has queued `stopped`, it waits up to 5 s for
  the objects still queued, `stopped` last, to be written, and then exits with the code it chose, whatever
  is still unwritten. A shell that reads its standard output continuously, as above, gets them all.

### 13.2 The events

- One object per line, as section 4.1 says, each at most 16384 bytes.
- `ready` is first, or absent: a refusal at start writes `stopped` alone. `stopped` is last, or absent: a
  signal `run` does not handle, a kill, or a standard output that broke writes nothing more, and objects not
  yet written to standard output within 5 s of `run`'s end are lost: a shell that stopped reading, so that
  the pipe is full, does not get them (13.1). One written in time stays in the pipe for the shell to read,
  after the process has exited too. Nothing follows `stopped`.
- Between them, `state`, `request` and `host_too_old` come in any order. A request's own objects are in
  order: `received`, `decided`, then `host_too_old` for that refusal, `started` for an operation that starts
  a process, and `finished`. The first `state` comes before any `request`. A `request` with no `finished`
  before `stopped`, or before the process exits, ended there: a shell closes it as stopped.
- `state` comes right after `ready` (unless `run` stops at once, section 6), before `run` first calls the
  hub, again whenever what it says changes (a room read or left, the gate, the pause flag, the policy, the
  clock's state, an offer), and at least every 60 s whatever changed. A hub that takes a connection and
  never answers holds `run` in each call until the call is given up, after 40 s, and a step can make
  several: meanwhile `state` still comes every 60 s, saying what `run` saw last, for up to 300 s of one
  step. So no `state` for 120 s means `run` is not stepping, and the shell treats it as stuck. The hub is
  out of reach when `last_hub_contact` is more than 120 s older than the shell's own clock (the same
  computer's): `run` reaches it at least every 25 s while it can.
- Events carry no time. A shell stamps each with its own clock as it arrives. `finished` says the request
  finished on this computer, not that Lucille received the reply.
- A line a shell cannot use (not JSON, longer than the bound) it drops, notes for diagnostics, and keeps
  reading. A last fragment with no newline at the end of the stream is discarded. On an object with another
  `v`, a shell stops trusting the events, keeps reading both streams, and stops `run` by closing its write
  end of standard input.

### 13.3 Exit codes of `run`

| Exit | `stopped` `reason` | Meaning |
|---|---|---|
| 0 | `drained` | Stopped on purpose, by what `cause` says: the first `SIGTERM`, `SIGHUP` or Ctrl-C, or with `--exit-on-stdin-eof` the end of standard input or a standard output nobody reads (13.4). The running operation finished and its reply was posted, or the reply was given 30 s. |
| 0 | `forgotten` | The pair was forgotten on this computer while `run` served it: by another command (`forget`, `pair reject`, a `pair` answered `n` at its prompt, or a `pair`, `pair status` or `pair confirm` that found the pair over or its SAS past its deadline), or by the pairing library within a touch of `run`'s that found, in the pair room, that this computer's side ended the pair (a `forget` here that stopped part way, or another copy of this computer's half). |
| 0 | `ended` | A resolve of `run`'s found the pair ended: by Lucille, or, in the pair room, by this computer's side (as for `forgotten`). This computer's half is forgotten too. |
| 20 | `not_paired` | This computer holds no pair. |
| 20 | `no_gate` | The pair has no local gate record, so it can never be confirmed here: `pair reject`, then pair again. |
| 20 | `several_pairs` | The profile holds more than one pair, and the host keeps one. |
| 20 | `pair_unusable` | The pairing library refused the pair here. |
| 20 | `leaked`, `expired` | The pairing ended early, or its SAS was not confirmed by its deadline, while `run` waited: the pair is forgotten. Pair again. |
| 21 | `already_running` | Another `run` of this profile runs. Nothing was changed. |
| 23 | `insecure_keyring`, `store_error` | The keyring backend the host keeps the pair in cannot be used here, or keyring's own off switch is set, or the store cannot be used. |
| 24 | `startup_refused` | The host does not start here: it runs as root, or a `SWITCHBOARD_*` variable is set. As root, `run` gives 24 before it resolves the profile, installs a handler or opens a file. |
| 25 | `downgrade` | This release is older than one that started `run` on this profile before (13.6). |
| 1 | `failed` | Anything else `run` met and stopped for; `code` says what, and standard error says why. An unexpected error (`internal`, which is also any failure `run` has no other code for) writes its traceback in place of that line. |
| 2 | | Usage: the command line could not be parsed. With `--json` or `--json-events`, one `error` object with `code` `usage`. |

- `run`'s start checks come in this order, each before the next can write or read anything: the profile and
  its host configuration (1); the environment (24); the run lock (21); the version record (25); the private
  runtime folder (1) and the store (23); how far each work room was read (`state/cursors.json`), which a
  damaged file makes `failed` `state_invalid` (1); the policy digest key, which a damaged file makes
  `failed` `state_invalid` rather than an invalid policy (1); the pair, its record in the store and its gate
  record, read on this computer (20, or 23 for a record the store cannot read); then `ready`, and the first
  hub call. The uid check comes before all of them, before the profile's paths are resolved (as it does for every verb, section 10). So root writes nothing, a second `run` writes nothing but its line in the run record (section
  16), and an older release reads no secret.
- A pair whose SAS is not confirmed yet is not refused at start: `run` serves it, and waits for `pair
  confirm` (section 13.1). Of the gate, only a pair with no gate record, which can never be confirmed here,
  gives 20 at start (`no_gate`). While `run` waits, `expired` comes once the SAS deadline passes, and
  `leaked` and `pair_unusable` as for a confirmed pair.
- 21, 23, 24 and 25 come only before `ready`, and so do `not_paired`, `no_gate` and `several_pairs` unless
  the pair goes while `run` runs. `pair_unusable` comes before `ready` for a record in the store the
  library refuses, and after it when the library refuses the pair at a hub call. After `ready`, a store
  that cannot be read is `failed` with `store_error`: a shell starts `run` again, and a store that still
  cannot be used then gives 23. 22 is not used.
- **How `run` ended comes first.** 20 to 25 are also signal numbers on macOS (`SIGCHLD` 20, `SIGTTIN` 21,
  `SIGTTOU` 22, `SIGIO` 23, `SIGXCPU` 24, `SIGXFSZ` 25), and `SIGXCPU` and `SIGXFSZ` end a process. So a
  shell checks that `run` exited (Foundation's `terminationReason` is `.exit`) before it reads the number, as
  section 4.3 says, and checks the number against the `stopped` reason it read. Its exit can be seen before
  its last line is read: a shell reads standard output to its end, for up to about 2 s, before it decides.
- **Within an exit code, the reason decides.** After 0, `drained` is a stop the shell asked for, or not;
  `forgotten` and `ended` mean the pair is gone. After 20, the reason says what the user does. When `stopped`
  is missing after 0 or 20, the shell asks `status --json` where the pair is before it acts.
- **What a shell does** ([decisions/0002-macos-app.md](../decisions/0002-macos-app.md) §9 says it for the
  Mac app):
  - after 1, a signal, an exit code it does not know, or a `drained` it did not ask for (a drain within a few
    seconds of `ready` above all), it starts `run` again with a back-off, and counts each toward giving up;
  - after 0 `forgotten` or `ended`, 20, 23, 24 or 25, it does not start `run` again, and shows why; after 2,
    which can only be its own mistake, it does not either, and shows standard error;
  - after 21, it asks `status --json`: while `run` is `running`, a `run` is still finishing, perhaps its own
    from before a relaunch, and it tries again with a back-off; once `run` is `stopped` or `not_seen`, it
    starts at once; if `run` stays `running` past the drain's bound (below), another copy runs, and it says
    so. `run` keeps writing its status while it drains.
- **A drain's bound** is the running operation's time limit (at most 300 s), then 30 s for its reply. A
  request still waiting for a valid policy, or held in a load of it, is not waited for. A drain begins when
  the step `run` is in ends. Against a hub that does not answer, that step waits for its hub calls, each given
  up after 40 s, and `state` and `status` go on saying `run` runs. But a request being
  decided can wait on a folder of the policy that is a stalled network mount (containment looks at it), and
  then the drain waits with it: a shell's last resort is `SIGKILL`, which can leave an operation's process
  group running without its time limit ([THREAT_MODEL.md](../THREAT_MODEL.md), "Known gaps").

### 13.4 `--exit-on-stdin-eof`

- `run` reads its standard input, and discards what it reads. At its end it drains exactly as on its first
  `SIGTERM` ([PROTOCOL.md](PROTOCOL.md) §16): the running operation finishes and its reply is posted, nothing
  new starts, and an operation decided and not yet started is `failed` with `interrupted`. It then writes
  `stopped` `drained` with `cause` `stdin_end`, and exits 0. A Ctrl-C after that kills the running
  operation, as a second Ctrl-C does.
- The end of standard input is: every holder of the pipe's write end closed it (a shell that quits, crashes
  or is force-quit); a read error; or no standard input at all (fd 0 closed when `run` started). A read end
  handed over non-blocking is made blocking.
- Before `run` serves, the end stops it between two of its start checks: `stopped` `drained`, with no
  `ready`. So does the first `SIGTERM`, `SIGHUP` or Ctrl-C, once the profile's name is checked, with `cause`
  `signal`, with or without this option; a Ctrl-C then kills nothing, since nothing runs. A start check that
  waits (on macOS, a Keychain prompt; a state file on a stalled mount) is not cut short: the end, or the
  signal, is seen once it returns.
- With `--json-events` too, a standard output that nobody reads any more (a write fails, or 1000 objects
  wait) drains `run` the same way, with `cause` `stdout_gone`. Without `--exit-on-stdin-eof`, such a standard
  output only stops the events; `run` goes on, and exits with the code it chose.
- Without `--exit-on-stdin-eof`, `run` never reads its standard input, so a `run` started under `nohup`, or
  with its standard input closed, keeps running.
- With a terminal as standard input, Ctrl-D drains. In the background of a shell with job control, where
  reading the terminal would stop the whole process, `run` does not read it and goes on; in the foreground
  again, Ctrl-D drains. The option is meant for a pipe.
- A shell that wants the running operation stopped, not finished, runs `lucille-host kill`, which also
  pauses.

### 13.5 The local policy while `run` runs

- `run` loads the policy file before each decision, so a change applies to the next request, with no
  restart and no signal: a folder removed is refused `path_outside_root`, an operation turned off
  `not_enabled`.
- While the file is invalid, `run` acts on nothing: it accepts no work room (and finishes no accept a stop
  interrupted), writes no request down, decides, runs and answers nothing, and starts reading no room. A reply
  it decided before is still posted. It keeps the pair (it resolves and touches it, and leaves a room that
  fails its checks or that a move gives up), and the controls work. `state` says `policy` `invalid`, and
  `status` why. Lucille's requests wait on the hub, as for a computer that is off, and expire if the file is
  not made valid in time.
- From when `run` notices: it looks at the file once a second, and again right before it accepts a room
  (a step can wait on the hub). A valid file that changed holds the offers until a load of it says it is
  still valid (`state` says `policy` `checking`), and one that is invalid by what it holds (not TOML, an
  unknown key) is `invalid` at once, even while an operation runs; an invalid file stays `invalid` until a
  load of it says it is valid. A file that is invalid only by a folder (one that became the home folder,
  through a link, the file unchanged) is found by the next load: before the next decision, and every 30 s
  between `run`'s steps, while an operation runs too. A step that waits on the hub can last longer than
  that, so `run` accepts no room under a load that began 30 s ago or more: the offer waits for a new load
  (`checking`).
- When the file is valid again, by `policy reset` or by hand, `run` goes on, with no restart. `ops` and
  `roots` refuse with `policy_invalid` while the file is invalid: they change it only once `policy reset` has.
- `run` never rewrites the file. `policy reset` sets an invalid one aside.

### 13.6 Downgrades

- `run` keeps, in `state/last_version.json`, the newest release that has started `run` on this profile, and
  refuses to start when it is older (exit 25, `stopped` `downgrade` with that release). An older release can
  be a vulnerable one.
- Releases are ordered as [PROTOCOL.md](PROTOCOL.md) §10 orders a host's version against `min_host_version`:
  by the numeric triple a version starts with; a version with anything after the triple comes before the same
  triple; a version that does not start with a triple comes before every other. The record holds only a
  release, `MAJOR.MINOR.PATCH` and nothing else, and never goes below it in that order. A build with
  anything after the triple (a development build) is checked against the record, and never recorded.
- So every release is a plain `MAJOR.MINOR.PATCH`, and a fix is a new PATCH, never a `.postN` or `+local`
  version, which would come before the release it fixes. The first release that keeps the record must ship
  with it: a release from before it does not read the record.
- The record says which release started `run`, not which one served: it is written once the run lock is held
  and the release is not refused, before the store and the pair are looked at.
- `run --allow-downgrade` starts all the same, and says so on standard error. The record keeps the newer
  release, so the next start of the older one is refused again.
- The record is per profile: a profile's `run` never refuses because another profile ran a newer release.
- The record is a file the user's account can change. It stops an older release by mistake, not on purpose.

### 13.7 A program that embeds the host

`lucille_host.library` serves a profile from inside another program, on a thread of its own, with no
command line: `start(dirs, store, ...)` returns a `Host` that is running, and `Host.stop()` drains it
as the first SIGTERM does (cause `stop`) and waits. It is not a verb and adds no field to the contract.
It keeps `run`'s files, its start checks (not as root, no `SWITCHBOARD_*` variable; both refused before
anything is written) and its one-run lock, but it installs no signal handler, and it writes no line to
the run record (section 16), which has one line per command of the CLI. Its environment check comes
before the host-config read, the reverse of `run` (both only read).

It runs no `github.read`: that operation's program is the host's own interpreter, which a program that
embeds the host does not name (its `sys.executable` is its own, and may not run `-I -m
lucille_host.proxy`). Enabled or not, `github.read` is refused `not_enabled` there, and `host.describe`
leaves it out of `enabled_ops` (decisions/0003, "First use").

- **`stop(raise_error=False)`** returns None for however the host ended, an `Exception` or not, and
  leaves it in `error`. `TimeoutError` (the host still runs after `timeout`) and an interruption of the
  caller's own wait still raise.
- **`stop()` from the host's own thread** (from `say`, `warn` or `on_event`) drains and then raises
  `RuntimeError`.
- **The program exits without `stop()`.** The host's thread is a daemon thread, so it is cut off
  where it is, and an operation running at that moment is left without what watched it. Observed with
  a `host.sleep` operation, for an exit by `os._exit` and for the program returning with the host
  running:
  - the operation's process is not killed (it has a session of its own), so it runs on, with no time
    limit and no kill request, until it ends by itself;
  - the audit log ends at the operation's `start` line, with no `execution` line, and Lucille gets no
    reply;
  - the journal holds the job as decided to run with no reply, and the room stays held;
  - the run lock is released, as its process ended, and its file stays;
  - the next `run` or `start` of the profile replies `failed interrupted` to that job and never runs
    it again, as after a `SIGKILL` of `run` (section 13.3's drain bound says the same of a group left
    running).

  A program that wants the operation finished, with its audit line and its reply, calls `stop()` and
  waits (at most the operation's time limit, then 30 s for its reply).

## 14. Not in version 1

- `diagnostics` and removing the host's data ([decisions/0002-macos-app.md](../decisions/0002-macos-app.md)
  §8).
- The typed pairing gate (decisions/0002 §11.2): decided on 2026-11-21, not built. Connect from Lucille's
  chat (§11.4 and §11.5) is built as `pair begin`, `pair resume` and `pair cancel` (section 7), and is the
  only way to pair.
- A typed history of what the host did, for a shell to show in words. `log_line` carries text only, which a
  shell shows as it is.
- During pairing, whether the hub could be reached and whether it refuses this computer's clock, beside
  `waiting`. They can be added to `waiting` as fields within version 1.
- `pair` stopping on its own when its standard input closes, so that a shell that dies cannot leave a
  `pair` holding the profile's pairing lock until the code expires. Meanwhile a shell stops its `pair`
  before it exits.

## 15. `credential`: credentials kept on this computer

`credential` stores credentials for online services (a password, an app token) in the system keyring of
this computer ([decisions/0003-local-credentials.md](../decisions/0003-local-credentials.md)). Two kinds
of operation use a stored credential, each off by default, each in a separate process, for the request
that names it: `github.read` reads one stored under the service `github` and bound to `api.github.com`
(decisions/0003, "First use"), and the `gmail` operations one stored under `gmail` and bound to
`imap.gmail.com` ("Second use"). Nothing Lucille sends can list or change one, nor read one any other
way; with `credential.ask` on, it can ask the user to save one, which the user types here ("Asks",
below). With `--json`, before the verb or after an action, it writes the objects of section 7, and
`credential set` takes the value only with `--value-stdin`.

| Form | What it does |
|---|---|
| `credential set <service> <name> --host <host> [--host <host> ...] [--value-stdin] [--replace]` | Stores one credential, bound to the hosts given. |
| `credential list` | One line per credential, by service then name: its service and name, its hosts and when it was stored. Never its value. With none stored, nothing on standard output, a line on standard error, and exit 0. |
| `credential remove <service> <name>` | Deletes that credential and no other. One not stored is refused, exit 1. |
| `credential asks` | One block per ask of Lucille's that waits for an answer (`credential.ask`, below), oldest first: its id, the service, the name, the hosts, what to type and where to make one, whether it replaces a credential saved here, how long it waits, and, last, Lucille's own words, cut into lines of at most 64 columns that each start `Lucille says: `. With none waiting, nothing on standard output, a line on standard error, and exit 0. |
| `credential answer <id> [--decline \| --value-stdin]` | Answers one ask. Without an option, standard input must be a terminal: it shows the ask on standard error, asks `Type yes to save it`, and anything but `yes` declines; after `yes`, the value is typed twice, as for `credential set`. `--decline` declines it, asking nothing. `--value-stdin` saves the value read from one line of standard input, asking nothing: the program that runs it showed the ask itself and the user chose to save. With `--json`, one of the two is needed. Saving binds the credential to the ask's hosts and no other, replacing one saved under that service and name; a value not in its service's form is refused (`bad_value`), nothing is saved, and the ask still waits. Each answer writes one `credential_answer` line to the audit log: the service, the name and the answer, never the value; for an ask that lapsed before its answer was recorded, the answer `lapsed` and whether the credential was saved all the same (`saved`). |
| `credential test <service> <name>` | Signs in with that credential and out again, reading nothing, by running `gmail.check`'s child, as a request would, whether or not `gmail.check` is enabled. `gmail` only. On success, one line, `gmail/<name>: signed in to imap.gmail.com`, and exit 0; otherwise the child's own fixed line and exit 1. Refused, starting nothing, for another service (`no_test`), a credential not stored (`no_such_credential`), while the host is paused (`paused`), and while keyring's own settings turn the keyring off (`insecure_keyring`). The child's exits give `bad_credential` (2), `unreachable` (4), `limit_reached` (5), `sign_in_refused` (7) and `internal` (any other). Each test writes one `credential_test` line to the audit log: the service, the name and the code (`ok` on success), never the value. Nothing Lucille sends can run it. |

**Asks (`credential.ask`).** Lucille may ask for a credential with the operation `credential.ask`
([PROTOCOL.md](PROTOCOL.md) §10), off by default. The request carries no value: a service with a template
(`github` or `gmail`), a name, and, optionally, Lucille's reason. Its child leaves the ask for `credential
asks` and `credential answer` and waits up to 240 s for the user's answer; Lucille is told only `saved` or
`declined`.
- The hosts are the service template's, never the request's: `api.github.com` for `github`,
  `imap.gmail.com` for `gmail`. A service with no template cannot be asked for.
- Declining is the default: an ask not answered within 240 s is declined, and so, without being shown,
  is an ask made while another waits and an ask made less than 600 s after any ask ended, for any
  credential.
- Lucille's words come last, after the host's own lines, cut at their spaces into lines of at most 64
  columns (a character beyond ASCII counts two), each starting `Lucille says: `, so every line fits in 78 columns
  and none reads as one of the host's. Rule `credential-ask-args` (PROTOCOL.md §10) keeps out of them the
  characters that would move, hide or blank out text, and runs of spaces.
- While an ask waits, `run` runs no other request: it runs one at a time.
- A shell shows an ask in a dialog of its own when `run` reports a `credential.ask` request `started`
  (section 6, `request`), reads it with `credential asks --json`, and answers it with `credential answer
  <id> --value-stdin --json` or `--decline --json`; closing the dialog declines.

**Warnings of `credential set`** (decisions/0003, "Hardening"). Once a credential is saved, `credential set`
warns on standard error, never naming the value: for a service with no template, that nothing in this
release uses it; for one with a template, when its hosts leave out the template's, and when its value is
not in the template's form (a GitHub token; a Gmail address and app password), which the operations never
send, such as an account's own password saved by mistake. It is saved all the same.

**Names.** A service and a credential name are each 1 to 64 characters of `a`-`z`, `0`-`9`, `.`, `_` and
`-`, starting with a letter or a digit: `gmail`, `github.com`, `app-password`. The whole name is matched:
a name with a newline at its end is refused.

**Hosts.** Each credential is bound to the hosts given with `--host`, the only hosts it may ever be sent
to (decisions/0003, "Use", rule 1): `github.read` sends one only when its hosts include `api.github.com`.
That use enforces exactly what is stored here:
- A host is an exact DNS name: two labels or more, separated by `.`, each label 1 to 63 characters of
  `a`-`z`, `0`-`9` and `-`, not starting or ending with `-`, the last starting with a letter; 253
  characters at most. The whole host is matched: a host with a newline at its end is refused.
- It is ASCII as given. A character that lowercases to an ASCII letter, such as the Kelvin sign (U+212A),
  is refused, since ASCII is checked before lowercasing. A name in another script is written in its
  `xn--` form, and a label that starts with `xn--` must decode as IDNA writes one; one that does not
  (`xn--abc`) is refused.
- What is given is lowercased, and one trailing `.` is taken off. A host given twice is kept once. The
  hosts are kept in the order given.
- Refused: a wildcard (`*.example.com`), a scheme (`https://`), a path, a port (`:443`), an IP address
  (IPv4 or IPv6), a single label (`localhost`, `gmail`), and a name of this computer, of a local network,
  or one that resolves nowhere on the internet: `localhost`, `localdomain`, `local`, `lan`, `internal`,
  `home.arpa`, `arpa`, `test` and `invalid`, and any name ending in `.` and one of them
  (`localhost.localdomain`, `printer.test`). A host is matched exactly, never as a pattern: `gmail.com`
  does not cover `imap.gmail.com`.
- One host at least, and 16 at most.
- A refusal names a host by its place (`host 2`), and does not repeat it.

**The value.**
- It is never an argument. Without `--value-stdin`, standard input must be a terminal: the value is asked
  for twice, at a prompt on standard error that turns the terminal's echo off while it is typed, and two
  values that differ are refused. With `--value-stdin`, standard input must not be a terminal; the verb
  reads one line, the value, and nothing more, taking off the line break (LF, or CR and LF) that ends it.
- Refused, with nothing stored: an empty value (an empty line, or standard input that ends at once), a
  value holding a control character (U+0000 to U+001F, or U+007F), one longer than 65536 characters, and
  one that is not text in standard input's encoding (`bad_value`, without the bytes it could not decode):
  a line that does not decode, or one holding a lone surrogate (U+D800 to U+DFFF), which is what
  standard input decodes undecodable bytes to under the C or POSIX locale or with `PYTHONUTF8=1`. Both
  are refused at the prompt and with `--value-stdin`.
- The names and hosts are checked, and a credential already stored without `--replace` is refused, before
  the value is asked for or read.
- The value is written to the keyring and nowhere else. No output, error message, traceback or file of
  the host's holds it, nor any other stored credential's value.
- A usage error of a `credential` command line repeats nothing typed on it, since a word there may be a
  value typed by mistake. That is any usage error the verb's parser, or one of its actions', raises, and
  any usage error of a line that names the verb anywhere, before `--` or after it
  (`lucille-host -- credential list <word>`), whichever parser raised it. It is argparse's own message
  only where that names the verb's arguments and options (`the following arguments are required: name`). An unknown action is
  `unknown credential subcommand: it is set, list, remove, test, asks or answer`, after the verb's usage. Any other says that
  a value is never given on the command line, and that one typed there is now in the shell's history.

**Replacing.** A credential already stored under that service and name is refused (exit 1), and nothing
changes, unless `--replace` is given. `--replace` replaces its value, and replaces its hosts with the hosts
given now: the hosts it had before are dropped. `--replace` with none stored stores it. Whether one is
stored is decided again under the store's lock when it is written, so one stored meanwhile by another
command is not replaced without `--replace`.

**The keyring.** A credential is stored only in the keyring backend the host builds for this system: the
Keychain on macOS, the Secret Service on Linux.
- A keyring the host refuses, or one turned off by keyring's own setting, is refused with the store's own
  message (`insecure_keyring`), exit 1. So is one that cannot be read or written for a reason the store
  has words for, such as a Secret Service that cannot be started (`store_error`): those words are the
  host's, with the error's class and D-Bus name, never its message.
- Any other error of a read or a write of the keyring is `store_error` with a fixed sentence: "the keyring
  refused to read (or write) the credentials item; its error is not shown, since it may hold a
  credential's value". Nothing of the keyring's error is shown: not its message, its notes, nor the error
  as a cause or context. The item holds every credential, written whole, so the error could carry any of
  them.
- An error of the verb's own that none of its refusals names is `internal`: its traceback shows where the
  verb was, and an error of the host's own in place of the one raised, whose message, notes, cause and
  context are not shown.
- A Ctrl-C on the verb's path, where no prompt catches it (`interrupted`), is raised again as a bare
  `KeyboardInterrupt`, which carries nothing of an error it interrupted the handling of.
- An item whose service, credential names or hosts this version would not write (a newline at the end
  of one, `a.example.com/x`) is not read, and is left as it is (`store_error`).
- There is no file fallback, as there is for the pair (`pair --store-file`), and `--store-file` is a usage
  error. The verb refuses to run as root, as every verb does (section 10).

## 16. The run record

Every command, before its work starts, appends one line to the run record of its profile: `runs.jsonl`
in the profile's state folder (`~/Library/Application Support/lucille-host/<profile>/state` on macOS,
`$XDG_STATE_HOME/lucille-host/<profile>` on Linux), mode 0600. That includes `status`, `roots list`,
`roots check`, `roots add --suggest-alias`, `--version`, a second `run` that finds the profile running,
and a command that then refuses. So any command, `--version` included, also makes the profile's state
folder, and each folder above it that is missing, at mode 0700, and sets the state folder to mode 0700
where it was there already.
Where this document says that a command writes or changes nothing, it means nothing but this.

The run record is kept on this computer only: nothing reads it at run time, and nothing sends it anywhere.
It is there so that the Mac app's checks can prove that the app started its own host
([macos/README.md](../macos/README.md), placement), from the host's own word, without catching a process
that may run for a fraction of a second. Any command counts for that, not only `run`.

Each line is one JSON object, of at most 1,024 bytes:

| Field | What it holds |
|---|---|
| `v` | `1`. |
| `command` | The verb the command line names (`status`, `run`, `credential`, …), or `version` for `--version` or no verb. The command line is read as the parser reads it: an option the parser takes by an abbreviation (`--prof`) counts as that option, and every word after `--` is a plain word. Never an argument: a line whose first plain word is not a verb, or that asks for help, writes no line, so a value typed on the command line by mistake never lands here. |
| `pid` | The command's process. |
| `ppid` | The process that started it. |
| `executable` | The real path of the interpreter that runs the command where it is an app bundle's runtime (`…/Contents/Resources/runtime/bin/lucille-host-runtime`); `null` otherwise, so that no path outside an app is kept, and `null` where the path would make the line longer than 1,024 bytes. |
| `started` | When the command started, in seconds since the epoch: a finite number. |

- **It never fails the command, and never blocks it.** A line that cannot be written is skipped, and
  nothing is said: a full disk, a folder where the file goes, or a symbolic link in its place, which is
  not followed. The record is only ever a regular file with one name: a FIFO, a device or a file with
  another name linked to it is left alone, and opening it never waits for a reader. Nothing is written
  as root (every verb is refused there first, section 10), or for a profile name that is not valid. The file is not synced to disk.
- **One line at a time.** Each line is appended under the record's lock (a POSIX lock on the whole file),
  which a command holds for one short write. While another command holds it, a command waits for it at
  most 0.2 s, then skips its line.
- **It is capped by bytes.** Once the record is larger than 64 KiB, the command that finds it so replaces
  it by its last 32 KiB, from the first whole line in them: written to a new file beside it, then renamed
  over it, under the lock. So the record is never larger than 64 KiB and one line, and a command reads
  at most 32 KiB of it, however large it was made. No line written after a cut is lost, a reader finds the
  old record or the new one and never an empty or half-written one, and a crash during a cut leaves the
  old record (and, at worst, a file named `.runs.jsonl.*.cut` beside it).
- **It is the host's word as far as your account is trusted.** Any process running as you can write a
  line of its own, in the right shape, and the Mac app's check would take it. That is no wider than what
  such a process can do already: run the host itself.
- **What it holds about you:** which commands ran on this computer, when, and from which app, but no
  argument, folder, credential, request or reply.

## 17. `profile check`: a profile an earlier run as root left behind

Before the host refused uid 0, a command run as root could leave folders and files in a user's profile
that root owns, mode 0700 or 0600, so that the user's own commands fail with `Permission denied`
([decisions/0004-running-as-root.md](../decisions/0004-running-as-root.md), 1.2 and 4.3). `profile check`,
run as the user, finds them and says how to repair each one. It changes nothing (the repair needs root,
and the host never runs as root: as root it is refused, as every verb is), takes no lock, sends nothing,
and writes nothing but its line in the run record (section 16: like every command, it makes the state
folder where that is missing). It exits 0 whatever it finds.

- **Which folders.** The profile's config folder, and its state folder where that is not inside the config
  folder. Each is resolved as every verb resolves it (`HOME` where it is absolute, an absolute
  `XDG_CONFIG_HOME` or `XDG_STATE_HOME`, a relative one ignored, the macOS layout), then with every
  symbolic link followed, since the host follows them too. A finding's `path` is the resolved one; `ok`
  names the two folders as every verb names them, before symbolic links are followed.
- **Above each folder.** It looks at the folder and each folder above it, up to the first one the user
  owns, and never at the home folder or above it. A missing folder is not reported: the host makes it. Of
  the folders another user owns, it writes the highest, since `chown -R` of it repairs the ones below. A
  folder that cannot be looked at because the one above it refuses the user is written as `unreadable` only
  when no folder above it is another user's.
- **Inside each folder**, where the folder itself is the user's: every folder and file. One another user
  owns is written, and nothing inside it is looked at; one that cannot be looked at is `unreadable`. A
  symbolic link is not followed into, and its own owner does not matter; what it points to, which the host
  follows (a `policy.toml` linked to a file kept elsewhere), is looked at as the folder or file it is, and
  written by its own resolved path.
- **The repair.** Inside the home folder, `look` is `ls -lR '<path>'` (`ls -l` for a file) and `repair` is
  `chown -R <uid> '<path>'` (`chown` for a file), `<uid>` being the user's own uid, which needs no lookup
  in the user database. The group is left as it is. The folder may hold another program's files by now
  (a `~/.config` root made), so a person runs `look` first. Outside the home folder it suggests no
  command (`outside_home`): whether another program needs that folder as it is is a person's call.
- The text form writes one finding per line, `look` and `repair` indented below it, or "every folder and
  file of this profile is yours".

## Changes

- **1.1** (the release after `0.1.0`, still a draft): `verify`, with its type `verify` and the codes
  `not_a_release`, `no_verifier`, `yanked`, `not_attested` and `files_differ`; `unreachable` is given by
  `verify` too. `profile check` and its type `profile_check` (section 17), for a profile an
  earlier run as root left unreadable.
- **1.0** (`0.1.0`, still a draft): the first version, with `run`'s event stream and exit codes, `policy reset`,
  and `credential` with `--json` (its types `credential`, `credential_removed` and `credential_test`), for
  the Mac app's Gmail window, and `credential asks` and `credential answer` (the types `credential_ask` and
  `credential_answer`, the code `no_such_ask`), for `credential.ask`; and Connect from Lucille's chat: `pair begin`, `pair resume`, `pair cancel`,
  the `connect` type, and its error codes. The paste form is gone: `pair` takes no pairing code, on its
  command line or with `--code-stdin`, and answers only a code delivered for its own Connect request.
- A build of the repository's development version is not a release, and writes the latest contract
  recorded here, whatever it holds.
