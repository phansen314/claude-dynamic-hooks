# read_once

Standalone CDHP handler. Tracks (session_id, file_path) → mtime per session and advises (warn mode) or blocks (deny mode) on re-reads of unchanged files.

No dependency on `claude-dynamic-hooks`. Pure stdlib + `cachetools`.

## Setup

```bash
uv sync
```

## Run

### Manual (dev)

```bash
CDH_BIND_ADDR=127.0.0.1:9001 .venv/bin/python read_once.py
```

Background it with `&` if you don't want a foreground process. Logs go to stderr.

### systemd user unit (production)

`~/.config/systemd/user/cdh-read-once.service`:

```ini
[Unit]
Description=cdh handler — read_once
After=network.target

[Service]
Type=simple
WorkingDirectory=/abs/path/to/examples/handlers/read_once
Environment=CDH_BIND_ADDR=127.0.0.1:9001
# Optional handler-specific config:
#   READ_ONCE_MODE=deny       (default: warn)
#   READ_ONCE_TTL=1200        (default: 1200s)
ExecStart=%h/code/claude-dynamic-hooks/examples/handlers/read_once/.venv/bin/python read_once.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now cdh-read-once
```

## Wire it to the router

Add to `~/.config/cdh/config.toml`:

```toml
[[handler]]
name = "read_once"
url = "http://127.0.0.1:9001"
events = ["preToolUse"]
```

Then `cdh start` (or `cdh stop && cdh start` if already running).

`cdh list-handlers` confirms it's alive.

## Config (env vars)

| Var | Default | Effect |
|---|---|---|
| `READ_ONCE_MODE` | `warn` | `warn` returns `allow` envelope with rationale; `deny` returns `block` |
| `READ_ONCE_TTL` | `1200` | seconds before cache entry expires |
| `READ_ONCE_DISABLED` | unset | set to `1` to no-op |
