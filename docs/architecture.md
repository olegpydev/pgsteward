# Architecture

## Layout

```
pgsteward/
  server.py         FastMCP tools, stdio entry point
  tooling.py        Tool decorator, auditing, domain-error translation
                    (shared by core + extensions)
  dependencies.py   Registry singleton, wired into the lifespan
  config.py         Settings: connections, credentials, security policy
  connections.py    Connection registry, lazy pools, lifecycle
  security.py       SQL classification and write policy
  health.py         PostgreSQL health checks: thresholds and pure logic
  serialization.py  One JSON traversal, shared by the tool layer and
                    the adapter's response budget
  models.py         Frozen pydantic models for every response
  errors.py         Domain errors
  logger.py         stderr logging
  py.typed          Marker: the package ships inline type information
  adapters/
    base.py         DBAdapter protocol, StatementKind, timing helpers
    postgres.py     asyncpg implementation
  extensions/
    __init__.py     register_all: which optional features to turn on
    federation.py   Cross-database joins via operator-maintained map (optional)
tests/              unit tests (fake pool) + integration tests (live server)
```

## Request path

```
MCP client
  -> server.py tool          validate arguments
  -> tooling.py decorator    audit, translate domain errors
  -> connections.py          resolve connection_id, get or create an adapter
  -> adapters/postgres.py    classify, apply policy, open the right transaction
  -> asyncpg                 server-side cursor, command_timeout
  <- models.py               frozen response model
  <- serialization.py        model_dump() -> JSON string
```

Tools return JSON strings rather than structured objects, so the response
shape is fully under this project's control and does not shift with
FastMCP's serialisation. A decorator (from `tooling.py`) audits the call
and translates domain errors, so each tool body is only the call it
actually makes; the traversal that turns the result into JSON lives in
`serialization.py`, because the adapter needs the same one to size a
response against `max_bytes`.

## Extensions

Optional server features are registered dynamically in `_lifespan` before
the first client request. Each extension lives in `pgsteward/extensions/`
and exports a `register(mcp) -> str` function that returns a status:

- `'on'`: feature fully active;
- `'off'`: feature disabled (e.g., no config file);
- `'degraded'`: partial operation (e.g., config file unusable);
  describe-only interface returned with error notes to the client.

Registration never raises. An optional feature does not get to decide
whether the server starts, so everything its file can do to fail — absent,
unreadable, not text, not JSON, not the expected schema — maps onto one of
the three statuses.

`federation` is the one extension today, and its status decides how many
of its two tools — `describe_relationships` and `federated_lookup` —
appear in `tools/list`. Which state yields which is in
[tools.md](tools.md#cross-database-links-optional). Registering nothing
when the map is absent saves ~120 context tokens per session.

## Connection registry

Adapters are created lazily on first use and reused afterwards. The lock is
per connection, not global: an unreachable database must not block work on
the others.

`list_connections` does not create adapters at all unless asked to probe:
it is the natural first call of a session, and probing by default would
open a connection to every configured database. With `probe=true` it
tests each connection concurrently using a short-lived connection that is
closed immediately, and reports failures per connection as a code instead
of raising.

Only the acquisition of a pooled connection is wrapped in the error
translator, not the query that follows: the pool may open a new physical
connection at any time and surface a credentials error, while an error
from the query itself describes the schema and belongs to the agent.

Pools are closed in the FastMCP lifespan via `close_all()`, which tolerates
individual failures — a broken pool should not prevent the rest from
shutting down.

## The adapter boundary

The `DBAdapter` protocol in `pgsteward/adapters/base.py` is the whole
contract the MCP layer knows: lifecycle (`connect`, `close`, `ping`),
introspection (`server_info`, `list_databases`, `list_schemas`,
`list_tables`, `describe_table`, `search_schema`) and work (`execute`,
`explain`, `analyze_health`). Every method returns a model from
`models.py`, never a driver object.

Response models carry `extra: dict[str, Any]` for data that does not fit
the shared shape — `estimated_row_count` rides there today. Models that
need to explain themselves also carry `notes: list[str]`, filled in only
when the explanation applies.

The protocol exists for one reason, and portability is not it: it is what
lets the unit tests drive a fake pool in place of a live database.
`ConnectionRegistry` constructs `PostgresAdapter` directly — there is no
factory, no engine registry and no engine field anywhere in the
configuration or the responses.

## Why there is no second engine

A deliberate decision, and the reasoning is worth having in one place.

`analyze_db_health` is eleven checks over `pg_stat_activity`, `reltuples`,
sequence `last_value`, replication lag and transaction id wraparound — the
last of which does not exist as a concept outside PostgreSQL. Schema
introspection and the `EXPLAIN (FORMAT JSON)` parser are equally
PostgreSQL-shaped: the protocol is under a hundred lines, the adapter
behind it is an order of magnitude larger.

More fundamentally, the security model rests on
`BEGIN TRANSACTION READ ONLY`. No other engine this project might target
has an equivalent, so a second adapter would first have to answer how it
guarantees read-only at all — before any of the introspection work starts.
That makes a second engine a second project of comparable size.

## Testing strategy

**Unit tests** drive a fake asyncpg pool. They cover pure logic — config
parsing, SQL classification, the registry — and the boundaries where a
mistake is expensive: which transaction mode was opened, how many rows the
cursor asked for, whether the limit was capped.

**Integration tests** exist only for what a fake cannot prove:

- the introspection SQL is correct against a real catalog;
- the `READ ONLY` barrier rejects disguised writes and leaves data intact;
- parameters are bound by the server rather than spliced into the
  statement — the one property a fake pool can only claim, not
  demonstrate;
- the SQL `federated_lookup` assembles from map identifiers is valid: it
  is the only statement pgsteward builds from names, and quoting is what
  decides whether a reserved word or a mixed-case table survives it;
- `_connect_kwargs` is a dictionary asyncpg accepts — pool bounds, TLS
  mode, and the DSN built from discrete fields;
- under a role without grants, every health check either answers or
  reports `skipped` — the masking is done by PostgreSQL, so no fake can
  reproduce it — and none of them degrades to `skipped` through an error
  in its own SQL;
- connection failures surface as a code with no host, port, role or
  database name in the message.

They create and drop their own schema and roles. For that reason they are
excluded from a bare `pytest` run and selected explicitly with
`-m integration`; they are skipped unless `PGSTEWARD_TEST_DSN` is set, and
refused if it names a database whose name does not contain `test`. CI runs
them against PostgreSQL 14 and 17.

That split is deliberate. The unit suite is fast and runs everywhere; the
integration suite is small and only asserts things that would otherwise be
taken on faith.
