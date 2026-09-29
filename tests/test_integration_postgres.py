"""Тесты против живой PostgreSQL.

Здесь проверяется только то, чего не покажет фейковый пул:
интроспекционный SQL, барьер READ ONLY транзакции и поведение проверок
под ролью с урезанными правами. Остальное — в юнит-тестах.

Требуют `PGSTEWARD_TEST_DSN`; без него модуль пропускается целиком
(см. `conftest.py`). Фикстура создаёт и удаляет собственную схему.
"""

import json
import math
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio
from pydantic import SecretStr

from pgsteward.adapters.postgres import PostgresAdapter
from pgsteward.config import (
    CONNECTIONS_ENV,
    ConnectionConfig,
    SecurityPolicy,
    get_app_config,
)
from pgsteward.dependencies import close_registry
from pgsteward.errors import (
    QueryTimeoutError,
    RelationNotFoundError,
    WriteForbiddenError,
)
from pgsteward.extensions.federation import (
    RELATIONSHIPS_FILE_ENV,
    load_relationships,
    run_federated_lookup,
)
from pgsteward.health import CHECK_FAILED_MESSAGE
from pgsteward.models import HealthCheckResult
from pgsteward.serialization import to_json

pytestmark = pytest.mark.integration

# Имя схемы начинается с "pg": незаэкранированный предикат
# `NOT LIKE 'pg_%'` трактует `_` как wildcard и скрыл бы такие схемы из
# list_schemas/search_schema.
SCHEMA = 'pgsteward_it'
# Отдельная схема нужна ровно для одного: проверить, что FK в другую
# схему возвращается вместе с её именем.
REF_SCHEMA = 'pgsteward_it_ref'

_SETUP_SQL = f"""
DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;
DROP SCHEMA IF EXISTS {REF_SCHEMA} CASCADE;
CREATE SCHEMA {SCHEMA};
CREATE SCHEMA {REF_SCHEMA};

CREATE TABLE {SCHEMA}.customer (
    customer_id bigserial PRIMARY KEY,
    email       text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX customer_email_uniq ON {SCHEMA}.customer (email);

CREATE TABLE {SCHEMA}.customer_order (
    order_id    bigserial PRIMARY KEY,
    customer_id bigint NOT NULL
        REFERENCES {SCHEMA}.customer (customer_id),
    total       numeric(12, 2) NOT NULL DEFAULT 0
);

CREATE INDEX customer_order_customer_idx
    ON {SCHEMA}.customer_order (customer_id);

-- Составной PK, объявленный в порядке, обратном порядку колонок в
-- таблице: сортировка по attnum дала бы [id, tenant_id].
CREATE TABLE {SCHEMA}.tenant_document (
    id        bigint NOT NULL,
    tenant_id bigint NOT NULL,
    slug      text NOT NULL,
    title     text NOT NULL,
    PRIMARY KEY (tenant_id, id)
);

-- Составной FK: пары (tenant_id -> tenant_id) и (doc_id -> id).
CREATE TABLE {SCHEMA}.tenant_document_line (
    line_id   bigserial PRIMARY KEY,
    doc_id    bigint NOT NULL,
    tenant_id bigint NOT NULL,
    FOREIGN KEY (tenant_id, doc_id)
        REFERENCES {SCHEMA}.tenant_document (tenant_id, id)
);

-- PK с INCLUDE: `label` лежит в indkey, но частью ключа не является.
CREATE TABLE {SCHEMA}.article (
    article_id bigint NOT NULL,
    label      text NOT NULL,
    PRIMARY KEY (article_id) INCLUDE (label)
);

-- Имя со смешанным регистром: to_regclass без квотирования приводит его
-- к нижнему регистру и возвращает NULL.
CREATE TABLE {SCHEMA}."MixedCase" (
    "Id"    bigint NOT NULL,
    "Value" text,
    PRIMARY KEY ("Id")
);

CREATE INDEX tenant_document_slug_lower_idx
    ON {SCHEMA}.tenant_document (lower(slug));

CREATE INDEX tenant_document_slug_covering_idx
    ON {SCHEMA}.tenant_document (slug) INCLUDE (title);

CREATE TABLE {REF_SCHEMA}.country (
    code text PRIMARY KEY
);

CREATE TABLE {SCHEMA}.shipping_address (
    address_id   bigserial PRIMARY KEY,
    country_code text NOT NULL
        REFERENCES {REF_SCHEMA}.country (code)
);

-- Materialized view: в information_schema его нет вообще, поэтому
-- интроспекция по нему отдавала отношение без единой колонки.
CREATE MATERIALIZED VIEW {SCHEMA}.customer_summary AS
SELECT customer_id, email FROM {SCHEMA}.customer;

CREATE VIEW {SCHEMA}.customer_view AS
SELECT customer_id, email FROM {SCHEMA}.customer;

-- Секционированная таблица: relkind 'p', а не 'r'.
CREATE TABLE {SCHEMA}.event_log (
    event_id   bigint NOT NULL,
    logged_at  date   NOT NULL
) PARTITION BY RANGE (logged_at);

CREATE TABLE {SCHEMA}.event_log_2024 PARTITION OF {SCHEMA}.event_log
    FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');

-- Две группы дублей на одной таблице: одного имени таблицы для
-- устойчивого порядка мало, а без ORDER BY внутри array_agg
-- нестабилен и состав каждой группы.
CREATE TABLE {SCHEMA}.duplicate_index_table (a int, b int);
CREATE INDEX dup_a_2 ON {SCHEMA}.duplicate_index_table (a);
CREATE INDEX dup_a_1 ON {SCHEMA}.duplicate_index_table (a);
CREATE INDEX dup_a_3 ON {SCHEMA}.duplicate_index_table (a);
CREATE INDEX dup_b_2 ON {SCHEMA}.duplicate_index_table (b);
CREATE INDEX dup_b_1 ON {SCHEMA}.duplicate_index_table (b);

-- Пара таблиц для federated_lookup. Имена — зарезервированные слова и
-- смешанный регистр: без quote_ident PostgreSQL приводит их к нижнему
-- регистру и спотыкается на синтаксисе, а оператор узнаёт об этом
-- только от агента.
CREATE TABLE {SCHEMA}."Order" (
    order_no int  NOT NULL,
    "user"   bigint NOT NULL
);

CREATE TABLE {SCHEMA}.member (
    "user" bigint PRIMARY KEY,
    "desc" text   NOT NULL
);

INSERT INTO {SCHEMA}."Order" (order_no, "user")
VALUES (1, 10), (2, 20), (3, 20), (4, 99);

INSERT INTO {SCHEMA}.member ("user", "desc")
VALUES (10, 'ten'), (20, 'twenty'), (30, 'thirty');

-- Колонка, удалённая после создания: запись остаётся в pg_attribute с
-- attisdropped до перезаписи таблицы.
CREATE TABLE {SCHEMA}.dropped_column_table (
    keep_me    text NOT NULL,
    drop_me    text
);
ALTER TABLE {SCHEMA}.dropped_column_table DROP COLUMN drop_me;

INSERT INTO {SCHEMA}.customer (email)
SELECT 'user' || g || '@example.test' FROM generate_series(1, 50) AS g;

INSERT INTO {SCHEMA}.customer_order (customer_id, total)
SELECT customer_id, (customer_id * 10)::numeric FROM {SCHEMA}.customer;

ANALYZE {SCHEMA}.customer;
ANALYZE {SCHEMA}.customer_order;
ANALYZE {SCHEMA}."MixedCase";
"""

