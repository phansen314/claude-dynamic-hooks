# claude-dynamic-hooks

HTTP-based router for Claude Code hooks. Each handler is a standalone HTTP/1.1 server in any language. You declare which events each handler answers for in `~/.config/cdh/config.toml`; the router fans Claude's hook payloads out to those handlers in registration order.

```
Claude Code ──POST /hooks/<event>──► cdh router (127.0.0.1:8765)
                                          │
                                          │ POST /hooks/<event>  in registration order
                                          ▼
                                       handler 1, handler 2, … (your URLs)
```

Reference Python implementation of **CDHP** (Claude Dynamic Hook Protocol). Polyglot by design — handlers don't import this package. Spec: [`docs/openapi.yaml`](docs/openapi.yaml).

## Install

```bash
uv tool install git+https://github.com/phansen314/claude-dynamic-hooks@v1.1.0
```

Installs the `cdh` and `cdh-daemon` commands into `~/.local/bin` (uv-managed isolated venv). Pulls Flask + Werkzeug + watchdog for the router; handlers in any language are unaffected — these deps live in the router only.

Upgrade later with:

```bash
uv tool upgrade claude-dynamic-hooks
```

For a development setup (editable install, dev extras), see [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Quickstart

**1. Set up the example handler**

```bash
cd examples/handlers/read_once
uv sync
```

**2. Start the handler** — your responsibility (manual, systemd, container, …).

```bash
CDH_BIND_ADDR=127.0.0.1:9001 .venv/bin/python read_once.py &
```

A sample systemd user unit lives in `examples/handlers/read_once/README.md`.

**3. Register it in `~/.config/cdh/config.toml`**

```toml
[daemon]
port = 8765

[[handler]]
name = "read_once"
url = "http://127.0.0.1:9001"
events = ["preToolUse"]
```

The `events` array declares which events this handler answers for. Add `terminal = true` on the handler to short-circuit the chain when this handler returns a non-null envelope.

`[daemon]` accepts `port` (1–65535, default 8765), `request_timeout_s` (0.1–60.0, default 5.0), `wire_max_bytes` (1024–67108864, default 1048576), and `max_workers` (0–1024, default 16; set `0` for an unbounded pool). Out-of-range values fail loudly on `cdh start`.

Edits to `config.toml` are applied live — the daemon watches the file and atomically swaps the handler chain, defaults, and wire timeout/cap on every save. Only `port` and the inbound `MAX_CONTENT_LENGTH` are pinned at startup.

**4. Start the router**

```bash
cdh start
cdh list-handlers
#   read_once — http://127.0.0.1:9001 — alive — events: preToolUse
```

**5. Wire Claude Code at `~/.claude/settings.json`**

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": ".*",
      "hooks": [{
        "type": "http",
        "url": "http://localhost:8765/hooks/preToolUse",
        "timeout": 5
      }]
    }]
  }
}
```

Restart your Claude Code session. `read_once` now sees every PreToolUse.

## Run under systemd (optional)

`cdh start` works fine for ad-hoc use, but for "always running" the cleanest path is a systemd **user** unit (no root needed; per-user state).

`~/.config/systemd/user/cdh.service`:

```ini
[Unit]
Description=cdh router daemon
After=network.target

