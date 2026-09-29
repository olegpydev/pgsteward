"""Health-checks PostgreSQL: только системный каталог, без extensions.

Общий принцип: проверка никогда не отвечает `ok`, не увидев данных.
Если роли не хватает прав или окно статистики слишком короткое, ответ —
`skipped` с указанием, чего не хватает. Пустая выборка под урезанной
видимостью здоровьем не является.

`idle_in_transaction`/`long_running_queries` показывают чужие сессии:
текст запроса (`pg_stat_activity.query`) в `details` не попадает,
только `pid`, `usename`, `application_name` и длительность.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeAlias

import asyncpg
import asyncpg.pool

from pgsteward.models import HealthCheckResult, Status

LOG = logging.getLogger(__name__)

# Соединение asyncpg, уже захваченное вызывающим: транзакцию открывает
# адаптер, потому что READ ONLY для всего набора проверок — его решение,
# а не их.
#
# Объединение, а не `Connection`: пул отдаёт `PoolConnectionProxy`, и он
# самостоятельный тип, а не подкласс. Для запросов они взаимозаменяемы.
PgConnection: TypeAlias = (
    'asyncpg.Connection[asyncpg.Record] '
    '| asyncpg.pool.PoolConnectionProxy[asyncpg.Record]'
)

# --- thresholds --------------------------------------------------------

_CONN_UTIL_WARN = 0.75
_CONN_UTIL_CRITICAL = 0.90

_IDLE_IN_TX_WARN_MINUTES = 5
_IDLE_IN_TX_CRITICAL_MINUTES = 30

_LONG_QUERY_WARN_MINUTES = 5

_CACHE_HIT_WARN = 0.99
_CACHE_HIT_CRITICAL = 0.90

_UNUSED_INDEX_MIN_SIZE_BYTES = 5 * 1024 * 1024

# Кумулятивные счётчики (idx_scan, heap_blks_*) считаются с момента
# сброса статистики. Сразу после сброса любой индекс выглядит
# неиспользованным, поэтому окно короче этого — не данные, а шум.
_STATS_MIN_WINDOW_DAYS = 7.0

_WRAPAROUND_DEFAULT_FREEZE_MAX_AGE = 200_000_000
# Абсолютные пороги, НЕ доля autovacuum_freeze_max_age: возраст 100-200M —
# штатный цикл (при freeze_max_age=200M Postgres сам запускает
# anti-wraparound vacuum), и относительный порог срабатывал бы на здоровой
# БД. warn — заметно выше штатного цикла, critical — выше
# vacuum_failsafe_age (1.6B в PG14+) и близко к hard limit ~2.1B.
_WRAPAROUND_WARN_AGE = 1_000_000_000
_WRAPAROUND_CRITICAL_AGE = 1_800_000_000
_WRAPAROUND_RELATION_LIMIT = 10

_SEQUENCE_WARN_PCT = 75.0
_SEQUENCE_CRITICAL_PCT = 90.0

_REPLICATION_LAG_WARN_BYTES = 16 * 1024 * 1024

# Потолки на размер ответа. Проверка тянет на строку больше и сообщает
# `truncated`: без флага нижняя граница читается как итог.
_SESSION_LIMIT = 20
_OBJECT_LIMIT = 50


def _classify(
    value: float, warn: float, critical: float, *, higher_is_worse: bool = True
) -> Status:
    if higher_is_worse:
        if value >= critical:
            return Status.CRITICAL
        if value >= warn:
            return Status.WARNING
        return Status.OK
    if value <= critical:
        return Status.CRITICAL
    if value <= warn:
        return Status.WARNING
    return Status.OK


def _rows_status(rows: list[Any]) -> Status:
    return Status.WARNING if rows else Status.OK


def _take(
    rows: list[asyncpg.Record], limit: int
) -> tuple[list[asyncpg.Record], bool]:
    """Обрезать до лимита; вернуть (rows, truncated).

    Запросы тянут на строку больше лимита — иначе полная выдача ровно в
    лимит неотличима от обрезанной, и число в сообщении читается как
    итог, хотя является потолком.
    """
    return rows[:limit], len(rows) > limit


def _counted(count: int, truncated: bool) -> str:
    """Начало сообщения: точное число или нижняя граница."""
    return f'At least {count}' if truncated else str(count)


def _require_row(row: asyncpg.Record | None, check_id: str) -> asyncpg.Record:
    """Строка агрегата, которая обязана существовать.

    Запросы, идущие через эту функцию, — агрегаты без `GROUP BY`:
    PostgreSQL возвращает по одной строке даже над пустой таблицей.
    Проверка записывает это допущение явно.
    """
    if row is None:
        raise RuntimeError(
            f'Health check "{check_id}" got no row from an aggregate query.'
        )
    return row


_STATS_HIDDEN_MESSAGE = (
    '{hidden} session(s) are invisible to this role, so this check '
    'cannot be answered: PostgreSQL hides state, xact_start and '
    'query_start of other users. Grant pg_monitor (or pg_read_all_stats) '
    'to the connection role.'
)
_STATS_PARTIAL_NOTE = (
    ' Partial result: {hidden} session(s) are invisible to this role; '
    'grant pg_monitor to see all of them.'
)
_REPLICATION_HIDDEN_MESSAGE = (
    '{total} replica(s) connected, but pg_stat_replication reports no '
    'details to this role. Grant pg_monitor (or pg_read_all_stats) to '
    'the connection role.'
)
_STATS_WINDOW_MESSAGE = (
    'Cumulative statistics were reset {age:.1f} day(s) ago, which is too '
    'short to tell an unused object from an idle one. This check needs at '
    'least {minimum:.0f} days of accumulation.'
)

# Строка `pg_stat_activity`, скрытая от текущей роли: у видимой строки
# `backend_type` заполнен всегда, у скрытой обнуляется вместе со `state`,
# `xact_start` и `query_start`. `datname IS NOT NULL` отсекает фоновые
# процессы кластера — они скрыты от непривилегированной роли всегда и
# сессиями не являются.
_HIDDEN_BACKENDS_FILTER = 'backend_type IS NULL AND datname IS NOT NULL'

# Бэкенды, занимающие слот `max_connections`. Фоновые процессы
# (checkpointer, walwriter, launcher'ы) присутствуют в pg_stat_activity,
# но слотов не занимают и `datname` у них пуст. У скрытой от роли сессии
# `backend_type` обнулён, поэтому NULL тоже засчитывается.
_SLOT_BACKENDS_FILTER = (
    'datname IS NOT NULL '
    "AND (backend_type = 'client backend' OR backend_type IS NULL)"
)


async def _hidden_backend_count(conn: PgConnection) -> int:
    """Число сессий, чей `state` скрыт от текущей роли.

    Факт времени выполнения, а не предположение о роли: ноль означает,
    что выборка полная, даже если `pg_monitor` не выдан (например, все
    сессии принадлежат той же роли).
    """
    value = await conn.fetchval(
        'SELECT count(*) FROM pg_stat_activity '
        f'WHERE {_HIDDEN_BACKENDS_FILTER}'
    )
    return int(value or 0)


async def _stats_window_age_days(
    conn: PgConnection,
) -> tuple[Any, float | None]:
    """(`stats_reset`, возраст окна в днях).

    `stats_reset IS NULL` означает «статистику ни разу не сбрасывали»,
    то есть окно максимально длинное; возраст в этом случае — `None`, и
    ограничение не применяется.
    """
    row = await conn.fetchrow(
        'SELECT stats_reset, '
        'extract(epoch FROM now() - stats_reset) / 86400.0 AS stats_age_days '
        'FROM pg_stat_database WHERE datname = current_database()'
    )
    if row is None:
        return None, None
    age = row.get('stats_age_days')
    return row.get('stats_reset'), (None if age is None else float(age))


def _stats_window_too_short(age_days: float | None) -> bool:
    return age_days is not None and age_days < _STATS_MIN_WINDOW_DAYS


@dataclass(frozen=True)
class _Facts:
    """Наблюдения, общие для нескольких проверок.

    Снимаются один раз на отчёт. Дело не только в лишних round-trip'ах:
    пока каждая проверка спрашивала сама, две соседние могли увидеть
    разное число скрытых сессий и разойтись в выводах внутри одного
    ответа.
    """

    hidden_backends: int
    stats_reset: Any
    stats_age_days: float | None

    @property
    def stats_window_too_short(self) -> bool:
        return _stats_window_too_short(self.stats_age_days)

    @classmethod
    async def collect(cls, conn: PgConnection) -> '_Facts':
        stats_reset, age_days = await _stats_window_age_days(conn)
        return cls(
            hidden_backends=await _hidden_backend_count(conn),
            stats_reset=stats_reset,
            stats_age_days=age_days,
        )

    def stats_window_result(self, check_id: str) -> HealthCheckResult:
        """Отказ проверки, которой нечего сказать на коротком окне."""
        return HealthCheckResult(
            id=check_id,
            status=Status.SKIPPED,
            message=_STATS_WINDOW_MESSAGE.format(
                age=self.stats_age_days, minimum=_STATS_MIN_WINDOW_DAYS
            ),
            details={
                'stats_reset': self.stats_reset,
                'stats_age_days': self.stats_age_days,
            },
        )


def overall_status(checks: list[HealthCheckResult]) -> Status:
    """Худший статус среди проверок; `skipped` в расчёт не входит.

    Если содержательных проверок не осталось вовсе, ответ — `unknown`, а
    не `ok`: под ролью без грантов все одиннадцать уходят в `skipped`, и
    сводка `ok` была бы тем самым уверенным неверным ответом, который
    каждая отдельная проверка отказывается давать.
    """
    if any(c.status == Status.CRITICAL for c in checks):
        return Status.CRITICAL
    if any(c.status == Status.WARNING for c in checks):
        return Status.WARNING
    if not any(c.status == Status.OK for c in checks):
        return Status.UNKNOWN
    return Status.OK


# --- checks ------------------------------------------------------------


async def _check_connection_utilization(
    conn: PgConnection, facts: _Facts
) -> HealthCheckResult:
    raw = await conn.fetchrow(
        f"""
        SELECT
            count(*) FILTER (WHERE {_SLOT_BACKENDS_FILTER}) AS total,
            count(*) FILTER (WHERE state = 'active') AS active,
            count(*) FILTER (WHERE state = 'idle') AS idle,
            count(*) FILTER (WHERE state = 'idle in transaction')
                AS idle_in_tx,
            count(*) FILTER (WHERE {_HIDDEN_BACKENDS_FILTER}) AS hidden,
            (SELECT setting::int FROM pg_settings
                WHERE name = 'max_connections') AS max_connections,
            (SELECT setting::int FROM pg_settings
                WHERE name = 'superuser_reserved_connections')
                AS superuser_reserved,
            (SELECT setting::int FROM pg_settings
                WHERE name = 'reserved_connections') AS reserved
        FROM pg_stat_activity
        """
    )
    row = _require_row(raw, 'connection_utilization')
    total = row['total']
    max_connections = row['max_connections']
    hidden = row['hidden']
    ratio = total / max_connections if max_connections else 0.0
    status = _classify(ratio, _CONN_UTIL_WARN, _CONN_UTIL_CRITICAL)
    # Сам счётчик верен при любых правах: строки чужих бэкендов роль
    # видит, скрыта только разбивка по `state`.
    message = (
        f'{total}/{max_connections} connections in use ({ratio * 100:.1f}%)'
    )
    if hidden:
        message += (
            f'; the active/idle breakdown covers {total - hidden} of them '
            f'({hidden} session(s) invisible to this role, which may '
            'include autovacuum workers that do not occupy a slot)'
        )
    return HealthCheckResult(
        id='connection_utilization',
        status=status,
        message=message,
        details={
            'total': total,
            'active': row['active'],
            'idle': row['idle'],
            'idle_in_transaction': row['idle_in_tx'],
            'hidden_backends': hidden,
            'max_connections': max_connections,
            'superuser_reserved_connections': row['superuser_reserved'],
            'reserved_connections': row['reserved'],
            'utilization_ratio': round(ratio, 4),
        },
    )


async def _check_idle_in_transaction(
    conn: PgConnection, facts: _Facts
) -> HealthCheckResult:
    raw = await conn.fetch(
        """
        SELECT pid, usename, application_name,
               extract(epoch FROM now() - xact_start) AS duration_seconds
        FROM pg_stat_activity
        WHERE state = 'idle in transaction'
          AND now() - xact_start > ($1 * interval '1 minute')
        ORDER BY duration_seconds DESC
        LIMIT $2
        """,
        _IDLE_IN_TX_WARN_MINUTES,
        _SESSION_LIMIT + 1,
    )
    rows, truncated = _take(raw, _SESSION_LIMIT)
    hidden = facts.hidden_backends
    if not rows and hidden:
        return HealthCheckResult(
            id='idle_in_transaction',
            status=Status.SKIPPED,
            message=_STATS_HIDDEN_MESSAGE.format(hidden=hidden),
            details={'sessions': [], 'hidden_backends': hidden},
        )

    max_duration = max((r['duration_seconds'] for r in rows), default=0.0)
    status: Status = (
        Status.CRITICAL
        if max_duration >= _IDLE_IN_TX_CRITICAL_MINUTES * 60
        else _rows_status(rows)
    )
    message = (
        f'{_counted(len(rows), truncated)} session(s) idle in transaction '
        f'for more than {_IDLE_IN_TX_WARN_MINUTES} min, which blocks vacuum'
        if rows
        else 'No long idle-in-transaction sessions'
    )
    if hidden:
        message += _STATS_PARTIAL_NOTE.format(hidden=hidden)
    return HealthCheckResult(
        id='idle_in_transaction',
        status=status,
        message=message,
        details={
            'sessions': [
                {
                    'pid': r['pid'],
                    'usename': r['usename'],
                    'application_name': r['application_name'],
                    'duration_seconds': round(r['duration_seconds'], 1),
                }
                for r in rows
            ],
            'hidden_backends': hidden,
            'truncated': truncated,
        },
    )


async def _check_long_running_queries(
    conn: PgConnection, facts: _Facts
) -> HealthCheckResult:
    raw = await conn.fetch(
        """
        SELECT pid, usename, application_name,
               extract(epoch FROM now() - query_start) AS duration_seconds
        FROM pg_stat_activity
        WHERE state = 'active'
          AND now() - query_start > ($1 * interval '1 minute')
        ORDER BY duration_seconds DESC
        LIMIT $2
        """,
        _LONG_QUERY_WARN_MINUTES,
        _SESSION_LIMIT + 1,
    )
    rows, truncated = _take(raw, _SESSION_LIMIT)
    hidden = facts.hidden_backends
    if not rows and hidden:
        return HealthCheckResult(
            id='long_running_queries',
            status=Status.SKIPPED,
            message=_STATS_HIDDEN_MESSAGE.format(hidden=hidden),
            details={'queries': [], 'hidden_backends': hidden},
        )

    message = (
        f'{_counted(len(rows), truncated)} query/queries running for more '
        f'than {_LONG_QUERY_WARN_MINUTES} min'
        if rows
        else 'No queries running past the threshold'
    )
    if hidden:
        message += _STATS_PARTIAL_NOTE.format(hidden=hidden)
    return HealthCheckResult(
        id='long_running_queries',
        status=_rows_status(rows),
        message=message,
        details={
            'queries': [
                {
                    'pid': r['pid'],
                    'usename': r['usename'],
                    'application_name': r['application_name'],
                    'duration_seconds': round(r['duration_seconds'], 1),
                }
                for r in rows
            ],
            'hidden_backends': hidden,
            'truncated': truncated,
        },
    )


async def _check_buffer_cache_hit_rate(
    conn: PgConnection, facts: _Facts
) -> HealthCheckResult:
    raw = await conn.fetchrow(
        """
        SELECT sum(heap_blks_hit) AS hit, sum(heap_blks_read) AS read
        FROM pg_statio_user_tables
        """
    )
    row = _require_row(raw, 'buffer_cache_hit_rate')
    # `sum(bigint)` возвращает numeric (asyncpg -> Decimal): приводим к
    # int, иначе в JSON уедет строка.
    hit = int(row['hit'] or 0)
    read = int(row['read'] or 0)
    total = hit + read
    if total == 0:
        return HealthCheckResult(
            id='buffer_cache_hit_rate',
            status=Status.SKIPPED,
            message=(
                'No table read statistics yet (empty database or no traffic '
                'since the last stats reset)'
            ),
            details={},
        )

    if facts.stats_window_too_short:
        return facts.stats_window_result('buffer_cache_hit_rate')

    ratio = hit / total
    status = _classify(
        ratio, _CACHE_HIT_WARN, _CACHE_HIT_CRITICAL, higher_is_worse=False
    )
    return HealthCheckResult(
        id='buffer_cache_hit_rate',
        status=status,
        message=f'Buffer cache hit rate: {ratio * 100:.2f}%',
        details={
            'heap_blks_hit': hit,
            'heap_blks_read': read,
            'ratio': round(ratio, 4),
            'stats_reset': facts.stats_reset,
            'stats_age_days': facts.stats_age_days,
        },
    )


async def _check_invalid_indexes(
    conn: PgConnection, facts: _Facts
) -> HealthCheckResult:
    raw = await conn.fetch(
        """
        SELECT n.nspname AS schema, c.relname AS index, t.relname AS "table"
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indexrelid
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE NOT i.indisvalid
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY n.nspname, t.relname, c.relname
        LIMIT $1
        """,
        _OBJECT_LIMIT + 1,
    )
    rows, truncated = _take(raw, _OBJECT_LIMIT)
    return HealthCheckResult(
        id='invalid_indexes',
        status=_rows_status(rows),
        message=(
            f'{_counted(len(rows), truncated)} invalid index(es), typically '
            'left behind by a failed CREATE INDEX CONCURRENTLY'
            if rows
            else 'No invalid indexes'
        ),
        details={
            'indexes': [dict(r) for r in rows],
            'truncated': truncated,
        },
    )


async def _check_invalid_constraints(
    conn: PgConnection, facts: _Facts
) -> HealthCheckResult:
    raw = await conn.fetch(
        """
        SELECT n.nspname AS schema, c.relname AS "table",
               con.conname AS constraint
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE NOT con.convalidated
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY n.nspname, c.relname, con.conname
        LIMIT $1
        """,
        _OBJECT_LIMIT + 1,
    )
    rows, truncated = _take(raw, _OBJECT_LIMIT)
    return HealthCheckResult(
        id='invalid_constraints',
        status=_rows_status(rows),
        message=(
            f'{_counted(len(rows), truncated)} constraint(s) marked NOT VALID '
            'or left unvalidated'
            if rows
            else 'No invalid constraints'
        ),
        details={
            'constraints': [dict(r) for r in rows],
            'truncated': truncated,
        },
    )


async def _check_unused_indexes(
    conn: PgConnection, facts: _Facts
) -> HealthCheckResult:
    if facts.stats_window_too_short:
        return facts.stats_window_result('unused_indexes')

    raw = await conn.fetch(
        """
        SELECT n.nspname AS schema, t.relname AS "table",
               i.relname AS index,
               pg_relation_size(i.oid) AS size_bytes, s.idx_scan
        FROM pg_stat_user_indexes s
        JOIN pg_index ix ON ix.indexrelid = s.indexrelid
        JOIN pg_class i ON i.oid = s.indexrelid
        JOIN pg_class t ON t.oid = s.relid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE s.idx_scan = 0
          AND NOT ix.indisprimary
          AND NOT ix.indisunique
          AND pg_relation_size(i.oid) > $1
        ORDER BY size_bytes DESC
        LIMIT $2
        """,
        _UNUSED_INDEX_MIN_SIZE_BYTES,
        _OBJECT_LIMIT + 1,
    )
    rows, truncated = _take(raw, _OBJECT_LIMIT)
    min_mb = _UNUSED_INDEX_MIN_SIZE_BYTES // (1024 * 1024)
    window = (
        'since statistics were last reset'
        if facts.stats_reset is not None
        else 'over the lifetime of this database'
    )
    return HealthCheckResult(
        id='unused_indexes',
        status=_rows_status(rows),
        message=(
            f'{_counted(len(rows), truncated)} index(es) larger than '
            f'{min_mb}MB were never scanned {window}'
            if rows
            else 'No large unused indexes'
        ),
        details={
            'indexes': [dict(r) for r in rows],
            'stats_reset': facts.stats_reset,
            'stats_age_days': facts.stats_age_days,
            'truncated': truncated,
        },
    )


async def _check_duplicate_indexes(
    conn: PgConnection, facts: _Facts
) -> HealthCheckResult:
    # Системные схемы отсекаются, как и в остальных проверках по
    # каталогу: дубли в pg_catalog не действие пользователя и чинить их
    # нечем.
    #
    # Порядок задаётся дважды. Внутри `array_agg` — иначе элементы
    # группы приходят в произвольном порядке, и один и тот же набор
    # индексов выглядит каждый раз по-новому. Снаружи — иначе `LIMIT`
    # отрезает произвольное подмножество групп; одного имени таблицы
    # для полного порядка мало.
    raw = await conn.fetch(
        """
        SELECT array_agg(i.indexrelid::regclass::text
                         ORDER BY i.indexrelid::regclass::text) AS indexes,
               i.indrelid::regclass::text AS "table"
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
        GROUP BY i.indrelid, i.indkey, i.indclass, i.indoption,
                 coalesce(i.indexprs::text, ''),
                 coalesce(i.indpred::text, '')
        HAVING count(*) > 1
        ORDER BY "table", indexes
        LIMIT $1
        """,
        _OBJECT_LIMIT + 1,
    )
    rows, truncated = _take(raw, _OBJECT_LIMIT)
    return HealthCheckResult(
        id='duplicate_indexes',
        status=_rows_status(rows),
        message=(
            f'{_counted(len(rows), truncated)} group(s) of duplicate indexes'
            if rows
            else 'No duplicate indexes'
        ),
        details={
            'groups': [dict(r) for r in rows],
            'truncated': truncated,
        },
    )


async def _check_transaction_id_wraparound(
    conn: PgConnection, facts: _Facts
) -> HealthCheckResult:
    freeze_max_age = (
        await conn.fetchval(
            'SELECT setting::bigint FROM pg_settings '
            "WHERE name = 'autovacuum_freeze_max_age'"
        )
        or _WRAPAROUND_DEFAULT_FREEZE_MAX_AGE
    )
    # Порядок по возрасту обязателен не только ради `LIMIT`: `worst_age`
    # берётся из этого же набора, и обрезка снизу его не меняет.
    raw_databases = await conn.fetch(
        """
        SELECT datname, age(datfrozenxid) AS xid_age
        FROM pg_database
        WHERE datallowconn
        ORDER BY xid_age DESC
        LIMIT $1
        """,
        _OBJECT_LIMIT + 1,
    )
    rows, databases_truncated = _take(raw_databases, _OBJECT_LIMIT)
    # `datfrozenxid` — минимум по всем отношениям базы, поэтому он
    # показывает риск, но не виновника. Отдельный список отношений
    # отвечает на вопрос «что вакуумить».
    relations = await conn.fetch(
        """
        SELECT n.nspname AS schema, c.relname AS relation,
               age(c.relfrozenxid) AS xid_age
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'm', 't')
          AND c.relfrozenxid <> '0'::xid
        ORDER BY age(c.relfrozenxid) DESC
        LIMIT $1
        """,
        _WRAPAROUND_RELATION_LIMIT,
    )
    worst_age = max((r['xid_age'] for r in rows), default=0)
    status = _classify(
        worst_age, _WRAPAROUND_WARN_AGE, _WRAPAROUND_CRITICAL_AGE
    )
    message = (
        f'Highest transaction id age: {worst_age} '
        f'(autovacuum_freeze_max_age={freeze_max_age})'
    )
    if relations and status is not Status.OK:
        top = relations[0]
        message += (
            f'; oldest relation is {top["schema"]}.{top["relation"]} '
            f'at {top["xid_age"]}'
        )
    return HealthCheckResult(
        id='transaction_id_wraparound',
        status=status,
        message=message,
        details={
            'databases': [
                {'datname': r['datname'], 'xid_age': r['xid_age']}
                for r in rows
            ],
            'databases_truncated': databases_truncated,
            # Отдельного `truncated` у списка отношений нет: это не
            # обрезанный ответ, а top-N по возрасту — вопрос «что
            # вакуумить первым» большего и не требует.
            'relations': [dict(r) for r in relations],
            'relations_limit': _WRAPAROUND_RELATION_LIMIT,
            'autovacuum_freeze_max_age': freeze_max_age,
        },
    )


async def _check_sequence_exhaustion(
    conn: PgConnection, facts: _Facts
) -> HealthCheckResult:
    # `pg_sequences` перечисляет все sequences независимо от прав, но
    # обнуляет `last_value` там, где нет SELECT/USAGE. Без явной проверки
    # привилегии «нет прав» неотличимо от «ни разу не использовалась», и
    # проверка отвечала бы `ok`, не посмотрев ни на один объект.
    raw_counts = await conn.fetchrow(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE NOT readable) AS unreadable
        FROM (
            SELECT has_sequence_privilege(
                       format('%I.%I', schemaname, sequencename),
                       'SELECT,USAGE') AS readable
            FROM pg_sequences
        ) s
        """
    )
    counts = _require_row(raw_counts, 'sequence_exhaustion')
    total = int(counts['total'] or 0)
    unreadable = int(counts['unreadable'] or 0)

    if total == 0:
        return HealthCheckResult(
            id='sequence_exhaustion',
            status=Status.OK,
            message='No sequences in this database',
            details={'total_sequences': 0, 'unreadable_sequences': 0},
        )
    if unreadable == total:
        return HealthCheckResult(
            id='sequence_exhaustion',
            status=Status.SKIPPED,
            message=(
                f'None of the {total} sequence(s) is readable by this role, '
                'so this check cannot be answered. Grant SELECT (or USAGE) '
                'on the sequences to the connection role.'
            ),
            details={
                'total_sequences': total,
                'unreadable_sequences': unreadable,
            },
        )

    raw = await conn.fetch(
        """
        SELECT schemaname, sequencename, last_value, max_value,
               round(100.0 * last_value / max_value, 2) AS pct_used
        FROM pg_sequences
        WHERE last_value IS NOT NULL
        ORDER BY pct_used DESC NULLS LAST
        LIMIT $1
        """,
        _OBJECT_LIMIT + 1,
    )
    # Порядок по убыванию заполненности, поэтому обрезается хвост:
    # худшая последовательность в выборке остаётся при любом лимите.
    rows, truncated = _take(raw, _OBJECT_LIMIT)
    worst = max(
        (float(r['pct_used']) for r in rows if r['pct_used'] is not None),
        default=0.0,
    )
    status = _classify(worst, _SEQUENCE_WARN_PCT, _SEQUENCE_CRITICAL_PCT)
    # `round(numeric, 2)` возвращает numeric (asyncpg -> Decimal).
    flagged = [
        {**dict(r), 'pct_used': float(r['pct_used'])}
        for r in rows
        if r['pct_used'] is not None
        and float(r['pct_used']) >= _SEQUENCE_WARN_PCT
    ]
    # Флаг считается по `flagged`, а не по `rows`: наружу уезжает именно
    # он. Выборка отсортирована по убыванию, поэтому `flagged` — её
    # префикс, и обрезка могла его задеть только одним способом: заняв
    # выборку целиком. Иначе за границей остались значения ниже порога, и
    # «At least» объявило бы нижней границей точное число.
    flagged_truncated = truncated and len(flagged) == len(rows)
    message = (
        f'{_counted(len(flagged), flagged_truncated)} sequence(s) more than '
        f'{_SEQUENCE_WARN_PCT:.0f}% consumed'
        if flagged
        else 'All readable sequences have ample headroom'
    )
    if unreadable:
        message += (
            f'. Partial result: {unreadable} of {total} sequence(s) are not '
            'readable by this role and were not checked'
        )
    return HealthCheckResult(
        id='sequence_exhaustion',
        status=status,
        message=message,
        details={
            'sequences': flagged,
            'total_sequences': total,
            'unreadable_sequences': unreadable,
            'truncated': flagged_truncated,
        },
    )


