"""Тесты health-checks: классификация статусов и пороги.

Соединение с БД подменено минимальным фейком (`fetchrow`/`fetch`/`fetchval`
возвращают заранее заданные значения) — интересна не SQL-строка, а то, что
статус/сообщение корректно вычисляются из результата запроса.
"""

import logging
from decimal import Decimal

import asyncpg
import pytest

from pgsteward import health
from pgsteward.models import HealthCheckResult

# Порядок ответа — часть контракта: агент читает отчёт сверху вниз.
# Список задан явно, а не выведен из `_ALL_CHECKS`: иначе тест сверял бы
# реализацию сама с собой.
_EXPECTED_CHECK_IDS = [
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
]

# Ответы, на которых все одиннадцать проверок отвечают содержательно.
_HEALTHY_RESPONSES = {
    'fetchrow': {
        'total': 1,
        'active': 1,
        'idle': 0,
        'idle_in_tx': 0,
        'hidden': 0,
        'max_connections': 100,
        'superuser_reserved': 3,
        'reserved': 0,
        'hit': 1,
        'read': 0,
        'total_sequences': 0,
        'unreadable': 0,
    },
    'fetch': [],
    'fetchval': 0,
}


class Responses:
    """Разные ответы на последовательные вызовы одного метода.

    Нужно там, где проверка делает два разных запроса через один и тот
    же метод (`_check_transaction_id_wraparound`: базы, затем
    отношения). После исчерпания списка повторяется последний элемент.
    """

    def __init__(self, *items):
        self._items = list(items)
        self._index = 0

    def take(self):
        item = self._items[min(self._index, len(self._items) - 1)]
        self._index += 1
        return item


def _take(value):
    return value.take() if isinstance(value, Responses) else value


class _FakeSavepoint:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        self._conn.savepoints += 1
        return self

    async def __aexit__(self, extype, exc, tb):
        if extype is not None:
            self._conn.rollbacks += 1
        return False


class FakeHealthConn:
    """Фейковое соединение: заранее заданный ответ на каждый метод.

    Реальный SQL не разбирается — проверяется, что статус и сообщение
    правильно вычисляются из результата запроса.

    `transaction()` обязателен: `run_health_checks` заворачивает каждую
    проверку в SAVEPOINT. Без него отчёт целиком уходил бы в `skipped`,
    и тест, смотрящий только на id, проходил бы вхолостую.
    """

    def __init__(self, *, fetchrow=None, fetch=None, fetchval=None):
        self._fetchrow = fetchrow
        self._fetch = fetch if fetch is not None else []
        self._fetchval = fetchval
        self.savepoints = 0
        self.rollbacks = 0

    def transaction(self, readonly=False):
        return _FakeSavepoint(self)

    async def fetchrow(self, statement, *args):
        return _take(self._fetchrow)

    async def fetch(self, statement, *args):
        return _take(self._fetch)

    async def fetchval(self, statement, *args):
        return _take(self._fetchval)


def _facts(hidden=0, stats_reset=None, stats_age_days=None):
    """Общий снимок, который `run_health_checks` делает один раз.

    В тестах он задаётся явно: проверка получает его готовым и сама за
    этими данными не ходит.
    """
    return health._Facts(
        hidden_backends=hidden,
        stats_reset=stats_reset,
        stats_age_days=stats_age_days,
    )


# --- pure classification helpers -------------------------------------------


def test_classify_higher_is_worse():
    assert health._classify(0.5, warn=0.75, critical=0.9) == 'ok'
    assert health._classify(0.8, warn=0.75, critical=0.9) == 'warning'
    assert health._classify(0.95, warn=0.75, critical=0.9) == 'critical'


def test_classify_lower_is_worse():
    assert (
        health._classify(
            0.995, warn=0.99, critical=0.90, higher_is_worse=False
        )
        == 'ok'
    )
    assert (
        health._classify(0.95, warn=0.99, critical=0.90, higher_is_worse=False)
        == 'warning'
    )
    assert (
        health._classify(0.80, warn=0.99, critical=0.90, higher_is_worse=False)
        == 'critical'
    )


