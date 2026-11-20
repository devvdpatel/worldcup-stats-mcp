"""MLS marts for the "World Cup -> MLS" dashboard, built in DuckDB from stg_asa_* + player_xref.

    python -m etl.mls_model            # build the four marts
    python -m etl.mls_model --export   # ... and write data/exports/<mart>.csv + .xlsx

Marts (every table carries `league`: 'mls', plus 'mlsnp' when MLS NEXT Pro was loaded):
  mls_player_window      one row per player per window (pre / post the World Cup break)
  mls_player_change      one row per player: post-minus-pre per-90 deltas + World Cup line
  mls_player_percentiles per-90 percentiles vs. positional peers (long: one row per KPI)
  mls_team_window        one row per club per window: xG for/against per game, World Cup load
  wc_nation_rounds       helper: furthest 2026 World Cup round per nation (from the fbref schedule)

KPIs (ASA definitions): goals; assists = primary assists; shots; xg = xgoals (incl.
penalties); xa = xassists; goals_added = g+ above average (field players from
players/goals-added, goalkeepers from goalkeepers/goals-added; summed when a player
appears in both). Per-90 = value * 90 / minutes.

Nothing is filtered on minutes: `meets_min_minutes` flags windows with
>= config.MIN_MINUTES_WINDOW minutes, and only flagged rows enter the percentile peer pools.
"""
from __future__ import annotations

import argparse

import duckdb

import config

KPIS = ["goals", "assists", "shots", "xg", "xa", "goals_added"]
GK_KPIS = ["goals_added"]   # attacking per-90s are meaningless for goalkeepers

def position_group(col: str) -> str:
    """ASA general position -> broad group."""
    return (f"CASE {col} WHEN 'GK' THEN 'Goalkeeper' WHEN 'CB' THEN 'Defender' "
            f"WHEN 'FB' THEN 'Defender' WHEN 'DM' THEN 'Midfielder' WHEN 'CM' THEN 'Midfielder' "
            f"WHEN 'AM' THEN 'Midfielder' WHEN 'W' THEN 'Forward' WHEN 'ST' THEN 'Forward' END")


def _p90(col: str, minutes: str = "minutes") -> str:
    return f"CASE WHEN {minutes} > 0 THEN {col} * 90.0 / {minutes} END"