async def _check_replication_lag(
    conn: PgConnection, facts: _Facts
) -> HealthCheckResult:
    rows = await conn.fetch(
        """
        SELECT client_addr::text AS client_addr, application_name, state,
               pg_wal_lsn_diff(sent_lsn, replay_lsn) AS lag_bytes
        FROM pg_stat_replication
        """
    )
    if not rows:
        return HealthCheckResult(
            id='replication_lag',
            status=Status.SKIPPED,
            message='No connected replicas (or this server is not a primary)',
            details={},
        )
    # Непривилегированной роли `pg_stat_get_wal_senders()` оставляет один
    # `pid`, обнуляя остальное. У настоящего walsender'а `state` заполнен
    # всегда, поэтому сплошной NULL — признак урезанной выдачи, а не
    # нулевого лага.
    if all(r['state'] is None for r in rows):
        return HealthCheckResult(
            id='replication_lag',
            status=Status.SKIPPED,
            message=_REPLICATION_HIDDEN_MESSAGE.format(total=len(rows)),
            details={'replicas': len(rows)},
        )

    # NULL в `lag_bytes` — «неизвестно» (реплика ещё не стримит);
    # превращать его в 0 значило бы занизить максимум.
    lags = [r['lag_bytes'] for r in rows if r['lag_bytes'] is not None]
    if not lags:
        return HealthCheckResult(
            id='replication_lag',
            status=Status.SKIPPED,
            message=(
                f'{len(rows)} replica(s) connected, but none of them '
                'reports an LSN position yet'
            ),
            details={'replicas': [dict(r) for r in rows]},
        )
    max_lag = max(lags)
    status: Status = (
        Status.WARNING if max_lag > _REPLICATION_LAG_WARN_BYTES else Status.OK
    )
    return HealthCheckResult(
        id='replication_lag',
        status=status,
        message=f'Highest replica lag: {max_lag} bytes',
        details={'replicas': [dict(r) for r in rows]},
    )


