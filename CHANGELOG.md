# Changelog

All notable changes to this project will be documented in this file. Format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.1] — 2026-05-03

### Fixed

- **Chunked-encoding body loss (B1).** `POST /hooks/<event>` used
  `if request.content_length:` to gate JSON parsing. Chunked-encoded
  requests carry no `Content-Length`, so the body was silently discarded
  and handlers received `{}`. Fixed by calling `request.get_json()` on
  every request regardless of content-length presence.
- **`process_lock.acquire()` duplicated PID read (B2).** Lock-contention
  path re-read the PID file inline instead of reusing the existing
  `holder_pid()` helper. Now calls `holder_pid()` directly and uses
  `LOCKED_UNKNOWN_PID` to emit a clearer error message when the PID file
  is corrupted.

### Added

- **Config reload status on `/health` (D1).** `/health` response now
  includes a `"config"` block with `last_reload_at`, `last_reload_ok`,
  and `last_reload_error`. On a failed reload the prior handler chain
  keeps serving, and the snapshot is updated with `last_reload_ok: false`
  so operators can detect the failure without tailing logs.
- **`cdh status` surfaces reload failures (D1).** If the daemon is
  running but the last config reload failed, `cdh status` prints
  `CONFIG RELOAD FAILED: <detail>` to stderr (best-effort probe via
  `/health`; silent if the probe fails).
- **Router→handler error-body preview in logs (D7).** When a handler
  returns a non-2xx response, the client now streams up to 200 bytes of
  the response body and includes it in the warning log line, making
  handler-side errors diagnosable without extra curl runs.

### Changed

- **Structured logging with rotation (D2).** All daemon log output
  (access log, chain steps, config-watcher events, client warnings) now
  goes through Python's `logging` module instead of bare
  `sys.stderr.write`. Log format: `YYYY-MM-DD HH:MM:SS LEVEL name:
  message`. `daemon.log` rotates at 10 MiB, keeps 3 backups.
  `sys.excepthook` is wired to log uncaught exceptions at CRITICAL.
- **`Snapshot.max_body_bytes` → `outbound_max_bytes` (D4).** Field
  rename clarifies that the live-reloadable byte cap applies to the
  router→handler hop only; the inbound Flask cap (`MAX_CONTENT_LENGTH`)
  stays pinned at the startup value.
- **Handler URL validation uses `urlparse` (D6).** The previous
  `url.startswith(("http://","https://"))` check accepted bare scheme
  strings like `http://` (no host). Replaced with `urlparse` checking
  both scheme and `netloc`, with a clearer error message.
- **Dead `_BUILTIN_DEFAULTS` dict removed (D5).** Empty dict was
  assigned, immediately copied, and discarded. Initialization now uses
  `{}` inline.

### Internal

- **Async-signal-safe shutdown (B3).** Signal handlers in both the
  daemon and the `read_once` example handler previously spawned threads
  from signal context (`threading.Thread(...).start()`), which is not
  async-signal-safe. Both now write a byte to an `os.pipe()` from the
  signal handler and block on the read end in a dedicated monitor thread
  that calls `server.shutdown()`. Eliminates a theoretical deadlock on
  lock-internal thread spawning.

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
  `~/.config/cdh/config.toml` via `events = [...]` on each `[[handler]]`
  block. `/health` is used by `cdh list-handlers` for liveness only.
- Wire on the router↔handler hop is opaque: `{"payload": "<json-str>"}` ↔
  `{"envelope": "<json-str>"|null}`. No Claude-schema typing on this hop.
- No SDK. Handlers are HTTP/1.1 servers in any language.
- Minimal runtime deps: Flask + requests + watchdog.

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
