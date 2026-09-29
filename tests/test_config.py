import logging
import os

import pytest
from pydantic import SecretStr, ValidationError

import pgsteward.config as config_module
from pgsteward.config import (
    CONFIG_DIR_NAME,
    ENV_FILE_ENV,
    ENV_PREFIX,
    XDG_CONFIG_HOME_ENV,
    AppConfig,
    ConnectionConfig,
    SecurityPolicy,
    Settings,
    _dotenv_fallback,
    _load_app_config,
    _parse_env_suffixes,
    get_env_suffixes,
    make_dsn,
    resolve_env_file,
    strip_env_suffix,
    user_config_dir,
    warn_if_tls_is_not_configured,
)
from pgsteward.errors import ConnectionMisconfiguredError

# Изоляция от `.env` разработчика и сброс кэшей — в tests/conftest.py.

# --- env loading ------------------------------------------------------


def _set_pg_env(monkeypatch, prefix, **overrides):
    defaults = {
        'HOST': '10.0.0.5',
        'PORT': '5432',
        'DATABASE': 'db',
        'USER': 'ro_user',
        'PASSWORD': 'secret',
    }
    defaults.update(overrides)
    for name, value in defaults.items():
        monkeypatch.setenv(f'{prefix}_PG_{name}', value)


def test_load_from_env_reads_pg_block(monkeypatch):
    _set_pg_env(monkeypatch, 'WEB_SHOP', DATABASE='web_shop')
    sec = AppConfig().security
    cfg = _load_app_config('web-shop:WEB_SHOP', sec)
    conn = cfg.connections[0]
    assert conn.id == 'web-shop'
    assert conn.env_prefix == 'WEB_SHOP'
    assert conn.host == '10.0.0.5'
    assert conn.port == 5432
    assert conn.database == 'web_shop'
    assert conn.user == 'ro_user'
    assert conn.read_only is True


def test_load_from_env_read_only_override(monkeypatch):
    _set_pg_env(monkeypatch, 'CRM', READ_ONLY='false')
    sec = AppConfig().security
    cfg = _load_app_config('crm:CRM', sec)
    assert cfg.connections[0].read_only is False


def test_load_from_env_optional_fields(monkeypatch):
    _set_pg_env(
        monkeypatch,
        'CRM',
        SSLMODE='require',
        QUERY_TIMEOUT='15',
        MAX_ROWS='500',
        MAX_BYTES='50000',
    )
    sec = AppConfig().security
    cfg = _load_app_config('crm:CRM', sec)
    conn = cfg.connections[0]
    assert conn.sslmode == 'require'
    assert conn.query_timeout == 15
    assert conn.max_rows == 500
    assert conn.max_bytes == 50000


def test_load_from_env_missing_fields_default(monkeypatch):
    # Ни одна переменная не задана — поля None/дефолты, старт не падает.
    sec = AppConfig().security
    cfg = _load_app_config('crm:CRM', sec)
    conn = cfg.connections[0]
    assert conn.host is None
    assert conn.port == 5432
    assert conn.password is None
    assert conn.read_only is True


# --- required variables -----------------------------------------------


def test_connection_without_address_is_marked_unusable():
    """Опечатка в префиксе не должна выглядеть как рабочая база.

    Пока host и user подставлялись по умолчанию, такое подключение
    молча уходило на 127.0.0.1 под ролью postgres.
    """
    cfg = _load_app_config('crm:CRM', AppConfig().security)
    conn = cfg.connections[0]

    assert conn.is_configured is False
    assert conn.missing_env == (
        'CRM_PG_HOST',
        'CRM_PG_USER',
        'CRM_PG_DATABASE',
    )


def test_one_broken_connection_leaves_the_others_usable(monkeypatch):
    """Флот баз не должен падать целиком из-за одного префикса."""
    _set_pg_env(monkeypatch, 'CRM')
    cfg = _load_app_config('crm:CRM,web-shop:WEB_SHOP', AppConfig().security)
    by_id = {c.id: c for c in cfg.connections}

    assert by_id['crm'].is_configured is True
    assert by_id['web-shop'].is_configured is False


