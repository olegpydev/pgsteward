"""Проверка MCP-слоя: аудит вызовов и перевод доменных ошибок.

Адаптер подменён фейком — интересует не SQL, а то, что попадает в
журнал и что уходит наружу вместо исключения.
"""

import json
import logging

import pytest

import pgsteward.dependencies as dependencies
import pgsteward.server as server
from pgsteward.config import (
    CONNECTIONS_ENV,
    ENV_PREFIX,
    AppConfig,
    ConnectionConfig,
    SecurityPolicy,
)
from pgsteward.errors import (
    ConnectionMisconfiguredError,
    WriteForbiddenError,
)
from pgsteward.models import (
    ConnectionStatus,
    HealthCheckResult,
    QueryResult,
    SchemaSearchResult,
    ServerInfo,
    TableInfo,
    TableSchema,
)

SECRET_SQL = "SELECT * FROM patients WHERE ssn = $1 AND name = 'Ivanov'"
SECRET_PARAM = '123-45-6789'


class FakeAdapter:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    async def execute(self, statement, params=None, limit=None):
        if self._error is not None:
            raise self._error
        return self._result

    async def ping(self):
        return True


def _query_result(**overrides):
    fields = {
        'columns': ['id'],
        'rows': [[1], [2]],
        'row_count': 2,
        'kind': 'read',
        'truncated': True,
        'execution_ms': 4.2,
    }
    fields.update(overrides)
    return QueryResult(**fields)


@pytest.fixture
def adapter(monkeypatch):
    """Подменить `_adapter`; вернуть сеттер используемого фейка."""
    holder: dict[str, FakeAdapter] = {}

    async def _fake_adapter(connection_id):
        return holder['adapter']

    monkeypatch.setattr(server, '_adapter', _fake_adapter)

    def use(**kwargs):
        holder['adapter'] = FakeAdapter(**kwargs)

    return use


async def test_successful_query_is_audited(adapter, audit):
    adapter(result=_query_result())

    raw = await server.query.fn(
        connection_id='web-shop',
        statement=SECRET_SQL,
        params=[SECRET_PARAM],
    )

    assert json.loads(raw)['row_count'] == 2

    (line,) = audit()
    assert 'tool=query' in line
    assert 'connection_id=web-shop' in line
    assert 'outcome=ok' in line
    assert 'kind=read' in line
    assert 'row_count=2' in line
    assert 'truncated=True' in line


async def test_audit_never_records_sql_or_parameter_values(adapter, audit):
    """Журнал с SQL и связанными значениями — копия самих данных.

    Логи уезжают туда, где защиты меньше, чем у базы.
    """
    adapter(result=_query_result())

    await server.query.fn(
        connection_id='web-shop',
        statement=SECRET_SQL,
        params=[SECRET_PARAM],
    )

    (line,) = audit()
    assert SECRET_PARAM not in line
    assert 'patients' not in line
    assert 'Ivanov' not in line
    assert 'ssn' not in line


async def test_refused_write_is_audited_as_an_error(adapter, audit):
    """Отклонённая попытка записи интересна аудиту не меньше чтения.

    Пока лог писался после успешного вызова, в него не попадала ни одна
    неудача — ни отказ политики, ни ошибка базы.
    """
    adapter(error=WriteForbiddenError('This connection is read-only.'))

    with pytest.raises(ValueError, match='read-only'):
        await server.query.fn(
            connection_id='web-shop', statement='DELETE FROM t'
        )

    (line,) = audit()
    assert 'outcome=error' in line
    assert 'error=WriteForbiddenError' in line
    assert 'connection_id=web-shop' in line


async def test_the_query_tool_refuses_a_zero_limit(adapter, audit):
    """`limit=0` отвергается наравне с `max_rows=0`.

    Отказ приходит до адаптера: выполнять запрос, результат которого
    заведомо будет обрезан до нуля строк, незачем.
    """
    # Любое обращение к адаптеру провалит тест другим исключением.
    adapter(error=AssertionError('до базы дело доходить не должно'))

    with pytest.raises(ValueError, match='0 would return no rows'):
        await server.query.fn(
            connection_id='web-shop', statement='SELECT 1', limit=0
        )

    (line,) = audit()
    assert 'outcome=error' in line


async def test_domain_errors_lose_their_cause(adapter, audit):
    """`__cause__` донёс бы до модели исходное сообщение драйвера."""
    adapter(error=ConnectionMisconfiguredError('crm', ('CRM_PG_HOST',)))

    with pytest.raises(ValueError) as exc_info:
        await server.query.fn(connection_id='crm', statement='SELECT 1')

    assert exc_info.value.__cause__ is None
    # Имена переменных окружения, наоборот, обязаны дойти.
    assert 'CRM_PG_HOST' in str(exc_info.value)


