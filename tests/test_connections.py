import asyncio
import logging

import pytest

import pgsteward.connections as connections_module
from pgsteward.config import AppConfig, ConnectionConfig
from pgsteward.connections import ConnectionRegistry
from pgsteward.errors import (
    AdapterConnectionError,
    ConnectionErrorCode,
    ConnectionMisconfiguredError,
    ConnectionNotFoundError,
)


class FakeAdapter:
    def __init__(self, conn, policy=None):
        self.conn = conn
        self.connect_calls = 0
        self.close_calls = 0
        self.connect_error: Exception | None = None

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error

    async def close(self) -> None:
        self.close_calls += 1

    async def ping(self) -> bool:
        return True


@pytest.fixture
def adapters(monkeypatch):
    """Подменить конструктор адаптера; вернуть созданные по connection_id."""
    created: dict[str, FakeAdapter] = {}

    def _create(conn, policy=None):
        adapter = created.get(conn.id)
        if adapter is None:
            adapter = FakeAdapter(conn, policy)
            created[conn.id] = adapter
        return adapter

    monkeypatch.setattr(connections_module, 'PostgresAdapter', _create)
    return created


def _registry(*ids) -> ConnectionRegistry:
    return ConnectionRegistry(
        AppConfig(
            connections=[ConnectionConfig(id=conn_id) for conn_id in ids]
        )
    )


def test_get_config_unknown_id_lists_available():
    registry = _registry('crm')
    with pytest.raises(ConnectionNotFoundError) as exc_info:
        registry.get_config('nope')
    assert 'crm' in str(exc_info.value)


async def test_adapter_created_once_under_concurrency(adapters):
    registry = _registry('crm')
    results = await asyncio.gather(
        *(registry.get_adapter('crm') for _ in range(5))
    )
    assert adapters['crm'].connect_calls == 1
    assert {id(r) for r in results} == {id(adapters['crm'])}


async def test_failed_connect_is_not_cached(adapters):
    registry = _registry('crm')
    broken = FakeAdapter(registry.get_config('crm'))
    broken.connect_error = RuntimeError('нет сети')
    adapters['crm'] = broken

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await registry.get_adapter('crm')
    assert broken.connect_calls == 2


async def test_list_connections_does_not_touch_the_network_by_default(
    adapters,
):
    """Справочный вызов не должен открывать соединение к каждой базе."""
    registry = _registry('crm', 'crm-prod')

    statuses = await registry.list_connections()

    assert adapters == {}
    assert {s.id for s in statuses} == {'crm', 'crm-prod'}
    assert all(s.alive is None for s in statuses)


@pytest.mark.parametrize(
    'code',
    [
        ConnectionErrorCode.AUTH_FAILED,
        ConnectionErrorCode.DATABASE_NOT_FOUND,
        ConnectionErrorCode.UNREACHABLE,
        ConnectionErrorCode.TIMEOUT,
        ConnectionErrorCode.TLS_FAILED,
    ],
    ids=lambda code: code.value,
)
async def test_probe_reports_the_classified_code(adapters, code):
    """Проба обязана донести именно тот код, который назвал адаптер.

    Адаптер уже разобрал ошибку драйвера (`classify_connection_error`),
    и подменять её общим `connection_failed` значит терять единственное,
    что отличает «неверный пароль» от «сервер недоступен».
    """
    registry = _registry('ok', 'broken')
    broken = FakeAdapter(registry.get_config('broken'))
    broken.connect_error = AdapterConnectionError('broken', code)
    adapters['broken'] = broken

    statuses = {s.id: s for s in await registry.list_connections(probe=True)}

    assert statuses['ok'].alive is True
    assert statuses['ok'].error_code is None
    assert statuses['broken'].alive is False
    assert statuses['broken'].error_code == code.value