def test_password_is_optional(monkeypatch):
    """`.pgpass`, `trust`, `peer` и IAM-токены пароля не требуют."""
    _set_pg_env(monkeypatch, 'CRM')
    monkeypatch.delenv('CRM_PG_PASSWORD')

    cfg = _load_app_config('crm:CRM', AppConfig().security)
    conn = cfg.connections[0]

    assert conn.is_configured is True
    assert conn.password is None


def test_empty_password_in_env_file_means_no_password(monkeypatch):
    """`<PREFIX>_PG_PASSWORD=` — ровно та форма, что в `.env.example`.

    Пустое значение доходит сюда именно из файла: в окружении процесса
    его раньше снимает `or` внутри `env_value`, а `dotenv_values` на
    `KEY=` отдаёт `''`.
    """
    _set_pg_env(monkeypatch, 'CRM')
    monkeypatch.delenv('CRM_PG_PASSWORD')
    monkeypatch.setattr(
        config_module, '_dotenv_fallback', lambda *_: {'CRM_PG_PASSWORD': ''}
    )

    cfg = _load_app_config('crm:CRM', AppConfig().security)
    conn = cfg.connections[0]

    assert conn.is_configured is True
    assert conn.password is None


def test_dsn_replaces_the_discrete_fields(monkeypatch):
    """Полный DSN — единственный способ задать сокет или sslrootcert."""
    monkeypatch.setenv(
        'CRM_PG_DSN', 'postgresql://u@/db?host=/var/run/postgresql'
    )

    cfg = _load_app_config('crm:CRM', AppConfig().security)
    conn = cfg.connections[0]

    assert conn.is_configured is True
    assert conn.dsn is not None
    assert 'var/run/postgresql' in conn.dsn.get_secret_value()


def test_pool_bounds_come_from_env(monkeypatch):
    _set_pg_env(monkeypatch, 'CRM', POOL_MIN='2', POOL_MAX='8')

    cfg = _load_app_config('crm:CRM', AppConfig().security)

    assert cfg.connections[0].pool.min == 2
    assert cfg.connections[0].pool.max == 8


@pytest.mark.parametrize(
    ('overrides', 'expected'),
    [
        ({'PORT': 'abc'}, ['CRM_PG_PORT']),
        ({'QUERY_TIMEOUT': '0'}, ['CRM_PG_QUERY_TIMEOUT']),
        ({'MAX_ROWS': '-5'}, ['CRM_PG_MAX_ROWS']),
        # `0` — самая дорогая опечатка: во многих инструментах это
        # «без ограничения», здесь он обрезал бы каждый ответ до нуля.
        ({'MAX_ROWS': '0'}, ['CRM_PG_MAX_ROWS']),
        # То же и по той же причине — только в байтах.
        ({'MAX_BYTES': '0'}, ['CRM_PG_MAX_BYTES']),
        ({'MAX_BYTES': '-5'}, ['CRM_PG_MAX_BYTES']),
        # Границы проверяет model-валидатор: виновата пара, не одна из.
        (
            {'POOL_MIN': '9', 'POOL_MAX': '2'},
            ['CRM_PG_POOL_MIN', 'CRM_PG_POOL_MAX'],
        ),
    ],
    ids=[
        'not_a_number',
        'timeout_disabled',
        'max_rows_negative',
        'max_rows_zero',
        'max_bytes_zero',
        'max_bytes_negative',
        'pool_inverted',
    ],
)
def test_invalid_value_disables_only_its_own_connection(
    monkeypatch, overrides, expected
):
    """Опечатка не должна ронять сервер целиком.

    Раньше исключение поднималось из `_load_app_config` в
    `get_app_config()` и дальше в lifespan, то есть одна опечатка
    закрывала агенту все базы сразу — при том что обращения к этому
    подключению могло и не быть.
    """
    _set_pg_env(monkeypatch, 'CRM', **overrides)
    _set_pg_env(monkeypatch, 'WEB_SHOP')

    cfg = _load_app_config('crm:CRM,web-shop:WEB_SHOP', AppConfig().security)
    crm, web_shop = cfg.connections

    assert list(crm.invalid_env) == expected
    assert crm.is_configured is False
    assert web_shop.is_configured is True


