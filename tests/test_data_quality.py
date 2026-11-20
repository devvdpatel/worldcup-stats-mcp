"""Data-quality assertions on the built warehouse (data/worldcup.duckdb or $WC_DUCKDB_PATH).

Skipped automatically when the warehouse (or a table) doesn't exist yet, e.g. in CI.
Run after:  python -m etl.pipeline && python -m etl.model && python -m etl.asa && python -m etl.xref
"""
from __future__ import annotations

import duckdb
import pytest

import config


@pytest.fixture(scope="module")
def con():
    if not config.DUCKDB_PATH.exists():
        pytest.skip(f"no warehouse at {config.DUCKDB_PATH}")
    c = duckdb.connect(str(config.DUCKDB_PATH), read_only=True)
    yield c
    c.close()


def _need(con, *tables):
    have = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    missing = [t for t in tables if t not in have]
    if missing:
        pytest.skip(f"table(s) not built yet: {', '.join(missing)}")


def _rows(con, sql, params=None):
    return con.execute(sql, params or []).fetchall()


# ------------------------------------------------------------------ unique keys
@pytest.mark.parametrize("table, key", [
    ("players", "season, fbref_player_id"),
    ("players", "season, team, player"),
    ("stg_asa_players", "league, player_id"),
    ("stg_asa_teams", "league, team_id"),
    ("stg_asa_games", "league, game_id"),
    ("stg_asa_windows", "league, \"window\""),
    ("stg_asa_player_xgoals", "league, \"window\", player_id, team_id"),
    ("stg_asa_player_goals_added", "league, \"window\", player_id, team_id"),
    ("stg_asa_team_xgoals", "league, \"window\", team_id"),
    ("player_xref", "season, fbref_player_id"),
    ("mls_player_window", "league, \"window\", asa_player_id"),
    ("mls_player_change", "league, asa_player_id"),
    ("mls_player_change", "league, player_label"),
])
def test_unique_keys(con, table, key):
    _need(con, table)
    dupes = _rows(con, f"SELECT {key}, count(*) FROM {table} GROUP BY ALL HAVING count(*) > 1 LIMIT 5")
    assert not dupes, f"duplicate {table}({key}): {dupes}"


def test_accepted_xref_links_are_one_to_one(con):
    _need(con, "player_xref")
    dupes = _rows(con, """SELECT asa_league, asa_player_id, list(fbref_player) FROM player_xref
                          WHERE is_accepted GROUP BY ALL HAVING count(*) > 1""")
    assert not dupes, f"ASA players linked to several World Cup players: {dupes}"


def test_accepted_xref_rows_point_at_real_asa_players(con):
    _need(con, "player_xref", "stg_asa_players")
    orphans = _rows(con, """SELECT x.fbref_player, x.asa_player_id FROM player_xref x
                            LEFT JOIN stg_asa_players p ON p.league = x.asa_league AND p.player_id = x.asa_player_id
                            WHERE x.is_accepted AND p.player_id IS NULL""")
    assert not orphans


# ------------------------------------------------------------------ minutes
@pytest.mark.parametrize("table, col", [
    ("players", "minutes"),
    ("stg_asa_player_xgoals", "minutes_played"),
    ("stg_asa_player_goals_added", "minutes_played"),
    ("stg_asa_goalkeeper_goals_added", "minutes_played"),
    ("mls_player_window", "minutes"),
])
def test_no_negative_minutes(con, table, col):
    _need(con, table)
    bad = _rows(con, f"SELECT count(*) FROM {table} WHERE {col} < 0")[0][0]
    assert bad == 0


def test_pre_plus_post_minutes_within_season(con):
    _need(con, "stg_asa_player_xgoals")
    bad = _rows(con, """
        WITH m AS (
            SELECT league, player_id,
                   sum(minutes_played) FILTER ("window" = 'season') AS season_min,
                   coalesce(sum(minutes_played) FILTER ("window" = 'pre'), 0)
                 + coalesce(sum(minutes_played) FILTER ("window" = 'post'), 0) AS window_min
            FROM stg_asa_player_xgoals GROUP BY ALL)
        SELECT league, player_id, season_min, window_min FROM m
        WHERE window_min > coalesce(season_min, 0) LIMIT 5""")
    assert not bad, f"pre + post minutes exceed season minutes: {bad}"


