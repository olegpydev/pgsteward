# Security model

For reporting vulnerabilities and deployment recommendations, see
[SECURITY.md](../SECURITY.md). This document explains how the guarantees
are implemented and where they stop.

## Three layers

### Layer 1 — classification before execution

`pgsteward/security.py`.

The statement is normalised by masking comments, string literals,
quoted identifiers and dollar-quoted blocks with spaces. Structure is
preserved, so `SELECT 'a;b'` remains one statement rather than two.

The masker models the parts of PostgreSQL's lexer that decide where a
literal ends, because that is what decides where a `;` is:

| Construct | Handling |
|---|---|
| `'...'` | `''` is an escaped quote; a backslash is an ordinary character |
| `E'...'` | `\'` is an escaped quote too. The `E` must start a token: in `aE'x'` the lexer reads `aE` as an identifier, so the string is an ordinary one |
| `U&'...'` | as `'...'`; `\0021` is a unicode escape and does not affect quoting |
| `"..."` | `""` is an escaped quote; a backslash is an ordinary character |
| `$tag$...$tag$` | opens only where `$` does not continue an identifier — `a$b$c` is one identifier, not `a` plus a string |
| `/* ... */` | nested, as PostgreSQL does it |
| `-- ...` | to end of line |

All of it assumes `standard_conforming_strings=on`, which the adapter pins
on every pooled connection precisely so the assumption cannot be falsified
by a server, database or role default.

Then:

- `assert_single_statement` rejects multi-statement input. Allowing several
  statements per call would both complicate the response shape and give a
  trivial way to smuggle a write past classification.
- `classify_statement` maps the first significant keyword to
  `READ` / `DML` / `DDL` / `OTHER`. Leading whitespace and parentheses are
  skipped, so `(SELECT ...) UNION (SELECT ...)` classifies correctly.
- `check_statement` compares that class to the effective policy. `OTHER` is
  always refused: if pgsteward cannot tell what a statement does, it does not
  run it.

**This layer is a filter, not a proof.** Classification looks at the first
significant keyword, which is weaker than a real parser; two well-known
shapes classify as READ and still write:

```sql
WITH removed AS (DELETE FROM t RETURNING id) SELECT id FROM removed;
EXPLAIN ANALYZE INSERT INTO t VALUES (1);
```

Neither this classification nor the single-statement rule is what the
guarantee rests on. Layer 2, below, is.

The single-statement rule holds for a separate reason. Every statement the
agent sends is executed through a prepared statement — `conn.fetch` or
`conn.cursor`, never asyncpg's `execute()` — and PostgreSQL's extended
query protocol refuses more than one command in a prepared statement.
`assert_single_statement` exists so a rejection reads as a policy decision
instead of a driver error; the protocol is what makes it true, as
[`tests/test_integration_postgres.py`](../tests/test_integration_postgres.py)
asserts against a live server. A future switch to the simple query
protocol therefore cannot happen quietly.

### Layer 2 — the transaction

`pgsteward/adapters/postgres.py`.

Reads always run inside `BEGIN TRANSACTION READ ONLY`, unconditionally —
not "when the connection is read-only". A write transaction is opened only
for DML/DDL that already passed layer 1.

"Reads" here means every statement pgsteward sends, not just the ones the
agent wrote. Schema exploration (`ping`, `get_server_info`,
`list_databases`, `list_schemas`, `list_tables`, `describe_table`,
`search_schema`) and the health checks go through the same transaction.
Those queries are constants in the source and cannot write anything, but a
boundary that depends on which tool you crossed it with is not a boundary.
Introspection and health checks go through `PostgresAdapter._read_tx`;
`query` and `explain_query` go through `_fetch_under_policy`, which is
separate because it picks the transaction mode from the statement class —
a DML statement the policy allows has to run in a writing one. Both paths
open `readonly=True` for reads. In `tests/test_postgres_adapter.py` a
parametrized test asserts the mode for every introspection entry point and
for the health report, and two further tests assert it for `query`,
including the statements below that only look like reads.

PostgreSQL then refuses both statements above with
`read-only transaction`. This is the actual guarantee: it does not depend
on pgsteward parsing SQL correctly. The integration suite asserts both
cases, including that the data is genuinely unchanged afterwards.

A side effect worth naming: `describe_table` issues several catalog
queries, and inside one transaction they see one snapshot. Without it a
concurrent `ALTER TABLE` could put columns from one version of the
relation next to indexes from another.

