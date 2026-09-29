"""Проверка исполнения statement'ов адаптером PostgreSQL.

Пул asyncpg подменён фейком: интересует не SQL, а режим транзакции и
объём вычитываемых строк.
"""

import json
import socket
import ssl

import asyncpg
import pytest
from pydantic import SecretStr

from pgsteward.adapters.postgres import (
    _RELKIND_NAMES,
    PostgresAdapter,
    _table_notes,
    _with_article,
    classify_connection_error,
)
from pgsteward.config import ConnectionConfig, SecurityPolicy
from pgsteward.errors import (
    AdapterConnectionError,
    ExplainError,
    QueryTimeoutError,
    StatementNotAllowedError,
    WriteForbiddenError,
)
from pgsteward.models import ForeignKeyInfo, IndexInfo
from pgsteward.serialization import json_size

# Текст, каким его даёт asyncpg при потере соединения: он должен
# остаться в логе и не уехать в контекст модели.
LOST_CONNECTION_MESSAGE = 'connection was closed in the middle of operation'

WRITING_READ_STATEMENTS = [
    'WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d',
    'EXPLAIN ANALYZE INSERT INTO t VALUES (1)',
]


class FakeCursor:
    """Курсор asyncpg: каждый `fetch` продвигает позицию.

    Настоящий курсор отдаёт следующую порцию, а не ту же самую с
    начала. Адаптер читает чанками, поэтому фейк, который не
    продвигается, либо зациклил бы его, либо выдал бы одни и те же
    строки за разные.
    """

    def __init__(self, records, log):
        self._records = records
        self._log = log

    async def fetch(self, count):
        self._log.append(count)
        batch = self._records[:count]
        self._records = self._records[count:]
        return batch


class FakeConnection:
    """Соединение asyncpg: запоминает и statement, и переданные args.

    Аргументы записываются отдельно намеренно. Драйвер получает
    параметры вне текста statement'а, и это единственное, что стоит
    между `query` и инъекцией; фейк, который их глотает, пропустил бы
    регрессию, подставляющую значения прямо в SQL.
    """

    def __init__(self, records):
        self._records = records
        self.readonly_flags: list[bool] = []
        self.savepoints = 0
        self.depth = 0
        self.statements: list[str] = []
        self.args: list[tuple] = []
        self.cursor_requests: list[int] = []

    def _record(self, statement, args):
        self.statements.append(statement)
        self.args.append(args)

    def transaction(self, readonly=False):
        return _FakeTransaction(self, readonly)

    async def fetch(self, statement, *args):
        self._record(statement, args)
        return list(self._records)

    async def cursor(self, statement, *args):
        self._record(statement, args)
        return FakeCursor(list(self._records), self.cursor_requests)

    async def fetchrow(self, statement, *args):
        self._record(statement, args)
        # Объединённый набор ключей всех потребителей fetchrow:
        # health-checks (connection_utilization, buffer_cache_hit_rate,
        # sequence_exhaustion) и `_fetch_relation`.
        return {
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
            'unreadable': 0,
            'oid': 1,
            'relkind': 'r',
            'reltuples': 0.0,
        }

    async def fetchval(self, statement, *args):
        self._record(statement, args)
        return None


class _FakeTransaction:
    """Транзакция asyncpg: вложенная становится SAVEPOINT.

    Различие существенно. `readonly` у вложенной игнорируется — она
    наследует режим внешней, — поэтому считать её открытием пишущей
    транзакции нельзя, а откат к точке сохранения как раз и есть то,
    ради чего health-checks в неё завёрнуты.
    """

    def __init__(self, conn, readonly):
        self._conn = conn
        self._readonly = readonly

    async def __aenter__(self):
        if self._conn.depth:
            self._conn.savepoints += 1
        else:
            self._conn.readonly_flags.append(self._readonly)
        self._conn.depth += 1
        return self

    async def __aexit__(self, *exc_info):
        self._conn.depth -= 1
        return False


class FakePool:
    """Пул asyncpg: соединение берётся и возвращается вручную.

    Так же делает адаптер, чтобы оборачивать в понятную ошибку только
    захват соединения.
    """

    def __init__(self, conn, acquire_error=None):
        self._conn = conn
        self._acquire_error = acquire_error
        self.released = 0
        self.acquire_timeouts: list[float | None] = []

    # ASYNC109: сигнатура повторяет asyncpg.Pool.acquire, а не вводит
    # свой таймаут — заменить его на asyncio.timeout здесь нечем.
    async def acquire(self, timeout=None):  # noqa: ASYNC109
        self.acquire_timeouts.append(timeout)
        if self._acquire_error is not None:
            raise self._acquire_error
        return self._conn

    async def release(self, conn):
        self.released += 1


def _adapter(
    records=(),
    *,
    read_only=True,
    max_rows=1000,
    max_bytes=-1,
    **policy_flags,
):
    """`max_bytes=-1` по умолчанию: бюджет проверяется отдельными тестами.

    В конфигурации он включён, но тесту про число строк лишний лимит
    только мешает рассуждать о причине обрезки.
    """
    conn = FakeConnection([dict(r) for r in records])
    config = ConnectionConfig(
        id='test',
        read_only=read_only,
        max_rows=max_rows,
        max_bytes=max_bytes,
    )
    adapter = PostgresAdapter(config, SecurityPolicy(**policy_flags))
    adapter._pool = FakePool(conn)
    return adapter, conn


