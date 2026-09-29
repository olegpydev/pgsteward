"""Настройка логгера.

Модуль маленький, но его поведение видно во всех остальных тестах:
`configure_logging` заменяет хендлеры и отключает propagate, а на
propagate завязан `caplog`. Восстановление состояния — в conftest.
"""

import logging

from pgsteward.logger import configure_logging, logger


def test_level_comes_from_the_argument():
    configure_logging('DEBUG')
    assert logger.level == logging.DEBUG


def test_repeated_configuration_does_not_duplicate_output():
    """Присваивание, а не addHandler.

    Второй вызов иначе добавил бы ещё один хендлер, и каждая строка
    аудита печаталась бы дважды.
    """
    configure_logging('INFO')
    configure_logging('INFO')

    assert len(logger.handlers) == 1


def test_records_do_not_reach_the_root_logger():
    """Иначе чужой хендлер на root напечатает их второй раз."""
    configure_logging('INFO')
    assert logger.propagate is False


def test_output_goes_to_stderr_not_stdout():
    """stdout занят транспортом MCP: туда идёт JSON-RPC."""
    import sys

    configure_logging('INFO')
    (handler,) = logger.handlers

    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stderr


def test_the_format_carries_level_time_and_logger_name(capsys):
    configure_logging('INFO')

    logging.getLogger('pgsteward.adapters.postgres').info('connection is up')

    err = capsys.readouterr().err
    assert err.startswith('INFO: [')
    assert '"pgsteward.adapters.postgres"' in err
    assert 'connection is up' in err


def test_only_our_own_tree_is_configured():
    """Хендлер на root печатал бы нашим форматом и чужие записи.

    fastmcp тянет собственные зависимости, и на DEBUG своё сообщение
    пришлось бы выискивать среди внутренностей драйвера.

    Сравнение до и после, а не с пустым списком: свои хендлеры на root
    ставит сам pytest.
    """
    root = logging.getLogger()
    before = list(root.handlers)

    configure_logging('INFO')

    assert root.handlers == before