async def test_unexpected_errors_are_audited_and_propagate(adapter, audit):
    """Недоменное исключение не должно проходить мимо журнала."""
    adapter(error=RuntimeError('boom'))

    with pytest.raises(RuntimeError):
        await server.query.fn(connection_id='crm', statement='SELECT 1')

    (line,) = audit()
    assert 'outcome=error' in line
    assert 'error=RuntimeError' in line


async def test_explain_query_is_audited_too(adapter, audit, monkeypatch):
    """`analyze=true` физически выполняет statement, как и `query`."""

    class ExplainingAdapter(FakeAdapter):
        async def explain(self, statement, params=None, analyze=False):
            return {'plan': {}}

    async def _fake_adapter(connection_id):
        return ExplainingAdapter()

    monkeypatch.setattr(server, '_adapter', _fake_adapter)

    await server.explain_query.fn(
        connection_id='web-shop', statement=SECRET_SQL, analyze=True
    )

    (line,) = audit()
    assert 'tool=explain_query' in line
    assert 'connection_id=web-shop' in line
    assert 'outcome=ok' in line
    assert 'patients' not in line


# --- the rest of the tool surface ------------------------------------------
#
# Каждая обёртка тонкая, но именно в ней живёт всё, что видит модель:
# форма ответа, обязательные параметры и строка аудита. Без теста
# опечатка в имени поля ушла бы в контекст молча.


class IntrospectingAdapter:
    """Адаптер, возвращающий узнаваемое значение на каждый метод."""

    def __init__(self):
        self.calls: list[tuple] = []

    def _record(self, name, *args):
        self.calls.append((name, *args))

    async def ping(self):
        self._record('ping')
        return True

    async def server_info(self):
        self._record('server_info')
        return ServerInfo(version='PostgreSQL 17.2', extra={'database': 'db'})

    async def list_databases(self):
        self._record('list_databases')
        return ['web_shop']

    async def list_schemas(self):
        self._record('list_schemas')
        return ['public']

    async def list_tables(self, schema=None):
        self._record('list_tables', schema)
        return [TableInfo(name='customer', schema='public')]

    async def describe_table(self, table, schema=None):
        self._record('describe_table', table, schema)
        return TableSchema(name=table, schema=schema or 'public', columns=[])

    async def search_schema(self, pattern, schema=None):
        self._record('search_schema', pattern, schema)
        return SchemaSearchResult(matches=[], match_count=0)

    async def analyze_health(self):
        self._record('analyze_health')
        return [
            HealthCheckResult(id='invalid_indexes', status='ok', message='—'),
            HealthCheckResult(
                id='replication_lag', status='warning', message='—'
            ),
        ]


@pytest.fixture
def introspecting(monkeypatch):
    fake = IntrospectingAdapter()

    async def _fake_adapter(connection_id):
        return fake

    monkeypatch.setattr(server, '_adapter', _fake_adapter)
    return fake


async def test_ping_reports_the_connection_it_probed(introspecting):
    """Без `connection_id` в ответе `alive` не к чему отнести."""
    payload = json.loads(await server.ping.fn(connection_id='web-shop'))

    assert payload == {'connection_id': 'web-shop', 'alive': True}


async def test_get_server_info_passes_the_adapter_answer_through(
    introspecting,
):
    payload = json.loads(
        await server.get_server_info.fn(connection_id='web-shop')
    )

    assert payload['version'] == 'PostgreSQL 17.2'
    assert payload['extra'] == {'database': 'db'}


@pytest.mark.parametrize(
    ('call', 'expected'),
    [
        (lambda: server.list_databases.fn(connection_id='c'), ['web_shop']),
        (lambda: server.list_schemas.fn(connection_id='c'), ['public']),
    ],
    ids=['list_databases', 'list_schemas'],
)
async def test_plain_list_tools_return_json_arrays(
    introspecting, call, expected
):
    assert json.loads(await call()) == expected


async def test_list_tables_defaults_to_no_schema(introspecting):
    """Дефолт подставляет адаптер, а не MCP-слой: `public` — его знание."""
    payload = json.loads(await server.list_tables.fn(connection_id='c'))

    assert introspecting.calls == [('list_tables', None)]
    assert payload[0]['schema'] == 'public'


