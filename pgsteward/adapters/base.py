"""Контракт адаптера БД: MCP-слой знает только протокол `DBAdapter`."""

import time
from enum import StrEnum
from typing import Any, Protocol

from pgsteward.models import (
    HealthCheckResult,
    QueryPlan,
    QueryResult,
    SchemaSearchResult,
    ServerInfo,
    TableInfo,
    TableSchema,
)


class StatementKind(StrEnum):
    """Класс SQL-statement по первому значащему ключевому слову."""

    READ = 'read'
    DML = 'dml'
    DDL = 'ddl'
    OTHER = 'other'


class DBAdapter(Protocol):
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def ping(self) -> bool: ...

    async def server_info(self) -> ServerInfo: ...

    async def list_databases(self) -> list[str]: ...

    async def list_schemas(self) -> list[str]: ...

    async def list_tables(
        self, schema: str | None = None
    ) -> list[TableInfo]: ...

    async def describe_table(
        self, table: str, schema: str | None = None
    ) -> TableSchema: ...

    async def search_schema(
        self, pattern: str, schema: str | None = None
    ) -> SchemaSearchResult: ...

    async def execute(
        self,
        statement: str,
        params: list[Any] | None = None,
        limit: int | None = None,
    ) -> QueryResult: ...

    async def explain(
        self,
        statement: str,
        params: list[Any] | None = None,
        analyze: bool = False,
    ) -> QueryPlan: ...

    async def analyze_health(self) -> list[HealthCheckResult]: ...


def now_ms() -> float:
    """Монотонные миллисекунды: для длительностей, не для дат."""
    return time.perf_counter() * 1000.0


def elapsed_ms(started_ms: float) -> float:
    return round(now_ms() - started_ms, 3)