def _explain_plan_row(plan: dict, planning_time=None, execution_time=None):
    """Ответ `EXPLAIN (FORMAT JSON)` в том виде, в каком его даёт PG.

    Без `ANALYZE` PostgreSQL не выводит ни `Planning Time`, ни
    `Execution Time`, поэтому оба поля опциональны.
    """
    doc: dict = {'Plan': plan}
    if planning_time is not None:
        doc['Planning Time'] = planning_time
    if execution_time is not None:
        doc['Execution Time'] = execution_time
    return {'QUERY PLAN': json.dumps([doc])}


@pytest.mark.parametrize('statement', WRITING_READ_STATEMENTS)
async def test_writing_statements_disguised_as_read_stay_in_readonly_tx(
    statement,
):
    """Парсер видит READ по первому слову — БД должна остаться барьером.

    Подключение доступно на запись, но политика DML запрещает её: такой
    statement обязан выполняться в READ ONLY транзакции, иначе запрет
    `allow_dml` обходится.
    """
    adapter, conn = _adapter(read_only=False, allow_dml=False)
    await adapter.execute(statement)
    assert conn.readonly_flags == [True]


async def test_read_on_readonly_connection_uses_readonly_tx():
    adapter, conn = _adapter([{'id': 1}])
    await adapter.execute('SELECT * FROM t')
    assert conn.readonly_flags == [True]


# Строка, покрывающая ключи всех интроспекционных запросов сразу: фейк
# отдаёт один и тот же набор на любой `fetch`.
_ANY_CATALOG_ROW = {
    'datname': 'db',
    'schema_name': 'public',
    'name': 't',
    'relkind': 'r',
    'schema': 'public',
    'table': 't',
    'column': None,
    'kind': 'table',
    'column_name': 'id',
    'data_type': 'integer',
    'nullable': False,
    'column_default': None,
    'readable': True,
    'constraint_name': 'fk_t_o',
    'ref_schema': 'public',
    'ref_table': 'o',
    'ref_column': 'id',
    'index_name': 'ix_t_id',
    'is_unique': False,
    'is_key': True,
}


@pytest.mark.parametrize(
    ('call', 'records'),
    [
        (lambda a: a.ping(), [_ANY_CATALOG_ROW]),
        (lambda a: a.server_info(), [_ANY_CATALOG_ROW]),
        (lambda a: a.list_databases(), [_ANY_CATALOG_ROW]),
        (lambda a: a.list_schemas(), [_ANY_CATALOG_ROW]),
        (lambda a: a.list_tables(), [_ANY_CATALOG_ROW]),
        (lambda a: a.describe_table('t'), [_ANY_CATALOG_ROW]),
        (lambda a: a.search_schema('id'), [_ANY_CATALOG_ROW]),
        # Health-checks разбирают свои строки сами, поэтому чужая им не
        # подходит; режим транзакции от набора не зависит.
        (lambda a: a.analyze_health(), []),
    ],
    ids=[
        'ping',
        'server_info',
        'list_databases',
        'list_schemas',
        'list_tables',
        'describe_table',
        'search_schema',
        'analyze_health',
    ],
)
async def test_every_catalog_read_opens_a_readonly_transaction(call, records):
    """SECURITY.md обещает границу безусловно, а не для одного `query`."""
    adapter, conn = _adapter(records)

    await call(adapter)

    assert conn.readonly_flags == [True]


async def test_describe_table_reads_one_catalog_snapshot():
    """Несколько подзапросов — одна транзакция, а не снапшот на каждый.

    Точное их число здесь не проверяется: слияние двух запросов в один
    ничего не ломает, а тест на равенство пятёрке падал бы.
    """
    adapter, conn = _adapter([_ANY_CATALOG_ROW])

    await adapter.describe_table('t')

    assert len(conn.readonly_flags) == 1
    assert len(conn.statements) > 1


async def test_allowed_dml_runs_in_writable_tx():
    adapter, conn = _adapter(read_only=False, allow_dml=True)
    await adapter.execute('INSERT INTO t VALUES (1)')
    assert conn.readonly_flags == [False]


async def test_dml_on_readonly_connection_never_reaches_pool():
    adapter, conn = _adapter(read_only=True)
    with pytest.raises(WriteForbiddenError):
        await adapter.execute('DELETE FROM t')
    assert conn.statements == []


async def test_multi_statement_never_reaches_pool():
    adapter, conn = _adapter()
    with pytest.raises(StatementNotAllowedError):
        await adapter.execute('SELECT 1; DROP TABLE t')
    assert conn.statements == []


async def test_cursor_reads_only_one_row_beyond_limit():
    """Лимит применяется курсором, а не обрезкой вычитанного результата."""
    records = [{'id': i} for i in range(100)]
    adapter, conn = _adapter(records, max_rows=2)

    result = await adapter.execute('SELECT * FROM t')

    assert conn.cursor_requests == [3]
    assert result.rows == [[0], [1]]
    assert result.row_count == 2
    assert result.truncated is True


