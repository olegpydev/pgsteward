# pgsteward

[![CI](https://github.com/olegpydev/pgsteward/actions/workflows/ci.yml/badge.svg)](https://github.com/olegpydev/pgsteward/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Read-only access to a fleet of PostgreSQL databases for LLM agents, over
MCP. The agent picks a connection by id and never sees the credentials.

Claude, Cursor, opencode or any other MCP client can explore your schema,
run queries, read execution plans and check database health from the chat.

[Русская версия](README.ru.md)

---

## Tools

| Tool | What it does |
|---|---|
| `list_connections` | Configured connections: id, read_only, environment. No network access unless `probe=true`. |
| `list_databases` | Databases on this connection's server. |
| `list_schemas` | Schemas in this connection's database (system schemas excluded). |
| `list_tables` | Tables and views in a schema. |
| `describe_table` | Columns, types, nullability, defaults, PK, FKs, indexes, row-count estimate. Errors if the relation does not exist. |
| `search_schema` | Substring search for tables and columns across every non-system schema; reports truncation. |
| `query` | One guarded SQL statement, positional parameters, server-side row cap. |
| `explain_query` | `EXPLAIN (FORMAT JSON)` as a parsed plan tree with heuristic warnings. |
| `analyze_db_health` | Eleven catalog checks; no extensions required, so it works on managed PostgreSQL. |
| `ping` | Connection reachability. |
| `get_server_info` | Server version and current database. |
| `get_security_config` | Effective policy: read_only, DML/DDL, max_rows, max_bytes, query_timeout. |

Two more — `describe_relationships` and `federated_lookup` — join an entity
that lives in several databases under different keys. They are registered
only when a relationship map is configured, so they cost no context in a
session that has no such map.

Every tool returns a JSON string. `describe_table`, abridged:

```json
{
  "name": "customer_order", "schema": "public",
  "columns": [
    {"name": "id", "data_type": "bigint", "nullable": false, "is_primary_key": true,
     "default": "nextval('customer_order_id_seq'::regclass)"},
    {"name": "customer_id", "data_type": "bigint", "nullable": false,
     "default": null, "is_primary_key": false},
    {"name": "total", "data_type": "numeric(12,2)", "nullable": false,
     "default": null, "is_primary_key": false}
  ],
  "primary_key": ["id"],
  "foreign_keys": [
    {"column": "customer_id", "references_schema": "public",
     "references_table": "customer", "references_column": "id",
     "constraint_name": "customer_order_customer_id_fkey"}
  ],
  "indexes": [
    {"name": "idx_order_customer", "columns": ["customer_id"],
     "is_unique": false, "included_columns": []}
  ],
  "extra": {"estimated_row_count": 28431, "relation_kind": "table"},
  "notes": []
}
```

Response shapes, health-check thresholds and the privilege matrix:
**[docs/tools.md](docs/tools.md)**.

## What this is for

pgsteward is built around one setup: an agent working with several
PostgreSQL databases in a single session — staging and production, or
several services. Four properties follow from that.

**Read-only is enforced by PostgreSQL, not by the SQL classifier.** Every
read runs inside `BEGIN TRANSACTION READ ONLY`, on top of
`default_transaction_read_only` and a server-side `statement_timeout`.
Statements that only look like reads — `WITH ... DELETE ... RETURNING`,
`EXPLAIN ANALYZE INSERT` — are rejected by the engine
([asserted against a live server](tests/test_integration_postgres.py)).

**Several connections, credentials on the server.** The agent picks a
`connection_id` and never sees a DSN. Connection failures come back as a
stable code — `auth_failed`, `database_not_found`, `unreachable` — so the
host names, ports and role names in driver messages stay in the server log
instead of entering the model's context.

**Health checks that refuse to guess.** Under a least-privilege role
PostgreSQL blanks the columns these checks need instead of raising an
error, so a check that trusts the result reports `ok` having examined
nothing. pgsteward returns `skipped` and names the missing grant.

**An audit log with no SQL in it.** Every tool call logs the connection,
statement class, duration, row count and outcome — failures included,
since a refused write is worth as much to an audit as a successful read.
The statement text and the parameter values are not written anywhere.

## Install

With [uv](https://docs.astral.sh/uv/) there is nothing to install ahead of
time: your MCP client can run `uvx pgsteward` directly.

```bash
uv tool install pgsteward     # or: pipx install pgsteward
```

Requires Python 3.11+ and a reachable PostgreSQL 14+. CI exercises 14
and 17 against a live server. 14 is the oldest release still supported
upstream; older servers are likely to work, but nothing verifies that.

## Quick start

**1. Point pgsteward at a database.** Configuration is entirely environment
variables — one line of metadata plus a block of credentials. Put them in
`~/.config/pgsteward/.env`, which pgsteward finds on its own:

```bash
mkdir -p ~/.config/pgsteward
touch ~/.config/pgsteward/.env && chmod 600 ~/.config/pgsteward/.env
```

```bash
PGSTEWARD_DB_CONNECTIONS=web-shop:WEB_SHOP

WEB_SHOP_PG_HOST=127.0.0.1
WEB_SHOP_PG_PORT=5432
WEB_SHOP_PG_DATABASE=web_shop
WEB_SHOP_PG_USER=readonly_user
WEB_SHOP_PG_PASSWORD=secret
```

`web-shop` is the id the agent uses; `WEB_SHOP` is the prefix of the
credentials block. `HOST`, `USER` and `DATABASE` are required — none of
them is guessed. Read-only and write-forbidden are the defaults.

Leave `WEB_SHOP_PG_PASSWORD` empty and no password is sent at all, so a
Unix socket with `peer`, a TLS client certificate, `.pgpass` or an IAM
token work instead. See
[docs/security.md](docs/security.md#not-storing-a-password-at-all).

**2. Register it with your MCP client.** For Claude Code:

```bash
claude mcp add pgsteward -- uvx pgsteward
```

For anything with a JSON config (Claude Desktop, Cursor, opencode, VS Code,
Windsurf, Zed) the shape is the same, and carries no credentials:

```jsonc
{
  "mcpServers": {
    "pgsteward": {
      "command": "uvx",
      "args": ["pgsteward"]
    }
  }
}
```

Exact file paths and per-client quirks: **[docs/integrations.md](docs/integrations.md)**.

The variables can go in the client's `env` block instead of the `.env`, and
for a throwaway local database that is fine. For anything else the `.env`
is the safer of the two — see
[docs/security.md](docs/security.md#where-the-credentials-sit).

**3. Ask.** "Which tables reference the customer id?", "Why is this query
slow?", "Is anything wrong with this database right now?"

To try it without a real database, [examples/quickstart](examples/quickstart)
brings up a seeded PostgreSQL in Docker with a ready-made config.

## Security model

Reads are protected at three levels, and only the last two are guarantees:

1. **Classification before execution.** The statement is classified by its
   first significant keyword and checked against the policy; more than one
   statement per call is rejected. This is a filter: a keyword classifier
   can be fooled, so the guarantee rests on layer 2.
2. **The transaction.** Reads always execute inside
   `BEGIN TRANSACTION READ ONLY`, whatever the connection flags say.
3. **The session.** Read-only connections also set
   `default_transaction_read_only=on`, and every connection sets a
   server-side `statement_timeout`.

Writes require *both* `read_only=false` on the connection and the matching
class enabled in policy. Both default to off.

Point it at a least-privilege role rather than a superuser. pgsteward does
not rewrite the values it returns, so keeping a column — personal data, a
secret — away from the agent is done with column-level grants or a view,
and enforced by PostgreSQL:
[hiding columns from the agent](docs/security.md#hiding-columns-from-the-agent).

How each layer is implemented and where it stops:
**[docs/security.md](docs/security.md)**. What pgsteward does *not* protect
against, and how to deploy it safely: **[SECURITY.md](SECURITY.md)**.

## Configuration

```
PGSTEWARD_DB_CONNECTIONS=web-shop:WEB_SHOP,analytics:ANALYTICS
```

CSV of `id:prefix`. `prefix` defaults to the id uppercased, with dashes
and spaces turned into underscores. Credentials are then read from
`<PREFIX>_PG_HOST / USER / DATABASE` — all three required, unless
`<PREFIX>_PG_DSN` supplies the address whole — plus optional `PORT`,
`PASSWORD`, `READ_ONLY`, `SSLMODE`, `QUERY_TIMEOUT`, `MAX_ROWS`,
`MAX_BYTES`, `POOL_MIN`, `POOL_MAX`, `ALLOW_DML`, `ALLOW_DDL`.

An id ending in one of the suffixes you list in `PGSTEWARD_ENV_SUFFIXES`
(`prod,staging`, `dev,uat` — whichever your ids use) reports that
suffix as its `environment` in `list_connections`, so the agent can tell
production apart before it queries it. It is a label, not a guard — writes
are stopped by `read_only` and the policy, never by the name.

Full reference, including how `.env` is resolved when pgsteward is installed
from PyPI: **[docs/configuration.md](docs/configuration.md)**.

## Scope

PostgreSQL only, and that is a decision rather than a current state: the
health checks, the `EXPLAIN (FORMAT JSON)` parser and the `READ ONLY`
barrier the security model rests on are all PostgreSQL features with no
equivalent elsewhere. There is no engine field in the configuration and no
engine in the responses. The reasoning in full:
[docs/architecture.md](docs/architecture.md#why-there-is-no-second-engine).

Transport is stdio. The MCP client starts pgsteward as a child process on
demand: there is no service to deploy, and no SSE or HTTP endpoint either.

Not included, so that you can rule pgsteward out quickly: index
recommendations, `pg_stat_statements` workload analysis, and table or
index bloat estimates.

## Development

```bash
git clone https://github.com/olegpydev/pgsteward && cd pgsteward
uv sync

uv run ruff check . && uv run ruff format --check .
uv run mypy pgsteward
uv run pytest -m "not integration"
```

Integration tests need a throwaway PostgreSQL and are skipped without one;
the command to start it is in [CONTRIBUTING.md](CONTRIBUTING.md). What each
suite is responsible for:
[docs/architecture.md](docs/architecture.md#testing-strategy).

## Documentation

| Document | Covers |
|---|---|
| [docs/configuration.md](docs/configuration.md) | Every environment variable, `.env` resolution, multiple environments |
| [docs/tools.md](docs/tools.md) | Response shapes, health-check thresholds, required privileges |
| [docs/security.md](docs/security.md) | How the three layers work, known gaps, hiding columns, audit log |
| [docs/integrations.md](docs/integrations.md) | Per-client config files and troubleshooting |
| [docs/architecture.md](docs/architecture.md) | Layout, request path, adapter boundary, testing strategy |
| [SECURITY.md](SECURITY.md) | Threat model and recommended deployment |

## License

MIT — see [LICENSE](LICENSE).