def test_overall_status_picks_worst():
    checks = [
        HealthCheckResult(id='a', status='ok', message=''),
        HealthCheckResult(id='b', status='skipped', message=''),
    ]
    assert health.overall_status(checks) == 'ok'

    checks.append(HealthCheckResult(id='c', status='warning', message=''))
    assert health.overall_status(checks) == 'warning'

    checks.append(HealthCheckResult(id='d', status='critical', message=''))
    assert health.overall_status(checks) == 'critical'


async def test_shared_facts_are_collected_once_per_report():
    """Две проверки не должны увидеть разное число скрытых сессий.

    Пока каждая спрашивала сама, `idle_in_transaction` и
    `long_running_queries` могли разойтись в выводах внутри одного
    ответа — и платили за это лишними round-trip'ами.
    """
    calls: list[str] = []

    class CountingConn(FakeHealthConn):
        async def fetchval(self, statement, *args):
            calls.append(statement)
            return await super().fetchval(statement, *args)

        async def fetchrow(self, statement, *args):
            calls.append(statement)
            return await super().fetchrow(statement, *args)

    conn = CountingConn(
        fetchrow={
            'total': 0,
            'active': 0,
            'idle': 0,
            'idle_in_tx': 0,
            'hidden': 0,
            'max_connections': 100,
            'superuser_reserved': 3,
            'reserved': 0,
            'hit': 0,
            'read': 0,
            'total_sequences': 0,
            'unreadable': 0,
            'stats_reset': None,
            'stats_age_days': None,
        },
        fetch=[],
        fetchval=0,
    )

    await health.run_health_checks(conn)

    hidden_query = 'SELECT count(*) FROM pg_stat_activity'
    assert sum(c.startswith(hidden_query) for c in calls) == 1
    assert sum('pg_stat_database' in c for c in calls) == 1


def test_overall_status_is_unknown_when_nothing_could_be_checked():
    """Под ролью без грантов все проверки уходят в `skipped`.

    Сводка `ok` в этом случае была бы тем самым уверенным неверным
    ответом, который каждая проверка по отдельности отказывается давать.
    """
    checks = [
        HealthCheckResult(id='a', status='skipped', message=''),
        HealthCheckResult(id='b', status='skipped', message=''),
    ]
    assert health.overall_status(checks) == 'unknown'

    checks.append(HealthCheckResult(id='c', status='ok', message=''))
    assert health.overall_status(checks) == 'ok'


# --- individual checks -----------------------------------------------------


async def test_connection_utilization_ok():
    conn = FakeHealthConn(
        fetchrow={
            'total': 10,
            'active': 2,
            'idle': 8,
            'idle_in_tx': 0,
            'hidden': 0,
            'max_connections': 100,
            'superuser_reserved': 3,
            'reserved': 0,
        }
    )
    result = await health._check_connection_utilization(conn, _facts())
    assert result.status == 'ok'
    assert result.details['utilization_ratio'] == 0.1


async def test_connection_utilization_critical():
    conn = FakeHealthConn(
        fetchrow={
            'total': 95,
            'active': 90,
            'idle': 5,
            'idle_in_tx': 0,
            'hidden': 0,
            'max_connections': 100,
            'superuser_reserved': 3,
            'reserved': 0,
        }
    )
    result = await health._check_connection_utilization(conn, _facts())
    assert result.status == 'critical'


async def test_connection_utilization_reports_hidden_backends():
    """Счётчик `total` верен при любых правах, разбивка по state — нет.

    Утилизация считается по `total`, поэтому статус остаётся валидным;
    но `active`/`idle` занижены, и сообщение обязано это признавать.
    """
    conn = FakeHealthConn(
        fetchrow={
            'total': 10,
            'active': 1,
            'idle': 1,
            'idle_in_tx': 0,
            'hidden': 8,
            'max_connections': 100,
            'superuser_reserved': 3,
            'reserved': 0,
        }
    )
    result = await health._check_connection_utilization(conn, _facts())
    assert result.status == 'ok'
    assert result.details['hidden_backends'] == 8
    assert 'invisible to this role' in result.message


async def test_buffer_cache_hit_rate_skipped_when_no_data():
    conn = FakeHealthConn(fetchrow={'hit': None, 'read': None})
    result = await health._check_buffer_cache_hit_rate(conn, _facts())
    assert result.status == 'skipped'