async def test_limit_above_max_rows_is_capped():
    """Эффективный лимит = min(limit, max_rows) — политику не обойти."""
    records = [{'id': i} for i in range(10)]
    adapter, _ = _adapter(records, max_rows=3)

    result = await adapter.execute('SELECT * FROM t', limit=1000)

    assert result.row_count == 3
    assert result.truncated is True
    # Запрошено 1000, применено 3: выдачу обрезал max_rows.
    assert result.applied_limit == 3


async def test_applied_limit_reports_the_caller_limit_when_it_binds():
    """`applied_limit` == запрошенному: `max_rows` ни при чём."""
    records = [{'id': i} for i in range(10)]
    adapter, _ = _adapter(records, max_rows=1000)

    result = await adapter.execute('SELECT * FROM t', limit=5)

    assert result.truncated is True
    assert result.applied_limit == 5


async def test_applied_limit_is_null_without_a_cap():
    """Отрицательный `max_rows` снимает предел — наружу это `None`."""
    records = [{'id': i} for i in range(10)]
    adapter, _ = _adapter(records, max_rows=-1)

    result = await adapter.execute('SELECT * FROM t')

    assert result.row_count == 10
    assert result.truncated is False
    assert result.applied_limit is None


async def test_explicit_limit_applies_when_max_rows_is_off():
    """Снятый `max_rows` не должен отменять лимит вызывающего."""
    records = [{'id': i} for i in range(10)]
    adapter, _ = _adapter(records, max_rows=-1)

    result = await adapter.execute('SELECT * FROM t', limit=4)

    assert result.row_count == 4
    assert result.truncated is True
    assert result.applied_limit == 4


async def test_non_finite_floats_are_reported_in_notes():
    """`null` вместо NaN неотличим от настоящего NULL без ноты.

    `double precision` хранит и то и другое, а JSON различить их не
    может: литералов `NaN`/`Infinity` в нём нет.
    """
    records = [
        {'x': float('nan'), 'y': 1.0},
        {'x': float('-inf'), 'y': None},
    ]
    adapter, _ = _adapter(records)

    result = await adapter.execute('SELECT x, y FROM t')

    (note,) = result.notes
    assert note.startswith('2 value(s)')
    assert 'not necessarily a SQL NULL' in note


# --- parameter binding -----------------------------------------------------


async def test_params_reach_the_driver_beside_the_statement():
    """Значения обязаны идти мимо текста statement'а.

    Это единственное, что отделяет `query` от инъекции: `$1` в тексте
    без отдельного аргумента разбирает уже не PostgreSQL, а тот, кто
    его туда подставил.
    """
    adapter, conn = _adapter([{'id': 1}])

    await adapter.execute('SELECT * FROM t WHERE a = $1 AND b = $2', [7, 'x'])

    assert conn.statements == ['SELECT * FROM t WHERE a = $1 AND b = $2']
    assert conn.args == [(7, 'x')]


async def test_a_statement_without_params_gets_none():
    adapter, conn = _adapter([{'id': 1}])

    await adapter.execute('SELECT 1')

    assert conn.args == [()]


async def test_a_param_that_looks_like_sql_is_not_spliced_into_the_text():
    """Опасная строка остаётся значением, а не становится синтаксисом.

    Ассерт смотрит на текст statement'а: подстановка выдала бы себя
    именно там, а не в результате, который фейк всё равно выдумывает.
    """
    payload = "'; DROP TABLE customer; --"
    adapter, conn = _adapter([{'id': 1}])

    await adapter.execute('SELECT * FROM t WHERE name = $1', [payload])

    assert payload not in conn.statements[0]
    assert conn.args == [(payload,)]


async def test_explain_passes_params_through_to_the_wrapped_statement():
    """`EXPLAIN` оборачивает текст, но не трогает значения."""
    plan = _explain_plan_row({'Node Type': 'Seq Scan'})
    adapter, conn = _adapter([plan])

    await adapter.explain('SELECT * FROM t WHERE a = $1', [42])

    assert conn.statements == [
        'EXPLAIN (FORMAT JSON) SELECT * FROM t WHERE a = $1'
    ]
    assert conn.args == [(42,)]


# --- the response byte budget -----------------------------------------


def _row_bytes(**values) -> int:
    """Размер одной строки так, как его меряет адаптер."""
    return json_size(list(values.values()))


async def test_the_byte_budget_cuts_the_result_and_says_so():
    """Обрезка по размеру чинится не тем же, чем обрезка по строкам.

    `truncated` без ноты агент прочитает как «строк было больше» и
    повторит запрос с меньшим `limit` — по ширине строки это не
    помогает.
    """
    records = [{'text': 'x' * 100} for _ in range(10)]
    budget = _row_bytes(text='x' * 100) * 3
    adapter, _ = _adapter(records, max_rows=-1, max_bytes=budget)

    result = await adapter.execute('SELECT text FROM t')

    assert result.row_count == 3
    assert result.truncated is True
    assert result.applied_byte_limit == budget
    assert result.applied_limit is None
    (note,) = result.notes
    assert 'cut to fit max_bytes' in note


