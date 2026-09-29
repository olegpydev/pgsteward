"""Реестр подключений для MCP-слоя.

`get_registry()` не содержит `await` между проверкой и присваиванием,
поэтому в однопоточном asyncio гонка невозможна и лок не нужен.
"""

from pgsteward.config import get_app_config
from pgsteward.connections import ConnectionRegistry

_registry: ConnectionRegistry | None = None


def get_registry() -> ConnectionRegistry:
    global _registry

    if _registry is None:
        _registry = ConnectionRegistry(get_app_config())
    return _registry


async def close_registry() -> None:
    global _registry

    if _registry is None:
        return
    await _registry.close_all()
    _registry = None