# Сигнатура `(conn, facts)` общая, хотя `facts` нужен не всем: так
# диспетчер вызывает весь список одинаково.
_ALL_CHECKS = (
    _check_connection_utilization,
    _check_idle_in_transaction,
    _check_long_running_queries,
    _check_buffer_cache_hit_rate,
    _check_invalid_indexes,
    _check_invalid_constraints,
    _check_unused_indexes,
    _check_duplicate_indexes,
    _check_transaction_id_wraparound,
    _check_sequence_exhaustion,
    _check_replication_lag,
)


_Check = Callable[[PgConnection, _Facts], Awaitable[HealthCheckResult]]

# `skipped` не по существу дела, а потому, что запрос не выполнился.
# Константа, а не литерал по месту: тесты отличают этот исход от
# честного отказа именно по нему, и разошедшаяся копия строки сделала бы
# ту проверку бессмысленной.
CHECK_FAILED_MESSAGE = (
    'This check could not be run against this server; the pgsteward '
    'server log has the reason. The other checks in this report are '
    'unaffected.'
)

# Отказ соединения, а не запроса. `ConnectionDoesNotExistError` и
# `ConnectionFailureError` — потомки `PostgresConnectionError`;
# `InterfaceError` покрывает закрытое или занятое соединение со стороны
# клиента (в том числе откат к точке сохранения, которому уже некуда
# откатываться); `InternalClientError` asyncpg поднимает, когда его
# состояние разошлось с соединением, и продолжать по нему нельзя.
#
# Ошибки уровня запроса (`UndefinedColumnError`,
# `InsufficientPrivilegeError`) сюда не попадают и изоляцию сохраняют.
#
# Список живёт здесь, а не рядом с `_ERROR_CODES` в адаптере: адаптер
# импортирует health, обратное направление замкнуло бы импорты.
CONNECTION_LOST = (
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.InternalClientError,
)