_TEARDOWN_SQL = (
    f'DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; '
    f'DROP SCHEMA IF EXISTS {REF_SCHEMA} CASCADE;'
)

# Роль без pg_monitor и без прав на sequences — та самая конфигурация,
# которую рекомендует docs/security.md ("LOGIN only, no SUPERUSER").
LIMITED_ROLE = 'pgsteward_it_limited'
LIMITED_PASSWORD = 'pgsteward_it_limited_pwd'

_LIMITED_ROLE_SETUP_SQL = f"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{LIMITED_ROLE}') THEN
        EXECUTE 'DROP OWNED BY {LIMITED_ROLE}';
        EXECUTE 'DROP ROLE {LIMITED_ROLE}';
    END IF;
END
$$;
CREATE ROLE {LIMITED_ROLE} LOGIN PASSWORD '{LIMITED_PASSWORD}';
DO $$
BEGIN
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO {LIMITED_ROLE}', current_database()
    );
END
$$;
GRANT USAGE ON SCHEMA {SCHEMA} TO {LIMITED_ROLE};
GRANT SELECT ON {SCHEMA}.customer TO {LIMITED_ROLE};
-- Только на две из трёх колонок: pg_attribute прав не учитывает, и без
-- has_column_privilege роль увидела бы `created_at`.
GRANT SELECT (order_id, customer_id) ON {SCHEMA}.customer_order
    TO {LIMITED_ROLE};
-- Никаких прав на sequences: pg_sequences покажет их роли, но с NULL в
-- last_value.
"""

_LIMITED_ROLE_TEARDOWN_SQL = f"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{LIMITED_ROLE}') THEN
        EXECUTE 'DROP OWNED BY {LIMITED_ROLE}';
        EXECUTE 'DROP ROLE {LIMITED_ROLE}';
    END IF;
END
$$;
"""

VALID_STATUSES = {'ok', 'warning', 'critical', 'skipped'}

ALL_CHECK_IDS = {
    'connection_utilization',
    'idle_in_transaction',
    'long_running_queries',
    'buffer_cache_hit_rate',
    'invalid_indexes',
    'invalid_constraints',
    'unused_indexes',
    'duplicate_indexes',
    'transaction_id_wraparound',
    'sequence_exhaustion',
    'replication_lag',
}

# Проверки, читающие только мировидимые каталоги: ни грантов сверх
# доступа к каталогу, ни окна накопленной статистики им не нужно.
CATALOG_ONLY_CHECKS = {
    'connection_utilization',
    'invalid_indexes',
    'invalid_constraints',
    'duplicate_indexes',
    'transaction_id_wraparound',
}


def _with_credentials(dsn: str, user: str, password: str) -> str | None:
    """DSN того же сервера под другой ролью; `None`, если DSN не URI."""
    parts = urlsplit(dsn)
    if parts.scheme not in ('postgres', 'postgresql') or not parts.hostname:
        return None
    netloc = (
        f'{quote(user, safe="")}:{quote(password, safe="")}@{parts.hostname}'
    )
    if parts.port:
        netloc = f'{netloc}:{parts.port}'
    return urlunsplit(parts._replace(netloc=netloc))


@pytest_asyncio.fixture
async def adapter(integration_dsn: str) -> AsyncIterator[PostgresAdapter]:
    """Подключённый адаптер поверх свежесозданной тестовой схемы."""
    raw = await asyncpg.connect(integration_dsn)
    try:
        await raw.execute(_SETUP_SQL)
    finally:
        await raw.close()

    config = ConnectionConfig(
        id='integration',
        dsn=SecretStr(integration_dsn),
        read_only=True,
        max_rows=1000,
        query_timeout=30,
    )
    instance = PostgresAdapter(config, SecurityPolicy())
    await instance.connect()
    try:
        yield instance
    finally:
        await instance.close()
        raw = await asyncpg.connect(integration_dsn)
        try:
            await raw.execute(_TEARDOWN_SQL)
        finally:
            await raw.close()


# --- 1. The READ ONLY barrier ----------------------------------------------
#
# Главная гарантия проекта. Оба statement'а классифицируются парсером как
# READ (первое слово `with` / `explain`), то есть первый барьер их
# пропускает — отклонить их обязана сама БД.


@pytest.mark.parametrize(
    ('label', 'statement'),
    [
        (
            'cte_delete',
            f'WITH removed AS ('
            f'  DELETE FROM {SCHEMA}.customer_order '
            f'  WHERE order_id = 1 RETURNING order_id'
            f') SELECT order_id FROM removed',
        ),
        (
            'explain_analyze_insert',
            f'EXPLAIN ANALYZE '
            f"INSERT INTO {SCHEMA}.customer (email) VALUES ('x@y.test')",
        ),
    ],
)
async def test_read_only_transaction_rejects_disguised_writes(
    adapter: PostgresAdapter, label: str, statement: str
) -> None:
    # Проверяется код, а не текст: сообщения PostgreSQL переводятся, и
    # на сервере с другой локалью сверка по подстроке ловила бы локаль,
    # а не отказ.
    with pytest.raises(asyncpg.ReadOnlySQLTransactionError) as exc_info:
        await adapter.execute(statement)
    assert exc_info.value.sqlstate == '25006'


async def test_read_only_transaction_leaves_data_intact(
    adapter: PostgresAdapter,
) -> None:
    """Отклонённая запись не должна частично примениться."""
    statement = (
        f'WITH removed AS ('
        f'  DELETE FROM {SCHEMA}.customer_order RETURNING order_id'
        f') SELECT order_id FROM removed'
    )
    with pytest.raises(asyncpg.PostgresError):
        await adapter.execute(statement)

    result = await adapter.execute(
        f'SELECT count(*) FROM {SCHEMA}.customer_order'
    )
    assert result.rows[0][0] == 50


@pytest_asyncio.fixture
async def writable_adapter(
    integration_dsn: str,
) -> AsyncIterator[PostgresAdapter]:
    """Адаптер с разрешённым DML — вторая половина политики.

    Путь записи `_fetch_under_policy` до сих пор проверялся только на
    фейковом пуле, то есть никто не подтверждал, что он действительно
    открывает пишущую транзакцию на живом сервере.
    """
    raw = await asyncpg.connect(integration_dsn)
    try:
        await raw.execute(_SETUP_SQL)
    finally:
        await raw.close()

    instance = PostgresAdapter(
        ConnectionConfig(
            id='integration-write',
            dsn=SecretStr(integration_dsn),
            read_only=False,
            allow_dml=True,
        ),
        SecurityPolicy(),
    )
    await instance.connect()
    try:
        yield instance
    finally:
        await instance.close()
        raw = await asyncpg.connect(integration_dsn)
        try:
            await raw.execute(_TEARDOWN_SQL)
        finally:
            await raw.close()