async def test_the_first_row_survives_a_budget_it_alone_exceeds():
    """Пустой ответ — та же ошибка, что `max_rows=0`.

    Агент прочитал бы его как «данных нет». На `explain_query` это к
    тому же обязательно: план приходит одной строкой.
    """
    adapter, _ = _adapter([{'text': 'x' * 10_000}], max_bytes=10)

    result = await adapter.execute('SELECT text FROM t')

    assert result.row_count == 1
    assert result.truncated is False
    assert result.notes == []


async def test_a_negative_max_bytes_removes_the_budget():
    records = [{'text': 'x' * 1000} for _ in range(5)]
    adapter, _ = _adapter(records, max_rows=-1, max_bytes=-1)

    result = await adapter.execute('SELECT text FROM t')

    assert result.row_count == 5
    assert result.applied_byte_limit is None


async def test_the_row_limit_wins_when_both_would_cut():
    """`truncated` от `max_rows` — не повод советовать сузить колонки."""
    records = [{'id': i} for i in range(100)]
    adapter, _ = _adapter(records, max_rows=2, max_bytes=1_000_000)

    result = await adapter.execute('SELECT id FROM t')

    assert result.row_count == 2
    assert result.truncated is True
    assert result.notes == []


async def test_the_cursor_is_read_in_chunks_when_no_row_limit_bounds_it():
    """Бюджет должен экономить и память процесса, а не только контекст.

    Без `max_rows` одного `fetch` на весь результат хватило бы, чтобы
    материализовать его целиком — то есть ровно то, от чего бюджет и
    защищает.
    """
    records = [{'text': 'x' * 10} for _ in range(250)]
    adapter, conn = _adapter(records, max_rows=-1, max_bytes=10_000_000)

    await adapter.execute('SELECT text FROM t')

    assert conn.cursor_requests == [100, 100, 100]


async def test_the_write_path_respects_the_budget_too():
    """У записи курсора нет, но потолок ответа тот же."""
    records = [{'text': 'x' * 100} for _ in range(10)]
    budget = _row_bytes(text='x' * 100) * 2
    adapter, _ = _adapter(
        records,
        read_only=False,
        max_rows=-1,
        max_bytes=budget,
        allow_dml=True,
    )

    result = await adapter.execute('DELETE FROM t RETURNING text')

    assert result.row_count == 2
    assert result.truncated is True
    assert 'cut to fit max_bytes' in result.notes[0]


async def test_the_write_path_reports_a_row_cut_as_a_row_cut():
    """Совет «сузьте колонки» на обрезку по `max_rows` вводил бы в
    заблуждение, и на пути записи это отдельная ветка."""
    records = [{'id': i} for i in range(10)]
    adapter, _ = _adapter(
        records, read_only=False, max_rows=3, max_bytes=10, allow_dml=True
    )

    result = await adapter.execute('DELETE FROM t RETURNING id')

    assert result.row_count == 3
    assert result.truncated is True
    assert result.notes == []


async def test_a_plan_larger_than_the_budget_still_parses():
    """`explain` читает `rows[0][0]`; обрезать его значит сломать JSON."""
    plan = {'Node Type': 'Seq Scan', 'Relation Name': 'x' * 5000}
    adapter, _ = _adapter([_explain_plan_row(plan)], max_bytes=10)

    result = await adapter.explain('SELECT * FROM t')

    assert result.plan == plan


async def test_non_finite_floats_are_counted_inside_containers():
    """`float8[]` и composite несут float, до которых нота не доходила.

    Сериализатор спускается в списки и словари и подменяет `NaN` на
    `null` там же, поэтому счётчик обязан спускаться вместе с ним:
    иначе колонка-массив отдаёт `null` без ноты, а это ровно тот
    неотличимый от SQL NULL случай, ради которого нота и написана.
    """
    records = [
        {'arr': [float('nan'), 1.0, float('inf')], 'rec': {'v': float('nan')}},
        {'arr': [[float('-inf')]], 'rec': None},
    ]
    adapter, _ = _adapter(records)

    result = await adapter.execute('SELECT arr, rec FROM t')

    (note,) = result.notes
    assert note.startswith('4 value(s)')


async def test_bytes_are_not_walked_as_containers():
    """`memoryview` итерируется целыми; спуск внутрь дал бы ложный счёт.

    `_serialize` переводит bytes-подобные в hex раньше, чем дошёл бы
    до элементов, и счётчик обязан вести себя так же.
    """
    adapter, _ = _adapter([{'blob': memoryview(b'\xff\x00'), 'b': b'\x01'}])

    assert (await adapter.execute('SELECT blob, b FROM t')).notes == []


async def test_ordinary_results_carry_no_notes():
    adapter, _ = _adapter([{'x': 1.5}])

    assert (await adapter.execute('SELECT x FROM t')).notes == []


