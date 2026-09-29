"""Доменные ошибки pgsteward.

Всё наследуется от `DatabaseMCPError`, чтобы MCP-слой ловил домен одним
`except`. Сообщения этих ошибок попадают в контекст модели, поэтому в них
не должно быть инфраструктурных подробностей — см. `ConnectionErrorCode`.
"""

from enum import StrEnum


class ConnectionErrorCode(StrEnum):
    """Стабильный код вместо сообщения драйвера.

    Тексты asyncpg содержат host, port, имя роли и имя базы; в
    транскрипт модели им попадать нельзя. Полная диагностика — в stderr.
    """

    AUTH_FAILED = 'auth_failed'
    DATABASE_NOT_FOUND = 'database_not_found'
    UNREACHABLE = 'unreachable'
    TLS_FAILED = 'tls_failed'
    TIMEOUT = 'timeout'
    CONNECTION_FAILED = 'connection_failed'
    # Подключение объявлено, но переменных для него нет. Единственный
    # код, устанавливаемый без обращения к сети.
    MISCONFIGURED = 'misconfigured'


class DatabaseMCPError(Exception):
    """Базовая ошибка домена."""


class ConnectionNotFoundError(DatabaseMCPError):
    """Запрошенный `connection_id` не найден в реестре подключений."""


class ConnectionMisconfiguredError(DatabaseMCPError):
    """Подключение объявлено, но пользоваться им нельзя.

    Две причины разделены: переменная не задана или её значение не
    разобрано. Чинятся они по-разному, и «is not set» про опечатку в
    числе только уводит в сторону.

    Здесь, в отличие от `AdapterConnectionError`, имена переменных
    называются прямо. Правило «только код, детали в лог» защищает от
    сообщений драйвера с host, port, ролью и именем базы; имена
    переменных окружения ничего из этого не несут, а починить
    конфигурацию без них нельзя. Само отвергнутое значение при этом
    наружу не идёт: в нём может оказаться что угодно.
    """

    def __init__(
        self,
        connection_id: str,
        missing: tuple[str, ...] = (),
        invalid: tuple[str, ...] = (),
    ) -> None:
        self.connection_id = connection_id
        self.missing = missing
        self.invalid = invalid
        reasons = []
        if missing:
            reasons.append(
                f'{", ".join(missing)} '
                f'{"is" if len(missing) == 1 else "are"} not set'
            )
        if invalid:
            reasons.append(
                f'{", ".join(invalid)} '
                f'{"has" if len(invalid) == 1 else "have"} a value that '
                'could not be used'
            )
        super().__init__(
            f'Connection "{connection_id}" is not configured: '
            f'{"; ".join(reasons)}. '
            'Fix it in the environment or in the pgsteward .env file; '
            'the server log names the problem.'
        )


class RelationNotFoundError(DatabaseMCPError):
    """Таблица или представление не найдены."""


class WriteForbiddenError(DatabaseMCPError):
    """Попытка записи на read-only подключении или при запрете DML/DDL."""


class StatementNotAllowedError(DatabaseMCPError):
    """Statement запрещён политикой (multi-statement, неизвестный вид)."""


class QueryTimeoutError(DatabaseMCPError):
    """Запрос превысил отведённый таймаут."""


class AdapterConnectionError(DatabaseMCPError):
    """Только код и `connection_id` (см. `ConnectionErrorCode`)."""

    def __init__(self, connection_id: str, code: ConnectionErrorCode) -> None:
        self.connection_id = connection_id
        self.code = code
        super().__init__(
            f'Connection "{connection_id}" is unavailable ({code.value}). '
            'Details are in the pgsteward server log.'
        )


class ExplainError(DatabaseMCPError):
    """`EXPLAIN` не вернул ожидаемый план выполнения."""