async def test_describe_table_forwards_the_schema(introspecting):
    await server.describe_table.fn(
        connection_id='c', table='customer', schema='sales'
    )

    assert introspecting.calls == [('describe_table', 'customer', 'sales')]


async def test_search_schema_forwards_the_schema(introspecting):
    await server.search_schema.fn(
        connection_id='c', pattern='email', schema='sales'
    )

    assert introspecting.calls == [('search_schema', 'email', 'sales')]


@pytest.mark.parametrize(
    ('call', 'parameter'),
    [
        (
            lambda: server.describe_table.fn(connection_id='c', table=' '),
            'table',
        ),
        (
            lambda: server.search_schema.fn(connection_id='c', pattern=''),
            'pattern',
        ),
        (
            lambda: server.query.fn(connection_id='c', statement='   '),
            'statement',
        ),
        (
            lambda: server.explain_query.fn(connection_id='c', statement=''),
            'statement',
        ),
    ],
    ids=['table', 'pattern', 'statement', 'explain_statement'],
)
async def test_a_blank_required_parameter_is_refused(
    introspecting, call, parameter
):
    """Пустая строка иначе доехала бы до SQL как валидное значение."""
    with pytest.raises(
        ValueError, match=f'Parameter "{parameter}" is required'
    ):
        await call()

    assert introspecting.calls == []


@pytest.mark.parametrize('probe', [False, True], ids=['no_probe', 'probe'])
async def test_list_connections_forwards_probe(monkeypatch, probe):
    """Значение `probe` решает, будет ли сетевое обращение вообще."""
    seen: list[bool] = []

    class FakeRegistry:
        async def list_connections(self, probe):
            seen.append(probe)
            return [
                ConnectionStatus(id='web-shop', read_only=True, alive=probe)
            ]

    monkeypatch.setattr(server, 'get_registry', lambda: FakeRegistry())

    payload = json.loads(await server.list_connections.fn(probe=probe))

    assert seen == [probe]
    assert payload[0]['id'] == 'web-shop'


async def test_list_connections_does_not_probe_by_default(monkeypatch):
    """Справочный вызов открывал бы пул к каждой базе, включая прод."""
    seen: list[bool] = []

    class FakeRegistry:
        async def list_connections(self, probe):
            seen.append(probe)
            return []

    monkeypatch.setattr(server, 'get_registry', lambda: FakeRegistry())

    assert json.loads(await server.list_connections.fn()) == []
    assert seen == [False]


async def test_analyze_db_health_summarises_the_checks(introspecting):
    """Сводка считается здесь, а не в адаптере: это решение отчёта."""
    payload = json.loads(
        await server.analyze_db_health.fn(connection_id='web-shop')
    )

    assert payload['connection_id'] == 'web-shop'
    assert payload['overall_status'] == 'warning'
    assert [c['id'] for c in payload['checks']] == [
        'invalid_indexes',
        'replication_lag',
    ]


async def test_get_security_config_answers_without_touching_the_database(
    monkeypatch,
):
    """Агент должен уметь спросить политику до первой попытки записи."""

    async def _must_not_connect(connection_id):
        raise AssertionError('get_security_config must not open a connection')

    monkeypatch.setattr(server, '_adapter', _must_not_connect)
    monkeypatch.setattr(
        server,
        '_config',
        lambda connection_id: ConnectionConfig(
            id=connection_id, read_only=True, max_rows=500, query_timeout=15
        ),
    )
    monkeypatch.setattr(
        server, 'get_app_config', lambda: AppConfig(security=SecurityPolicy())
    )

    payload = json.loads(
        await server.get_security_config.fn(connection_id='web-shop')
    )

    assert payload == {
        'read_only': True,
        'allow_dml': False,
        'allow_ddl': False,
        'max_rows': 500,
        'max_bytes': 100_000,
        'query_timeout': 15,
    }


# --- lifespan and entry point ----------------------------------------------


async def test_lifespan_registers_extensions_and_closes_the_registry(
    monkeypatch,
):
    """Точка сборки сервера: что здесь не вызвано, того в сессии нет.

    Расширения регистрируются до `yield` — иначе первый же `tools/list`
    от клиента уйдёт без них.
    """
    events: list[str] = []

    monkeypatch.setenv(CONNECTIONS_ENV, '')
    monkeypatch.setattr(
        server, 'register_all', lambda mcp: events.append('registered') or {}
    )

    async def _close():
        events.append('closed')

    monkeypatch.setattr(server, 'close_registry', _close)

    async with server._lifespan(server.mcp):
        assert events == ['registered']
        assert dependencies._registry is not None

    assert events == ['registered', 'closed']


