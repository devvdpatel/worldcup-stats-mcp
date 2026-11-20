"""Recover fbref identifiers that soccerdata's table parser throws away.

soccerdata flattens each fbref cell to its text, which loses:
  * the stable fbref player id (`data-append-csv="5bc43860"` on the player cell), and
  * for 2026+, the club's country and tier, which live only in the club cell's sort
    key (`csk="1.-England-Leeds United"`) and flag span, plus the club's fbref squad id.

The club country is what lets us tell Sporting KC (USA) from Sporting CP (Portugal),
or Philadelphia Union from Union SG, when matching World Cup players to MLS rosters.

This module re-reads the *already cached* `players_<league>_<season>_standard.html`
page (no extra requests) and returns one row per (season, team, player).
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
from lxml import etree, html

import config

_PLAYER_ID = re.compile(r"/players/([0-9a-f]{8})/")
_SQUAD_ID = re.compile(r"/squads/([0-9a-f]{8})/")


def standard_page(season: str, league: str = config.LEAGUE) -> Path:
    return config.SOCCERDATA_DIR / f"players_{league}_{season}_standard.html"


def _first(values: list, default=None) -> str | None:
    """First xpath result as a plain str (lxml returns str subclasses DuckDB can't ingest)."""
    return str(values[0]) if values else default


def _text(td) -> str | None:
    if td is None:
        return None
    t = " ".join(td.itertext()).strip()
    return t or None


def parse_player_meta(season: str, league: str = config.LEAGUE) -> pd.DataFrame:
    """Parse ids + club metadata from the cached standard-stats page for one season.

    Returns a frame indexed like soccerdata's readers: (league, season, team, player).
    """
    path = standard_page(season, league)
    if not path.exists():
        raise FileNotFoundError(f"{path} not cached yet; run the standard stats extract first")
    tree = html.parse(str(path))
    comments = tree.xpath("//comment()[contains(., 'div_stats_standard')]")
    if comments:  # fbref ships the player table inside an HTML comment
        table = etree.fromstring(comments[0].text, etree.HTMLParser(recover=True)).xpath(
            "//table[contains(@id, 'stats_standard')]"
        )[0]
    else:
        table = tree.xpath("//table[contains(@id, 'stats_standard')]")[0]

    rows = []
    for tr in table.xpath(".//tbody/tr[not(contains(@class, 'thead'))]"):
        cell = {td.get("data-stat"): td for td in tr.xpath("./td|./th")}
        p = cell.get("player")
        if p is None or _text(p) in (None, "Player"):
            continue
        href = _first(p.xpath(".//a/@href"), "")
        pid = p.get("data-append-csv")
        if not pid and (m := _PLAYER_ID.search(href)):
            pid = m.group(1)
        pid = str(pid) if pid else None
        team_td = cell.get("team")
        team_name = _first(team_td.xpath(".//a/text()")) if team_td is not None else None

        club_td = cell.get("club")
        club_tier = club_country = club_name = club_id = None
        if club_td is not None:
            csk = club_td.get("csk") or ""
            parts = csk.split("-", 2)  # "1.-England-Leeds United"
            if len(parts) == 3:
                club_tier = pd.to_numeric(parts[0].rstrip("."), errors="coerce")
                club_country, club_name = parts[1] or None, parts[2] or None
            club_name = _first(club_td.xpath(".//a/text()"), club_name)
            club_href = _first(club_td.xpath(".//a/@href"), "")
            club_id = m.group(1) if (m := _SQUAD_ID.search(club_href)) else None
            if club_country is None:
                club_country = _first(club_td.xpath(".//span[@title]/@title"))

        rows.append({
            "league": league,
            "season": str(season),
            "team": team_name,
            "player": _text(p),
            "fbref_player_id": pid,
            "club": club_name,
            "club_country": club_country,
            "club_tier": club_tier,
            "club_fbref_id": club_id,
            "birth_year": pd.to_numeric(_text(cell.get("birth_year")), errors="coerce"),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError(f"no player rows parsed from {path.name}")
    df["club_tier"] = df["club_tier"].astype("Int64")
    df["birth_year"] = df["birth_year"].astype("Int64")
    # same (league, season, team, player) row index as soccerdata's readers
    return df.set_index(["league", "season", "team", "player"])
