# 0003: Local credentials (draft)

**Status: storage built; entry and use chosen, not built.** `src/lucille_host/credentials.py` can save, list and remove a credential
for an online service, kept only on this computer. Nothing yet writes to it from the command line, and
nothing Lucille sends can reach it. This record says what was built, and what is still to be decided
before a credential kept here can be used.

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
- **Only the system keyring.** `credentials.check_backend` accepts a backend only if its exact class is
  one it names: on macOS the Keychain (`keyring.backends.macOS.Keyring`), on Linux the Secret Service,
  libsecret or KWallet. A chainer passes only if every backend it chains is one of those. The check
  runs on every read and every write, so a backend picked through `PYTHON_KEYRING_BACKEND`, a
  `keyringrc.cfg` or an installed plugin is refused unless it is one of those, whatever it is called.
  This is stricter than the pair's rule (`store.check_backend`), which refuses only backends it knows
  to be unfit. There is no file fallback like `--store-file`.
  - What this does not cover: code running as the user that replaces the trusted class itself, for
    example a `.pth` file that patches keyring at start-up. That code could also read the value from
    this program's memory, so no check inside the program can stop it.
- **Names, never values.** Service and credential names follow Lucille's rule (`^[a-z0-9][a-z0-9._-]{0,63}$`).
  `names()` returns names and save times only. Error messages hold nothing read from the item, and a
  value is returned only by `value()`.
- **An item this version did not write is left as it is.** It may hold the only copy of the user's
  credentials.
- **Unreachable from Lucille.** No runtime module but `credentials.py` imports it
  (`tests/test_credentials.py`, `test_nothing_else_in_the_runtime_imports_it`). No request, operation or
  reply can write, read or list it.

## What was open, and how it was decided

1. **How a credential gets in.** Decided 2026-11-11: see "Entry: Lucille asks, the user types it here"
   below. The value is entered on the computer and never passes through Lucille.
2. **Whether Lucille learns the names.** Decided 2026-11-11: yes. Lucille may learn a credential's
   service, name and bound hosts, never its value. The claim gains the sentence: "Lucille learns the names
   of the credentials you saved here, never their values."
3. **How a credential is used.** Decided 2026-11-11: see "Use: the host as a
   credential proxy" below.

## Use: the host as a credential proxy (chosen 2026-11-11, not built)

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

## Entry: Lucille asks, the user types it here (chosen 2026-11-11, not built)

Lucille may ask the host to collect a credential. For example: "ask the user for `gmail/app-password`,
for hosts `imap.gmail.com` and `smtp.gmail.com`". The request carries no value. Once this is built, the
user would also be able to add one unprompted, with a `credential set` command that does not exist yet.

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