async def test_lifespan_closes_the_registry_even_if_startup_fails(
    monkeypatch,
):
    """Иначе неудачный старт оставляет открытые пулы висеть."""
    closed: list[int] = []

    async def _close():
        closed.append(1)

    def _boom(mcp):
        raise RuntimeError('extension exploded')

    monkeypatch.setenv(CONNECTIONS_ENV, '')
    monkeypatch.setattr(server, 'register_all', _boom)
    monkeypatch.setattr(server, 'close_registry', _close)

    with pytest.raises(RuntimeError, match='extension exploded'):
        async with server._lifespan(server.mcp):
            pass

    assert closed == [1]


def test_main_configures_logging_before_running(monkeypatch):
    """`mcp.run` перехватывает stdio: после него настраивать поздно.

    Вызовов `configure_logging` два. Первый ставит запасной уровень
    до чтения настроек: `PGSTEWARD_LOG_LEVEL` отвергает сам `Settings`,
    и сообщить об этом нужно уже настроенным логгером.
    """
    order: list[str] = []

    monkeypatch.setattr(
        server, 'configure_logging', lambda level: order.append(f'log:{level}')
    )
    monkeypatch.setattr(
        server.mcp,
        'run',
        lambda **kwargs: order.append(f'run:{kwargs}'),
    )
    monkeypatch.setenv(f'{ENV_PREFIX}LOG_LEVEL', 'debug')

    server.main()

    # Баннер выключен: он ушёл бы в stdout, где живёт JSON-RPC.
    assert order == [
        f'log:{server._FALLBACK_LOG_LEVEL}',
        'log:DEBUG',
        "run:{'show_banner': False}",
    ]


@pytest.mark.parametrize(
    ('env', 'expected'),
    [
        ({f'{ENV_PREFIX}LOG_LEVEL': 'verbose'}, 'must be one of'),
        ({f'{ENV_PREFIX}DB_CONNECTIONS': 'a:b:c'}, 'format is id:prefix'),
        ({f'{ENV_PREFIX}DB_CONNECTIONS': 'a,a'}, 'Duplicate connection id'),
    ],
    ids=['log_level', 'malformed_csv', 'duplicate_id'],
)
def test_unusable_configuration_exits_without_a_traceback(
    monkeypatch, caplog, env, expected
):
    """Оператор читает stderr, а не двадцать кадров чужого стека.

    Отказ конфигурации раньше выходил из `_lifespan` через anyio и
    FastMCP, и найти сообщение в traceback мог не всякий.
    """
    started: list[str] = []
    monkeypatch.setattr(server.mcp, 'run', lambda **_: started.append('run'))
    # Настоящая `configure_logging` снимает propagate, и запись не
    # доходит до caplog. Здесь проверяется содержание сообщения, а не
    # настройка логгера — она в тесте выше.
    monkeypatch.setattr(server, 'configure_logging', lambda level: None)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    with (
        caplog.at_level(logging.ERROR, logger='pgsteward'),
        pytest.raises(SystemExit) as exc_info,
    ):
        server.main()

    assert exc_info.value.code == server._CONFIG_EXIT_CODE
    assert started == [], 'сервер не должен подниматься на битой конфигурации'
    assert expected in caplog.text
    # Внутренности pydantic в сообщении оператору не нужны.
    assert 'Value error,' not in caplog.text
    assert 'errors.pydantic.dev' not in caplog.text


def test_a_broken_connection_does_not_stop_the_server(monkeypatch):
    """Непригодное подключение — предупреждение, а не отказ запуска.

    Граница проходит именно здесь: отказ конфигурации процесса
    останавливает старт, проблема отдельного подключения — нет.
    """
    started: list[str] = []
    monkeypatch.setattr(server.mcp, 'run', lambda **_: started.append('run'))
    monkeypatch.setattr(server, 'configure_logging', lambda level: None)
    monkeypatch.setenv(CONNECTIONS_ENV, 'crm:CRM')
    monkeypatch.delenv('CRM_PG_HOST', raising=False)

    server.main()

    assert started == ['run']


async def test_every_core_tool_is_registered():
    """Отсутствующий инструмент незаметен: модель просто его не увидит."""
    assert set(await server.mcp.get_tools()) == {
        'list_connections',
        'list_databases',
        'list_schemas',
        'list_tables',
        'describe_table',
        'search_schema',
        'query',
        'explain_query',
        'analyze_db_health',
        'ping',
        'get_server_info',
        'get_security_config',
    }
