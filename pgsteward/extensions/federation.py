"""Федерация: карта сущностей и join между разными базами.

Карта опциональна: при её отсутствии или ошибке валидации инструменты
отключены, сохраняя чистоту контекста модели.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)

from pgsteward.config import env_value, strip_env_suffix, user_config_dir
from pgsteward.logger import logger
from pgsteward.tooling import (
    _adapter,
    _require,
    _require_row_limit,
    make_tool,
)

RELATIONSHIPS_FILE_ENV = 'PGSTEWARD_RELATIONSHIPS_FILE'
RELATIONSHIPS_FILE_NAME = 'relationships.json'

_MODEL_CONFIG = ConfigDict(populate_by_name=True)


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    """`1 entity` / `2 entities`: строка счёта без «1 entities»."""
    if count == 1:
        return f'{count} {singular}'
    return f'{count} {plural or singular + "s"}'


def quote_ident(name: str) -> str:
    """Идентификатор в двойных кавычках, как `quote_ident()` сервера.

    Имена из карты попадают в текст statement'а, а не в параметр:
    таблицу во `FROM` параметром не передать, и серверный
    `format('%I')`, которым пользуется `describe_table`, здесь тоже не
    применить. Без кавычек PostgreSQL приводит имя к нижнему регистру и
    спотыкается на всём, что требует цитирования, — `"Orders"`,
    пробелы, и особенно зарезервированные слова: колонка `group` или
    `user` в карте ломала бы вызов синтаксической ошибкой, которую
    оператор увидел бы только от агента.

    Инъекции это не закрывает и закрывать не должно: карту пишет
    оператор, а `source_where` рядом — сырой SQL по определению фичи.
    """
    return '"' + name.replace('"', '""') + '"'


def _validate_identifier(value: str) -> str:
    """Отвергнуть имя, которое нельзя закавычить.

    Пустая строка даёт `""` — невалидный идентификатор, NUL PostgreSQL
    не принимает вовсе. Ловится при разборе карты, чтобы такой файл
    переводил федерацию в `degraded` на старте, а не падал на вызове.
    """
    if not value or '\x00' in value:
        raise ValueError(
            'identifier must be a non-empty string without NUL characters'
        )
    return value


# Идентификатор объекта БД из карты связей.
Identifier = Annotated[str, AfterValidator(_validate_identifier)]


def _relationships_file() -> Path | None:
    """`PGSTEWARD_RELATIONSHIPS_FILE`, иначе каталог конфигурации.

    Переменная читается при вызове, а не на импорте, чтобы значение от
    MCP-клиента или теста применялось без перезагрузки модуля.
    """
    raw = env_value(RELATIONSHIPS_FILE_ENV)
    if raw is not None and raw.strip():
        return Path(raw.strip()).expanduser()
    default = user_config_dir() / RELATIONSHIPS_FILE_NAME
    return default if default.is_file() else None


class EntityKeyRef(BaseModel):
    """Место хранения сущности в конкретном подключении."""

    model_config = _MODEL_CONFIG

    connection: str
    table: Identifier
    column: Identifier
    schema_: Identifier = Field('public', alias='schema')
    is_primary_key: bool = False
    note: str | None = None


class ColumnRef(BaseModel):
    """Сторона join'а: подключение + таблица + колонка."""

    model_config = _MODEL_CONFIG

    connection: str
    table: Identifier
    column: Identifier
    schema_: Identifier = Field('public', alias='schema')


class RelationshipJoin(BaseModel):
    model_config = _MODEL_CONFIG

    entity: str
    from_: ColumnRef = Field(alias='from')
    to: ColumnRef
    relationship: str = 'many-to-one'


class OrientedJoin(BaseModel):
    """Join, ориентированный под пару `connection_id` агента."""

    source: ColumnRef
    target: ColumnRef
    relationship: str


class RelationshipEntity(BaseModel):
    id: str
    description: str
    keys: list[EntityKeyRef] = Field(default_factory=list)