def player_window_sql(min_minutes: int) -> str:
    p90 = ",\n    ".join(f"{_p90(k)} AS {k}_p90" for k in KPIS)
    return f"""
CREATE OR REPLACE TABLE mls_player_window AS
WITH w AS (
    SELECT league, "window", start_date, end_date, split_method
    FROM stg_asa_windows WHERE "window" IN ('pre', 'post')
),
ga AS (   -- g+ per player-team-window; field + goalkeeper tables cover different minutes
    SELECT league, "window", player_id, team_id,
           sum(goals_added_above_avg) AS goals_added, sum(goals_added_raw) AS goals_added_raw
    FROM (SELECT league, "window", player_id, team_id, goals_added_above_avg, goals_added_raw
          FROM stg_asa_player_goals_added
          UNION ALL
          SELECT league, "window", player_id, team_id, goals_added_above_avg, goals_added_raw
          FROM stg_asa_goalkeeper_goals_added)
    WHERE "window" IN ('pre', 'post')
    GROUP BY ALL
),
pt AS (   -- player-team-window
    SELECT x.league, x."window", x.player_id, x.team_id, x.general_position, x.minutes_played,
           x.goals, x.primary_assists, x.shots, x.shots_on_target, x.xgoals, x.xassists,
           x.key_passes, ga.goals_added, ga.goals_added_raw
    FROM stg_asa_player_xgoals x
    LEFT JOIN ga USING (league, "window", player_id, team_id)
    WHERE x."window" IN ('pre', 'post')
),
agg AS (  -- player-window (a player traded inside a window has several team rows)
    SELECT league, "window", player_id,
           sum(minutes_played)              AS minutes,
           sum(goals)                       AS goals,
           sum(primary_assists)             AS assists,
           sum(shots)                       AS shots,
           sum(shots_on_target)             AS shots_on_target,
           sum(xgoals)                      AS xg,
           sum(xassists)                    AS xa,
           sum(key_passes)                  AS key_passes,
           sum(goals_added)                 AS goals_added,
           sum(goals_added_raw)             AS goals_added_raw,
           arg_max(team_id, minutes_played) AS team_id,
           arg_max(general_position, minutes_played) AS window_position,
           count(DISTINCT team_id)          AS n_clubs
    FROM pt GROUP BY ALL
),
season_pos AS (  -- one position per player for the season, so peers don't shift between windows
    SELECT league, player_id, arg_max(general_position, minutes_played) AS position
    FROM stg_asa_player_xgoals WHERE "window" = 'season' GROUP BY ALL
),
games AS (
    SELECT g.league, w."window", g.player_id,
           count(DISTINCT g.game_id) FILTER (g.minutes_played > 0) AS games,
           min(g.game_date_utc) FILTER (g.minutes_played > 0)      AS first_game_date,
           max(g.game_date_utc) FILTER (g.minutes_played > 0)      AS last_game_date
    FROM stg_asa_player_games g
    JOIN w ON w.league = g.league AND g.game_date_utc BETWEEN w.start_date AND w.end_date
    GROUP BY ALL
),
wc AS (
    SELECT asa_league AS league, asa_player_id AS player_id, fbref_player_id
    FROM player_xref WHERE is_accepted
)
SELECT
    a.league,
    a."window",
    CASE a."window" WHEN 'pre' THEN 1 ELSE 2 END        AS window_order,
    w.start_date                                        AS window_start,
    w.end_date                                          AS window_end,
    w.split_method,
    a.player_id                                         AS asa_player_id,
    p.player_name,
    a.team_id                                           AS asa_team_id,
    t.team_name                                         AS club,
    t.team_abbreviation                                 AS club_abbreviation,
    a.n_clubs,
    sp.position,
    {position_group('sp.position')} AS position_group,
    a.window_position,
    p.nationality,
    p.birth_date::DATE                                  AS birth_date,
    floor(date_diff('day', p.birth_date::DATE, w.start_date) / 365.25)::INTEGER AS age,
    a.minutes,
    coalesce(gm.games, 0)                               AS games,
    gm.first_game_date,
    gm.last_game_date,
    a.goals, a.assists, a.shots, a.shots_on_target, a.xg, a.xa, a.key_passes,
    a.goals_added, a.goals_added_raw,
    {p90},
    (wc.player_id IS NOT NULL)                          AS world_cup_player,
    wc.fbref_player_id,
    {min_minutes}                                       AS min_minutes_threshold,
    (a.minutes >= {min_minutes})                        AS meets_min_minutes
FROM agg a
JOIN w USING (league, "window")
LEFT JOIN stg_asa_players p ON p.league = a.league AND p.player_id = a.player_id
LEFT JOIN stg_asa_teams t   ON t.league = a.league AND t.team_id = a.team_id
LEFT JOIN season_pos sp     ON sp.league = a.league AND sp.player_id = a.player_id
LEFT JOIN games gm          ON gm.league = a.league AND gm."window" = a."window" AND gm.player_id = a.player_id
LEFT JOIN wc                ON wc.league = a.league AND wc.player_id = a.player_id
"""


def wc_rounds_sql(season: str) -> str:
    """Furthest round per nation from the fbref schedule (+ final / third-place results)."""
    order = ", ".join(f"('{r}', {i + 1})" for i, r in enumerate(config.WC_ROUND_ORDER))
    return f"""
CREATE OR REPLACE TABLE wc_nation_rounds AS
WITH r(round, rr) AS (VALUES {order}),
m AS (
    SELECT s.date::DATE AS d, s.round, r.rr, s.home_team, s.away_team,
           TRY_CAST(regexp_extract(s.score, '(\\d+)\\s*–\\s*(\\d+)', 1) AS INTEGER) AS hg,
           TRY_CAST(regexp_extract(s.score, '(\\d+)\\s*–\\s*(\\d+)', 2) AS INTEGER) AS ag,
           TRY_CAST(regexp_extract(s.score, '^\\((\\d+)\\)', 1) AS INTEGER)          AS hp,
           TRY_CAST(regexp_extract(s.score, '\\((\\d+)\\)$', 1) AS INTEGER)          AS ap
    FROM stg_schedule s JOIN r USING (round)
    WHERE s.season = '{season}'
),
t AS (
    SELECT home_team AS nation, d, rr, (hg > ag OR (hg = ag AND hp > ap)) AS won FROM m
    UNION ALL
    SELECT away_team, d, rr, (ag > hg OR (hg = ag AND ap > hp)) FROM m
),
last AS (SELECT nation, arg_max(rr, d) AS rr, arg_max(won, d) AS won_last, max(d) AS last_match_date,
                count(*) AS matches FROM t GROUP BY nation)
SELECT '{season}' AS season, nation, last_match_date, matches AS nation_matches,
       CASE WHEN rr = 7 AND won_last THEN 'Champion'
            WHEN rr = 7 THEN 'Runner-up'
            WHEN rr = 6 AND won_last THEN 'Third place'
            WHEN rr = 6 THEN 'Fourth place'
            ELSE (SELECT round FROM r WHERE r.rr = last.rr) END AS round_reached,
       CASE WHEN rr = 7 AND won_last THEN 8 WHEN rr = 7 THEN 7
            WHEN rr = 6 AND won_last THEN 6 WHEN rr = 6 THEN 5 ELSE rr END AS round_reached_rank
FROM last
"""


