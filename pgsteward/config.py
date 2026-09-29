"""Конфигурация подключений и безопасности.

Подключения предзаданы на сервере: агент выбирает по `connection_id`,
секреты не проходят через LLM. Метаданные — env
`PGSTEWARD_DB_CONNECTIONS` в формате CSV `id:prefix`; креды — плоские
`<PREFIX>_PG_*` из окружения процесса или `.env`. Полная справка —
docs/configuration.md.
"""

import logging
import os
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

LOG = logging.getLogger(__name__)

APPLICATION_NAME = 'pgsteward'

# Потолок ответа в байтах. Считать строками нельзя: в них не измеряется
# то, что кончается первым. Тысяча строк `SELECT n` — это 6 КБ, тысяча
# строк таблицы с текстовыми описаниями — два мегабайта, и один и тот
# же `max_rows` разводит их на два порядка. 100 КБ — это примерно 25
# тысяч токенов: много для одного ответа, но не фатально ни для одного
# окна контекста.
DEFAULT_MAX_BYTES = 100_000

# Префикс общий для всех собственных переменных сервера. Имена вроде
# `LOG_LEVEL` слишком ходовые: MCP-клиент передаёт процессу своё
# окружение целиком, и чужое значение молча меняло бы поведение.
ENV_PREFIX = 'PGSTEWARD_'
CONNECTIONS_ENV = f'{ENV_PREFIX}DB_CONNECTIONS'

ENV_FILE_ENV = 'PGSTEWARD_ENV_FILE'
XDG_CONFIG_HOME_ENV = 'XDG_CONFIG_HOME'
CONFIG_DIR_NAME = 'pgsteward'


def user_config_dir() -> Path:
    """Каталог пользовательской конфигурации: XDG, иначе `~/.config`."""
    raw = os.environ.get(XDG_CONFIG_HOME_ENV)
    base = (
        Path(raw.strip()).expanduser()
        if raw and raw.strip()
        else Path.home() / '.config'
    )
    return base / CONFIG_DIR_NAME


def resolve_env_file() -> Path | None:
    """`PGSTEWARD_ENV_FILE`, каталог конфигурации, каталог пакета.

    Рабочий каталог намеренно не участвует: часть MCP-клиентов
    игнорирует `cwd` и стартует из каталога открытого проекта, так что
    чужой `.env` подменял бы подключения.
    """
    explicit = os.environ.get(ENV_FILE_ENV)
    if explicit and explicit.strip():
        return Path(explicit.strip()).expanduser()
    candidates = (
        user_config_dir() / '.env',
        Path(__file__).resolve().parent.parent / '.env',
    )
    return next((c for c in candidates if c.is_file()), None)


def _warn_if_world_readable(env_file: Path) -> None:
    """Предупредить, если env-файл доступен не только владельцу.

    Файл держит пароли, а создают его обычно через `cp`, то есть с
    umask пользователя. Предупреждение, а не отказ: у работающих
    установок нельзя отнимать подключения из-за режима файла. От
    процессов того же пользователя `600` не защищает — см.
    docs/security.md.
    """
    if os.name == 'nt':
        # Права POSIX на Windows не отражают реальный доступ к файлу.
        return
    try:
        mode = env_file.stat().st_mode
    except OSError:
        # Файл только что прочитан; гонка здесь не повод падать.
        return
    if mode & 0o077:
        LOG.warning(
            'env file %s is accessible to other users (mode %s); '
            'it holds database passwords — run: chmod 600 %s',
            env_file,
            format(mode & 0o777, '03o'),
            env_file,
        )


@lru_cache(maxsize=1)
def _dotenv_fallback(env_file: Path | None) -> dict[str, str | None]:
    """Значения `.env`, прочитанные отдельно от `Settings`.

    `pydantic-settings` читает файл только для объявленных полей и не
    мутирует `os.environ`, поэтому плоским кредам нужен свой источник.

    Проверка прав живёт здесь, а не в `resolve_env_file`: та вызывается
    до `configure_logging`, и предупреждение ушло бы мимо формата через
    `logging.lastResort`. Кэш гарантирует ровно одну строку на запуск.
    """
    if env_file is None or not env_file.is_file():
        return {}
    _warn_if_world_readable(env_file)
    return dotenv_values(env_file)