class RelationshipMap(BaseModel):
    version: int = 1
    entities: list[RelationshipEntity] = Field(default_factory=list)
    joins: list[RelationshipJoin] = Field(default_factory=list)
    notes: str | None = None

    def filter_by_entity(self, entity_id: str) -> 'RelationshipMap':
        """Карта, сужённая до одной сущности; пустая, если её нет."""
        return RelationshipMap(
            version=self.version,
            entities=[e for e in self.entities if e.id == entity_id],
            joins=[j for j in self.joins if j.entity == entity_id],
            notes=self.notes,
        )

    def find_join(
        self,
        entity_id: str,
        source_connection_id: str,
        target_connection_id: str,
    ) -> OrientedJoin | None:
        """Join между парой connection_id, в любом порядке.

        Суффикс окружения отбрасывается: `orders-prod` совпадёт с
        записью `orders`.
        """
        source_base = strip_env_suffix(source_connection_id)
        target_base = strip_env_suffix(target_connection_id)
        for j in self.joins:
            if j.entity != entity_id:
                continue
            if j.from_.connection == source_base and (
                j.to.connection == target_base
            ):
                return OrientedJoin(
                    source=j.from_, target=j.to, relationship=j.relationship
                )
            if j.from_.connection == target_base and (
                j.to.connection == source_base
            ):
                return OrientedJoin(
                    source=j.to, target=j.from_, relationship=j.relationship
                )
        return None


# Всё, чем может ответить попытка прочитать карту, кроме успеха.
# `OSError` — файл есть, но не читается (права, снятый том, гонка с
# `is_file()`); `UnicodeDecodeError` — читается, но это не текст.
# Оба обязаны вести в degraded наравне с битым JSON: федерация
# опциональна, и её файл не должен решать, поднимется ли сервер.
MAP_READ_ERRORS = (
    json.JSONDecodeError,
    ValidationError,
    OSError,
    UnicodeDecodeError,
)


@lru_cache(maxsize=1)
def load_relationships() -> RelationshipMap:
    """Прочитать и провалидировать файл карты.

    Отсутствие файла — пустая карта, а не ошибка старта. Результат
    кэшируется: правка файла требует перезапуска сервера.

    Ошибки чтения и разбора наружу идут как есть: решение, что с ними
    делать, принимает `register`, а не эта функция.
    """
    path = _relationships_file()
    if path is None or not path.is_file():
        return RelationshipMap()
    raw = json.loads(path.read_text(encoding='utf-8'))
    return RelationshipMap.model_validate(raw)


