# Changelog

All notable changes to this project will be documented in this file. Format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] — 2026-05-02

### Added

- **Hot config reload.** Daemon now watches `~/.config/cdh/config.toml`
  (via `watchdog`) and atomically swaps the live handler list, hook
  defaults, and per-call wire timeout / byte cap on every save. No
  restart needed for adding/removing handlers or editing event lists.
  Invalid TOML is logged and the prior chain keeps serving until the next
  valid save. `[daemon].port` and the inbound `MAX_CONTENT_LENGTH` are
  still pinned at startup.
- **Bounded inbound thread pool.** New `[daemon].max_workers` field
  (range 0–1024, default 16). Excess concurrent requests queue inside
  the pool instead of spawning unbounded werkzeug threads. Set
  `max_workers = 0` to revert to the prior unbounded behavior.
- **`cdh test <handler> <event>`.** POST a synthetic event payload
  directly to one handler, bypassing the chain. Accepts inline
  `--payload`, `--payload-file`, or `--payload -` (stdin).
- **`cdh logs [-n N] [-f]`.** Tail or follow the daemon log file
  without needing to know its path.

### Changed

- Added `watchdog>=4.0` to runtime dependencies (required for the
  config-file watcher; ~zero transitive deps on Linux/macOS).

## [1.0.1] — 2026-05-02

### Changed

- **Handler config schema flattened.** Replace `[handler.events.<event>]`
  per-event tables with a flat `events = [...]` array on each `[[handler]]`
  block, plus an optional top-level `terminal = true|false`. Per-event
  `timeout_s` and `max_bytes` overrides are removed — wire timeout and byte
  cap come from `[daemon]` globals only. Existing configs using the table
  form must be rewritten.
- **Built-in `preToolUse` default flipped from `ask` → passthrough.** When
  every handler abstains and no user-configured default exists, router now
  returns `{}` instead of an `ask` envelope. Claude Code's own permission
  flow handles the request. Users who relied on cdh as a fail-safe ask gate
  can opt in explicitly:
  ```toml
  [hook_defaults]
  preToolUse = "ask"
  ```
  Motivation: the old default forced a confirmation prompt for every
  tool call (Bash, Edit, Write, …) when only a Read-only handler was
  configured — surprising and hostile to the common case.

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
[1.0.1]: https://github.com/phansen314/claude-dynamic-hooks/releases/tag/v1.0.1
