"""Приведение ответа к JSON и учёт того, что при этом изменилось.

Отдельный модуль, потому что у обхода два потребителя на разных концах
запроса, и разойтись им нельзя. `tooling` сериализует ответ на выходе,
а адаптер при сборке `QueryResult` должен знать про тот же обход две
вещи: сколько значений он подменит на `null` (нота) и сколько байт
займёт строка (бюджет ответа). Пока счёт жил в адаптере отдельной
функцией, он смотрел только верхний уровень строки, и `float8[]` с
`NaN` внутри уезжал как `null` без ноты.

Импортировать `tooling` адаптеру нельзя: `tooling` -> `dependencies` ->
`connections` -> `adapters.postgres` замыкается в цикл. Здесь же нет
ничего, кроме stdlib и pydantic.
"""

import json
import math
from typing import Any

from pydantic import BaseModel


def serialize(value: Any) -> Any:
    """Привести ответ к типам, которые JSON выражает без потерь.

    Обход заходит и внутрь `model_dump`: строки `QueryResult` — это
    значения колонок как есть, и пока ветка `BaseModel` возвращала dict
    без рекурсии, до них не доходила ни одна проверка.

    Два типа JSON выразить не может, и оба приезжают из обычных колонок:

    * `bytes` (`bytea`). `json.dumps(default=str)` дал бы Python-repr
      `"b'\\x89PNG'"` — не читаемо, не обратимо и не похоже ни на одно
      принятое представление. Отдаётся текстовый формат самого
      PostgreSQL, `\\x89504e47`, который можно вернуть в запрос.
    * `nan` и `±inf` (`double precision`, `real`). В JSON литералов для
      них нет: `json.dumps` по умолчанию печатает `NaN`/`Infinity`, и
      получается документ, который строгий парсер не примет. Отдаётся
      `null`, а `QueryResult.notes` говорит, что замена была.
    """
    if isinstance(value, BaseModel):
        return serialize(value.model_dump(mode='python', by_alias=True))
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [serialize(item) for item in value]
    if isinstance(value, bytes | bytearray | memoryview):
        return '\\x' + bytes(value).hex()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def to_json(value: Any) -> str:
    """Сериализовать ответ; `allow_nan=False` — страховка, не режим.

    Нечисловые float снимает `serialize`. Если какой-то путь его
    обойдёт, `json.dumps` упадёт здесь вместо того, чтобы молча выдать
    за JSON документ с литералом `NaN`.
    """
    return json.dumps(
        serialize(value), ensure_ascii=False, default=str, allow_nan=False
    )


def count_non_finite(value: Any) -> int:
    """Сколько значений `serialize` подменит на `null`.

    Контейнерные ветки обязаны повторять `serialize`: подменяет он и
    вложенные значения тоже. В bytes-подобные спускаться при этом
    нельзя — они становятся hex-строкой раньше, чем обход дошёл бы до
    элементов. Инвариант «сколько null'ов подставлено, столько и
    насчитано» закреплён тестом.
    """
    if isinstance(value, dict):
        return sum(count_non_finite(item) for item in value.values())
    if isinstance(value, list | tuple):
        return sum(count_non_finite(item) for item in value)
    return int(isinstance(value, float) and not math.isfinite(value))


def json_size(value: Any) -> int:
    """Размер значения в байтах ровно в том виде, в каком оно уедет.

    Мерить приблизительно нельзя: число попадает в ответ как
    `applied_byte_limit`, и расхождение с реальной длиной документа
    сделало бы его выдумкой. `ensure_ascii=False` оставляет кириллицу
    как есть, поэтому считаются байты UTF-8, а не символы.
    """
    return len(to_json(value).encode('utf-8'))
