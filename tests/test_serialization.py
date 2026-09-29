"""Обход ответа: во что он превращает значения и что при этом считает.

У обхода два потребителя. `tooling` гонит через него всё, что вернул
инструмент; адаптер спрашивает у него же, сколько значений станут
`null` и сколько байт займёт строка. Ошибки здесь не видны ни в SQL, ни
в схеме: наружу просто уезжает испорченный или невалидный JSON — или
число в ответе, которому нечего соответствовать.
"""

import json
import math
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

import pgsteward.serialization as serialization
from pgsteward.models import QueryResult, ServerInfo
from pgsteward.serialization import (
    count_non_finite,
    json_size,
    serialize,
    to_json,
)


def _result(rows: list[list[Any]], columns: list[str]) -> QueryResult:
    return QueryResult(
        columns=columns, rows=rows, row_count=len(rows), kind='read'
    )


def test_serialize_walks_inside_a_model():
    """Значения колонок лежат внутри `model_dump`.

    Пока ветка `BaseModel` возвращала dict без рекурсии, ни одна
    проверка до `rows` не доходила, и весь остальной обход был
    декоративным.
    """
    serialized = serialize(_result([[b'\x00\xff']], ['blob']))

    assert serialized['rows'] == [['\\x00ff']]


def test_bytea_is_rendered_as_postgres_hex():
    """`default=str` дал бы Python-repr `"b'\\x89PNG'"`."""
    payload = json.loads(to_json(_result([[b'\x89PNG']], ['blob'])))

    assert payload['rows'] == [['\\x89504e47']]


def _reject_constant(name: str) -> float:
    raise AssertionError(f'payload contains the non-JSON literal {name}')


@pytest.mark.parametrize(
    'value',
    [float('nan'), float('inf'), float('-inf')],
    ids=['nan', 'inf', '-inf'],
)
def test_non_finite_floats_become_null(value):
    """JSON не имеет литералов для них, а `double precision` — хранит.

    `parse_constant` вызывается ровно на `NaN`/`Infinity`/`-Infinity`;
    поиск подстроки спутал бы литерал с теми же словами в `notes`.
    """
    payload = json.loads(
        to_json(_result([[value]], ['x'])), parse_constant=_reject_constant
    )

    assert payload['rows'] == [[None]]


def test_finite_floats_are_untouched():
    assert json.loads(to_json(_result([[1.5], [0.0]], ['x'])))['rows'] == [
        [1.5],
        [0.0],
    ]


def test_to_json_refuses_to_emit_a_non_finite_literal(monkeypatch):
    """Страховка на случай пути мимо `serialize`.

    В норме он недостижим, поэтому обход подставляется явно. Без
    `allow_nan=False` наружу уехал бы документ с голым литералом `NaN`.
    """
    monkeypatch.setattr(serialization, 'serialize', lambda value: value)

    with pytest.raises(ValueError, match='Out of range float'):
        to_json({'x': math.nan})


def test_serialize_recurses_through_lists_and_dicts():
    value = {
        'items': [ServerInfo(version='16.2'), {'nested': b'\x01'}],
        'tuple': (b'\x02',),
    }

    assert serialize(value) == {
        'items': [{'version': '16.2', 'extra': {}}, {'nested': '\\x01'}],
        'tuple': ['\\x02'],
    }


def _count_nulls(value: Any) -> int:
    """Сколько `null` в уже сериализованной структуре."""
    if isinstance(value, dict):
        return sum(_count_nulls(item) for item in value.values())
    if isinstance(value, list):
        return sum(_count_nulls(item) for item in value)
    return int(value is None)


def test_the_counter_agrees_with_every_null_the_serializer_substitutes():
    """Инвариант между `serialize` и `count_non_finite`.

    Один обход подменяет не-конечные float на `null`, второй считает,
    сколько раз это произошло, и по этому числу собирается нота. Пока
    счётчик жил в адаптере и не спускался в контейнеры, `float8[]` с
    `NaN` внутри уезжал как `null` без ноты.

    В исходных данных настоящих `None` нет намеренно: тогда каждый
    `null` на выходе — работа сериализатора, и числа обязаны сойтись.
    """
    value = {
        'rows': [
            [float('nan'), 1.0, [float('inf'), {'deep': float('-inf')}]],
            [(float('nan'),), b'\xff'],
        ],
        'scalar': float('inf'),
    }

    assert _count_nulls(serialize(value)) == count_non_finite(value) == 5


def test_json_keeps_non_ascii_readable():
    """`ensure_ascii=False`: кириллица не должна ехать как \\uXXXX."""
    assert 'Иванов' in to_json(_result([['Иванов']], ['name']))


def test_types_without_a_json_form_fall_back_to_str():
    raw = to_json(_result([[Decimal('12.30'), date(2026, 9, 24)]], ['a', 'b']))

    assert json.loads(raw)['rows'] == [['12.30', '2026-09-24']]


def test_json_size_is_the_length_of_the_document_that_ships():
    """Бюджет ответа приедет наружу числом; выдумывать его нельзя."""
    value = _result([[1, 'ok']], ['a', 'b'])

    assert json_size(value) == len(to_json(value).encode('utf-8'))


def test_json_size_counts_utf8_bytes_not_characters():
    """`ensure_ascii=False` оставляет кириллицу как есть — по два байта."""
    assert json_size('Иванов') == len('"Иванов"'.encode())