@pytest.mark.parametrize(
    'exc',
    [
        # Клиентский command_timeout asyncpg.
        TimeoutError(),
        # Серверный statement_timeout. Именно он срабатывает первым:
        # command_timeout выставлен заведомо позже.
        asyncpg.QueryCanceledError(),
        asyncpg.IdleInTransactionSessionTimeoutError(),
    ],
    ids=['command_timeout', 'statement_timeout', 'idle_in_transaction'],
)
async def test_every_timeout_becomes_a_domain_error(exc):
    """Сырое исключение драйвера не должно доходить до MCP-слоя.

    Тот ловит только `DatabaseMCPError`, поэтому всё остальное уходит в
    контекст модели как есть.
    """
    adapter, conn = _adapter([{'id': 1}])

    async def raise_timeout(statement, *args):
        raise exc

    conn.cursor = raise_timeout

    with pytest.raises(QueryTimeoutError) as exc_info:
        await adapter.execute('SELECT 1')

    assert 'query_timeout' in str(exc_info.value)
    assert exc_info.value.__cause__ is exc


async def test_explain_wraps_statement_and_runs_readonly():
    plan = {
        'Node Type': 'Seq Scan',
        'Relation Name': 'orders',
        'Total Cost': 123.4,
    }
    adapter, conn = _adapter([_explain_plan_row(plan)])

    result = await adapter.explain('SELECT * FROM orders')

    assert conn.readonly_flags == [True]
    assert 'EXPLAIN (FORMAT JSON) SELECT * FROM orders' in conn.statements
    assert result.plan == plan
    # Без ANALYZE замеров времени нет — только оценки планировщика.
    assert result.planning_time_ms is None
    assert result.execution_time_ms is None
    assert any('Seq Scan' in w for w in result.warnings)


async def test_explain_analyze_adds_analyze_buffers_and_flags_mismatch():
    plan = {
        'Node Type': 'Index Scan',
        'Relation Name': 'orders',
        'Total Cost': 1.0,
        'Plan Rows': 10,
        'Actual Rows': 500,
    }
    adapter, conn = _adapter(
        [_explain_plan_row(plan, planning_time=0.3, execution_time=42.0)]
    )

    result = await adapter.explain('SELECT * FROM orders', analyze=True)

    assert (
        'EXPLAIN (FORMAT JSON, ANALYZE, BUFFERS) SELECT * FROM orders'
        in conn.statements
    )
    assert result.planning_time_ms == 0.3
    assert result.execution_time_ms == 42.0
    assert any('Row estimate is off' in w for w in result.warnings)


async def test_explain_analyze_ignores_small_row_mismatch_noise():
    """Мелкое расхождение оценки в warnings не попадает.

    План=1/факт=0 — нормальное поведение точечного поиска по индексу на
    несуществующее значение. Без нижнего порога по абсолютному числу
    строк эвристика срабатывала бы почти на любом селективном lookup.
    """
    plan = {
        'Node Type': 'Index Scan',
        'Relation Name': 'pg_class',
        'Total Cost': 8.29,
        'Plan Rows': 1,
        'Actual Rows': 0,
    }
    adapter, _ = _adapter([_explain_plan_row(plan, execution_time=0.044)])

    result = await adapter.explain('SELECT 1', analyze=True)

    assert result.warnings == []


async def test_explain_analyze_flags_zero_plan_rows_mismatch():
    """Нулевая оценка при значимом факте попадает в warnings.

    План=0 — худший вид недооценки, и он должен отмечаться наравне с
    расхождением в другую сторону.
    """
    plan = {
        'Node Type': 'Seq Scan',
        'Relation Name': 'orders',
        'Total Cost': 1.0,
        'Plan Rows': 0,
        'Actual Rows': 500,
    }
    adapter, _ = _adapter([_explain_plan_row(plan, execution_time=5.0)])

    result = await adapter.explain('SELECT * FROM orders', analyze=True)

    assert any('Row estimate is off' in w for w in result.warnings)


async def test_explain_analyze_refuses_a_write_before_the_pool():
    """`EXPLAIN ANALYZE INSERT` отклоняется до похода в БД.

    READ ONLY транзакция отбила бы его и так, но сообщением драйвера
    про read-only transaction, которого агент не писал. Подключение
    доступно на запись: отказ описывает возможности инструмента, а не
    политику.
    """
    adapter, conn = _adapter(read_only=False, allow_dml=True)

    with pytest.raises(StatementNotAllowedError):
        await adapter.explain('INSERT INTO t VALUES (1)', analyze=True)

    assert conn.statements == []


async def test_explain_without_analyze_plans_a_write():
    """Без `ANALYZE` statement не исполняется — план нужен и для DML."""
    plan = {'Node Type': 'Insert', 'Total Cost': 0.0}
    adapter, conn = _adapter([_explain_plan_row(plan)])

    result = await adapter.explain('INSERT INTO t VALUES (1)')

    assert conn.readonly_flags == [True]
    assert 'EXPLAIN (FORMAT JSON) INSERT INTO t VALUES (1)' in conn.statements
    assert result.plan == plan


async def test_explain_flags_an_over_estimate_too():
    """Планировщик ошибается в обе стороны.

    Завышенная оценка уводит в лишний seq scan так же надёжно, как
    заниженная — в nested loop по миллиону строк.
    """
    plan = {
        'Node Type': 'Bitmap Heap Scan',
        'Relation Name': 'orders',
        'Total Cost': 100.0,
        'Plan Rows': 5000,
        'Actual Rows': 120,
    }
    adapter, _ = _adapter([_explain_plan_row(plan, execution_time=1.0)])

    result = await adapter.explain('SELECT 1', analyze=True)

    assert any('planned=5000, actual=120' in w for w in result.warnings)


