# spec/

The protocol between Lucille and lucille-host, `lucille.exec/1`, and the contract between the
`lucille-host` command line and any app that drives it.

- [PROTOCOL.md](PROTOCOL.md): the protocol, in prose.
- [PROTOCOL-CHANGELOG.md](PROTOCOL-CHANGELOG.md): every change to it, and the versioning rule.
- [`src/lucille_host/spec/v1/`](../src/lucille_host/spec/v1/): the protocol's JSON Schemas and golden
  fixtures.
- [CLI.md](CLI.md): the command line's `--json` contract, with its own version and its own changes.
- [`src/lucille_host/spec/cli/v1/`](../src/lucille_host/spec/cli/v1/): its JSON Schemas and golden fixtures.

The schemas and fixtures live inside the package so that the wheel ships them; `lucille_host.spec`
reads them from wherever the package is installed.