def test_the_error_names_the_variable_not_the_field(monkeypatch):
    """`max_rows` в сообщении править нечего, `CRM_PG_MAX_ROWS` — есть."""
    _set_pg_env(monkeypatch, 'CRM', MAX_ROWS='0')

    conn = _load_app_config('crm:CRM', AppConfig().security).connections[0]
    rendered = str(
        ConnectionMisconfiguredError('crm', conn.missing_env, conn.invalid_env)
    )

    assert 'CRM_PG_MAX_ROWS' in rendered
    assert 'has a value that could not be used' in rendered
    # Отвергнутое значение наружу не идёт: в нём может быть что угодно.
    assert 'not set' not in rendered


def test_missing_and_invalid_are_reported_separately(monkeypatch):
    """«Не задана» про опечатку в числе только уводит в сторону."""
    monkeypatch.setenv('CRM_PG_USER', 'ro')
    monkeypatch.setenv('CRM_PG_DATABASE', 'crm')
    monkeypatch.setenv('CRM_PG_MAX_ROWS', 'abc')

    conn = _load_app_config('crm:CRM', AppConfig().security).connections[0]
    rendered = str(
        ConnectionMisconfiguredError('crm', conn.missing_env, conn.invalid_env)
    )

    assert conn.missing_env == ('CRM_PG_HOST',)
    assert conn.invalid_env == ('CRM_PG_MAX_ROWS',)
    assert 'CRM_PG_HOST is not set' in rendered
    assert 'CRM_PG_MAX_ROWS has a value that could not be used' in rendered


def test_only_the_rejected_field_falls_back_to_its_default(monkeypatch):
    """Остальные значения разобрались и остаются как есть.

    Отвергнутое при этом не хранится — пользоваться подключением всё
    равно нельзя.
    """
    _set_pg_env(
        monkeypatch,
        'CRM',
        MAX_ROWS='0',
        READ_ONLY='false',
        QUERY_TIMEOUT='7',
        POOL_MAX='3',
    )

    conn = _load_app_config('crm:CRM', AppConfig().security).connections[0]

    assert conn.is_configured is False
    assert conn.invalid_env == ('CRM_PG_MAX_ROWS',)
    assert conn.max_rows == 1000
    assert conn.read_only is False
    assert conn.query_timeout == 7
    assert conn.pool.max == 3
    assert conn.host == '10.0.0.5'


def test_broken_pool_bounds_name_both_variables(monkeypatch):
    """Границы пула проверяет model-валидатор: виновата пара, не одна."""
    _set_pg_env(monkeypatch, 'CRM', POOL_MIN='9', POOL_MAX='2')

    conn = _load_app_config('crm:CRM', AppConfig().security).connections[0]

    assert conn.is_configured is False
    assert conn.invalid_env == ('CRM_PG_POOL_MIN', 'CRM_PG_POOL_MAX')
    assert conn.pool.min == 1
    assert conn.pool.max == 5


def test_invalid_env_never_repeats_a_variable(monkeypatch):
    """Одна переменная может провалиться дважды: разбором и границей."""
    _set_pg_env(monkeypatch, 'CRM', POOL_MIN='abc', POOL_MAX='0')

    conn = _load_app_config('crm:CRM', AppConfig().security).connections[0]

    assert conn.invalid_env == ('CRM_PG_POOL_MIN', 'CRM_PG_POOL_MAX')


def test_database_is_required(monkeypatch):
    """Имя БД не выводится из `id`: это выбор цели подключения.

    Пока оно выводилось, `orders-prod` без переменной уезжал в базу
    `orders` — тихая ошибка, если рядом лежит `orders_v2`.
    """
    _set_pg_env(monkeypatch, 'WEB_SHOP')
    monkeypatch.delenv('WEB_SHOP_PG_DATABASE')

    conn = _load_app_config(
        'web-shop:WEB_SHOP', AppConfig().security
    ).connections[0]

    assert conn.is_configured is False
    assert conn.missing_env == ('WEB_SHOP_PG_DATABASE',)
    assert conn.database is None


def test_database_is_not_required_when_a_dsn_is_set(monkeypatch):
    """DSN несёт имя базы сам; дискретные поля при нём не нужны."""
    monkeypatch.setenv(
        'WEB_SHOP_PG_DSN', 'postgresql://ro@db.example.com/web_shop'
    )

    conn = _load_app_config(
        'web-shop:WEB_SHOP', AppConfig().security
    ).connections[0]

    assert conn.is_configured is True
    assert conn.missing_env == ()


