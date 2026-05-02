# Contributing

## Dev setup

```bash
uv sync --extra dev
```

## Test

```bash
uv run pytest         # 60+ tests, ~25s; spawns real subprocesses for daemon integration
uv run ruff check     # lint
uv run mypy src       # types
```

## Live verification

Unit tests don't cover the Claude→router HTTP hop. Smoke-test in a real
Claude Code session:

```bash
cdh start
cdh list-handlers     # confirm alive status
# trigger a Read in Claude Code, then trigger the same Read again — read_once
# should advise "already in context" the second time.
cdh stop
```

## Pull requests

- Match existing style (see `pyproject.toml` ruff config).
- Include a test for behavioral changes. Tests that hit real subprocesses are
  preferred over mocks for daemon/router code.
- Update `docs/openapi.yaml` and `docs/CDHP.md` together for any wire change.
- Bump `CHANGELOG.md` under an `## [Unreleased]` heading.

## Scope

Read `CLAUDE.md` § "Things to NOT add back" before proposing new features.
Specifically: no Python SDK, no handler manifest, no router-side supervision /
spawning, no Claude-schema typing on the router↔handler hop, no custom RPC
layer, no background `/health` polling, no cross-language conformance corpus,
no OpenAPI codegen for wire models. Each was tried and removed; reconsider
only with concrete evidence the original removal rationale no longer holds.

## Reporting issues

https://github.com/phansen314/claude-dynamic-hooks/issues