async def test_probe_reports_error_code_without_driver_text(adapters):
    """Наружу идёт код, а не сообщение драйвера: в нём host и роль.

    Ошибка не от адаптера (а значит неклассифицированная) обязана
    сводиться к общему коду, не унося с собой текст.
    """
    registry = _registry('ok', 'broken')
    broken = FakeAdapter(registry.get_config('broken'))
    broken.connect_error = RuntimeError(
        'password authentication failed for user "readonly_prod"'
    )
    adapters['broken'] = broken

    statuses = {s.id: s for s in await registry.list_connections(probe=True)}

    assert statuses['ok'].alive is True
    assert statuses['ok'].error_code is None
    assert statuses['broken'].alive is False
    assert statuses['broken'].error_code == 'connection_failed'
    assert 'readonly_prod' not in str(statuses['broken'].model_dump())


async def test_probe_closes_the_connection_it_opened(adapters):
    """Проба не оставляет пул: адаптер закрывается сразу."""
    registry = _registry('crm')

    await registry.list_connections(probe=True)

    assert adapters['crm'].close_calls == 1
    assert registry._adapters == {}


class _SlowConnectTracker:
    """Адаптеры, застревающие в `connect`, и счётчик построенных."""

    def __init__(self) -> None:
        self.built = 0
        self.gate = asyncio.Event()


@pytest.fixture
def slow_connect(monkeypatch):
    """Адаптер, чей `connect` ждёт открытия ворот.

    Установление соединения — единственное место, где реестр держит
    незавершённое подключение: между взятием лока и записью в
    `_adapters` есть `await`. Чтобы проверить, что туда не влезает
    второй пул, эту паузу надо сделать управляемой.
    """
    tracker = _SlowConnectTracker()

    class SlowAdapter:
        def __init__(self, conn, policy=None):
            tracker.built += 1

        async def connect(self) -> None:
            await tracker.gate.wait()

        async def close(self) -> None:
            pass

        async def ping(self) -> bool:
            return True

    monkeypatch.setattr(connections_module, 'PostgresAdapter', SlowAdapter)
    return tracker


async def _settle() -> None:
    """Дать планировщику прокрутить все готовые задачи."""
    for _ in range(10):
        await asyncio.sleep(0)


async def test_probe_waits_for_an_in_flight_connect(slow_connect):
    """Проба не открывает второй пул к базе, которая ещё подключается.

    `_probe` создаёт одноразовый адаптер, и без общего с `get_adapter`
    лока обе ветки видели пустой реестр: к одной базе открывалось два
    пула, а закрывался из них только временный.
    """
    registry = _registry('crm')

    pending = asyncio.create_task(registry.get_adapter('crm'))
    await _settle()
    assert slow_connect.built == 1

    probe = asyncio.create_task(registry.list_connections(probe=True))
    await _settle()

    # Реестр ещё пуст: рабочий адаптер не записан, пока не подключится.
    assert registry._adapters == {}
    assert slow_connect.built == 1

    slow_connect.gate.set()
    adapter, statuses = await asyncio.gather(pending, probe)

    assert slow_connect.built == 1
    assert statuses[0].alive is True
    assert registry._adapters == {'crm': adapter}


def _misconfigured(conn_id='broken', missing=('BROKEN_PG_HOST',)):
    return ConnectionConfig(id=conn_id, missing_env=missing)


async def test_unusable_connection_is_refused_by_name(adapters):
    """Имена переменных идут наружу намеренно.

    Правило «только код, детали в лог» защищает от сообщений драйвера с
    host, port и ролью. Имя переменной окружения ничего из этого не
    несёт, а починить конфигурацию без него нельзя.
    """
    registry = ConnectionRegistry(
        AppConfig(connections=[_misconfigured(missing=('X_PG_HOST',))])
    )

    with pytest.raises(ConnectionMisconfiguredError) as exc_info:
        await registry.get_adapter('broken')

    assert 'X_PG_HOST' in str(exc_info.value)
    # До адаптера дело не доходит: подключаться не к чему.
    assert adapters == {}


