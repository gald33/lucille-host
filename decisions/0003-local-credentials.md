# 0003: Local credentials (draft)

**Status: storage, entry from the command line, the first use, `github.read`, and the second use, Gmail
over IMAP with an app password, with Capsule's Gmail window, built; entry at Lucille's request,
`credential.ask`, built on the host's side, and Hardening's service templates, risk tiers and warnings
built; Capsule's dialog for an ask, and Hardening's rules 5 to 7, not built.**
`src/lucille_host/credentials.py` can save, list, read and remove a credential for an online service,
kept only on this computer. `lucille-host credential set`, `list` and `remove` write to it from the command line, each credential bound to the hosts it may be
sent to (spec/CLI.md §15). Five operations use a stored credential, each off by default: `github.read`,
whose child process reads the one credential a request names and sends it to `api.github.com` alone
("First use", below), and the four `gmail.` operations, whose child sends it to `imap.gmail.com` alone
("Second use"). Nothing Lucille sends can list or change a credential, nor read one any other way; with
`credential.ask` on, Lucille can ask the user to save one, which the user types here ("Entry"). This
record says what was built, and what is still to be decided before a credential kept here can be used
for anything else.

## Why

A Lucille user can choose where a credential they give Lucille (a password, an app token) is kept:

1. on their own computer, the private option, in this program;
2. in Lucille's own storage, encrypted with Lucille's key, as integration tokens are kept today;
3. later, with a third-party execution runtime.

This repository builds only the first. The other two belong to Lucille's backend.

## What is built

- **A separate keyring item.** Service `lucille-host.credentials`, user name the profile, one JSON
  document `{"v": 1, "credentials": {"<service>/<name>": {"service", "name", "value", "saved_at"}}}`.
  It is not the pair's item (`store.py`), so:
  - a damaged credential item never risks the only copy of the pair secret;
  - `forget`, which ends the pair, leaves the user's credentials alone.
- **Only the system keyring, by the pair's rule.** The item is kept only in the keyring backend the
  host builds for this system itself (`store.KNOWN_BACKENDS`): keyring's Keychain backend
  (`keyring.backends.macOS.Keyring`) on macOS, its Secret Service backend
  (`keyring.backends.SecretService.Keyring`) on Linux. `store.check_backend` checks it before every
  read and every write of the item, comparing its class's module and name, and the read or write
  goes to the backend it returns, never through keyring's own `get_password` and `set_password`. So
  a subclass, a chain of backends, or a backend under any other name, one that writes to a plain
  file among them, is refused and never read or written; keyring's off switch is honoured as it is
  for the pair. The refusal is the module's own (`credentials.REFUSAL`) and names no fallback:
  there is no file fallback like `--store-file`. On a Linux desktop whose only keyring is KWallet,
  there is no credential store.
  - What this does not cover: code running as the user that replaces the system's class itself,
    for example a `.pth` file that patches keyring at start-up. That code could also read the value
    from this program's memory, so no check inside the program can stop it.
- **Names, never values.** Service and credential names follow Lucille's rule (`^[a-z0-9][a-z0-9._-]{0,63}$`).
  `names()` returns names and save times only. Error messages hold nothing read from the item, and a
  value is returned only by `value()`.
- **An item this version did not write is left as it is.** It may hold the only copy of the user's
  credentials.
- **Unreachable from Lucille, but through `github.read` and the `gmail.` operations.** No runtime
  module but `credentials.py` imports it, except the command line's `credential` verb, in its own
  function, `github.read`'s child, `proxy.py`, and the `gmail.` operations' child, `gmail.py`, each in
  the one function that reads the credential. So no other verb's process, `run` among them, loads it;
  the verb never reads a value back; and only a child reads a value, on a request for an enabled
  `github.read` or `gmail.` operation, or, for `gmail.check`'s child, on the user's own `credential test`
  at this computer, which no request can reach (`tests/test_credentials.py`,
  `test_only_the_credential_verb_and_the_proxy_import_it`,
  `test_importing_the_command_line_does_not_load_it`, `test_the_credential_verb_never_reads_a_value`;
  `tests/test_credential_verb.py`, `test_no_request_can_reach_credential_test`). No request or reply can
  write or list it, and only those five operations can have one read. `credential.ask`'s child,
  `asks.py`, does not import it either: the value an ask collects is saved by the user's own
  `credential answer`, in the verb's function (`tests/test_asks.py`,
  `test_the_child_imports_no_credential_store_and_no_network`).

## What was open, and how it was decided

1. **How a credential gets in.** Decided 2026-11-11, built 2026-11-27: see "Entry: Lucille asks, the user
   types it here" below. The value is entered on the computer and never passes through Lucille.
2. **Whether Lucille learns the names.** Decided 2026-11-11: yes. Lucille may learn a credential's
   service, name and bound hosts, never its value. The claim gains the sentence: "Lucille learns the names
   of the credentials you saved here, never their values."
3. **How a credential is used.** Decided 2026-11-11: see "Use: the host as a
   credential proxy" below.

## Use: the host as a credential proxy (chosen 2026-11-11; its first use, `github.read`, built)

This was chosen over two alternatives. A local program that asks the host for a credential
would let more than this program read the item. Handing the value to Lucille for each use would end
the private option. A third-party confidential-computing runtime (a hardware enclave, checked by
attestation before the host releases a value) was discussed and left for Lucille's `third_party`
store. Its program would enforce the same rules as below.

**The split.** Lucille decides what to do, on Lucille's servers, and that code never comes to this
computer. The host runs a small, public, reviewed proxy: it takes a request that names a credential,
checks it against the rules, adds the credential itself and makes the call. The reply goes back to
Lucille. Lucille's logic never sees the value, so it can stay private without putting the value at
risk. The user trusts this repository's code, not Lucille's.

**Rules the proxy enforces, all of them, or a value can leave through the service:**

1. **Hosts per credential.** Each credential is bound to a list of hosts when it is saved, and is
   attached only to requests to those hosts. Lucille cannot widen the list.
2. **No redirects off the list** while a credential is attached.
3. **A fixed place in the request.** The credential goes only where its kind says, such as an
   `Authorization` header or a login step, never into a URL, query or body Lucille wrote. Otherwise
   an allowed service can store it where Lucille reads it back, for example as the text of a draft.
4. **The value is scrubbed from replies,** in case a service echoes it.

**A local log the user can read**: which credential was used, against which host, how many times, and
when. The user sees what was done with a credential, not why Lucille did it.

**What this does not protect.** While the proxy is on, Lucille can do whatever the credential allows
on that service and read the replies. It protects the value, not the data the value opens. Narrowly
scoped tokens limit that where a service offers them.

