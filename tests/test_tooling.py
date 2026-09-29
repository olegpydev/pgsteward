"""Выходная граница MCP-слоя: декоратор, аудит, перевод ошибок.

Всё, что инструмент сделал, проходит через `_audit`, и всё, чем он
ответил, — через декоратор. Сам обход ответа проверяется отдельно, в
`test_serialization.py`.
"""

import json
from typing import Any

import pytest
from fastmcp import FastMCP

from pgsteward.config import CONNECTIONS_ENV
from pgsteward.dependencies import get_registry
from pgsteward.errors import (
    ConnectionMisconfiguredError,
    ConnectionNotFoundError,
    WriteForbiddenError,
)
from pgsteward.models import QueryResult
from pgsteward.tooling import (
    _adapter,
    _audit,
    _config,
    _require,
    _require_row_limit,
    make_tool,
)


def _result(rows: list[list[Any]], columns: list[str]) -> QueryResult:
    return QueryResult(
        columns=columns, rows=rows, row_count=len(rows), kind='read'
    )


# --- _require ---------------------------------------------------------


@pytest.mark.parametrize('value', [None, '', '   '], ids=['none', '', 'blank'])
def test_require_rejects_empty_values(value):
    with pytest.raises(ValueError, match='Parameter "table" is required'):
        _require(value, 'table')


def test_require_returns_the_value_unchanged():
    assert _require(' public ', 'schema') == ' public '


# --- _require_row_limit -----------------------------------------------


def test_a_zero_row_limit_is_refused():
    """Та же опечатка, что и `max_rows=0`, и та же цена."""
    with pytest.raises(ValueError, match='0 would return no rows'):
        _require_row_limit(0)


@pytest.mark.parametrize(
    'limit', [None, -1, 1, 1000], ids=['unset', 'negative', 'one', 'many']
)
def test_every_other_row_limit_passes_through(limit):
    """Отрицательное — «своего предела нет, работает max_rows»."""
    assert _require_row_limit(limit) == limit


# --- connection resolution --------------------------------------------


@pytest.fixture
def one_connection(monkeypatch):
    """Реестр с единственным настроенным подключением."""
    monkeypatch.setenv(CONNECTIONS_ENV, 'crm:CRM')
    monkeypatch.setenv('CRM_PG_HOST', '10.0.0.5')
    monkeypatch.setenv('CRM_PG_USER', 'ro')
    monkeypatch.setenv('CRM_PG_DATABASE', 'crm')
    return get_registry()


def test_config_answers_without_opening_a_connection(one_connection):
    """`get_security_config` обязан отвечать до первого подключения."""
    assert _config('crm').host == '10.0.0.5'


@pytest.mark.parametrize('value', ['', '   '], ids=['empty', 'blank'])
def test_a_blank_connection_id_is_refused_before_the_registry(value):
    with pytest.raises(ValueError, match='Parameter "connection_id"'):
        _config(value)


def test_an_unknown_connection_id_lists_the_available_ones(one_connection):
    """Иначе агенту негде узнать, что именно он написал не так."""
    with pytest.raises(ConnectionNotFoundError) as exc_info:
        _config('crn')

    assert 'crm' in str(exc_info.value)


async def test_adapter_refuses_an_unusable_connection(monkeypatch):
    """До пула дело дойти не должно: чинить нужно конфигурацию."""
    monkeypatch.setenv(CONNECTIONS_ENV, 'crm:CRM')
    monkeypatch.delenv('CRM_PG_HOST', raising=False)
    monkeypatch.setenv('CRM_PG_USER', 'ro')
    monkeypatch.setenv('CRM_PG_DATABASE', 'crm')

    with pytest.raises(ConnectionMisconfiguredError, match='CRM_PG_HOST'):
        await _adapter('crm')


