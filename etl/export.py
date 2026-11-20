"""Export the MLS marts for Tableau Public (which cannot read DuckDB).

    python -m etl.export

Writes, for each mart in etl.mls_model.MARTS + HELPERS:
  data/exports/<mart>.csv    UTF-8, header row, ISO dates (YYYY-MM-DD), true/false booleans
  data/exports/<mart>.xlsx   same rows, one sheet named after the mart, native Excel
                             booleans and dates (Tableau Public reads either format)
Floats are rounded to 4 decimals; column names are already snake_case.
"""
from __future__ import annotations

import duckdb
import pandas as pd

import config
from etl import mls_model

FLOAT_DECIMALS = 4


def _frame(con: duckdb.DuckDBPyConnection, table: str) -> pd.DataFrame:
    order = {
        "mls_player_window": "league, window_order, club, player_name",
        "mls_player_change": "league, club, player_name",
        "mls_player_percentiles": "league, window_order, position, player_name, kpi_order",
        "mls_team_window": "league, club, window_order",
        "wc_nation_rounds": "round_reached_rank DESC, nation",
    }.get(table, "1")
    df = con.execute(f"SELECT * FROM {table} ORDER BY {order}").df()
    for c in df.columns:
        if pd.api.types.is_float_dtype(df[c]):
            df[c] = df[c].round(FLOAT_DECIMALS)
    return df


def _for_csv(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_bool_dtype(out[c]) or str(out[c].dtype) == "boolean":
            out[c] = out[c].map({True: "true", False: "false"})
        elif pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%d")
    return out


def export_all(con: duckdb.DuckDBPyConnection | None = None) -> dict[str, int]:
    own = con is None
    if own:
        con = duckdb.connect(str(config.DUCKDB_PATH), read_only=True)
    out = {}
    try:
        config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        for table in mls_model.MARTS + mls_model.HELPERS:
            df = _frame(con, table)
            _for_csv(df).to_csv(config.EXPORT_DIR / f"{table}.csv", index=False)
            with pd.ExcelWriter(config.EXPORT_DIR / f"{table}.xlsx", engine="openpyxl") as xw:
                df.to_excel(xw, sheet_name=table[:31], index=False)
            out[table] = len(df)
            print(f"  exported {table}: {len(df)} rows -> {config.EXPORT_DIR}/{table}.csv|.xlsx")
        return out
    finally:
        if own:
            con.close()


if __name__ == "__main__":
    export_all()
