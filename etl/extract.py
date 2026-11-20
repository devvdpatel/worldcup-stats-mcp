"""Extract World Cup data from fbref.com via the soccerdata library.

soccerdata.FBref handles the browser-based, rate-limited, locally-cached scrape.
These functions return the raw (MultiIndex) DataFrames; transformation/flattening
happens in etl.load.

Politeness / resilience:
  * one FBref reader (one headless browser session) is reused for the whole run,
    instead of launching a new browser for every season x table;
  * `CACHE_ONLY = True` (pipeline `--cache-only`) never contacts fbref: a page that is
    not already in data/soccerdata_cache raises NotCachedError and that season/table
    is skipped. Use it when fbref is rate-limiting or showing a bot challenge.
  * circuit breaker: the first page that fbref refuses (soccerdata gives up after its
    own retries) switches the rest of the run to cache-only, so a block or bot check
    is never hammered with further requests.
"""
from __future__ import annotations

from pathlib import Path

import soccerdata as sd

import config

CACHE_ONLY = False

# soccerdata page names (used in cache filenames) per stat_type
_PLAYER_PAGE = {"standard": "standard", "shooting": "shooting", "keeper": "keeper",
                "playing_time": "playing_time", "misc": "misc"}
_TEAM_PAGE = {"standard": "stats", "keeper": "keepers", "playing_time": "playingtime",
              "shooting": "shooting", "misc": "misc"}

_READER: sd.FBref | None = None
BLOCKED: str | None = None  # set to the error text once fbref refuses a page this run


class NotCachedError(FileNotFoundError):
    """Raised in cache-only mode when a page would have to be downloaded."""


def _require_cached(*files: str) -> None:
    if not (CACHE_ONLY or BLOCKED):
        return
    missing = [f for f in files if not (Path(config.SOCCERDATA_DIR) / f).exists()]
    if missing:
        why = "cache-only mode" if CACHE_ONLY else "fbref refused an earlier page this run"
        raise NotCachedError(f"{why} and not cached: {', '.join(missing)}")


def _guard(fetch):
    """Run a soccerdata read; if fbref refuses a page, trip the circuit breaker."""
    global BLOCKED
    try:
        return fetch()
    except ConnectionError as exc:  # soccerdata: "Could not download <url>." after retries
        BLOCKED = str(exc)
        raise


def _reader(seasons) -> sd.FBref:
    """Return the shared soccerdata FBref reader, pointed at the given season(s)."""
    global _READER
    if _READER is None:
        _READER = sd.FBref(
            leagues=config.LEAGUE,
            seasons=seasons,
            data_dir=config.SOCCERDATA_DIR,
        )
    else:
        _READER.seasons = seasons
    return _READER


def close() -> None:
    """Quit the shared reader's browser (safe to call more than once)."""
    global _READER
    if _READER is not None:
        driver = getattr(_READER, "_driver", None)
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        _READER = None


def _lg() -> str:
    return config.LEAGUE


def extract_schedule(seasons):
    """Match schedule + results for the given season(s)."""
    for s in _as_list(seasons):
        _require_cached(f"teams_{_lg()}_{s}.html", f"schedule_{_lg()}_{s}.html")
    return _guard(lambda: _reader(seasons).read_schedule())


def extract_player_season_stats(seasons, stat_type: str):
    """Player season-stats table (stat_type e.g. 'standard', 'shooting', 'keeper')."""
    for s in _as_list(seasons):
        _require_cached(f"players_{_lg()}_{s}_{_PLAYER_PAGE.get(stat_type, stat_type)}.html")
    return _guard(lambda: _reader(seasons).read_player_season_stats(stat_type=stat_type))


def extract_team_season_stats(seasons, stat_type: str):
    """Team season-stats table."""
    for s in _as_list(seasons):
        _require_cached(f"teams_{_lg()}_{s}_{_TEAM_PAGE.get(stat_type, stat_type)}.html")
    return _guard(lambda: _reader(seasons).read_team_season_stats(stat_type=stat_type))


def _as_list(seasons) -> list[str]:
    return [str(seasons)] if isinstance(seasons, (str, int)) else [str(s) for s in seasons]
