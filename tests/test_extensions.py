"""Регистрация расширений на старте сервера.

Оркестратор в три строки, но он решает, какие инструменты вообще
увидит модель: то, что зарегистрировано в `_lifespan`, попадает в
первый же `tools/list`.
"""

from fastmcp import FastMCP

from pgsteward.extensions import register_all
from pgsteward.extensions.federation import RELATIONSHIPS_FILE_ENV
from tests.conftest import EXAMPLE_MAP


async def _tool_names(mcp: FastMCP) -> set[str]:
    return set(await mcp.get_tools())


async def test_without_a_map_no_federation_tool_is_advertised():
    """Описания инструментов занимают контекст каждой сессии.

    Рекламировать тот, который откажет на любом вызове, — чистый
    убыток.
    """
    mcp = FastMCP('test')

    assert register_all(mcp) == {'federation': 'off'}
    assert await _tool_names(mcp) == set()


async def test_a_valid_map_registers_both_tools(monkeypatch):
    monkeypatch.setenv(RELATIONSHIPS_FILE_ENV, str(EXAMPLE_MAP))
    mcp = FastMCP('test')

    assert register_all(mcp) == {'federation': 'on'}
    assert await _tool_names(mcp) == {
        'describe_relationships',
        'federated_lookup',
    }


async def test_a_broken_map_keeps_only_the_descriptive_tool(
    monkeypatch, tmp_path
):
    """В degraded-режиме `federated_lookup` отказал бы на любом вызове.

    `describe_relationships` при этом остаётся: он сообщает оператору,
    что карта не разобралась.
    """
    broken = tmp_path / 'relationships.json'
    broken.write_text('{not json')
    monkeypatch.setenv(RELATIONSHIPS_FILE_ENV, str(broken))
    mcp = FastMCP('test')

    assert register_all(mcp) == {'federation': 'degraded'}
    assert await _tool_names(mcp) == {'describe_relationships'}
