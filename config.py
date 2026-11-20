"""Central configuration: paths, season, and league constants."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
SOCCERDATA_DIR = DATA_DIR / "soccerdata_cache"
# WC_DUCKDB_PATH points the whole project at another warehouse file (tests, scratch copies)
DUCKDB_PATH = Path(os.environ.get("WC_DUCKDB_PATH", DATA_DIR / "worldcup.duckdb"))

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

# --- American Soccer Analysis (ASA) API, via the itscalledsoccer client -------------
# MLS player/team xG, xA and goals added (g+) for the season the World Cup was played in.
ASA_SEASON = "2026"
ASA_LEAGUES = ["mls", "mlsnp"]          # mls is required; mlsnp (MLS NEXT Pro) is optional
ASA_REQUIRED_LEAGUES = ["mls"]
ASA_STAGE = "Regular Season"
ASA_CACHE_DIR = DATA_DIR / "asa_cache"   # raw JSON responses, one file per request
ASA_CACHE_MAX_AGE_DAYS = 7               # re-download older responses (or pass --refresh)
ASA_MIN_REQUEST_INTERVAL_S = 1.0         # polite spacing between live API requests
# A gap in a league's game dates counts as its World Cup break if it overlaps the
# tournament and lasts at least this many days; otherwise split on the tournament dates.
BREAK_MIN_GAP_DAYS = 14

# --- MLS marts (etl/mls_model.py) ---------------------------------------------------
# rows are never filtered on minutes; these thresholds only set the meets_min_minutes flags
# (and the percentile peer pools). 450 = five full matches in a ~14-game half-season window.
MIN_MINUTES_WINDOW = 450
# World Cup rounds, earliest first (fbref `round` labels)
WC_ROUND_ORDER = ["Group stage", "Round of 32", "Round of 16", "Quarter-finals",
                  "Semi-finals", "Third-place match", "Final"]

REVIEW_DIR = DATA_DIR / "review"         # hand-check files (player matching)
# hand decisions on player matches (tracked in git, unlike data/): accept / reject rows
MATCH_OVERRIDES_PATH = PROJECT_ROOT / "reference" / "player_match_overrides.csv"
EXPORT_DIR = DATA_DIR / "exports"        # CSVs for Tableau Public

for _d in (DATA_DIR, SOCCERDATA_DIR, ASA_CACHE_DIR, REVIEW_DIR, EXPORT_DIR):
    _d.mkdir(parents=True, exist_ok=True)
