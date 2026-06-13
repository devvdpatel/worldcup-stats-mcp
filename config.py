"""Central configuration: paths, season, and league constants."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
SOCCERDATA_DIR = DATA_DIR / "soccerdata_cache"
DUCKDB_PATH = DATA_DIR / "worldcup.duckdb"

# fbref / soccerdata
LEAGUE = "INT-World Cup"

# The current, live tournament (schedule now; player stats fill in as matches are played).
CURRENT_SEASON = "2026"

# Completed World Cups to backfill for rich, complete historical data (most recent first).
# Confirmed/adjusted in Step 2.
HISTORICAL_SEASONS = ["2022", "2018", "2014", "2010"]

# Player stat tables to pull from fbref (soccerdata stat_type names).
# NOTE: for the World Cup competition, fbref only exposes these five tables
# (passing/defense/possession are league-only and raise a validation error).
PLAYER_STAT_TYPES = ["standard", "shooting", "keeper", "playing_time", "misc"]

for _d in (DATA_DIR, SOCCERDATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)
