"""Инфраструктура MCP-инструментов: декоратор, аудит, перевод ошибок.

Отдельно от `server.py`, чтобы расширения могли регистрировать свои
инструменты на том же сервере. Сам обход ответа живёт в
`serialization`: у него есть второй потребитель на стороне адаптера.
"""

import functools
import inspect
from collections.abc import Callable
from typing import Any

from pgsteward.adapters.base import DBAdapter, elapsed_ms, now_ms
from pgsteward.config import ConnectionConfig
from pgsteward.dependencies import get_registry
from pgsteward.errors import ConnectionMisconfiguredError, DatabaseMCPError
from pgsteward.logger import logger
from pgsteward.models import QueryResult
from pgsteward.serialization import to_json


def _require(value: str | None, name: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f'Parameter "{name}" is required.')
    return value


def _require_row_limit(limit: int | None) -> int | None:
    """Отклонить `limit=0` — ту же опечатку, что и `max_rows=0`.

    Основание то же, что у `config.ConnectionConfig._validate_max_rows`:
    `0` проходит до `_effective_limit` и даёт пустой результат с
    `truncated=true`, по которому агент заключает, что строк нет, хотя
    не просил ни одной.

    Отрицательное значение пропускается: его `_effective_limit`
    трактует как «своего предела нет, работает max_rows», и это
    осмысленный способ снять лимит вызова, не снимая лимит подключения.
    """
    if limit == 0:
        raise ValueError(
            'limit must be a positive number; 0 would return no rows at '
            'all. Omit limit to use the connection max_rows.'
        )
    return limit


def _config(connection_id: str) -> ConnectionConfig:
    """Валидация `connection_id` + его конфигурация, без подключения.

    Непригодное подключение отклоняется здесь, а не только на пути к
    пулу. Иначе `get_security_config` отвечал бы по такой записи
    политикой, собранной из дефолтов модели, — то есть уверенно и
    неверно, при том что именно к нему агента отправляют сверяться
    перед попыткой записи.
    """
    _require(connection_id, 'connection_id')
    conn = get_registry().get_config(connection_id)
    if not conn.is_configured:
        raise ConnectionMisconfiguredError(
            conn.id, conn.missing_env, conn.invalid_env
        )
    return conn


async def _adapter(connection_id: str) -> DBAdapter:
    """Валидация `connection_id` + получение адаптера."""
    _config(connection_id)
    return await get_registry().get_adapter(connection_id)


def _audit(
    name: str,
    started: float,
    kwargs: dict[str, Any],
    result: Any = None,
    error: BaseException | None = None,
) -> None:
    """Одна строка на вызов инструмента.

    Ни текста statement, ни значений параметров: журнал запросов с SQL и
    связанными значениями сам по себе является копией данных, а логи
    обычно уезжают туда, где защиты меньше, чем у базы.

    Отказы пишутся наравне с успехами — отклонённая политикой попытка
    записи интересна аудиту ровно настолько же, насколько удавшееся
    чтение. Класс исключения безопасен: сообщение может нести
    подробности, имя типа — нет.
    """
    fields: dict[str, Any] = {'tool': name}
    # `federated_lookup` работает с парой баз и называет их
    # source_/target_connection_id, поэтому берутся все подходящие.
    fields.update(
        (key, value)
        for key, value in kwargs.items()
        if key.endswith('connection_id')
    )
    fields['elapsed_ms'] = elapsed_ms(started)
    fields['outcome'] = 'error' if error is not None else 'ok'
    if error is not None:
        fields['error'] = type(error).__name__
    if isinstance(result, QueryResult):
        fields['kind'] = result.kind
        fields['row_count'] = result.row_count
        fields['truncated'] = result.truncated

    logger.info(
        ' '.join(f'{k}={v}' for k, v in fields.items() if v is not None)
    )


def make_tool(
    mcp: Any,
) -> Callable[[str, str], Callable[[Callable[..., Any]], Any]]:
    """Фабрика декоратора tool для переданного mcp-инстанса.

    Позволяет расширениям регистрировать tool'ы на том же mcp, что и ядро.
    """

    def tool(
        name: str, description: str
    ) -> Callable[[Callable[..., Any]], Any]:
        """Зарегистрировать tool: сериализация ответа, аудит, перевод ошибок.

        Доменные ошибки становятся `ValueError` без цепочки исключений:
        `__cause__` содержал бы исходное сообщение драйвера, а оно не должно
        попадать в контекст модели.
        """

        def decorator(fn: Callable[..., Any]) -> Any:
            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> str:
                started = now_ms()
                try:
                    result = await fn(*args, **kwargs)
                    # Сериализация внутри try: её отказ — такой же
                    # исход вызова, как ошибка самого инструмента, и
                    # мимо журнала он проходить не должен.
                    payload = to_json(result)
                except DatabaseMCPError as exc:
                    _audit(name, started, kwargs, error=exc)
                    raise ValueError(str(exc)) from None
                except Exception as exc:
                    _audit(name, started, kwargs, error=exc)
                    raise
                _audit(name, started, kwargs, result=result)
                return payload

            # FastMCP строит схему параметров по сигнатуре; наружу tool
            # всегда отдаёт JSON-строку, поэтому возвращаемый тип подменяем.
            wrapper.__signature__ = inspect.signature(fn).replace(  # type: ignore[attr-defined]
                return_annotation=str
            )
            wrapper.__annotations__ = {**fn.__annotations__, 'return': str}
            del wrapper.__wrapped__
            return mcp.tool(name, description=description)(wrapper)

        return decorator

    return tool