@pytest.mark.parametrize(
    ('overrides', 'expected'),
    [
        ({'CRM_PG_HOST': None}, 'CRM_PG_HOST'),
        ({'CRM_PG_MAX_ROWS': '0'}, 'CRM_PG_MAX_ROWS'),
    ],
    ids=['missing', 'invalid'],
)
def test_config_refuses_an_unusable_connection(
    monkeypatch, overrides, expected
):
    """Отказ должен быть и на пути, не доходящем до пула.

    `get_security_config` ходит через `_config` и не подключается. Пока
    проверка жила только в `get_adapter`, он отвечал по такой записи
    политикой из дефолтов модели.
    """
    monkeypatch.setenv(CONNECTIONS_ENV, 'crm:CRM')
    monkeypatch.setenv('CRM_PG_HOST', '10.0.0.5')
    monkeypatch.setenv('CRM_PG_USER', 'ro')
    monkeypatch.setenv('CRM_PG_DATABASE', 'crm')
    for name, value in overrides.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    with pytest.raises(ConnectionMisconfiguredError, match=expected):
        _config('crm')


# --- audit ------------------------------------------------------------


def test_audit_records_every_connection_id(audit):
    """`federated_lookup` работает с парой баз, а не с одной."""
    _audit(
        'federated_lookup',
        0.0,
        {
            'source_connection_id': 'orders',
            'target_connection_id': 'billing',
            'source_where': "ssn = '123-45-6789'",
        },
    )

    (line,) = audit()
    assert 'source_connection_id=orders' in line
    assert 'target_connection_id=billing' in line
    assert '123-45-6789' not in line
    assert 'ssn' not in line


def test_audit_omits_query_fields_for_other_results(audit):
    """`kind`/`row_count` осмысленны только для `QueryResult`."""
    _audit('describe_table', 0.0, {'connection_id': 'crm'}, result={'a': 1})

    (line,) = audit()
    assert 'tool=describe_table' in line
    assert 'outcome=ok' in line
    assert 'kind=' not in line
    assert 'row_count=' not in line


def test_audit_reports_the_error_class_not_its_message(audit):
    _audit(
        'query',
        0.0,
        {'connection_id': 'crm'},
        error=WriteForbiddenError('host=10.0.0.5 role=admin'),
    )

    (line,) = audit()
    assert 'error=WriteForbiddenError' in line
    assert '10.0.0.5' not in line


# --- make_tool --------------------------------------------------------


@pytest.fixture
def registered():
    """Инструмент, зарегистрированный на изолированном FastMCP."""
    mcp = FastMCP('test')
    tool = make_tool(mcp)

    def register(fn, name='probe', description='A probe.'):
        return tool(name, description)(fn)

    return register


async def test_tool_declares_a_string_return_type(registered):
    """FastMCP строит схему по сигнатуре, а наружу всегда едет JSON."""

    async def probe(connection_id: str) -> QueryResult:
        return _result([[1]], ['id'])

    handler = registered(probe)

    assert handler.fn.__signature__.return_annotation is str
    assert handler.fn.__annotations__['return'] is str
    # `functools.wraps` проставил бы `__wrapped__`, и FastMCP взял бы
    # сигнатуру исходной функции вместе с её типом возврата.
    assert not hasattr(handler.fn, '__wrapped__')


async def test_tool_keeps_the_parameter_signature(registered):
    async def probe(connection_id: str, schema: str | None = None) -> dict:
        return {}

    handler = registered(probe)
    params = handler.fn.__signature__.parameters

    assert list(params) == ['connection_id', 'schema']
    assert params['schema'].default is None


async def test_tool_returns_serialized_json(registered):
    async def probe() -> QueryResult:
        return _result([[b'\x01', float('nan')]], ['blob', 'x'])

    handler = registered(probe)

    assert json.loads(await handler.fn())['rows'] == [['\\x01', None]]


async def test_domain_error_becomes_value_error_without_a_cause(registered):
    async def probe() -> None:
        raise WriteForbiddenError('This connection is read-only.')

    handler = registered(probe)

    with pytest.raises(ValueError, match='read-only') as exc_info:
        await handler.fn()
    assert exc_info.value.__cause__ is None


async def test_a_serialization_failure_is_audited(registered, audit):
    """Отказ сериализации — такой же исход вызова, как ошибка запроса.

    Пока `_to_json` стоял после журнала, такой вызов уходил наружу
    вообще без строки аудита.
    """

    async def probe() -> dict:
        # Ключ-кортеж проходит `_serialize` и падает в `json.dumps`.
        return {('composite', 'key'): 1}

    handler = registered(probe)

    with pytest.raises(TypeError):
        await handler.fn()

    (line,) = audit()
    assert 'outcome=error' in line
    assert 'error=TypeError' in line