def test_database_comes_from_its_own_variable(monkeypatch):
    _set_pg_env(monkeypatch, 'CRM', DATABASE='custom_name')
    sec = AppConfig().security
    cfg = _load_app_config('crm:CRM', sec)
    assert cfg.connections[0].database == 'custom_name'


# --- environment suffixes ---------------------------------------------


@pytest.mark.parametrize(
    'conn_id, expected',
    [
        ('web-shop', None),
        ('web-shop-staging', 'staging'),
        ('web-shop-prod', 'prod'),
        ('airprod', None),
        # Суффикс вне заданного списка — метки нет, а не «prod».
        ('web-shop-uat', None),
    ],
)
def test_environment_inferred_from_id(monkeypatch, conn_id, expected):
    monkeypatch.setenv(f'{ENV_PREFIX}ENV_SUFFIXES', 'prod,staging')
    sec = AppConfig().security
    cfg = _load_app_config(f'{conn_id}:PREFIX', sec)
    assert cfg.connections[0].environment == expected


@pytest.mark.parametrize(
    'conn_id, expected',
    [
        ('web-shop-uat', 'uat'),
        ('web-shop-dev', 'dev'),
        # Дефолтный список заменяется, а не дополняется.
        ('web-shop-prod', None),
    ],
)
def test_environment_suffixes_are_configurable(monkeypatch, conn_id, expected):
    """Единого соглашения об именовании нет; список задаёт оператор."""
    monkeypatch.setenv(f'{ENV_PREFIX}ENV_SUFFIXES', 'uat, DEV')
    sec = AppConfig().security
    cfg = _load_app_config(f'{conn_id}:PREFIX', sec)
    assert cfg.connections[0].environment == expected


@pytest.mark.parametrize(
    'setup',
    [
        lambda mp: mp.delenv(f'{ENV_PREFIX}ENV_SUFFIXES', raising=False),
        lambda mp: mp.setenv(f'{ENV_PREFIX}ENV_SUFFIXES', ''),
    ],
    ids=['unset', 'empty'],
)
def test_no_suffixes_means_no_inference(monkeypatch, setup):
    """Списка по умолчанию нет; пустое значение равно отсутствию.

    Своё соглашение об именовании есть не у всех, и молча угаданная
    метка была бы хуже её отсутствия. Пустое значение в `.env` должно
    вести себя так же, иначе выключение зависит от того, стёрли строку
    или обнулили.
    """
    setup(monkeypatch)
    sec = AppConfig().security
    cfg = _load_app_config('web-shop-prod:PREFIX', sec)

    assert get_env_suffixes() == ()
    assert cfg.connections[0].environment is None
    # Без списка карта связей матчится точным совпадением `id`.
    assert strip_env_suffix('web-shop-prod') == 'web-shop-prod'


@pytest.mark.parametrize(
    'raw, expected',
    [
        ('prod,staging', ('prod', 'staging')),
        ('  PROD , Staging  ', ('prod', 'staging')),
        ('prod,,prod,staging', ('prod', 'staging')),
        ('', ()),
        ('   ', ()),
    ],
    ids=['plain', 'normalised', 'deduped', 'empty', 'blank'],
)
def test_env_suffixes_are_normalised(raw, expected):
    """Сравнение идёт с `conn_id.lower()`, поэтому регистр снимается."""
    assert _parse_env_suffixes(raw) == expected


def test_strip_env_suffix_follows_the_configured_list(monkeypatch):
    """Карта связей снимает суффикс тем же списком, что и метка."""
    monkeypatch.setenv(f'{ENV_PREFIX}ENV_SUFFIXES', 'staging')

    assert strip_env_suffix('orders-staging') == 'orders'
    assert strip_env_suffix('orders-prod') == 'orders-prod'


def test_allow_dml_ddl_default_none(monkeypatch):
    """Без явного override — `None` (наследуется глобальная политика)."""
    sec = AppConfig().security
    cfg = _load_app_config('crm:CRM', sec)
    conn = cfg.connections[0]
    assert conn.allow_dml is None
    assert conn.allow_ddl is None


