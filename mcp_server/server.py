"""FastMCP server exposing the World Cup DuckDB warehouse as MCP tools.

Run (stdio transport, for an MCP client to launch):
    python -m mcp_server.server

Tools are intentionally read-only: queries run against a read-only DuckDB
connection, and `run_sql` additionally rejects anything that isn't a SELECT/WITH.
"""
from __future__ import annotations

import duckdb
from mcp.server.fastmcp import FastMCP

import config

mcp = FastMCP("worldcup-stats")


def _query(sql: str, params: list | None = None) -> str:
    """Run a read-only query and return a readable text table (or a message)."""
    con = duckdb.connect(str(config.DUCKDB_PATH), read_only=True)
    try:
        df = con.execute(sql, params or []).df()
    finally:
        con.close()
    if df.empty:
        return "No rows found."
    return df.to_string(index=False)


@mcp.tool()
def list_tables() -> str:
    """List the queryable tables/views and their columns (use before run_sql)."""
    con = duckdb.connect(str(config.DUCKDB_PATH), read_only=True)
    try:
        out = []
        for (name,) in con.execute("SHOW TABLES").fetchall():
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")').fetchall()]
            out.append(f"{name}: {', '.join(cols)}")
        return "\n".join(out)
    finally:
        con.close()


@mcp.tool()
def list_seasons() -> str:
    """List the World Cup tournaments (seasons) available in the data."""
    return _query("SELECT DISTINCT season FROM players ORDER BY season")


@mcp.tool()
def top_scorers(season: str | None = None, limit: int = 10) -> str:
    """Top goalscorers. Optionally filter to one World Cup year (e.g. '2022')."""
    if season:
        return _query(
            "SELECT season, player, team, goals, assists, shots, shots_on_target "
            "FROM players WHERE season = ? ORDER BY goals DESC NULLS LAST, assists DESC NULLS LAST LIMIT ?",
            [season, limit],
        )
    return _query(
        "SELECT season, player, team, goals, assists FROM players "
        "ORDER BY goals DESC NULLS LAST, assists DESC NULLS LAST LIMIT ?",
        [limit],
    )


@mcp.tool()
def player_stats(player: str, season: str | None = None) -> str:
    """Full stat line(s) for a player by (partial) name. Optionally filter by season."""
    if season:
        return _query(
            "SELECT * FROM players WHERE player ILIKE ? AND season = ?",
            [f"%{player}%", season],
        )
    return _query("SELECT * FROM players WHERE player ILIKE ? ORDER BY season", [f"%{player}%"])


@mcp.tool()
def compare_players(player_a: str, player_b: str, season: str | None = None) -> str:
    """Compare two players side by side on key stats (partial names ok)."""
    cols = ("season, player, team, position, matches_played, minutes, goals, assists, "
            "non_penalty_goals, shots, shots_on_target, yellow_cards, red_cards")
    if season:
        sql = (f"SELECT {cols} FROM players WHERE (player ILIKE ? OR player ILIKE ?) AND season = ? "
               "ORDER BY goals DESC NULLS LAST")
        return _query(sql, [f"%{player_a}%", f"%{player_b}%", season])
    sql = (f"SELECT {cols} FROM players WHERE player ILIKE ? OR player ILIKE ? "
           "ORDER BY player, season")
    return _query(sql, [f"%{player_a}%", f"%{player_b}%"])


@mcp.tool()
def team_summary(team: str, season: str | None = None) -> str:
    """Team-tournament aggregates for a team (partial name ok). Optionally one season."""
    if season:
        return _query("SELECT * FROM team_tournament WHERE team ILIKE ? AND season = ?",
                      [f"%{team}%", season])
    return _query("SELECT * FROM team_tournament WHERE team ILIKE ? ORDER BY season",
                  [f"%{team}%"])


@mcp.tool()
def run_sql(query: str) -> str:
    """Run a read-only SQL SELECT against the warehouse (tables: players, team_tournament,
    top_scorers, and the raw stg_* tables). Only SELECT/WITH queries are allowed."""
    stripped = query.strip().rstrip(";").lstrip("(")
    if not stripped[:6].lower().startswith(("select", "with")):
        return "Only read-only SELECT/WITH queries are allowed."
    try:
        return _query(query)
    except Exception as exc:  # surface SQL errors to the model
        return f"SQL error: {exc}"


if __name__ == "__main__":
    mcp.run()
