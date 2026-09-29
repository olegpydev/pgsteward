# Tools reference

Every tool returns a JSON string, and domain errors surface as ordinary
MCP tool errors. Most take `connection_id`. The optional federation tools
are the exception: `describe_relationships` takes no connection at all
(only an optional `entity` filter), and `federated_lookup` takes
`source_connection_id` and `target_connection_id`. These are present only
when a relationship map is configured — see
[cross-database links](#cross-database-links-optional).

The description of each tool — the text the model reads when choosing one
— lives in `pgsteward/server.py` and is deliberately short. This page is not
a copy of it: it covers response shapes, thresholds, required privileges
and limitations, which the model does not need in order to pick a tool.

Facts that matter when *reading* a response arrive in that response's
`notes` field, and only when they apply: the `INCLUDE`-column note appears
only for a table that has such an index, the truncation note only when
something was truncated.

## Introspection source

`list_tables`, `describe_table` and `search_schema` all read `pg_catalog`
rather than `information_schema`. The standard views cover only what the
SQL standard defines, so a materialized view — a PostgreSQL extension — is
absent from them entirely; describing one through `information_schema`
returned a relation with no columns at all.

`pg_catalog` in turn ignores privileges, so every query filters on them
explicitly. Relations the role can read nothing from, and columns it has
no `SELECT` grant on, are left out — which is what `information_schema`
did implicitly.

## Values that JSON cannot carry

A few PostgreSQL types have to be re-expressed on the way out. Two of them
change what you see:

| Type | Sent as | Why |
|---|---|---|
| `bytea` | `"\\x89504e47"` | PostgreSQL's own hex text format, so the value can go straight back into a query. A Python byte-string repr could not. |
| `nan`, `±inf` in `float4`/`float8` | `null`, plus a note | JSON has no literal for them. Emitting the bare words `NaN`/`Infinity` would produce a document a strict parser rejects. |
| `numeric`, `date`, `timestamp`, `uuid`, `interval` | string | No JSON equivalent; the text form is exact. |
| `json`, `jsonb` | string holding JSON | Passed through as the server rendered it, never re-parsed. |

The `null` for a non-finite float is the one substitution you cannot see
from the value alone, so `query` reports it in `notes`: *"N value(s) in
this result are NaN or Infinity and are reported as null."* A `null` in a
float column without that note is a real SQL `NULL`.

## Response shapes

### `list_connections`

`probe` defaults to `false`, and in that mode the call performs no network
access at all: it reports `id`, `read_only`, `environment` and
`alive: null`. The one exception is a `misconfigured` connection, which
reports `alive: false` in both modes — nothing was attempted, and there
is nothing to attempt. The tool is the natural first call of a session,
and a probing default would open a connection to every configured
database, production included.

`environment` is the id's environment suffix — whatever
`PGSTEWARD_ENV_SUFFIXES` lists — and `null` when the id ends in none of
them, or when the variable is not set at all. It exists so that production
is identifiable before a query runs;
write protection itself comes from `read_only` and the policy that
`get_security_config` reports. See
[configuration.md](configuration.md#environments).

With `probe=true` each connection is tested in parallel. A connection that
already has a pool is pinged through it; one that does not is tested with
a short-lived connection that is closed immediately, so probing never
leaves a pool behind that was not there before. Failures are reported per
connection as `error_code`, never as the driver's message; see
[security.md](security.md#connection-errors).

| `error_code` | Meaning |
|---|---|
| `auth_failed` | Wrong credentials, or the role may not log in. |
| `database_not_found` | The server is reachable, the database is not there. |
| `unreachable` | DNS, refused connection, or no route. |
| `tls_failed` | TLS negotiation or certificate validation failed. |
| `timeout` | The connection attempt exceeded the timeout. |
| `connection_failed` | Anything else; details are in the server log. |
| `misconfigured` | The id is declared, but a variable it needs is missing or holds a value that could not be used. |

`misconfigured` is the one code reported without `probe`: it is not a
network failure but a configuration problem, knowable without touching
the database. Such a connection is never probed, and any call against it
fails naming the environment variables at fault — separating the ones
that are not set from the ones whose value was refused, because the two
are fixed differently. The refused value itself is not echoed back.

### `list_databases` and `list_schemas`

Both return a flat array of names. `list_databases` lists every database
on the server except templates; querying one of them needs its own
configured `connection_id`, because a PostgreSQL session cannot read
across databases. `list_schemas` lists the schemas of the connection's own
database, minus `pg_catalog`, `information_schema` and anything the server
prefixes with `pg_` — the underscore is escaped, so a user schema named
`pgbouncer` or `pgq` is not swept up with them.

`list_schemas` is the one introspection call that reads
`information_schema` rather than `pg_catalog`: the standard view already
restricts itself to schemas the role may use.

### `list_tables`

Name, schema and `kind` for every relation in the schema that the role can
read something from. `kind` is the exact relation kind — `table`,
`partitioned table`, `view`, `materialized view` or `foreign table` — the
same vocabulary as `describe_table`'s `extra.relation_kind`. A partition
is a relation in its own right and appears as a `table` next to the
`partitioned table` it belongs to.

### `describe_table`

Columns (type, nullability, default, primary-key flag), the primary key,
foreign keys, indexes, plus `extra.estimated_row_count` and
`extra.relation_kind`.

A relation that does not exist is an **error**, not an empty result: an
empty column list is indistinguishable from "this table has no columns"
once it reaches the model — the same empty answer that reading
`information_schema` used to produce for a materialized view. Naming an
index or a sequence produces a different error that says what the relation
actually is. `relation_kind` is one of `table`, `partitioned table`,
`view`, `materialized view`, `foreign table`.

Column types carry their length and precision — `character varying(255)`,
`numeric(12,2)` — because they come from `format_type`, where
`information_schema.columns` reports a bare type name and keeps the size
in separate columns. Columns removed by `DROP COLUMN` are left out:
PostgreSQL keeps their `pg_attribute` row until the table is rewritten.

Composite keys keep the order they were declared in, not the order the
columns appear in the table: `PRIMARY KEY (tenant_id, id)` is reported as
`["tenant_id", "id"]` even when `id` comes first. Non-key `INCLUDE`
columns of a primary key are left out.

A composite foreign key is reported as one entry per key position, all
sharing a `constraint_name` and carrying `references_schema`, paired in
order — `FOREIGN KEY (a, b) REFERENCES t (x, y)` yields exactly `a -> x`
and `b -> y`.

For indexes, `columns` holds the key columns in index order; a position
that indexes an expression appears as the expression text
(`lower(email)`). Non-key columns from `INCLUDE (...)` go to
`included_columns`: an index-only scan can read them, but the index
cannot search by them.

`extra.estimated_row_count` comes from `pg_class.reltuples` — planner
statistics as of the last `ANALYZE`/`VACUUM`, not an exact `COUNT(*)`, but
read from the catalog instantly, which is what lets an agent notice a
table is large *before* querying it. It is `null` when the relation was
never analyzed.

Columns the connection role has no `SELECT` privilege on are left out of
`columns`, and `notes` then reports how many — the count, never the names.
`primary_key`, `foreign_keys` and `indexes` are not filtered that way, so
a restricted column is named there if it takes part in a key or an index;
its *values* stay unreadable either way. Why it is a count, why the
structure is reported in full, and how to keep a column name out of the
answer altogether:
[hiding columns from the agent](security.md#hiding-columns-from-the-agent).

Identifiers are quoted when resolved, so mixed-case and otherwise special
names behave the same as lowercase ones.

### `search_schema`

Substring search (`ILIKE`) over table and column names across every
non-system schema. `_` and `%` in the pattern are `ILIKE` wildcards —
matching any single character and any sequence respectively — and are not
escaped.

Pass `schema` to search one schema instead of all of them. On a database
with many schemas that is also the way out of a truncated result.

Results are capped at 200. The response carries `match_count`, `limit` and
`truncated`, so a truncated result is distinguishable from a complete one.

### `query`

One statement per call. `params` are positional PostgreSQL parameters
(`$1`, `$2`).

The effective row cap is `min(limit, connection max_rows)`, applied by a
server-side cursor, so rows beyond the limit are never read at all;
`truncated=true` means rows were dropped. `max_rows = -1` disables the cap
entirely, and then `limit` alone decides.

Zero is refused on both sides of that expression, and for the same
reason: in most tools it reads as "unlimited", here it would cap every
answer at nothing. `max_rows = 0` is rejected when the configuration is
read; `limit = 0` is rejected before the statement is sent. Omit `limit`
to fall back to the connection's `max_rows`, or pass a negative value to
say the call itself imposes no cap.

That cap comes back as `applied_limit` — `null` when there was none.
`truncated` alone cannot say who cut the result: ask for `limit=5` on a
large table and it is `true` either way. Compare the two. `applied_limit`
equal to the `limit` you sent means you got what you asked for;
`applied_limit` below it means `max_rows` intervened, and the answer is
therefore incomplete.

There is a second cap, on size rather than count: `max_bytes`, reported
as `applied_byte_limit`. Rows are not the unit that runs out. A thousand
rows of `SELECT n` is six kilobytes; a thousand rows of a table with text
descriptions is two megabytes, and one `max_rows` covers both. The budget
counts the rows as the JSON they ship as, stops on a whole row, and says
so in `notes`, because the fix is the opposite of the one for a row cap:
asking for fewer rows does not help when what is wide is the row. Ask for
fewer columns.

The first row is always kept, even when it alone is over budget. Zero
rows would read as "there is no data", which is the same trap `max_rows=0`
sets, and `explain_query` would break outright — a plan arrives as one
row, and half of it is not JSON.

The budget applies to `query` and `federated_lookup`, the two tools whose
size the agent decides. Schema introspection has its own bounds.

Reads always run in a `READ ONLY` transaction. Writes need both
`read_only=false` on the connection and the statement class enabled in
policy — check `get_security_config` first. The write path is also the one
without a cursor, since asyncpg cannot open one for DML that has no
`RETURNING`, so a `DELETE ... RETURNING` over a huge table is materialised
in full. Writes are off by default.

### `explain_query`

| Field | Notes |
|---|---|
| `plan` | The parsed plan tree (the `Plan` node). |
| `planning_time_ms` | **Only with `analyze=true`.** |
| `execution_time_ms` | Only with `analyze=true`. |
| `warnings` | Heuristics, see below. |
| `execution_ms` | How long the EXPLAIN call itself took. |

Warnings:

- **Seq Scan on a table** — reported with the relation name and cost.
- **Row estimate is off** (`analyze=true` only) — planned versus actual
  row counts differ by 10x or more, in either direction. Suppressed unless
  at least one of the two numbers is 100 or more: a selective index lookup
  normally plans one row and may return zero, and flagging that would make
  the heuristic useless.

`analyze=true` **physically executes the statement**, so it is accepted
only for reads: the plan runs inside the same `READ ONLY` transaction and
a write could never complete there anyway. `EXPLAIN ANALYZE INSERT` is
refused up front, naming the reason, instead of reaching the database and
coming back as `cannot execute INSERT in a read-only transaction`.

Without `analyze` nothing is executed, so any statement class can be
planned — that is the way to inspect a write you cannot run.

The refusal is a convenience, not a barrier. The same text sent straight
to `query` still classifies as `READ` and is stopped by the database; see
[security](security.md).

## `analyze_db_health`

Deterministic checks over the system catalog. No extensions are required —
not `pg_stat_statements`, not `hypopg` — so this works on managed
PostgreSQL. Thresholds live in `pgsteward/health.py`.

| Check | What it looks at | Thresholds |
|---|---|---|
| `connection_utilization` | Client backends vs `max_connections` | warning at 75%, critical at 90% |
| `idle_in_transaction` | Sessions idle in transaction over 5 min | warning if any, critical at 30 min |
| `long_running_queries` | Queries active over 5 min | warning if any |
| `buffer_cache_hit_rate` | Cache reads vs disk reads | warning at 99%, critical at 90% |
| `invalid_indexes` | Left behind by failed `CREATE INDEX CONCURRENTLY` | warning if any |
| `invalid_constraints` | `NOT VALID` or failed validation | warning if any |
| `unused_indexes` | `idx_scan=0`, not PK/unique, over 5MB | warning (informational) |
| `duplicate_indexes` | Identical index definitions | warning if any |
| `transaction_id_wraparound` | `datfrozenxid` age, plus the ten oldest relations | warning at 1B, critical at 1.8B |
| `sequence_exhaustion` | Percent of the sequence range consumed | warning at 75%, critical at 90% |
| `replication_lag` | Replica lag, if this is a primary | warning above 16MB |

Thresholds on a *measured quantity* are inclusive: a cache hit rate of
exactly 99%, a transaction id age of exactly 1B, a sequence exactly 75%
consumed and a transaction idle for exactly 30 minutes all already count.
Thresholds that act as a *filter deciding what to look at* are strict, so
a value exactly at the number is not reported: the 5 min in
`idle_in_transaction` and `long_running_queries`, the 5MB in
`unused_indexes`, and the 16MB in `replication_lag`.

Each check returns `id`, `status` (`ok`/`warning`/`critical`/`skipped`),
`message` and `details`. `overall_status` is the worst status across all
checks; `skipped` does not count towards it. If nothing substantive was
answered at all, `overall_status` is `unknown`, so that a green summary
never stands for eleven unanswered checks. In practice this needs a
server that refuses even plain catalog reads: five of the checks read only
world-readable catalogs and answer under any role.

A check can also be `skipped` because it could not be run at all — an
unexpected error from the server. It says so in a distinct message, so
this outcome stays distinguishable from a refusal on the merits, and the
reason goes to the server log instead of into the answer. The other ten
checks are unaffected: each runs inside its own savepoint, so one failure
cannot abort the transaction the rest share.

Lists inside `details` are capped, because the checks that build them are
bounded by the size of the schema rather than by anything smaller. Session
lists are capped at 20, object lists at 50, and each carries `truncated`
next to it; when that is `true` the count in `message` reads "At least N",
a floor rather than a total.

Three checks shape their lists differently. `transaction_id_wraparound`
builds two: its `databases` list is capped like any other and reports
`details.databases_truncated` — ordered by age, so the verdict does not
depend on where the cut falls — while its `relations` list is a top-10 by
age rather than a cut-down answer, has no flag, and states the size of the
sample as `details.relations_limit`. `replication_lag.replicas` is not
capped at all: `pg_stat_replication` holds one row per connected walsender
and is bounded by `max_wal_senders`, which no schema can grow.

`sequence_exhaustion` is the third: it reads the 50 fullest sequences but
lists only the ones past the warning threshold, so its `truncated`
describes that list rather than the scan. A cut that fell on sequences
below the threshold leaves the answer complete and the count exact — the
full population is `details.total_sequences` either way.

`connection_utilization` counts only backends that occupy a
`max_connections` slot; background processes appear in `pg_stat_activity`
but do not consume one. One imprecision remains and is stated in the
message: for a role without `pg_monitor` an autovacuum worker is
indistinguishable from a hidden client session, so utilisation may be
overstated by up to `autovacuum_max_workers`.

The wraparound thresholds are absolute rather than relative to
`autovacuum_freeze_max_age` on purpose: an age of 100–200M is the normal
autovacuum cycle, not a risk, and a relative threshold would cry wolf on
every healthy database. `details.relations` names the ten relations with
the oldest `relfrozenxid`, because `datfrozenxid` shows the risk but not
what to vacuum.

`idle_in_transaction` and `long_running_queries` describe other sessions.
They report `pid`, `usename`, `application_name` and duration — never
`pg_stat_activity.query`. See [security.md](security.md#audit-log).

### Privileges and statistics required

A check reports `skipped` rather than `ok` when it cannot see the data it
would need. An empty result set under a restricted view says nothing about
health, and reporting it as `ok` would be a confident wrong answer.

| Check | Needs | Without it |
|---|---|---|
| `idle_in_transaction` | `pg_monitor` (or `pg_read_all_stats`) to see other sessions' `state` | `skipped`, naming the grant |
| `long_running_queries` | same | `skipped`, naming the grant |
| `replication_lag` | same, for `pg_stat_replication` details | `skipped`, naming the grant |
| `connection_utilization` | nothing; `count(*)` is visible to everyone | answers, and reports `hidden_backends` |
| `sequence_exhaustion` | `SELECT` or `USAGE` on each sequence | `skipped` if none is readable, partial if some are |
| `unused_indexes` | at least 7 days since the last statistics reset | `skipped`, naming the reset time |
| `buffer_cache_hit_rate` | same | `skipped`, naming the reset time |
| everything else | nothing beyond catalog access | answers |

Not every `skipped` is about a grant. `replication_lag` also reports it
when there are no replicas at all, and `buffer_cache_hit_rate` when no
table has been read since the last reset — in both cases there is nothing
to measure rather than something withheld. The message says which.

`pg_sequences` lists every sequence regardless of privileges but blanks
`last_value` where the role has no grant, so "no privilege" and "never
used" look identical without an explicit `has_sequence_privilege` check.
`pg_stat_activity` behaves the same way with `state` and `backend_type`.

When statistics have never been reset (`stats_reset IS NULL`), the
accumulation window is the lifetime of the database, and the cumulative
checks run normally.

To answer the session checks:

```sql
GRANT pg_monitor TO <role>;
```

`pg_monitor` grants read access to statistics and configuration, not to
table data.

## Diagnostics

`ping` returns `{connection_id, alive}`. `get_server_info` returns the
server version string and the current database name.
`get_security_config` returns the effective policy for a connection:
`read_only`, `allow_dml`, `allow_ddl`, `max_rows`, `max_bytes`,
`query_timeout`. For a
`misconfigured` connection it refuses with the same message `query` gives,
naming the variables at fault, rather than answering with defaults that
were never configured.

## Cross-database links (optional)

`describe_relationships` and `federated_lookup` are present only when
configured and valid. They are optional server extensions, registered at
startup. When the relationship map is missing, empty, or corrupt, these
tools are either absent or present in degraded form:

- Map file not found or path not set: tools are absent from `tools/list`.
- Map file parses but has no entities: tools are absent from `tools/list`.
- Map file cannot be read or does not parse — invalid JSON, a schema
  mismatch, no read permission, not text at all: only
  `describe_relationships` is present and returns the reason in notes.
  An empty file lands here too, since zero bytes is not valid JSON.

None of these stops the server. Federation is optional, and its file does
not get to decide whether the agent reaches the databases at all.

The tools are withheld rather than registered in a state where they refuse
every call, because a registered tool costs context tokens in every session
to advertise a dead end. The operator fixes the configuration and restarts
the server; no other tool is affected.

The map is written by hand; nothing keeps it in sync with the schema, and
a missing entity does not prove there is no link. Format and a worked
example: [examples/relationships.example.json](../examples/relationships.example.json).
Where the file lives and when it is read:
[configuration.md](configuration.md#cross-database-relationship-map).

### `describe_relationships`

Returns the map, optionally filtered to one `entity`. An unknown entity
yields an empty map rather than an error, so the agent can call it without
arguments to discover the available ids.

`connection` in the map names a connection without its environment
suffix, so one map serves every environment: a call made on `orders-staging`
or on `orders-prod` finds the entry written for `orders`. With
`PGSTEWARD_ENV_SUFFIXES` unset, or a suffix missing from it, the id has to
match the entry exactly. Matching a map entry decides nothing about where
a query is sent — each step of `federated_lookup` still names its own
`connection_id`.

### `federated_lookup`

Does the two-step join in one call:

1. `SELECT <key> FROM <source table> WHERE <source_where>` on
   `source_connection_id`.
2. If ids came back,
   `SELECT <target_columns|*> FROM <target table> WHERE <key> = ANY($1)`
   on `target_connection_id`.

The link is matched in either direction. Limitations: a single hop only
(`A -> B -> C` takes several calls) and a single-column key only. If the
source result hit `max_rows`, the response carries a note saying the join
ran on an incomplete set of ids.

`limit` caps the rows coming back from the target only. The source step
is always capped by the connection's own `max_rows`, because the ids it
collects decide what the second query even asks for.

`source_where` and `target_columns` are raw SQL fragments. Read
[security.md](security.md#raw-sql-in-federated_lookup) before using this
with anything other than agent-generated input. The identifiers around
them come from the map and are quoted, so a mixed-case name or a column
called `group` works.