async def test_allowed_dml_commits_on_a_live_server(
    writable_adapter: PostgresAdapter,
) -> None:
    result = await writable_adapter.execute(
        f"INSERT INTO {SCHEMA}.customer (email) VALUES ('new@example.test') "
        'RETURNING customer_id'
    )

    assert result.kind == 'dml'
    assert result.row_count == 1

    # Отдельный вызов — отдельная транзакция: если бы запись не
    # закоммитилась, строки здесь уже не было бы.
    check = await writable_adapter.execute(
        f'SELECT count(*) FROM {SCHEMA}.customer '
        "WHERE email = 'new@example.test'"
    )
    assert check.rows[0][0] == 1


async def test_ddl_stays_forbidden_when_only_dml_is_allowed(
    writable_adapter: PostgresAdapter,
) -> None:
    """`allow_dml` не должен открывать дверь и для DDL."""
    with pytest.raises(WriteForbiddenError):
        await writable_adapter.execute(f'DROP TABLE {SCHEMA}.customer')

    remaining = await writable_adapter.execute(
        f'SELECT count(*) FROM {SCHEMA}.customer'
    )
    assert remaining.rows[0][0] == 50


async def test_server_side_statement_timeout_is_applied(
    adapter: PostgresAdapter,
) -> None:
    """Лимит держит сервер, а не только cancel request драйвера."""
    value = await adapter.execute('SHOW statement_timeout')
    assert value.rows[0][0] == '30s'


async def test_server_side_timeout_surfaces_as_a_domain_error(
    integration_dsn: str,
) -> None:
    """Сработавший `statement_timeout` обязан стать `QueryTimeoutError`.

    Именно он срабатывает первым: `command_timeout` выставлен заведомо
    позже, и клиентский `TimeoutError` в норме недостижим. Наружу же
    asyncpg отдаёт `QueryCanceledError`, который MCP-слой не ловит —
    он перехватывает только доменные ошибки.
    """
    config = ConnectionConfig(
        id='integration-timeout',
        dsn=SecretStr(integration_dsn),
        read_only=True,
        query_timeout=1,
    )
    adapter = PostgresAdapter(config, SecurityPolicy())
    await adapter.connect()
    try:
        with pytest.raises(QueryTimeoutError) as exc_info:
            await adapter.execute('SELECT pg_sleep(5)')
    finally:
        await adapter.close()

    assert 'query_timeout' in str(exc_info.value)


async def test_the_byte_budget_stops_a_real_cursor_early(
    integration_dsn: str,
) -> None:
    """Бюджет должен резать до того, как строки доехали до процесса.

    `max_rows` снят намеренно: тогда единственное, что может
    остановить чтение, — размер, и видно, что курсор действительно
    читается порциями, а не целиком с последующей обрезкой.
    """
    config = ConnectionConfig(
        id='integration-bytes',
        dsn=SecretStr(integration_dsn),
        read_only=True,
        max_rows=-1,
        max_bytes=2_000,
    )
    adapter = PostgresAdapter(config, SecurityPolicy())
    await adapter.connect()
    try:
        result = await adapter.execute(
            "SELECT repeat('x', 200) AS wide FROM generate_series(1, 100000)"
        )
    finally:
        await adapter.close()

    assert result.truncated is True
    assert result.applied_byte_limit == 2_000
    assert result.applied_limit is None
    # Строка занимает чуть больше 200 байт: их влезает около десятка,
    # а не сто тысяч.
    assert 1 < result.row_count < 20
    assert any('cut to fit max_bytes' in n for n in result.notes)


async def test_standard_conforming_strings_is_on_in_the_session(
    adapter: PostgresAdapter,
) -> None:
    """Условие корректности маскировки литералов в security.py.

    При `off` backslash экранирует и в обычных строках; маскировщик
    этого не моделирует и разошёлся бы с сервером.
    """
    value = await adapter.execute('SHOW standard_conforming_strings')
    assert value.rows[0][0] == 'on'


async def test_dsn_parameters_cannot_unpin_the_session_settings(
    integration_dsn: str,
) -> None:
    """Нераспознанный query-параметр DSN — это session setting.

    asyncpg отдаёт остаток query-строки серверу как `server_settings`,
    и тем же путём оператор мог бы снять `statement_timeout` или
    `standard_conforming_strings`. Настройки адаптера обязаны иметь
    приоритет; проверяется здесь, потому что порядок слияния —
    поведение драйвера, а не этого кода.
    """
    parts = urlsplit(integration_dsn)
    hostile = urlunsplit(
        parts._replace(
            query='&'.join(
                filter(
                    None,
                    (
                        parts.query,
                        'statement_timeout=0',
                        'standard_conforming_strings=off',
                        'application_name=not_pgsteward',
                    ),
                )
            )
        )
    )
    config = ConnectionConfig(
        id='integration-hostile-dsn',
        dsn=SecretStr(hostile),
        read_only=True,
        query_timeout=30,
    )
    adapter = PostgresAdapter(config, SecurityPolicy())
    await adapter.connect()
    try:
        timeout = await adapter.execute('SHOW statement_timeout')
        strings = await adapter.execute('SHOW standard_conforming_strings')
        name = await adapter.execute('SHOW application_name')
    finally:
        await adapter.close()

    assert timeout.rows[0][0] == '30s'
    assert strings.rows[0][0] == 'on'
    assert name.rows[0][0] == 'pgsteward'


async def test_multi_statement_is_refused_by_the_protocol_too(
    integration_dsn: str,
) -> None:
    """Настоящий запрет multi-statement — extended query protocol.

    `assert_single_statement` разбирает SQL приблизительно; надёжный
    барьер — prepared statement, куда PostgreSQL несколько команд не
    пускает. Если драйвер однажды переключится на simple protocol,
    барьер останется один, и узнать об этом надо здесь.
    """
    conn = await asyncpg.connect(integration_dsn)
    try:
        with pytest.raises(asyncpg.PostgresSyntaxError) as exc_info:
            await conn.fetch('SELECT 1; SELECT 2')
    finally:
        await conn.close()
    assert exc_info.value.sqlstate == '42601'


async def test_params_are_bound_by_the_server(
    adapter: PostgresAdapter,
) -> None:
    """Значения разбирает PostgreSQL, а не строковая склейка.

    Ассерт на результат, а не на текст: сервер — единственная
    инстанция, чьё «это параметр» что-то значит.
    """
    result = await adapter.execute('SELECT $1::int + $2::int', [1, 2])

    assert result.rows == [[3]]