**What it changes here.** The host stops making outbound HTTPS to its hub only: it also reaches the
hosts the user bound to a credential. `CLAIMS.md`, `PRIVACY.md`, `THREAT_MODEL.md`, the transport
section of 0001 and the AST and Switchboard lints change with it, in the same PR as the proxy.

**Anything that runs on this computer can be read by its owner.** That is true of this program today
and stays true. The proxy design keeps Lucille's own logic off the computer; only this repository's
public code runs here. A future feature that ships Lucille logic to run here (for example to keep data
off Lucille's servers) would expose that logic to the user; obfuscation slows a reader and stops none.

## First use: `github.read` (decided 2026-11-18, built)

The repository owner ruled on 2026-11-18: the first use is a read-only proxy, and its first service is
GitHub's REST API. It comes in two pull requests: this text and the protocol's (spec/PROTOCOL.md §10 and
§11) first, then the code, with the final text of the claim documents listed at the end of this section.

**The operation.** A catalog entry, `github.read`, off by default; the user turns it on with `lucille-host
ops enable github.read`. Its arguments are `{credential, path}`:

- `credential`: a credential's name, by Lucille's rule (`^[a-z0-9][a-z0-9._-]{0,63}$`). The service is
  fixed: the proxy uses the credential stored as `github/<credential>`. The user saves one with
  `lucille-host credential set github <name> --host api.github.com`.
- `path`: one of a fixed list of GitHub read endpoints, with a fixed set of query keys, matched by the
  pattern in the args schema (spec/PROTOCOL.md §10). A path off the list is refused `bad_args`, before
  anything is read or sent. The list: `/user`, `/user/repos`, `/search/issues`, and, under
  `/repos/{owner}/{repo}`, the repository itself, `/issues`, `/issues/{number}`,
  `/issues/{number}/comments`, `/pulls`, `/pulls/{number}`, `/commits` and `/readme`. The query keys:
  `per_page`, `page`, `state`, `sort`, `direction` and `q`, each with a bounded value. A repository named
  `.` or `..` does not match, so no path can climb out of `/repos/{owner}`.

**Where it runs: a child process, never `run`.** The catalog entry runs the host's own interpreter:
`<the host's interpreter> -I -B -W ignore -X utf8 -m lucille_host.proxy --state <the profile's state
folder> --profile <the host's profile> -- {credential} {path}`. `-W ignore` keeps Python's warnings off
standard error, which is reply output. Three elements are filled by the host, never from a request:

- the interpreter, as `sys.executable` gives it: an absolute path, not resolved through links. In a
  virtual environment (a `pipx` install among them), resolving the link would land on the base
  interpreter, which does not load the environment, and `-m lucille_host.proxy` would not import;
- the profile's state folder, which holds the credential store's lock file. The child's environment
  holds no `XDG_STATE_HOME` (Linux), so the child could not find the folder itself; the host passes the
  one it uses (added in the implementation, beside what was reviewed);
- the host's own profile name.

Where the host does not know its interpreter, `github.read` is unavailable, and refused `not_enabled`:
a program that embeds the host (`lucille_host.library`, spec/CLI.md §13.7) runs under its own
`sys.executable`, which may not be a Python that can run `-I -m lucille_host.proxy`, so it passes none.
Only `lucille-host run` passes its own.

Every string from the request comes after `--`, as in every entry. So:

- **The `run` process never imports `lucille_host.credentials`**, and never holds a value: only the
  short-lived child reads one. A test of `sys.modules` holds it. The network modules are another matter:
  `run`'s dependencies (the Switchboard client, keyring) already load `ssl`, `socket`, `http.client` and
  `ipaddress`, so no test could hold that `run` never imports them. What holds instead is that, among the
  host's own modules, only `proxy.py` may import `socket`, `ssl`, `http.client`, `ipaddress` or `certifi`:
  the AST lint's zone, below.
- The child's environment is `CHILD_ENV` plus a fixed allow-list of names, each copied from the host's own
  environment when it is set: `HOME` on macOS; `DBUS_SESSION_BUS_ADDRESS` and `XDG_RUNTIME_DIR` on Linux.
  The keyring needs them. Nothing else is copied: no proxy setting (`HTTPS_PROXY` and the rest), no
  `SSL_CERT_*`, `LD_*`, `DYLD_*` or `PYTHON*` variable (`-I` also makes the interpreter ignore
  `PYTHON*`).
- **The definition hash grows.** Today `_definition()` covers the argv template and `CHILD_ENV`. It is
  extended to cover each OS's allow-list of copied names, and which argv elements the host fills (as
  markers in the template, not their values, which differ from one computer to another). So a release
  that changes either turns an enabled `github.read` off until the user enables it again. The
  interpreter's sha256 is recorded in the audit log, as every executable's is.
- **Field check, not a claim:** whether macOS opens the credential's Keychain item to this child without a
  prompt is not known. It is checked on the repository owner's Mac with the implementation's first build,
  and the result is recorded in macos/README.md. So is whether a Linux desktop's Secret Service answers
  the child.
- The executable's sha256 in the audit log is the interpreter's; it does not cover `lucille_host.proxy`,
  which is part of the installed release.
- **Keyring's off switch is the host's to honour.** The child's environment carries neither
  `PYTHON_KEYRING_BACKEND` nor `XDG_CONFIG_HOME`, so the child cannot see an off switch set through
  either. So `run` decides: while keyring's own settings turn the keyring off in the host's environment
  (`store.keyring_turned_off`: `PYTHON_KEYRING_BACKEND` naming keyring's `null` or `fail` backend, or a
  `keyringrc.cfg` that does, in keyring's own configuration folder: on Linux, under `$XDG_CONFIG_HOME`,
  or under `~/.config` when that is not set), `github.read` is unavailable, refused
  `not_enabled` before its child starts, and left out of `host.describe`'s `enabled_ops`
  (`tests/test_jobs.py`, `test_github_read_is_refused_before_its_child_starts_while_keyring_is_off`).
  The child still reads its own default `keyringrc.cfg`, and refuses every backend but the system's own.

**What the child does, in order.** Each refusal writes one fixed line on standard error, holds no value,
and sends nothing.

0. **Its own arguments, and anything unexpected.** It reads its arguments by position, exactly
   `--state <state folder> --profile <profile> -- <credential> <path>`, not with `argparse`, which
   would exit 2 on a usage error and collide with exit 2 below. It also checks the credential's name
   and the path against the catalog's schema again. A top-level handler, the first thing `main` sets up, catches every
   exception the child does not raise on purpose: it writes one fixed line (`lucille-host proxy: internal
   error`), never the exception's text, which could hold a header's value, and exits 6. A malformed
   argument list exits 6 the same way. The child writes standard output once, at the end, from the
   scrubbed buffer, so a failure before that writes none. Python's own output can still reach standard
   error outside the handler in one way: when the interpreter cannot start the module (an import that
   fails before `main` runs), it prints a traceback and exits 1. No credential has been read then, so
   that output cannot hold one.