async def test_buffer_cache_hit_rate_critical_on_low_ratio():
    conn = FakeHealthConn(fetchrow={'hit': 80, 'read': 20})
    result = await health._check_buffer_cache_hit_rate(conn, _facts())
    assert result.status == 'critical'
    assert result.details['ratio'] == 0.8


async def test_buffer_cache_hit_rate_returns_native_numbers_not_decimal():
    """`sum(bigint)` в Postgres -> `numeric` -> asyncpg Decimal.

    `_to_json` сериализует `Decimal` через `default=str`, превращая
    числа в строки, поэтому details должны быть нативными int/float.
    """
    conn = FakeHealthConn(
        fetchrow={'hit': Decimal('80'), 'read': Decimal('20')}
    )
    result = await health._check_buffer_cache_hit_rate(conn, _facts())
    assert isinstance(result.details['heap_blks_hit'], int)
    assert isinstance(result.details['heap_blks_read'], int)
    assert isinstance(result.details['ratio'], float)


async def test_idle_in_transaction_no_sessions_is_ok():
    conn = FakeHealthConn(fetch=[], fetchval=0)
    result = await health._check_idle_in_transaction(conn, _facts())
    assert result.status == 'ok'
    assert result.details['sessions'] == []


async def test_idle_in_transaction_warning_below_critical_threshold():
    conn = FakeHealthConn(
        fetch=[
            {
                'pid': 1,
                'usename': 'app',
                'application_name': 'svc',
                'duration_seconds': 400.0,
            },
        ],
        fetchval=0,
    )
    result = await health._check_idle_in_transaction(conn, _facts())
    assert result.status == 'warning'
    # Текст чужого запроса не должен попадать в details (приватность).
    assert 'query' not in result.details['sessions'][0]


async def test_idle_in_transaction_critical_above_threshold():
    conn = FakeHealthConn(
        fetch=[
            {
                'pid': 1,
                'usename': 'app',
                'application_name': 'svc',
                'duration_seconds': 2000.0,
            },
        ],
        fetchval=0,
    )
    result = await health._check_idle_in_transaction(conn, _facts())
    assert result.status == 'critical'


async def test_long_running_queries_does_not_expose_query_text():
    conn = FakeHealthConn(
        fetch=[
            {
                'pid': 7,
                'usename': 'app',
                'application_name': 'svc',
                'duration_seconds': 600.0,
            },
        ],
        fetchval=0,
    )
    result = await health._check_long_running_queries(conn, _facts())
    assert result.status == 'warning'
    assert 'query' not in result.details['queries'][0]


# --- visibility of other sessions ------------------------------------------
#
# Роль без pg_monitor видит строки чужих бэкендов, но с NULL в state:
# фильтр `state = '...'` не находит ничего, и пустая выборка означает не
# «всё хорошо», а «ответить нечем». Раньше обе проверки возвращали в этой
# ситуации `ok` — ложное подтверждение здоровья ровно под той ролью,
# которую рекомендует docs/security.md.


async def test_idle_in_transaction_skipped_when_backends_are_hidden():
    conn = FakeHealthConn(fetch=[])
    result = await health._check_idle_in_transaction(conn, _facts(hidden=3))
    assert result.status == 'skipped'
    assert result.details['hidden_backends'] == 3
    assert 'pg_monitor' in result.message


async def test_long_running_queries_skipped_when_backends_are_hidden():
    conn = FakeHealthConn(fetch=[])
    result = await health._check_long_running_queries(conn, _facts(hidden=3))
    assert result.status == 'skipped'
    assert result.details['hidden_backends'] == 3
    assert 'pg_monitor' in result.message


async def test_idle_in_transaction_keeps_status_when_partially_visible():
    """Нашлась хоть одна сессия — находка валидна, но выборка неполная.

    Понижать такой результат до `skipped` нельзя: проблема реальна.
    Выдавать его за полный — тоже, поэтому статус сохраняется, а неполнота
    уходит в message и details.
    """
    conn = FakeHealthConn(
        fetch=[
            {
                'pid': 1,
                'usename': 'app',
                'application_name': 'svc',
                'duration_seconds': 2000.0,
            },
        ],
    )
    result = await health._check_idle_in_transaction(conn, _facts(hidden=2))
    assert result.status == 'critical'
    assert result.details['hidden_backends'] == 2
    assert 'Partial result' in result.message