async def test_a_param_carrying_sql_stays_a_value(
    adapter: PostgresAdapter,
) -> None:
    """Классическая строка-инъекция обязана вернуться как текст.

    Интерполяция вместо bind закрыла бы statement и выполнила `DROP`;
    проверка живой таблицы после вызова ловит и этот исход.
    """
    payload = f"'; DROP TABLE {SCHEMA}.customer; --"

    result = await adapter.execute('SELECT $1::text', [payload])

    assert result.rows == [[payload]]
    survived = await adapter.execute(f'SELECT count(*) FROM {SCHEMA}.customer')
    assert survived.rows[0][0] == 50


async def test_a_param_is_typed_not_quoted(
    adapter: PostgresAdapter,
) -> None:
    """Число приезжает числом, а не строкой в кавычках.

    `WHERE customer_id = $1` на интерполированном значении работал бы
    тоже, поэтому сравнение идёт с параметром в арифметике, где
    подмена типа заметна.
    """
    result = await adapter.execute(
        f'SELECT count(*) FROM {SCHEMA}.customer WHERE customer_id <= $1',
        [10],
    )

    assert result.rows == [[10]]


# --- 2. Health-checks ------------------------------------------------------


async def test_no_check_fails_on_its_own_sql(
    adapter: PostgresAdapter,
) -> None:
    """Сломанный запрос обязан быть виден, а не тонуть в `skipped`.

    `_run_check` превращает любую ошибку уровня запроса в `skipped`,
    поэтому опечатка в имени колонки выглядит ровно как честный отказ
    по правам. Отличает их только сообщение, и это единственный
    ассерт, который ловит регрессию во всех одиннадцати сразу.
    """
    checks = await adapter.analyze_health()

    failed = [c.id for c in checks if c.message == CHECK_FAILED_MESSAGE]

    assert failed == []


async def test_catalog_only_checks_answer_under_any_role(
    adapter: PostgresAdapter,
) -> None:
    """Пять проверок читают только мировидимые каталоги.

    Для них `skipped` не бывает честным исходом ни при какой роли, и
    без этого ассерта их поломка проходила бы зелёной.
    """
    checks = {c.id: c for c in await adapter.analyze_health()}

    for check_id in CATALOG_ONLY_CHECKS:
        assert checks[check_id].status != 'skipped', check_id


async def test_analyze_health_runs_every_check(
    adapter: PostgresAdapter,
) -> None:
    """Все проверки исполняются на живом каталоге и дают валидный статус.

    Одиннадцать запросов к `pg_stat_*`/`pg_class`/`pg_settings` — самый
    большой объём SQL в проекте. Проверяется исполнимость и форма
    ответа: конкретные значения зависят от состояния сервера.
    """
    checks = await adapter.analyze_health()

    assert {c.id for c in checks} == ALL_CHECK_IDS
    assert all(c.status in VALID_STATUSES for c in checks)
    assert all(c.message for c in checks)

    # Под привилегированной ролью скрытых сессий нет, и проверки по
    # pg_stat_activity обязаны остаться содержательными.
    by_id = {c.id: c for c in checks}
    for check_id in ('idle_in_transaction', 'long_running_queries'):
        assert by_id[check_id].details['hidden_backends'] == 0
        assert by_id[check_id].status != 'skipped'
    # Sequences здесь читаются, поэтому проверка должна быть полной.
    assert by_id['sequence_exhaustion'].status != 'skipped'
    assert by_id['sequence_exhaustion'].details['unreadable_sequences'] == 0


async def test_duplicate_indexes_are_stable_between_calls(
    adapter: PostgresAdapter,
) -> None:
    """Два одинаковых вызова обязаны дать одинаковый ответ.

    Без полного `ORDER BY` состав группы и выбор групп под `LIMIT`
    приходили в произвольном порядке.
    """
    first = await adapter.analyze_health()
    second = await adapter.analyze_health()

    def groups(checks: list[HealthCheckResult]) -> list[dict[str, object]]:
        by_id = {c.id: c for c in checks}
        return [
            g
            for g in by_id['duplicate_indexes'].details['groups']
            if g['table'].endswith('.duplicate_index_table')
        ]

    ours = groups(first)

    assert ours == groups(second)
    assert [g['indexes'] for g in ours] == [
        [f'{SCHEMA}.dup_a_1', f'{SCHEMA}.dup_a_2', f'{SCHEMA}.dup_a_3'],
        [f'{SCHEMA}.dup_b_1', f'{SCHEMA}.dup_b_2'],
    ]


async def test_connection_utilization_ignores_background_processes(
    adapter: PostgresAdapter,
) -> None:
    """`max_connections` ограничивает клиентские сессии.

    checkpointer, walwriter и launcher'ы присутствуют в
    pg_stat_activity, но слотов не занимают; счёт по `count(*)`
    завышал утилизацию на число фоновых процессов.
    """
    raw = await adapter.execute('SELECT count(*) FROM pg_stat_activity')
    raw_backends = raw.rows[0][0]

    checks = {c.id: c for c in await adapter.analyze_health()}
    counted = checks['connection_utilization'].details['total']

    assert 0 < counted < raw_backends
    assert (
        checks['connection_utilization'].details['max_connections'] is not None
    )


@pytest_asyncio.fixture
async def limited_role_adapter(
    adapter: PostgresAdapter, integration_dsn: str
) -> AsyncIterator[PostgresAdapter]:
    """Адаптер под ролью без pg_monitor, при живой чужой сессии.

    Соединение суперюзера удерживается открытым на всё время теста: без
    хотя бы одного чужого клиентского бэкенда скрывать нечего, счётчик
    будет нулевым, и проверяемая ветка не сработает.
    """
    limited_dsn = _with_credentials(
        integration_dsn, LIMITED_ROLE, LIMITED_PASSWORD
    )
    if limited_dsn is None:
        pytest.skip('PGSTEWARD_TEST_DSN не URI — DSN роли не собрать')

    admin = await asyncpg.connect(integration_dsn)
    try:
        try:
            await admin.execute(_LIMITED_ROLE_SETUP_SQL)
        except asyncpg.InsufficientPrivilegeError:
            pytest.skip(
                'CREATE ROLE требует суперюзерского PGSTEWARD_TEST_DSN'
            )

        config = ConnectionConfig(
            id='integration-limited',
            dsn=SecretStr(limited_dsn),
            read_only=True,
        )
        instance = PostgresAdapter(config, SecurityPolicy())
        await instance.connect()
        try:
            yield instance
        finally:
            await instance.close()
            await admin.execute(_LIMITED_ROLE_TEARDOWN_SQL)
    finally:
        await admin.close()


