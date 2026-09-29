# Configuration

Everything is environment variables. Nothing about a connection lives in a
file that the agent or the MCP client can read.

Every variable the server reads for itself is prefixed `PGSTEWARD_`. An
MCP client passes its whole environment to the process it spawns, and a
plain `LOG_LEVEL` or `DB_CONNECTIONS` inherited from somewhere else would
silently reconfigure the server. Credential blocks carry a prefix of your
own instead, and the `_PG_` infix keeps them out of the same trouble — see
[Credentials](#credentials).

## Server settings

| Variable | Default | Meaning |
|---|---|---|
| `PGSTEWARD_DB_CONNECTIONS` | *(empty)* | CSV of connection metadata, `id:prefix`. |
| `PGSTEWARD_ENV_SUFFIXES` | *(empty)* | CSV of id suffixes that name an environment — see [Environments](#environments). Unset: every `environment` is `null`, and the relationship map matches connection ids exactly. |
| `PGSTEWARD_SECURITY_ALLOW_DML` | `false` | Globally allow `INSERT`/`UPDATE`/`DELETE`/... |
| `PGSTEWARD_SECURITY_ALLOW_DDL` | `false` | Globally allow `CREATE`/`ALTER`/`DROP`/... |
| `PGSTEWARD_LOG_LEVEL` | `INFO` | Log level for pgsteward's own logger, libraries excluded — `DEBUG` will not show you asyncpg or FastMCP internals. Everything goes to stderr, and nowhere else: a client that discards that stream leaves no log to read. |
| `PGSTEWARD_ENV_FILE` | `~/.config/pgsteward/.env` | Path to a `.env` file (`~` is expanded; an absolute path is safest, since the working directory is chosen by the MCP client). Process environment only — see below. |
| `PGSTEWARD_RELATIONSHIPS_FILE` | `~/.config/pgsteward/relationships.json` | Path to a cross-database relationship map. |

An empty `PGSTEWARD_DB_CONNECTIONS` is not an error: the server starts,
`list_connections` returns an empty list, and every other tool refuses the
`connection_id` it was given, naming the ids that do exist.

A value these variables cannot be read from at all — a malformed
`PGSTEWARD_DB_CONNECTIONS` record, a duplicate connection id, an unknown
log level — stops the server before it serves anything, with one line on
stderr naming the problem and exit code `2`. That is deliberately not how
a broken *connection* behaves: see [Credentials](#credentials).

## Connections

### Metadata

```
PGSTEWARD_DB_CONNECTIONS=web-shop:WEB_SHOP,analytics:ANALYTICS,crm
```

Each record is `id:prefix`:

- **`id`** — what the agent passes as `connection_id`. May contain dashes.
- **`prefix`** — prefix of the environment block holding the credentials.
  Optional; defaults to the id uppercased with `-` and spaces turned into `_`.

A record with a third field is rejected with
`format is id:prefix` rather than partially ignored, so a stale
`id:prefix:postgresql` entry fails loudly instead of hiding a typo.

Two distinct names exist on purpose: `connection_id` is an interface the
agent sees, `prefix` is a detail of how the process is configured. They can
be changed independently.

### Credentials

Read from the environment using the prefix and the `_PG_` infix:

| Variable | Default | Meaning |
|---|---|---|
| `<PREFIX>_PG_HOST` | **required** | |
| `<PREFIX>_PG_USER` | **required** | |
| `<PREFIX>_PG_DATABASE` | **required** | Not derived from the id — see below. |
| `<PREFIX>_PG_PORT` | `5432` | |
| `<PREFIX>_PG_PASSWORD` | *(unset)* | Unset or empty means no password is sent — see [Without a password](#without-a-password). |
| `<PREFIX>_PG_DSN` | *(unset)* | Full connection URI. Replaces `HOST`/`PORT`/`USER`/`DATABASE`, which are then ignored. |
| `<PREFIX>_PG_READ_ONLY` | `true` | Set `false` to make writes *possible* — still subject to policy. |
| `<PREFIX>_PG_SSLMODE` | *(unset)* | Passed to asyncpg, e.g. `require`. Unset means asyncpg's own default, `prefer` — see [Transport](#transport). |
| `<PREFIX>_PG_QUERY_TIMEOUT` | `30` | Seconds, at least 1. Applied as a server-side `statement_timeout`; `command_timeout` is set slightly higher as a backstop. Give the MCP client a longer timeout than this, or it drops the connection before the timeout message arrives — see [integrations](integrations.md). |
| `<PREFIX>_PG_MAX_ROWS` | `1000` | Hard cap on rows returned. `-1` disables the cap; `0` is refused — see [tools.md](tools.md#query). |
| `<PREFIX>_PG_MAX_BYTES` | `100000` | Hard cap on the size of the rows returned, in bytes of the JSON they ship as. `-1` disables it; `0` is refused for the same reason `MAX_ROWS=0` is — see [tools.md](tools.md#query). |
| `<PREFIX>_PG_POOL_MIN` | `1` | Connections held open. |
| `<PREFIX>_PG_POOL_MAX` | `5` | Upper bound on the pool. |
| `<PREFIX>_PG_ALLOW_DML` | inherit global | Per-connection override. |
| `<PREFIX>_PG_ALLOW_DDL` | inherit global | Per-connection override. |

The `_PG_` infix is what reserves those names, and it is not there to say
"PostgreSQL" — the server speaks nothing else. `WEB_SHOP_HOST` and
`CRM_PASSWORD` are the shape docker-compose, CI and any 12-factor app
produce by the dozen, and the whole environment reaches pgsteward. Nor
does the prefix save you on its own: it defaults to the id uppercased, so
`PGSTEWARD_DB_CONNECTIONS=analytics` claims `ANALYTICS_*` without anyone
having chosen it. Three characters against connecting somewhere you did
not mean to.

`HOST`, `USER` and `DATABASE` are required and have no defaults. A
connection that lacks one — most often a typo in the prefix, so that the
whole block is read as empty — is reported by `list_connections` with
`error_code: misconfigured`, and any call against it fails naming the
variables that are missing. It does not take the rest of the fleet down
with it: the other connections keep working, and the server logs a
warning for each unusable one at startup.

`DATABASE` deserves spelling out, because the obvious default is tempting.
Deriving the database name from the `connection_id` would let a naming
convention decide *which database you connect to*: `orders-prod` with no
`ORDERS_PROD_PG_DATABASE` would go to a database called `orders`, and on
a server that also has `orders_v2` nothing would report the mistake. One
explicit line is cheaper than an answer from the wrong database.

A value that cannot be used is handled the same way, and reported
separately from a missing one: `MAX_ROWS=abc`, `QUERY_TIMEOUT=0`,
`POOL_MIN` above `POOL_MAX`. The connection is marked `misconfigured` and
names the variable; the server still starts and the other connections
still work. The rejected value itself never leaves the process — only the
name of the variable holding it.

### Transport

`<PREFIX>_PG_SSLMODE` is unset by default, which leaves asyncpg in its own
default mode, `prefer`: TLS is used when the server offers it and silently
not used when it does not. Nothing in the response or in the driver log
says which of the two happened, so pgsteward logs a warning at startup for
every connection that sets neither `SSLMODE` nor an `sslmode=` in its DSN.
Unix sockets are exempt — there is nothing to encrypt.

It is a warning rather than a refusal because `prefer` is a working
configuration for local development, and an upgrade should not take
connections away from installations that already run. For anything the
traffic leaves the host for, set `require` at least, and `verify-full`
with `sslrootcert` when the network is not trusted.

Use `<PREFIX>_PG_DSN` when the address does not fit into discrete fields
— a Unix socket, `target_session_attrs`, `sslrootcert`, a `service` entry.
It is the whole connection string, carrying the address, the role *and*
the database, so the discrete fields are ignored and their absence is not
an error.

Query parameters the driver does not recognise are sent on as session
settings, which is how a connection gets its own `search_path`:

```
WEB_SHOP_PG_DSN=postgresql://ro_user@db/web_shop?search_path=safe,public
```

pgsteward's own settings win over anything named there and cannot be
unpinned this way: `application_name`, `statement_timeout`,
`idle_in_transaction_session_timeout`, `standard_conforming_strings`, and
`default_transaction_read_only` on read-only connections. The first two
bound a runaway query, the third is what
[the literal masker](security.md#layer-1--classification-before-execution)
assumes — none of them are settings a connection string should be able to
turn off.

Otherwise the DSN is assembled from the discrete fields. The user, the
password and the database name are percent-escaped — a password containing
`@`, `:` or `/` would otherwise break URI parsing. The host and the port
are interpolated as given, so a literal IPv6 address needs
`<PREFIX>_PG_DSN` with the brackets URIs require
(`postgresql://ro_user@[::1]:5432/web_shop`); a hostname that resolves to
IPv6 needs nothing special. Passwords are held as `SecretStr` and masked
whenever settings are logged.

### Without a password

`<PREFIX>_PG_PASSWORD=` with nothing after it means the same as leaving
the line out: pgsteward normalises an empty value to "no password", and
then puts no password in the DSN at all. That is deliberate, because it is
what lets asyncpg run its own resolution — and that is how every
passwordless setup works:

| Method | How |
|---|---|
| Unix socket with `peer` | `<PREFIX>_PG_DSN=postgresql://ro_user@/web_shop?host=/var/run/postgresql` |
| TLS client certificate | `sslcert` and `sslkey` in `<PREFIX>_PG_DSN` |
| Short-lived IAM token | `PGPASSWORD` in the process environment; read once at startup |
| `.pgpass` | `~/.pgpass` or `PGPASSFILE`, matched on host, port, database and user |
| `trust` | Nothing to configure |

The first two remove the stored secret rather than relocating it, and are
the ones worth reaching for first. Which of them fits, and what each is
actually worth, is in
[security.md](security.md#not-storing-a-password-at-all).

`application_name` is always `pgsteward`, so you can spot the server's
sessions in `pg_stat_activity`. Read-only connections additionally set
`default_transaction_read_only=on` on the session.

**One database, one `connection_id`.** PostgreSQL cannot query across
databases within a session, so several databases on the same server mean
several connections.

## Environments

The setup pgsteward is built for usually includes the same system twice:
staging and production, side by side. Both are reachable at any moment,
and nothing in a `connection_id` says which is which — `web-shop` and
`web-shop-prod` are just two strings.

An id ending in one of the suffixes listed in `PGSTEWARD_ENV_SUFFIXES`
reports that suffix as its `environment` in `list_connections`, so an
agent can tell it is about to query production before it does, and a
person reading the transcript can tell which database an answer came from.

The label is information: it refuses nothing, warns about nothing, and
changes no policy. What stops a mistaken write is `read_only` plus
`ALLOW_DML`/`ALLOW_DDL`, set per connection. The list itself has a second
use — see [One map for every environment](#one-map-for-every-environment).

### Choosing the suffixes

```
PGSTEWARD_ENV_SUFFIXES=prod,staging,dev
```

There is no built-in list: naming conventions differ, and a set fixed in
the code would declare every other one nonexistent. Until the variable is
set, every `environment` comes back `null`.

Values are case-insensitive, and the separator before the suffix is always
`-`, so `airprod` is not a production connection.

A `null` also covers an unsuffixed id like `web-shop`, so pgsteward
cannot tell a deliberate absence from a suffix you forgot to list, and
neither guesses nor warns. Call `list_connections` once after changing
the list and check the labels came out as you expect.

### Two environments in one process

```
PGSTEWARD_DB_CONNECTIONS=web-shop,web-shop-prod
PGSTEWARD_ENV_SUFFIXES=prod

WEB_SHOP_PG_HOST=staging-host
WEB_SHOP_PG_USER=readonly_user
WEB_SHOP_PG_DATABASE=web_shop
WEB_SHOP_PG_PASSWORD=...

WEB_SHOP_PROD_PG_HOST=prod-host
WEB_SHOP_PROD_PG_USER=readonly_user
WEB_SHOP_PROD_PG_DATABASE=web_shop
WEB_SHOP_PROD_PG_PASSWORD=...
WEB_SHOP_PROD_PG_ALLOW_DML=false
WEB_SHOP_PROD_PG_ALLOW_DDL=false
```

Both connections live in the same process and are available to the agent
simultaneously. There is no hidden "current environment": every tool call
names its `connection_id` explicitly, and the unsuffixed id — shorter and
more obvious — ends up being the agent's natural default, which is why
staging usually gets it.

Setting `ALLOW_DML`/`ALLOW_DDL` to `false` on production connections is
worth doing even when the global policy already forbids writes: it means a
later `PGSTEWARD_SECURITY_ALLOW_DML=true`, enabled for staging, cannot
silently reach production.

### One map for every environment

The cross-database relationship map is written once for all of them.
Entries name `orders`, and a call made on `orders-staging` or on
`orders-prod` finds the same one — the suffix is not part of what is
matched. The map describes which column links to which, and that does not
change between staging and production; splitting it in two would state the
same fact twice and let the copies drift apart.

That holds for the suffixes listed in `PGSTEWARD_ENV_SUFFIXES`, which is
the second reason to list one and the reason to keep the list in sync with
your ids: a suffix that is not listed is part of the name, so the entry is
not found and `federated_lookup` refuses the pair, naming the ones it does
know. Matching a map entry still decides nothing about where a query is
sent — each step names its own `connection_id`. See
[tools.md](tools.md#describe_relationships).

## Where credentials come from

pgsteward reads process environment variables first, then falls back to a
`.env` file if one is found. The file is located in this order:

1. `PGSTEWARD_ENV_FILE`, if set. `~` is expanded. A relative path is
   resolved against the working directory, which the MCP client chooses —
   prefer an absolute one.
2. `$XDG_CONFIG_HOME/pgsteward/.env`, i.e. `~/.config/pgsteward/.env` unless
   `XDG_CONFIG_HOME` says otherwise.
3. `.env` next to the package sources.

Step 2 is the intended setup for a normal install: passwords stay out of a
config file that tends to get synced or committed, and the client config
shrinks to `{"command": "uvx", "args": ["pgsteward"]}`.

```bash
mkdir -p ~/.config/pgsteward
cp .env.example ~/.config/pgsteward/.env   # then fill it in
chmod 600 ~/.config/pgsteward/.env
```

`chmod 600` keeps other users out, and pgsteward logs a warning at startup
if the file is more permissive. What that buys and what it does not is in
[security.md](security.md#where-the-credentials-sit).

Step 3 only ever matches when running from a repository checkout. Installed
from PyPI, the package lives in `site-packages`, where no `.env` exists. If
none of the three matches, that is not an error: credentials then come
purely from the `env` block of the MCP client config.

Keeping the file somewhere else is fine, but then its absolute path has to
be passed explicitly:

```jsonc
"env": { "PGSTEWARD_ENV_FILE": "/opt/secrets/pgsteward.env" }
```

`PGSTEWARD_ENV_FILE` is read from the process environment only — it is what
selects the file, so setting it *inside* a `.env` has no effect. Every
other variable in this document can come from either source.

The current working directory is deliberately not consulted. Several MCP
clients ignore the `cwd` field for local servers and start the process in
whatever project the user happens to have open, so a stray `.env` there
would silently replace your connections.

## Cross-database relationship map

Optional. The document is taken from `PGSTEWARD_RELATIONSHIPS_FILE` if
set, otherwise from `~/.config/pgsteward/relationships.json`. Note that
this default follows `XDG_CONFIG_HOME`, not `PGSTEWARD_ENV_FILE`: pointing
the `.env` somewhere else does not move the map with it.

The file is read once at startup, so editing it requires a restart. A
missing or unparseable map never stops the server; which tools survive in
each case, and the format itself, are in
[tools.md](tools.md#cross-database-links-optional).
