# 0003: Local credentials (draft)

**Status: storage, entry from the command line, and the first use, `github.read`, built; entry at
Lucille's request chosen, not built.** `src/lucille_host/credentials.py` can save, list, read and
remove a credential for an online service, kept only on this computer. `lucille-host credential set`,
`list` and `remove` write to it from the command line, each credential bound to the hosts it may be
sent to (spec/CLI.md §15). One operation uses a stored credential: `github.read`, off by default, whose
child process reads the one credential a request names and sends it to `api.github.com` alone ("First
use", below). Nothing Lucille sends can list or change a credential, nor read one any other way. This
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
- **Unreachable from Lucille, but through `github.read`.** No runtime module but `credentials.py`
  imports it, except the command line's `credential` verb, in its own function, and `github.read`'s
  child, `proxy.py`, in the one function that reads the credential. So no other verb's process, `run`
  among them, loads it; the verb never reads a value back; and only the child reads a value, on a
  request for an enabled `github.read` (`tests/test_credentials.py`,
  `test_only_the_credential_verb_and_the_proxy_import_it`,
  `test_importing_the_command_line_does_not_load_it`, `test_the_credential_verb_never_reads_a_value`).
  No request or reply can write or list it, and only `github.read` can have one read.

## What was open, and how it was decided

1. **How a credential gets in.** Decided 2026-11-11: see "Entry: Lucille asks, the user types it here"
   below. The value is entered on the computer and never passes through Lucille.
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
- **Keyring's off switch, in the child.** The child reads keyring's off switch as the host does
  (`store.check_backend`), but from its own environment: `PYTHON_KEYRING_BACKEND` is not copied, and on
  Linux the `keyringrc.cfg` is looked for under `~/.config` only, since `XDG_CONFIG_HOME` is not copied
  either. So a host started with keyring turned off by that variable, or by a `keyringrc.cfg` under a
  custom `XDG_CONFIG_HOME`, does not turn it off for the child, which then reads the system keyring. The
  credential store refuses every backend but the system's own in either process, so this changes which
  keyring is asked, never whether a file backend is.

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
   is not read at all (exit 5, no body), since the scrub cannot see into a compressed body. The reply carries at most what §13 of the protocol allows any child's output (64 KiB
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

## Entry: Lucille asks, the user types it here (chosen 2026-11-11, not built)

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

## Hardening (chosen 2026-11-11, not built)

The proxy and the entry prompt protect the value. These narrow what a request can do with it, who
else on the computer can reach it, and what a bad update could do.

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