async def test_no_check_claims_ok_without_the_data_to_say_so(
    limited_role_adapter: PostgresAdapter,
) -> None:
    """Под least-privilege ролью: ответ по существу либо `skipped`.

    Третьего не дано, и `skipped` обязан называть недостающее право.
    `pg_stat_activity` и `pg_sequences` для роли без грантов отдают
    строки со значимыми колонками, обнулёнными до NULL.
    """
    checks = {c.id: c for c in await limited_role_adapter.analyze_health()}

    assert set(checks) == ALL_CHECK_IDS
    assert all(c.status in VALID_STATUSES for c in checks.values())
    # Под урезанной ролью особенно легко принять сломанный запрос за
    # отказ по правам: исход один и тот же, различает их сообщение.
    assert [
        c.id for c in checks.values() if c.message == CHECK_FAILED_MESSAGE
    ] == []
    for check_id in CATALOG_ONLY_CHECKS:
        assert checks[check_id].status != 'skipped', check_id

    # Чужие сессии: state скрыт.
    for check_id in ('idle_in_transaction', 'long_running_queries'):
        assert checks[check_id].status == 'skipped'
        assert checks[check_id].details['hidden_backends'] > 0

    # Sequences: строки видны, last_value обнулён.
    sequences = checks['sequence_exhaustion']
    assert sequences.status == 'skipped'
    assert sequences.details['unreadable_sequences'] > 0
    assert 'grant' in sequences.message.lower()

    # Утилизация считается по count(*), который виден всем: проверка
    # обязана остаться содержательной, а не уйти в skipped заодно.
    utilization = checks['connection_utilization']
    assert utilization.status in {'ok', 'warning', 'critical'}
    assert utilization.details['hidden_backends'] > 0


async def test_partially_readable_sequences_are_reported_as_partial(
    limited_role_adapter: PostgresAdapter, integration_dsn: str
) -> None:
    """Частично читаемые sequences дают частичный, но честный ответ.

    Проверка отвечает по доступным и сообщает, сколько объектов осталось
    непроверенными.
    """
    admin = await asyncpg.connect(integration_dsn)
    try:
        await admin.execute(
            f'GRANT SELECT ON SEQUENCE {SCHEMA}.customer_customer_id_seq '
            f'TO {LIMITED_ROLE}'
        )
    finally:
        await admin.close()

    checks = {c.id: c for c in await limited_role_adapter.analyze_health()}
    sequences = checks['sequence_exhaustion']

    assert sequences.status != 'skipped'
    assert sequences.details['unreadable_sequences'] > 0
    assert 'Partial result' in sequences.message


# --- 3. describe_table -----------------------------------------------------


async def test_describe_table_reads_columns_pk_fk_and_indexes(
    adapter: PostgresAdapter,
) -> None:
    """PK/FK/индексы собираются джойнами по pg_catalog."""
    schema = await adapter.describe_table('customer_order', SCHEMA)

    assert schema.name == 'customer_order'
    assert schema.schema_ == SCHEMA

    columns = {c.name: c for c in schema.columns}
    assert list(columns) == ['order_id', 'customer_id', 'total']
    # format_type несёт точность; information_schema отдавал голое
    # 'numeric' и прятал (12, 2) в отдельные колонки.
    assert columns['total'].data_type == 'numeric(12,2)'
    assert columns['customer_id'].nullable is False
    assert columns['order_id'].is_primary_key is True
    assert columns['customer_id'].is_primary_key is False

    assert schema.primary_key == ['order_id']

    assert len(schema.foreign_keys) == 1
    fk = schema.foreign_keys[0]
    assert fk.column == 'customer_id'
    assert fk.references_table == 'customer'
    assert fk.references_column == 'customer_id'

    indexes = {i.name: i for i in schema.indexes}
    assert indexes['customer_order_customer_idx'].columns == ['customer_id']
    assert indexes['customer_order_customer_idx'].is_unique is False
    assert indexes['customer_order_pkey'].is_unique is True

    # reltuples заполняется ANALYZE из фикстуры.
    assert schema.extra['estimated_row_count'] == 50
    assert schema.extra['relation_kind'] == 'table'


async def test_describe_table_rejects_an_unknown_relation(
    adapter: PostgresAdapter,
) -> None:
    """Пустой успешный ответ агент прочитал бы как «колонок нет»."""
    with pytest.raises(RelationNotFoundError) as exc_info:
        await adapter.describe_table('no_such_table', SCHEMA)

    message = str(exc_info.value)
    assert 'does not exist' in message
    assert 'search_schema' in message


async def test_describe_table_rejects_a_non_table_relation(
    adapter: PostgresAdapter,
) -> None:
    with pytest.raises(RelationNotFoundError) as exc_info:
        await adapter.describe_table('customer_email_uniq', SCHEMA)

    # Артикль по первой букве: сообщение читает модель, и «a index» в
    # нём — видимый признак склейки строк.
    assert 'is an index' in str(exc_info.value)


@pytest.mark.parametrize(
    ('relation', 'relation_kind', 'expected_columns'),
    [
        ('customer_summary', 'materialized view', ['customer_id', 'email']),
        ('customer_view', 'view', ['customer_id', 'email']),
        ('event_log', 'partitioned table', ['event_id', 'logged_at']),
    ],
)
async def test_describe_table_covers_every_describable_relkind(
    adapter: PostgresAdapter,
    relation: str,
    relation_kind: str,
    expected_columns: list[str],
) -> None:
    """Колонки читаются из pg_attribute, а не из information_schema.

    Materialized view в information_schema нет вовсе, и выборка по
    нему даёт отношение без единой колонки — ответ, неотличимый от
    «таблица без колонок».
    """
    schema = await adapter.describe_table(relation, SCHEMA)

    assert [c.name for c in schema.columns] == expected_columns
    assert schema.extra['relation_kind'] == relation_kind


async def test_describe_table_skips_dropped_columns(
    adapter: PostgresAdapter,
) -> None:
    """DROP COLUMN оставляет запись в pg_attribute до перезаписи."""
    schema = await adapter.describe_table('dropped_column_table', SCHEMA)

    assert [c.name for c in schema.columns] == ['keep_me']


async def test_describe_table_handles_mixed_case_identifiers(
    adapter: PostgresAdapter,
) -> None:
    """Имя в смешанном регистре резолвится закавыченным.

    `to_regclass('s.MixedCase')` без квотирования приводит имя к нижнему
    регистру и возвращает NULL, из-за чего PK и оценка строк молча
    пропадали, хотя колонки и индексы возвращались.
    """
    schema = await adapter.describe_table('MixedCase', SCHEMA)

    assert [c.name for c in schema.columns] == ['Id', 'Value']
    assert schema.primary_key == ['Id']
    assert schema.extra['estimated_row_count'] is not None


