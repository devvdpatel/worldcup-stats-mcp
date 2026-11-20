"""Build clean analytic marts in DuckDB from the stg_* staging tables.

Run after the ETL pipeline:
    python -m etl.model

Produces:
  players          - one wide row per (player, World Cup) joining all 5 player stat tables
  team_tournament  - team-per-World-Cup aggregates derived from the player table
  top_scorers      - leaderboard view for quick lookups

Schema drift: fbref removed its Opta-sourced stats in January 2026, so the 2026 pages
carry fewer columns (and some stat tables may be missing entirely). The SELECT list is
generated from the columns that actually exist in each staging table, so a missing
column becomes NULL instead of a Binder error, and 2010-2022 keep their full stat lines.
"""
from __future__ import annotations

import duckdb

import config

# (staging alias, staging column, mart column, DuckDB type used when the column is absent)
PLAYER_COLUMNS: list[tuple[str, str, str, str]] = [
    # identity
    ("s", "season", "season", "VARCHAR"),
    ("s", "team", "team", "VARCHAR"),
    ("s", "player", "player", "VARCHAR"),
    ("s", "nation", "nation", "VARCHAR"),
    ("s", "pos", "position", "VARCHAR"),
    ("s", "age", "age", "VARCHAR"),
    ("s", "born", "born", "VARCHAR"),
    ("meta", "fbref_player_id", "fbref_player_id", "VARCHAR"),
    # club at tournament time (fbref shows this from 2026 on)
    ("meta", "club", "club", "VARCHAR"),
    ("meta", "club_country", "club_country", "VARCHAR"),
    ("meta", "club_tier", "club_tier", "INTEGER"),
    # appearances
    ("s", "playing_time_mp", "matches_played", "BIGINT"),
    ("s", "playing_time_starts", "starts", "BIGINT"),
    ("s", "playing_time_min", "minutes", "BIGINT"),
    ("pt", "subs_subs", "sub_appearances", "BIGINT"),
    ("pt", "starts_compl", "complete_matches", "BIGINT"),
    # scoring (standard)
    ("s", "performance_gls", "goals", "BIGINT"),
    ("s", "performance_ast", "assists", "BIGINT"),
    ("s", "performance_g_a", "goals_plus_assists", "BIGINT"),
    ("s", "performance_g_pk", "non_penalty_goals", "BIGINT"),
    ("s", "performance_pk", "penalties_scored", "BIGINT"),
    ("s", "performance_pkatt", "penalties_attempted", "BIGINT"),
    # shooting
    ("sh", "standard_sh", "shots", "BIGINT"),
    ("sh", "standard_sot", "shots_on_target", "BIGINT"),
    ("sh", "standard_sot_1", "shots_on_target_pct", "DOUBLE"),
    ("sh", "standard_g_sh", "goals_per_shot", "DOUBLE"),
    # discipline / misc
    ("s", "performance_crdy", "yellow_cards", "BIGINT"),
    ("s", "performance_crdr", "red_cards", "BIGINT"),
    ("m", "performance_2crdy", "second_yellow_cards", "BIGINT"),
    ("m", "performance_fls", "fouls_committed", "BIGINT"),
    ("m", "performance_fld", "fouls_drawn", "BIGINT"),
    ("m", "performance_off", "offsides", "BIGINT"),
    ("m", "performance_crs", "crosses", "BIGINT"),
    ("m", "performance_int", "interceptions", "BIGINT"),
    ("m", "performance_tklw", "tackles_won", "BIGINT"),
    ("m", "performance_pkwon", "penalties_won", "BIGINT"),
    ("m", "performance_pkcon", "penalties_conceded", "BIGINT"),
    ("m", "performance_og", "own_goals", "BIGINT"),
    # goalkeeping (GKs only)
    ("k", "performance_saves", "gk_saves", "BIGINT"),
    ("k", "performance_save", "gk_save_pct", "DOUBLE"),
    ("k", "performance_cs", "gk_clean_sheets", "BIGINT"),
    ("k", "performance_ga", "gk_goals_against", "BIGINT"),
    ("k", "performance_sota", "gk_shots_on_target_against", "BIGINT"),
    ("k", "performance_w", "gk_wins", "BIGINT"),
    ("k", "performance_d", "gk_draws", "BIGINT"),
    ("k", "performance_l", "gk_losses", "BIGINT"),
]