def test_allow_dml_ddl_explicit_override(monkeypatch):
    _set_pg_env(monkeypatch, 'CRM_PROD', ALLOW_DML='false', ALLOW_DDL='false')
    sec = AppConfig().security
    cfg = _load_app_config('crm-prod:CRM_PROD', sec)
    conn = cfg.connections[0]
    assert conn.allow_dml is False
    assert conn.allow_ddl is False


def test_load_from_env_third_csv_field_rejected():
    """Старый формат `id:prefix:engine` должен падать целиком.

    Работать наполовину он не должен: молчаливое игнорирование третьего
    поля прятало бы опечатки.
    """
    sec = AppConfig().security
    with pytest.raises(ValueError, match='format is id:prefix'):
        _load_app_config('cache:CACHE:redis', sec)


def test_load_from_env_falls_back_to_dotenv_when_var_missing(monkeypatch):
    """`.env` не попадает в os.environ, поэтому нужен свой fallback."""
    monkeypatch.setattr(
        config_module,
        '_dotenv_fallback',
        lambda *_: {'CRM_PG_HOST': '10.1.1.1', 'CRM_PG_PASSWORD': 'x'},
    )
    sec = AppConfig().security
    cfg = _load_app_config('crm:CRM', sec)
    conn = cfg.connections[0]
    assert conn.host == '10.1.1.1'
    assert conn.password is not None
    assert conn.password.get_secret_value() == 'x'


def test_load_from_env_os_environ_has_priority_over_dotenv(monkeypatch):
    monkeypatch.setenv('CRM_PG_HOST', 'real-env-host')
    monkeypatch.setattr(
        config_module,
        '_dotenv_fallback',
        lambda *_: {'CRM_PG_HOST': 'dotenv-host'},
    )
    sec = AppConfig().security
    cfg = _load_app_config('crm:CRM', sec)
    assert cfg.connections[0].host == 'real-env-host'


def test_an_empty_process_variable_does_not_fall_through_to_dotenv(
    monkeypatch,
):
    """`X=` в конфигурации MCP-клиента должно снимать значение, не терять.

    Пока `env_value` выбирал источник по истинности, пустое значение
    проваливалось в `.env`: попытка вернуть подключению безопасный
    дефолт возвращала `read_only=false` из старого файла.
    """
    _set_pg_env(monkeypatch, 'CRM')
    monkeypatch.setenv('CRM_PG_READ_ONLY', '')
    monkeypatch.setattr(
        config_module,
        '_dotenv_fallback',
        lambda *_: {'CRM_PG_READ_ONLY': 'false'},
    )

    cfg = _load_app_config('crm:CRM', AppConfig().security)

    assert cfg.connections[0].read_only is True


def test_empty_config_is_valid():
    sec = AppConfig().security
    cfg = _load_app_config('', sec)
    assert cfg.connections == []
    assert cfg.security.allow_dml is False


# --- validation -------------------------------------------------------


def test_duplicate_id_rejected():
    conn = {'id': 'x'}
    with pytest.raises(ValidationError):
        AppConfig.model_validate({'connections': [conn, conn]})


# --- make_dsn ---------------------------------------------------------


def test_make_dsn_fields():
    dsn = make_dsn(
        dbname='web_shop',
        host='h',
        port=6432,
        user='u',
        password='p',
    )
    assert dsn == (
        'postgresql://u:p@h:6432/web_shop?application_name=pgsteward'
    )


def test_make_dsn_omits_an_unset_password():
    """Пустое место в URI оставляет работать PGPASSWORD и `.pgpass`."""
    dsn = make_dsn(dbname='db', host='h', port=5432, user='u')
    assert dsn == 'postgresql://u@h:5432/db?application_name=pgsteward'


def test_make_dsn_escapes_special_chars():
    """asyncpg разбирает DSN как URI: `@`, `:`, `/` ломали бы адрес."""
    dsn = make_dsn(
        dbname='db/1',
        host='h',
        port=5432,
        user='u@r',
        password='p@ss:w/rd',
    )
    assert dsn == (
        'postgresql://u%40r:p%40ss%3Aw%2Frd@h:5432/db%2F1'
        '?application_name=pgsteward'
    )


# --- secrets masking --------------------------------------------------