async def test_one_unusable_connection_does_not_disable_the_others(adapters):
    """Опечатка в одном префиксе не должна закрывать агенту весь флот."""
    registry = ConnectionRegistry(
        AppConfig(connections=[ConnectionConfig(id='crm'), _misconfigured()])
    )

    await registry.get_adapter('crm')
    with pytest.raises(ConnectionMisconfiguredError):
        await registry.get_adapter('broken')

    assert adapters['crm'].connect_calls == 1


@pytest.mark.parametrize('probe', [False, True])
async def test_list_connections_flags_unusable_ones(adapters, probe):
    """Агент должен увидеть проблему до того, как упрётся в неё.

    Даже без `probe`: это не сетевой отказ, а отсутствие конфигурации,
    и знать о нём можно не обращаясь к базе.
    """
    registry = ConnectionRegistry(
        AppConfig(connections=[ConnectionConfig(id='crm'), _misconfigured()])
    )

    statuses = {s.id: s for s in await registry.list_connections(probe=probe)}

    assert statuses['broken'].error_code == 'misconfigured'
    assert statuses['broken'].alive is False
    assert statuses['crm'].error_code is None
    # Проба к незаполненному подключению не ходит в сеть.
    assert 'broken' not in adapters


async def test_list_connections_reports_environment(adapters):
    registry = ConnectionRegistry(
        AppConfig(
            connections=[
                ConnectionConfig(id='crm'),
                ConnectionConfig(id='crm-prod', environment='prod'),
            ]
        )
    )

    statuses = {s.id: s for s in await registry.list_connections(probe=True)}
    assert statuses['crm'].environment is None
    assert statuses['crm-prod'].environment == 'prod'


async def test_close_all_closes_adapters_and_is_idempotent(adapters):
    registry = _registry('crm')
    await registry.get_adapter('crm')

    await registry.close_all()
    await registry.close_all()

    assert adapters['crm'].close_calls == 1


# --- configuration audit at startup ---------------------------------------
#
# Все претензии к конфигурации оператор видит в одном месте — выводе
# старта, — а не по мере того, как агент дойдёт до каждой базы.


def _startup_log(connections, caplog):
    with caplog.at_level(logging.WARNING, logger='pgsteward'):
        ConnectionRegistry(AppConfig(connections=connections))
    return caplog.text


def test_missing_tls_is_reported_when_the_registry_is_built(caplog):
    """Предупреждение должно быть видно до обращения к базе.

    Пока оно жило в `connect()`, подключение, к которому агент за
    сессию не пришёл, не проверялось вовсе.
    """
    conn = ConnectionConfig(
        id='web-shop', env_prefix='WEB_SHOP', host='db.test', user='ro'
    )

    assert 'WEB_SHOP_PG_SSLMODE' in _startup_log([conn], caplog)


def test_an_unusable_connection_is_not_also_blamed_for_tls(caplog):
    """Оно и так не поднимется; лишняя строка уводит от причины."""
    conn = ConnectionConfig(
        id='crm',
        env_prefix='CRM',
        missing_env=('CRM_PG_HOST',),
    )

    text = _startup_log([conn], caplog)

    assert 'CRM_PG_HOST not set' in text
    assert 'sslmode' not in text


def test_building_the_registry_opens_no_connection(adapters):
    """Аудит конфигурации не должен трогать сеть."""
    _registry('crm', 'billing')

    assert adapters == {}


async def test_an_unusable_connection_reports_what_was_configured():
    """Строка статуса не должна выдумывать значения.

    Подключение непригодно, но `read_only` в нём разобрался — и именно
    его оператор увидит в `list_connections`, а не дефолт модели.
    """
    conn = ConnectionConfig(
        id='crm',
        read_only=False,
        environment='prod',
        invalid_env=('CRM_PG_MAX_ROWS',),
    )
    registry = ConnectionRegistry(AppConfig(connections=[conn]))

    (status,) = await registry.list_connections()

    assert status.error_code == 'misconfigured'
    assert status.alive is False
    assert status.read_only is False
    assert status.environment == 'prod'
