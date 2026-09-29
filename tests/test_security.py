"""Первый барьер: классификация statement'ов и политика записи.

Чистые функции, без соединения с БД. Проверяется то, что решается до
выполнения: класс statement'а, запрет multi-statement, сверка с
эффективной политикой и обрезка строк. Что произойдёт с пропущенным
сюда statement'ом дальше — в test_postgres_adapter.py и в
test_integration_postgres.py, где барьером выступает сама БД.
"""

import pytest

from pgsteward import security
from pgsteward.adapters.base import StatementKind
from pgsteward.config import ConnectionConfig, SecurityPolicy
from pgsteward.errors import StatementNotAllowedError, WriteForbiddenError
from pgsteward.models import EffectiveSecurity
from pgsteward.security import (
    assert_single_statement,
    check_statement,
    classify_statement,
    enforce_max_bytes,
    enforce_max_rows,
    resolve_security,
)


@pytest.mark.parametrize(
    'sql, expected',
    [
        ('SELECT 1', StatementKind.READ),
        ('  select * from t', StatementKind.READ),
        ('WITH x AS (SELECT 1) SELECT * FROM x', StatementKind.READ),
        ('SHOW search_path', StatementKind.READ),
        ('EXPLAIN SELECT 1', StatementKind.READ),
        ('INSERT INTO t VALUES (1)', StatementKind.DML),
        ('update t set a=1', StatementKind.DML),
        ('DELETE FROM t', StatementKind.DML),
        ('CREATE TABLE t (id int)', StatementKind.DDL),
        ('DROP TABLE t', StatementKind.DDL),
        ('TRUNCATE t', StatementKind.DDL),
        # Ключевое слово есть, но ни в одном из наборов.
        ('SET search_path TO public', StatementKind.OTHER),
        ('BEGIN', StatementKind.OTHER),
        # Ключевого слова нет вовсе — отдельная ветка разбора.
        ('12345', StatementKind.OTHER),
        # Только шум перед словом, которого нет.
        ('((( ', StatementKind.OTHER),
        # Ведущая `;` не входит в допустимый шум, и слово за ней не
        # читается: statement уходит в OTHER, то есть отклоняется.
        (';SELECT 1', StatementKind.OTHER),
    ],
)
def test_classify_statement(sql, expected):
    assert classify_statement(sql) == expected


@pytest.mark.parametrize(
    ('keywords', 'expected'),
    [
        (security._READ_KEYWORDS, StatementKind.READ),
        (security._DML_KEYWORDS, StatementKind.DML),
        (security._DDL_KEYWORDS, StatementKind.DDL),
    ],
    ids=['read', 'dml', 'ddl'],
)
def test_every_declared_keyword_is_classified(keywords, expected):
    """Слово, попавшее в набор, но не разбираемое, — тихая дыра.

    `merge`, `call`, `refresh` и остальные редкие раньше не проверялись
    вовсе: перепутанный набор заметить было нечем.
    """
    for keyword in keywords:
        assert classify_statement(f'{keyword} something') == expected
        assert classify_statement(f'  {keyword.upper()} x') == expected


def test_classify_ignores_comments():
    assert classify_statement('-- c\n/* b */ SELECT 1') == StatementKind.READ


def test_classify_skips_leading_parens():
    sql = '((SELECT 1) UNION (SELECT 2))'
    assert classify_statement(sql) == StatementKind.READ


def test_classify_ignores_keywords_inside_literals():
    sql = "SELECT 'drop table t' AS hint"
    assert classify_statement(sql) == StatementKind.READ