async def test_unused_indexes_reports_found_rows():
    conn = FakeHealthConn(
        fetch=[
            {
                'schema': 'public',
                'table': 't',
                'index': 'idx_t',
                'size_bytes': 10_000_000,
                'idx_scan': 0,
            },
        ]
    )
    result = await health._check_unused_indexes(conn, _facts())
    assert result.status == 'warning'
    assert len(result.details['indexes']) == 1


async def test_transaction_id_wraparound_ok_below_threshold():
    conn = FakeHealthConn(
        fetchval=200_000_000,
        fetch=Responses([{'datname': 'db', 'xid_age': 50_000_000}], []),
    )
    result = await health._check_transaction_id_wraparound(conn, _facts())
    assert result.status == 'ok'


async def test_transaction_id_wraparound_ok_near_freeze_max_age():
    """Возраст у автовакуумного порога — штатный цикл, а не риск."""
    conn = FakeHealthConn(
        fetchval=200_000_000,
        fetch=Responses([{'datname': 'db', 'xid_age': 198_564_928}], []),
    )
    result = await health._check_transaction_id_wraparound(conn, _facts())
    assert result.status == 'ok'


async def test_transaction_id_wraparound_warning_above_absolute_threshold():
    conn = FakeHealthConn(
        fetchval=200_000_000,
        fetch=Responses(
            [{'datname': 'db', 'xid_age': 1_200_000_000}],
            [
                {
                    'schema': 'public',
                    'relation': 'big',
                    'xid_age': 1_200_000_000,
                }
            ],
        ),
    )
    result = await health._check_transaction_id_wraparound(conn, _facts())
    assert result.status == 'warning'


async def test_transaction_id_wraparound_critical_near_hard_limit():
    conn = FakeHealthConn(
        fetchval=200_000_000,
        fetch=Responses([{'datname': 'db', 'xid_age': 1_900_000_000}], []),
    )
    result = await health._check_transaction_id_wraparound(conn, _facts())
    assert result.status == 'critical'


async def test_sequence_exhaustion_flags_only_above_warn_threshold():
    conn = FakeHealthConn(
        fetchrow={'total': 2, 'unreadable': 0},
        fetch=[
            {
                'schemaname': 'public',
                'sequencename': 'low_seq',
                'last_value': 10,
                'max_value': 1000,
                'pct_used': 1.0,
            },
            {
                'schemaname': 'public',
                'sequencename': 'hot_seq',
                'last_value': 950,
                'max_value': 1000,
                'pct_used': 95.0,
            },
        ],
    )
    result = await health._check_sequence_exhaustion(conn, _facts())
    assert result.status == 'critical'
    assert len(result.details['sequences']) == 1
    assert result.details['sequences'][0]['sequencename'] == 'hot_seq'


async def test_sequence_exhaustion_returns_native_float_not_decimal():
    """В `details` должен лежать нативный float, а не Decimal.

    `round(numeric, 2)` в SQL приходит от asyncpg как Decimal, и в JSON
    он уехал бы строкой.
    """
    conn = FakeHealthConn(
        fetchrow={'total': 1, 'unreadable': 0},
        fetch=[
            {
                'schemaname': 'public',
                'sequencename': 'hot_seq',
                'last_value': 950,
                'max_value': 1000,
                'pct_used': Decimal('95.00'),
            },
        ],
    )
    result = await health._check_sequence_exhaustion(conn, _facts())
    assert isinstance(result.details['sequences'][0]['pct_used'], float)


async def test_replication_lag_skipped_when_no_replicas():
    conn = FakeHealthConn(fetch=[])
    result = await health._check_replication_lag(conn, _facts())
    assert result.status == 'skipped'


async def test_replication_lag_warning_above_threshold():
    conn = FakeHealthConn(
        fetch=[
            {
                'client_addr': '10.0.0.1',
                'application_name': 'replica1',
                'state': 'streaming',
                'lag_bytes': 32 * 1024 * 1024,
            },
        ]
    )
    result = await health._check_replication_lag(conn, _facts())
    assert result.status == 'warning'


