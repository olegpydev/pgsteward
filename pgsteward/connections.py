"""Реестр подключений и жизненный цикл адаптеров.

Адаптеры создаются лениво при первом обращении и переиспользуются. Лок
берётся на подключение, а не на реестр: недоступная база не должна
блокировать работу с остальными.
"""

import asyncio
import logging

from pgsteward.adapters.base import DBAdapter
from pgsteward.adapters.postgres import PostgresAdapter
from pgsteward.config import (
    AppConfig,
    ConnectionConfig,
    warn_if_tls_is_not_configured,
)
from pgsteward.errors import (
    AdapterConnectionError,
    ConnectionErrorCode,
    ConnectionMisconfiguredError,
    ConnectionNotFoundError,
)
from pgsteward.models import ConnectionStatus

LOG = logging.getLogger(__name__)


class ConnectionRegistry:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._configs: dict[str, ConnectionConfig] = {
            c.id: c for c in config.connections
        }
        self._adapters: dict[str, DBAdapter] = {}
        self._locks: dict[str, asyncio.Lock] = {
            conn_id: asyncio.Lock() for conn_id in self._configs
        }
        # Проблемы конфигурации — ошибка оператора, и заметить её он
        # может только в stderr: обращения к такому подключению может и
        # не быть. Поэтому весь аудит идёт здесь, при сборке реестра, а
        # не при первом подключении.
        for conn in self._configs.values():
            if conn.missing_env:
                LOG.warning(
                    'connection "%s" is unusable: %s not set',
                    conn.id,
                    ', '.join(conn.missing_env),
                )
            if conn.invalid_env:
                LOG.warning(
                    'connection "%s" is unusable: could not use the value '
                    'of %s',
                    conn.id,
                    ', '.join(conn.invalid_env),
                )
            if conn.is_configured:
                # Непригодному подключению эта претензия ни к чему: оно
                # и так не поднимется, а строка в логе только уводит от
                # настоящей причины.
                warn_if_tls_is_not_configured(conn)

    @property
    def connection_ids(self) -> list[str]:
        return list(self._configs.keys())

    def get_config(self, connection_id: str) -> ConnectionConfig:
        conn = self._configs.get(connection_id)
        if conn is None:
            raise ConnectionNotFoundError(
                f'Connection "{connection_id}" is not configured. '
                f'Available: {", ".join(self._configs) or "none"}.'
            )
        return conn

    async def get_adapter(self, connection_id: str) -> DBAdapter:
        adapter = self._adapters.get(connection_id)
        if adapter is not None:
            return adapter

        conn = self.get_config(connection_id)
        if not conn.is_configured:
            raise ConnectionMisconfiguredError(
                conn.id, conn.missing_env, conn.invalid_env
            )
        async with self._locks[connection_id]:
            adapter = self._adapters.get(connection_id)
            if adapter is not None:
                return adapter
            adapter = PostgresAdapter(conn, self._config.security)
            await adapter.connect()
            self._adapters[connection_id] = adapter
            return adapter

    async def list_connections(
        self, probe: bool = False
    ) -> list[ConnectionStatus]:
        """Статусы подключений.

        Без `probe` — ни одного сетевого обращения: иначе справочный
        вызов открывал бы пул к каждой базе, включая production.

        Незаполненное подключение видно здесь в обоих режимах: это
        единственный способ для агента отличить его от рабочего, не
        наткнувшись на ошибку в середине работы.
        """
        if not probe:
            return [self._status(conn) for conn in self._configs.values()]
        return list(
            await asyncio.gather(
                *(self._probe(c) for c in self._configs.values())
            )
        )

    @staticmethod
    def _status(
        conn: ConnectionConfig,
        *,
        alive: bool | None = None,
        error_code: str | None = None,
    ) -> ConnectionStatus:
        if not conn.is_configured:
            # Проверять нечего: доступность не определена не из-за сети.
            alive = False
            error_code = ConnectionErrorCode.MISCONFIGURED.value
        return ConnectionStatus(
            id=conn.id,
            read_only=conn.read_only,
            alive=alive,
            error_code=error_code,
            environment=conn.environment,
        )

    async def _probe(self, conn: ConnectionConfig) -> ConnectionStatus:
        """Проверить доступность, не оставляя пул.

        Одноразовый адаптер создаётся под тем же локом, что и рабочий:
        иначе параллельный `get_adapter` мог бы открыть второй пул к той
        же базе, а закрылся бы из них только этот.
        """
        if not conn.is_configured:
            # Без адреса и роли подключаться не к чему; сеть не трогаем.
            return self._status(conn)

        error_code: str | None = None
        alive = False
        try:
            async with self._locks[conn.id]:
                adapter = self._adapters.get(conn.id)
                alive = (
                    await adapter.ping()
                    if adapter is not None
                    else await self._ping_once(conn)
                )
        except AdapterConnectionError as exc:
            error_code = exc.code.value
        except Exception:
            # Текст исключения драйвера наружу не идёт (`errors.py`).
            LOG.warning(
                'probe of connection "%s" failed', conn.id, exc_info=True
            )
            error_code = ConnectionErrorCode.CONNECTION_FAILED.value
        return self._status(conn, alive=alive, error_code=error_code)

    async def _ping_once(self, conn: ConnectionConfig) -> bool:
        """Пинг через одноразовый пул, закрываемый сразу же."""
        adapter = PostgresAdapter(conn, self._config.security)
        try:
            await adapter.connect()
            return await adapter.ping()
        finally:
            await adapter.close()

    async def close_all(self) -> None:
        for connection_id in list(self._adapters):
            async with self._locks[connection_id]:
                adapter = self._adapters.pop(connection_id, None)
                if adapter is None:
                    continue
                try:
                    await adapter.close()
                except Exception:
                    LOG.warning(
                        'failed to close connection "%s"',
                        connection_id,
                        exc_info=True,
                    )
