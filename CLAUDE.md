# claude-dynamic-hooks

CDHP reference impl. HTTP-based router for Claude Code hooks. Polyglot handlers; zero coupling between router and handler process trees.

Authoritative wire spec: `docs/openapi.yaml`. Protocol overview: `docs/CDHP.md`.

## Architecture invariants

- **Router does not spawn handlers.** Operators run handlers (manual, systemd, container, …). Router only routes.
- **Router does not probe handlers.** Events are declared statically per handler in `~/.config/cdh/config.toml` under `[handler.events.<event>]`. Handler liveness is detected per-request (ECONNREFUSED is fast on loopback). `cdh list-handlers` does an on-demand `/health` probe just for UX.
- **Handlers don't import this package.** They're standalone HTTP/1.1 servers in any language.
- **Wire is opaque.** Router↔handler hop wraps Claude payload as `{"payload": "<json-str>"}` and unwraps `{"envelope": "<json-str>"|null}`. Don't add Claude-schema typing on this hop — the whole point is decoupling from Claude's evolving envelope schema.
- **No SDK.** No Python helper classes for writing handlers — they would couple handlers to this package's lifecycle.
- **Minimal runtime deps.** Currently: Flask (router HTTP server) and requests (router→handler client). Justify additions in PR description; reach for stdlib first.

## Layout

| Path | Purpose |
|---|---|
| `src/claude_dynamic_hooks/_server/` | daemon (Flask app + werkzeug WSGI server), http_client, router chain, process lock |
| `src/claude_dynamic_hooks/_cli/` | `cdh` entry points |
| `src/claude_dynamic_hooks/{config,events,paths}.py` | TOML schema, EventType enum, filesystem paths |
| `examples/handlers/read_once/` | sole canonical example handler |
| `docs/openapi.yaml` | handler contract (machine-readable) |
| `docs/CDHP.md` | protocol overview (prose) |

## Things to NOT add back

- A Python SDK (`api/`, `Handler` base class, envelope helpers). Handlers are HTTP servers.
- A `handler.toml` manifest. Handlers self-describe via `/health`.
- Supervision / spawning logic in the router. Lifecycle is the operator's job.
- Claude schema types on the router↔handler wire. The opaque-string wrap is load-bearing.
- A custom RPC layer (UDS, framing, JSON-RPC, …). Claude Code ships HTTP hooks natively; we route HTTP.
- Background `/health` polling / probe registry. Single-user dev tool; static event declaration in config is enough. Tried it; ~80 LOC of polling machinery for sub-second detection latency improvements that didn't matter.
- A cross-language conformance corpus (`tests/conformance/`) with JSON fixtures + reference echo handler + Python runner. Tried it; ended up testing test code (the echo fixture) for a polyglot scenario that doesn't exist. Reconsider only when a real second-language handler appears.
- OpenAPI codegen for wire models (`datamodel-code-generator`, generated dataclasses). Tried it; ~109 LOC of plumbing for 7 fields total across 4 schemas, plus a counterproductive strict-extra-key check on `/health`. Drift protection is better served by `tests/test_spec_examples.py` asserting field shapes directly. Reconsider only if the protocol grows substantially (≥3 schemas with ≥10 fields each, with regular schema churn).

## Tests

`uv run pytest` — single suite. `tests/test_daemon_integration.py` spawns the router + a stub handler as subprocesses for end-to-end coverage.

## Live verification

`cdh start && cdh list-handlers` plus a Read-twice scenario in a real Claude Code session is the smoke test for end-to-end correctness — unit tests don't cover the Claude→router HTTP hop.