def test_league_break_windows_cover_the_whole_season(con):
    """When a league paused for the World Cup, pre + post must equal the season exactly
    (a dropped boundary game would show up here)."""
    _need(con, "stg_asa_windows", "stg_asa_team_xgoals")
    rows = _rows(con, """
        SELECT t.league,
               sum(count_games) FILTER ("window" = 'season') AS season_games,
               sum(count_games) FILTER ("window" IN ('pre', 'post')) AS window_games
        FROM stg_asa_team_xgoals t
        JOIN (SELECT DISTINCT league, split_method FROM stg_asa_windows) w USING (league)
        WHERE w.split_method = 'league_break' GROUP BY t.league""")
    for league, season_games, window_games in rows:
        assert season_games == window_games, f"{league}: {window_games} window games vs {season_games}"


def test_windows_do_not_overlap(con):
    _need(con, "stg_asa_windows")
    bad = _rows(con, """SELECT pre.league FROM stg_asa_windows pre JOIN stg_asa_windows post USING (league)
                        WHERE pre."window" = 'pre' AND post."window" = 'post'
                          AND pre.end_date >= post.start_date""")
    assert not bad


# ------------------------------------------------------------------ World Cup flag
def test_every_world_cup_flag_is_backed_by_an_accepted_xref_row(con):
    _need(con, "mls_player_window", "player_xref")
    bad = _rows(con, """SELECT w.league, w.asa_player_id, w.player_name FROM mls_player_window w
                        LEFT JOIN player_xref x ON x.is_accepted AND x.asa_league = w.league
                                               AND x.asa_player_id = w.asa_player_id
                        WHERE w.world_cup_player AND x.asa_player_id IS NULL LIMIT 5""")
    assert not bad, f"world_cup_player without an accepted xref row: {bad}"


def test_every_accepted_xref_row_sets_the_flag(con):
    _need(con, "mls_player_window", "player_xref")
    missing = _rows(con, """SELECT x.fbref_player FROM player_xref x
                            WHERE x.is_accepted AND NOT EXISTS (
                                SELECT 1 FROM mls_player_window w
                                WHERE w.league = x.asa_league AND w.asa_player_id = x.asa_player_id
                                  AND w.world_cup_player)
                              AND EXISTS (SELECT 1 FROM mls_player_window w
                                          WHERE w.league = x.asa_league AND w.asa_player_id = x.asa_player_id)""")
    assert not missing, f"accepted links without the flag: {missing}"


def test_world_cup_dates_come_from_the_fbref_schedule(con):
    _need(con, "stg_asa_windows", "stg_schedule")
    wc = _rows(con, "SELECT min(date)::DATE, max(date)::DATE FROM stg_schedule WHERE season = ?",
               [config.CURRENT_SEASON])[0]
    win = _rows(con, "SELECT DISTINCT world_cup_start, world_cup_end FROM stg_asa_windows")
    assert win == [wc]


# ------------------------------------------------------------------ ASA completeness
def test_per_game_minutes_add_up_to_season_minutes(con):
    """Catches truncated per-game pulls (the ASA client stops at a 10,000-row cap).
    Exact for MLS; ASA's MLS NEXT Pro per-game rows are ~0.5% short for two teams."""
    _need(con, "stg_asa_player_games", "stg_asa_player_xgoals")
    rows = _rows(con, """
        WITH g AS (SELECT league, sum(minutes_played) AS m FROM stg_asa_player_games GROUP BY 1),
             s AS (SELECT league, sum(minutes_played) AS m FROM stg_asa_player_xgoals
                   WHERE "window" = 'season' GROUP BY 1)
        SELECT league, g.m, s.m FROM g JOIN s USING (league)""")
    for league, game_min, season_min in rows:
        if league == "mls":
            assert game_min == season_min, f"mls per-game {game_min} vs season {season_min}"
        else:
            assert game_min <= season_min and game_min >= 0.99 * season_min, (league, game_min, season_min)


