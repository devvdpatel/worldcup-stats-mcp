"""Load the 2026 MLS (and optional MLS NEXT Pro) season from the American Soccer Analysis API.

    python -m etl.asa                  # mls + mlsnp (mlsnp is optional; failures are reported)
    python -m etl.asa --leagues mls    # MLS only
    python -m etl.asa --refresh        # ignore the on-disk response cache

Uses the `itscalledsoccer` client (https://github.com/American-Soccer-Analysis/itscalledsoccer)
with two additions for politeness and reproducibility:
  * every API response is cached as JSON under data/asa_cache/ (config.ASA_CACHE_MAX_AGE_DAYS),
  * live requests are spaced at least config.ASA_MIN_REQUEST_INTERVAL_S apart.
Players and teams are fetched for the requested leagues only (the client's own
get_players/get_teams download every league ASA covers).

Windows. Stats are pulled three times per league: the whole regular season
(`season_name`) and two date windows (`start_date`/`end_date`; the API cannot combine
the two). The World Cup dates come from the fbref schedule in DuckDB (stg_schedule,
season 2026). The league's break is the gap between consecutive game dates that covers
the most tournament days, if it covers and lasts >= config.BREAK_MIN_GAP_DAYS; pre = season start
-> last game before the gap, post = first game after the gap -> season end. If a league
has no such gap, the windows are split on the World Cup dates instead (games played
during the tournament then fall in neither window). Window bounds are padded by one day
so a UTC-vs-local date difference can't drop a boundary game; the 52-day gap makes that safe.

Tables (all carry `league`; stat tables carry `window` in {season, pre, post}):
  stg_asa_windows, stg_asa_teams, stg_asa_players, stg_asa_games,
  stg_asa_player_xgoals, stg_asa_player_goals_added,
  stg_asa_goalkeeper_goals_added, stg_asa_team_xgoals,
  stg_asa_player_games (season only: one row per player per game, with game_date_utc)
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import time
from pathlib import Path

import duckdb
import pandas as pd
from itscalledsoccer.client import AmericanSoccerAnalysis, _dataframe_from_json

import config
from etl import load

WINDOWS = ("season", "pre", "post")


# --------------------------------------------------------------------------- client
class CachedASA:
    """itscalledsoccer client whose every HTTP request goes through a JSON disk cache."""

    def __init__(self, cache_dir: Path = config.ASA_CACHE_DIR, refresh: bool = False,
                 max_age_days: float = config.ASA_CACHE_MAX_AGE_DAYS,
                 min_interval_s: float = config.ASA_MIN_REQUEST_INTERVAL_S):
        self.client = AmericanSoccerAnalysis(lazy_load=True, logging_level="WARNING")
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.refresh = refresh
        self.max_age = dt.timedelta(days=max_age_days)
        self.min_interval_s = min_interval_s
        self._last_request = 0.0
        self.live_requests = 0
        self.cache_hits = 0
        # every client method funnels through _single_request(url, params)
        self.client._single_request = self._single_request

    def _cache_path(self, url: str, params: dict) -> Path:
        key = json.dumps({"url": url, "params": {k: str(v) for k, v in sorted(params.items())}},
                         sort_keys=True)
        return self.cache_dir / f"{hashlib.sha1(key.encode()).hexdigest()}.json"

    def get_json(self, url: str, params: dict) -> list | dict:
        params = {k: v for k, v in params.items() if v is not None}
        path = self._cache_path(url, params)
        if path.exists() and not self.refresh:
            cached = json.loads(path.read_text())
            age = dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(cached["fetched_at"])
            if age <= self.max_age:
                self.cache_hits += 1
                return cached["payload"]
        wait = self.min_interval_s - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        resp = self.client.session.get(url=url, params=params, timeout=self.client.request_timeout)
        self._last_request = time.monotonic()
        self.live_requests += 1
        resp.raise_for_status()
        payload = resp.json()
        path.write_text(json.dumps({
            "url": url, "params": params,
            "fetched_at": dt.datetime.now(dt.UTC).isoformat(), "payload": payload,
        }))
        return payload

    def _single_request(self, url: str, params: dict):
        return _dataframe_from_json(self.get_json(url, dict(params)))

    def entity(self, league: str, kind: str) -> pd.DataFrame:
        """players / teams for ONE league (paginated by the client's _execute_query)."""
        df = self.client._execute_query(f"{self.client.base_url}{league}/{kind}", {})
        return _to_pandas(df)

    def fetch_all(self, path: str, params: dict) -> pd.DataFrame:
        """GET every row of an endpoint, paging with `offset` past any server cap.

        The client only pages when a response has exactly 1,000 rows, but some endpoints
        (e.g. players/xgoals with split_by_games) cap at 10,000, which it silently
        truncates. Here: keep paging while pages come back as full as the first one.
        """
        url = f"{self.client.base_url}{path}"
        rows = self.get_json(url, params)
        page_size = len(rows)
        if page_size >= self.client.MAX_API_LIMIT:
            offset = page_size
            while True:
                page = self.get_json(url, {**params, "offset": str(offset)})
                rows += page
                offset += len(page)
                if len(page) < page_size:
                    break
        return pd.DataFrame(rows)


def _to_pandas(df) -> pd.DataFrame:
    """polars -> pandas without pyarrow (list/struct cells stay Python objects)."""
    return pd.DataFrame(df.to_dicts()) if df.height else pd.DataFrame()


# --------------------------------------------------------------------------- windows
def world_cup_dates(con: duckdb.DuckDBPyConnection, season: str = config.CURRENT_SEASON
                    ) -> tuple[dt.date, dt.date]:
    row = con.execute(
        "SELECT min(date)::DATE, max(date)::DATE FROM stg_schedule WHERE season = ?", [season]
    ).fetchone()
    if row is None or row[0] is None:
        raise RuntimeError(
            f"No {season} World Cup schedule in stg_schedule; run `python -m etl.pipeline "
            f"--seasons {season}` first"
        )
    return row[0], row[1]


def compute_windows(game_dates: list[dt.date], wc_start: dt.date, wc_end: dt.date,
                    min_gap_days: int = config.BREAK_MIN_GAP_DAYS) -> dict:
    """Pre/post windows from a league's game dates (see module docstring)."""
    days = sorted(set(game_dates))
    if not days:
        raise ValueError("no game dates")
    first, last = days[0], days[-1]

    def overlap(a: dt.date, b: dt.date) -> int:
        """days of the tournament that fall inside the no-games gap (a, b)"""
        return max(0, (min(b, wc_end) - max(a, wc_start)).days)

    # the break = the gap covering the most tournament days (ties -> the longer gap);
    # a long gap that merely touches the tournament (e.g. a league restarting mid-tournament
    # and then pausing in August) is not picked
    cands = [(overlap(a, b), (b - a).days, a, b) for a, b in zip(days, days[1:])]
    best = max(cands, default=None)
    pad = dt.timedelta(days=1)
    if best and best[0] >= min_gap_days and best[1] >= min_gap_days:
        _, gap, pre_last, post_first = best
        out = {
            "split_method": "league_break",
            "pre_start": first - pad, "pre_end": pre_last + pad,
            "post_start": post_first - pad, "post_end": last + pad,
            "break_last_game": pre_last, "break_first_game": post_first, "break_days": gap,
        }
    else:
        out = {
            "split_method": "world_cup_dates",
            "pre_start": first - pad, "pre_end": wc_start - pad,
            "post_start": wc_end + pad, "post_end": last + pad,
            "break_last_game": None, "break_first_game": None,
            "break_days": best[1] if best else None,   # longest gap that overlapped (too short)
        }
    out.update({"season_first_game": first, "season_last_game": last,
                "world_cup_start": wc_start, "world_cup_end": wc_end})
    return out


def windows_frame(league: str, w: dict) -> pd.DataFrame:
    rows = [
        {"window": "season", "start_date": w["season_first_game"], "end_date": w["season_last_game"]},
        {"window": "pre", "start_date": w["pre_start"], "end_date": w["pre_end"]},
        {"window": "post", "start_date": w["post_start"], "end_date": w["post_end"]},
    ]
    df = pd.DataFrame(rows)
    for k in ("split_method", "break_last_game", "break_first_game", "break_days",
              "season_first_game", "season_last_game", "world_cup_start", "world_cup_end"):
        df[k] = w[k]
    df.insert(0, "league", league)
    df["season"] = config.ASA_SEASON
    return df


# --------------------------------------------------------------------------- flatten
def _flatten_goals_added(df: pd.DataFrame) -> pd.DataFrame:
    """Expand the nested `data` list [{action_type, goals_added_raw, goals_added_above_avg,
    count_actions}, ...] into ga_<action>_above_avg / _raw / _actions columns + totals."""
    if df.empty or "data" not in df:
        return df
    out = df.drop(columns=["data"]).copy()
    wide: dict[str, list] = {}
    for i, actions in enumerate(df["data"]):
        for a in actions or []:
            key = str(a["action_type"]).lower()
            for src, suffix in (("goals_added_above_avg", "above_avg"),
                                ("goals_added_raw", "raw"), ("count_actions", "actions")):
                wide.setdefault(f"ga_{key}_{suffix}", [None] * len(df))[i] = a.get(src)
    for col, vals in sorted(wide.items()):
        out[col] = pd.to_numeric(pd.Series(vals, index=out.index), errors="coerce")
    above = [c for c in out if c.endswith("_above_avg") and c.startswith("ga_")]
    raw = [c for c in out if c.endswith("_raw") and c.startswith("ga_")]
    out["goals_added_above_avg"] = out[above].sum(axis=1, min_count=1)
    out["goals_added_raw"] = out[raw].sum(axis=1, min_count=1)
    return out


def _clean(df: pd.DataFrame, league: str, window: str | None = None) -> pd.DataFrame:
    df = df.copy()
    def scalar_list(v) -> bool:
        return isinstance(v, (list, tuple)) and not any(isinstance(x, dict) for x in v)

    for c in df.columns:  # lists of scalars (e.g. players.season_name) -> comma strings
        if df[c].map(scalar_list).any():
            df[c] = df[c].map(lambda v: ",".join(map(str, v)) if scalar_list(v) else v)
    df.insert(0, "league", league)
    if window is not None:
        df.insert(1, "window", window)
    return df


# --------------------------------------------------------------------------- load
def fetch_league(api: CachedASA, league: str, wc_start: dt.date, wc_end: dt.date) -> dict[str, pd.DataFrame]:
    c, season, stage = api.client, config.ASA_SEASON, config.ASA_STAGE
    games = _to_pandas(c.get_games(leagues=league, season_name=season, stages=stage))
    if games.empty:
        raise RuntimeError(f"ASA returned no {season} {stage} games for {league}")
    games["game_date_utc"] = pd.to_datetime(games["date_time_utc"], utc=True).dt.date
    played = games[games["status"].eq("FullTime")] if "status" in games else games
    w = compute_windows(list(played["game_date_utc"]), wc_start, wc_end)

    def by_window(fetch) -> pd.DataFrame:
        frames = []
        for name in WINDOWS:
            if name == "season":
                kw = {"season_name": season}
            else:
                kw = {"start_date": w[f"{name}_start"].isoformat(), "end_date": w[f"{name}_end"].isoformat()}
            frames.append(_clean(_to_pandas(fetch(stage_name=stage, **kw)), league, name))
        return pd.concat(frames, ignore_index=True)

    out = {
        "stg_asa_windows": windows_frame(league, w),
        "stg_asa_games": _clean(games, league),
        "stg_asa_teams": _clean(api.entity(league, "teams"), league),
        "stg_asa_player_xgoals": by_window(
            lambda **kw: c.get_player_xgoals(leagues=league, split_by_teams=True, **kw)),
        "stg_asa_player_goals_added": _flatten_goals_added(by_window(
            lambda **kw: c.get_player_goals_added(leagues=league, split_by_teams=True, **kw))),
        "stg_asa_goalkeeper_goals_added": _flatten_goals_added(by_window(
            lambda **kw: c.get_goalkeeper_goals_added(leagues=league, split_by_teams=True, **kw))),
        "stg_asa_team_xgoals": by_window(lambda **kw: c.get_team_xgoals(leagues=league, **kw)),
        # one row per player per game (minutes): appearances per window + first game back
        "stg_asa_player_games": _clean(api.fetch_all(f"{league}/players/xgoals", {
            "season_name": season, "stage_name": stage, "split_by_games": "true"}), league),
    }
    pg = out["stg_asa_player_games"]
    if not pg.empty:
        dates = games.set_index("game_id")["game_date_utc"]
        pg["game_date_utc"] = pg["game_id"].map(dates)
    # players dimension: only players who appear in this season's stats
    ids = set(out["stg_asa_player_xgoals"]["player_id"]) | set(out["stg_asa_player_goals_added"]["player_id"])
    ids |= set(out["stg_asa_goalkeeper_goals_added"].get("player_id", pd.Series(dtype=str)))
    players = api.entity(league, "players")
    out["stg_asa_players"] = _clean(players[players["player_id"].isin(ids)].reset_index(drop=True), league)
    return out


def run(leagues: list[str], refresh: bool = False) -> dict[str, int]:
    with duckdb.connect(str(config.DUCKDB_PATH), read_only=True) as ro:
        wc_start, wc_end = world_cup_dates(ro)
    print(f"ASA: season {config.ASA_SEASON}, leagues {leagues}; World Cup {wc_start} -> {wc_end} (fbref)")
    api = CachedASA(refresh=refresh)
    tables: dict[str, list[pd.DataFrame]] = {}
    for league in leagues:
        try:
            data = fetch_league(api, league, wc_start, wc_end)
        except Exception as exc:
            if league in config.ASA_REQUIRED_LEAGUES:
                raise
            print(f"  [{league}] skipped (optional league): {type(exc).__name__}: {exc}")
            continue
        w = data["stg_asa_windows"].iloc[0]
        print(f"  [{league}] {len(data['stg_asa_games'])} games {w.season_first_game} -> {w.season_last_game}; "
              f"split={w.split_method}"
              + (f" (break {w.break_last_game} -> {w.break_first_game}, {w.break_days} days)"
                 if w.split_method == "league_break" else ""))
        for name, df in data.items():
            tables.setdefault(name, []).append(df)
    print(f"  requests: {api.live_requests} live, {api.cache_hits} from cache ({config.ASA_CACHE_DIR})")

    counts = {}
    con = duckdb.connect(str(config.DUCKDB_PATH))
    try:
        for name, frames in tables.items():
            df = pd.concat(frames, ignore_index=True)
            # one league's refresh must not wipe the other's rows
            counts[name] = load.upsert_partitions(df, name, con, key="league")
            print(f"  -> {name}: {counts[name]} rows")
    finally:
        con.close()
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description="American Soccer Analysis -> DuckDB (stg_asa_*)")
    ap.add_argument("--leagues", nargs="*", default=config.ASA_LEAGUES,
                    help=f"ASA league codes (default: {' '.join(config.ASA_LEAGUES)})")
    ap.add_argument("--refresh", action="store_true", help="ignore cached API responses")
    args = ap.parse_args()
    run(args.leagues, refresh=args.refresh)


if __name__ == "__main__":
    main()