# Границы литералов — самая тонкая часть маскировки, и ошибиться в ней
# можно в обе стороны: «съесть» разделитель, приняв multi-statement за
# один, или закрыть литерал рано и отклонить валидный SQL. Ожидания ниже
# сверены с лексером PostgreSQL 17: каждый вход отдан живому серверу как
# prepared statement, и `accept`/`reject` совпадают с тем, видит ли он в
# нём одну команду или несколько.
@pytest.mark.parametrize(
    'sql',
    [
        "SELECT * FROM t WHERE name = 'a;b'",
        'SELECT "col;name" FROM t',
        'SELECT $tag$ a; b $tag$',
        'SELECT $$ a; b $$',
        'SELECT 1 -- ; not a statement',
        'SELECT 1 /* ; not a statement */',
        "SELECT 'it''s ok; really'",
        'SELECT 1;',
        # `\;` внутри E-строки — часть литерала, а не разделитель.
        r"SELECT E'a\'; SELECT 9; --'",
        r"SELECT E'plain'",
        # `\0021` — unicode-escape U&-строки, backslash там не экранирует
        # кавычку, и закрывает её обычная одиночная.
        r"SELECT U&'a\0021b'",
        # `a$b$c` — идентификатор целиком: `$` разрешён не в первой
        # позиции, поэтому dollar-quoted строка здесь не открывается.
        'SELECT 1 AS a$b$c',
        # `aE` — идентификатор, а значит строка обычная, а не E-строка:
        # `\` в ней не экранирует, `''` экранирует, литерал остаётся
        # незакрытым — один (невалидный) statement, отказывает сервер.
        r"SELECT aE'x\'' ; SELECT 1",
        # Вложенный блочный комментарий: закрывающая `*/` внутреннего не
        # завершает внешний, поэтому `;` остаётся закомментированной.
        'SELECT 1 /* a /* b */ ; still a comment */',
        # Незакрытые конструкции: всё до конца строки — часть литерала
        # или комментария, разделителей за ним нет.
        'SELECT 1 /* ; unterminated',
        'SELECT $$ a; b',
        'SELECT "col; unterminated',
        "SELECT 'a; unterminated",
        # Позиционные параметры: `$` стоит после пробела, то есть может
        # открыть тег, но за ним цифра — тега нет, и `$1` остаётся
        # обычным текстом.
        'SELECT $1, $2 FROM t WHERE a = $1',
        # E-строка в самой первой позиции: перед `E` нет ничего, и
        # проверка предыдущего символа не должна выходить за границу.
        r"E'a\'; not a statement'",
    ],
)
def test_assert_single_statement_accepts(sql):
    assert_single_statement(sql)


def test_assert_single_statement_returns_nothing_but_also_raises_nothing():
    """Отдельно от параметризации: тело выше — голый вызов.

    Без этой проверки набор `accepts` проходил бы и на реализации,
    которая не делает ничего.
    """
    with pytest.raises(StatementNotAllowedError):
        assert_single_statement('SELECT 1; SELECT 2')


@pytest.mark.parametrize(
    'sql',
    [
        'SELECT 1; DROP TABLE t',
        "SELECT 'a;b'; DROP TABLE t",
        '   ',
        '-- only a comment',
        # В E-строке `\'` — экранированная кавычка, поэтому следующая
        # одиночная закрывает литерал, и `;` оказывается снаружи.
        r"SELECT E'x\'' ; SELECT 1",
        r"SELECT e'x\'' ; DROP TABLE t",
        r"SELECT (E'x\'') ; SELECT 1",
        # При `standard_conforming_strings=on` (адаптер его пинит)
        # backslash в обычной строке ничего не экранирует.
        r"SELECT 'a\' ; SELECT 1",
        'SELECT 1 AS a$b$c ; SELECT 2',
    ],
)
def test_assert_single_statement_rejects(sql):
    with pytest.raises(StatementNotAllowedError):
        assert_single_statement(sql)


def _sec(read_only=True, allow_dml=False, allow_ddl=False):
    return EffectiveSecurity(
        read_only=read_only,
        allow_dml=allow_dml,
        allow_ddl=allow_ddl,
        max_rows=1000,
        max_bytes=100_000,
        query_timeout=30,
    )


@pytest.mark.parametrize(
    'security_policy',
    [
        _sec(read_only=True),
        _sec(read_only=False, allow_dml=False, allow_ddl=False),
    ],
    ids=['read_only', 'writable'],
)
def test_check_read_always_allowed(security_policy):
    """Возврат `None` — часть контракта: отказ выражается исключением."""
    assert check_statement(StatementKind.READ, security_policy) is None


