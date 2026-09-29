"""PostgreSQL-адаптер (asyncpg).

Чтение всегда идёт в `BEGIN TRANSACTION READ ONLY` — движковая гарантия
поверх классификации statement, закрывающая пропуски парсера. Это
относится и к интроспекции каталога: её запросы константны и записать
ничего не могут, но граница не должна зависеть от того, каким
инструментом её пересекают.

Исключение одно и намеренное: `_fetch_under_policy` выбирает режим
транзакции по классу statement, потому что DML/DDL, разрешённый
политикой, обязан идти в пишущей.
"""

import json
import logging
import socket
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from pgsteward.adapters.base import StatementKind, elapsed_ms, now_ms
from pgsteward.config import (
    APPLICATION_NAME,
    ConnectionConfig,
    SecurityPolicy,
    make_dsn,
)
from pgsteward.errors import (
    AdapterConnectionError,
    ConnectionErrorCode,
    ExplainError,
    QueryTimeoutError,
    RelationNotFoundError,
    StatementNotAllowedError,
)
from pgsteward.health import CONNECTION_LOST, PgConnection, run_health_checks
from pgsteward.models import (
    ColumnInfo,
    ForeignKeyInfo,
    HealthCheckResult,
    IndexInfo,
    QueryPlan,
    QueryResult,
    SchemaMatch,
    SchemaSearchResult,
    ServerInfo,
    TableInfo,
    TableSchema,
)
from pgsteward.security import (
    assert_single_statement,
    check_statement,
    classify_statement,
    enforce_max_bytes,
    enforce_max_rows,
    resolve_security,
)
from pgsteward.serialization import count_non_finite, json_size

LOG = logging.getLogger(__name__)

_SYSTEM_SCHEMAS = ('pg_catalog', 'information_schema')
_DEFAULT_SCHEMA = 'public'
_SEARCH_SCHEMA_LIMIT = 200

# Сколько строк брать у курсора за раз. Лимит по размеру ответа не
# знает заранее, на какой строке сработает, а `row_limit` может быть
# снят вовсе, поэтому читать нужно порциями: иначе бюджет экономил бы
# контекст модели, но не память процесса.
_FETCH_CHUNK_ROWS = 100

# Служебные схемы (`pg_toast`, `pg_temp_1`, ...) отсекаются по префиксу.
# Подчёркивание экранировано намеренно: в LIKE `_` — wildcard на один
# символ, поэтому `'pg_%'` без экранирования скрывал бы и
# пользовательские схемы вида `pgbouncer`, `pgq`, `pgagent`.
_NOT_PG_PREFIXED = r"NOT LIKE 'pg\_%'"

# relkind'ы, для которых describe_table осмысленен.
#
# Интроспекция читает `pg_catalog`, а не `information_schema`:
# materialized view — расширение PostgreSQL вне стандарта SQL, и в
# `information_schema` его нет вообще. Выборка по нему даёт отношение
# без единой колонки — ровно тот пустой ответ, который `describe_table`
# обязан отличать от «таблицы не существует».
_DESCRIBABLE_RELKINDS = {
    'r': 'table',
    'p': 'partitioned table',
    'v': 'view',
    'm': 'materialized view',
    'f': 'foreign table',
}

# Готовый SQL-литерал для `relkind = ANY(...)`. Параметром его не
# передать: `"char"[]` asyncpg кодирует из Python-последовательности, а
# набор здесь — константа модуля, не пользовательский ввод.
_DESCRIBABLE_RELKINDS_SQL = (
    "'{" + ','.join(sorted(_DESCRIBABLE_RELKINDS)) + '}\'::"char"[]'
)
_RELKIND_NAMES = {
    'i': 'index',
    'I': 'partitioned index',
    'S': 'sequence',
    'c': 'composite type',
    't': 'TOAST table',
}


def _with_article(noun: str) -> str:
    """`an index`, `a sequence`.

    Правило по первой букве, а не по словарю: все значения
    `_RELKIND_NAMES` и `relkind=<char>` под него подходят.
    """
    return f'{"an" if noun[:1].lower() in "aeiou" else "a"} {noun}'


# Расхождение actual/plan rows больше этого множителя — признак
# устаревшей статистики планировщика.
_ROW_ESTIMATE_MISMATCH_RATIO = 10
# Ниже этого числа строк расхождение — статистический шум: точечный поиск
# по индексу планировщик оценивает в 1 строку, и любое отклонение
# тривиально пересекает 10-кратный ratio.
_ROW_ESTIMATE_MISMATCH_MIN_ROWS = 100

