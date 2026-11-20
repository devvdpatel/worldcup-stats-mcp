"""Transform soccerdata DataFrames and load them into DuckDB."""
from __future__ import annotations

import re

import duckdb
import pandas as pd

import config


def flatten(df: pd.DataFrame) -> pd.DataFrame:
    """Reset the row MultiIndex and flatten MultiIndex columns to snake_case.

    soccerdata returns a (league, season, team, player) row index and 2-level
    column headers like ('Performance', 'Gls'). This produces flat, DuckDB-friendly
    column names such as `performance_gls`, de-duplicating any collisions.
    """
    df = df.reset_index()

    if isinstance(df.columns, pd.MultiIndex):
        flat_cols = []
        for tup in df.columns:
            parts = [
                str(p).strip()
                for p in tup
                if p is not None and str(p).strip() != "" and not str(p).startswith("Unnamed")
            ]
            flat_cols.append("_".join(parts) if parts else "col")
        df.columns = flat_cols

    df.columns = [re.sub(r"[^0-9a-zA-Z]+", "_", str(c)).strip("_").lower() for c in df.columns]

    # de-duplicate any column-name collisions
    seen: dict[str, int] = {}
    deduped = []
    for c in df.columns:
        if c in seen:
            seen[c] += 1
            deduped.append(f"{c}_{seen[c]}")
        else:
            seen[c] = 0
            deduped.append(c)
    df.columns = deduped
    return df


def table_exists(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone()[0])


def load_dataframe(df: pd.DataFrame, table: str, con: duckdb.DuckDBPyConnection | None = None) -> int:
    """Replace `table` in DuckDB with the contents of `df`. Returns the row count."""
    own = con is None
    if own:
        con = duckdb.connect(str(config.DUCKDB_PATH))
    try:
        con.register("_src", df)
        con.execute(f'CREATE OR REPLACE TABLE "{table}" AS SELECT * FROM _src')
        con.unregister("_src")
        return con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    finally:
        if own:
            con.close()


def upsert_partitions(
    df: pd.DataFrame,
    table: str,
    con: duckdb.DuckDBPyConnection,
    key: str = "season",
) -> int:
    """Replace only the `key` partitions present in `df`, keeping every other partition.

    `python -m etl.pipeline --seasons 2026` therefore refreshes 2026 without wiping
    2010-2022. Rows are combined with UNION ALL BY NAME, so a season with fewer
    columns (fbref dropped its Opta-sourced stats in Jan 2026) gets NULLs, and a
    season with new columns (2026 adds `club`) widens the table.
    Returns the table's total row count.
    """
    if not table_exists(con, table):
        return load_dataframe(df, table, con)
    keys = sorted({str(k) for k in df[key].dropna().unique()})
    con.register("_src", df)
    try:
        con.execute("BEGIN TRANSACTION")
        con.execute(
            f'CREATE OR REPLACE TEMP TABLE _merged AS '
            f'SELECT * FROM "{table}" WHERE CAST("{key}" AS VARCHAR) NOT IN '
            f'(SELECT unnest(?::VARCHAR[])) '
            f'UNION ALL BY NAME SELECT * FROM _src',
            [keys],
        )
        con.execute(f'CREATE OR REPLACE TABLE "{table}" AS SELECT * FROM _merged')
        con.execute("DROP TABLE _merged")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.unregister("_src")
    return con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