1. **The credential.** It reads `github/<credential>`, and refuses (exit 2) when there is none, when its
   bound hosts do not include `api.github.com`, or when its value is not in the form of a GitHub token
   (`github_pat_` followed by 22 to 255 of `[A-Za-z0-9_]`, or `ghp_` followed by 36 to 251 of
   `[A-Za-z0-9]`). That last check also keeps a password saved by mistake from being sent.
2. **The address.** Only `api.github.com`, port 443, over HTTPS. It resolves the name and refuses (exit 4)
   unless every address it gets is allowed, by its own fixed list, not by `ipaddress.is_global`, whose
   meaning changed between Python 3.11 and 3.13. One refused address refuses the whole request.
   - IPv4 refused: `0.0.0.0/8`, `10.0.0.0/8`, RFC 6598's shared address space for carrier-grade NAT (`100.64/10`), `127.0.0.0/8`,
     `169.254.0.0/16`, `172.16.0.0/12`, `192.0.0.0/24`, `192.0.2.0/24`, `192.88.99.0/24`,
     `192.168.0.0/16`, `198.18.0.0/15`, `198.51.100.0/24`, `203.0.113.0/24`, `224.0.0.0/4` and
     `240.0.0.0/4`, which holds `255.255.255.255`. Any other IPv4 address is allowed.
   - IPv6 allowed: only `2000::/3`, less these: `2001::/23` (Teredo and the other protocol assignments),
     `2001:db8::/32` and `3fff::/20` (both for documentation), and `2002::/16` (6to4, which can carry a
     private IPv4 address). So `::`, `::1`,
     `::ffff:0:0/96` (IPv4-mapped), `64:ff9b::/96` and `64:ff9b:1::/48` (NAT64, which can reach a
     private IPv4 address through a translator), `100::/64`, `fc00::/7`, `fe80::/10` and `ff00::/8` are
     all refused. On a network that reaches GitHub only through NAT64, `github.read` therefore fails
     (exit 4); that is the price of not reasoning about what the translator reaches.
3. **One connection per request.** It connects to a vetted address, never to the name again, so nothing
   can re-point the name between the check and the connection. TLS sends `api.github.com` as the server
   name (SNI) and checks the certificate for that name against certifi's bundle, the trust the hub
   client already uses. It sends
   `Connection: close` and makes no second request on the connection, so there is no keep-alive to
   re-check. It reads no proxy setting from the environment.
4. **The request.** `GET` only, with no body, and fixed headers: `Host`, `Authorization: Bearer
   <token>`, `Accept: application/vnd.github+json`, `Accept-Encoding: identity`, `X-GitHub-Api-Version:
   2022-11-28` and `User-Agent: lucille-host/<version>`. The token goes nowhere else: not in the path, the query or any
   other header. Lucille writes none of the headers.
5. **No redirects.** A 3xx is reported, never followed.
6. **Caps, and no compressed body.** The response's headers are read up to 64 KiB in all (and at most 100
   headers, `http.client`'s own limit), and its body up to 1 MiB; past either, it stops reading and exits
   5, having written what it read of the body. A response with a `Content-Encoding` other than `identity`
   is not read at all (exit 5, no body), since the scrub cannot see into a compressed body; nor is one with
   a transfer coding other than a plain `chunked` (`gzip, chunked` among them), which `http.client` would
   pass through as raw bytes. The reply carries at most what §13 of the protocol allows any child's output (64 KiB
   of each stream), with the stream's truncation flag set.
7. **The scrub.** Before anything is written, every occurrence of the token is replaced, in the body and
   in what goes on standard error. Because the token's characters are `[A-Za-z0-9_]`, URL-encoding leaves
   them unchanged and a standard JSON encoder escapes none of them, so the raw form also covers an echo
   in URL-encoded or JSON-escaped text. The proxy also replaces the token's base64 encoding at each of
   the three byte alignments (the four-character groups that depend only on the token).
   - **Where it runs.** Once, on the whole body buffer, after `http.client` has undone any chunked
     transfer encoding: never per read, so never across a chunk boundary. At the 1 MiB cut (exit 5), any
     tail of the cut buffer that is a beginning of the raw token is dropped before writing, so no
     beginning of the raw token is written at the cut. (A part of its base64 form can be: the scope,
     below, leaves parts out.)
   - **Its scope.** It is defence in depth. Lucille cannot make GitHub reflect the `Authorization` header,
     and nothing else sends the token. It does not cover JSON's optional `\uXXXX` escaping of ordinary
     characters, base64 broken across lines (the readme endpoint wraps its base64), a change of case, or a
     part of the token.
8. **What it writes.** Standard output: the response body. Standard error: `HTTP <status>`, then, when
   GitHub sent one, its `Link` header (for paging), both scrubbed. Exit code: 0 for a 2xx; 3 for any other
   status (a 3xx included); 2, 4, 5 and 6 as above, and 4 also when the connection breaks before the
   response has been read; 1 only when the interpreter could not start the module (above). These are the child's exit codes, which the reply carries as data under
   `done`: none of them is a refusal code of the protocol.

The catalog's default time limit, 30 s, applies.

**The usage log.** The audit log already records, for every request, its operation and arguments
(`received`) and how it ended (`execution`): so which credential, which path, and when. The
implementation adds the HTTP status to `execution`. It never holds the value.

**Imports.** A proxy-only zone in `tools/guards/ast_lint.py`, like the one `_exec.py` has:
`src/lucille_host/proxy.py` alone may also import `socket`, `ssl`, `http.client`, `ipaddress` and
`certifi`. `RUNTIME_IMPORTS` is not widened, and the re-export table is written again
(`tools/guards/reexport_probe.py`). The Switchboard-surface lint does not change: the proxy does not touch
`switchboard`.

**What it does not change.** No new refusal code and no new reply field: everything the proxy decides is
the child's output and exit code. Lucille sends `github.read` only with `min_host_version` set to the
first release that has it (spec/PROTOCOL-CHANGELOG.md), and needs its own change, outside this
repository, to send it.

**The claim documents, with the implementation.** Each of these sentences changed in the pull request
that built `github.read`, and not before, since until then it stayed true:

- PRIVACY.md, "the programs it starts get none of its keys and none of your environment variables":
  `github.read`'s child reads a credential you saved for it, and gets `HOME` (macOS) or
  `DBUS_SESSION_BUS_ADDRESS` and `XDG_RUNTIME_DIR` (Linux), which the keyring needs.
- PRIVACY.md, "What leaves it": also what `github.read` returns (GitHub's reply), and the names of the
  credentials it is asked to use, never their values.
- PRIVACY.md, the paragraph on how requests travel: with `github.read` on, your computer also connects
  to `api.github.com`.
- CLAIMS.md, C1: four catalog entries, not three, and rows for the proxy's tests.
- THREAT_MODEL.md: a row for a compromised Lucille using a stored credential. What bounds it: the
  operation is off by default, `GET` on the listed paths only, the token's own scope, and `pause` and
  `kill`. What is left: whatever that token can read on GitHub, while the host runs.
- decisions/0001-host-design.md, "Outbound HTTPS to one hub only": the hub, and `api.github.com` from
  `github.read`'s child, when it is on.
- This record's "Unreachable from Lucille": the credential verb and the proxy's child import
  `credentials`, and only the child reads a value, on a request for an enabled `github.read`.

## Second use: Gmail over IMAP, with an app password (decided 2026-11-18, built)

**Built.** `src/lucille_host/gmail.py` is the child; the four catalog entries, the `{@args}` element and
the rule `gmail-args` are in `src/lucille_host/ops/__init__.py`; `credential test` is in
`src/lucille_host/cli.py`. The tests run the child against a fake IMAP server (`tests/_fake_gmail.py`,
`tests/test_gmail.py`); no test signs in to Google. Two points of how, beyond what follows:

- `imaplib` never opens a connection. The child resolves and vets the addresses and opens the TLS
  connection with `proxy.py`'s `resolve` and `tls_connect`, then hands that socket to a subclass of
  `imaplib.IMAP4` whose `open` uses it; `IMAP4_SSL`, which would connect to the name itself, is not used
  (`test_imaplib_never_opens_a_connection_of_its_own`).
- `imaplib`'s debug transcript, which would hold the `LOGIN` line, is off: the subclass sets its debug
  level to 0, whatever `imaplib.Debug` says, and nothing `imaplib` writes reaches the output, since the
  child sets its standard streams aside as `github.read`'s does.

Capsule's Gmail window ("Entering the app password: Capsule", below) came in a change of its own, with
`credential --json` (spec/CLI.md §6 and §7), which it reads. From the command line, the credential is
saved with `lucille-host credential set gmail default --host imap.gmail.com` and checked with
`lucille-host credential test gmail default`.

The repository owner ruled on 2026-11-18: the second use of a stored credential reads Gmail over IMAP
with a Google app password, read-only, in four operations, `gmail.check`, `gmail.mailboxes`,
`gmail.search` and `gmail.read`; the app password is entered in Capsule; `gmail.read` returns a capped
text body. As for `github.read`, this text and the protocol's come first, then the code, with the claim
documents listed at the end of this section.

**Why IMAP and an app password.** Gmail's API, and IMAP through OAuth, need a restricted OAuth scope
(IMAP needs `https://mail.google.com/`), and a Google Cloud project that asks for one goes through
restricted-scope verification, with a yearly security assessment (CASA). An app password is the user's
own credential for their own account: no Google Cloud project, OAuth client or scope is involved, so that
verification has nothing to attach to. What Google's own pages say, as of 2026-11-18:

- An app password needs 2-Step Verification. The option is missing where 2-Step Verification uses only
  security keys, on a work, school or other organization's account, and with Advanced Protection. Google
  says app passwords "aren't recommended and are unnecessary in most cases", and revokes them all when the
  account's password changes ([Sign in with app passwords](https://support.google.com/accounts/answer/185833)).
- IMAP is always on for personal Gmail since January 2025
  ([Use IMAP to check Gmail](https://support.google.com/mail/answer/7126229)).
- Google Workspace stopped password sign-in to IMAP ("less secure apps") for its accounts as of May 1,
  2025, and names an app password as the alternative for what cannot use OAuth; an administrator may not
  allow them
  ([Winding down Google Sync and less secure apps support](https://workspaceupdates.googleblog.com/2023/09/winding-down-google-sync-and-less-secure-apps-support.html)).
- An account takes at most 15 IMAP connections at once and 2,500 MB of IMAP download a day
  ([Gmail bandwidth limits](https://support.google.com/a/answer/1071518)).
- IMAP through OAuth would need `https://mail.google.com/`, a restricted scope, and so verification and the
  assessment ([OAuth verification FAQ](https://support.google.com/cloud/answer/13463817)).

So: **supported**, a personal Gmail account with 2-Step Verification by any means but security keys alone,
without Advanced Protection; **not supported**, the others, and a Workspace account unless its
administrator allows app passwords. **Not settled, and to be confirmed before a public release:** whether
Google's terms, Gmail's program policies or its user-data policies limit a commercial service that asks its
users to make an app password for it, and whether Google withdraws app passwords as it withdrew password
access for Workspace. This record does not say that IMAP exempts anything from any policy.

**The operations.** Four catalog entries, each off by default and enabled on its own. Each reads; none
sends, moves, deletes, flags or marks a message read. Each takes `credential`, a credential's name by
Lucille's rule; the host uses the one stored as `gmail/<credential>`.

- `gmail.check {credential}`: signs in and out. It reads no message and no mailbox.
- `gmail.mailboxes {credential}`: the account's mailboxes (Gmail's labels), at most 500.
- `gmail.search {credential, mailbox?, query?, from?, to?, subject?, since?, before?, unseen?, limit?}`:
  the newest `limit` messages (1 to 50, 20 when not given) of `mailbox` (`INBOX` when not given) that match
  every criterion given. `query` is Gmail's own search syntax (Gmail's `X-GM-RAW`); `from`, `to` and
  `subject` match those headers; `since` and `before` are dates; `unseen` is a boolean. `query` is 1 to 256
  characters, `mailbox`, `from`, `to` and `subject` 1 to 128. How each is sent is under "IMAP, exactly".
- `gmail.read {credential, id}`: one message, by `id`, Gmail's own message id (`X-GM-MSGID`, a decimal
  integer of at most 2^64 − 1). That id is the same in every mailbox and stays the same, where an IMAP UID
  changes with the mailbox and with its `UIDVALIDITY`. The host finds the message in the mailbox Gmail marks
  `\All` (its name depends on the account's language). A user can hide that mailbox from IMAP in Gmail's
  settings; `gmail.read` then exits 3, with a line that says so, and reads nothing else. Gmail keeps
  messages in Spam and Trash out of `\All`, so an id that `gmail.search` found there, in `\Junk` or
  `\Trash`, is not found by `gmail.read`: exit 3. Reading them too would mean looking in those mailboxes as
  well, which this design does not do.

**The host's own check of the arguments,** after the schema's and before any child starts (step 8 of the
refusal order), refuses `bad_args`:

- any argument holding a control character, U+0000 to U+001F or U+007F, so no line break, carriage return
  or NUL reaches the child; the schema alone cannot say this, since its one free-text pattern refuses only
  U+0000;
- a date that does not exist (`2026-02-31`), read as the calendar reads it;
- an `id` above 2^64 − 1 (18446744073709551615).

The child checks the same again and exits 6 if one gets through.

**Where it runs.** As `github.read`: a child of the host's own interpreter, `<the host's interpreter> -I -B
-W ignore -X utf8 -m lucille_host.gmail --state <the profile's state folder> --profile <the host's profile>
-- <operation> <args>`, with the same environment allow-list, the same keyring and interpreter rules (not
run where the host does not know its interpreter, nor while keyring's own settings turn it off), and the
same definition hash rules. `<args>` is the request's `args`, which passed the schema, written by the host
as compact JSON with sorted keys; the child checks them again against the schema. The catalog's template
gains this one new element, which the definition hash covers. `src/lucille_host/gmail.py` joins
`proxy.py`'s import zone, which gains `imaplib`, `email` and `html` (for `html.parser`), and uses
`proxy.py`'s address list, TLS set-up, scrub and stream handling, not copies of them.

**The credential.**

- Bound hosts must include `imap.gmail.com`.
- Its value is the account's address, one space, and the app password's 16 lowercase letters (Google shows
  them in four groups of four; the screen and the command line drop the spaces). The address is no secret;
  it is kept in the value so that the store's format does not change. The whole value must match, as one
  ECMA-262 and Python regex with no flag:
  `^[A-Za-z0-9._%+-]{1,64}@([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.){1,8}[A-Za-z]{2,63} [a-z]{16}$`,
  and its address be at most 254 characters. The child matches the whole value, as Python's
  `re.fullmatch` does (never `re.match`, under which `$` also takes a final line break). A value not in
  that form is refused (exit 2), so an account's
  real password saved by mistake is never sent. The form leaves neither part a quote, a backslash, a space
  or a control character, so both go in `LOGIN` as IMAP quoted strings that nothing can break out of.
- The app password goes in the IMAP `LOGIN` command and nowhere else (rule 3 of "Use"). It is replaced,
  raw, in Google's spaced form and in base64 at the three alignments, in everything the child writes. In
  a document, each string is scrubbed before any cut, and its letters are also matched in any case and
  with up to three spaces or hyphens between any two, so a cut never leaves a part of it.

**What the child does, in order.**

0. Its arguments, by position, and a catch-all, as `github.read`'s (exit 6).
1. The credential: none, not bound to `imap.gmail.com`, or not in the form above: exit 2.
2. The address: `imap.gmail.com` only, port 993, every address it resolves to allowed by `github.read`'s
   fixed list, or exit 4.
3. One connection to a vetted address, TLS from the first byte (no `STARTTLS`), `imap.gmail.com` as the
   server name, the certificate checked against certifi's bundle.
4. Only the commands and fetch items under "IMAP, exactly", below. A test records every command the child
   sends, with every fetch item, and fails on any other.
5. Limits on what it returns, which are not failures: at most 500 mailboxes, at most 50 messages, and at
   most the first 4 MiB of a message (`BODY.PEEK[]<0.4194304>`), past which the message is parsed as far as
   it goes. Each sets its document's flag (`truncated`, `message_truncated`), and the exit is 0.
6. One limit that is a failure: at most 8 MiB read from Gmail in a request, counted on the connection,
   with IMAP lines within `imaplib`'s own limit; no read asks for more than one byte past what is left,
   and a literal Gmail announces past it is not read. Past it, the child stops reading, writes nothing on standard
   output, writes one fixed line on standard error, and exits 5.
7. `LOGIN` answered `NO` (a wrong or revoked app password, or IMAP closed to the account): exit 7, with a
   fixed line that says to make a new app password. Any other `NO` (no such mailbox, no such message, a
   search Gmail refused): exit 3. A `BAD` to any command, `LOGIN` included, means the child sent something
   Gmail could not parse, a fault of the child's own: exit 6. In every case the line is the child's own,
   fixed; nothing Gmail wrote, not the text of a `NO` or `BAD`, nor an `[ALERT]`, reaches the output.
8. On success it writes one JSON document on standard output (§11 of the protocol), scrubbed, and nothing
   on standard error.

**IMAP, exactly.**

- **Commands**: `CAPABILITY`; `LOGIN` with the address and the app password as quoted strings; `LIST "" "*"`;
  `EXAMINE` (a mailbox opened read-only; `SELECT` is never sent, nor `ENABLE`); `UID SEARCH`; `UID FETCH`
  with the items below; `LOGOUT`. Nothing else: no `STORE`, `COPY`, `MOVE`, `EXPUNGE`, `APPEND`, `CREATE`,
  `DELETE`, `RENAME`, `SUBSCRIBE`, `IDLE`, `ENABLE`, `COMPRESS` or `AUTHENTICATE`.
- **Fetch items**, the closed list: `UID`, `FLAGS`, `INTERNALDATE`, `RFC822.SIZE`, `X-GM-MSGID`, `X-GM-THRID`,
  `BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO)]` and `BODY.PEEK[]<0.4194304>`.
  Not `RFC822`, `RFC822.TEXT` or any `BODY[...]` without `.PEEK`, each of which marks a message read, and
  not `RFC822.HEADER`, `BODYSTRUCTURE` or `X-GM-LABELS`, which nothing here needs.
- **Mailbox names** go in modified UTF-7 (RFC 3501 §5.1.3), the encoding Gmail's IMAP uses unless `ENABLE
  UTF8=ACCEPT` is sent, which it never is. The host encodes a `mailbox` argument to it and decodes the names
  `LIST` returns from it. A name in modified UTF-7 is ASCII, and goes as an IMAP quoted string in which
  each `\` and `"` is escaped with a `\` (RFC 3501 §4.3); modified UTF-7 leaves both as they are, so the
  escaping is the child's to do. It sends no other character that a quoted string cannot hold: a name with
  a control character never reaches it (rule `gmail-args`).
- **Search criteria**: each text criterion (`query` as `X-GM-RAW`, and `from`, `to`, `subject`) goes in a
  `UID SEARCH CHARSET UTF-8`, as a synchronizing literal (`{n}`, then the server's continuation; never
  `LITERAL+`'s `{n+}`), whose length `n` counts the UTF-8 octets. `imaplib` sends one literal per command,
  so the child sends one `UID SEARCH` for each text criterion given, each with the non-text criteria
  (`SINCE`, `BEFORE`, `UNSEEN`) beside it, and keeps only the UIDs that all of them returned. With no text
  criterion, one `UID SEARCH` with the others, or `ALL`. The UIDs that come back count toward the 8 MiB:
  a search that matches about a million messages (a whole large mailbox, with no criterion) returns more
  than that and exits 5. A narrower criterion (`since`, `from`, `query`) answers it.
  `gmail.read` finds its message with `UID SEARCH X-GM-MSGID <id>`, the id an atom of digits.
- **Gmail's extensions.** `X-GM-RAW`, `X-GM-MSGID` and `X-GM-THRID` are Gmail's, offered when `CAPABILITY`
  lists `X-GM-EXT-1`. The child reads `CAPABILITY` after `LOGIN`; without `X-GM-EXT-1`, `gmail.search` and
  `gmail.read` exit 3 with their own line and send nothing more. `gmail.check` and `gmail.mailboxes` need
  none of them.

**What `gmail.read` returns.** From, to, cc, date, subject, `Message-ID`, `In-Reply-To`, Gmail's thread id,
the text body, and each attachment's file name, content type and size. Never an attachment's contents.

- The text body is the message's `text/plain` part, or, with none, its `text/html` part turned into text
  by `html.parser` (`script` and `style` dropped, no link followed, no image fetched), decoded by each
  part's charset (`email`'s default policy; an unknown charset is read as UTF-8 with replacement).
- Text that HTML hides from a reader is dropped where the markup says so plainly: an element with the
  `hidden` attribute, `aria-hidden="true"`, or an inline style of `display:none`, `visibility:hidden`,
  `font-size:0` or `opacity:0`. Not caught: hiding by a stylesheet, by colour or by position, and anything
  else; such text reaches Lucille as text (THREAT_MODEL.md).
- A `multipart/alternative` message's `text/plain` part is preferred, and its sender can make it say other
  things than the HTML part a reader sees. Lucille then reads text no reader saw. That is not caught either.
- It is cut at 32 KiB of UTF-8, with a flag saying so. Each header value is cut at 1 KiB, a recipient list
  at 4 KiB. The document always fits §13's 64 KiB, measured as rule `stream-size` measures it: past
  60 KiB, the child leaves out what does not fit, each with its flag (spec/PROTOCOL.md §11).

**Retries, limits, background.** The child retries nothing; Lucille may ask again. Each request opens one
connection and closes it, well inside Gmail's 15. No synchronization and no background polling: the host
reads only what a request asks for.

- **Requests in a row.** At most 60 sign-ins with one credential in any 10 minutes, counted in a file in
  the state folder, under a lock of its own there (`.gmail-signins.lock`), so that it never waits on a
  `credential set`. Past that, the child signs in to nothing and exits 5,
  with its own fixed line. That bounds how often it signs in, and so Google's reasons to lock the account.
  It does not protect the account's 2,500 MB of IMAP a day: 60 requests of 8 MiB each in 10 minutes could
  still use it up in under an hour.
- **After a refused sign-in.** A `LOGIN` answered `NO` is recorded the same way. For the next 15 minutes the
  child does not try that credential again: it exits 7 without connecting, so Lucille cannot drive repeated
  failed sign-ins, which Google may answer by locking the account. Saving the credential again (Capsule's
  Save, or `credential set --replace`) clears the record.

**Entering the app password: Capsule.**

- A Gmail window in Capsule, from the menu's own Gmail… item, shown whether or not the Mac is connected
  (not inside the Settings submenu, which shows only while connected, so that Gmail can be set up before
  pairing): the address, the app password (a secure field), and Save, Test and Remove, with the account
  limits above and a button, Open Google's App Passwords Page, that shows
  `https://myaccount.google.com/apppasswords` in the web browser, through the app's one opener
  (decisions/0002 §20, which gained it as its one web page). The window checks the value's form before it
  sends it, as the child does, and sends nothing else, so an account's own password is never saved. The
  repository owner chose the button over plain text, `credential --json` over the exit status, and the
  menu's own item over Settings, on 2026-11-19.
- Save runs `credential set gmail default --host imap.gmail.com --value-stdin --replace` with the value on
  standard input, never in the arguments, with `--json`. Remove runs `credential remove gmail default
  --json`, and the window reads whether one is kept from `credential list --json`.
- Test runs a new action, `credential test <service> <name>` (spec/CLI.md §15, with the implementation):
  - It takes the service `gmail` only; any other service is refused (`no_test`, exit 1), as is a name
    with no credential stored (`no_such_credential`, exit 1).
  - It runs `gmail.check`'s child for that credential, whether or not `gmail.check` is enabled: it is the
    user's own act at this computer, not a request from Lucille, and reads no mail.
  - It is refused, and starts nothing, while the host is paused (`paused`, exit 1), so that `pause` still
    stops it starting anything; and while keyring's own settings turn the keyring off (`insecure_keyring`,
    exit 1), as the operation is.
  - As the rest of the verb, it writes text, or with `--json` the objects of spec/CLI.md §7 (a
    `credential_test`, or an `error` with the code below). On success, one line, `gmail/<name>: signed in to
    imap.gmail.com`, and exit 0. Otherwise the child's fixed line and exit 1, with the code named by the
    child's exit: `bad_credential` (2), `sign_in_refused` (7), `unreachable` (4), `limit_reached` (5) or
    `internal` (1, 3 and 6; with exit 1, the interpreter's own failure, the line is the host's, never the
    traceback). It never shows the value.
  - The audit log gets one `credential_test` line: the service, the name, and the code, never the value.
  - It is the credential verb's only action that uses a value, and it does so only through the child.
- The credential's name is `default`. Lucille sends `credential: "default"`.
- The command line stays as it is for everyone else: `lucille-host credential set gmail <name> --host
  imap.gmail.com`.

**What crosses the device boundary.** The app password never does: it goes only to `imap.gmail.com`, in
`LOGIN`. What an operation returns does, to Lucille, as every reply does (PRIVACY.md): mailbox names,
search results' headers, and, for `gmail.read`, up to 32 KiB of a message's text and its attachments'
names, types and sizes. Lucille's operators, its assistant and the AI provider it uses can read them. So
the claim is that the credential stays on the computer, never that the mail does. Keeping a message's
text on the computer would need the model to run here, which this record does not cover.

**What a compromised Lucille can do,** while these operations are on: read any message of the account,
50 at a time, and search it. It cannot send, delete, move or mark anything, and cannot learn the app
password. Two consequences go in THREAT_MODEL.md:

- **Taking over other accounts.** A mailbox holds password-reset links and one-time codes. A compromised
  Lucille that can ask another service to reset a password, or to send a code, can then search for the
  message and read it, and so take over that account. Nothing here stops that while `gmail.search` and
  `gmail.read` are on; `pause`, and turning them off, do.
- **Instructions in mail.** A message's text, hidden text that gets past the rule above included, can hold
  instructions aimed at Lucille's assistant. That is Lucille's to handle.

**The claim documents, with the implementation.** As for `github.read`, each changes in the pull request
that builds these operations: PRIVACY.md (the child's credential; what leaves; the connection to
`imap.gmail.com`), CLAIMS.md (the catalog's entries; rows for the tests), THREAT_MODEL.md (the row above),
decisions/0001 (the transport section), spec/CLI.md (§8's words; §15's `credential test`), macos/README.md
(the Gmail section), and this record's "Unreachable from Lucille".

**The questions this design left open, as the repository owner settled them on 2026-11-18.** Gmail is the
host's first protocol other than HTTPS and its first access to mail, so these were put to the owner before
merge:

1. **Which commands and which mailboxes.** The commands and fetch items under "IMAP, exactly", read-only.
   Any mailbox the account shows to IMAP, opened only with `EXAMINE`; none is held out of reach, and there
   is no list of allowed labels.
2. **The transport.** TLS from the first byte on port 993 only: no `STARTTLS`, so nothing to downgrade, and
   no port 143. The certificate is checked for `imap.gmail.com` against certifi's bundle, and the address
   against `github.read`'s fixed list.
3. **Bodies.** At most 4 MiB of a message fetched; its text cut at 32 KiB, header values at 1 KiB and
   recipient lists at 4 KiB; the app password replaced, raw, spaced and in base64 at three alignments, in
   all output, and the host's own redaction of child output (§13) runs as well.
4. **Attachments.** Each one's file name, content type and size; never its contents, and no operation reads
   one.
5. **The app password's scope and revocation.** It opens the whole account over IMAP (Google offers nothing
   narrower), so its scope here is these operations' read-only rules. Google revokes it when the account's
   password changes; the user revokes it at Google's app-password page; Capsule's Remove or `credential
   remove` deletes it here. A revoked one makes each operation exit 7. Capsule's Gmail screen gives no
   warning when one is saved: its fields, and the link to Google's page on app passwords.
6. **`credential test`.** It runs whether or not `gmail.check` is enabled, and is refused while the host is
   paused, so that `pause` still stops it starting anything.

**Field check, not a claim.** That a real app password signs in from a Mac, through Capsule's bundled
runtime, and that the Keychain gives the child the value without a prompt, is checked with a dedicated
test account on the repository owner's Mac, with the implementation's first build.

## Entry: Lucille asks, the user types it here (chosen 2026-11-11; the host's side built 2026-11-27)

Lucille may ask the host to collect a credential. For example: "ask the user for `gmail/app-password`,
for hosts `imap.gmail.com` and `smtp.gmail.com`". The request carries no value. The user can also add
one unprompted, with `lucille-host credential set` (spec/CLI.md §15).

**The risk this creates is phishing through our own program.** A request Lucille sends is just a
request. If Lucille, or anyone who controls Lucille's account or servers, could ask for "your bank
password" bound to a host it controls, rule 1 of the proxy would send the password there. So:

- **The prompt is shown by the host, on the computer, never in chat.** A native dialog in the Mac app,
  or the terminal for the command-line host. Lucille cannot draw it and cannot fill it in.
- **The hosts are shown as the user's decision.** The dialog lists every host the credential will be
  sent to, as plain domain names the user approves. Lucille's request proposes them; the user's OK
  binds them. Lucille cannot add a host later. Changing hosts means asking again.
- **Lucille's own words are marked as Lucille's.** Any reason text it sends is shown under "Lucille
  says:", apart from the service, name and hosts, which the host lays out itself.
- **Declining is the default.** Closing or ignoring the dialog declines. Lucille learns only "saved"
  or "declined". Repeated requests for the same credential are rate-limited, so Lucille cannot nag.
- **The reply never echoes the value,** and the request-to-collect is logged in the usage log like a
  use.

What stays true: the value is typed only on this computer, into this program, and Lucille never receives
it. What does not: a user who approves a host they should not have will send their password there. The
dialog's job is to make that choice plain, and it cannot make it for them.

**What is built (2026-11-27).** The host's side: the operation, its child, the command line's prompt and
the actions a shell answers through. Capsule's dialog is not built; until it is, an ask is answered in a
terminal. Where the text above left something open, the smallest safe form was chosen, and is marked
*(chosen here)*.

- **The operation.** `credential.ask {service, name, reason?}`, off by default, like every operation that
  touches a credential (spec/PROTOCOL.md §10, §11). It carries no value. Its child,
  `src/lucille_host/asks.py`, runs as `github.read`'s does (`<the host's interpreter> -I -B -W ignore -X
  utf8 -m lucille_host.asks --state <state folder> --profile <profile> -- <args>`), with `CHILD_ENV` and
  nothing copied: it reads and writes no credential, does not import `credentials`, and connects to
  nothing. It writes the ask to `credential-asks.json` in the state folder, under a lock of its own, waits
  for the answer, and writes `{"answer": "saved"}` or `{"answer": "declined"}`, exit 0. That is all Lucille
  learns.
- **No hosts in the request** *(chosen here)*. The example above has Lucille propose hosts. Instead, an ask
  names only a service the host has a template for (`src/lucille_host/services.py`, rule 4 of
  "Hardening": `github` and `gmail`), and the credential is bound to that template's hosts and no other
  (`api.github.com`, `imap.gmail.com`). The host still lists them, as the user's decision. A credential for
  any other host can be bound only by the user's own `credential set`. So "your bank password, bound to a
  host Lucille controls" cannot be asked for at all: the risk this section names is closed for asks, not
  only made plain. A host Lucille needs later comes with a template, in a release.
- **Only the template's form is saved** *(chosen here)*. `credential answer` refuses a value not in the
  form its service's template says (`bad_value`), so an account's own password typed by mistake is not
  saved, and the ask still waits.
- **The prompt.** `lucille-host credential asks` shows each waiting ask, and `lucille-host credential answer
  <id>` answers one at a terminal: the service, the name, the hosts, what to type and where to make one,
  whether it replaces one saved, and how long it waits, laid out by the host; then, last, Lucille's words,
  each of their lines starting `Lucille says: `; then "Type yes to save it", where anything but `yes`
  declines, and the value, typed twice, unseen. A shell answers with `--value-stdin` or `--decline`, having
  shown the ask itself (spec/CLI.md §15). A `run` in a terminal does not interrupt itself to ask: the user
  runs `credential asks` *(chosen here)*.
- **Lucille's words** *(chosen here)*: at most 300 characters. Rule `credential-ask-args` refuses
  (`bad_args`) a reason with a control character, a line break, a character that reorders text, white
  space other than a plain space, a character of no width or one drawn blank (spec/PROTOCOL.md §10 lists
  them), a space at its start or end, two spaces in a row, or only white space. The host cuts Lucille's
  words itself, at their spaces, into lines of at most 64 columns (a character beyond ASCII counts two),
  and starts each with `Lucille says: `, so each fits in 78 columns. So no line the host prints of
  Lucille's words starts as one of the host's does: the host's own detail lines start with two spaces,
  and each of Lucille's with `Lucille says:`. A terminal narrower than 78 columns wraps lines itself, the
  host's as well; with no run of spaces, a line it wraps starts with at most one space.
- **Declining is the default.** An ask waits 240 s, inside the operation's 270 s time limit, then is
  declined *(chosen here)*. Rate limit *(chosen here)*: one ask waits at a time, and an ask made less than
  10 minutes after any ask ended, for any credential, is declined at once, so asking under one name after
  another does not get around it; neither is shown. While an ask waits, `run` runs no other request: it
  runs one at a time *(accepted here: the user is at the computer, and Lucille waits on the answer
  anyway)*.
- **Logged like a use.** The request is in the audit log as any request is (`received`, `execution`), and
  each answer writes a `credential_answer` line: the service, the name and the answer, never the value.
  An ask that lapsed while its value was typed is logged with the answer `lapsed` and whether the
  credential was saved all the same.
- **Not built.** Capsule's dialog for an ask, which shows it when `run` reports a `credential.ask` request
  `started`, reads it with `credential asks --json` and answers it with `credential answer`; closing it
  declines. It is the Mac app's part, in a change of its own. Until then Capsule has no switch for
  `credential.ask`, in its menu or in Set Up *(chosen here)*: turned on there, an ask would wait for an
  answer Capsule never asks for, and `run` would run nothing else until it lapsed. It is turned on with
  `lucille-host ops enable credential.ask`.

## Hardening (chosen 2026-11-11; rules 1 to 4 built 2026-11-27, in the forms below)

The proxy and the entry prompt protect the value. These narrow what a request can do with it, who
else on the computer can reach it, and what a bad update could do.

**What is built, rule by rule (2026-11-27).** Where a rule left its form open, the smallest safe one was
chosen:

- **1, risk tiers:** every operation that uses a credential reads and changes nothing, so each runs on its
  own once enabled. The tier comes from the service template, never from Lucille: a template lists its
  operations as `reads`, and `services.Service` refuses, when it is defined, a template that lists any
  operation that changes something (`changes`), since the prompt before each use that such an operation
  needs is not built. A test holds that every operation reading a credential is a read of exactly one
  template (`tests/test_asks.py`).
- **2, narrow credentials:** each template names where the user makes the narrowest credential its
  service offers (a fine-grained GitHub token; a Google app password), which `credential asks` and the
  warnings show. `credential set` warns, on standard error and never naming the value, when the value is
  not in its template's form (which is what an account's own password looks like here: the children never
  send it), when the hosts leave out the template's, and when the service has no template, so nothing
  uses it. It is saved all the same: it is the user's own act. An ask refuses such a value instead.
  OAuth sign-in on the computer is not built: no service here uses OAuth.
- **3, no local network:** built with the first use: `hosts_of` refuses IP literals, `localhost` and the
  local suffixes, and each child checks every resolved address against its fixed list and connects only
  to a vetted one ("First use", step 2 and 3). The children speak only TLS.
- **4, service templates:** `src/lucille_host/services.py`. A template holds the service's hosts, the
  operations that use it, and the form of its value, which the children check (`proxy.TOKEN` and
  `gmail.VALUE` are the template's). The request shapes each allows are the operation's own: `github.read`'s
  path list and the Gmail operations' commands. A service with no template cannot be used, and cannot be
  asked for.
- **5, the keychain item opens only for this app:** not built. It needs a stable signature, which ad hoc
  builds do not have.
- **6, pause, expiry, notifications:** `pause` (and `kill`) stops every use, from the computer, and was
  built already. Not built: approvals that lapse after 30 days, notifications on a first use and on
  unusual volume, and a per-credential cap for `github.read` (the Gmail operations have theirs, 60
  sign-ins in 10 minutes). Lapsing approvals and notifications need Capsule to renew and to notify; built
  on the host alone, a lapse would turn a credential off with no way in the app to renew it.
- **7, updates:** not built. The host installs no update; a warning before an update that changes the
  credential or proxy code belongs to whatever installs one.

**As chosen:**

**With the first use PR:**

1. **Risk tiers per credential, chosen by the user.** Reads run on their own. Anything that changes
   something (sends, pays, deletes, edits) shows a prompt on the computer first, and declines on close
   or timeout. Whether a request is a read comes from the service template below, never from Lucille.
   This covers the risk the proxy leaves open: Lucille, or whoever controls it, acting as the user.
2. **Narrow, revocable credentials first.** Where a service offers app passwords or OAuth, the host
   points the user there. For OAuth, the host runs the sign-in on the computer with the narrowest
   scopes, so the token is created here and seen nowhere else. Saving what looks like an account's main
   password draws a warning.
3. **No local network.** The proxy refuses plain HTTP, IP literals, `localhost`, link-local and private
   addresses. It checks the resolved address on every connection, so a name cannot be re-pointed at a
   device on the user's network between the check and the call.

**With each service:**

4. **Service templates.** A credential is bound to the request shapes its service's reviewed template
   allows (method and path patterns, and which are reads), not to every request to its hosts. A service
   with no template cannot be used.

**Separately, each with its own item:**

5. **The keychain item opens only for this app.** On macOS, an access-control list on the signing
   identity, and optionally Touch ID or the login password once per session. Needs a stable signature,
   which ad hoc builds (0002) do not have.
6. **Pause, expiry, notifications.** One control on the computer stops every use. Approvals lapse
   after 30 days unless renewed. A notification on a credential's first use and on unusual volume, and
   a per-credential rate cap.
7. **Updates.** A malicious update could read every credential whatever the rules say, so updates are
   the last trust point. Rebuildable, signed releases are already planned; on top of them, the host
   warns before installing an update that changes the credential or proxy code.
