"""Реестр подключений как синглтон процесса.

Модуль в три строки, но он стоит между MCP-слоем и всеми пулами: если
`close_registry` не обнулит ссылку, следующий `get_registry` вернёт
реестр с закрытыми пулами. Сброс между тестами — в conftest.
"""

import pgsteward.dependencies as dependencies
from pgsteward.config import CONNECTIONS_ENV
from pgsteward.dependencies import close_registry, get_registry


def test_the_registry_is_built_once(monkeypatch):
    monkeypatch.delenv(CONNECTIONS_ENV, raising=False)

    assert get_registry() is get_registry()


def test_the_registry_is_not_built_at_import_time():
    """Ошибка конфигурации должна давать сообщение при старте.

    На импорте она превратилась бы в traceback неизвестно откуда.
    """
    assert dependencies._registry is None


async def test_closing_releases_the_reference(monkeypatch):
    """Иначе следующий `get_registry` вернёт реестр с закрытыми пулами."""
    monkeypatch.delenv(CONNECTIONS_ENV, raising=False)
    first = get_registry()

    await close_registry()

    assert dependencies._registry is None
    assert get_registry() is not first


async def test_closing_an_absent_registry_is_a_no_op():
    """`_lifespan` зовёт это в `finally`, в том числе после сбоя старта."""
    await close_registry()
    await close_registry()

    assert dependencies._registry is None


async def test_closing_closes_every_open_adapter(monkeypatch):
    monkeypatch.setenv(CONNECTIONS_ENV, 'crm:CRM')
    monkeypatch.setenv('CRM_PG_HOST', '10.0.0.5')
    monkeypatch.setenv('CRM_PG_USER', 'ro')
    monkeypatch.setenv('CRM_PG_DATABASE', 'crm')
    closed: list[str] = []

    class FakeAdapter:
        async def close(self):
            closed.append('crm')

    registry = get_registry()
    # Адаптер подставляется напрямую: поднимать настоящий пул в этом
    # тесте нечем, а интересует именно то, что lifespan его закроет.
    registry._adapters['crm'] = FakeAdapter()

    await close_registry()

    assert closed == ['crm']
