"""Общие фикстуры и изоляция процессного состояния.

Почти всё состояние pgsteward живёт в кэшах уровня процесса
(`get_settings`, `get_app_config`, `_dotenv_fallback`,
`load_relationships`), в глобальном реестре подключений и в настройках
логгера. Без сброса первый же тест, который их тронет, пинит значение
на всю сессию, и результат начинает зависеть от порядка файлов.
"""

import logging
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest

import pgsteward.config as config_module
import pgsteward.dependencies as dependencies_module
from pgsteward.config import (
    ENV_PREFIX,
    XDG_CONFIG_HOME_ENV,
    _dotenv_fallback,
    get_app_config,
    get_settings,
)
from pgsteward.extensions.federation import load_relationships
from pgsteward.logger import logger as pgsteward_logger

TEST_DSN_ENV = 'PGSTEWARD_TEST_DSN'

# Тот же файл, который документация предлагает пользователю как образец:
# тесты заодно проверяют, что пример валиден и не разошёлся со схемой
# моделей. Живёт здесь, а не в тестовом модуле, — импорт одного теста из
# другого ломается при `--import-mode=importlib`.
EXAMPLE_MAP = (
    Path(__file__).resolve().parent.parent
    / 'examples'
    / 'relationships.example.json'
)


def _clear_config_caches() -> None:
    """Сбросить кэши по прямым ссылкам, а не через атрибуты модуля.

    Отдельные тесты подменяют `config._dotenv_fallback` обычной
    функцией; атрибутный доступ упёрся бы в заглушку без `cache_clear`.
    """
    get_settings.cache_clear()
    get_app_config.cache_clear()
    _dotenv_fallback.cache_clear()
    load_relationships.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_process_state() -> Iterator[None]:
    """Сбросить кэши конфигурации и реестр до и после каждого теста."""
    _clear_config_caches()
    dependencies_module._registry = None
    yield
    _clear_config_caches()
    dependencies_module._registry = None


@pytest.fixture(autouse=True)
def _isolate_from_local_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Тесты не должны видеть конфигурацию разработчика.

    `.env` закрывается через `resolve_env_file`: сквозь неё ходят оба
    потребителя файла. Сам `_dotenv_fallback` остаётся настоящим,
    поэтому тесты прав доступа вызывают его напрямую.

    Каталог конфигурации — через `XDG_CONFIG_HOME`: иначе настоящий
    `~/.config/pgsteward/relationships.json` делал бы результат тестов
    federation зависимым от машины.

    Переменные `PGSTEWARD_*` из окружения снимаются целиком. Файл — не
    единственный способ настроить сервер, и экспортированный в шелле
    `PGSTEWARD_DB_CONNECTIONS` тихо подменял бы `Settings()` там, где
    тест ожидает пустую конфигурацию. Исключение одно: DSN
    integration-тестов, который задаётся ровно так и никак иначе.
    """
    monkeypatch.setattr(config_module, 'resolve_env_file', lambda: None)
    monkeypatch.setenv(XDG_CONFIG_HOME_ENV, str(tmp_path / 'xdg'))
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX) and name != TEST_DSN_ENV:
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _restore_logger() -> Iterator[None]:
    """Вернуть логгер в исходное состояние.

    `configure_logging` заменяет хендлеры и ставит `propagate = False`
    (`pgsteward/logger.py`). Без восстановления любой тест, который её
    вызвал, обнуляет `caplog` для всех последующих, и audit-ассерты
    начинают проходить вхолостую.
    """
    handlers = list(pgsteward_logger.handlers)
    level = pgsteward_logger.level
    propagate = pgsteward_logger.propagate
    yield
    pgsteward_logger.handlers = handlers
    pgsteward_logger.setLevel(level)
    pgsteward_logger.propagate = propagate


@pytest.fixture
def audit(caplog: pytest.LogCaptureFixture) -> Callable[[], list[str]]:
    """Строки аудита, записанные инструментами за время теста."""
    caplog.set_level(logging.INFO, logger='pgsteward')

    def lines() -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.name.startswith('pgsteward')
        ]

    return lines


def _refuse_a_database_that_is_not_disposable(dsn: str) -> None:
    """Отказаться работать по базе, чьё имя не заявляет её одноразовой.

    Integration-тесты выполняют `DROP SCHEMA ... CASCADE`, `DROP OWNED
    BY` и `DROP ROLE`. Цена ошибки в переменной окружения несимметрична,
    поэтому дешёвое соглашение об имени лучше его отсутствия.

    Не-URI DSN проверку проходит: такой формат поддерживается, а имя
    базы из него не достать.
    """
    parts = urlsplit(dsn)
    if parts.scheme not in ('postgres', 'postgresql'):
        return
    database = parts.path.lstrip('/')
    if database and 'test' not in database.lower():
        pytest.fail(
            f'{TEST_DSN_ENV} points at database "{database}", whose name '
            'does not contain "test". These tests drop schemas and roles; '
            'point them at a disposable database.'
        )


@pytest.fixture(scope='session')
def integration_dsn() -> str:
    """DSN одноразовой PostgreSQL для integration-тестов.

    Без переменной окружения тесты не падают, а пропускаются: локальный
    прогон не должен требовать поднятой БД. CI задаёт её явно.
    """
    dsn = os.environ.get(TEST_DSN_ENV)
    if not dsn or not dsn.strip():
        pytest.skip(f'{TEST_DSN_ENV} не задан — integration-тесты пропущены')
    dsn = dsn.strip()
    _refuse_a_database_that_is_not_disposable(dsn)
    return dsn