async def test_describe_table_pairs_composite_foreign_key_columns(
    adapter: PostgresAdapter,
) -> None:
    """Колонки составного FK сопоставляются попарно, а не перекрёстно.

    `conkey` и `confkey` связаны только порядком: развёрнутые двумя
    независимыми `= ANY(...)`, они дали бы декартово произведение.
    """
    schema = await adapter.describe_table('tenant_document_line', SCHEMA)

    pairs = [(fk.column, fk.references_column) for fk in schema.foreign_keys]
    assert pairs == [('tenant_id', 'tenant_id'), ('doc_id', 'id')]
    assert all(
        fk.references_table == 'tenant_document' for fk in schema.foreign_keys
    )
    assert any('composite foreign key' in n for n in schema.notes)


async def test_describe_table_reports_foreign_key_schema(
    adapter: PostgresAdapter,
) -> None:
    """Без схемы цели FK в другую схему неотличим от FK внутри своей."""
    schema = await adapter.describe_table('shipping_address', SCHEMA)

    assert len(schema.foreign_keys) == 1
    fk = schema.foreign_keys[0]
    assert fk.references_schema == REF_SCHEMA
    assert fk.references_table == 'country'


async def test_describe_table_preserves_declared_primary_key_order(
    adapter: PostgresAdapter,
) -> None:
    """Порядок PK задаётся indkey, а не порядком колонок в таблице."""
    schema = await adapter.describe_table('tenant_document', SCHEMA)
    assert schema.primary_key == ['tenant_id', 'id']


async def test_describe_table_primary_key_excludes_include_columns(
    adapter: PostgresAdapter,
) -> None:
    """INCLUDE-колонка первичного ключа в PK не попадает.

    В `PRIMARY KEY (id) INCLUDE (label)` `label` лежит в indkey, но
    ключом не является.
    """
    schema = await adapter.describe_table('article', SCHEMA)
    assert schema.primary_key == ['article_id']


async def test_describe_table_reports_expression_index(
    adapter: PostgresAdapter,
) -> None:
    """Индекс по выражению отдаётся текстом выражения.

    Позиции-выражения записаны в indkey нулями, строк в pg_attribute для
    них нет.
    """
    schema = await adapter.describe_table('tenant_document', SCHEMA)
    indexes = {i.name: i for i in schema.indexes}

    expression_index = indexes['tenant_document_slug_lower_idx']
    assert expression_index.columns == ['lower(slug)']
    assert expression_index.included_columns == []


async def test_describe_table_separates_index_include_columns(
    adapter: PostgresAdapter,
) -> None:
    """По INCLUDE-колонке индекс не ищет: в `columns` ей не место."""
    schema = await adapter.describe_table('tenant_document', SCHEMA)
    indexes = {i.name: i for i in schema.indexes}

    covering = indexes['tenant_document_slug_covering_idx']
    assert covering.columns == ['slug']
    assert covering.included_columns == ['title']
    assert any('INCLUDE' in n for n in schema.notes)


# --- 4. list_tables and search_schema --------------------------------------


async def test_list_tables_reports_every_describable_relkind(
    adapter: PostgresAdapter,
) -> None:
    """Интроспекция читает pg_catalog, и вот зачем.

    `information_schema.tables` не перечисляет materialized view, так
    что на нём `list_tables` молча терял бы отношение, которое
    `describe_table` прекрасно описывает.
    """
    by_name = {t.name: t for t in await adapter.list_tables(SCHEMA)}

    assert by_name['customer'].kind == 'table'
    assert by_name['customer_view'].kind == 'view'
    assert by_name['customer_summary'].kind == 'materialized view'
    assert by_name['event_log'].kind == 'partitioned table'
    # Секция — самостоятельное отношение relkind 'r' и видна отдельно.
    assert by_name['event_log_2024'].kind == 'table'
    assert all(t.schema_ == SCHEMA for t in by_name.values())


async def test_list_tables_hides_relations_the_role_cannot_read(
    limited_role_adapter: PostgresAdapter,
) -> None:
    """pg_class прав не учитывает — фильтр обязан быть явным.

    information_schema давал этот фильтр даром, и без замены в выдаче
    оказались бы все таблицы схемы.
    """
    names = {t.name for t in await limited_role_adapter.list_tables(SCHEMA)}

    assert 'customer' in names
    # Только column-level грант: has_table_privilege его не видит, а
    # information_schema такую таблицу показывал.
    assert 'customer_order' in names
    assert 'tenant_document' not in names
    assert 'customer_summary' not in names


async def test_describe_table_hides_columns_without_a_grant(
    limited_role_adapter: PostgresAdapter,
) -> None:
    """`GRANT SELECT (order_id, customer_id)` — остальные недоступны."""
    schema = await limited_role_adapter.describe_table(
        'customer_order', SCHEMA
    )

    assert [c.name for c in schema.columns] == ['order_id', 'customer_id']


async def test_describe_table_reports_how_many_columns_are_hidden(
    limited_role_adapter: PostgresAdapter,
) -> None:
    """Скрытая колонка обязана быть отличима от несуществующей.

    Её нет ни в `describe_table`, ни в `search_schema`, поэтому без
    ноты агент заключает, что таких данных база не хранит.
    """
    schema = await limited_role_adapter.describe_table(
        'customer_order', SCHEMA
    )

    note = next((n for n in schema.notes if 'SELECT privilege' in n), None)
    assert note is not None
    # Три колонки, грант на две: скрыта одна — `total`.
    assert note.startswith('1 column exists')
    # У `total` есть DEFAULT, и его выражение не должно быть вычислено
    # для недоступной колонки — оно может нести значение.
    assert all(c.name != 'total' for c in schema.columns)


async def test_describe_table_is_silent_when_every_column_is_readable(
    adapter: PostgresAdapter,
) -> None:
    """Нота платится контекстом, поэтому появляется только по делу."""
    schema = await adapter.describe_table('customer_order', SCHEMA)

    assert not any('SELECT privilege' in n for n in schema.notes)


async def test_select_star_fails_on_a_table_with_column_grants(
    limited_role_adapter: PostgresAdapter,
) -> None:
    """То, о чём предупреждает нота, обязано быть правдой.

    `*` ссылается на каждую колонку отношения, поэтому поколоночный
    грант ломает именно тот запрос, который агент пишет по привычке.
    """
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await limited_role_adapter.execute(
            f'SELECT * FROM {SCHEMA}.customer_order'
        )

    named = await limited_role_adapter.execute(
        f'SELECT order_id, customer_id FROM {SCHEMA}.customer_order LIMIT 1'
    )
    assert named.row_count == 1


async def test_search_schema_respects_privileges(
    limited_role_adapter: PostgresAdapter,
) -> None:
    result = await limited_role_adapter.search_schema('total', schema=SCHEMA)

    assert not any(m.column == 'total' for m in result.matches)


async def test_search_schema_finds_tables_and_columns(
    adapter: PostgresAdapter,
) -> None:
    """UNION с `$4::text IS NULL` — пятипараметрический запрос."""
    result = await adapter.search_schema('customer_order')
    found = {(m.schema_, m.table, m.kind, m.column) for m in result.matches}
    assert (SCHEMA, 'customer_order', 'table', None) in found
    assert result.truncated is False

    columns = await adapter.search_schema('customer_id', schema=SCHEMA)
    hits = {(m.table, m.column) for m in columns.matches if m.kind == 'column'}
    assert ('customer_order', 'customer_id') in hits
    assert ('customer', 'customer_id') in hits


