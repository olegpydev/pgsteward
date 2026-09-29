# Client integrations

pgsteward speaks MCP over **stdio**: the client starts it as a child process
on demand. There is no server to deploy and no port to open.

Every example below uses `uvx pgsteward`, which downloads and runs the
published package without a permanent install. If you prefer a pinned
install, use `uv tool install pgsteward` and replace the command with the
absolute path to the installed `pgsteward` binary (`which pgsteward`).

## Keeping secrets out of the client config

All examples inline the credentials for readability. In practice a client
config file is a worse place for them than a `.env` — see
[security.md](security.md#where-the-credentials-sit). Put the same
variables in `~/.config/pgsteward/.env` instead: pgsteward finds that file
on its own, and the `env` block of every example below can then be dropped
entirely. Setup, alternative locations, and why the working directory is
never consulted:
[configuration.md](configuration.md#where-credentials-come-from).

Better still, configure an authentication method that stores no password
and leave `WEB_SHOP_PG_PASSWORD` out of both files:
[security.md](security.md#not-storing-a-password-at-all).

Every example below is written against one shared credential block, shown
in full here and abbreviated to `/* shared env block */` afterwards:

```jsonc
{
  "PGSTEWARD_DB_CONNECTIONS": "web-shop:WEB_SHOP",
  "WEB_SHOP_PG_HOST": "127.0.0.1",
  "WEB_SHOP_PG_PORT": "5432",
  "WEB_SHOP_PG_DATABASE": "web_shop",
  "WEB_SHOP_PG_USER": "readonly_user",
  "WEB_SHOP_PG_PASSWORD": "secret"
}
```

`HOST`, `USER` and `DATABASE` are required; the rest are optional.

---

## Claude Code

```bash
claude mcp add pgsteward \
  --env PGSTEWARD_DB_CONNECTIONS=web-shop:WEB_SHOP \
  --env WEB_SHOP_PG_HOST=127.0.0.1 \
  --env WEB_SHOP_PG_DATABASE=web_shop \
  --env WEB_SHOP_PG_USER=readonly_user \
  --env WEB_SHOP_PG_PASSWORD=secret \
  -- uvx pgsteward
```

Add `--scope project` to write it into the repository's `.mcp.json` and
share it with the team — in which case use `PGSTEWARD_ENV_FILE` rather than
inline credentials.

Verify with `claude mcp list`, then ask Claude to call `list_connections`.

## Claude Desktop

Config file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%AppData%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

```jsonc
{
  "mcpServers": {
    "pgsteward": {
      "command": "uvx",
      "args": ["pgsteward"],
      "env": { /* shared env block */ }
    }
  }
}
```

Claude Desktop does not inherit your shell `PATH`, so if `uvx` is not found
use its absolute path (`which uvx`). Restart the app after editing.

## opencode

`opencode.json` in the project, or `~/.config/opencode/opencode.json`
globally:

```jsonc
{
  "mcp": {
    "pgsteward": {
      "type": "local",
      "enabled": true,
      "command": ["uvx", "pgsteward"],
      "environment": { /* shared env block */ },
      "timeout": 45000
    }
  }
}
```

Note the key is `environment`, not `env`, and the command is a single
array. With the credentials in `~/.config/pgsteward/.env` the whole
`environment` block goes away.

Keep `timeout` above `<PREFIX>_PG_QUERY_TIMEOUT` (30 seconds by default,
so 45000 ms here). A statement that runs out of time is stopped by
`statement_timeout` and reported as `The statement exceeded the connection
query_timeout.` — but only if the client waits long enough to receive it.
Set the two to the same value and the client gives up first, replacing
that message with an opaque transport error. The server keeps the same
margin internally: `command_timeout` is `query_timeout + 5`.

## Cursor

`.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` globally:

```jsonc
{
  "mcpServers": {
    "pgsteward": {
      "command": "uvx",
      "args": ["pgsteward"],
      "env": { /* shared env block */ }
    }
  }
}
```

Enable the server in Settings → MCP; Cursor lists its tools once it starts.

## VS Code (GitHub Copilot)

`.vscode/mcp.json` in the workspace. VS Code supports `inputs`, which is
the cleanest way to avoid storing a password:

```jsonc
{
  "inputs": [
    {
      "type": "promptString",
      "id": "pg-password",
      "description": "PostgreSQL password",
      "password": true
    }
  ],
  "servers": {
    "pgsteward": {
      "type": "stdio",
      "command": "uvx",
      "args": ["pgsteward"],
      "env": {
        /* shared env block, but: */
        "WEB_SHOP_PG_PASSWORD": "${input:pg-password}"
      }
    }
  }
}
```

Use it from Copilot Chat in Agent mode.

## Windsurf

`~/.codeium/windsurf/mcp_config.json`, exactly the Claude Desktop shape:
`mcpServers` → `pgsteward` → `command`, `args`, `env`.

## Zed

`settings.json` (`cmd-shift-p` → `zed: open settings`). Same fields, but
under `context_servers` and with a `source`:

```jsonc
{
  "context_servers": {
    "pgsteward": {
      "source": "custom",
      "command": "uvx",
      "args": ["pgsteward"],
      "env": { /* shared env block */ }
    }
  }
}
```

## Running from a checkout

For development, skip `uvx` and run the local sources:

```jsonc
{
  "mcpServers": {
    "pgsteward": {
      "command": "/absolute/path/to/pgsteward/.venv/bin/python",
      "args": ["-m", "pgsteward.server"],
      "env": { "PGSTEWARD_ENV_FILE": "/absolute/path/to/pgsteward/.env" }
    }
  }
}
```

Pointing at `.venv/bin/python` directly rather than going through `uv run`
avoids a dependency-sync check on every start. The project is installed as
an editable package, so `-m pgsteward.server` resolves regardless of the
process working directory.

A `.env` in the checkout root is picked up without `PGSTEWARD_ENV_FILE`,
but only if `~/.config/pgsteward/.env` does not exist — that one wins.
Naming the file explicitly removes the ambiguity, which is worth doing on
a machine that runs both a checkout and an installed copy.

---

## Troubleshooting

**The client reports that the server exited immediately.** A
configuration the process cannot be started with — a malformed
`PGSTEWARD_DB_CONNECTIONS` entry, a duplicate connection id, an unknown
`PGSTEWARD_LOG_LEVEL` — stops the server before it serves anything. It
says why in a single line on stderr and exits with code `2`. Not every
MCP client shows you that stream; several capture it and drop it, so an
empty client log proves nothing. Run the server outside the client to
read it:

```sh
PGSTEWARD_ENV_FILE=~/.config/pgsteward/.env pgsteward < /dev/null
```

With a usable configuration it writes its startup diagnostics, then exits
on end of input; with an unusable one it prints the reason. Problems with
an individual connection are *not* in this category — they are warnings,
the server starts, and the rest of the fleet keeps working. Failures that
happen later, while serving a call, have their own reporting that does
not depend on stderr: see `error_code` below.

**`uvx: command not found`.** The client is not inheriting your shell
`PATH`. Use the absolute path from `which uvx`.

**Tools appear, but every call says the connection is not configured.**
`PGSTEWARD_DB_CONNECTIONS` did not reach the process. Ask the agent to call
`list_connections`: an empty list means the environment block is missing or
misspelled. Remember the key is `environment` in opencode and `env`
elsewhere.

**`list_connections` shows a connection with `error_code:
misconfigured`.** The id reached the process but its credential block did
not — usually a typo in the prefix, which makes the whole `<PREFIX>_PG_*`
block read as empty, or a missing `<PREFIX>_PG_DATABASE`, which is
required and has no default. Calling it fails with the names of the
missing variables, which the server also logs at startup. The rest of the
fleet keeps working.

**Everything is refused as read-only.** That is the default. See
`get_security_config`, and [configuration.md](configuration.md#connections)
for how to relax it deliberately.