async def test_replication_lag_skipped_when_stats_are_masked():
    """Скрытая статистика реплик — `skipped`, а не нулевой лаг.

    `pg_stat_get_wal_senders()` оставляет непривилегированной роли только
    `pid`: реплики есть, но их лаг неизвестен, и читать NULL как 0
    значило бы отрапортовать «Highest replica lag: 0 bytes».
    """
    conn = FakeHealthConn(
        fetch=[
            {
                'client_addr': None,
                'application_name': None,
                'state': None,
                'lag_bytes': None,
            },
        ]
    )
    result = await health._check_replication_lag(conn, _facts())
    assert result.status == 'skipped'
    assert 'pg_monitor' in result.message


async def test_replication_lag_ignores_replicas_without_lsn():
    """NULL у одной реплики не должен занижать максимум до нуля."""
    conn = FakeHealthConn(
        fetch=[
            {
                'client_addr': '10.0.0.1',
                'application_name': 'starting',
                'state': 'startup',
                'lag_bytes': None,
            },
            {
                'client_addr': '10.0.0.2',
                'application_name': 'replica2',
                'state': 'streaming',
                'lag_bytes': 32 * 1024 * 1024,
            },
        ]
    )
    result = await health._check_replication_lag(conn, _facts())
    assert result.status == 'warning'
    assert '33554432 bytes' in result.message


async def test_run_health_checks_executes_all_checks():
    conn = FakeHealthConn(**_HEALTHY_RESPONSES)
    results = await health.run_health_checks(conn)
    by_id = {r.id: r for r in results}

    assert [r.id for r in results] == _EXPECTED_CHECK_IDS
    # На этих ответах отвечают все, кроме `replication_lag`: реплик нет,
    # и это его штатный отказ, а не сбой.
    assert by_id['replication_lag'].status == 'skipped'
    assert 'No connected replicas' in by_id['replication_lag'].message
    # Сравнение с `len(_ALL_CHECKS)` сверяло реализацию саму с собой, а
    # список одних только id проходил и тогда, когда каждая проверка
    # отвечала `skipped` из-за упавшего запроса.
    assert all(
        r.status != 'skipped' for r in results if r.id != 'replication_lag'
    )
    assert conn.savepoints == len(_EXPECTED_CHECK_IDS)
    assert conn.rollbacks == 0


# --- isolation between checks ----------------------------------------------

# `pg_constraint` встречается ровно в одной проверке, и на пустой
# выборке та отвечает `ok` — значит `skipped` у неё может быть только
# следствием сбоя, а не штатным отказом.
_ONLY_IN_INVALID_CONSTRAINTS = 'pg_constraint'


class _FailingConn(FakeHealthConn):
    """Соединение, роняющее запросы одной проверки."""

    def __init__(self, failing_statement: str, **kwargs):
        super().__init__(**kwargs)
        self._failing = failing_statement

    async def fetch(self, statement, *args):
        if self._failing in statement:
            raise asyncpg.UndefinedColumnError('column "convalidated" ...')
        return await super().fetch(statement, *args)


async def test_one_failing_check_does_not_sink_the_report(caplog):
    """Проверка умеет отвечать `skipped`, но не на исключение драйвера.

    Одна незнакомая версией колонка роняла все одиннадцать сразу.
    """
    caplog.set_level(logging.WARNING, logger='pgsteward')
    conn = _FailingConn(_ONLY_IN_INVALID_CONSTRAINTS, **_HEALTHY_RESPONSES)

    results = await health.run_health_checks(conn)
    by_id = {r.id: r for r in results}

    assert by_id['invalid_constraints'].status == 'skipped'
    assert 'server log' in by_id['invalid_constraints'].message
    assert all(
        r.status != 'skipped'
        for r in results
        if r.id not in ('invalid_constraints', 'replication_lag')
    )
    # Откат к точке сохранения — не деталь реализации: без него
    # PostgreSQL держит транзакцию в aborted, и каждая следующая
    # проверка падала бы с `current transaction is aborted`.
    assert conn.rollbacks == 1
    assert 'invalid_constraints' in caplog.text


async def test_a_failing_check_keeps_the_driver_message_out_of_the_answer():
    """Текст исключения описывает схему и в контекст модели не идёт."""
    conn = _FailingConn(_ONLY_IN_INVALID_CONSTRAINTS, **_HEALTHY_RESPONSES)

    results = await health.run_health_checks(conn)
    message = next(r for r in results if r.id == 'invalid_constraints').message

    assert 'convalidated' not in message


