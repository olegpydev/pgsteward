# Quickstart

A seeded PostgreSQL and a ready-made pgsteward config, so you can try the
tools without pointing anything at a real database.

## 1. Start the database

```bash
cd examples/quickstart
docker compose up -d
```

This brings up PostgreSQL 17 on **port 55432** (not 5432, to avoid
clashing with a local install) and loads `seed.sql`: a small web-shop
schema in `public`, an `invoice` table in a second `billing` schema, a
view, a partial index, a deliberately unused index, ~30k rows, and a
`readonly_user` role with `SELECT`-only grants.

Wait for it to report healthy:

```bash
docker compose ps
```

## 2. Point your MCP client at it

Add this to your client config ([full list of clients and file
locations](../../docs/integrations.md)):

```jsonc
{
  "mcpServers": {
    "pgsteward": {
      "command": "uvx",
      "args": ["pgsteward"],
      "env": {
        "PGSTEWARD_DB_CONNECTIONS": "web-shop:WEB_SHOP",
        "WEB_SHOP_PG_HOST": "127.0.0.1",
        "WEB_SHOP_PG_PORT": "55432",
        "WEB_SHOP_PG_DATABASE": "web_shop",
        "WEB_SHOP_PG_USER": "readonly_user",
        "WEB_SHOP_PG_PASSWORD": "readonly",
        "PGSTEWARD_RELATIONSHIPS_FILE": "/absolute/path/to/pgsteward/examples/relationships.example.json"
      }
    }
  }
}
```

The relationship map is optional (see
[configuration.md](../../docs/configuration.md)); include it to try
`describe_relationships` and `federated_lookup`, which are conditional tools
that register only when configured. Note that the example map references
connections named `orders`, `crm` and `billing`, which do not exist in this
quickstart — it is there to show the format, not to be joined against.

## 3. Things to ask

Each of these exercises a different tool:

- *"What connections do you have?"* — `list_connections`; add
  "check they are alive" to make it probe
- *"What's in this database?"* — `list_schemas`, `list_tables`
- *"Which tables have a customer_id column?"* — `search_schema`, and note
  that it finds `billing.invoice` outside the default `public` schema
- *"Describe the customer_order table."* — `describe_table`, including the
  foreign keys and the row estimate
- *"How many orders are still open, by country?"* — `query`
- *"Why might that query be slow? Are the indexes used?"* — `explain_query`
- *"Is anything wrong with this database?"* — `analyze_db_health`; on a
  freshly seeded database expect `unused_indexes` to stay quiet until the
  tables grow past the 5MB threshold, `replication_lag` to be `skipped`
  because there are no replicas, and `sequence_exhaustion` to be `skipped`
  because `readonly_user` is granted nothing on sequences. The last one is
  the point of the design: PostgreSQL blanks `last_value` instead of
  raising, so a check that trusted the result would report `ok` having
  measured nothing
- *"Delete the archived products."* — refused. The connection is read-only
  and the global policy forbids DML.

## 4. Clean up

```bash
docker compose down -v
```