async def run_federated_lookup(
    entity: str,
    source_connection_id: str,
    source_where: str,
    target_connection_id: str,
    target_columns: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Логика federated_lookup: джойн карты через две БД.

    Параметризация позволяет тестировать отдельно от MCP-слоя.
    """
    _require_row_limit(limit)
    rel_map = load_relationships()
    entity_map = rel_map.filter_by_entity(entity)
    if not entity_map.entities:
        raise ValueError(
            f'Entity "{entity}" is not present in the relationship map. '
            'Call describe_relationships without arguments to see which '
            'entities are available.'
        )
    joined = rel_map.find_join(
        entity, source_connection_id, target_connection_id
    )
    if joined is None:
        available = sorted(
            {
                f'{j.from_.connection}<->{j.to.connection}'
                for j in entity_map.joins
            }
        )
        raise ValueError(
            f'No known join of entity "{entity}" between '
            f'"{source_connection_id}" and "{target_connection_id}". '
            f'Known pairs: {", ".join(available) or "none"}.'
        )

    source_adapter = await _adapter(source_connection_id)
    # S608: `source_where` — сырой SQL от агента, как и в tool `query`;
    # параметризовать его нельзя по определению фичи. Statement проходит
    # assert_single_statement + check_statement и выполняется в READ ONLY
    # транзакции, поэтому новой поверхности для записи не появляется.
    source_sql = (
        f'SELECT {quote_ident(joined.source.column)} '  # noqa: S608
        f'FROM {quote_ident(joined.source.schema_)}'
        f'.{quote_ident(joined.source.table)} '
        f'WHERE {source_where}'
    )
    source_result = await source_adapter.execute(source_sql)

    source_meta = {
        'connection_id': source_connection_id,
        'schema': joined.source.schema_,
        'table': joined.source.table,
        'column': joined.source.column,
        'row_count': source_result.row_count,
        'truncated': source_result.truncated,
    }
    ids = [row[0] for row in source_result.rows]
    target_meta: dict[str, Any] = {
        'connection_id': target_connection_id,
        'schema': joined.target.schema_,
        'table': joined.target.table,
        'column': joined.target.column,
    }

    if not ids:
        return {
            'entity': entity,
            'source': source_meta,
            'target': target_meta,
            'result': None,
            'notes': [],
        }

    target_adapter = await _adapter(target_connection_id)
    # S608: тот же сырой SQL; список id передаётся параметром ($1).
    target_sql = (
        f'SELECT {target_columns or "*"} '  # noqa: S608
        f'FROM {quote_ident(joined.target.schema_)}'
        f'.{quote_ident(joined.target.table)} '
        f'WHERE {quote_ident(joined.target.column)} = ANY($1)'
    )
    target_result = await target_adapter.execute(
        target_sql, params=[ids], limit=limit
    )

    target_meta['row_count'] = target_result.row_count
    target_meta['truncated'] = target_result.truncated

    notes = (
        [
            'The source hit max_rows, so the join ran on an incomplete set '
            'of ids. Treat this result as partial.'
        ]
        if source_result.truncated
        else []
    )
    return {
        'entity': entity,
        'source': source_meta,
        'target': target_meta,
        'result': target_result,
        'notes': notes,
    }


def register(mcp: Any) -> str:
    """Зарегистрировать инструменты федеративного доступа: 'on' / 'off'
    / 'degraded'.

    Ни один исход не роняет сервер: федерация опциональна, и её файл не
    должен решать, получит ли агент доступ к базам.
    """
    tool = make_tool(mcp)

    map_error: tuple[str, str] | None = None
    try:
        rel_map = load_relationships()
    except MAP_READ_ERRORS as e:
        map_error = (type(e).__name__, str(_relationships_file() or 'unknown'))
        rel_map = RelationshipMap()

    has_entities = bool(rel_map.entities)
    is_degraded = map_error is not None

    if not has_entities and not is_degraded:
        logger.info('federation: off (no configured relationship map)')
        return 'off'

    error_note: str | None = None
    if is_degraded and map_error is not None:
        # «could not be read», а не «failed to parse»: сюда же приходит
        # файл, до содержимого которого дело не дошло вовсе.
        error_note = (
            f'Relationship map could not be read ({map_error[0]}). '
            f'Path: {map_error[1]}. See the server log for details.'
        )

    @tool(
        'describe_relationships',
        'Operator-maintained map of business entities that live in several '
        'databases under different keys. Consult it before joining data '
        'across connection_ids. Empty unless configured.',
    )
    async def describe_relationships(
        entity: Annotated[
            str | None, 'Filter by entity id; omit to list everything.'
        ] = None,
    ) -> dict[str, Any]:
        if is_degraded:
            result = rel_map.filter_by_entity(entity) if entity else rel_map
            output = result.model_dump(mode='python', by_alias=True)
            if error_note:
                output['notes'] = error_note
            return output
        filtered = rel_map.filter_by_entity(entity) if entity else rel_map
        return filtered.model_dump(mode='python', by_alias=True)

    if has_entities and not is_degraded:

        @tool(
            'federated_lookup',
            'Join a mapped entity (see describe_relationships) across two '
            'connections in one call: collect ids in the source, then look '
            'them up in the target. Single hop and a single-column key only. '
            'source_where and target_columns are raw SQL with the same trust '
            'level as the query tool.',
        )
        async def federated_lookup(
            entity: Annotated[str, 'Entity id from describe_relationships.'],
            source_connection_id: Annotated[
                str, 'Connection the ids come from.'
            ],
            source_where: Annotated[
                str,
                'WHERE condition without the keyword, selecting source rows.',
            ],
            target_connection_id: Annotated[
                str, 'Connection the related rows are looked up in.'
            ],
            target_columns: Annotated[
                str | None, 'Column list for the target SELECT (default "*").'
            ] = None,
            limit: Annotated[
                int | None, 'Maximum rows from the target.'
            ] = None,
        ) -> dict[str, Any]:
            _require(entity, 'entity')
            _require(source_where, 'source_where')
            return await run_federated_lookup(
                entity,
                source_connection_id,
                source_where,
                target_connection_id,
                target_columns,
                limit,
            )

        entities = _plural(len(rel_map.entities), 'entity', 'entities')
        joins = _plural(len(rel_map.joins), 'join')
        logger.info(f'federation: on ({entities}, {joins})')
        return 'on'

    assert map_error is not None  # гарантировано из is_degraded
    logger.error(
        f'federation: degraded (could not read the map at {map_error[1]}: '
        f'{map_error[0]}). describe_relationships available, '
        f'federated_lookup disabled.'
    )
    return 'degraded'