class _DisconnectingConn(FakeHealthConn):
    """Соединение, потерянное на первом же запросе проверки."""

    def __init__(self, error, **kwargs):
        super().__init__(**kwargs)
        self._error = error

    async def fetch(self, statement, *args):
        raise self._error


@pytest.mark.parametrize(
    'error',
    [
        asyncpg.ConnectionDoesNotExistError(
            'connection was closed in the middle of operation'
        ),
        asyncpg.InterfaceError('connection is closed'),
        asyncpg.InternalClientError('cannot switch to state 15'),
    ],
    ids=['postgres_connection', 'interface', 'internal_client'],
)
async def test_a_lost_connection_is_not_reported_as_eleven_skips(error):
    """Изоляция — для ошибок запроса, а не для потери соединения.

    Откатывать к точке сохранения уже некуда, и отчёт из одиннадцати
    «проверка не выполнилась» уводит от настоящей причины: база
    недоступна.
    """
    conn = _DisconnectingConn(error, **_HEALTHY_RESPONSES)

    with pytest.raises(health.CONNECTION_LOST):
        await health.run_health_checks(conn)


# --- refusing to answer instead of a false ok ------------------------------
#
# `pg_sequences` перечисляет все sequences независимо от прав, обнуляя
# `last_value`. Без проверки привилегии «нет прав» неотличимо от «ни разу
# не использовалась», и проверка отвечала бы ok, не посмотрев ни на один
# объект — под той самой least-privilege ролью, которую рекомендует
# docs/security.md.


async def test_sequence_exhaustion_skipped_when_nothing_is_readable():
    conn = FakeHealthConn(fetchrow={'total': 3, 'unreadable': 3}, fetch=[])
    result = await health._check_sequence_exhaustion(conn, _facts())

    assert result.status == 'skipped'
    assert result.details['unreadable_sequences'] == 3
    assert 'GRANT' in result.message.upper()


async def test_sequence_exhaustion_flags_partial_visibility():
    conn = FakeHealthConn(
        fetchrow={'total': 3, 'unreadable': 2},
        fetch=[
            {
                'schemaname': 'public',
                'sequencename': 'seq',
                'last_value': 10,
                'max_value': 1000,
                'pct_used': 1.0,
            }
        ],
    )
    result = await health._check_sequence_exhaustion(conn, _facts())

    assert result.status == 'ok'
    assert result.details['unreadable_sequences'] == 2
    assert 'Partial result' in result.message


async def test_sequence_exhaustion_ok_without_sequences():
    conn = FakeHealthConn(fetchrow={'total': 0, 'unreadable': 0})
    result = await health._check_sequence_exhaustion(conn, _facts())

    assert result.status == 'ok'
    assert result.details['total_sequences'] == 0


# --- result caps -----------------------------------------------------------


def _sessions(count: int) -> list[dict]:
    return [
        {
            'pid': i,
            'usename': 'app',
            'application_name': 'worker',
            'duration_seconds': 600.0 + i,
        }
        for i in range(count)
    ]


async def test_a_capped_check_says_the_number_is_a_floor():
    """Без флага нижняя граница читается как итог.

    `search_schema` тянет на строку больше лимита ровно за этим;
    health-checks просто обрезали и молчали.
    """
    conn = FakeHealthConn(fetch=_sessions(health._SESSION_LIMIT + 1))

    result = await health._check_idle_in_transaction(conn, _facts())

    assert result.details['truncated'] is True
    assert len(result.details['sessions']) == health._SESSION_LIMIT
    assert result.message.startswith(f'At least {health._SESSION_LIMIT} ')


async def test_a_full_result_at_exactly_the_cap_is_not_called_truncated():
    """Выдача ровно в лимит — полная, и число в сообщении точное."""
    conn = FakeHealthConn(fetch=_sessions(health._SESSION_LIMIT))

    result = await health._check_idle_in_transaction(conn, _facts())

    assert result.details['truncated'] is False
    assert result.message.startswith(f'{health._SESSION_LIMIT} ')


def _objects(count: int, key: str) -> list[dict]:
    """`count` строк каталожной проверки, различающихся именем объекта.

    Ключи — ровно те, что возвращает запрос: лишний столбец в фейке
    скрыл бы опечатку в настоящем.
    """
    return [
        {'schema': 'public', 'table': 't', key: f'obj{i}'}
        for i in range(count)
    ]