def player_change_sql(min_minutes: int, season: str) -> str:
    cols = []
    for k in KPIS:
        cols += [f"pre.{k}_p90 AS pre_{k}_p90", f"post.{k}_p90 AS post_{k}_p90",
                 f"post.{k}_p90 - pre.{k}_p90 AS delta_{k}_p90"]
    kpi_cols = ",\n    ".join(cols)
    return f"""
CREATE OR REPLACE TABLE mls_player_change AS
WITH pre AS (SELECT * FROM mls_player_window WHERE "window" = 'pre'),
post AS (SELECT * FROM mls_player_window WHERE "window" = 'post'),
both_ AS (SELECT league, asa_player_id FROM pre UNION SELECT league, asa_player_id FROM post)
SELECT
    b.league,
    b.asa_player_id,
    coalesce(post.player_name, pre.player_name)         AS player_name,
    coalesce(post.position, pre.position)               AS position,
    coalesce(post.position_group, pre.position_group)   AS position_group,
    coalesce(post.nationality, pre.nationality)         AS nationality,
    coalesce(pre.age, post.age)                         AS age_pre,
    pre.club                                            AS club_pre,
    post.club                                           AS club_post,
    coalesce(post.club, pre.club)                       AS club,
    coalesce(post.asa_team_id, pre.asa_team_id)         AS asa_team_id,
    CASE WHEN pre.asa_player_id IS NOT NULL AND post.asa_player_id IS NOT NULL THEN 'both windows'
         WHEN post.asa_player_id IS NOT NULL THEN 'post only (arrived after the break)'
         ELSE 'pre only (no minutes after the break)' END AS window_coverage,
    coalesce(pre.minutes, 0)                            AS pre_minutes,
    coalesce(post.minutes, 0)                           AS post_minutes,
    coalesce(pre.games, 0)                              AS pre_games,
    coalesce(post.games, 0)                             AS post_games,
    coalesce(pre.meets_min_minutes, false)              AS pre_meets_min_minutes,
    coalesce(post.meets_min_minutes, false)             AS post_meets_min_minutes,
    coalesce(pre.meets_min_minutes AND post.meets_min_minutes, false) AS meets_min_minutes_both,
    {min_minutes}                                       AS min_minutes_threshold,
    {kpi_cols},
    coalesce(post.world_cup_player, pre.world_cup_player) AS world_cup_player,
    x.fbref_player_id,
    x.nation                                            AS world_cup_nation,
    wp.minutes                                          AS wc_minutes,
    wp.matches_played                                   AS wc_matches,
    wp.starts                                           AS wc_starts,
    wp.goals                                            AS wc_goals,
    wp.assists                                          AS wc_assists,
    r.round_reached                                     AS wc_round_reached,
    r.round_reached_rank                                AS wc_round_reached_rank,
    r.last_match_date                                   AS wc_nation_last_match,
    post.first_game_date                                AS first_post_break_game,
    date_diff('day', r.last_match_date, post.first_game_date) AS days_from_wc_exit_to_first_mls_game
FROM both_ b
LEFT JOIN pre  ON pre.league = b.league  AND pre.asa_player_id = b.asa_player_id
LEFT JOIN post ON post.league = b.league AND post.asa_player_id = b.asa_player_id
LEFT JOIN player_xref x ON x.is_accepted AND x.asa_league = b.league AND x.asa_player_id = b.asa_player_id
LEFT JOIN players wp ON wp.season = '{season}' AND wp.fbref_player_id = x.fbref_player_id
LEFT JOIN wc_nation_rounds r ON r.nation = x.nation
"""


def player_percentiles_sql() -> str:
    """Long table: one row per (league, window, player, KPI) for min-minutes players.
    Peers = same league, window and ASA season position; goalkeepers only get goals_added."""
    long = "\n    UNION ALL\n    ".join(
        f"SELECT league, \"window\", window_order, asa_player_id, player_name, club, position, "
        f"position_group, world_cup_player, minutes, '{k}' AS kpi, {i + 1} AS kpi_order, "
        f"{k}_p90 AS value_p90 FROM mls_player_window WHERE meets_min_minutes"
        for i, k in enumerate(KPIS)
    )
    gk_kpis = ", ".join(f"'{k}'" for k in GK_KPIS)
    return f"""
CREATE OR REPLACE TABLE mls_player_percentiles AS
WITH long AS (
    {long}
),
peers AS (
    SELECT * FROM long WHERE coalesce(position, '') <> 'GK' OR kpi IN ({gk_kpis})
)
SELECT *,
       round(100 * percent_rank() OVER (PARTITION BY league, "window", position, kpi
                                        ORDER BY value_p90), 1) AS percentile,
       count(*) OVER (PARTITION BY league, "window", position, kpi) AS peer_count
FROM peers
"""