# Тип исключения драйвера -> код (почему только код — в `errors.py`).
_ERROR_CODES: tuple[tuple[type[BaseException], ConnectionErrorCode], ...] = (
    (
        asyncpg.InvalidAuthorizationSpecificationError,
        ConnectionErrorCode.AUTH_FAILED,
    ),
    (asyncpg.InvalidCatalogNameError, ConnectionErrorCode.DATABASE_NOT_FOUND),
    (ssl.SSLError, ConnectionErrorCode.TLS_FAILED),
    (TimeoutError, ConnectionErrorCode.TIMEOUT),
    (socket.gaierror, ConnectionErrorCode.UNREACHABLE),
    (ConnectionError, ConnectionErrorCode.UNREACHABLE),
    (OSError, ConnectionErrorCode.UNREACHABLE),
    (asyncpg.PostgresConnectionError, ConnectionErrorCode.UNREACHABLE),
    (asyncpg.TooManyConnectionsError, ConnectionErrorCode.UNREACHABLE),
    (asyncpg.CannotConnectNowError, ConnectionErrorCode.UNREACHABLE),
)


# Исчерпание отведённого времени. `TimeoutError` — это клиентский
# `command_timeout` asyncpg, но он выставлен заведомо позже серверного,
# поэтому на практике первым срабатывает `statement_timeout` и приходит
# `QueryCanceledError`.
_TIMEOUT_ERRORS = (
    TimeoutError,
    asyncpg.QueryCanceledError,
    asyncpg.IdleInTransactionSessionTimeoutError,
)


def classify_connection_error(exc: BaseException) -> ConnectionErrorCode:
    for exc_type, code in _ERROR_CODES:
        if isinstance(exc, exc_type):
            return code
    return ConnectionErrorCode.CONNECTION_FAILED


def _walk_plan_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [node]
    for child in node.get('Plans', []):
        nodes.extend(_walk_plan_nodes(child))
    return nodes


def _plan_warnings(plan: dict[str, Any], analyze: bool) -> list[str]:
    """Подсказки по плану, не диагноз.

    `Actual Rows` есть только при `EXPLAIN ANALYZE`.
    """
    warnings: list[str] = []
    for node in _walk_plan_nodes(plan):
        node_type = node.get('Node Type')
        if node_type == 'Seq Scan':
            relation = node.get('Relation Name', '?')
            cost = node.get('Total Cost')
            warnings.append(f'Seq Scan on table "{relation}" (cost={cost})')

        if analyze:
            plan_rows = node.get('Plan Rows')
            actual_rows = node.get('Actual Rows')
            if (
                plan_rows is not None
                and actual_rows is not None
                and max(plan_rows, actual_rows)
                >= _ROW_ESTIMATE_MISMATCH_MIN_ROWS
            ):
                if plan_rows == 0:
                    # Оценка 0 при значимом факте — худший вид недооценки,
                    # ratio здесь не посчитать.
                    mismatched = actual_rows > 0
                else:
                    ratio = actual_rows / plan_rows
                    mismatched = (
                        ratio >= _ROW_ESTIMATE_MISMATCH_RATIO
                        or ratio <= 1 / _ROW_ESTIMATE_MISMATCH_RATIO
                    )
                if mismatched:
                    warnings.append(
                        f'Row estimate is off on "{node_type}": '
                        f'planned={plan_rows}, actual={actual_rows}. '
                        'Statistics are probably stale; run ANALYZE on '
                        'the table.'
                    )
    return warnings


def _record_size(record: asyncpg.Record) -> int:
    """Размер строки в ответе — ровно как её увидит клиент.

    Меряется список значений, а не `Record`: наружу строка едет именно
    так, и обход тот же, что потом сериализует ответ.
    """
    return json_size(list(record.values()))


def _non_finite_count(rows: list[list[Any]]) -> int:
    """Сколько значений результата сериализатор подменит на `null`.

    JSON литералов `nan`/`±inf` не знает, и без ноты подмена
    неотличима от настоящего NULL, а `double precision` хранит и то и
    другое. Обход — общий с сериализатором, см. `serialization`.
    """
    return sum(count_non_finite(row) for row in rows)


def _query_notes(non_finite: int, byte_limited: bool) -> list[str]:
    notes: list[str] = []
    if non_finite:
        notes.append(
            f'{non_finite} value(s) in this result are NaN or Infinity and '
            'are reported as null: JSON has no literal for them. A null '
            'here is not necessarily a SQL NULL.'
        )
    if byte_limited:
        # Без этой ноты `truncated` читается как «строк было больше», и
        # агент повторяет запрос с меньшим limit — по размеру ответа
        # это не помогает, потому что режет его ширина строки, а не их
        # количество.
        notes.append(
            'This result was cut to fit max_bytes, not by row count: '
            'applied_limit was not reached. Ask for fewer columns rather '
            'than fewer rows.'
        )
    return notes


