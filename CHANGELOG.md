# Changelog

All notable changes to this project will be documented in this file. Format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-05-02

Initial stable release.

### Versioning policy

- **Package version** (this file, `pyproject.toml`): SemVer. Breaking changes
  to the CLI, config schema, or wire protocol bump the major.
- **CDHP wire protocol** (`docs/openapi.yaml`, `HealthResponse.protocol_version`):
  pinned at `1.0`. A wire bump is necessarily a package major bump.

### Frozen architectural invariants

The following invariants are stable as of 1.0 and won't be reverted (see
`CLAUDE.md` for full rationale):

- Router does not spawn handlers — operators run them.
- Router does not probe handlers for events — events declared statically in
  `~/.config/cdh/config.toml` under `[handler.events.<event>]`.
- Wire on the router↔handler hop is opaque: `{"payload": "<json-str>"}` ↔
  `{"envelope": "<json-str>"|null}`. No Claude-schema typing on this hop.
- No SDK. Handlers are HTTP/1.1 servers in any language.
- Minimal runtime deps: Flask + requests.

### Added

- `cdh start` / `stop` / `status` / `list-handlers` CLI.
- Sequential chain semantics with terminal short-circuit and last-non-null
  result selection (`router_chain.run`).
- Per-handler-per-event config overrides: `terminal`, `timeout_s`, `max_bytes`.
- Per-event built-in defaults (`hook_defaults`); PreToolUse defaults to `ask`.
- `XDG_CONFIG_HOME` / `XDG_STATE_HOME` support, with `CDH_CONFIG_DIR` /
  `CDH_STATE_DIR` overrides taking precedence.
- Reference handler `examples/handlers/read_once/` with systemd unit.
- `docs/openapi.yaml` machine-readable handler contract; `docs/CDHP.md`
  protocol overview.
- Spec drift guard test (`tests/test_spec_examples.py`).

### Notes for handler authors

- Implement `GET /health` returning `{name, protocol_version: "1.0", events,
  uptime_s}` and `POST /hooks/<event>` accepting `{"payload": "<json-str>"}`
  and returning `{"envelope": "<json-str>"|null}`.
- Bring your own dependencies; do not install into the cdh router venv.
- Bind on TCP loopback by convention.

[1.0.0]: https://github.com/phansen314/claude-dynamic-hooks/releases/tag/v1.0.0
