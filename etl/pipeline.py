"""End-to-end ETL orchestrator (CLI).

Pulls the World Cup schedule and player season-stats for the configured seasons
from fbref (via soccerdata) and loads them into DuckDB as `stg_*` staging tables.

    python -m etl.pipeline                  # all seasons (historical + live 2026)
    python -m etl.pipeline --seasons 2022   # just one season
    python -m etl.pipeline --current-only   # only the 2026 tournament
    python -m etl.pipeline --replace        # rebuild tables instead of upserting by season
    python -m etl.pipeline --cache-only     # no fbref requests; load cached pages only

Seasons are upserted: re-running one season replaces only that season's rows.
"""
from __future__ import annotations

import argparse

import duckdb
import pandas as pd

import config
from etl import extract, fbref_meta, load


def all_seasons() -> list[str]:
    return list(config.HISTORICAL_SEASONS) + [config.CURRENT_SEASON]


def _gather(seasons: list[str], fetch, label: str) -> pd.DataFrame | None:
    """Fetch + flatten `fetch(season)` per season and concat. Per-season try/except
    so one missing/sparse season (e.g. a stat page fbref no longer serves) can't sink
    the rest; concat aligns columns by name, NaN-filling years that lack a stat."""
    frames = []
    for s in seasons:
        try:
            frames.append(load.flatten(fetch(s)))
            frames[-1]["season"] = frames[-1]["season"].astype(str)
            print(f"  [{label}] {s}: ok ({len(frames[-1])} rows)")
        except Exception as exc:
            print(f"  [{label}] {s}: skipped ({type(exc).__name__}: {str(exc)[:160]})")
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)


def _save(df: pd.DataFrame | None, table: str, con, replace: bool) -> None:
    if df is None:
        print(f"  -> {table}: nothing loaded (kept existing rows)")
        return
    n = load.load_dataframe(df, table, con) if replace else load.upsert_partitions(df, table, con)
    print(f"  -> {table}: {n} rows total")


def run(seasons: list[str], stat_types: list[str], replace: bool = False) -> None:
    """Load the given seasons into stg_* tables.

    By default only the requested seasons' rows are replaced (upsert by season), so a
    `--seasons 2026` refresh keeps 2010-2022. `replace=True` rebuilds each table from
    just these seasons (the original behaviour).
    """
    mode = "replace" if replace else "upsert by season"
    print(f"ETL: seasons {seasons}, stats {stat_types}, {mode} -> {config.DUCKDB_PATH}")
    con = duckdb.connect(str(config.DUCKDB_PATH))
    try:
        print("[schedule]")
        _save(_gather(seasons, extract.extract_schedule, "schedule"), "stg_schedule", con, replace)

        for st in stat_types:
            print(f"[player:{st}]")
            df = _gather(seasons, lambda s, _st=st: extract.extract_player_season_stats(s, _st), st)
            _save(df, f"stg_player_{st}", con, replace)

        if "standard" in stat_types:
            # fbref player ids + club country/tier, re-parsed from the cached standard page
            print("[player:meta]")
            _save(_gather(seasons, fbref_meta.parse_player_meta, "meta"), "stg_player_meta", con, replace)

        for st in stat_types:
            print(f"[team:{st}]")
            df = _gather(seasons, lambda s, _st=st: extract.extract_team_season_stats(s, _st), f"team:{st}")
            _save(df, f"stg_team_{st}", con, replace)
    finally:
        con.close()
        extract.close()
    print("ETL complete.")


def main() -> None:
    ap = argparse.ArgumentParser(description="World Cup fbref -> DuckDB ETL")
    ap.add_argument("--seasons", nargs="*", help="e.g. 2022 2018 2026 (default: all)")
    ap.add_argument("--current-only", action="store_true", help="only the live 2026 tournament")
    ap.add_argument("--stat-types", nargs="*", help="player stat tables (default: all in config)")
    ap.add_argument("--cache-only", action="store_true",
                    help="never contact fbref; load only pages already in data/soccerdata_cache")
    ap.add_argument("--replace", action="store_true",
                    help="rebuild stg_* tables from only these seasons (default: upsert by season)")
    args = ap.parse_args()

    if args.current_only:
        seasons = [config.CURRENT_SEASON]
    elif args.seasons:
        seasons = args.seasons
    else:
        seasons = all_seasons()
    stat_types = args.stat_types or config.PLAYER_STAT_TYPES
    extract.CACHE_ONLY = args.cache_only
    run(seasons, stat_types, replace=args.replace)


if __name__ == "__main__":
    main()