### Layer 3 — the session

Every pooled connection is opened with server-side settings:

| Setting | Value |
|---|---|
| `statement_timeout` | `query_timeout` seconds |
| `idle_in_transaction_session_timeout` | `query_timeout` seconds |
| `default_transaction_read_only` | `on`, for connections with `read_only=true` |
| `standard_conforming_strings` | `on`, always |
| `application_name` | `pgsteward` |

`default_transaction_read_only` makes read-only connections refuse writes
even if layers 1 and 2 were both bypassed by a bug.

`standard_conforming_strings` is not a barrier but a precondition of
layer 1: with `off` a backslash escapes inside ordinary `'...'` strings
too, and the masker, which expects that only inside `E'...'`, would
disagree with the server about where a literal ends. `on` has been the
default since PostgreSQL 9.1; pinning it removes the question.

## Known gap: writes disguised as reads

The two shapes shown under layer 1 — a data-modifying CTE, and
`EXPLAIN ANALYZE` around a write — classify as `READ`, and nothing
downstream reclassifies them.

On a read-only connection that is harmless: layer 2 refuses them anyway,
and the only cost is an audit line reading `kind=read` for a statement
that tried to write. On a connection with `read_only=false` and
`allow_dml=true` it becomes a functional limitation rather than a security
one — **these statements do not work**, because layer 1 calls them reads
and layer 2 therefore opens a read-only transaction for them. Write a
plain `DELETE ... RETURNING`, or run the `EXPLAIN` without `ANALYZE`.

`explain_query` says so directly: with `analyze=true` it classifies the
statement it is about to wrap and refuses anything that is not a read, so
the dead end is reported as such instead of surfacing as a driver error
about a read-only transaction. That is only a better message; the gap
above is still there for the same text sent through `query`.

Closing this needs a real PostgreSQL parser, and the obvious one —
`pglast`, wrapping `libpg_query` — is GPL-3.0-or-later against this
project's MIT. Widening the hand-written masker to statement structure
would trade a known limitation for an unknown one, so this stays a
documented gap until data-modifying CTEs are actually needed on a
write-enabled connection.

## Known gap: side effects that are not writes

`READ ONLY` is narrower than "changes nothing". PostgreSQL defines it as
a ban on writing to tables, on DDL and on a handful of commands — not as
a ban on functions with effects outside the transaction. A `SELECT` that
calls one classifies as a read at layer 1, passes layer 2 because it
writes to no table, and runs:

```sql
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE ...
SELECT pg_advisory_lock(42);
SELECT pg_drop_replication_slot('...');
SELECT pg_logical_emit_message(true, 'x', 'y');
```

None of the three layers is the control here. The control is the role,
and it is the usual one: PostgreSQL gates each of these on a privilege,
and `pg_terminate_backend` additionally on `pg_signal_backend` membership
— which every role has over its own sessions. A connection whose role is
also the role your application runs as can therefore end your
application's sessions.

