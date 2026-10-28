# spec/

The protocol between Lucille and lucille-host, `lucille.exec/1`.

- [PROTOCOL.md](PROTOCOL.md): the spec, in prose.
- [PROTOCOL-CHANGELOG.md](PROTOCOL-CHANGELOG.md): every change, and the versioning rule.
- [`src/lucille_host/spec/v1/`](../src/lucille_host/spec/v1/): the JSON Schemas and the golden fixtures.
  They live inside the package so that the wheel ships them; `lucille_host.spec` reads them from wherever
  the package is installed.