def env_value(name: str) -> str | None:
    """Значение переменной: окружение процесса, затем `.env`.

    Проверяется наличие ключа, а не истинность значения. Пустая строка в
    окружении процесса — это заданное значение: `<PREFIX>_PG_READ_ONLY=`
    в конфигурации MCP-клиента должно снимать значение из `.env`, а не
    проваливаться в него. Пустую строку в смысл «не задано» переводит
    уже `_read_connection`, каждый раз явно.
    """
    if name in os.environ:
        return os.environ[name]
    return _dotenv_fallback(resolve_env_file()).get(name)


class PoolConfig(BaseModel):
    min: int = Field(1, ge=0)
    max: int = Field(5, ge=1)

    @model_validator(mode='after')
    def _validate_bounds(self) -> 'PoolConfig':
        if self.max < self.min:
            raise ValueError(
                f'Pool max ({self.max}) is below pool min ({self.min}).'
            )
        return self


class SecurityPolicy(BaseModel):
    """Глобальные разрешения на запись (read_only задаётся на подключении)."""

    allow_dml: bool = False
    allow_ddl: bool = False


class ConnectionConfig(BaseModel):
    """Предзаданное подключение. Одна база = один `connection_id`."""

    id: str
    env_prefix: str | None = None
    environment: str | None = None
    dsn: SecretStr | None = None
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: SecretStr | None = None
    database: str | None = None
    read_only: bool = True
    allow_dml: bool | None = None
    allow_ddl: bool | None = None
    sslmode: str | None = None
    pool: PoolConfig = Field(default_factory=PoolConfig)
    # `0` отключил бы `statement_timeout` — единственный лимит, который
    # держит сам сервер.
    query_timeout: int = Field(30, ge=1)
    # `-1` снимает лимит.
    max_rows: int = Field(1000, ge=-1)
    # `-1` снимает лимит.
    max_bytes: int = Field(DEFAULT_MAX_BYTES, ge=-1)
    # Переменные, без которых подключением пользоваться нельзя. Пустые
    # кортежи — конфигурация полная. Подключение всё равно создаётся:
    # опечатка в одном префиксе не должна лишать агента остальных баз.
    missing_env: tuple[str, ...] = ()
    # Переменные, значение которых разобрать не удалось. Отдельно от
    # `missing_env`, потому что чинятся они по-разному, и сообщение
    # «не задана» на опечатку в числе только сбивает с толку.
    invalid_env: tuple[str, ...] = ()

    @property
    def is_configured(self) -> bool:
        return not self.missing_env and not self.invalid_env

    @field_validator('max_rows')
    @classmethod
    def _validate_max_rows(cls, value: int) -> int:
        """`0` — самая дорогая опечатка из возможных.

        Во многих инструментах `0` значит «без ограничения», здесь же он
        прошёл бы валидацию и обрезал каждый ответ до нуля строк, а
        `explain_query` сломал бы совсем: план тоже приходит строкой.
        """
        if value == 0:
            raise ValueError(
                'max_rows must be -1 (no cap) or a positive number; '
                '0 would cap every result at zero rows.'
            )
        return value

    @field_validator('max_bytes')
    @classmethod
    def _validate_max_bytes(cls, value: int) -> int:
        """`0` — та же опечатка, что и `max_rows=0`, и та же цена."""
        if value == 0:
            raise ValueError(
                'max_bytes must be -1 (no cap) or a positive number; '
                '0 would cap every result at zero bytes.'
            )
        return value