That is one more reason for the dedicated least-privilege role in
[SECURITY.md](../SECURITY.md#recommended-deployment), and it is the place
to close this:

```sql
REVOKE EXECUTE ON FUNCTION
    pg_terminate_backend(integer, bigint),
    pg_cancel_backend(integer),
    pg_advisory_lock(bigint),
    pg_advisory_lock(integer, integer)
  FROM <role>;
```

pgsteward does not keep a deny-list of function names. It would be the
same kind of filter as layer 1 and would fail the same way — one
`search_path` trick, one wrapper function, one signature nobody listed —
while reading as a guarantee. The privilege is enforced by the server on
every call, including the ones nobody thought of.

## Effective policy

`resolve_security(connection, global_policy)`:

| Field | Source |
|---|---|
| `read_only` | The connection. |
| `allow_dml` | The connection if set explicitly, otherwise the global policy. |
| `allow_ddl` | Same. |
| `max_rows` | The connection. |
| `max_bytes` | The connection. |
| `query_timeout` | The connection. |

A write needs `read_only=false` **and** the matching class allowed. Both
default to forbidden, so a fresh install cannot write to anything. The
per-connection override exists so staging and production can share one
process with different policies. Agents can inspect the result through
`get_security_config`, so an agent can find out it may not write before it
tries.

## Row limits and timeouts

Row capping, the size budget beside it, and their one exception on the
write path are described in [tools.md](tools.md#query). What matters here
is where each limit is enforced. The row cursor is server-side and is
read in chunks, so rows beyond either cap are never read at all and a
large result never materialises in the process — which is the point of
capping by size as well as by count, since a thousand rows is six
kilobytes or two megabytes depending only on how wide they are.

Timeouts are enforced by the server too. asyncpg's `command_timeout` only
sends a cancel request, and a query keeps running if that request does not
arrive; `statement_timeout` stops it inside PostgreSQL. `command_timeout`
is still set, deliberately a few seconds higher, so that the server wins
and the client sees a proper PostgreSQL error instead of a cancelled
call.

This bounds the cost of a bad query. It does not prevent one — an agent
can still ask for something expensive to plan or to scan.

## Connection errors

Driver messages are not passed through. asyncpg reports
`password authentication failed for user "readonly_prod"`,
`Connect call failed ('10.0.3.17', 5432)` and
`database "billing" does not exist` — role names, host names, ports and
database names, all of which would otherwise land in the model's context
and in the chat history.

Instead, a failure is classified into one of the six connection-failure
codes listed in [tools.md](tools.md#list_connections) and reported as:

```
Connection "web-shop-prod" is unavailable (auth_failed).
Details are in the pgsteward server log.
```

The original exception, with everything in it, is logged to stderr at
`WARNING` with a traceback. The exception chain is deliberately broken
(`raise ... from None`) so that no framework can re-expose the cause.

Errors raised by PostgreSQL about the *statement* — `permission denied for
table orders`, `column x does not exist` — are passed through unchanged.
They describe the schema, the agent needs them, and they reveal nothing
about the deployment that the agent could not learn from `describe_table`.

A connection whose credentials were never set is a different case, and it
names them:

```
Connection "web-shop" is not configured: WEB_SHOP_PG_HOST,
WEB_SHOP_PG_USER are not set.
```

Variable names carry no host, port, role or database, so the rule above
does not apply, and withholding them would leave the one person who can
fix the problem — the operator, usually sitting in the same chat — with
nothing to act on. The same connection appears in `list_connections` with
`error_code: misconfigured`, so the agent sees it before running into it,
and the rest of the fleet stays usable.

## Where the credentials sit

A password has to reach `asyncpg` in cleartext at connect time, so it is
in the process's memory whatever produced it. A vault, a keychain and a
`.env` file all end in the same place. The question is not whether the
secret is ever in the clear, but where it rests and who can read it there.

pgsteward's answer is the environment, with a `.env` file as a fallback,
and the recommended location is `~/.config/pgsteward/.env` with mode
`600`. What that buys, and what it does not:

| Reader | Stopped? |
|---|---|
| Another user on the machine | Yes, by mode `600` |
| The agent, and anything downstream of it | Yes — credentials are never in a tool argument or result |
| A synced MCP client config | Yes, if the credentials are in the `.env` rather than in the client's `env` block |
| **Another process running as you** | **No** |
| Backup or file sync of `~/.config` | No |

The fourth row is the one that matters on a developer machine, and mode
`600` does nothing about it: a package post-install script, an editor
extension or another MCP server all run as the same user and can read the
file. Treat the credentials as readable by anything you have installed.

pgsteward warns at startup when the file is more permissive than `600`,
which catches the common case of a `.env` copied with the default umask.
It only warns; asyncpg, by comparison, refuses outright to read a
`.pgpass` with those permissions.

### Not storing a password at all

Every one of these is supported today, and each removes the stored
password instead of relocating it:

- **Unix socket with `peer`** — for a local database, no secret exists.
  Needs `<PREFIX>_PG_DSN`, since a socket path has no discrete field.
- **TLS client certificate** — `sslcert` and `sslkey` in the DSN. The key
  is still a file on disk, but a credential scanner grepping for
  `PASSWORD=` will not find it.
- **A short-lived IAM token** (RDS IAM, Cloud SQL IAM) via `PGPASSWORD`
  in the process environment. The only option here that removes the
  long-lived secret entirely — with a caveat. asyncpg re-reads
  `PGPASSWORD` every time the pool opens a connection, but nothing
  refreshes `os.environ`: the value stays whatever the MCP client set when
  it spawned the process. Once the token expires, the next connection the
  pool opens fails, and idle ones are recycled after five minutes. Usable
  for a short session, not for a long-lived server.
- **`.pgpass`** — the same cleartext on the same disk under the same mode
  `600`. Consolidation, not protection.

Leaving `<PREFIX>_PG_PASSWORD` unset or empty is what enables all of
them: an empty value is normalised to "no password", so nothing is put in
the DSN and asyncpg's own `PGPASSWORD` / `.pgpass` / `trust` / `peer`
resolution proceeds untouched.

### What this is worth

Less than the database role. An attacker who can read the `.env` is
already running code as you and has other ways to reach the same data;
what bounds the damage is what the role is allowed to see. Narrowing the
grants is therefore worth more than any scheme for storing the password
more carefully. The grants worth starting from are in
[SECURITY.md](../SECURITY.md#recommended-deployment).

## Audit log

Every tool call logs one structured line at `INFO`, whether it succeeded
or not. `PGSTEWARD_LOG_LEVEL=WARNING` therefore turns the audit trail off;
if you are running one, keep the level at `INFO` or below.

```
tool=query connection_id=web-shop elapsed_ms=12.4 outcome=ok
      kind=read row_count=42 truncated=False
```

```
tool=query connection_id=web-shop-prod elapsed_ms=0.4 outcome=error
      error=WriteForbiddenError
```

No statement text. No parameter values. This is deliberate: a query log
that contains SQL and bound parameters is itself a copy of the data, and
logs are routinely shipped somewhere less protected than the database.
For a failure the exception's *class* is recorded, not its message, for
the same reason.

Failures are logged precisely because a refused write is worth as much
to an audit as a successful read. Every tool is covered, not only
`query`: `explain_query` with `analyze=true` executes the statement for
real, and `federated_lookup` runs two of them — it records both
`source_connection_id` and `target_connection_id`.

`kind` is the statement class the policy was actually applied to,
carried back from the adapter rather than re-derived from the text, so
the line cannot disagree with the decision that was made. It appears only
where there is one: `query` returns a classified result, while
`explain_query` and `federated_lookup` log the tool, the connections, the
duration and the outcome without `kind` or `row_count`.

The same reasoning applies to `analyze_db_health`: the
`idle_in_transaction` and `long_running_queries` checks report `pid`,
`usename`, `application_name` and duration for other sessions, but never
`pg_stat_activity.query`.

## Raw SQL in `federated_lookup`

`source_where` and `target_columns` are interpolated into the generated
statement. They are raw SQL fragments and are not sanitised.

This is not a new hole: the agent can already run arbitrary SQL through
`query`, and the generated statement still goes through `execute()`, so it
is classified, checked against policy, limited to one statement, and run in
a `READ ONLY` transaction like everything else.

It does mean those two parameters must never carry untrusted third-party
input — a string that reached the agent from an end user, rather than one
the agent wrote itself. Where that can happen, treat `federated_lookup`
exactly as you would treat `query`.

The schema, table and column names around them come from the relationship
map rather than from the agent, and they are quoted. Not as a barrier —
the operator writes that file — but because an unquoted identifier is
silently lowercased and breaks outright on anything that needs quoting, a
reserved word such as a column named `group` included. A name that no
amount of quoting would save, empty or holding a NUL, is refused when the
map is read, so the failure lands at startup rather than on a call.

## What this does not do

pgsteward does not restrict *which data* can be read. Anything the
configured database role can read, the agent can read. Scoping access is
the database role's job, and the grants worth starting from are in
[SECURITY.md](../SECURITY.md#recommended-deployment).

It also does not transform what it returns. Values reach the model as
PostgreSQL produced them; there is no layer in between that rewrites,
truncates or replaces them. Errors about a statement are passed through
for the same reason — they describe the schema, and the agent can read
the schema anyway — but note that a PostgreSQL message quotes offending
values as well as object names: `invalid input syntax for type integer:
"..."` carries whatever was in the statement.

## Hiding columns from the agent

Because pgsteward neither restricts nor transforms what it returns, the
way to keep a column away from the agent is to make it unreadable for the
connection role. PostgreSQL then enforces it, and the outcome does not
depend on what SQL the agent writes — an alias, an expression or a
whole-row `to_jsonb(t)` all hit the same check.

### Column-level grants

```sql
REVOKE SELECT ON public.clients FROM mcp_agent;
GRANT  SELECT (client_id, city, created_at) ON public.clients TO mcp_agent;
```

Revoking the table-level `SELECT` is the part that matters: while it is
held it covers every column, and the column grant adds nothing.

The cost is maintenance. `GRANT SELECT ON ALL TABLES IN SCHEMA` no longer
expresses what you want, so grants are written per table, and a column
added later is unreadable until you say otherwise. That default is the
right way round — a new PII column is closed, not open — but it is a
standing task rather than a one-off.

### A view

When the agent needs the shape of a value rather than the value:

```sql
CREATE VIEW public.clients_safe AS
SELECT client_id, city, md5(last_name) AS last_name_key, left(phone, 4)
FROM public.clients;

REVOKE SELECT ON public.clients      FROM mcp_agent;
GRANT  SELECT ON public.clients_safe TO   mcp_agent;
```

`md5(last_name)` keeps rows groupable and joinable without disclosing the
name. Note that a hash is a pseudonym, not an erasure: on a low-cardinality
column — a city, a status, a gender — anyone who can guess the domain can
rebuild the mapping.

To have the agent land on the view without naming a second schema, put the
search path in the connection URI:

```
<PREFIX>_PG_DSN=postgresql://mcp_agent@db/shop?search_path=safe,public
```

That is a convenience. `SELECT set_config('search_path', ...)` classifies
as a read and is allowed, so the search path is reachable from the agent's
side. What keeps the base table out of reach is the `REVOKE`, not the
search path.

### What pgsteward does on its side

Start with what PostgreSQL does not do: it does not treat a column *name*
as a secret. `pg_attribute` is world-readable, and any role that can reach
the catalog can list the columns of any relation. `REVOKE SELECT (salary)`
protects the values, not the identifier. What pgsteward does below is
therefore hygiene in what lands in the model's context — the boundary is
the `REVOKE`, and it holds whatever `describe_table` prints.

Columns the role cannot read are left out of the `columns` list in
`describe_table` and out of `search_schema`: both filter on
`has_column_privilege`, which is also why a column added to a table after
the grants were written stays invisible until it is granted.

`describe_table` reports **how many** columns it left out:

```
"notes": ["1 column exists on this relation but is not listed: the
          connection role has no SELECT privilege on it. SELECT * will
          fail here; only the columns listed above can be read."]
```

Without that line a hidden column is indistinguishable from one that was
never there, and the failure is silent in the worst way: the agent reports
to the user that the database does not hold the data. The count is
deliberate — it is enough to stop the agent writing `SELECT *` and enough
to stop it concluding the field does not exist, without listing the
columns in the `columns` array.

### Where the name still appears

`primary_key`, `foreign_keys` and `indexes` are read from the catalog
without that filter, so a restricted column is named there whenever it
takes part in a key or an index. This is deliberate, and it costs nothing
real: the name was never protected — the same `SELECT attname FROM
pg_attribute` is open to the role either way — while the structure is
worth a great deal to an agent reasoning about a query plan. Dropping an
index from the answer because one of its columns is restricted would leave
the agent guessing why a query is slow.

Filtering them is also not the one-line predicate it looks like, which is
worth recording so the question does not get reopened cheaply. A column
can reach an index through three different catalog paths: `indkey` carries
key and `INCLUDE` positions but writes a zero where the position is an
expression, and does not mention a partial index's predicate columns at
all; `pg_depend` carries both of those, but holds no column dependencies
for the index behind a primary key constraint. Neither source alone covers
every index, so any such filter has to union the two — and then decide
what to return for a composite key half of which is readable, where a
shortened `primary_key` claims a uniqueness that does not hold.

What follows for an operator: if the *name* of a column must not reach the
model, a `REVOKE` on that column is not the tool. Put the agent on a view
that does not have the column at all — see the recipe above.

## Monitoring privileges

The same isolation that keeps one role out of another role's data also
keeps it out of that role's *session state*. PostgreSQL blanks `state`,
`xact_start`, `query_start` and `backend_type` in `pg_stat_activity` for
backends the role may not inspect, and `pg_sequences` does the same with
`last_value`.

The security-relevant part is that this masking is silent: a restricted
view returns rows rather than an error. That is why the health checks
verify the grant explicitly instead of trusting an empty result, and
report `skipped` when they cannot see what they would need. Which check
needs which grant is the matrix in
[tools.md](tools.md#privileges-and-statistics-required).

A read-only MCP server pointed at a role with full access is a read-only
window onto everything.