def test_env_password_is_secret(monkeypatch):
    _set_pg_env(monkeypatch, 'CRM', PASSWORD='supersecret')
    sec = AppConfig().security
    cfg = _load_app_config('crm:CRM', sec)
    conn = cfg.connections[0]
    assert 'supersecret' not in repr(conn)
    assert 'supersecret' not in str(conn)
    assert conn.password is not None
    assert conn.password.get_secret_value() == 'supersecret'


# --- Settings integration ---------------------------------------------


def _app_config(monkeypatch, **env):
    """`AppConfig` из окружения, в обход кэша `get_app_config`."""
    for name, value in env.items():
        monkeypatch.setenv(f'{ENV_PREFIX}{name}', value)
    settings = Settings()
    return _load_app_config(
        settings.db_connections,
        SecurityPolicy(
            allow_dml=settings.security_allow_dml,
            allow_ddl=settings.security_allow_ddl,
        ),
    )


def test_settings_loads_app_from_env(monkeypatch):
    _set_pg_env(monkeypatch, 'WEB_SHOP')
    app = _app_config(monkeypatch, DB_CONNECTIONS='web-shop:WEB_SHOP')
    assert app.connections[0].id == 'web-shop'


def test_settings_security_from_env(monkeypatch):
    app = _app_config(
        monkeypatch, DB_CONNECTIONS='', SECURITY_ALLOW_DML='true'
    )
    assert app.security.allow_dml is True
    assert app.security.allow_ddl is False


def test_settings_ignores_unprefixed_variables(monkeypatch):
    """`LOG_LEVEL` и `DB_CONNECTIONS` слишком ходовые имена.

    MCP-клиент передаёт процессу своё окружение целиком, и без
    неймспейса чужое значение меняло бы конфигурацию сервера.
    """
    monkeypatch.setenv('DB_CONNECTIONS', 'someone-elses:X')
    monkeypatch.setenv('LOG_LEVEL', 'DEBUG')

    settings = Settings()

    assert settings.db_connections == ''
    assert settings.log_level == 'INFO'


def test_settings_rejects_an_unknown_log_level(monkeypatch):
    """Иначе неизвестный уровень роняет `basicConfig` уже в `main()`."""
    monkeypatch.setenv(f'{ENV_PREFIX}LOG_LEVEL', 'verbose')

    with pytest.raises(ValidationError, match='LOG_LEVEL must be one of'):
        Settings()


def test_settings_has_no_app_field(monkeypatch):
    """Собранного `AppConfig` среди полей быть не должно.

    Объявленное поле — это ещё одна переменная окружения: pydantic-
    settings начнёт искать под него значение и уронит старт на любом,
    которое не разберёт. Сборка живёт в `get_app_config()`.
    """
    monkeypatch.setenv(f'{ENV_PREFIX}APP', 'anything')

    assert 'app' not in Settings.model_fields
    # Непонятное значение под этим именем не должно ничего ломать:
    # ради этого поля и нет. `extra='ignore'` — вторая половина того же
    # решения, и без вызова она не проверяется.
    assert Settings().db_connections == ''


# --- env file resolution ----------------------------------------------


def _write_env(directory, text='DB_CONNECTIONS=\n'):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / '.env'
    path.write_text(text)
    return path


