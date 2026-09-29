"""Классификация SQL и проверка политики записи — первый барьер.

Второй барьер и единственная гарантия — READ ONLY транзакция на стороне
БД: `WITH ... (DELETE ... RETURNING)` и `EXPLAIN ANALYZE INSERT` по
первому слову выглядят чтением, и отклонить их может только движок.
"""

import re
from collections.abc import Callable
from typing import TypeVar

from pgsteward.adapters.base import StatementKind
from pgsteward.config import ConnectionConfig, SecurityPolicy
from pgsteward.errors import StatementNotAllowedError, WriteForbiddenError
from pgsteward.models import EffectiveSecurity

# Ключевые слова по классам. Классификация — по первому значащему слову.
_READ_KEYWORDS = frozenset(
    {
        'select',
        'with',
        'show',
        'explain',
        'values',
        'table',
    }
)
_DML_KEYWORDS = frozenset(
    {
        'insert',
        'update',
        'delete',
        'merge',
        'upsert',
        'copy',
        'call',
    }
)
_DDL_KEYWORDS = frozenset(
    {
        'create',
        'alter',
        'drop',
        'truncate',
        'rename',
        'comment',
        'grant',
        'revoke',
        'vacuum',
        'analyze',
        'reindex',
        'cluster',
        'refresh',
    }
)

# Открывающий тег dollar-quoted строки: `$$` или `$tag$`.
_DOLLAR_TAG_RE = re.compile(r'\$(?:[A-Za-z_]\w*)?\$')

# Первому значащему слову могут предшествовать пробелы и скобки
# (`(SELECT ...) UNION (SELECT ...)`).
_LEADING_NOISE_RE = re.compile(r'^[\s(]+')

_Row = TypeVar('_Row')


def _is_ident_char(char: str) -> bool:
    """Символ, который может продолжать идентификатор PostgreSQL.

    `isalnum` намеренно, а не `[A-Za-z0-9]`: PostgreSQL допускает в
    идентификаторах буквы вне ASCII.
    """
    return char.isalnum() or char in '_$'


def _starts_escape_string(sql: str, quote_index: int) -> bool:
    """Открывается ли по `quote_index` строка вида `E'...'`.

    Backslash экранирует только в `E'...'`; в обычных строках при
    `standard_conforming_strings=on` он — обычный символ. Адаптер
    пинит эту настройку, поэтому допущение держится независимо от
    дефолтов сервера, базы и роли.

    Префикс `E` обязан начинать токен: в `SELECT aE'x'` PostgreSQL
    жадно читает `aE` как идентификатор, и строка получается обычной.
    """
    if quote_index == 0 or sql[quote_index - 1] not in 'Ee':
        return False
    return quote_index < 2 or not _is_ident_char(sql[quote_index - 2])


def _mask_noise(sql: str) -> str:
    """Заменить комментарии и литералы пробелами, сохранив структуру.

    Чтобы `;` и ключевые слова внутри строк и комментариев не влияли на
    разбор: `SELECT 'a;b'` — один statement, а не два.
    """
    out: list[str] = []
    index = 0
    length = len(sql)

    while index < length:
        pair = sql[index : index + 2]

        if pair == '--':
            newline = sql.find('\n', index)
            index = length if newline == -1 else newline
            out.append(' ')
            continue

        if pair == '/*':
            index = _skip_block_comment(sql, index)
            out.append(' ')
            continue

        char = sql[index]
        if char == "'":
            index = _skip_quoted(
                sql,
                index,
                char,
                backslash_escapes=_starts_escape_string(sql, index),
            )
            out.append(' ')
            continue

        if char == '"':
            # В quoted identifier backslash ничего не экранирует.
            index = _skip_quoted(sql, index, char, backslash_escapes=False)
            out.append(' ')
            continue

        # `$` открывает dollar-quoted строку, только если не продолжает
        # идентификатор: `a$b$c` — валидное имя целиком, а не `a` плюс
        # строка `$b$`. Правило чуть строже PostgreSQL (в `1$$a$$` тот
        # видит строку, а мы — нет), но расходится лишь на невалидном
        # SQL и лишь в сторону отказа.
        if char == '$' and (index == 0 or not _is_ident_char(sql[index - 1])):
            tag = _DOLLAR_TAG_RE.match(sql, index)
            if tag is not None:
                closing = sql.find(tag.group(0), tag.end())
                index = (
                    length if closing == -1 else closing + len(tag.group(0))
                )
                out.append(' ')
                continue

        out.append(char)
        index += 1

    return ''.join(out)


def _skip_block_comment(sql: str, index: int) -> int:
    """Позиция за концом блочного комментария (учитывая вложенность)."""
    depth = 0
    length = len(sql)
    while index < length:
        pair = sql[index : index + 2]
        if pair == '/*':
            depth += 1
            index += 2
        elif pair == '*/':
            depth -= 1
            index += 2
            if depth == 0:
                return index
        else:
            index += 1
    return length