# ------------------------------------------------------------------ marts
def test_world_cup_rounds_have_a_48_team_shape(con):
    _need(con, "wc_nation_rounds")
    got = dict(_rows(con, "SELECT round_reached, count(*) FROM wc_nation_rounds GROUP BY 1"))
    assert got == {"Group stage": 16, "Round of 32": 16, "Round of 16": 8, "Quarter-finals": 4,
                   "Fourth place": 1, "Third place": 1, "Runner-up": 1, "Champion": 1}


def test_change_deltas_equal_post_minus_pre(con):
    _need(con, "mls_player_change")
    for k in ("goals", "assists", "shots", "xg", "xa", "goals_added"):
        bad = _rows(con, f"""SELECT count(*) FROM mls_player_change
                             WHERE abs(delta_{k}_p90 - (post_{k}_p90 - pre_{k}_p90)) > 1e-9
                                OR (delta_{k}_p90 IS NULL) <> (pre_{k}_p90 IS NULL OR post_{k}_p90 IS NULL)""")[0][0]
        assert bad == 0, k


def test_window_minutes_match_staging(con):
    _need(con, "mls_player_window", "stg_asa_player_xgoals")
    rows = _rows(con, """
        SELECT a.league, a."window", a.m, b.m FROM
          (SELECT league, "window", sum(minutes) AS m FROM mls_player_window GROUP BY ALL) a
          JOIN (SELECT league, "window", sum(minutes_played) AS m FROM stg_asa_player_xgoals
                WHERE "window" IN ('pre', 'post') GROUP BY ALL) b USING (league, "window")""")
    assert rows and all(r[2] == r[3] for r in rows), rows


def test_percentiles_are_in_range_and_only_cover_min_minutes_players(con):
    _need(con, "mls_player_percentiles", "mls_player_window")
    out_of_range = _rows(con, "SELECT count(*) FROM mls_player_percentiles WHERE percentile NOT BETWEEN 0 AND 100")[0][0]
    assert out_of_range == 0
    not_eligible = _rows(con, """SELECT count(*) FROM mls_player_percentiles p JOIN mls_player_window w
                                 USING (league, "window", asa_player_id) WHERE NOT w.meets_min_minutes""")[0][0]
    assert not_eligible == 0
    gk_attacking = _rows(con, """SELECT count(*) FROM mls_player_percentiles
                                 WHERE position = 'GK' AND kpi <> 'goals_added'""")[0][0]
    assert gk_attacking == 0


def test_team_world_cup_counts_match_players(con):
    _need(con, "mls_team_window", "mls_player_window")
    sent = _rows(con, """SELECT league, sum(wc_players_sent) FROM mls_team_window WHERE "window" = 'pre' GROUP BY 1""")
    players = dict(_rows(con, """SELECT league, count(*) FROM mls_player_window
                                 WHERE "window" = 'pre' AND world_cup_player GROUP BY 1"""))
    for league, n in sent:
        assert n == players.get(league, 0), (league, n, players.get(league))


# ------------------------------------------------------------------ exports
@pytest.mark.parametrize("table", ["mls_player_window", "mls_player_change", "mls_player_percentiles",
                                   "mls_team_window", "wc_nation_rounds"])
def test_exports_match_their_mart(con, table):
    import re

    import pandas as pd
    _need(con, table)
    csv, xlsx = config.EXPORT_DIR / f"{table}.csv", config.EXPORT_DIR / f"{table}.xlsx"
    if not csv.exists():
        pytest.skip("exports not written yet (python -m etl.export)")
    n = _rows(con, f"SELECT count(*) FROM {table}")[0][0]
    cols = [r[0] for r in _rows(con, f"SELECT column_name FROM information_schema.columns "
                                     f"WHERE table_name = '{table}' ORDER BY ordinal_position")]
    c = pd.read_csv(csv)
    assert len(c) == n and list(c.columns) == cols
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", col) for col in c.columns)
    if xlsx.exists():
        assert len(pd.read_excel(xlsx)) == n