# Каталожные проверки: (функция, ключ списка в details, колонка имени).
_CATALOG_CHECKS = [
    (health._check_invalid_indexes, 'indexes', 'index'),
    (health._check_invalid_constraints, 'constraints', 'constraint'),
]
_CATALOG_CHECK_IDS = ['invalid_indexes', 'invalid_constraints']


@pytest.mark.parametrize(
    ('check', 'details_key', 'column'), _CATALOG_CHECKS, ids=_CATALOG_CHECK_IDS
)
async def test_catalog_object_lists_are_capped(check, details_key, column):
    """Каталожные проверки капятся наравне с теми, что читают статистику.

    Ни одна из них не ограничена числом объектов по природе: после
    неудачного `CREATE INDEX CONCURRENTLY` в цикле или миграции,
    добавившей `NOT VALID` на каждую таблицу, список равен размеру
    схемы и уезжает в контекст модели целиком.
    """
    conn = FakeHealthConn(fetch=_objects(health._OBJECT_LIMIT + 1, column))

    result = await check(conn, _facts())

    assert len(result.details[details_key]) == health._OBJECT_LIMIT
    assert result.details['truncated'] is True
    assert result.message.startswith(f'At least {health._OBJECT_LIMIT} ')


@pytest.mark.parametrize(
    ('check', 'details_key', 'column'), _CATALOG_CHECKS, ids=_CATALOG_CHECK_IDS
)
async def test_a_catalog_list_at_exactly_the_cap_is_not_truncated(
    check, details_key, column
):
    conn = FakeHealthConn(fetch=_objects(health._OBJECT_LIMIT, column))

    result = await check(conn, _facts())

    assert len(result.details[details_key]) == health._OBJECT_LIMIT
    assert result.details['truncated'] is False
    assert result.message.startswith(f'{health._OBJECT_LIMIT} ')


async def test_wraparound_caps_the_database_list_without_losing_the_worst():
    """Обрезка списка баз не должна менять сам вердикт.

    `worst_age` берётся из той же выборки, поэтому порядок по возрасту —
    не косметика: после обрезки снизу максимум обязан остаться на месте.
    """
    databases = [
        {'datname': f'db{i}', 'xid_age': 2_000_000_000 - i}
        for i in range(health._OBJECT_LIMIT + 1)
    ]
    conn = FakeHealthConn(fetch=Responses(databases, []), fetchval=200_000_000)

    result = await health._check_transaction_id_wraparound(conn, _facts())

    assert len(result.details['databases']) == health._OBJECT_LIMIT
    assert result.details['databases_truncated'] is True
    assert result.status == 'critical'
    assert '2000000000' in result.message


async def test_wraparound_names_the_limit_on_the_relation_list():
    """Список отношений — top-N, а не обрезанный ответ.

    Флага `truncated` у него поэтому нет: вопрос «что вакуумить первым»
    полного перечня не требует. Но размер выборки назван, иначе десять
    строк читаются как весь список проблемных отношений.
    """
    relations = [
        {'schema': 'public', 'relation': f'r{i}', 'xid_age': 1_900_000_000}
        for i in range(health._WRAPAROUND_RELATION_LIMIT)
    ]
    conn = FakeHealthConn(
        fetch=Responses(
            [{'datname': 'db', 'xid_age': 1_900_000_000}], relations
        ),
        fetchval=200_000_000,
    )

    result = await health._check_transaction_id_wraparound(conn, _facts())

    assert 'truncated' not in result.details
    assert (
        result.details['relations_limit'] == health._WRAPAROUND_RELATION_LIMIT
    )
    assert len(result.details['relations']) == (
        health._WRAPAROUND_RELATION_LIMIT
    )


def _sequences(pcts: list[float]) -> list[dict]:
    """Строки `pg_sequences` с заданной заполненностью.

    Порядок сохраняется как есть: запрос отдаёт их по убыванию
    `pct_used`, и обрезка проверяется именно относительно него.
    """
    return [
        {
            'schemaname': 'public',
            'sequencename': f'seq{i}',
            'last_value': pct,
            'max_value': 100,
            'pct_used': pct,
        }
        for i, pct in enumerate(pcts)
    ]


