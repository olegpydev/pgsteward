"""Расширение `federation`: карта связей и cross-database join.

Фикстура — `examples/relationships.example.json` (см. `EXAMPLE_MAP` в
`conftest.py`): тот же файл, который документация предлагает
пользователю как образец, поэтому тесты заодно проверяют, что пример
валиден и не разошёлся со схемой моделей.

Собранный SQL здесь только сверяется со строкой; исполняет его
PostgreSQL в `test_integration_postgres.py`.
"""

import json
import os
from typing import Any

import pytest
from pydantic import ValidationError

import pgsteward.config as config_module
from pgsteward.config import CONFIG_DIR_NAME, ENV_PREFIX
from pgsteward.extensions.federation import (
    RELATIONSHIPS_FILE_ENV,
    ColumnRef,
    RelationshipMap,
    load_relationships,
    quote_ident,
)
from pgsteward.models import QueryResult
from tests.conftest import EXAMPLE_MAP

# Изоляция от карты и `.env` разработчика, подмена XDG-каталога и сброс
# кэша `load_relationships` — в tests/conftest.py.


@pytest.fixture
def example_map(monkeypatch) -> RelationshipMap:
    monkeypatch.setenv(RELATIONSHIPS_FILE_ENV, str(EXAMPLE_MAP))
    load_relationships.cache_clear()
    return load_relationships()


def test_example_file_parses(example_map):
    assert example_map.version == 1
    assert [e.id for e in example_map.entities] == ['customer']


def test_example_joins_chain_orders_to_billing(example_map):
    pairs = {
        (j.from_.connection, j.to.connection)
        for j in example_map.joins
        if j.entity == 'customer'
    }
    assert pairs == {('orders', 'crm'), ('crm', 'billing')}


def test_unset_env_returns_empty_map(monkeypatch):
    monkeypatch.delenv(RELATIONSHIPS_FILE_ENV, raising=False)
    load_relationships.cache_clear()
    assert load_relationships() == RelationshipMap()


def test_user_config_map_is_used(monkeypatch, tmp_path):
    """Без env-переменной карта берётся из каталога конфигурации."""
    monkeypatch.delenv(RELATIONSHIPS_FILE_ENV, raising=False)
    config_dir = tmp_path / 'xdg' / CONFIG_DIR_NAME
    config_dir.mkdir(parents=True)
    (config_dir / 'relationships.json').write_text(
        EXAMPLE_MAP.read_text(encoding='utf-8'), encoding='utf-8'
    )
    load_relationships.cache_clear()
    assert [e.id for e in load_relationships().entities] == ['customer']


def test_env_file_value_is_used(monkeypatch):
    """Путь можно задать в `.env`, а не только в окружении процесса."""
    monkeypatch.delenv(RELATIONSHIPS_FILE_ENV, raising=False)
    monkeypatch.setattr(
        config_module,
        '_dotenv_fallback',
        lambda *_: {RELATIONSHIPS_FILE_ENV: str(EXAMPLE_MAP)},
    )
    load_relationships.cache_clear()
    assert [e.id for e in load_relationships().entities] == ['customer']


def test_missing_file_returns_empty_map(monkeypatch, tmp_path):
    monkeypatch.setenv(RELATIONSHIPS_FILE_ENV, str(tmp_path / 'missing.json'))
    load_relationships.cache_clear()
    assert load_relationships() == RelationshipMap()


def test_invalid_json_raises(monkeypatch, tmp_path):
    bad_file = tmp_path / 'bad.json'
    bad_file.write_text('{not valid json', encoding='utf-8')
    monkeypatch.setenv(RELATIONSHIPS_FILE_ENV, str(bad_file))
    load_relationships.cache_clear()
    with pytest.raises(json.JSONDecodeError):
        load_relationships()


def test_filter_by_entity_narrows_map(example_map):
    filtered = example_map.filter_by_entity('customer')
    assert len(filtered.entities) == 1
    assert filtered.entities[0].id == 'customer'
    assert all(j.entity == 'customer' for j in filtered.joins)


def test_filter_by_unknown_entity_returns_empty_lists(example_map):
    filtered = example_map.filter_by_entity('nonexistent')
    assert filtered.entities == []
    assert filtered.joins == []


def test_find_join_matches_direct_order(example_map):
    joined = example_map.find_join('customer', 'orders', 'crm')
    assert joined is not None
    assert joined.source.connection == 'orders'
    assert joined.source.column == 'customer_id'
    assert joined.target.connection == 'crm'
    assert joined.target.column == 'customer_id'


