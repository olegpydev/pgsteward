# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-29

Initial public release.

### Added

- Read-only MCP server for PostgreSQL over stdio, built on FastMCP.
  Connections are preconfigured on the server: the agent selects one by
  `connection_id` and never sees a DSN.
- Configuration through `PGSTEWARD_*` variables plus one `<PREFIX>_PG_*`
  credential block per connection, read from the environment or a `.env`
  file. A connection whose block is missing or unusable is reported as
  `misconfigured` and refused by name, leaving the rest of the fleet
  working; a problem with the process configuration itself stops the
  server with one line on stderr and exit code `2`.
  `PGSTEWARD_ENV_SUFFIXES` labels connections by environment and lets one
  relationship map serve all of them. See
  [docs/configuration.md](docs/configuration.md).
- Schema exploration: `list_connections`, `list_databases`, `list_schemas`,
  `list_tables`, `describe_table`, `search_schema`.
- `query`, `explain_query`, `analyze_db_health` (eleven catalog checks, no
  extensions required, so it works on managed PostgreSQL), `ping`,
  `get_server_info`, `get_security_config`. See
  [docs/tools.md](docs/tools.md).
- `describe_relationships` and `federated_lookup`, registered only when a
  relationship map is configured, so they cost no context without one.

### Security

- Reads run inside `BEGIN TRANSACTION READ ONLY`, whatever the connection
  flags say. Writes need both `read_only=false` on the connection and the
  matching statement class enabled in policy; both are off by default.
- Connection failures return a stable `error_code`. Driver messages, which
  carry host names, ports, role names and database names, stay in the
  server log.
- `query_timeout` is enforced as a server-side `statement_timeout`.
- `list_connections` performs no network access unless `probe=true`.
- Audit log of every tool call, failures included, without statement text
  or parameter values.
- Startup warnings for a world-readable `.env` file and for connections
  that set no `sslmode`, which leaves asyncpg free to fall back to an
  unencrypted connection.
- See [docs/security.md](docs/security.md) and [SECURITY.md](SECURITY.md).

[0.1.0]: https://github.com/olegpydev/pgsteward/releases/tag/v0.1.0