async def test_search_schema_scopes_to_requested_schema(
    adapter: PostgresAdapter,
) -> None:
    """Ветка `$4 IS NOT NULL` должна сужать выдачу до одной схемы."""
    scoped = await adapter.search_schema('customer', schema=SCHEMA)
    assert scoped.matches
    assert {m.schema_ for m in scoped.matches} == {SCHEMA}

    elsewhere = await adapter.search_schema('customer', schema='public')
    assert all(m.schema_ == 'public' for m in elsewhere.matches)
    assert not any(m.table == 'customer_order' for m in elsewhere.matches)


async def test_list_schemas_keeps_user_schemas_starting_with_pg(
    adapter: PostgresAdapter,
) -> None:
    """Отсекать нужно служебные `pg_*`, а не всё, что начинается на "pg".

    `NOT LIKE 'pg_%'` без экранирования подчёркивания убрал бы из выдачи
    `pgbouncer`, `pgq` и любую другую пользовательскую схему.
    """
    schemas = await adapter.list_schemas()
    assert SCHEMA in schemas
    assert 'public' in schemas
    assert not any(s.startswith('pg_') for s in schemas)
    assert 'information_schema' not in schemas


async def test_list_databases_names_the_current_database(
    adapter: PostgresAdapter,
) -> None:
    """Каталожный запрос, который иначе не проверен живым сервером.

    Юнит-тест подтверждает лишь то, что фейк отдаёт ключ `datname`.
    Существует ли такая колонка и отсекает ли `datistemplate` шаблоны,
    отвечает только PostgreSQL.
    """
    databases = await adapter.list_databases()
    current = (await adapter.execute('SELECT current_database()')).rows[0][0]

    assert current in databases
    assert 'template0' not in databases
    assert 'template1' not in databases


# --- 5. explain ------------------------------------------------------------


async def test_explain_returns_planner_estimates_without_timings(
    adapter: PostgresAdapter,
) -> None:
    """Форма `EXPLAIN (FORMAT JSON)` — контракт с конкретной версией PG.

    Без `ANALYZE` PostgreSQL не выводит `Planning Time`/`Execution Time`,
    поэтому оба поля обязаны быть `None`.
    """
    plan = await adapter.explain(
        f'SELECT * FROM {SCHEMA}.customer_order WHERE customer_id = $1',
        params=[1],
    )

    assert plan.plan.get('Node Type')
    assert plan.plan.get('Total Cost') is not None
    assert plan.planning_time_ms is None
    assert plan.execution_time_ms is None


async def test_explain_analyze_reports_timings_and_actual_rows(
    adapter: PostgresAdapter,
) -> None:
    plan = await adapter.explain(
        f'SELECT count(*) FROM {SCHEMA}.customer_order', analyze=True
    )
    assert plan.planning_time_ms is not None
    assert plan.execution_time_ms is not None
    assert plan.plan.get('Actual Rows') is not None


# --- 6. value serialisation ------------------------------------------------
#
# Что именно драйвер отдаёт для bytea и для нечисловых float, можно
# узнать только у живого сервера: фейковый пул вернул бы ровно то, что
# в него положили.


def _reject_constant(name: str) -> float:
    raise AssertionError(f'payload contains the non-JSON literal {name}')


async def test_bytea_round_trips_through_the_tool_boundary(
    adapter: PostgresAdapter,
) -> None:
    """`default=str` дал бы Python-repr `"b'\\x89PNG'"`."""
    result = await adapter.execute(
        r"SELECT '\x89504e47'::bytea AS blob",
    )

    assert isinstance(result.rows[0][0], bytes)
    assert json.loads(to_json(result))['rows'] == [['\\x89504e47']]


async def test_non_finite_floats_leave_as_valid_json(
    adapter: PostgresAdapter,
) -> None:
    """`double precision` хранит NaN и ±Infinity, а JSON — не выражает."""
    result = await adapter.execute(
        "SELECT 'NaN'::float8 AS a, 'Infinity'::float8 AS b, "
        "'-Infinity'::float8 AS c"
    )

    assert all(math.isnan(v) or math.isinf(v) for v in result.rows[0])

    # `parse_constant` вызывается ровно на литералах NaN/Infinity/-Infinity.
    # Подстроки здесь недостаточно: слова `NaN` и `Infinity` есть и в ноте.
    payload = json.loads(to_json(result), parse_constant=_reject_constant)

    assert payload['rows'] == [[None, None, None]]
    assert result.notes and '3 value(s)' in result.notes[0]


async def test_non_finite_floats_inside_an_array_are_reported_too(
    adapter: PostgresAdapter,
) -> None:
    """`float8[]` отдаёт `null` внутри массива на тех же основаниях.

    Нота существует ровно затем, чтобы такой `null` не читался как SQL
    NULL, и разницы между скалярной колонкой и массивом тут нет.
    """
    result = await adapter.execute(
        "SELECT ARRAY['NaN', 'Infinity', 1]::float8[] AS a"
    )

    payload = json.loads(to_json(result), parse_constant=_reject_constant)

    assert payload['rows'] == [[[None, None, 1.0]]]
    assert result.notes and '2 value(s)' in result.notes[0]


# --- 7. connection errors --------------------------------------------------


async def test_connection_failure_reports_a_code_not_the_dsn(
    integration_dsn: str,
) -> None:
    """Сообщения драйвера содержат host, порт, роль и имя базы."""
    parts = urlsplit(integration_dsn)
    broken = urlunsplit(parts._replace(path='/pgsteward_no_such_database_xyz'))
    adapter = PostgresAdapter(
        ConnectionConfig(id='web-shop-prod', dsn=SecretStr(broken))
    )

    with pytest.raises(Exception) as exc_info:
        await adapter.connect()

    rendered = str(exc_info.value)
    assert 'web-shop-prod' in rendered
    assert 'database_not_found' in rendered
    assert 'pgsteward_no_such_database_xyz' not in rendered


async def test_pool_bounds_and_tls_are_accepted_by_the_driver(
    integration_dsn: str,
) -> None:
    """Значения из конфигурации обязаны быть тем, что asyncpg понимает.

    Юнит-тест сверяет словарь `_connect_kwargs`, но принимает его
    драйвер: `sslmode` едет в `ssl=` строкой, а границы пула — числами,
    и ошибка в любом из них видна только на живом подключении.
    """
    config = ConnectionConfig(
        id='integration-pool',
        dsn=SecretStr(integration_dsn),
        sslmode='prefer',
        pool={'min': 2, 'max': 3},
    )
    adapter = PostgresAdapter(config, SecurityPolicy())

    await adapter.connect()
    try:
        assert await adapter.ping() is True
    finally:
        await adapter.close()