def test_find_join_matches_reversed_order(example_map):
    """Порядок source/target от агента может не совпадать с картой."""
    joined = example_map.find_join('customer', 'crm', 'orders')
    assert joined is not None
    assert joined.source.connection == 'crm'
    assert joined.target.connection == 'orders'


def test_find_join_strips_env_suffix(example_map, monkeypatch):
    """connection_id агента (`-staging`/`-prod`) сопоставляется с базовым."""
    monkeypatch.setenv(f'{ENV_PREFIX}ENV_SUFFIXES', 'prod,staging')
    joined = example_map.find_join('customer', 'orders-prod', 'crm-staging')
    assert joined is not None
    assert joined.source.connection == 'orders'
    assert joined.target.connection == 'crm'


def test_find_join_returns_none_for_unknown_pair(example_map):
    assert example_map.find_join('customer', 'orders', 'warehouse') is None


def test_find_join_returns_none_for_unknown_entity(example_map):
    assert example_map.find_join('bogus', 'orders', 'crm') is None


@pytest.mark.parametrize(
    ('name', 'quoted'),
    [
        ('group', '"group"'),
        ('Orders', '"Orders"'),
        ('order id', '"order id"'),
        ('a"b', '"a""b"'),
    ],
    ids=['reserved-word', 'mixed-case', 'space', 'embedded-quote'],
)
def test_quote_ident_matches_postgres_rules(name, quoted):
    assert quote_ident(name) == quoted


@pytest.mark.parametrize(
    'name', ['', '\x00', 'a\x00b'], ids=['empty', 'nul', 'embedded-nul']
)
def test_an_unquotable_identifier_is_refused_when_the_map_is_read(name):
    """Такое имя не спасают кавычки, и узнать об этом надо на старте.

    Иначе карта разбирается, федерация включается, а падает потом
    первый же вызов — и оператор узнаёт о поломке от агента.
    `register` переводит отвергнутую карту в `degraded`.
    """
    with pytest.raises(ValidationError):
        ColumnRef(connection='orders', table=name, column='id')


def test_serialization_uses_schema_alias(example_map):
    payload = example_map.model_dump(mode='python', by_alias=True)
    customer = next(e for e in payload['entities'] if e['id'] == 'customer')
    assert 'schema' in customer['keys'][0]
    assert 'schema_' not in customer['keys'][0]


# ============= Tests for run_federated_lookup and registration =============


@pytest.fixture
def mock_adapter(monkeypatch):
    """Подменяем _adapter для тестирования логики federated_lookup."""
    adapters: dict[str, Any] = {}

    async def _fake_adapter(connection_id: str):
        if connection_id not in adapters:
            raise ValueError(f'Unknown connection: {connection_id}')
        return adapters[connection_id]

    monkeypatch.setattr(
        'pgsteward.extensions.federation._adapter', _fake_adapter
    )
    return adapters


def FakeQueryResult(rows, row_count=None, truncated=False):
    """Настоящий `QueryResult`, а не самодельный двойник.

    Разница видна на границе инструмента: объект без pydantic-модели
    `_serialize` пропускает как есть, и `json.dumps(default=str)`
    превращает его в строку с repr вместо вложенного объекта.
    """
    return QueryResult(
        columns=[f'c{i}' for i in range(len(rows[0]) if rows else 0)],
        rows=rows,
        row_count=len(rows) if row_count is None else row_count,
        kind='read',
        truncated=truncated,
    )