class AppConfig(BaseModel):
    connections: list[ConnectionConfig] = Field(default_factory=list)
    security: SecurityPolicy = Field(default_factory=SecurityPolicy)

    @model_validator(mode='after')
    def _validate_unique_ids(self) -> 'AppConfig':
        seen: set[str] = set()
        for conn in self.connections:
            if conn.id in seen:
                raise ValueError(f'Duplicate connection id: "{conn.id}".')
            seen.add(conn.id)
        return self


def make_dsn(
    dbname: str,
    host: str,
    port: int,
    user: str,
    password: str | None = None,
    application_name: str = APPLICATION_NAME,
) -> str:
    """Собрать DSN из дискретных полей.

    `asyncpg` принимает DSN только как URI, поэтому части экранируются:
    иначе `@`, `:` или `/` в пароле сломали бы разбор адреса.

    Отсутствующий `password` в DSN не появляется вовсе. Подставлять на
    его место что-нибудь нельзя: без пароля в URI asyncpg обращается к
    `PGPASSWORD` и `.pgpass`, а `trust`, `peer` и внешний IAM-токен
    пароля от клиента не ждут; любая подстановка это перекрыла бы.
    """
    credentials = quote(user, safe='')
    if password is not None:
        credentials = f'{credentials}:{quote(password, safe="")}'
    return (
        f'postgresql://{credentials}'
        f'@{host}:{port}/{quote(dbname, safe="")}'
        f'?application_name={quote(application_name, safe="")}'
    )


_TRUE_VALUES = frozenset({'1', 'true', 'yes', 'on'})


def _env_bool(value: str | None) -> bool | None:
    """`None` — переменная не задана: значение берётся уровнем выше."""
    if value is None or not value.strip():
        return None
    return value.strip().lower() in _TRUE_VALUES


def _env_int(name: str, value: str | None, default: int) -> int:
    """Целое из переменной; в ошибке — её имя.

    Без имени сообщение `invalid literal for int()` не подсказывает,
    что именно править в конфигурации.
    """
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError:
        raise ValueError(
            f'{name} must be an integer, got "{value.strip()}".'
        ) from None


def _parse_env_suffixes(raw: str) -> tuple[str, ...]:
    """CSV суффиксов окружения из `PGSTEWARD_ENV_SUFFIXES`.

    Списка, зашитого в код, нет: без переменной результат пуст, и
    окружение не выводится вовсе. Это рабочее состояние, а не ошибка.
    """
    items = (chunk.strip().lower() for chunk in raw.split(','))
    return tuple(dict.fromkeys(item for item in items if item))


def get_env_suffixes() -> tuple[str, ...]:
    """Суффиксы окружения из настроек процесса."""
    return _parse_env_suffixes(get_settings().env_suffixes)


def _infer_environment(
    conn_id: str, suffixes: tuple[str, ...] | None = None
) -> str | None:
    """Окружение по суффиксу `id`; `None`, если ни один не подошёл.

    Разделитель `-` обязателен, чтобы суффикс не срабатывал на
    случайную подстроку: `airprod` — не прод.
    """
    lowered = conn_id.lower()
    for suffix in get_env_suffixes() if suffixes is None else suffixes:
        if lowered.endswith(f'-{suffix}'):
            return suffix
    return None


def strip_env_suffix(
    conn_id: str, suffixes: tuple[str, ...] | None = None
) -> str:
    """`id` без суффикса окружения; база для карты связей.

    Суффикс снимается, чтобы одна карта обслуживала все окружения. Без
    заданного списка `id` остаётся как есть, и записи карты матчатся
    точным совпадением.
    """
    env = _infer_environment(conn_id, suffixes)
    return conn_id if env is None else conn_id[: -(len(env) + 1)]


# Поле модели -> переменные, из которых оно собирается. Нужно, чтобы
# ошибку валидации назвать именем переменной окружения: `pool.max` в
# сообщении нечего править, `WEB_SHOP_PG_POOL_MAX` — есть.
_FIELD_ENV_SUFFIXES: dict[str, tuple[str, ...]] = {
    'port': ('PORT',),
    'query_timeout': ('QUERY_TIMEOUT',),
    'max_rows': ('MAX_ROWS',),
    'pool': ('POOL_MIN', 'POOL_MAX'),
}


