"""MCP tools over the MLS marts (skip without a built warehouse)."""
from __future__ import annotations

import duckdb
import pytest

import config


def _have(*tables) -> bool:
    if not config.DUCKDB_PATH.exists():
        return False
    with duckdb.connect(str(config.DUCKDB_PATH), read_only=True) as c:
        have = {r[0] for r in c.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    return set(tables) <= have


def test_world_cup_effect_rejects_unknown_kpi():
    from mcp_server import server
    assert server.world_cup_effect("bogus").startswith("Unknown kpi")


@pytest.mark.skipif(not _have("mls_player_change", "mls_player_window"), reason="marts not built")
def test_world_cup_effect_returns_both_groups_and_windows():
    from mcp_server import server
    out = server.world_cup_effect("xg", 450)
    assert out.count("World Cup players") == 2 and out.count("Other MLS players") == 2
    assert "players" in out and "minutes" in out


@pytest.mark.skipif(not _have("mls_player_change", "mls_player_percentiles"), reason="marts not built")
def test_mls_player_profile_finds_a_world_cup_player():
    from mcp_server import server
    out = server.mls_player_profile("Messi")
    assert "Lionel Messi" in out and "Percentiles" in out


def test_run_sql_still_refuses_writes():
    from mcp_server import server
    assert server.run_sql("DROP TABLE players").startswith("Only read-only")