class FakeAdapter:
    """Фейковый адаптер БД."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.statements: list[str] = []

    async def execute(self, statement, params=None, limit=None):
        self.statements.append(statement)
        if self.error is not None:
            raise self.error
        return self.result


async def test_run_federated_lookup_collects_and_joins(
    example_map, mock_adapter, monkeypatch
):
    """Тест базовой логики: выбираем id в source, джойним в target."""
    from pgsteward.extensions.federation import run_federated_lookup

    # Два адаптера: source и target
    mock_adapter['orders'] = FakeAdapter(
        FakeQueryResult(rows=[[1], [2], [3]], row_count=3)
    )
    mock_adapter['crm'] = FakeAdapter(
        FakeQueryResult(
            rows=[[1, 'Alice'], [2, 'Bob'], [3, 'Charlie']], row_count=3
        )
    )

    # Монкпэтчим load_relationships
    monkeypatch.setattr(
        'pgsteward.extensions.federation.load_relationships',
        lambda: example_map,
    )

    result = await run_federated_lookup(
        entity='customer',
        source_connection_id='orders',
        source_where='amount > 100',
        target_connection_id='crm',
        target_columns='customer_id, name',
    )

    assert result['entity'] == 'customer'
    assert result['source']['row_count'] == 3
    assert result['target']['row_count'] == 3
    assert len(result['result'].rows) == 3
    assert result['notes'] == []


async def test_generated_sql_quotes_the_identifiers_from_the_map(
    mock_adapter, monkeypatch
):
    """Имя из карты попадает в текст statement'а, а не в параметр.

    Без кавычек `group` — зарезервированное слово, а `Orders`
    приводится к нижнему регистру, и оба ломают вызов синтаксической
    ошибкой уже на живой базе. `source_where` и `target_columns`
    остаются как есть: это сырой SQL по определению фичи.
    """
    from pgsteward.extensions.federation import run_federated_lookup

    rel_map = RelationshipMap.model_validate(
        {
            'entities': [{'id': 'customer', 'description': 'x', 'keys': []}],
            'joins': [
                {
                    'entity': 'customer',
                    'from': {
                        'connection': 'orders',
                        'schema': 'Sales',
                        'table': 'Orders',
                        'column': 'group',
                    },
                    'to': {
                        'connection': 'crm',
                        'schema': 'public',
                        'table': 'customer',
                        'column': 'user',
                    },
                }
            ],
        }
    )
    source = FakeAdapter(FakeQueryResult(rows=[[1]]))
    target = FakeAdapter(FakeQueryResult(rows=[[1, 'Alice']]))
    mock_adapter['orders'] = source
    mock_adapter['crm'] = target
    monkeypatch.setattr(
        'pgsteward.extensions.federation.load_relationships', lambda: rel_map
    )

    await run_federated_lookup(
        entity='customer',
        source_connection_id='orders',
        source_where='amount > 100',
        target_connection_id='crm',
        target_columns='customer_id, name',
    )

    assert source.statements == [
        'SELECT "group" FROM "Sales"."Orders" WHERE amount > 100'
    ]
    assert target.statements == [
        'SELECT customer_id, name FROM "public"."customer" '
        'WHERE "user" = ANY($1)'
    ]


async def test_run_federated_lookup_empty_ids(
    example_map, mock_adapter, monkeypatch
):
    """Если source вернул пусто — target не запрашиваем."""
    from pgsteward.extensions.federation import run_federated_lookup

    mock_adapter['orders'] = FakeAdapter(FakeQueryResult(rows=[]))
    mock_adapter['crm'] = FakeAdapter()  # не должен быть вызван

    monkeypatch.setattr(
        'pgsteward.extensions.federation.load_relationships',
        lambda: example_map,
    )

    result = await run_federated_lookup(
        entity='customer',
        source_connection_id='orders',
        source_where='amount > 1000000',
        target_connection_id='crm',
    )

    assert result['source']['row_count'] == 0
    assert result['result'] is None
    assert result['notes'] == []


async def test_run_federated_lookup_truncated_note(
    example_map, mock_adapter, monkeypatch
):
    """Если source попал в truncated — добавляем note."""
    from pgsteward.extensions.federation import run_federated_lookup

    mock_adapter['orders'] = FakeAdapter(
        FakeQueryResult(rows=[[1], [2]], row_count=2, truncated=True)
    )
    mock_adapter['crm'] = FakeAdapter(
        FakeQueryResult(rows=[[1, 'Alice'], [2, 'Bob']], row_count=2)
    )

    monkeypatch.setattr(
        'pgsteward.extensions.federation.load_relationships',
        lambda: example_map,
    )

    result = await run_federated_lookup(
        entity='customer',
        source_connection_id='orders',
        source_where='true',
        target_connection_id='crm',
    )

    assert result['source']['truncated'] is True
    assert len(result['notes']) == 1
    assert 'incomplete' in result['notes'][0]


async def test_run_federated_lookup_unknown_entity(
    example_map, mock_adapter, monkeypatch
):
    """Неизвестная сущность → ValueError."""
    from pgsteward.extensions.federation import run_federated_lookup

    monkeypatch.setattr(
        'pgsteward.extensions.federation.load_relationships',
        lambda: example_map,
    )

    with pytest.raises(
        ValueError, match='not present in the relationship map'
    ):
        await run_federated_lookup(
            entity='unknown_entity',
            source_connection_id='orders',
            source_where='true',
            target_connection_id='crm',
        )


async def test_run_federated_lookup_unknown_pair(
    example_map, mock_adapter, monkeypatch
):
    """Неизвестная пара connection_id → ValueError."""
    from pgsteward.extensions.federation import run_federated_lookup

    monkeypatch.setattr(
        'pgsteward.extensions.federation.load_relationships',
        lambda: example_map,
    )

    with pytest.raises(ValueError, match='No known join'):
        await run_federated_lookup(
            entity='customer',
            source_connection_id='orders',
            source_where='true',
            target_connection_id='warehouse',  # неизвестное подключение
        )


async def test_an_unlisted_suffix_stops_the_map_from_matching(
    example_map, mock_adapter, monkeypatch
):
    """Суффикс вне `PGSTEWARD_ENV_SUFFIXES` не снимается перед матчингом.

    Список суффиксов надо держать в согласии с `id`, иначе запись карты
    не находится и `federated_lookup` отказывает (configuration.md).

    В списке есть `staging`, которого нет в `id`, и нет `prod`, который
    в `id` есть. Поэтому тест падает и если настройку игнорируют, и
    если её применяют не к той стороне пары.
    """
    from pgsteward.extensions.federation import run_federated_lookup

    monkeypatch.setattr(
        'pgsteward.extensions.federation.load_relationships',
        lambda: example_map,
    )
    monkeypatch.setenv(f'{ENV_PREFIX}ENV_SUFFIXES', 'staging')

    # `prod` в списке не значится, поэтому `orders-prod` остаётся собой
    # и записи `orders` в карте не находит.
    with pytest.raises(ValueError, match='No known join') as exc_info:
        await run_federated_lookup(
            entity='customer',
            source_connection_id='orders-prod',
            source_where='true',
            target_connection_id='crm',
        )
    # Отказ называет пары, которые карта знает, — иначе чинить нечего.
    assert 'orders<->crm' in str(exc_info.value)

    # `staging` в списке есть, и тот же вызов проходит матчинг.
    mock_adapter['orders-staging'] = FakeAdapter(result=FakeQueryResult([[1]]))
    mock_adapter['crm'] = FakeAdapter(result=FakeQueryResult([[1, 'a']]))
    result = await run_federated_lookup(
        entity='customer',
        source_connection_id='orders-staging',
        source_where='true',
        target_connection_id='crm',
    )
    assert result['source']['table'] == 'order'
    assert result['target']['table'] == 'customer'


async def test_run_federated_lookup_refuses_a_zero_limit(
    example_map, mock_adapter, monkeypatch
):
    """`limit` уходит в `execute` того же адаптера, что и у `query`.

    Отказ стоит до карты и до обеих баз: запрос, чей результат заведомо
    обрежется до нуля строк, выполнять незачем.
    """
    from pgsteward.extensions.federation import run_federated_lookup

    monkeypatch.setattr(
        'pgsteward.extensions.federation.load_relationships',
        lambda: example_map,
    )

    with pytest.raises(ValueError, match='0 would return no rows'):
        await run_federated_lookup(
            entity='customer',
            source_connection_id='orders',
            source_where='true',
            target_connection_id='crm',
            limit=0,
        )


async def test_federation_registration_on(monkeypatch, tmp_path):
    """Когда карта есть с entities — регистрируются оба tool'а."""
    from fastmcp import FastMCP

    from pgsteward.extensions.federation import register

    map_file = tmp_path / 'map.json'
    map_file.write_text(EXAMPLE_MAP.read_text(encoding='utf-8'))
    monkeypatch.setenv('PGSTEWARD_RELATIONSHIPS_FILE', str(map_file))
    import pgsteward.extensions.federation as fed

    fed.load_relationships.cache_clear()

    mcp = FastMCP('test')
    status = register(mcp)

    assert status == 'on'
    tools = await mcp.get_tools()
    assert 'describe_relationships' in tools
    assert 'federated_lookup' in tools


