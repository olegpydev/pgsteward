# Security Policy

## Reporting a vulnerability

Please report security issues privately via
[GitHub Security Advisories](https://github.com/olegpydev/pgsteward/security/advisories/new)
rather than a public issue.

## Threat model

pgsteward gives an LLM agent the ability to run SQL against a database you
configured. Read [docs/security.md](docs/security.md) before pointing it at
anything that matters. The short version:

**What pgsteward guarantees**

- Credentials live on the server. Connection strings and passwords never
  enter the model's context; the agent only ever sees a `connection_id`.
  Connection failures are reported as a stable error code, so host names,
  ports, role names and database names stay in the server log too.
- Reads always execute inside `BEGIN TRANSACTION READ ONLY`, regardless of
  connection flags and regardless of which tool issued them — schema
  exploration and health checks included. This is enforced by PostgreSQL,
  not by the SQL parser. Read-only connections also set
  `default_transaction_read_only=on`.
- Writes are refused unless the connection has `read_only=false` *and* the
  effective policy allows that statement class (`allow_dml` / `allow_ddl`).
  Both default to forbidden.
- One statement per call. Multi-statement input is rejected before
  execution, and cannot happen anyway: every statement runs as a prepared
  statement, and PostgreSQL's extended query protocol refuses more than one
  command in one.
- The audit log records the tool, the connections it touched, timing and
  outcome for every call — and, for `query`, the statement class and row
  count. Never the statement text or parameter values.
- A health check that cannot see the data it needs reports `skipped`,
  naming the missing grant, rather than reporting `ok`. One check failing
  outright does not take the other ten with it.

**What pgsteward does not guarantee**

- It does not stop an agent from reading any data the configured database
  user can read, and it does not transform the values it returns. Access
  scoping is your database role's job, not pgsteward's. Use a dedicated
  read-only role with the narrowest grants that work; for keeping
  particular columns out of reach, see
  [docs/security.md](docs/security.md#hiding-columns-from-the-agent).
- It does not sanitise `federated_lookup`'s `source_where` and
  `target_columns`, which are raw SQL fragments. This is the same trust
  level as the `query` tool — the agent can already run arbitrary SQL —
  but it means those parameters must never carry untrusted third-party
  input.
- The SQL classifier is a first barrier, not a proof: it looks at the
  first significant keyword, which is weaker than a real SQL parser. The
  `READ ONLY` transaction is the actual guarantee. One consequence is
  visible in normal use: a data-modifying CTE and `EXPLAIN ANALYZE` around
  a write are classified as reads, so they are refused even on a connection
  whose policy allows writes. See
  [docs/security.md](docs/security.md#known-gap-writes-disguised-as-reads).
- `READ ONLY` bans writing to tables, not every effect. A `SELECT` calling
  `pg_terminate_backend`, `pg_advisory_lock` or `pg_drop_replication_slot`
  passes all three layers, because none of them writes to a table. What
  stops those is the role's privileges — and over its own sessions every
  role may signal itself, so a connection sharing a role with your
  application can end that application's sessions. What to revoke:
  [docs/security.md](docs/security.md#known-gap-side-effects-that-are-not-writes).
- It does not protect against an agent running an expensive query.
  `max_rows` and a server-side `statement_timeout` bound the damage; they
  do not eliminate it.
- Errors raised by PostgreSQL about a statement are passed through
  unchanged, so schema names, table names and column names can appear in
  the transcript. The agent can read those through `describe_table`
  anyway, but a deployment that treats the schema itself as secret should
  know this. Values can appear too, not only names: PostgreSQL quotes the
  offending input in messages such as
  `invalid input syntax for type integer: "..."`.
- It does not encrypt the credentials it is given. They live in the
  process environment or in a `.env` file, in cleartext. What file
  permissions buy and what they do not:
  [docs/security.md](docs/security.md#where-the-credentials-sit).

## Recommended deployment

- Dedicated PostgreSQL role, read-only, granted only on the schemas the
  agent genuinely needs. This is the control that actually bounds the
  damage; everything below it is secondary.
- Where individual columns must stay out of reach — personal data, secrets
  — use column-level grants or a view, and let PostgreSQL enforce it.
  Recipes and their trade-offs:
  [docs/security.md](docs/security.md#hiding-columns-from-the-agent).
- Authenticate without storing a password where you can. Leave
  `<PREFIX>_PG_PASSWORD` unset and use a Unix socket with `peer` locally,
  a TLS client certificate (`sslcert`/`sslkey` in `<PREFIX>_PG_DSN`), or a
  short-lived IAM token in `PGPASSWORD`.
- If a password is unavoidable, keep it in `~/.config/pgsteward/.env` with
  `chmod 600` rather than in the MCP client's config. Why that location
  wins: [docs/security.md](docs/security.md#where-the-credentials-sit).
- `PGSTEWARD_SECURITY_ALLOW_DML=false` and `PGSTEWARD_SECURITY_ALLOW_DDL=false` globally.
- For production connections, set `<PREFIX>_PG_ALLOW_DML=false` and
  `<PREFIX>_PG_ALLOW_DDL=false` explicitly, so a later relaxation of the
  global policy cannot reach production.
- `sslmode=require` (or stricter) for anything that is not localhost.
