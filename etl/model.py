"""Build clean analytic marts in DuckDB from the stg_* staging tables.

Run after the ETL pipeline:
    python -m etl.model

Produces:
  players          - one wide row per (player, World Cup) joining all 5 player stat tables
  team_tournament  - team-per-World-Cup aggregates derived from the player table
  top_scorers      - leaderboard view for quick lookups
"""
from __future__ import annotations

import duckdb

import config

PLAYERS_SQL = """
CREATE OR REPLACE TABLE players AS
SELECT
    s.season,
    s.team,
    s.player,
    s.nation,
    s.pos                       AS position,
    s.age,
    -- appearances
    s.playing_time_mp           AS matches_played,
    s.playing_time_starts       AS starts,
    s.playing_time_min          AS minutes,
    pt.subs_subs                AS sub_appearances,
    pt.starts_compl             AS complete_matches,
    -- scoring (standard)
    s.performance_gls           AS goals,
    s.performance_ast           AS assists,
    s.performance_g_a           AS goals_plus_assists,
    s.performance_g_pk          AS non_penalty_goals,
    s.performance_pk            AS penalties_scored,
    s.performance_pkatt         AS penalties_attempted,
    -- shooting
    sh.standard_sh              AS shots,
    sh.standard_sot             AS shots_on_target,
    sh.standard_sot_1           AS shots_on_target_pct,
    sh.standard_g_sh            AS goals_per_shot,
    -- discipline / misc
    s.performance_crdy          AS yellow_cards,
    s.performance_crdr          AS red_cards,
    m.performance_2crdy         AS second_yellow_cards,
    m.performance_fls           AS fouls_committed,
    m.performance_fld           AS fouls_drawn,
    m.performance_off           AS offsides,
    m.performance_crs           AS crosses,
    m.performance_int           AS interceptions,
    m.performance_tklw          AS tackles_won,
    m.performance_pkwon         AS penalties_won,
    m.performance_pkcon         AS penalties_conceded,
    m.performance_og            AS own_goals,
    -- goalkeeping (GKs only)
    (k.player IS NOT NULL)      AS is_goalkeeper,
    k.performance_saves         AS gk_saves,
    k.performance_save          AS gk_save_pct,
    k.performance_cs            AS gk_clean_sheets,
    k.performance_ga            AS gk_goals_against,
    k.performance_sota          AS gk_shots_on_target_against,
    k.performance_w             AS gk_wins,
    k.performance_d             AS gk_draws,
    k.performance_l             AS gk_losses
FROM stg_player_standard s
LEFT JOIN stg_player_shooting sh
       ON sh.season = s.season AND sh.team = s.team AND sh.player = s.player
LEFT JOIN stg_player_misc m
       ON  m.season = s.season AND  m.team = s.team AND  m.player = s.player
LEFT JOIN stg_player_playing_time pt
       ON pt.season = s.season AND pt.team = s.team AND pt.player = s.player
LEFT JOIN stg_player_keeper k
       ON  k.season = s.season AND  k.team = s.team AND  k.player = s.player
-- guard against a join fan-out from duplicate (season, team, player) keys
QUALIFY row_number() OVER (PARTITION BY s.season, s.team, s.player
                           ORDER BY s.playing_time_min DESC NULLS LAST) = 1
"""

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


def build(con: duckdb.DuckDBPyConnection | None = None) -> None:
    own = con is None
    if own:
        con = duckdb.connect(str(config.DUCKDB_PATH))
    try:
        con.execute(PLAYERS_SQL)
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
