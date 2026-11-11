# 0003: Local credentials (draft)

**Status: storage built; use chosen (credential proxy), not built.** `src/lucille_host/credentials.py` can save, list and remove a credential
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
- **The same keyring rule as the pair.** `store.check_backend` refuses a backend that stores nothing or
  stores in the clear. There is no file fallback like `--store-file`: a credential is never written to
  a plain file by this program.
- **Names, never values.** Service and credential names follow Lucille's rule (`^[a-z0-9][a-z0-9._-]{0,63}$`).
  `names()` returns names and save times only. Error messages hold nothing read from the item, and a
  value is returned only by `value()`.
- **An item this version did not write is left as it is.** It may hold the only copy of the user's
  credentials.
- **Unreachable from Lucille.** No runtime module but `credentials.py` imports it
  (`tests/test_credentials.py`, `test_nothing_else_in_the_runtime_imports_it`). No request, operation or
  reply can write, read or list it.

## What is not decided

1. **How a credential gets in.** The value must be entered on the computer and must never pass through
   Lucille, or this would not be the private option. The likely shape is `lucille-host credential set
   <service> <name>`, reading the value from stdin with no echo, plus `list` and `remove`. That needs
   `spec/CLI.md` objects for `--json`, and the Mac app's panel. Not built.
2. **Whether Lucille learns the names.** Lucille's backend keeps a row for each credential the user said
   is on their computer, so it can show the user where each one is. Whether the host reports its names
   (for example in `host.describe`) or the user tells Lucille, is open. Reporting names would add a
   sentence to the claim: "Lucille learns the names of the credentials you saved here, never their values."
3. **How a credential is used.** Decided 2026-11-11 by the operator: see "Use: the host as a
   credential proxy" below.

## Use: the host as a credential proxy (chosen 2026-11-11, not built)

The operator chose this over two alternatives. A local program that asks the host for a credential
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