def _table_notes(
    indexes: list[IndexInfo],
    foreign_keys: list[ForeignKeyInfo],
    row_estimate: int | None,
    hidden_columns: int = 0,
) -> list[str]:
    """Пояснения, нужные только при чтении конкретного ответа.

    В описании tool'а они занимали бы контекст каждой сессии.
    """
    notes: list[str] = []
    if hidden_columns:
        # Без этой ноты колонка, закрытая грантом, неотличима от
        # несуществующей: её нет ни здесь, ни в search_schema. Агент
        # сообщает пользователю, что база таких данных не хранит, —
        # тихая ошибка вместо отказа. Имена не называются: количества
        # хватает, чтобы не писать `SELECT *` и не искать поле дальше.
        missing = (
            '1 column exists on this relation but is not listed: the '
            'connection role has no SELECT privilege on it'
            if hidden_columns == 1
            else f'{hidden_columns} columns exist on this relation but '
            'are not listed: the connection role has no SELECT '
            'privilege on them'
        )
        # Хвост не предлагает «перечислить колонки выше»: когда закрыты
        # все, списка выше нет, и такой совет провоцирует выдумать имена.
        notes.append(
            f'{missing}. SELECT * will fail here; only the columns '
            'listed above can be read.'
        )
    if any(i.included_columns for i in indexes):
        notes.append(
            'included_columns are non-key INCLUDE columns: an index-only '
            'scan can read them, but the index cannot search by them.'
        )
    names = [fk.constraint_name for fk in foreign_keys if fk.constraint_name]
    if len(names) != len(set(names)):
        notes.append(
            'A composite foreign key is listed as one entry per key '
            'position, paired in order and sharing a constraint_name.'
        )
    if row_estimate is not None:
        notes.append(
            'extra.estimated_row_count comes from pg_class.reltuples: a '
            'planner estimate as of the last ANALYZE, not an exact count.'
        )
    return notes