async def _run_check(
    check: _Check, conn: PgConnection, facts: _Facts
) -> HealthCheckResult:
    """Выполнить проверку, не дав ей утащить за собой отчёт.

    Все проверки идут в одной READ ONLY транзакции, и после неудачного
    запроса PostgreSQL переводит её в aborted. Вложенная транзакция
    asyncpg — это SAVEPOINT: без отката к нему каждая следующая
    проверка падала бы с `current transaction is aborted`, и один
    честный отказ превращался бы в одиннадцать выдуманных.

    Изолируются ошибки уровня запроса. Потеря соединения уходит
    наверх, где адаптер переводит её в код подключения: откатывать там
    уже некуда, а «проверка не выполнилась» вместо «база недоступна»
    уводит от причины.
    """
    check_id = check.__name__.removeprefix('_check_')
    try:
        async with conn.transaction():
            return await check(conn, facts)
    except CONNECTION_LOST:
        raise
    except Exception:
        LOG.warning('health check "%s" failed', check_id, exc_info=True)
        return HealthCheckResult(
            id=check_id,
            status=Status.SKIPPED,
            message=CHECK_FAILED_MESSAGE,
        )


async def run_health_checks(conn: PgConnection) -> list[HealthCheckResult]:
    """Все проверки поверх одного снимка общих наблюдений.

    `_Facts.collect` намеренно не изолирован: это предусловие всех
    проверок, и без него отчёта нет вовсе.
    """
    facts = await _Facts.collect(conn)
    return [await _run_check(check, conn, facts) for check in _ALL_CHECKS]
