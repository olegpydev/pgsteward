"""Движко-независимые модели ответов.

`extra` несёт движко-специфику, которой нет в общем контракте.
`notes` — пояснения, которые нужны только при чтении конкретного ответа
(они намеренно не живут в описаниях tool'ов, чтобы не занимать контекст
в каждой сессии).
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# `schema` затеняет атрибут pydantic.BaseModel, поэтому поле называется
# `schema_`, а наружу отдаётся под именем `schema` через alias.
_MODEL_CONFIG = ConfigDict(frozen=True, populate_by_name=True)


class ColumnInfo(BaseModel):
    model_config = _MODEL_CONFIG

    name: str
    data_type: str
    nullable: bool
    default: str | None = None
    is_primary_key: bool = False


class ForeignKeyInfo(BaseModel):
    """Одна пара «колонка -> колонка».

    Составной FK — несколько записей с общим `constraint_name`, по одной
    на позицию ключа.
    """

    model_config = _MODEL_CONFIG

    column: str
    references_table: str
    references_column: str
    references_schema: str | None = None
    constraint_name: str | None = None


class IndexInfo(BaseModel):
    """Индекс: ключевые колонки отдельно от `INCLUDE`.

    `columns` — ключевые колонки в порядке индекса (позиция-выражение
    записана текстом). `included_columns` — неключевые `INCLUDE (...)`:
    доступны index-only scan'у, но поиск по ним невозможен.
    """

    model_config = _MODEL_CONFIG

    name: str
    columns: list[str]
    is_unique: bool = False
    included_columns: list[str] = Field(default_factory=list)


class TableInfo(BaseModel):
    """Одна строка `list_tables`.

    `extra` здесь нет намеренно, в отличие от `TableSchema`: заполнять
    его было нечем, а пустой словарь ехал в контекст модели в каждой из
    сотен строк перечисления.
    """

    model_config = _MODEL_CONFIG

    name: str
    schema_: str | None = Field(default=None, alias='schema')
    kind: str = 'table'


class SchemaMatch(BaseModel):
    """Одно совпадение поиска по имени таблицы/колонки."""

    model_config = _MODEL_CONFIG

    schema_: str = Field(alias='schema')
    table: str
    kind: str
    column: str | None = None


class SchemaSearchResult(BaseModel):
    """Без `truncated` агент не отличит «это всё» от «это первые N»."""

    model_config = _MODEL_CONFIG

    matches: list[SchemaMatch]
    match_count: int
    truncated: bool = False
    limit: int | None = None
    notes: list[str] = Field(default_factory=list)


class TableSchema(BaseModel):
    model_config = _MODEL_CONFIG

    name: str
    schema_: str | None = Field(default=None, alias='schema')
    columns: list[ColumnInfo]
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKeyInfo] = Field(default_factory=list)
    indexes: list[IndexInfo] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class QueryResult(BaseModel):
    """`kind` — класс statement, по которому применялась политика.

    Он возвращается, а не вычисляется заново на стороне вызывающего:
    повторная классификация того же текста могла бы разойтись с тем
    решением, которое реально было принято.

    `applied_limit` — фактический предел строк, `min(limit, max_rows)`;
    `None`, если предела не было. Один `truncated` не отвечает, кто
    обрезал выдачу: при `limit=5` он `True` и когда строк просто больше
    пяти (ровно то, о чём просили), и когда сработал `max_rows`
    (неполный ответ, о котором агент иначе не узнает).

    `applied_byte_limit` — предел на размер строк ответа, `max_bytes`
    подключения. Отдельное поле, а не ещё одно значение в `truncated`:
    ответ, обрезанный по размеру, чинится не тем же, чем обрезанный по
    числу строк. Просить меньше строк бесполезно — нужно просить меньше
    колонок, и сказать об этом может только нота.

    `notes` заполняются, когда значение пришлось представить иначе, чем
    оно лежит в базе, — иначе подмена неотличима от самих данных.
    """

    model_config = _MODEL_CONFIG

    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    kind: str
    truncated: bool = False
    applied_limit: int | None = None
    applied_byte_limit: int | None = None
    execution_ms: float | None = None
    notes: list[str] = Field(default_factory=list)


class ConnectionStatus(BaseModel):
    """`alive` — `None`, если не проверялось (`probe=false`).

    `error_code` — код, а не текст драйвера (см. `ConnectionErrorCode`).
    """

    model_config = _MODEL_CONFIG

    id: str
    read_only: bool
    alive: bool | None = None
    error_code: str | None = None
    environment: str | None = None


class ServerInfo(BaseModel):
    model_config = _MODEL_CONFIG

    version: str
    extra: dict[str, Any] = Field(default_factory=dict)


class QueryPlan(BaseModel):
    """Тайминги заполняются только при `analyze=True`."""

    model_config = _MODEL_CONFIG

    plan: dict[str, Any]
    planning_time_ms: float | None = None
    execution_time_ms: float | None = None
    warnings: list[str] = Field(default_factory=list)
    execution_ms: float | None = None


class Status(StrEnum):
    """Исход health-проверки.

    `SKIPPED` — «посмотреть не удалось»: прав не хватает или окно
    статистики слишком короткое. Это не разновидность `OK`, и в сводку
    он не идёт. `UNKNOWN` ставится только сводкой, когда содержательных
    проверок не осталось вовсе.
    """

    OK = 'ok'
    WARNING = 'warning'
    CRITICAL = 'critical'
    SKIPPED = 'skipped'
    UNKNOWN = 'unknown'


class HealthCheckResult(BaseModel):
    model_config = _MODEL_CONFIG

    id: str
    status: Status
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class HealthReport(BaseModel):
    model_config = _MODEL_CONFIG

    connection_id: str
    overall_status: Status
    checks: list[HealthCheckResult]


class EffectiveSecurity(BaseModel):
    model_config = _MODEL_CONFIG

    read_only: bool
    allow_dml: bool
    allow_ddl: bool
    max_rows: int
    max_bytes: int
    query_timeout: int
