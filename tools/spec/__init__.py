"""Tooling for the protocol spec: the generator of the fixtures for each place in each pattern.

- ``gen_fixtures``: writes those fixtures into ``src/lucille_host/spec/v1/fixtures/``, or, with
  ``--check``, reports where the committed files differ from what it writes.
- ``patterns``: names the places in a schema pattern, and changes one of them.

``tests/spec/test_generated_fixtures.py`` holds the committed fixtures to the generator.
"""