# alias -> staging table joined on (season, team, player)
JOINS: dict[str, str] = {
    "sh": "stg_player_shooting",
    "m": "stg_player_misc",
    "pt": "stg_player_playing_time",
    "k": "stg_player_keeper",
    "meta": "stg_player_meta",
}

TEAM_TOURNAMENT_SQL = """
CREATE OR REPLACE TABLE team_tournament AS
SELECT
    season,
    team,
    count(*)                    AS squad_size,
    sum(is_goalkeeper::INT)     AS goalkeepers,
    sum(goals)                  AS goals,
    sum(assists)                AS assists,
    sum(non_penalty_goals)      AS non_penalty_goals,
    sum(penalties_scored)       AS penalties_scored,
    sum(shots)                  AS shots,
    sum(shots_on_target)        AS shots_on_target,
    sum(yellow_cards)           AS yellow_cards,
    sum(red_cards)              AS red_cards,
    sum(fouls_committed)        AS fouls_committed,
    sum(own_goals)              AS own_goals
FROM players
GROUP BY season, team
"""

TOP_SCORERS_SQL = """
CREATE OR REPLACE VIEW top_scorers AS
SELECT
    season, player, team, goals, assists, shots, shots_on_target,
    row_number() OVER (PARTITION BY season ORDER BY goals DESC NULLS LAST,
                                                     assists DESC NULLS LAST) AS rank_in_tournament
FROM players
"""


def _columns(con: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [table]
    ).fetchall()}


def players_sql(con: duckdb.DuckDBPyConnection) -> tuple[str, list[str]]:
    """Generate the `players` mart SQL from the staging columns that exist.

    Returns (sql, missing) where `missing` lists "table.column" entries filled with NULL.
    """
    available = {"s": _columns(con, "stg_player_standard")}
    if not available["s"]:
        raise RuntimeError("stg_player_standard is missing; run `python -m etl.pipeline` first")
    for alias, table in JOINS.items():
        available[alias] = _columns(con, table)

    select, missing = [], []
    for alias, src, out, typ in PLAYER_COLUMNS:
        if src in available[alias]:
            expr = f"{alias}.{src}"
            if typ in ("BIGINT", "DOUBLE", "INTEGER"):
                expr = f"TRY_CAST({expr} AS {typ})"
            select.append(f"{expr} AS {out}")
        else:
            select.append(f"CAST(NULL AS {typ}) AS {out}")
            missing.append(f"{JOINS.get(alias, 'stg_player_standard')}.{src}")
    # keeper-table membership, falling back to position for seasons without a keeper table
    gk_pos = "coalesce(s.pos LIKE 'GK%', false)"
    if available["k"]:
        select.append(f"(k.player IS NOT NULL OR {gk_pos}) AS is_goalkeeper")
    else:
        select.append(f"{gk_pos} AS is_goalkeeper")

    joins = []
    for alias, table in JOINS.items():
        if available[alias]:
            joins.append(
                f"LEFT JOIN {table} {alias} ON {alias}.season = s.season "
                f"AND {alias}.team = s.team AND {alias}.player = s.player"
            )
    sql = (
        "CREATE OR REPLACE TABLE players AS\nSELECT\n    "
        + ",\n    ".join(select)
        + "\nFROM stg_player_standard s\n"
        + "\n".join(joins)
        + "\n-- guard against a join fan-out from duplicate (season, team, player) keys\n"
        + "QUALIFY row_number() OVER (PARTITION BY s.season, s.team, s.player\n"
        + "                           ORDER BY TRY_CAST(s.playing_time_min AS BIGINT) DESC NULLS LAST) = 1"
    )
    return sql, missing


def build(con: duckdb.DuckDBPyConnection | None = None) -> None:
    own = con is None
    if own:
        con = duckdb.connect(str(config.DUCKDB_PATH))
    try:
        sql, missing = players_sql(con)
        if missing:
            print(f"  note: {len(missing)} source columns absent, filled with NULL: {', '.join(missing)}")
        con.execute(sql)
        con.execute(TEAM_TOURNAMENT_SQL)
        con.execute(TOP_SCORERS_SQL)
        for obj in ("players", "team_tournament"):
            n = con.execute(f"SELECT count(*) FROM {obj}").fetchone()[0]
            print(f"  built {obj}: {n} rows")
        print("  built view top_scorers")
    finally:
        if own:
            con.close()


if __name__ == "__main__":
    build()