async def test_explain_without_a_plan_is_a_domain_error():
    """Пустой ответ `EXPLAIN` наружу как IndexError идти не должен."""
    adapter, _ = _adapter([])

    with pytest.raises(ExplainError):
        await adapter.explain('SELECT 1')


@pytest.mark.parametrize(
    ('error', 'expected_code'),
    [
        (
            asyncpg.ConnectionDoesNotExistError(LOST_CONNECTION_MESSAGE),
            'unreachable',
        ),
        (
            asyncpg.InterfaceError(LOST_CONNECTION_MESSAGE),
            'connection_failed',
        ),
    ],
    ids=['postgres_connection', 'interface'],
)
async def test_a_lost_connection_becomes_a_code_not_a_report(
    error, expected_code
):
    """Одиннадцать «проверка не выполнилась» скрывали бы причину."""
    adapter, conn = _adapter()

    async def _raise(statement, *args):
        raise error

    conn.fetch = _raise

    with pytest.raises(AdapterConnectionError) as exc_info:
        await adapter.analyze_health()

    assert exc_info.value.code.value == expected_code
    # Текст драйвера остаётся в логе наравне с ошибками подключения.
    assert LOST_CONNECTION_MESSAGE not in str(exc_info.value)


async def test_analyze_health_runs_in_readonly_transaction():
    adapter, conn = _adapter()
    results = await adapter.analyze_health()
    assert conn.readonly_flags == [True]
    assert len(results) > 0
    assert {r.id for r in results} >= {
        'connection_utilization',
        'buffer_cache_hit_rate',
    }


# --- pool lifecycle --------------------------------------------------------


async def test_using_the_adapter_before_connect_is_a_domain_error():
    """Иначе наружу уедет `AttributeError: 'NoneType'`."""
    adapter = PostgresAdapter(ConnectionConfig(id='web-shop'))

    with pytest.raises(AdapterConnectionError) as exc_info:
        await adapter.ping()

    assert exc_info.value.code.value == 'connection_failed'


async def test_connect_is_idempotent(monkeypatch):
    """`get_adapter` берёт лок, но `connect` зовут и напрямую."""
    created: list[int] = []

    async def _pool(**kwargs):
        created.append(1)
        return object()

    monkeypatch.setattr(asyncpg, 'create_pool', _pool)
    adapter = PostgresAdapter(
        ConnectionConfig(id='a', host='h', user='u', sslmode='require')
    )

    await adapter.connect()
    await adapter.connect()

    assert created == [1]


async def test_close_releases_the_pool_and_can_be_repeated():
    """`close_all` вызывается в `finally`, в том числе после сбоя."""
    closed: list[int] = []

    class ClosablePool(FakePool):
        async def close(self):
            closed.append(1)

    adapter, _ = _adapter()
    adapter._pool = ClosablePool(None)

    await adapter.close()
    await adapter.close()

    assert closed == [1]
    assert adapter._pool is None


@pytest.mark.parametrize(
    ('reltuples', 'expected'),
    [(1234.0, 1234), (0.0, 0), (-1.0, None), (None, None)],
    ids=['rows', 'empty', 'never_analyzed', 'missing'],
)
def test_row_estimate_distinguishes_zero_from_unknown(reltuples, expected):
    """`-1` в `reltuples` — «не анализировали», а не «нет строк»."""
    assert PostgresAdapter._row_estimate(reltuples) == expected


# --- connection error sanitising -------------------------------------------
#
# Сообщения asyncpg содержат host, port, имя роли и имя базы. Наружу, в
# контекст модели, должен идти только стабильный код.


@pytest.mark.parametrize(
    ('exc', 'code'),
    [
        (
            asyncpg.InvalidPasswordError(
                'password authentication failed for user "readonly_prod"'
            ),
            'auth_failed',
        ),
        (
            asyncpg.InvalidCatalogNameError(
                'database "billing_secret" does not exist'
            ),
            'database_not_found',
        ),
        (
            ConnectionRefusedError(
                61, "Connect call failed ('10.0.3.17', 5432)"
            ),
            'unreachable',
        ),
        (socket.gaierror(8, 'nodename nor servname provided'), 'unreachable'),
        (ssl.SSLError('certificate verify failed'), 'tls_failed'),
        (TimeoutError('timed out'), 'timeout'),
        (RuntimeError('something else'), 'connection_failed'),
    ],
)
def test_connection_errors_map_to_stable_codes(exc, code):
    assert classify_connection_error(exc).value == code


SECRETS = ['readonly_prod', 'billing_secret', '10.0.3.17', '5432', 'nodename']