async def test_discrete_fields_build_a_dsn_the_driver_accepts(
    integration_dsn: str,
) -> None:
    """Путь без `<PREFIX>_PG_DSN`: адрес собирается из отдельных полей.

    `make_dsn` экранирует части и приклеивает `application_name`;
    проверить это можно только тем, что подключение состоялось и сервер
    увидел то самое имя.
    """
    parts = urlsplit(integration_dsn)
    if parts.scheme not in ('postgres', 'postgresql') or not parts.hostname:
        pytest.skip('PGSTEWARD_TEST_DSN не URI — дискретные поля не собрать')

    config = ConnectionConfig(
        id='integration-discrete',
        host=parts.hostname,
        port=parts.port or 5432,
        user=parts.username or 'postgres',
        password=(
            SecretStr(parts.password) if parts.password is not None else None
        ),
        database=parts.path.lstrip('/'),
    )
    adapter = PostgresAdapter(config, SecurityPolicy())

    await adapter.connect()
    try:
        name = await adapter.execute('SHOW application_name')
    finally:
        await adapter.close()

    assert name.rows[0][0] == 'pgsteward'


# --- 8. federated_lookup ---------------------------------------------------
#
# Единственное место, где pgsteward сам собирает текст statement'а из
# имён. Юнит-тесты сверяют собранную строку, но принимает её PostgreSQL,
# и только он отвечает, закавычено ли `"user"` достаточно и понимает ли
# он `= ANY($1)` на списке id.

SOURCE_CONNECTION = 'fed-source'
TARGET_CONNECTION = 'fed-target'

_FEDERATION_MAP = {
    'version': 1,
    'entities': [
        {
            'id': 'member',
            'description': 'A member, keyed by id in both databases.',
            'keys': [
                {
                    'connection': SOURCE_CONNECTION,
                    'schema': SCHEMA,
                    'table': 'Order',
                    'column': 'user',
                },
                {
                    'connection': TARGET_CONNECTION,
                    'schema': SCHEMA,
                    'table': 'member',
                    'column': 'user',
                    'is_primary_key': True,
                },
            ],
        }
    ],
    'joins': [
        {
            'entity': 'member',
            'from': {
                'connection': SOURCE_CONNECTION,
                'schema': SCHEMA,
                'table': 'Order',
                'column': 'user',
            },
            'to': {
                'connection': TARGET_CONNECTION,
                'schema': SCHEMA,
                'table': 'member',
                'column': 'user',
            },
        }
    ],
}


@pytest_asyncio.fixture
async def federated(
    adapter: PostgresAdapter,
    integration_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[None]:
    """Два подключения и карта связей, собранные через настоящий реестр.

    `run_federated_lookup` ходит за адаптером через
    `tooling._adapter`, поэтому подменять реестр здесь нечем: путь от
    `connection_id` до пула и есть часть проверяемого.

    Зависимость от `adapter` — ради схемы: фикстура создаёт её и
    удаляет после теста.
    """
    map_file = tmp_path / 'relationships.json'
    map_file.write_text(json.dumps(_FEDERATION_MAP), encoding='utf-8')
    monkeypatch.setenv(RELATIONSHIPS_FILE_ENV, str(map_file))
    monkeypatch.setenv(
        CONNECTIONS_ENV, f'{SOURCE_CONNECTION}:SRC,{TARGET_CONNECTION}:TGT'
    )
    monkeypatch.setenv('SRC_PG_DSN', integration_dsn)
    monkeypatch.setenv('TGT_PG_DSN', integration_dsn)
    load_relationships.cache_clear()
    get_app_config.cache_clear()
    try:
        yield
    finally:
        await close_registry()


async def test_federated_lookup_joins_across_two_connections(
    federated: None,
) -> None:
    """Сквозной путь: id собираются в источнике, строки — в цели.

    Все три имени в statement'е требуют квотирования: `"Order"` —
    регистром, `"user"` — тем, что это зарезервированное слово.
    """
    result = await run_federated_lookup(
        entity='member',
        source_connection_id=SOURCE_CONNECTION,
        source_where='order_no <= 3',
        target_connection_id=TARGET_CONNECTION,
    )

    rows = result['result'].rows
    assert sorted(r[0] for r in rows) == [10, 20]
    assert result['source']['row_count'] == 3
    assert result['target']['row_count'] == 2
    assert result['notes'] == []


async def test_federated_lookup_binds_the_id_list_as_one_parameter(
    federated: None,
) -> None:
    """`= ANY($1)` получает список одним значением, а не склейкой.

    Это то же обещание, что у `query`: id приходят из чужой базы и в
    текст statement'а попадать не должны.
    """
    result = await run_federated_lookup(
        entity='member',
        source_connection_id=SOURCE_CONNECTION,
        source_where='order_no = 4',
        target_connection_id=TARGET_CONNECTION,
    )

    # В источнике есть заказ на пользователя 99, в цели такого нет.
    assert result['source']['row_count'] == 1
    assert result['result'].row_count == 0


async def test_federated_lookup_selects_only_the_asked_columns(
    federated: None,
) -> None:
    result = await run_federated_lookup(
        entity='member',
        source_connection_id=SOURCE_CONNECTION,
        source_where='order_no = 1',
        target_connection_id=TARGET_CONNECTION,
        target_columns='"desc"',
    )

    assert result['result'].columns == ['desc']
    assert result['result'].rows == [['ten']]


async def test_federated_lookup_skips_the_target_when_no_id_matched(
    federated: None,
) -> None:
    """Пустой набор id — не повод открывать второй пул."""
    result = await run_federated_lookup(
        entity='member',
        source_connection_id=SOURCE_CONNECTION,
        source_where='order_no > 100',
        target_connection_id=TARGET_CONNECTION,
    )

    assert result['result'] is None
    assert result['source']['row_count'] == 0
    assert 'row_count' not in result['target']


async def test_federated_lookup_warns_when_the_source_was_truncated(
    federated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Обрезанный источник делает джойн неполным, и это надо сказать.

    Без ноты частичный ответ неотличим от полного, а решение по нему
    принимает модель.
    """
    monkeypatch.setenv('SRC_PG_MAX_ROWS', '2')
    get_app_config.cache_clear()

    result = await run_federated_lookup(
        entity='member',
        source_connection_id=SOURCE_CONNECTION,
        source_where='order_no <= 3',
        target_connection_id=TARGET_CONNECTION,
    )

    assert result['source']['truncated'] is True
    assert result['notes'] == [
        'The source hit max_rows, so the join ran on an incomplete set '
        'of ids. Treat this result as partial.'
    ]


async def test_federated_lookup_refuses_a_pair_it_has_no_join_for(
    federated: None,
) -> None:
    with pytest.raises(ValueError, match='No known join'):
        await run_federated_lookup(
            entity='member',
            source_connection_id=TARGET_CONNECTION,
            source_where='true',
            target_connection_id=TARGET_CONNECTION,
        )