[Service]
Type=simple
ExecStart=%h/.local/bin/cdh-daemon
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
```

Enable + start:

```bash
systemctl --user daemon-reload
systemctl --user enable --now cdh
systemctl --user status cdh
```

Survive reboots without an active login session:

```bash
loginctl enable-linger $USER
```

Notes:

- Don't run `cdh start` while the systemd unit is active — both fight for the PID lock; the second exits 1.
- `cdh-daemon` redirects its own stderr into `~/.local/state/cdh/daemon.log` via `dup2`, so the systemd journal only shows pre-startup errors. Use `cdh logs -f` for runtime logs.
- Hot config reload still works under systemd — edits to `~/.config/cdh/config.toml` apply live; no `systemctl restart` needed.
- Port changes need a restart: `systemctl --user restart cdh`.

## CLI

```bash
cdh start              # start router daemon
cdh stop               # stop router daemon
cdh status             # show router pid
cdh list-handlers      # list configured handlers + live health
cdh test <name> <ev>   # POST a synthetic event directly to one handler (bypasses chain)
cdh logs [-f] [-n N]   # show daemon log; -f to stream
```

## Writing a handler

A handler is a standalone HTTP/1.1 server. Pick a language, bind on `127.0.0.1:<port>`, implement one route. Authoritative spec: [`docs/openapi.yaml`](docs/openapi.yaml).

| Endpoint | Behavior |
|---|---|
| `POST /hooks/<declared event>` | request `{"payload": "<json-string>"}`, response `{"envelope": "<json-string>" \| null}` |
| `POST /hooks/<undeclared event>` | 404 |
| Bad body / missing `payload` field | 400 |
| `GET /health` | required; return `{"name", "protocol_version": "1.0", "events": [...], "uptime_s"}`. Used by `cdh list-handlers`. |
| `SIGTERM` | drain, close, exit 0 |

You declare which events the handler answers for in the **router config** (`events = [...]` on the `[[handler]]` block), not in the handler itself. The router only POSTs declared events.

`payload` and `envelope` are JSON strings — your handler decodes/encodes them with its own JSON library. The wire is intentionally opaque so Claude's envelope schema can evolve without touching CDHP.

By convention, handlers read `CDH_BIND_ADDR=127.0.0.1:<port>` from the environment. The router never sets this — you do, in your systemd unit / shell command / container env. The router only cares that `[[handler]] url = ...` reaches the handler.

See `examples/handlers/read_once/read_once.py` for a Flask-based Python reference (~190 LOC). Smoke-test your handler with `curl http://localhost:<port>/hooks/preToolUse -d '{"payload":"{}"}'` against the spec.

### Per-handler `terminal`

`terminal = false` by default. Set `terminal = true` on the handler to short-circuit the chain whenever this handler returns a non-null envelope:

```toml
[[handler]]
name = "bash_guard"
url = "http://127.0.0.1:9002"
events = ["preToolUse"]
terminal = true
```

Wire timeout and byte cap come from `[daemon].request_timeout_s` / `[daemon].wire_max_bytes` — they apply to every handler call.

## Chain semantics

For each event, router POSTs every handler that declared it (per config), in registration order:

- Sequential. Handler N+1 fires only after N responds (or times out).
- Last non-null `envelope` wins. `null` means "no opinion" — chain continues with the prior result intact.
- `terminal = true` short-circuits on non-null.
- Timeout / 5xx / connection-refused / malformed envelope → treated as null + logged. Chain continues.
- All-null → router applies `[hook_defaults.<event>]` if configured, else returns `{}` to Claude.

### Failure modes

- **Handler down** → POST returns ECONNREFUSED in ~1ms on loopback; chain treats step as null + logs, continues.
- **Handler restart while router runs** → seamless; first request after handler is up routes correctly. No probe latency.
- **Router itself down** → Claude treats connection-refused as non-blocking (fail-open default). Run the router under systemd if you need guaranteed enforcement.

## Architecture

```
~/.config/cdh/config.toml          ← [daemon] + [[handler]] entries (with declared events)
        │
        ▼
router-daemon (port 8765)           ← cdh-daemon entrypoint
   ├── Flask app                     (werkzeug WSGI server, threaded)
   └── router_chain.run()            (sequential POST fan-out)

handler N (your responsibility)     ← any language, any process manager
   └── HTTP server on its URL
```

Source under `src/claude_dynamic_hooks/`. Operational details in [`docs/CDHP.md`](docs/CDHP.md).

## Logs

```
~/.local/state/cdh/daemon.log    ← router stderr
```

Each line is one of:

- request log — `<ts> <method> <path> <status> <body_bytes>`
- chain step — `chain <event> → <name>: <outcome>`

Handler logs are wherever your process manager sends them (your stdout/stderr, journalctl, container log driver, etc.).

## License & changelog

MIT — see [LICENSE](LICENSE). Release history: [CHANGELOG.md](CHANGELOG.md). Contributing: [CONTRIBUTING.md](CONTRIBUTING.md).