async def test_federation_registration_off(monkeypatch):
    """Когда карты нет — оба tool'а не регистрируются."""
    from fastmcp import FastMCP

    from pgsteward.extensions.federation import register

    monkeypatch.delenv('PGSTEWARD_RELATIONSHIPS_FILE', raising=False)
    import pgsteward.extensions.federation as fed

    fed.load_relationships.cache_clear()

    mcp = FastMCP('test')
    status = register(mcp)

    assert status == 'off'
    tools = await mcp.get_tools()
    assert 'describe_relationships' not in tools
    assert 'federated_lookup' not in tools


async def test_federation_registration_degraded(monkeypatch, tmp_path):
    """Когда файл не парсится — только describe_relationships."""
    from fastmcp import FastMCP

    from pgsteward.extensions.federation import register

    map_file = tmp_path / 'bad.json'
    map_file.write_text('{not valid json')
    monkeypatch.setenv('PGSTEWARD_RELATIONSHIPS_FILE', str(map_file))
    import pgsteward.extensions.federation as fed

    fed.load_relationships.cache_clear()

    mcp = FastMCP('test')
    status = register(mcp)

    assert status == 'degraded'
    tools = await mcp.get_tools()
    assert 'describe_relationships' in tools
    assert 'federated_lookup' not in tools


