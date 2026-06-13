"""End-to-end ETL orchestrator (CLI).

Pulls the World Cup schedule and player season-stats for the configured seasons
from fbref (via soccerdata) and loads them into DuckDB as `stg_*` staging tables.

    python -m etl.pipeline                  # all seasons (historical + live 2026)
    python -m etl.pipeline --seasons 2022   # just one season
    python -m etl.pipeline --current-only   # only the live 2026 tournament (daily refresh)
"""
from __future__ import annotations

import argparse

import duckdb
import pandas as pd

import config
from etl import extract, load


def all_seasons() -> list[str]:
    return list(config.HISTORICAL_SEASONS) + [config.CURRENT_SEASON]


def _gather(seasons: list[str], fetch, label: str) -> pd.DataFrame | None:
    """Fetch + flatten `fetch(season)` per season and concat. Per-season try/except
    so one missing/sparse season (e.g. the live 2026 tournament) can't sink the rest;
    concat aligns columns by name, NaN-filling years that lack newer stats (e.g. xG)."""
    frames = []
    for s in seasons:
        try:
            frames.append(load.flatten(fetch(s)))
            print(f"  [{label}] {s}: ok ({len(frames[-1])} rows)")
        except Exception as exc:
            print(f"  [{label}] {s}: skipped ({exc})")
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)


def run(seasons: list[str], stat_types: list[str]) -> None:
    print(f"ETL: seasons {seasons}, stats {stat_types} -> {config.DUCKDB_PATH}")
    con = duckdb.connect(str(config.DUCKDB_PATH))
    try:
        print("[schedule]")
        sched = _gather(seasons, extract.extract_schedule, "schedule")
        if sched is not None:
            print("  -> stg_schedule:", load.load_dataframe(sched, "stg_schedule", con), "rows")

        for st in stat_types:
            print(f"[player:{st}]")
            df = _gather(seasons, lambda s, _st=st: extract.extract_player_season_stats(s, _st), st)
            if df is not None:
                print(f"  -> stg_player_{st}:", load.load_dataframe(df, f"stg_player_{st}", con), "rows")

        for st in stat_types:
            print(f"[team:{st}]")
            df = _gather(seasons, lambda s, _st=st: extract.extract_team_season_stats(s, _st), f"team:{st}")
            if df is not None:
                print(f"  -> stg_team_{st}:", load.load_dataframe(df, f"stg_team_{st}", con), "rows")
    finally:
        con.close()
    print("ETL complete.")


def main() -> None:
    ap = argparse.ArgumentParser(description="World Cup fbref -> DuckDB ETL")
    ap.add_argument("--seasons", nargs="*", help="e.g. 2022 2018 2026 (default: all)")
    ap.add_argument("--current-only", action="store_true", help="only the live 2026 tournament")
    ap.add_argument("--stat-types", nargs="*", help="player stat tables (default: all in config)")
    args = ap.parse_args()

    if args.current_only:
        seasons = [config.CURRENT_SEASON]
    elif args.seasons:
        seasons = args.seasons
    else:
        seasons = all_seasons()
    stat_types = args.stat_types or config.PLAYER_STAT_TYPES
    run(seasons, stat_types)


if __name__ == "__main__":
    main()