def _validation_targets(
    exc: ValidationError, name_of: Callable[[str], str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(поля модели, переменные окружения), стоящие за ошибками.

    Поля нужны, чтобы вернуть их к дефолтам и собрать подключение из
    остального; переменные — чтобы назвать оператору то, что он может
    править (см. `_FIELD_ENV_SUFFIXES`).

    Пустой `loc` бывает у model-валидаторов: они относятся к объекту
    целиком, конкретное поле не назвать. Тогда полей нет вовсе, и
    указывается весь блок переменных.
    """
    fields: list[str] = []
    names: list[str] = []
    for error in exc.errors():
        field = str(error['loc'][0]) if error['loc'] else ''
        if field:
            fields.append(field)
        suffixes = _FIELD_ENV_SUFFIXES.get(field)
        if suffixes is None:
            suffixes = (field.upper(),) if field else ('*',)
        names.extend(name_of(suffix) for suffix in suffixes)
    # Порядок сохраняется, дубли снимаются: одно поле может дать
    # несколько ошибок, а `pool` — две переменные.
    return tuple(dict.fromkeys(fields)), tuple(dict.fromkeys(names))


def _read_connection(
    conn_id: str, prefix: str, env_suffixes: tuple[str, ...] | None = None
) -> ConnectionConfig:
    """Прочитать плоские `<PREFIX>_PG_*` переменные.

    Ни отсутствие обязательной переменной, ни неразбираемое значение не
    бросают исключение наружу: они запоминаются в `missing_env` и
    `invalid_env`. Опечатка в одном префиксе не должна закрывать агенту
    доступ к остальным базам — и тем более ронять сервер целиком, когда
    обращения к этому подключению может вообще не быть.
    """

    def name_of(suffix: str) -> str:
        return f'{prefix}_PG_{suffix}'

    def env_var(suffix: str) -> str | None:
        return env_value(name_of(suffix))

    invalid: list[str] = []

    def env_int(suffix: str, default: int) -> int:
        try:
            return _env_int(name_of(suffix), env_var(suffix), default)
        except ValueError as exc:
            LOG.warning('connection "%s": %s', conn_id, exc)
            invalid.append(name_of(suffix))
            return default

    dsn = env_var('DSN')
    # Пустое значение приравнивается к незаданному: `<PREFIX>_PG_PASSWORD=`
    # означает «пароля нет», а не «пароль — пустая строка». Решение
    # принимается здесь, а не оставляется драйверу: asyncpg отбрасывает
    # пустой пароль только когда тот пришёл внутри DSN, и при переходе на
    # дискретные аргументы `''` молча уехал бы на сервер как настоящий
    # пароль, отключив `PGPASSWORD`, `.pgpass`, `trust` и `peer`.
    password = env_var('PASSWORD') or None
    host = env_var('HOST')
    user = env_var('USER')
    # Имя БД не выводится из `id`: вывод был бы соглашением об
    # именовании, решающим, к какой базе идёт подключение. HOST и USER
    # обязательны по той же причине, и у DATABASE оснований на
    # исключение нет.
    database = env_var('DATABASE')

    # DSN несёт адрес, роль и базу целиком, поэтому дискретные поля при
    # нём не обязательны — и не используются.
    missing: tuple[str, ...] = ()
    if not dsn:
        missing = tuple(
            name_of(suffix)
            for suffix, value in (
                ('HOST', host),
                ('USER', user),
                ('DATABASE', database),
            )
            if not value
        )
    elif host or user or database:
        LOG.info(
            '%s is set for connection "%s"; the discrete '
            '%s_PG_HOST/PORT/USER/DATABASE variables are ignored',
            name_of('DSN'),
            conn_id,
            prefix,
        )

    read_only = _env_bool(env_var('READ_ONLY'))
    # Пул собирается отдельно: его границы проверяет model-валидатор, и
    # его ошибка не несёт имени поля — сопоставить её с переменной можно
    # только здесь, где обе переменные известны.
    try:
        pool = PoolConfig(
            min=env_int('POOL_MIN', 1), max=env_int('POOL_MAX', 5)
        )
    except ValidationError as exc:
        LOG.warning('connection "%s": %s', conn_id, exc)
        invalid.extend((name_of('POOL_MIN'), name_of('POOL_MAX')))
        pool = PoolConfig()

    fields: dict[str, Any] = {
        'id': conn_id,
        'env_prefix': prefix,
        'environment': _infer_environment(conn_id, env_suffixes),
        'dsn': SecretStr(dsn) if dsn else None,
        'host': host,
        'port': env_int('PORT', 5432),
        'user': user,
        'password': SecretStr(password) if password is not None else None,
        'database': database,
        'read_only': True if read_only is None else read_only,
        'allow_dml': _env_bool(env_var('ALLOW_DML')),
        'allow_ddl': _env_bool(env_var('ALLOW_DDL')),
        'sslmode': env_var('SSLMODE'),
        'pool': pool,
        'query_timeout': env_int('QUERY_TIMEOUT', 30),
        'max_rows': env_int('MAX_ROWS', 1000),
        'max_bytes': env_int('MAX_BYTES', DEFAULT_MAX_BYTES),
    }

    def build(extra_invalid: tuple[str, ...] = ()) -> ConnectionConfig:
        return ConnectionConfig(
            **fields,
            missing_env=missing,
            # Дедупликация здесь одна на оба пути: `pool` даёт две
            # переменные сразу, и они легко складываются с теми, что уже
            # не разобрались по отдельности.
            invalid_env=tuple(dict.fromkeys(tuple(invalid) + extra_invalid)),
        )

    try:
        return build()
    except ValidationError as exc:
        # Значение разобралось как число, но не прошло ограничение
        # (`MAX_ROWS=0`, `QUERY_TIMEOUT=0`, ...). Подключение всё равно
        # создаётся — с непустым `invalid_env`, из-за которого им нельзя
        # воспользоваться, и с дефолтом вместо отвергнутого значения.
        #
        # К дефолтам сбрасываются только провалившиеся поля: остальные
        # разобрались и верны. Иначе `list_connections` показывал бы
        # `read_only: true` там, где оператор написал `false`, —
        # выдуманный ответ вместо честного.
        LOG.warning('connection "%s" is unusable: %s', conn_id, exc)
        failed, names = _validation_targets(exc, name_of)
        for field in failed:
            fields.pop(field, None)
        try:
            return build(names)
        except ValidationError:
            # Сбрасывать было нечего (ошибка model-валидатора) или
            # дефолты тоже не сошлись. Форма ошибки не разобрана —
            # остаётся запись, по которой видно только имя и причину.
            LOG.warning(
                'connection "%s": could not rebuild it from the values '
                'that did parse',
                conn_id,
            )
            return ConnectionConfig(
                id=conn_id,
                env_prefix=prefix,
                environment=_infer_environment(conn_id, env_suffixes),
                missing_env=missing,
                invalid_env=tuple(dict.fromkeys(tuple(invalid) + names)),
            )


def warn_if_tls_is_not_configured(conn: ConnectionConfig) -> None:
    """Предупредить, что шифрование не затребовано.

    Без `sslmode` asyncpg подключается в режиме `prefer`: TLS
    используется, если сервер его предложил, и молча не используется,
    если нет. Понижения не видно ни в ответе, ни в логе драйвера.

    Отказом это не делается: `prefer` — рабочая конфигурация для
    локальной разработки, и отнимать подключения у существующих
    установок из-за умолчания нельзя. Unix-сокет пропускается: шифровать
    там нечего.

    Вызывается при сборке реестра, а не из `connect()`: аудит
    конфигурации должен быть виден там же, где остальные её проблемы,
    то есть в выводе старта.
    """
    if conn.sslmode:
        return
    dsn = conn.dsn
    if dsn is not None:
        raw = dsn.get_secret_value()
        if 'sslmode=' in raw or 'sslrootcert=' in raw:
            return
        try:
            if not urlsplit(raw).hostname:
                return
        except ValueError:
            # Неразбираемый DSN — не повод молчать: пусть предупреждение
            # останется.
            pass
    LOG.warning(
        'connection "%s" does not set sslmode, so asyncpg connects with '
        '"prefer": it falls back to an unencrypted connection without '
        'saying so. Set %s_PG_SSLMODE=require (or stronger) for anything '
        'the traffic leaves the host for.',
        conn.id,
        conn.env_prefix or _default_prefix(conn.id),
    )


def _parse_connections_csv(raw: str) -> list[tuple[str, str]]:
    """CSV `id:prefix`; prefix опционален."""
    entries: list[tuple[str, str]] = []
    for chunk in raw.split(','):
        item = chunk.strip()
        if not item:
            continue
        parts = [p.strip() for p in item.split(':')]
        if len(parts) > 2:
            raise ValueError(
                f'Invalid {CONNECTIONS_ENV} entry "{item}": '
                'format is id:prefix.'
            )
        conn_id = parts[0]
        if not conn_id:
            raise ValueError(f'Empty connection id in {CONNECTIONS_ENV}.')
        prefix = (
            parts[1]
            if len(parts) > 1 and parts[1]
            else _default_prefix(conn_id)
        )
        entries.append((conn_id, prefix))
    return entries


def _default_prefix(conn_id: str) -> str:
    return conn_id.upper().replace('-', '_').replace(' ', '_')


def _load_app_config(
    raw_connections: str,
    security: SecurityPolicy,
    env_suffixes: tuple[str, ...] | None = None,
) -> AppConfig:
    """Пустой список подключений — не ошибка: сервер стартует без баз."""
    suffixes = get_env_suffixes() if env_suffixes is None else env_suffixes
    connections = [
        _read_connection(conn_id, prefix, suffixes)
        for conn_id, prefix in _parse_connections_csv(raw_connections)
    ]
    return AppConfig(connections=connections, security=security)


_LOG_LEVELS = frozenset(
    {'CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG', 'NOTSET'}
)


class Settings(BaseSettings):
    """Плоское отражение переменных окружения.

    Собранного `AppConfig` здесь намеренно нет: объявленное поле
    означает для pydantic-settings ещё одну переменную окружения, и
    любое её значение роняло бы старт при разборе. Сборка — в
    `get_app_config()`.
    """

    log_level: Annotated[str, StringConstraints(to_upper=True)] = 'INFO'
    db_connections: str = ''
    env_suffixes: str = ''
    security_allow_dml: bool = False
    security_allow_ddl: bool = False

    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX, extra='ignore')

    @field_validator('log_level')
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        """Неизвестный уровень иначе роняет `basicConfig` при старте."""
        if value not in _LOG_LEVELS:
            raise ValueError(
                f'{ENV_PREFIX}LOG_LEVEL must be one of '
                f'{", ".join(sorted(_LOG_LEVELS))}, got "{value}".'
            )
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Настройки процесса, читаемые при первом обращении.

    Не на импорте: ошибка в конфигурации должна давать понятное
    сообщение при старте, а не traceback при импорте. `.env`
    подставляется здесь, чтобы `Settings()` в тестах не зависел от
    файла разработчика.
    """
    return Settings(_env_file=resolve_env_file())


@lru_cache(maxsize=1)
def get_app_config() -> AppConfig:
    """Подключения и глобальная политика, собранные из настроек."""
    settings = get_settings()
    return _load_app_config(
        settings.db_connections,
        SecurityPolicy(
            allow_dml=settings.security_allow_dml,
            allow_ddl=settings.security_allow_ddl,
        ),
    )