@pytest.mark.parametrize(
    'exc',
    [
        asyncpg.InvalidPasswordError(
            'password authentication failed for user "readonly_prod"'
        ),
        asyncpg.InvalidCatalogNameError(
            'database "billing_secret" does not exist'
        ),
        ConnectionRefusedError(61, "Connect call failed ('10.0.3.17', 5432)"),
    ],
)
async def test_connect_failure_never_leaks_infrastructure(exc, monkeypatch):
    async def _boom(**kwargs):
        raise exc

    monkeypatch.setattr(asyncpg, 'create_pool', _boom)
    adapter = PostgresAdapter(ConnectionConfig(id='web-shop'))

    with pytest.raises(AdapterConnectionError) as exc_info:
        await adapter.connect()

    rendered = str(exc_info.value)
    assert 'web-shop' in rendered
    assert not any(secret in rendered for secret in SECRETS)
    # Цепочка исключений тоже не должна тянуть текст драйвера наружу.
    assert exc_info.value.__cause__ is None


async def test_acquire_failure_is_translated_to_a_code():
    """Ошибка при захвате соединения тоже становится кодом.

    Пул может открыть новое физическое соединение и получить
    креды-ошибку уже после успешного старта, поэтому захват оборачивается
    наравне с подключением.
    """
    adapter, _ = _adapter()
    adapter._pool = FakePool(
        None,
        acquire_error=asyncpg.InvalidPasswordError(
            'password authentication failed for user "readonly_prod"'
        ),
    )

    with pytest.raises(AdapterConnectionError) as exc_info:
        await adapter.ping()

    assert exc_info.value.code.value == 'auth_failed'
    assert 'readonly_prod' not in str(exc_info.value)


async def test_connection_is_released_back_to_the_pool():
    adapter, _ = _adapter([{'id': 1}])
    await adapter.execute('SELECT 1')
    assert adapter._pool.released == 1


async def test_waiting_for_a_pool_slot_is_bounded():
    """`statement_timeout` считается с начала запроса, не с очереди.

    Без явного таймаута `acquire()` ждёт бесконечно, и при исчерпанном
    пуле вызов зависал бы, не дойдя ни до одного лимита.
    """
    adapter, _ = _adapter([{'id': 1}])

    await adapter.execute('SELECT 1')

    assert adapter._pool.acquire_timeouts == [35.0]


async def test_a_pool_slot_timeout_becomes_a_connection_code():
    adapter, _ = _adapter()
    adapter._pool = FakePool(None, acquire_error=TimeoutError())

    with pytest.raises(AdapterConnectionError) as exc_info:
        await adapter.ping()

    assert exc_info.value.code.value == 'timeout'


# --- server-side guards ----------------------------------------------------


def test_read_only_connection_sets_server_side_guards():
    """Лимит и read-only держит сервер, а не клиент.

    `command_timeout` клиентский: он лишь шлёт cancel.
    """
    adapter, _ = _adapter(read_only=True)
    settings = adapter._server_settings()

    assert settings['statement_timeout'] == '30000'
    assert settings['default_transaction_read_only'] == 'on'
    assert settings['application_name'] == 'pgsteward'
    # Клиентский таймаут заведомо больше, чтобы первым сработал сервер.
    assert adapter._connect_kwargs()['command_timeout'] > 30


def test_writable_connection_does_not_force_read_only_transactions():
    adapter, _ = _adapter(read_only=False, allow_dml=True)
    assert 'default_transaction_read_only' not in adapter._server_settings()


@pytest.mark.parametrize('read_only', [True, False])
def test_standard_conforming_strings_is_pinned(read_only):
    """Условие корректности маскировки литералов в security.py.

    Настройка не зависит от того, разрешена ли запись: барьер один и
    тот же.
    """
    adapter, _ = _adapter(read_only=read_only, allow_dml=not read_only)
    assert adapter._server_settings()['standard_conforming_strings'] == 'on'


# Предупреждение об отсутствии TLS — аудит конфигурации, а не адаптера:
# см. tests/test_config.py и tests/test_connections.py.


def test_pool_bounds_and_tls_reach_the_driver():
    """Границы пула и `sslmode` едут в asyncpg, а не оседают в конфиге.

    `create_pool` в тестах подменён, поэтому опечатка в имени ключа
    видна только здесь: настоящий вызов просто взял бы дефолт, и пул
    молча работал бы не с теми границами, а соединение — без TLS.
    Работоспособность самих значений подтверждает интеграционный тест.
    """
    config = ConnectionConfig(
        id='a',
        host='h',
        user='u',
        database='d',
        sslmode='require',
        pool={'min': 2, 'max': 7},
        query_timeout=30,
    )

    kwargs = PostgresAdapter(config)._connect_kwargs()

    assert kwargs['min_size'] == 2
    assert kwargs['max_size'] == 7
    assert kwargs['ssl'] == 'require'
    assert kwargs['command_timeout'] == 35
    assert kwargs['dsn'].startswith('postgresql://u@h:5432/d')


def test_a_connection_without_sslmode_passes_no_ssl_argument():
    """Ключа быть не должно вовсе.

    `ssl=None` для asyncpg — не «как получится», а явное значение, и
    подставлять его вместо отсутствия ключа значит менять поведение
    драйвера по умолчанию.
    """
    config = ConnectionConfig(id='a', host='h', user='u', database='d')

    assert 'ssl' not in PostgresAdapter(config)._connect_kwargs()