async def _sequence_result(rows: list[dict]) -> HealthCheckResult:
    conn = FakeHealthConn(
        fetchrow={'total': len(rows), 'unreadable': 0}, fetch=rows
    )
    return await health._check_sequence_exhaustion(conn, _facts())


async def test_sequence_count_is_exact_when_the_cut_fell_below_the_threshold():
    """Обрезан хвост ниже порога — значит флагнутые сосчитаны полностью.

    Список в `details` — не выборка, а только превысившие порог, и флаг
    рядом с ним должен говорить о нём. Иначе точное число уходит наружу
    как «At least N», и модель читает полный ответ как частичный.
    """
    rows = _sequences([90.0] * 3 + [1.0] * (health._OBJECT_LIMIT - 2))
    assert len(rows) == health._OBJECT_LIMIT + 1

    result = await _sequence_result(rows)

    assert result.details['truncated'] is False
    assert len(result.details['sequences']) == 3
    assert result.message.startswith('3 sequence(s) ')


async def test_sequence_count_is_a_floor_when_flagged_filled_the_cap():
    """Выборка целиком выше порога — за границей могут быть ещё."""
    rows = _sequences([90.0] * (health._OBJECT_LIMIT + 1))

    result = await _sequence_result(rows)

    assert result.details['truncated'] is True
    assert len(result.details['sequences']) == health._OBJECT_LIMIT
    assert result.message.startswith(f'At least {health._OBJECT_LIMIT} ')


async def test_a_cut_below_the_threshold_leaves_the_empty_list_complete():
    """Ни одной флагнутой — ответ полный, сколько бы ни было обрезано."""
    rows = _sequences([1.0] * (health._OBJECT_LIMIT + 1))

    result = await _sequence_result(rows)

    assert result.details['truncated'] is False
    assert result.details['sequences'] == []
    assert result.status == 'ok'


async def test_unused_indexes_skipped_right_after_a_stats_reset():
    """Сразу после сброса статистики проверка отказывается отвечать.

    `idx_scan = 0` в этом окне означает «нет данных», а не «индекс не
    нужен».
    """
    conn = FakeHealthConn(
        fetch=[
            {
                'schema': 'public',
                'table': 't',
                'index': 'idx_t',
                'size_bytes': 10_000_000,
                'idx_scan': 0,
            }
        ],
    )
    result = await health._check_unused_indexes(
        conn, _facts(stats_reset='recent', stats_age_days=0.5)
    )

    assert result.status == 'skipped'
    assert 'reset' in result.message


async def test_unused_indexes_active_when_statistics_were_never_reset():
    """`stats_reset IS NULL` — не повод пропускать проверку.

    Окно в этом случае максимально длинное, а не неизвестно плохое,
    поэтому проверка обязана остаться содержательной.
    """
    conn = FakeHealthConn(
        fetch=[
            {
                'schema': 'public',
                'table': 't',
                'index': 'idx_t',
                'size_bytes': 10_000_000,
                'idx_scan': 0,
            }
        ],
    )
    result = await health._check_unused_indexes(conn, _facts())

    assert result.status == 'warning'
    assert 'lifetime of this database' in result.message


async def test_buffer_cache_skipped_right_after_a_stats_reset():
    conn = FakeHealthConn(fetchrow={'hit': 10, 'read': 90})
    result = await health._check_buffer_cache_hit_rate(
        conn, _facts(stats_reset='recent', stats_age_days=0.1)
    )

    assert result.status == 'skipped'


async def test_wraparound_names_the_relation_to_vacuum():
    """Проверка называет отношение, которое нужно вакуумить.

    `datfrozenxid` — минимум по базе: он показывает риск, но не то, что
    именно его создаёт.
    """
    conn = FakeHealthConn(
        fetchval=200_000_000,
        fetch=Responses(
            [{'datname': 'db', 'xid_age': 1_200_000_000}],
            [
                {
                    'schema': 'public',
                    'relation': 'events',
                    'xid_age': 1_200_000_000,
                }
            ],
        ),
    )
    result = await health._check_transaction_id_wraparound(conn, _facts())

    assert result.status == 'warning'
    assert result.details['relations'][0]['relation'] == 'events'
    assert 'public.events' in result.message
