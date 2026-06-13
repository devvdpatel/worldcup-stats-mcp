"""Extract World Cup data from fbref.com via the soccerdata library.

soccerdata.FBref handles the browser-based, rate-limited, locally-cached scrape.
These functions return the raw (MultiIndex) DataFrames; transformation/flattening
happens in etl.load.
"""
from __future__ import annotations

import soccerdata as sd

import config


def _reader(seasons) -> sd.FBref:
    """Build a soccerdata FBref reader for the given World Cup season(s)."""
    return sd.FBref(
        leagues=config.LEAGUE,
        seasons=seasons,
        data_dir=config.SOCCERDATA_DIR,
    )


def extract_schedule(seasons):
    """Match schedule + results for the given season(s)."""
    return _reader(seasons).read_schedule()


def extract_player_season_stats(seasons, stat_type: str):
    """Player season-stats table (stat_type e.g. 'standard', 'shooting', 'passing')."""
    return _reader(seasons).read_player_season_stats(stat_type=stat_type)


def extract_team_season_stats(seasons, stat_type: str):
    """Team season-stats table."""
    return _reader(seasons).read_team_season_stats(stat_type=stat_type)