def test_a_dsn_connection_passes_the_dsn_verbatim():
    config = ConnectionConfig(
        id='a', dsn=SecretStr('postgresql://u:p@h:5433/d?sslmode=require')
    )

    kwargs = PostgresAdapter(config)._connect_kwargs()

    assert kwargs['dsn'] == 'postgresql://u:p@h:5433/d?sslmode=require'


# --- search_schema ---------------------------------------------------------


async def test_search_schema_reports_truncation():
    """Без флага агент не отличит «это все совпадения» от «первые N»."""
    rows = [
        {'schema': 'public', 'table': f't{i}', 'column': None, 'kind': 'table'}
        for i in range(500)
    ]
    adapter, _ = _adapter(rows)

    result = await adapter.search_schema('t')

    assert result.truncated is True
    assert result.match_count == result.limit == 200
    assert len(result.matches) == 200
    assert result.notes and 'first 200' in result.notes[0]


async def test_search_schema_without_truncation_has_no_notes():
    rows = [
        {'schema': 'public', 'table': 't1', 'column': None, 'kind': 'table'}
    ]
    adapter, _ = _adapter(rows)

    result = await adapter.search_schema('t')

    assert result.truncated is False
    assert result.match_count == 1
    assert result.notes == []


# --- contextual notes in the response --------------------------------------


def test_notes_appear_only_when_the_fact_is_relevant():
    """Пояснения едут в ответе, а не в описании tool'а.

    И только тогда, когда в таблице действительно есть соответствующая
    конструкция.
    """
    plain = _table_notes(
        indexes=[IndexInfo(name='i', columns=['a'])],
        foreign_keys=[],
        row_estimate=None,
    )
    assert plain == []

    hidden = _table_notes(
        indexes=[IndexInfo(name='i', columns=['a'])],
        foreign_keys=[],
        row_estimate=None,
        hidden_columns=2,
    )
    assert len(hidden) == 1
    assert 'SELECT *' in hidden[0]

    rich = _table_notes(
        indexes=[
            IndexInfo(name='i', columns=['a'], included_columns=['b']),
        ],
        foreign_keys=[
            ForeignKeyInfo(
                column='a',
                references_table='t',
                references_column='x',
                constraint_name='fk',
            ),
            ForeignKeyInfo(
                column='b',
                references_table='t',
                references_column='y',
                constraint_name='fk',
            ),
        ],
        row_estimate=50,
    )
    assert len(rich) == 3
    assert any('INCLUDE' in n for n in rich)
    assert any('composite foreign key' in n for n in rich)
    assert any('reltuples' in n for n in rich)


@pytest.mark.parametrize(
    ('noun', 'expected'),
    [
        ('index', 'an index'),
        ('sequence', 'a sequence'),
        ('composite type', 'a composite type'),
        ('partitioned index', 'a partitioned index'),
        ('TOAST table', 'a TOAST table'),
        ('relkind=x', 'a relkind=x'),
    ],
)
def test_relkind_name_gets_the_right_article(noun, expected):
    """Сообщение читает модель, и «a index» в нём — брак склейки."""
    assert _with_article(noun) == expected


def test_every_known_relkind_name_gets_an_article():
    """Словарь может пополниться; правило должно работать и тогда."""
    for name in _RELKIND_NAMES.values():
        assert _with_article(name).split()[0] in {'a', 'an'}


@pytest.mark.parametrize(
    ('hidden', 'expected'),
    [
        (
            1,
            '1 column exists on this relation but is not listed: the '
            'connection role has no SELECT privilege on it.',
        ),
        (
            3,
            '3 columns exist on this relation but are not listed: the '
            'connection role has no SELECT privilege on them.',
        ),
    ],
)
def test_hidden_column_note_agrees_in_number(hidden, expected):
    """Нота читается моделью, поэтому согласование не косметика.

    Сверяется предложение целиком: проверка по префиксу пропускала
    рассогласованное «1 column ... privilege on them».
    """
    notes = _table_notes(
        indexes=[],
        foreign_keys=[],
        row_estimate=None,
        hidden_columns=hidden,
    )

    assert notes[0].startswith(expected)


def test_hidden_column_note_does_not_promise_a_list_that_may_be_empty():
    """Когда закрыты все колонки, перечислять выше нечего.

    Совет «name the columns above» в этом случае толкает модель
    выдумать имена, поэтому формулировка обязана оставаться верной и
    при пустом списке.
    """
    notes = _table_notes(
        indexes=[], foreign_keys=[], row_estimate=None, hidden_columns=4
    )

    assert 'only the columns listed above can be read' in notes[0]


async def test_fetch_columns_counts_the_ones_without_a_grant():
    """Скрытая колонка не просто пропадает из выдачи — она считается."""
    rows = [
        {
            'column_name': 'id',
            'data_type': 'integer',
            'nullable': False,
            'column_default': None,
            'readable': True,
        },
        {
            'column_name': 'last_name',
            'data_type': 'text',
            'nullable': True,
            'column_default': None,
            'readable': False,
        },
    ]
    adapter, conn = _adapter(rows)

    columns, hidden = await adapter._fetch_columns(conn, 42)

    assert [c.name for c in columns] == ['id']
    assert hidden == 1