def _skip_quoted(
    sql: str, index: int, quote: str, *, backslash_escapes: bool
) -> int:
    r"""Позиция за закрывающей кавычкой; `''` внутри — экранированная.

    `backslash_escapes` включается для `E'...'`, где `\'` — это тоже
    кавычка. Без него маскировка `E'a\''` считала бы `''` экраном и
    съедала текст за строкой вместе с `;`.
    """
    index += 1
    length = len(sql)
    while index < length:
        char = sql[index]
        if backslash_escapes and char == '\\':
            index += 2
            continue
        if char != quote:
            index += 1
            continue
        if sql[index + 1 : index + 2] == quote:
            index += 2
            continue
        return index + 1
    return length


def classify_statement(sql: str) -> StatementKind:
    """Определить класс по первому значащему ключевому слову."""
    cleaned = _LEADING_NOISE_RE.sub('', _mask_noise(sql))
    match = re.match(r'[a-zA-Z]+', cleaned)
    if match is None:
        return StatementKind.OTHER

    keyword = match.group(0).lower()
    if keyword in _READ_KEYWORDS:
        return StatementKind.READ
    if keyword in _DML_KEYWORDS:
        return StatementKind.DML
    if keyword in _DDL_KEYWORDS:
        return StatementKind.DDL
    return StatementKind.OTHER


def assert_single_statement(sql: str) -> None:
    """Запретить multi-statement (обход политики, форма QueryResult)."""
    statements = [part for part in _mask_noise(sql).split(';') if part.strip()]
    if len(statements) > 1:
        raise StatementNotAllowedError(
            'Only one SQL statement is allowed per call. '
            'Send several statements as separate calls.'
        )
    if not statements:
        raise StatementNotAllowedError('The statement is empty.')


def check_statement(kind: StatementKind, security: EffectiveSecurity) -> None:
    """Проверить statement против эффективной политики."""
    if kind is StatementKind.READ:
        return

    if kind is StatementKind.OTHER:
        raise StatementNotAllowedError(
            'The statement could not be classified, so it was refused. '
            'Rewrite it as a plain SELECT, WITH, SHOW, EXPLAIN, VALUES or '
            'TABLE statement.'
        )

    if security.read_only:
        raise WriteForbiddenError(
            'This connection is read-only; writes are refused.'
        )

    if kind is StatementKind.DML and not security.allow_dml:
        raise WriteForbiddenError(
            'DML statements are forbidden by the current policy.'
        )

    if kind is StatementKind.DDL and not security.allow_ddl:
        raise WriteForbiddenError(
            'DDL statements are forbidden by the current policy.'
        )


def enforce_max_rows(
    rows: list[_Row], max_rows: int | None
) -> tuple[list[_Row], bool]:
    """Обрезать строки на сервере; вернуть (rows, truncated)."""
    if max_rows is None or max_rows < 0:
        return rows, False
    if len(rows) > max_rows:
        return rows[:max_rows], True
    return rows, False


def enforce_max_bytes(
    rows: list[_Row], max_bytes: int, sizer: Callable[[_Row], int]
) -> tuple[list[_Row], bool]:
    """Обрезать строки по размеру; вернуть (rows, truncated).

    Первая строка остаётся всегда, даже если одна уже не влезает в
    бюджет. Пустой ответ здесь был бы той же ошибкой, что `max_rows=0`:
    агент прочитал бы его как «данных нет». На `explain_query` это к
    тому же обязательно — план приходит одной строкой, и обрезать её
    значит отдать сломанный JSON вместо плана.

    Размер меряется снаружи: считать его должен тот же обход, который
    потом сериализует ответ, иначе `applied_byte_limit` перестаёт
    что-либо значить.
    """
    if max_bytes < 0:
        return rows, False
    kept: list[_Row] = []
    size = 0
    for row in rows:
        row_size = sizer(row)
        if kept and size + row_size > max_bytes:
            return kept, True
        kept.append(row)
        size += row_size
    return kept, False


def resolve_security(
    conn: ConnectionConfig, policy: SecurityPolicy
) -> EffectiveSecurity:
    """Эффективная политика: connection переопределяет глобальную.

    `allow_dml`/`allow_ddl` берутся с подключения, если заданы явно,
    иначе из глобальной политики. Это позволяет держать в одном
    процессе несколько окружений с разной политикой записи.
    """
    return EffectiveSecurity(
        read_only=conn.read_only,
        allow_dml=(
            conn.allow_dml if conn.allow_dml is not None else policy.allow_dml
        ),
        allow_ddl=(
            conn.allow_ddl if conn.allow_ddl is not None else policy.allow_ddl
        ),
        max_rows=conn.max_rows,
        max_bytes=conn.max_bytes,
        query_timeout=conn.query_timeout,
    )
