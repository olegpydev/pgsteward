"""MCP-слой: FastMCP-сервер с инструментами доступа к БД.

Строки, которые читает модель (описания tool'ов, подсказки параметров,
сообщения об ошибках), написаны по-английски; комментарии и docstring'и —
для разработчика.

Описания намеренно короткие: они попадают в контекст каждой сессии.
Всё, что нужно не при выборе инструмента, а при чтении ответа, приходит
в поле `notes` самого ответа или лежит в docs/tools.md.

Точка stdio-запуска: `python -m pgsteward.server`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import ValidationError

from pgsteward.config import get_app_config, get_settings
from pgsteward.dependencies import close_registry, get_registry
from pgsteward.extensions import register_all
from pgsteward.health import overall_status
from pgsteward.logger import configure_logging, logger
from pgsteward.models import HealthReport
from pgsteward.security import resolve_security
from pgsteward.tooling import (
    _adapter,
    _config,
    _require,
    _require_row_limit,
    make_tool,
)

_CONNECTION_ID_ARG = 'Connection id. See list_connections.'
_SCHEMA_ARG = 'Schema name; defaults to "public".'
_PARAMS_ARG = 'Positional PostgreSQL parameters ($1, $2, ...).'

# Уровень на время чтения настроек: неизвестный `PGSTEWARD_LOG_LEVEL`
# отвергает сам `Settings`, и сообщить об этом нужно уже настроенным
# логгером, а не через `logging.lastResort`.
_FALLBACK_LOG_LEVEL = 'INFO'

# Код выхода для отказа конфигурации. Отличен от 1, чтобы обёртка могла
# отличить «сервер не с чем запускать» от падения во время работы.
_CONFIG_EXIT_CODE = 2


@asynccontextmanager
async def _lifespan(mcp: FastMCP) -> AsyncIterator[dict[str, Any]]:
    try:
        get_registry()
        register_all(mcp)
        yield {}
    finally:
        await close_registry()


mcp = FastMCP(
    'pgsteward',
    lifespan=_lifespan,
    instructions=(
        'Read-only PostgreSQL access: schema exploration, guarded SQL, '
        'query plans and health checks. Every call targets a preconfigured '
        'connection_id; credentials stay on the server. '
        'Start from list_connections, then narrow down with list_schemas, '
        'list_tables or search_schema before writing SQL.'
    ),
)

tool = make_tool(mcp)


@tool(
    'list_connections',
    'List the configured connections: id, read_only and inferred '
    'environment. Start here; every other tool needs a connection_id. '
    'Set probe=true to also test reachability, which opens a connection '
    'to each database.',
)
async def list_connections(
    probe: Annotated[
        bool,
        'Test reachability of every connection (default false: no network '
        'access, alive is null).',
    ] = False,
) -> Any:
    registry = get_registry()
    return await registry.list_connections(probe=probe)


@tool(
    'list_databases',
    "List the databases on this connection's server. Querying one needs "
    'its own configured connection: PostgreSQL cannot read across '
    'databases in a session.',
)
async def list_databases(
    connection_id: Annotated[str, _CONNECTION_ID_ARG],
) -> Any:
    return await (await _adapter(connection_id)).list_databases()


@tool(
    'list_schemas',
    "List the non-system schemas in this connection's database.",
)
async def list_schemas(
    connection_id: Annotated[str, _CONNECTION_ID_ARG],
) -> Any:
    return await (await _adapter(connection_id)).list_schemas()


@tool(
    'list_tables',
    'List tables and views in a schema (default "public").',
)
async def list_tables(
    connection_id: Annotated[str, _CONNECTION_ID_ARG],
    schema: Annotated[str | None, _SCHEMA_ARG] = None,
) -> Any:
    return await (await _adapter(connection_id)).list_tables(schema)


@tool(
    'describe_table',
    'Describe one table or view: columns, primary key, foreign keys, '
    'indexes and an instant row-count estimate. Fails with an explicit '
    'error if the relation does not exist.',
)
async def describe_table(
    connection_id: Annotated[str, _CONNECTION_ID_ARG],
    table: Annotated[str, 'Table name.'],
    schema: Annotated[str | None, _SCHEMA_ARG] = None,
) -> Any:
    _require(table, 'table')
    return await (await _adapter(connection_id)).describe_table(table, schema)


@tool(
    'search_schema',
    'Find tables and columns whose name contains a substring, across '
    'every non-system schema. Use it when you know the field you need '
    'but not which table holds it. Note that "_" and "%" are wildcards.',
)
async def search_schema(
    connection_id: Annotated[str, _CONNECTION_ID_ARG],
    pattern: Annotated[
        str, 'Substring to look for in table and column names.'
    ],
    schema: Annotated[str | None, 'Restrict the search to one schema.'] = None,
) -> Any:
    _require(pattern, 'pattern')
    return await (await _adapter(connection_id)).search_schema(pattern, schema)


@tool(
    'query',
    'Run a single SQL statement. Read-only by default; writes need the '
    'connection policy to allow them. Use params for positional '
    'parameters ($1) rather than string interpolation. Rows are capped '
    'by min(limit, the connection max_rows): applied_limit is the cap '
    'that was used and truncated says rows were dropped. When '
    'applied_limit is below the limit you asked for, max_rows cut the '
    'result and the answer is incomplete. A separate cap on response '
    'size can also cut it; when it does, notes says so and the fix is '
    'fewer columns, not fewer rows.',
)
async def query(
    connection_id: Annotated[str, _CONNECTION_ID_ARG],
    statement: Annotated[str, 'A single SQL statement.'],
    params: Annotated[list[Any] | None, _PARAMS_ARG] = None,
    limit: Annotated[int | None, 'Maximum rows to return.'] = None,
) -> Any:
    _require(statement, 'statement')
    _require_row_limit(limit)
    adapter = await _adapter(connection_id)
    return await adapter.execute(statement, params=params, limit=limit)


@tool(
    'explain_query',
    'Execution plan via EXPLAIN (FORMAT JSON), parsed into a tree with '
    'warnings for sequential scans and stale statistics. analyze=true '
    'physically executes the statement to get actual row counts and '
    'timings; without it there are planner estimates and no timings.',
)
async def explain_query(
    connection_id: Annotated[str, _CONNECTION_ID_ARG],
    statement: Annotated[str, 'SQL statement to analyze.'],
    params: Annotated[list[Any] | None, _PARAMS_ARG] = None,
    analyze: Annotated[
        bool, 'Run EXPLAIN ANALYZE, executing the statement.'
    ] = False,
) -> Any:
    _require(statement, 'statement')
    return await (await _adapter(connection_id)).explain(
        statement, params=params, analyze=analyze
    )


@tool(
    'analyze_db_health',
    'Eleven deterministic checks over the system catalog: connections, '
    'idle transactions, long queries, cache hit rate, invalid, unused and '
    'duplicate indexes, constraints, wraparound, sequences, replication. '
    'No extensions required. A check returns skipped, not ok, when the '
    'role lacks the privilege or the statistics window is too short to '
    'judge; overall_status is unknown when nothing could be checked.',
)
async def analyze_db_health(
    connection_id: Annotated[str, _CONNECTION_ID_ARG],
) -> Any:
    checks = await (await _adapter(connection_id)).analyze_health()
    return HealthReport(
        connection_id=connection_id,
        overall_status=overall_status(checks),
        checks=checks,
    )


@tool('ping', 'Check that a connection is reachable (SELECT 1).')
async def ping(
    connection_id: Annotated[str, _CONNECTION_ID_ARG],
) -> Any:
    alive = await (await _adapter(connection_id)).ping()
    return {'connection_id': connection_id, 'alive': alive}


@tool(
    'get_server_info',
    'Server version and current database behind a connection.',
)
async def get_server_info(
    connection_id: Annotated[str, _CONNECTION_ID_ARG],
) -> Any:
    return await (await _adapter(connection_id)).server_info()


@tool(
    'get_security_config',
    'Effective policy of a connection: read_only, whether DML and DDL '
    'are allowed, max_rows, max_bytes and query_timeout. Check it before '
    'attempting a write.',
)
async def get_security_config(
    connection_id: Annotated[str, _CONNECTION_ID_ARG],
) -> Any:
    return resolve_security(_config(connection_id), get_app_config().security)


def _config_error(exc: Exception) -> str:
    """Причина отказа одной строкой.

    `ValidationError` печатает заголовок, путь к полю и ссылку на
    документацию pydantic — три строки на ошибку, из которых оператору
    нужна ровно одна. Префикс `Value error, ` добавляет сам pydantic;
    сообщения валидаторов в `config.py` уже написаны как готовый совет,
    и предисловие им только мешает.
    """
    if isinstance(exc, ValidationError):
        messages = [
            str(error['msg']).removeprefix('Value error, ')
            for error in exc.errors()
        ]
        return '; '.join(dict.fromkeys(messages))
    return str(exc)


def main() -> None:
    """Точка входа stdio.

    Конфигурация читается здесь, а не только в `_lifespan`. Отказ на ней
    — это отказ запуска, и сообщить о нём нужно строкой, которую
    оператор прочитает: из `_lifespan` то же исключение выходит через
    anyio и FastMCP, и причина теряется в двух десятках кадров чужого
    стека. Проблемы отдельных подключений сюда не относятся — они
    остаются предупреждениями, а сервер с ними стартует.
    """
    configure_logging(_FALLBACK_LOG_LEVEL)
    try:
        configure_logging(get_settings().log_level)
        # Прогрев: дальше реестр собирается из уже прочитанной
        # конфигурации и упасть на ней не может.
        get_app_config()
    except (ValidationError, ValueError) as exc:
        logger.error('configuration is unusable: %s', _config_error(exc))
        raise SystemExit(_CONFIG_EXIT_CODE) from None
    mcp.run(show_banner=False)


if __name__ == '__main__':
    main()