class PostgresAdapter:
    def __init__(
        self,
        conn: ConnectionConfig,
        policy: SecurityPolicy | None = None,
    ) -> None:
        self._conn = conn
        self._pool: asyncpg.Pool | None = None
        self._security = resolve_security(conn, policy or SecurityPolicy())
        # Тот же запас, что и у `command_timeout`: ждать слот дольше,
        # чем позволено выполняться запросу, смысла нет.
        self._acquire_timeout = float(conn.query_timeout + 5)

    # --- lifecycle -----------------------------------------------------

    def _server_settings(self) -> dict[str, str]:
        r"""Ограничения, которые обеспечивает сам сервер.

        `command_timeout` asyncpg лишь посылает cancel request; если тот
        не доходит, запрос продолжает выполняться. Лимит держит
        `statement_timeout`, а `default_transaction_read_only` — третий
        барьер после классификации и явной READ ONLY транзакции.

        `standard_conforming_strings` — не барьер, а условие
        корректности первого: маскировка литералов в `security.py`
        исходит из того, что backslash экранирует только в `E'...'`.
        При `off` он экранирует и в обычных строках, и `'a\''`
        разбирается иначе, чем ожидает маскировщик. Значение `on` —
        дефолт с PostgreSQL 9.1; пин снимает зависимость от настроек
        сервера, базы и роли.
        """
        settings = {
            'application_name': APPLICATION_NAME,
            'statement_timeout': str(self._conn.query_timeout * 1000),
            'idle_in_transaction_session_timeout': str(
                self._conn.query_timeout * 1000
            ),
            'standard_conforming_strings': 'on',
        }
        if self._conn.read_only:
            settings['default_transaction_read_only'] = 'on'
        return settings

    def _connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            'min_size': self._conn.pool.min,
            'max_size': self._conn.pool.max,
            # Сервер должен успеть прервать запрос сам.
            'command_timeout': self._conn.query_timeout + 5,
            'server_settings': self._server_settings(),
        }
        if self._conn.dsn is not None:
            kwargs['dsn'] = self._conn.dsn.get_secret_value()
        else:
            # Host, user и database обязательны и проверены при чтении
            # конфигурации: подставлять на их место localhost/postgres
            # значило бы превращать опечатку в префиксе в тихое
            # подключение к чужой локальной базе.
            kwargs['dsn'] = make_dsn(
                dbname=self._conn.database or '',
                host=self._conn.host or '',
                port=self._conn.port or 5432,
                user=self._conn.user or '',
                password=(
                    self._conn.password.get_secret_value()
                    if self._conn.password is not None
                    else None
                ),
            )

        if self._conn.sslmode:
            kwargs['ssl'] = self._conn.sslmode
        return kwargs

    def _connection_error(self, exc: BaseException) -> AdapterConnectionError:
        """Перевести ошибку драйвера в код, оставив подробности в логе."""
        code = classify_connection_error(exc)
        LOG.warning(
            'connection "%s" failed with %s: %s',
            self._conn.id,
            code.value,
            exc,
            exc_info=True,
        )
        return AdapterConnectionError(self._conn.id, code)

    async def connect(self) -> None:
        if self._pool is not None:
            return
        try:
            self._pool = await asyncpg.create_pool(**self._connect_kwargs())
        except Exception as exc:
            raise self._connection_error(exc) from None

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise AdapterConnectionError(
                self._conn.id, ConnectionErrorCode.CONNECTION_FAILED
            )
        return self._pool

    @asynccontextmanager
    async def _acquire(self) -> AsyncIterator[PgConnection]:
        """Взять соединение, переведя ошибки подключения в код.

        Оборачивается только захват: пул может открыть новое физическое
        соединение и получить сообщение с host/портом/ролью. Ошибки
        самого запроса проходят как есть — они описывают схему.

        Ожидание слота ограничено тем же сроком, что и выполнение:
        `statement_timeout` считается с начала запроса и на очередь к
        пулу не распространяется, а `acquire()` без таймаута ждёт
        бесконечно. `TimeoutError` отсюда становится кодом `timeout`.
        """
        pool = self._require_pool()
        try:
            conn = await pool.acquire(timeout=self._acquire_timeout)
        except Exception as exc:
            raise self._connection_error(exc) from None
        try:
            yield conn
        finally:
            await pool.release(conn)

    @asynccontextmanager
    async def _read_tx(self) -> AsyncIterator[PgConnection]:
        """Соединение внутри `BEGIN TRANSACTION READ ONLY`.

        Через него ходит всё чтение, включая интроспекцию каталога (см.
        docstring модуля). Побочный, но нужный эффект: подзапросы
        `describe_table` читают один снапшот каталога, а не пять разных.
        """
        async with (
            self._acquire() as conn,
            conn.transaction(readonly=True),
        ):
            yield conn

    # --- introspection -------------------------------------------------

    async def ping(self) -> bool:
        async with self._read_tx() as conn:
            value = await conn.fetchval('SELECT 1')
        return bool(value == 1)

    async def server_info(self) -> ServerInfo:
        async with self._read_tx() as conn:
            version = await conn.fetchval('SELECT version()')
            database = await conn.fetchval('SELECT current_database()')
        return ServerInfo(
            version=str(version),
            extra={'database': database},
        )

    async def list_databases(self) -> list[str]:
        async with self._read_tx() as conn:
            rows = await conn.fetch(
                'SELECT datname FROM pg_database '
                'WHERE NOT datistemplate ORDER BY datname'
            )
        return [r['datname'] for r in rows]

    async def list_schemas(self) -> list[str]:
        async with self._read_tx() as conn:
            rows = await conn.fetch(
                'SELECT schema_name FROM information_schema.schemata '
                'WHERE schema_name NOT IN ($1, $2) '
                f'AND schema_name {_NOT_PG_PREFIXED} '
                'ORDER BY schema_name',
                *_SYSTEM_SCHEMAS,
            )
        return [r['schema_name'] for r in rows]

    async def list_tables(self, schema: str | None = None) -> list[TableInfo]:
        """Отношения схемы, которые агент может прочитать.

        Фильтр по правам восстанавливает то, что `information_schema`
        давал даром: `pg_class` привилегий не учитывает, и без проверки
        в списке оказались бы недоступные роли таблицы.

        Предикат — `has_any_column_privilege`, а не
        `has_table_privilege`: второй не видит `GRANT SELECT (col)` и
        скрыл бы таблицу, из которой роль может читать часть колонок.
        Права на таблицу он покрывает тоже — они неявно
        распространяются на каждую колонку.
        """
        target = schema or _DEFAULT_SCHEMA
        async with self._read_tx() as conn:
            rows = await conn.fetch(
                # `relkind` имеет тип "char", который asyncpg отдаёт как
                # bytes; текстовый каст оставляет сравнение со строками.
                'SELECT c.relname AS name, c.relkind::text AS relkind '
                'FROM pg_class c '
                'JOIN pg_namespace n ON n.oid = c.relnamespace '
                'WHERE n.nspname = $1 '
                f'  AND c.relkind = ANY({_DESCRIBABLE_RELKINDS_SQL}) '
                "  AND has_any_column_privilege(c.oid, 'SELECT') "
                'ORDER BY c.relname',
                target,
            )
        return [
            TableInfo(
                name=r['name'],
                schema=target,
                kind=_DESCRIBABLE_RELKINDS[r['relkind']],
            )
            for r in rows
        ]

    async def describe_table(
        self, table: str, schema: str | None = None
    ) -> TableSchema:
        target = schema or _DEFAULT_SCHEMA
        async with self._read_tx() as conn:
            relation = await self._fetch_relation(conn, table, target)
            oid = relation['oid']
            columns, hidden_columns = await self._fetch_columns(conn, oid)
            pk = await self._fetch_primary_key(conn, oid)
            fks = await self._fetch_foreign_keys(conn, table, target)
            indexes = await self._fetch_indexes(conn, table, target)

        row_estimate = self._row_estimate(relation['reltuples'])
        pk_set = set(pk)
        columns = [
            c.model_copy(update={'is_primary_key': c.name in pk_set})
            for c in columns
        ]
        return TableSchema(
            name=table,
            schema=target,
            columns=columns,
            primary_key=pk,
            foreign_keys=fks,
            indexes=indexes,
            extra={
                'estimated_row_count': row_estimate,
                'relation_kind': _DESCRIBABLE_RELKINDS[relation['relkind']],
            },
            notes=_table_notes(indexes, fks, row_estimate, hidden_columns),
        )

    async def _fetch_relation(
        self, conn: PgConnection, table: str, schema: str
    ) -> asyncpg.Record:
        """oid/relkind/reltuples отношения; отсутствие — ошибка.

        Квотирование через `format('%I.%I', ...)` обязательно:
        `to_regclass('app.MyTable')` приводит незакавыченное имя к
        нижнему регистру и возвращает NULL.
        """
        row = await conn.fetchrow(
            'SELECT c.oid, c.relkind::text AS relkind, c.reltuples '
            'FROM pg_class c '
            # Касты обязательны: `format` вариадична, и без них
            # PostgreSQL не может вывести типы параметров.
            "WHERE c.oid = to_regclass(format('%I.%I', $1::text, $2::text))",
            schema,
            table,
        )
        if row is None:
            raise RelationNotFoundError(
                f'Relation "{schema}.{table}" does not exist. '
                'Use search_schema to find it by substring, or list_tables '
                'to see what the schema contains.'
            )
        relkind = row['relkind']
        if relkind not in _DESCRIBABLE_RELKINDS:
            kind = _RELKIND_NAMES.get(relkind, f'relkind={relkind}')
            raise RelationNotFoundError(
                f'"{schema}.{table}" is {_with_article(kind)}, not a table '
                'or view, so it has no columns to describe.'
            )
        return row

    @staticmethod
    def _row_estimate(reltuples: float | None) -> int | None:
        """Оценка на момент последнего ANALYZE; минус — не анализировалась."""
        if reltuples is None or reltuples < 0:
            return None
        return int(reltuples)

    async def _fetch_columns(
        self, conn: PgConnection, oid: int
    ) -> tuple[list[ColumnInfo], int]:
        """Колонки отношения по oid и число скрытых грантами.

        `format_type` даёт тип вместе с длиной и точностью
        (`character varying(255)`, `numeric(12,2)`), тогда как
        `information_schema.columns` отдаёт голое имя типа и прячет
        размер в отдельные колонки.

        Отбрасываются системные (`attnum <= 0`) и удалённые колонки:
        `DROP COLUMN` оставляет запись в `pg_attribute` до перезаписи
        таблицы. `has_column_privilege` сохраняет видимость на уровне
        `information_schema`, который показывал только колонки с
        грантом.

        Недоступные колонки отсеиваются здесь, а не в `WHERE`, чтобы
        вернуть их количество: молча исчезнувшая колонка неотличима
        для агента от несуществующей, и он делает вывод, что данных в
        базе нет.

        `CASE` не даёт вычислять и гнать по сети выражение `DEFAULT`
        недоступной колонки: оно может нести значение.
        """
        rows = await conn.fetch(
            'SELECT '
            '  a.attname AS column_name, '
            '  format_type(a.atttypid, a.atttypmod) AS data_type, '
            '  NOT a.attnotnull AS nullable, '
            '  CASE WHEN '
            "    has_column_privilege(a.attrelid, a.attnum, 'SELECT') "
            '    THEN pg_get_expr(d.adbin, d.adrelid) '
            '  END AS column_default, '
            "  has_column_privilege(a.attrelid, a.attnum, 'SELECT') "
            '    AS readable '
            'FROM pg_attribute a '
            'LEFT JOIN pg_attrdef d '
            '  ON d.adrelid = a.attrelid AND d.adnum = a.attnum '
            'WHERE a.attrelid = $1::oid '
            '  AND a.attnum > 0 AND NOT a.attisdropped '
            'ORDER BY a.attnum',
            oid,
        )
        readable = [r for r in rows if r['readable']]
        columns = [
            ColumnInfo(
                name=r['column_name'],
                data_type=r['data_type'],
                nullable=r['nullable'],
                default=r['column_default'],
            )
            for r in readable
        ]
        return columns, len(rows) - len(readable)

    async def _fetch_primary_key(
        self, conn: PgConnection, oid: int
    ) -> list[str]:
        """Колонки PK в порядке объявления ключа.

        Порядок задаёт `indkey`, а не `attnum`: по `attnum`
        `PRIMARY KEY (tenant_id, id)` вернулся бы как `[id, tenant_id]`.
        INCLUDE-колонки тоже лежат в `indkey` и отсекаются по
        `indnkeyatts`.
        """
        rows = await conn.fetch(
            'SELECT a.attname AS column_name '
            'FROM pg_index i '
            'CROSS JOIN LATERAL unnest(i.indkey::int2[]) '
            '  WITH ORDINALITY AS k(attnum, ord) '
            'JOIN pg_attribute a '
            '  ON a.attrelid = i.indrelid AND a.attnum = k.attnum '
            'WHERE i.indrelid = $1::oid AND i.indisprimary '
            '  AND k.ord <= i.indnkeyatts '
            'ORDER BY k.ord',
            oid,
        )
        return [r['column_name'] for r in rows]

    async def _fetch_foreign_keys(
        self, conn: PgConnection, table: str, schema: str
    ) -> list[ForeignKeyInfo]:
        """Колонки FK, сопоставленные попарно по позиции ключа.

        `conkey` и `confkey` связаны только порядком: два независимых
        `= ANY(...)` дали бы декартово произведение, поэтому
        `unnest(conkey, confkey)` разворачивает их синхронно. Схема цели
        возвращается отдельно — без неё FK в другую схему неотличим от
        внутреннего.
        """
        rows = await conn.fetch(
            'SELECT '
            '  con.conname AS constraint_name, '
            '  att.attname AS column_name, '
            '  rns.nspname AS ref_schema, '
            '  cl.relname AS ref_table, '
            '  ratt.attname AS ref_column '
            'FROM pg_constraint con '
            'JOIN pg_class c ON c.oid = con.conrelid '
            'JOIN pg_namespace ns ON ns.oid = c.relnamespace '
            'CROSS JOIN LATERAL unnest(con.conkey, con.confkey) '
            '  WITH ORDINALITY AS k(attnum, rattnum, ord) '
            'JOIN pg_attribute att '
            '  ON att.attrelid = con.conrelid AND att.attnum = k.attnum '
            'JOIN pg_class cl ON cl.oid = con.confrelid '
            'JOIN pg_namespace rns ON rns.oid = cl.relnamespace '
            'JOIN pg_attribute ratt '
            '  ON ratt.attrelid = con.confrelid '
            '  AND ratt.attnum = k.rattnum '
            "WHERE con.contype = 'f' "
            '  AND ns.nspname = $1 AND c.relname = $2 '
            'ORDER BY con.conname, k.ord',
            schema,
            table,
        )
        return [
            ForeignKeyInfo(
                column=r['column_name'],
                references_table=r['ref_table'],
                references_column=r['ref_column'],
                references_schema=r['ref_schema'],
                constraint_name=r['constraint_name'],
            )
            for r in rows
        ]

    async def _fetch_indexes(
        self, conn: PgConnection, table: str, schema: str
    ) -> list[IndexInfo]:
        """Индексы таблицы: ключевые колонки, выражения и INCLUDE.

        Позиции выражений записаны в `indkey` нулями, строк в
        `pg_attribute` для них нет, поэтому join левый, а текст берётся
        из `pg_get_indexdef`. `attname` предпочитается ему там, где
        есть: `pg_get_indexdef` закавычивает имена вроде `"Order Id"`.
        """
        rows = await conn.fetch(
            'SELECT '
            '  i.relname AS index_name, '
            '  ix.indisunique AS is_unique, '
            '  coalesce('
            '    a.attname, '
            '    pg_get_indexdef(ix.indexrelid, k.ord::int, true)'
            '  ) AS column_name, '
            '  (k.ord <= ix.indnkeyatts) AS is_key '
            'FROM pg_index ix '
            'JOIN pg_class i ON i.oid = ix.indexrelid '
            'JOIN pg_class t ON t.oid = ix.indrelid '
            'JOIN pg_namespace ns ON ns.oid = t.relnamespace '
            'CROSS JOIN LATERAL unnest(ix.indkey::int2[]) '
            '  WITH ORDINALITY AS k(attnum, ord) '
            'LEFT JOIN pg_attribute a '
            '  ON a.attrelid = ix.indrelid AND a.attnum = k.attnum '
            'WHERE ns.nspname = $1 AND t.relname = $2 '
            'ORDER BY i.relname, k.ord',
            schema,
            table,
        )
        grouped: dict[str, dict[str, Any]] = {}
        for r in rows:
            entry = grouped.setdefault(
                r['index_name'],
                {
                    'is_unique': r['is_unique'],
                    'columns': [],
                    'included_columns': [],
                },
            )
            bucket = 'columns' if r['is_key'] else 'included_columns'
            entry[bucket].append(r['column_name'])
        return [
            IndexInfo(
                name=name,
                columns=data['columns'],
                is_unique=data['is_unique'],
                included_columns=data['included_columns'],
            )
            for name, data in grouped.items()
        ]

    async def search_schema(
        self, pattern: str, schema: str | None = None
    ) -> SchemaSearchResult:
        """Таблицы/колонки, чьё имя содержит `pattern` (ILIKE).

        Без `schema` ищет по всем не-системным схемам. `_` и `%` в
        шаблоне — служебные символы ILIKE и не экранируются. Тянется на
        строку больше лимита, чтобы отличить полную выдачу от
        обрезанной.

        Источник — `pg_catalog`, как и в `list_tables`/`describe_table`:
        `information_schema` не показывает materialized view. Права
        проверяются явно, потому что `pg_class` и `pg_attribute` их не
        учитывают.
        """
        like_pattern = f'%{pattern}%'
        async with self._read_tx() as conn:
            rows = await conn.fetch(
                f"""
                SELECT n.nspname AS "schema", c.relname AS "table",
                       NULL::text AS "column", 'table' AS "kind"
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = ANY({_DESCRIBABLE_RELKINDS_SQL})
                  AND n.nspname NOT IN ($2, $3)
                  AND n.nspname {_NOT_PG_PREFIXED}
                  AND ($4::text IS NULL OR n.nspname = $4)
                  AND c.relname ILIKE $1
                  AND has_any_column_privilege(c.oid, 'SELECT')
                UNION ALL
                SELECT n.nspname, c.relname, a.attname, 'column'
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = ANY({_DESCRIBABLE_RELKINDS_SQL})
                  AND a.attnum > 0 AND NOT a.attisdropped
                  AND n.nspname NOT IN ($2, $3)
                  AND n.nspname {_NOT_PG_PREFIXED}
                  AND ($4::text IS NULL OR n.nspname = $4)
                  AND a.attname ILIKE $1
                  AND has_column_privilege(a.attrelid, a.attnum, 'SELECT')
                ORDER BY "schema", "table", "kind", "column"
                LIMIT $5
                """,
                like_pattern,
                *_SYSTEM_SCHEMAS,
                schema,
                _SEARCH_SCHEMA_LIMIT + 1,
            )
        truncated = len(rows) > _SEARCH_SCHEMA_LIMIT
        matches = [
            SchemaMatch(
                schema=r['schema'],
                table=r['table'],
                kind=r['kind'],
                column=r['column'],
            )
            for r in rows[:_SEARCH_SCHEMA_LIMIT]
        ]
        notes = (
            [
                f'Only the first {_SEARCH_SCHEMA_LIMIT} matches are shown. '
                'Narrow the pattern or pass schema to see the rest.'
            ]
            if truncated
            else []
        )
        return SchemaSearchResult(
            matches=matches,
            match_count=len(matches),
            truncated=truncated,
            limit=_SEARCH_SCHEMA_LIMIT,
            notes=notes,
        )

    # --- execution ------------------------------------------------------

    async def execute(
        self,
        statement: str,
        params: list[Any] | None = None,
        limit: int | None = None,
    ) -> QueryResult:
        assert_single_statement(statement)
        kind = classify_statement(statement)
        check_statement(kind, self._security)

        row_limit = self._effective_limit(limit)
        byte_limit = self._security.max_bytes
        args = params or []

        started = now_ms()
        try:
            records, truncated, byte_limited = await self._fetch_under_policy(
                statement, args, kind, row_limit, byte_limit
            )
        except _TIMEOUT_ERRORS as exc:
            raise QueryTimeoutError(
                'The statement exceeded the connection query_timeout.'
            ) from exc
        elapsed = elapsed_ms(started)

        rows = [list(record.values()) for record in records]
        return QueryResult(
            columns=list(records[0].keys()) if records else [],
            rows=rows,
            row_count=len(rows),
            kind=kind.value,
            truncated=truncated,
            # Отрицательный `row_limit` — соглашение конфигурации, а не
            # число строк; наружу «без предела» едет как `null`.
            applied_limit=row_limit if row_limit >= 0 else None,
            applied_byte_limit=byte_limit if byte_limit >= 0 else None,
            execution_ms=elapsed,
            notes=_query_notes(_non_finite_count(rows), byte_limited),
        )

    def _effective_limit(self, limit: int | None) -> int:
        """Эффективный лимит строк; отрицательное значение = без лимита."""
        max_rows = self._security.max_rows
        if max_rows < 0:
            return limit if limit is not None and limit >= 0 else max_rows
        if limit is None or limit < 0:
            return max_rows
        return min(limit, max_rows)

    async def _fetch_under_policy(
        self,
        statement: str,
        args: list[Any],
        kind: StatementKind,
        row_limit: int,
        byte_limit: int,
    ) -> tuple[list[asyncpg.Record], bool, bool]:
        """Выполнить statement в режиме, разрешённом политикой.

        Возвращает (строки, обрезано, обрезано по размеру).

        `_read_tx` здесь не подходит: режим транзакции выбирается по
        классу statement, а не фиксирован. Почему чтение всё равно
        идёт в READ ONLY — в docstring модуля.
        """
        async with self._acquire() as conn:
            if kind is StatementKind.READ:
                async with conn.transaction(readonly=True):
                    return await self._fetch_page(
                        conn, statement, args, row_limit, byte_limit
                    )
            # Путь записи читает результат целиком: курсор для DML без
            # RETURNING невозможен. Запись выключена по умолчанию.
            async with conn.transaction():
                records = await conn.fetch(statement, *args)
        records, truncated = enforce_max_rows(records, row_limit)
        if truncated:
            return records, True, False
        records, byte_limited = enforce_max_bytes(
            records, byte_limit, _record_size
        )
        return records, byte_limited, byte_limited

    @staticmethod
    async def _fetch_page(
        conn: PgConnection,
        statement: str,
        args: list[Any],
        row_limit: int,
        byte_limit: int,
    ) -> tuple[list[asyncpg.Record], bool, bool]:
        """Читать курсором, пока не упрёшься в один из двух лимитов.

        Курсор вычитывается чанками, а не одним `fetch(row_limit + 1)`:
        иначе тысяча широких строк материализуется в процессе целиком
        ровно затем, чтобы бюджет выбросил девятьсот из них. Лишняя
        строка сверх `row_limit` по-прежнему запрашивается — она и
        отвечает на вопрос, был ли результат обрезан.

        Первая строка остаётся всегда, даже если одна не влезает в
        бюджет: см. `security.enforce_max_bytes`.

        Без обоих лимитов курсор не нужен — читается всё разом.
        """
        if row_limit < 0 and byte_limit < 0:
            return await conn.fetch(statement, *args), False, False

        cursor = await conn.cursor(statement, *args)
        records: list[asyncpg.Record] = []
        size = 0
        while True:
            wanted = (
                _FETCH_CHUNK_ROWS
                if row_limit < 0
                else min(_FETCH_CHUNK_ROWS, row_limit + 1 - len(records))
            )
            batch = await cursor.fetch(wanted)
            for record in batch:
                if row_limit >= 0 and len(records) == row_limit:
                    return records, True, False
                row_size = _record_size(record)
                if (
                    byte_limit >= 0
                    and records
                    and size + row_size > byte_limit
                ):
                    return records, True, True
                records.append(record)
                size += row_size
            if len(batch) < wanted:
                # Курсор исчерпан: отдано всё, что было.
                return records, False, False

    # --- explain & health ----------------------------------------------

    async def explain(
        self,
        statement: str,
        params: list[Any] | None = None,
        analyze: bool = False,
    ) -> QueryPlan:
        """`EXPLAIN (FORMAT JSON[, ANALYZE, BUFFERS])` через `execute()`.

        `ANALYZE` исполняет statement, а обёртка классифицируется как
        READ и уходит в READ ONLY транзакцию — то есть записать не
        выйдет ни при какой политике. Не-READ отклоняется здесь, чтобы
        вместо `cannot execute UPDATE in a read-only transaction` от
        драйвера агент получил причину отказа.

        Барьером это не является: тот же текст, отправленный в `query`
        напрямую, по-прежнему отклоняет только БД.

        Без `ANALYZE` statement не исполняется, поэтому план строится
        для любого класса. `Planning Time`/`Execution Time` PostgreSQL
        выводит только под `ANALYZE`.
        """
        if analyze and classify_statement(statement) is not StatementKind.READ:
            raise StatementNotAllowedError(
                'EXPLAIN ANALYZE executes the statement, and it always '
                'runs in a READ ONLY transaction, so only read statements '
                'are supported. Drop analyze to get the plan without '
                'executing.'
            )
        options = 'FORMAT JSON' + (', ANALYZE, BUFFERS' if analyze else '')
        wrapped = f'EXPLAIN ({options}) {statement}'
        result = await self.execute(wrapped, params=params, limit=1)
        if not result.rows:
            raise ExplainError('EXPLAIN returned no plan.')

        raw = result.rows[0][0]
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        top = parsed[0] if isinstance(parsed, list) else parsed
        plan = top.get('Plan', {})

        return QueryPlan(
            plan=plan,
            planning_time_ms=top.get('Planning Time'),
            execution_time_ms=top.get('Execution Time'),
            warnings=_plan_warnings(plan, analyze),
            execution_ms=result.execution_ms,
        )

    async def analyze_health(self) -> list[HealthCheckResult]:
        """Набор проверок и пороги — в `pgsteward/health.py`.

        Отдельная проверка, упавшая на запросе, изолируется там же и
        приходит как `skipped`. Наверх доходит только потеря самого
        соединения — её нужно назвать кодом, а не отчётом из
        одиннадцати отказов, и текст драйвера при этом остаётся в логе.
        """
        try:
            async with self._read_tx() as conn:
                return await run_health_checks(conn)
        except CONNECTION_LOST as exc:
            raise self._connection_error(exc) from None
