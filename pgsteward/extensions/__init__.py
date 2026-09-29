"""Расширения сервера: опциональные фичи, регистрируемые на старте.

Каждое определяет `register(mcp) -> str` и само логирует свой статус.
Регистрация идёт из `_lifespan` до первого запроса клиента, иначе
инструментов не будет в ответе на `list_tools`.
"""

from typing import Any

from pgsteward.extensions.federation import register as register_federation


def register_all(mcp: Any) -> dict[str, str]:
    """Расширение -> его статус ('on', 'off', 'degraded')."""
    return {
        'federation': register_federation(mcp),
    }
