-- Sample schema for the pgsteward quickstart.
--
-- Small but deliberately not trivial: a foreign key, a unique index, a
-- partial index, a view, an intentionally unused index and a second schema
-- so that search_schema and analyze_db_health have something to report.

CREATE SCHEMA billing;

CREATE TABLE public.customer (
    customer_id bigserial PRIMARY KEY,
    email       text        NOT NULL,
    country     text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX customer_email_uniq ON public.customer (email);

CREATE TABLE public.product (
    product_id bigserial PRIMARY KEY,
    sku        text           NOT NULL,
    title      text           NOT NULL,
    price      numeric(12, 2) NOT NULL CHECK (price >= 0),
    archived   boolean        NOT NULL DEFAULT false
);

CREATE UNIQUE INDEX product_sku_uniq ON public.product (sku);

CREATE TABLE public.customer_order (
    order_id    bigserial   PRIMARY KEY,
    customer_id bigint      NOT NULL REFERENCES public.customer (customer_id),
    status      text        NOT NULL DEFAULT 'new',
    placed_at   timestamptz NOT NULL DEFAULT now(),
    total       numeric(12, 2) NOT NULL DEFAULT 0
);

CREATE INDEX customer_order_customer_idx
    ON public.customer_order (customer_id);

CREATE INDEX customer_order_open_idx
    ON public.customer_order (placed_at)
    WHERE status <> 'done';

-- Never queried by anything below: analyze_db_health should list it under
-- unused_indexes once the table is large enough.
CREATE INDEX customer_order_total_idx ON public.customer_order (total);

CREATE TABLE public.order_item (
    order_item_id bigserial PRIMARY KEY,
    order_id      bigint    NOT NULL REFERENCES public.customer_order (order_id),
    product_id    bigint    NOT NULL REFERENCES public.product (product_id),
    quantity      int       NOT NULL CHECK (quantity > 0),
    unit_price    numeric(12, 2) NOT NULL
);

CREATE INDEX order_item_order_idx ON public.order_item (order_id);

-- Second schema: list_tables defaults to "public" and would not find this,
-- but search_schema will.
CREATE TABLE billing.invoice (
    invoice_id  bigserial PRIMARY KEY,
    order_id    bigint         NOT NULL,
    customer_id bigint         NOT NULL,
    amount      numeric(12, 2) NOT NULL,
    paid        boolean        NOT NULL DEFAULT false,
    issued_at   timestamptz    NOT NULL DEFAULT now()
);

CREATE INDEX invoice_customer_idx ON billing.invoice (customer_id);

CREATE VIEW public.open_order AS
SELECT o.order_id, o.customer_id, c.email, o.total, o.placed_at
FROM public.customer_order o
JOIN public.customer c USING (customer_id)
WHERE o.status <> 'done';

-- --- data ------------------------------------------------------------------

INSERT INTO public.customer (email, country, created_at)
SELECT
    'customer' || g || '@example.test',
    (ARRAY['DE', 'FR', 'NL', 'PL', 'ES'])[1 + (g % 5)],
    now() - (g || ' hours')::interval
FROM generate_series(1, 2000) AS g;

INSERT INTO public.product (sku, title, price, archived)
SELECT
    'SKU-' || lpad(g::text, 5, '0'),
    'Product ' || g,
    round((5 + (g % 300) * 1.37)::numeric, 2),
    g % 25 = 0
FROM generate_series(1, 500) AS g;

INSERT INTO public.customer_order (customer_id, status, placed_at, total)
SELECT
    1 + (g % 2000),
    (ARRAY['new', 'paid', 'shipped', 'done'])[1 + (g % 4)],
    now() - (g || ' minutes')::interval,
    round((10 + (g % 900) * 1.11)::numeric, 2)
FROM generate_series(1, 8000) AS g;

INSERT INTO public.order_item (order_id, product_id, quantity, unit_price)
SELECT
    1 + (g % 8000),
    1 + (g % 500),
    1 + (g % 4),
    round((5 + (g % 300) * 1.37)::numeric, 2)
FROM generate_series(1, 20000) AS g;

INSERT INTO billing.invoice (order_id, customer_id, amount, paid, issued_at)
SELECT o.order_id, o.customer_id, o.total, o.status = 'done', o.placed_at
FROM public.customer_order o
WHERE o.status <> 'new';

-- Populate planner statistics so describe_table reports a row estimate.
ANALYZE;

-- Read-only role, as recommended in SECURITY.md.
CREATE ROLE readonly_user LOGIN PASSWORD 'readonly';
GRANT CONNECT ON DATABASE web_shop TO readonly_user;
GRANT USAGE ON SCHEMA public, billing TO readonly_user;
GRANT SELECT ON ALL TABLES IN SCHEMA public, billing TO readonly_user;