TEAM_WINDOW_SQL = """
CREATE OR REPLACE TABLE mls_team_window AS
WITH tw AS (
    SELECT league, "window", team_id, count_games AS games, goals_for, goals_against,
           xgoals_for AS xg_for, xgoals_against AS xg_against, points, xpoints
    FROM stg_asa_team_xgoals WHERE "window" IN ('pre', 'post')
),
sent_pre AS (   -- World Cup players on the club's books before the break (pre-window primary club)
    SELECT pw.league, pw.asa_team_id AS team_id, count(*) AS wc_players_sent,
           sum(ch.wc_minutes)::INTEGER AS wc_minutes_sent,
           string_agg(pw.player_name, ', ' ORDER BY ch.wc_minutes DESC) AS wc_players_sent_names
    FROM mls_player_window pw
    JOIN mls_player_change ch USING (league, asa_player_id)
    WHERE pw."window" = 'pre' AND pw.world_cup_player
    GROUP BY ALL
),
arrived AS (  -- World Cup players with no pre-break minutes who played for the club after it
    SELECT pw.league, pw.asa_team_id AS team_id, count(*) AS wc_players_arrived,
           string_agg(pw.player_name, ', ') AS wc_players_arrived_names
    FROM mls_player_window pw
    JOIN mls_player_change ch USING (league, asa_player_id)
    WHERE pw."window" = 'post' AND pw.world_cup_player AND ch.pre_minutes = 0
    GROUP BY ALL
),
base AS (
    SELECT tw.*,
           CASE tw."window" WHEN 'pre' THEN 1 ELSE 2 END AS window_order,
           t.team_name AS club, t.team_abbreviation AS club_abbreviation,
           tw.xg_for / nullif(tw.games, 0)                     AS xg_for_per_game,
           tw.xg_against / nullif(tw.games, 0)                 AS xg_against_per_game,
           (tw.xg_for - tw.xg_against) / nullif(tw.games, 0)   AS xgd_per_game,
           tw.goals_for / nullif(tw.games, 0)                  AS goals_for_per_game,
           tw.goals_against / nullif(tw.games, 0)              AS goals_against_per_game,
           tw.points / nullif(tw.games, 0)                     AS points_per_game
    FROM tw LEFT JOIN stg_asa_teams t USING (league, team_id)
)
SELECT b.league, b."window", b.window_order, b.team_id AS asa_team_id, b.club, b.club_abbreviation,
       b.games, b.goals_for, b.goals_against, b.xg_for, b.xg_against, b.points, b.xpoints,
       b.xg_for_per_game, b.xg_against_per_game, b.xgd_per_game,
       b.goals_for_per_game, b.goals_against_per_game, b.points_per_game,
       max(b.xgd_per_game) FILTER (b."window" = 'post') OVER (PARTITION BY b.league, b.team_id)
     - max(b.xgd_per_game) FILTER (b."window" = 'pre')  OVER (PARTITION BY b.league, b.team_id)
                                                     AS xgd_per_game_change,
       coalesce(s.wc_players_sent, 0)               AS wc_players_sent,
       coalesce(s.wc_minutes_sent, 0)               AS wc_minutes_sent,
       s.wc_players_sent_names,
       coalesce(a.wc_players_arrived, 0)            AS wc_players_arrived,
       a.wc_players_arrived_names
FROM base b
LEFT JOIN sent_pre s USING (league, team_id)
LEFT JOIN arrived a  USING (league, team_id)
"""

MARTS = ["mls_player_window", "mls_player_change", "mls_player_percentiles", "mls_team_window"]
HELPERS = ["wc_nation_rounds"]   # small lookup also exported for Tableau


def build(con: duckdb.DuckDBPyConnection | None = None, min_minutes: int = config.MIN_MINUTES_WINDOW,
          season: str = config.CURRENT_SEASON) -> dict[str, int]:
    own = con is None
    if own:
        con = duckdb.connect(str(config.DUCKDB_PATH))
    try:
        con.execute(player_window_sql(min_minutes))
        con.execute(wc_rounds_sql(season))
        con.execute(player_change_sql(min_minutes, season))
        con.execute(player_percentiles_sql())
        con.execute(TEAM_WINDOW_SQL)
        counts = {m: con.execute(f"SELECT count(*) FROM {m}").fetchone()[0] for m in MARTS + HELPERS}
        for m, n in counts.items():
            print(f"  built {m}: {n} rows")
        return counts
    finally:
        if own:
            con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the MLS World Cup marts")
    ap.add_argument("--export", action="store_true", help="also write data/exports/*.csv and *.xlsx")
    args = ap.parse_args()
    build()
    if args.export:
        from etl import export
        export.export_all()


if __name__ == "__main__":
    main()