async def test_federation_degraded_has_error_notes(monkeypatch, tmp_path):
    """В режиме degraded describe_relationships возвращает notes с ошибкой."""
    from fastmcp import FastMCP

    from pgsteward.extensions.federation import register

    map_file = tmp_path / 'bad.json'
    map_file.write_text('{invalid json content')
    monkeypatch.setenv('PGSTEWARD_RELATIONSHIPS_FILE', str(map_file))
    import pgsteward.extensions.federation as fed

    fed.load_relationships.cache_clear()

    mcp = FastMCP('test')
    status = register(mcp)
    assert status == 'degraded'

    tools = await mcp.get_tools()
    describe_tool = tools['describe_relationships']
    json_str = await describe_tool.fn()
    result = json.loads(json_str)

    assert result['entities'] == []
    assert result['joins'] == []
    assert 'notes' in result
    assert 'JSONDecodeError' in result['notes']
    assert str(map_file) in result['notes']


async def test_a_schema_valid_file_that_is_not_a_map_also_degrades(
    monkeypatch, tmp_path
):
    """JSON разбирается, схема — нет: вторая половина `except`.

    Раньше degraded достигался только битым JSON, и ветка
    `ValidationError` не выполнялась ни разу.
    """
    from fastmcp import FastMCP

    from pgsteward.extensions.federation import register

    map_file = tmp_path / 'map.json'
    # `entities` обязан быть списком объектов с `id` и `description`.
    map_file.write_text(json.dumps({'version': 1, 'entities': 'customer'}))
    monkeypatch.setenv(RELATIONSHIPS_FILE_ENV, str(map_file))
    load_relationships.cache_clear()

    mcp = FastMCP('test')

    assert register(mcp) == 'degraded'

    tools = await mcp.get_tools()
    result = json.loads(await tools['describe_relationships'].fn())

    assert 'ValidationError' in result['notes']
    assert str(map_file) in result['notes']


async def test_a_file_that_cannot_be_read_degrades_instead_of_crashing(
    monkeypatch, tmp_path
):
    """Файл есть, но это не текст: сервер обязан подняться.

    Раньше `register` ловил только ошибки разбора, и `UnicodeDecodeError`
    из `read_text` выносил исключение наружу через `_lifespan`.
    """
    from fastmcp import FastMCP

    from pgsteward.extensions.federation import register

    map_file = tmp_path / 'map.json'
    map_file.write_bytes(b'\xca\xfe\xba\xbe')
    monkeypatch.setenv(RELATIONSHIPS_FILE_ENV, str(map_file))
    load_relationships.cache_clear()

    mcp = FastMCP('test')

    assert register(mcp) == 'degraded'

    tools = await mcp.get_tools()
    assert 'federated_lookup' not in tools
    result = json.loads(await tools['describe_relationships'].fn())
    assert 'UnicodeDecodeError' in result['notes']


async def test_a_file_without_read_permission_degrades(monkeypatch, tmp_path):
    """`OSError` — та же категория: до содержимого дело не дошло."""
    from fastmcp import FastMCP

    from pgsteward.extensions.federation import register

    map_file = tmp_path / 'map.json'
    map_file.write_text('{}', encoding='utf-8')
    map_file.chmod(0o000)
    if os.access(map_file, os.R_OK):  # pragma: no cover - root ignores mode
        pytest.skip('запущено под root: режим файла не ограничивает чтение')
    monkeypatch.setenv(RELATIONSHIPS_FILE_ENV, str(map_file))
    load_relationships.cache_clear()

    mcp = FastMCP('test')
    try:
        assert register(mcp) == 'degraded'
        result = json.loads(
            await (await mcp.get_tools())['describe_relationships'].fn()
        )
        assert 'PermissionError' in result['notes']
    finally:
        # Иначе tmp_path не удаляется после теста.
        map_file.chmod(0o600)


