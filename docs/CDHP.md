# CDHP — Claude Dynamic Hook Protocol

**Version: 1.0**

CDHP defines how a router fans Claude Code hook events out to one or more
standalone HTTP handlers, and how those handlers reply.

The handler-side wire is specified machine-readably in
[`openapi.yaml`](./openapi.yaml). This document covers the rest: the router's
behavior, chain semantics, and operational defaults that aren't part of the
handler's HTTP surface.

## Roles

```
Claude Code  ──HTTP POST──►  router-daemon  ──HTTP POST──►  handler 1
                              (port 8765)   ──HTTP POST──►  handler 2
                                            ──HTTP POST──►  handler N
```

- **Router** (`cdh-daemon`) listens on `127.0.0.1:8765` (configurable). Claude
  Code's `"type": "http"` hook posts payloads here.
- **Handler** is an HTTP/1.1 server you run yourself (manual / systemd /
  container). It implements `POST /hooks/<event>` and `GET /health` per
  `openapi.yaml`. `/health` is required: `cdh list-handlers` calls it to
  report alive/down status. Events are still declared statically in router
  config — `/health` is for liveness, not capability discovery.

## Wire shape (handler-side, summary)

| Endpoint | Request | Response |
|---|---|---|
| `POST /hooks/<event>` | `{"payload": "<json-string>"}` | `{"envelope": "<json-string>" \| null}` |
| `GET /health` | — | `{name, protocol_version, events, uptime_s}` |

`payload` and `envelope` are JSON strings. Handler decodes `payload` with its
own JSON parser; emits `envelope` the same way. Router treats both as opaque.

This decouples the wire from Claude's hook payload schema — Claude can evolve
its envelope shape without touching CDHP.

See `openapi.yaml` for full schemas, examples, and error responses.

## Event declaration (router config)

Handlers declare which events they answer for in the **router's** config, not
in the handler itself. The router only POSTs declared events:

```toml
[[handler]]
name = "read_once"
url = "http://127.0.0.1:9001"
events = ["preToolUse"]
```

The router does not probe handlers. If a handler is configured for an event
it doesn't actually implement, the request hits the handler and presumably
returns a 404 or error — chain treats that as null per failure modes below.

## Chain semantics (router)

For each event, router POSTs each handler that declared the event in config,
in registration order:

- **Sequential.** Handler N+1 is not called until handler N responds (or
  times out).
- **Last non-null wins.** Each handler's envelope (if non-null) overwrites the
  prior result. `envelope: null` is "no opinion" — chain continues with the
  prior result intact.
- **Terminal short-circuit.** If a handler is configured `terminal = true`
  and returns a non-null envelope, the chain stops and that envelope is
  returned to Claude.
- **Errors are non-blocking.** Timeout, connection refused, non-2xx, or
  malformed `envelope` field → router logs + treats as null → chain
  continues. Single broken handler does not break the chain.
- **All-null fallback.** If every handler abstains, router applies
  `[hook_defaults.<event>]` from config (if present), else returns `{}` to
  Claude.

## Configuration (router)

`~/.config/cdh/config.toml`:

```toml
[daemon]
port = 8765
request_timeout_s = 5.0
wire_max_bytes = 1048576
max_workers = 16

[[handler]]
name = "read_once"
url = "http://127.0.0.1:9001"
events = ["preToolUse"]
terminal = false   # optional; default false. Short-circuit chain on non-null reply.

[hook_defaults]
# Optional fallback when every handler abstains. Default: passthrough
# (router returns `{}`, Claude Code applies its own permission flow).
# preToolUse = "ask"   # opt into a fail-safe ask gate
```

`request_timeout_s` and `wire_max_bytes` are router-global — they apply to
every handler call. `terminal` is per-handler.

`[daemon]` value bounds (out-of-range raises `ValueError` on `cdh start`):

| Field | Range | Default |
|---|---|---|
| `port` | 1 – 65535 | 8765 |
| `request_timeout_s` | 0.1 – 60.0 | 5.0 |
| `wire_max_bytes` | 1024 – 67108864 | 1048576 (1 MiB) |
| `max_workers` | 0 – 1024 | 16 (set `0` for unbounded) |

`max_workers` bounds the inbound request thread pool. Excess concurrent
requests queue inside the executor instead of spawning new threads.

### Hot config reload

The daemon watches `~/.config/cdh/config.toml` (via `watchdog`) and
atomically swaps the live handler list, hook defaults, and per-call wire
timeout / byte cap whenever the file changes. No restart needed for
adding/removing handlers or editing event lists.

`[daemon].port` is bound at startup and is not hot-reloadable; changing
it requires `cdh stop && cdh start`. Likewise the inbound `MAX_CONTENT_LENGTH`
(set from `wire_max_bytes` at startup) is fixed for the daemon's lifetime;
hot-reloaded `wire_max_bytes` only affects the router→handler hop.

If a save produces invalid TOML, the parse error is logged and the prior
chain keeps serving — fix the file and the next save reloads cleanly.

## Failure modes

| Condition | Router behavior |
|---|---|
| Handler down (ECONNREFUSED) | Step returns ~1ms on loopback; treated as null; chain continues. |
| Handler hung (TCP accepted, no response) | Step times out per `timeout_s`; treated as null; chain continues. |
| Handler 5xx / non-2xx | Step treated as null; chain continues. |
| Handler returns missing/malformed `envelope` field | Step treated as null + logged. |
| Router itself down | Claude's HTTP hook treats connection-refused as non-blocking (fail-open default). |
| Router under high concurrent load | Inbound requests are served from a fixed-size thread pool (`[daemon].max_workers`, default 16). Excess requests queue inside the executor; set `max_workers = 0` to revert to the old unbounded werkzeug behavior. |

## Versioning

`protocol_version` is `"1.0"`. Future incompatible wire changes bump the
major (`"2.0"`).

## Authoring a handler in another language

Implement `POST /hooks/<event>` per `openapi.yaml`. Bind on `127.0.0.1:<port>`.
Register in router config with `[[handler]] name = "..." url = "..." events = [...]`.
Smoke-test with `curl` against the spec.

A Python reference (Flask) lives at
`examples/handlers/read_once/read_once.py`.