@pytest.mark.parametrize(
    'kind',
    [StatementKind.DML, StatementKind.DDL],
    ids=['dml', 'ddl'],
)
def test_check_write_on_readonly_forbidden(kind):
    """`read_only` перекрывает `allow_dml`/`allow_ddl`, а не наоборот."""
    with pytest.raises(WriteForbiddenError, match='read-only'):
        check_statement(
            kind, _sec(read_only=True, allow_dml=True, allow_ddl=True)
        )


@pytest.mark.parametrize(
    'kind, allow',
    [
        (StatementKind.DML, 'allow_dml'),
        (StatementKind.DDL, 'allow_ddl'),
    ],
)
def test_check_write_follows_policy(kind, allow):
    check_statement(kind, _sec(read_only=False, **{allow: True}))
    with pytest.raises(WriteForbiddenError):
        check_statement(kind, _sec(read_only=False, **{allow: False}))


def test_check_unclassified_forbidden_before_read_only_check():
    # Сообщение должно объяснять реальную причину, а не «read-only».
    with pytest.raises(StatementNotAllowedError):
        check_statement(StatementKind.OTHER, _sec(read_only=True))


@pytest.mark.parametrize(
    'rows, max_rows, expected_rows, truncated',
    [
        ([[1], [2]], 5, [[1], [2]], False),
        ([[1], [2], [3]], 2, [[1], [2]], True),
        ([[1], [2]], None, [[1], [2]], False),
        ([[1], [2]], -1, [[1], [2]], False),
        ([], 10, [], False),
        # `0` конфигурация больше не допускает, но функция обязана вести
        # себя предсказуемо и на нём: лимит есть, строк под него нет.
        ([[1]], 0, [], True),
        ([], 0, [], False),
    ],
)
def test_enforce_max_rows(rows, max_rows, expected_rows, truncated):
    out, was_truncated = enforce_max_rows(rows, max_rows)
    assert out == expected_rows
    assert was_truncated is truncated


@pytest.mark.parametrize(
    'rows, max_bytes, expected_rows, truncated',
    [
        ([[1], [2]], 100, [[1], [2]], False),
        ([[1], [2], [3]], 2, [[1], [2]], True),
        ([[1], [2]], -1, [[1], [2]], False),
        ([], 10, [], False),
        # Одна строка не влезает в бюджет целиком — и всё равно едет.
        # Пустой ответ агент читает как «данных нет», а `explain_query`
        # получил бы половину JSON вместо плана.
        ([[1], [2]], 0, [[1]], True),
    ],
    ids=['fits', 'cut', 'no-cap', 'empty', 'first-row-always-kept'],
)
def test_enforce_max_bytes(rows, max_bytes, expected_rows, truncated):
    out, was_truncated = enforce_max_bytes(rows, max_bytes, lambda _: 1)
    assert out == expected_rows
    assert was_truncated is truncated


def test_resolve_security_uses_connection_read_only():
    conn = ConnectionConfig(
        id='a',
        read_only=False,
        max_rows=500,
        query_timeout=15,
    )
    sec = resolve_security(conn, SecurityPolicy(allow_dml=True))
    assert sec.read_only is False
    assert sec.allow_dml is True
    assert sec.allow_ddl is False
    assert sec.max_rows == 500
    assert sec.query_timeout == 15


def test_resolve_security_inherits_global_when_connection_unset():
    conn = ConnectionConfig(id='a', read_only=False)
    sec = resolve_security(
        conn, SecurityPolicy(allow_dml=True, allow_ddl=True)
    )
    assert sec.allow_dml is True
    assert sec.allow_ddl is True


def test_resolve_security_connection_override_wins_over_global():
    # Прод должен уметь запретить DML/DDL, даже если глобальная политика
    # разрешает их (например, ради тестового окружения).
    conn = ConnectionConfig(
        id='a-prod',
        read_only=False,
        allow_dml=False,
        allow_ddl=False,
    )
    sec = resolve_security(
        conn, SecurityPolicy(allow_dml=True, allow_ddl=True)
    )
    assert sec.allow_dml is False
    assert sec.allow_ddl is False


def test_resolve_security_connection_can_also_grant_more_than_global():
    conn = ConnectionConfig(
        id='a',
        read_only=False,
        allow_dml=True,
    )
    sec = resolve_security(conn, SecurityPolicy(allow_dml=False))
    assert sec.allow_dml is True