def test_user_config_dir_defaults_to_home_config(monkeypatch, tmp_path):
    monkeypatch.delenv(XDG_CONFIG_HOME_ENV, raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    assert user_config_dir() == tmp_path / '.config' / CONFIG_DIR_NAME


def test_explicit_env_file_wins(monkeypatch, tmp_path):
    explicit = _write_env(tmp_path / 'explicit')
    _write_env(tmp_path / 'xdg' / CONFIG_DIR_NAME)
    monkeypatch.setenv(XDG_CONFIG_HOME_ENV, str(tmp_path / 'xdg'))
    monkeypatch.setenv(ENV_FILE_ENV, str(explicit))
    assert resolve_env_file() == explicit


def test_user_config_env_file_is_used(monkeypatch, tmp_path):
    monkeypatch.delenv(ENV_FILE_ENV, raising=False)
    monkeypatch.setenv(XDG_CONFIG_HOME_ENV, str(tmp_path / 'xdg'))
    expected = _write_env(tmp_path / 'xdg' / CONFIG_DIR_NAME)
    assert resolve_env_file() == expected


def test_cwd_env_file_is_ignored(monkeypatch, tmp_path):
    """cwd задаёт MCP-клиент, там лежит `.env` чужого проекта.

    Ассерт `!= foreign` проходил бы и при возврате `None`, и при любом
    другом пути, поэтому рядом с чужим файлом кладётся свой: правильный
    ответ ровно один.
    """
    monkeypatch.delenv(ENV_FILE_ENV, raising=False)
    monkeypatch.setenv(XDG_CONFIG_HOME_ENV, str(tmp_path / 'xdg'))
    expected = _write_env(tmp_path / 'xdg' / CONFIG_DIR_NAME)
    _write_env(tmp_path / 'project')
    monkeypatch.chdir(tmp_path / 'project')

    assert resolve_env_file() == expected


# --- env file permissions ---------------------------------------------

_nt = pytest.mark.skipif(
    os.name == 'nt', reason='POSIX modes do not describe access on Windows'
)


def _read_env_file(path, caplog):
    """Прочитать файл настоящим `_dotenv_fallback`, мимо autouse-заглушки."""
    _dotenv_fallback.cache_clear()
    with caplog.at_level(logging.WARNING, logger='pgsteward.config'):
        _dotenv_fallback(path)
    _dotenv_fallback.cache_clear()
    return caplog.text


@_nt
def test_world_readable_env_file_is_reported(tmp_path, caplog):
    """Файл с паролями обычно создают `cp`, то есть по umask."""
    path = _write_env(tmp_path / 'cfg')
    path.chmod(0o644)

    text = _read_env_file(path, caplog)

    assert 'chmod 600' in text
    assert str(path) in text


@_nt
def test_private_env_file_is_not_reported(tmp_path, caplog):
    path = _write_env(tmp_path / 'cfg')
    path.chmod(0o600)

    assert _read_env_file(path, caplog) == ''


# --- transport --------------------------------------------------------
#
# Проверка чистая: она смотрит только на конфигурацию, поэтому живёт
# здесь, а не в тестах адаптера. Что её вызывает реестр при сборке — в
# tests/test_connections.py.


def _tls_warning(conn, caplog):
    with caplog.at_level(logging.WARNING, logger='pgsteward.config'):
        warn_if_tls_is_not_configured(conn)
    return caplog.text


def test_a_connection_without_sslmode_is_reported(caplog):
    """Без `sslmode` asyncpg подключается в режиме `prefer`.

    Понижение до незашифрованного соединения ничем не видно: ни в
    ответе, ни в логе драйвера.
    """
    conn = ConnectionConfig(
        id='web-shop', env_prefix='WEB_SHOP', host='db.example.test', user='ro'
    )

    text = _tls_warning(conn, caplog)

    assert 'WEB_SHOP_PG_SSLMODE' in text
    assert 'prefer' in text


def test_the_warning_names_a_variable_that_could_exist(caplog):
    """`web-shop`.upper() дал бы `WEB-SHOP_PG_SSLMODE` — такой нет."""
    conn = ConnectionConfig(id='web-shop', host='db.test', user='ro')

    assert 'WEB_SHOP_PG_SSLMODE' in _tls_warning(conn, caplog)


@pytest.mark.parametrize(
    'conn',
    [
        ConnectionConfig(id='a', host='db.test', user='ro', sslmode='require'),
        ConnectionConfig(
            id='b',
            dsn=SecretStr('postgresql://ro@db.test/x?sslmode=verify-full'),
        ),
        ConnectionConfig(
            id='c',
            dsn=SecretStr(
                'postgresql://ro@db.test/x?sslrootcert=/etc/ssl/ca.crt'
            ),
        ),
        # Unix-сокет: шифровать нечего, предупреждать не о чем.
        ConnectionConfig(
            id='d',
            dsn=SecretStr('postgresql://ro@/x?host=/var/run/postgresql'),
        ),
    ],
    ids=['sslmode', 'dsn_sslmode', 'dsn_rootcert', 'unix_socket'],
)
def test_configured_transport_is_not_reported(conn, caplog):
    assert _tls_warning(conn, caplog) == ''
