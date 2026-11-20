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


_KPI_COLUMNS = {"xg": "xg", "xa": "xa", "goals_added": "goals_added", "g+": "goals_added",
                "shots": "shots", "goals": "goals", "assists": "assists"}


@mcp.tool()
def world_cup_effect(kpi: str = "xg", min_minutes: int = 450, position: str | None = None) -> str:
    """2026 MLS: compare players who played at the 2026 World Cup with those who didn't,
    before vs after the World Cup break, on one per-90 KPI.

    kpi: xg | xa | goals_added (g+) | shots | goals | assists.
    min_minutes: players need at least this many MLS minutes in BOTH windows (default 450).
    position: optional ASA position filter (GK, CB, FB, DM, CM, AM, W, ST).
    Returns pooled (minutes-weighted) per-90 for each group and window, with players and
    minutes, plus the mean player-level change. These are associations from half-season
    samples, not causal effects; always report the sample sizes.
    """
    col = _KPI_COLUMNS.get(kpi.strip().lower())
    if col is None:
        return f"Unknown kpi '{kpi}'. Use one of: {', '.join(sorted(set(_KPI_COLUMNS)))}."
    pos_sql, params = "", [min_minutes, min_minutes]
    if position:
        pos_sql, params = " AND c.position = ?", params + [position.strip().upper()]
    return _query(f"""
        WITH elig AS (
            SELECT c.league, c.asa_player_id, c.world_cup_player, c.delta_{col}_p90 AS delta
            FROM mls_player_change c
            WHERE c.league = 'mls' AND c.pre_minutes >= ? AND c.post_minutes >= ?{pos_sql}
        )
        SELECT CASE WHEN e.world_cup_player THEN 'World Cup players' ELSE 'Other MLS players' END AS grp,
               w."window",
               count(DISTINCT w.asa_player_id)                     AS players,
               sum(w.minutes)                                      AS minutes,
               round(sum(w.{col}) * 90.0 / sum(w.minutes), 3)      AS pooled_{col}_per90,
               round(avg(e.delta), 3)                              AS mean_player_change_per90
        FROM elig e JOIN mls_player_window w USING (league, asa_player_id)
        GROUP BY ALL ORDER BY grp DESC, min(w.window_order)""", params)


@mcp.tool()
def mls_player_profile(player: str) -> str:
    """2026 MLS profile for a player (partial, accent-sensitive name): clubs, minutes and
    per-90 xG / xA / g+ / shots / goals / assists before and after the World Cup break, the
    change, his 2026 World Cup line (nation, minutes, goals, round reached, days until his
    first MLS game back) and his per-90 percentiles vs. MLS players at his position
    (>= 450 minutes in that window)."""
    like = f"%{player}%"
    head = _query("""
        SELECT player_label, position, club_pre, club_post, window_coverage, pre_minutes, post_minutes,
               round(pre_xg_p90, 3) AS pre_xg90, round(post_xg_p90, 3) AS post_xg90,
               round(pre_xa_p90, 3) AS pre_xa90, round(post_xa_p90, 3) AS post_xa90,
               round(pre_goals_added_p90, 3) AS pre_gplus90, round(post_goals_added_p90, 3) AS post_gplus90,
               round(pre_goals_p90, 3) AS pre_g90, round(post_goals_p90, 3) AS post_g90,
               world_cup_player, world_cup_nation, wc_minutes, wc_goals, wc_assists, wc_round_reached,
               days_from_wc_exit_to_first_mls_game AS days_to_first_mls_game
        FROM mls_player_change WHERE league = 'mls' AND player_name ILIKE ?
        ORDER BY post_minutes + pre_minutes DESC LIMIT 5""", [like])
    pct = _query("""
        SELECT player_label, "window", kpi, round(value_p90, 3) AS per90, percentile, peer_count, position
        FROM mls_player_percentiles WHERE league = 'mls' AND player_name ILIKE ?
        ORDER BY player_label, window_order, kpi_order LIMIT 60""", [like])
    return f"{head}\n\nPercentiles vs. positional peers:\n{pct}"


@mcp.tool()
def run_sql(query: str) -> str:
    """Run a read-only SQL SELECT against the warehouse. World Cup tables: players,
    team_tournament, top_scorers, wc_nation_rounds. 2026 MLS tables: mls_player_window,
    mls_player_change, mls_player_percentiles, mls_team_window, player_xref (World Cup <->
    MLS player links). Raw stg_* tables too. Only SELECT/WITH queries are allowed."""
    stripped = query.strip().rstrip(";").lstrip("(")
    if not stripped[:6].lower().startswith(("select", "with")):
        return "Only read-only SELECT/WITH queries are allowed."
    try:
        return _query(query)
    except Exception as exc:  # surface SQL errors to the model
        return f"SQL error: {exc}"


if __name__ == "__main__":
    mcp.run()
