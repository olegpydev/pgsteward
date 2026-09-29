# pgsteward

[![CI](https://github.com/olegpydev/pgsteward/actions/workflows/ci.yml/badge.svg)](https://github.com/olegpydev/pgsteward/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Read-only доступ к парку баз PostgreSQL для LLM-агентов через MCP. Агент
выбирает подключение по идентификатору и не видит креды.

Из Claude, Cursor, opencode или любого другого MCP-клиента можно
исследовать схему, выполнять запросы, читать планы выполнения и
проверять здоровье базы.

[English version](README.md)

> Документация проекта ведётся на английском: [docs/](docs/).
> Этот файл — краткий обзор для русскоязычных читателей.

---

## Инструменты

| Инструмент | Назначение |
|---|---|
| `list_connections` | Список подключений: id, read_only, окружение. Без `probe=true` не ходит в сеть. |
| `list_databases` | Базы на сервере подключения. |
| `list_schemas` | Схемы внутри базы (системные исключены). |
| `list_tables` | Таблицы и представления в схеме. |
| `describe_table` | Колонки, типы, nullable, дефолты, PK, FK, индексы, оценка числа строк. Ошибка, если отношения нет. |
| `search_schema` | Поиск таблиц и колонок по подстроке по всем не-системным схемам; сообщает об обрезке. |
| `query` | Один SQL-statement с проверкой политики, позиционные параметры, серверный лимит строк. |
| `explain_query` | `EXPLAIN (FORMAT JSON)` как разобранное дерево плана с эвристическими предупреждениями. |
| `analyze_db_health` | Одиннадцать проверок каталога; extensions не нужны, работает на managed-PostgreSQL. |
| `ping` | Доступность подключения. |
| `get_server_info` | Версия сервера и текущая база. |
| `get_security_config` | Эффективная политика: read_only, DML/DDL, max_rows, max_bytes, query_timeout. |

Ещё два — `describe_relationships` и `federated_lookup` — соединяют
сущность, которая живёт в нескольких базах под разными ключами. Они
регистрируются, только если настроена карта связей, и не занимают
контекст сессии, которой не нужны.

Каждый инструмент возвращает JSON-строку. `describe_table`, сокращённо:

```json
{
  "name": "customer_order", "schema": "public",
  "columns": [
    {"name": "id", "data_type": "bigint", "nullable": false, "is_primary_key": true,
     "default": "nextval('customer_order_id_seq'::regclass)"},
    {"name": "customer_id", "data_type": "bigint", "nullable": false,
     "default": null, "is_primary_key": false},
    {"name": "total", "data_type": "numeric(12,2)", "nullable": false,
     "default": null, "is_primary_key": false}
  ],
  "primary_key": ["id"],
  "foreign_keys": [
    {"column": "customer_id", "references_schema": "public",
     "references_table": "customer", "references_column": "id",
     "constraint_name": "customer_order_customer_id_fkey"}
  ],
  "indexes": [
    {"name": "idx_order_customer", "columns": ["customer_id"],
     "is_unique": false, "included_columns": []}
  ],
  "extra": {"estimated_row_count": 28431, "relation_kind": "table"},
  "notes": []
}
```

Форма ответов, пороги health-проверок и матрица прав —
**[docs/tools.md](docs/tools.md)**.

## Для чего это

pgsteward сделан под один сценарий: агент работает с несколькими базами
PostgreSQL в одной сессии, будь то стейдж и прод или разные сервисы.
Отсюда четыре свойства.

**Read-only обеспечивает PostgreSQL, а не классификатор SQL.** Чтение
идёт в `BEGIN TRANSACTION READ ONLY` поверх
`default_transaction_read_only` и серверного `statement_timeout`.
Запросы, которые лишь выглядят чтением (`WITH ... DELETE ... RETURNING`,
`EXPLAIN ANALYZE INSERT`), отклоняет движок
([проверено на живом сервере](tests/test_integration_postgres.py)).

**Несколько подключений, креды на сервере.** Агент выбирает
`connection_id` и никогда не видит DSN. Ошибки подключения отдаются
стабильным кодом — `auth_failed`, `database_not_found`, `unreachable`, —
поэтому host, порт и имя роли из сообщений драйвера остаются в логе
сервера и в контекст модели не попадают.

**Health-проверки, которые не угадывают.** Под ролью с минимальными
правами PostgreSQL обнуляет нужные проверкам колонки вместо того, чтобы
выдать ошибку, — и проверка, которая доверяет результату, отвечает `ok`,
не посмотрев ни на один объект. pgsteward возвращает `skipped` и называет
недостающий грант.

**Аудит-лог без текста запросов.** На каждый вызов инструмента пишутся
подключение, класс statement'а, длительность, число строк и исход,
включая неудачи: отклонённая попытка записи интересна аудиту не меньше
удавшегося чтения. Текста SQL и значений параметров в логе нет.

## Установка

Если пользуетесь [uv](https://docs.astral.sh/uv/), ставить заранее ничего
не нужно: MCP-клиент сам запустит `uvx pgsteward`.

```bash
uv tool install pgsteward     # или: pipx install pgsteward
```

Требуется Python 3.11+ и доступный PostgreSQL 14+. CI прогоняет тесты
на живых 14 и 17. 14 — самая старая версия, которую ещё поддерживает
апстрим; на более старых сервер, скорее всего, заработает, но этого
никто не проверяет.

## Быстрый старт

**1. Указать базу.** Конфигурация — только переменные окружения: строка
метаданных плюс блок кред. Положите их в `~/.config/pgsteward/.env` —
pgsteward найдёт файл сам:

```bash
mkdir -p ~/.config/pgsteward
touch ~/.config/pgsteward/.env && chmod 600 ~/.config/pgsteward/.env
```

```bash
PGSTEWARD_DB_CONNECTIONS=web-shop:WEB_SHOP

WEB_SHOP_PG_HOST=127.0.0.1
WEB_SHOP_PG_PORT=5432
WEB_SHOP_PG_DATABASE=web_shop
WEB_SHOP_PG_USER=readonly_user
WEB_SHOP_PG_PASSWORD=secret
```

`web-shop` — идентификатор, которым пользуется агент; `WEB_SHOP` — префикс
блока кред. `HOST`, `USER` и `DATABASE` обязательны — ни одно из них не
угадывается. Read-only и запрет записи включены по умолчанию.

Оставьте `WEB_SHOP_PG_PASSWORD` пустым — и пароль не отправляется вовсе:
тогда работают Unix-сокет с `peer`, клиентский TLS-сертификат, `.pgpass`
или IAM-токен. См.
[docs/security.md](docs/security.md#not-storing-a-password-at-all).

**2. Подключить к MCP-клиенту.** Для Claude Code:

```bash
claude mcp add pgsteward -- uvx pgsteward
```

Для клиентов с JSON-конфигом (Claude Desktop, Cursor, opencode, VS Code,
Windsurf, Zed) форма одна и та же, и кред в ней нет:

```jsonc
{
  "mcpServers": {
    "pgsteward": {
      "command": "uvx",
      "args": ["pgsteward"]
    }
  }
}
```

Точные пути к файлам и особенности каждого клиента —
**[docs/integrations.md](docs/integrations.md)**.

Те же переменные можно положить в блок `env` конфига клиента вместо
`.env` — для одноразовой локальной базы это нормально. В остальных
случаях `.env` безопаснее: см.
[где лежат креды](docs/security.md#where-the-credentials-sit).

**3. Спрашивать.** «Какие таблицы ссылаются на customer id?», «Почему этот
запрос медленный?», «С базой сейчас всё в порядке?»

Попробовать без настоящей базы: [examples/quickstart](examples/quickstart)
поднимает в Docker PostgreSQL с наполненной схемой и готовым конфигом.

## Модель безопасности

Чтение защищено на трёх уровнях; гарантию дают только два последних:

1. **Классификация до выполнения.** Statement относится по первому
   значащему ключевому слову к классу и сверяется с политикой; больше
   одного statement'а за вызов отклоняется. Это фильтр: классификатор по
   ключевому слову можно обмануть, поэтому гарантию даёт уровень 2.
2. **Транзакция.** Чтение всегда выполняется в
   `BEGIN TRANSACTION READ ONLY`, независимо от флагов подключения.
3. **Сессия.** На read-only подключениях дополнительно ставится
   `default_transaction_read_only=on`, а на каждом — серверный
   `statement_timeout`.

Для записи нужно *одновременно* `read_only=false` на подключении и
разрешённый класс в политике. И то и другое по умолчанию выключено.

Подключать стоит роль с минимальными правами, а не суперюзера. Значения
pgsteward не преобразует, поэтому закрыть от агента отдельную колонку —
персональные данные, секрет — можно поколоночным грантом или вьюхой, и
держать это будет PostgreSQL:
[как прятать колонки](docs/security.md#hiding-columns-from-the-agent).

Как устроен каждый уровень и где он заканчивается —
**[docs/security.md](docs/security.md)**. От чего pgsteward не защищает и
как разворачивать его безопасно — **[SECURITY.md](SECURITY.md)**.

## Конфигурация

```
PGSTEWARD_DB_CONNECTIONS=web-shop:WEB_SHOP,analytics:ANALYTICS
```

CSV из записей `id:prefix`. `prefix` по умолчанию — id в верхнем
регистре, где дефисы и пробелы заменены на подчёркивания. Креды
читаются из `<PREFIX>_PG_HOST / USER / DATABASE` — все три обязательны,
если адрес целиком не задан через `<PREFIX>_PG_DSN`, — плюс
опциональные `PORT`, `PASSWORD`, `READ_ONLY`, `SSLMODE`,
`QUERY_TIMEOUT`, `MAX_ROWS`, `MAX_BYTES`, `POOL_MIN`, `POOL_MAX`,
`ALLOW_DML`, `ALLOW_DDL`.

`id`, заканчивающийся на один из суффиксов, перечисленных в
`PGSTEWARD_ENV_SUFFIXES` (`prod,staging`, `dev,uat` — какие приняты у
вас), отдаёт этот суффикс как `environment` в `list_connections`, так что
агент отличает прод до того, как в него обратится. Это метка, а не
барьер: запись останавливают `read_only` и политика, а не имя.

Полный справочник, включая то, как резолвится `.env` при установке из
PyPI — **[docs/configuration.md](docs/configuration.md)**.

## Границы проекта

Только PostgreSQL, и это решение, а не текущее состояние: health-проверки,
разбор `EXPLAIN (FORMAT JSON)` и барьер `READ ONLY`, на котором стоит вся
модель безопасности, — возможности PostgreSQL, аналогов которым у других
движков нет. Поля движка нет ни в конфигурации, ни в ответах. Разбор
целиком:
[docs/architecture.md](docs/architecture.md#why-there-is-no-second-engine).

Транспорт — stdio. MCP-клиент запускает pgsteward дочерним процессом по
требованию: разворачивать сервис не нужно, но и SSE/HTTP-эндпоинта нет.

Чего здесь нет, чтобы можно было сразу отсеять: рекомендаций по
индексам, анализа нагрузки через `pg_stat_statements`, оценки раздувания
таблиц и индексов.

## Разработка

```bash
git clone https://github.com/olegpydev/pgsteward && cd pgsteward
uv sync

uv run ruff check . && uv run ruff format --check .
uv run mypy pgsteward
uv run pytest -m "not integration"
```

Integration-тестам нужен одноразовый PostgreSQL; без него они
пропускаются, команда запуска — в [CONTRIBUTING.md](CONTRIBUTING.md). За
что отвечает каждый набор тестов:
[docs/architecture.md](docs/architecture.md#testing-strategy).

## Документация

| Документ | О чём |
|---|---|
| [docs/configuration.md](docs/configuration.md) | Все переменные окружения, поиск `.env`, несколько окружений |
| [docs/tools.md](docs/tools.md) | Форма ответов, пороги health-проверок, нужные права |
| [docs/security.md](docs/security.md) | Как работают три уровня, известные дыры, сокрытие колонок, аудит-лог |
| [docs/integrations.md](docs/integrations.md) | Конфиги под каждый клиент и разбор проблем |
| [docs/architecture.md](docs/architecture.md) | Раскладка, путь запроса, граница адаптера, стратегия тестов |
| [SECURITY.md](SECURITY.md) | Модель угроз и рекомендуемое развёртывание |

## Лицензия

MIT — см. [LICENSE](LICENSE).