async def test_describe_relationships_filters_by_entity(monkeypatch):
    """Режим `on` раньше только регистрировался, но не вызывался."""
    from fastmcp import FastMCP

    from pgsteward.extensions.federation import register

    monkeypatch.setenv(RELATIONSHIPS_FILE_ENV, str(EXAMPLE_MAP))
    load_relationships.cache_clear()

    mcp = FastMCP('test')
    register(mcp)
    describe = (await mcp.get_tools())['describe_relationships']

    everything = json.loads(await describe.fn())
    unknown = json.loads(await describe.fn(entity='no-such-entity'))

    assert [e['id'] for e in everything['entities']] == ['customer']
    # Неизвестная сущность — пустая карта, а не ошибка: так агент может
    # позвать инструмент без аргументов и увидеть доступные id.
    assert unknown['entities'] == []
    assert unknown['joins'] == []


async def test_the_federated_lookup_tool_refuses_blank_arguments(
    monkeypatch, example_map, mock_adapter
):
    """Сама обёртка раньше не вызывалась ни разу.

    Проверялась только `run_federated_lookup`, то есть `_require` и
    сериализация ответа оставались непокрытыми.
    """
    from fastmcp import FastMCP

    from pgsteward.extensions.federation import register

    monkeypatch.setenv(RELATIONSHIPS_FILE_ENV, str(EXAMPLE_MAP))
    load_relationships.cache_clear()

    mcp = FastMCP('test')
    register(mcp)
    lookup = (await mcp.get_tools())['federated_lookup']

    with pytest.raises(ValueError, match='Parameter "source_where"'):
        await lookup.fn(
            entity='customer',
            source_connection_id='orders',
            source_where='  ',
            target_connection_id='crm',
        )


async def test_the_federated_lookup_tool_returns_json(
    monkeypatch, example_map, mock_adapter
):
    """Наружу инструмент отдаёт JSON-строку, а не dict."""
    from fastmcp import FastMCP

    from pgsteward.extensions.federation import register

    mock_adapter['orders'] = FakeAdapter(
        FakeQueryResult(rows=[[1], [2]], row_count=2)
    )
    mock_adapter['crm'] = FakeAdapter(
        FakeQueryResult(rows=[[1, 'Alice'], [2, 'Bob']], row_count=2)
    )
    monkeypatch.setenv(RELATIONSHIPS_FILE_ENV, str(EXAMPLE_MAP))
    load_relationships.cache_clear()

    mcp = FastMCP('test')
    register(mcp)
    lookup = (await mcp.get_tools())['federated_lookup']

    raw = await lookup.fn(
        entity='customer',
        source_connection_id='orders',
        source_where='id IN (1, 2)',
        target_connection_id='crm',
    )
    payload = json.loads(raw)

    assert payload['source']['connection_id'] == 'orders'
    assert payload['target']['connection_id'] == 'crm'
    assert payload['result']['rows'] == [[1, 'Alice'], [2, 'Bob']]


async def test_the_lookup_is_audited_with_both_connection_ids(
    monkeypatch, example_map, mock_adapter, audit
):
    """Именно ради этой пары `_audit` перебирает все `*connection_id`."""
    from fastmcp import FastMCP

    from pgsteward.extensions.federation import register

    mock_adapter['orders'] = FakeAdapter(FakeQueryResult(rows=[]))
    monkeypatch.setenv(RELATIONSHIPS_FILE_ENV, str(EXAMPLE_MAP))
    load_relationships.cache_clear()

    mcp = FastMCP('test')
    register(mcp)
    lookup = (await mcp.get_tools())['federated_lookup']

    await lookup.fn(
        entity='customer',
        source_connection_id='orders',
        source_where="ssn = '123-45-6789'",
        target_connection_id='crm',
    )

    # `register` тоже пишет в этот логгер свой статус.
    (line,) = [entry for entry in audit() if entry.startswith('tool=')]
    assert 'source_connection_id=orders' in line
    assert 'target_connection_id=crm' in line
    assert '123-45-6789' not in line
