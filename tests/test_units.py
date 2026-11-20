"""Unit tests that need no data: name/nation/club normalization, windows, upserts."""
from __future__ import annotations

import datetime as dt

import duckdb
import pandas as pd
import pytest

from etl import asa, load, xref


# ------------------------------------------------------------------ names & nations
@pytest.mark.parametrize("raw, expected", [
    ("Jürgen Locadia", "jurgen locadia"),
    ("Marco Pašalić", "marco pasalic"),
    ("Aiden O'Neill", "aiden oneill"),
    ("Danley Jean-Jacques", "danley jean jacques"),
    ("Martin Ødegaard", "martin odegaard"),
    ("  Derrick  Etienne Jr. ", "derrick etienne jr"),
    (None, ""),
])
def test_normalize(raw, expected):
    assert xref.normalize(raw) == expected


def test_name_similarity_ladder():
    n = xref.normalize
    assert xref.name_similarity(n("Rodrigo De Paul"), n("Rodrigo de Paul")) == 1.0
    assert xref.name_similarity(n("Son Heung-min"), n("Heung-Min Son")) == 0.98
    assert xref.name_similarity(n("Derrick Etienne"), n("Derrick Etienne Jr.")) == 0.92
    assert xref.name_similarity(n("Nico Williams"), n("Loic Williams")) < 0.90


@pytest.mark.parametrize("fbref, asa_name", [
    ("Korea Republic", "South Korea"), ("Côte d'Ivoire", "Ivory Coast"), ("IR Iran", "Iran"),
    ("Türkiye", "Turkey"), ("Czechia", "Czech Republic"), ("Cabo Verde", "Republic of Cape Verde"),
    ("Congo DR", "DR Congo"), ("Bosnia–Herz", "Bosnia and Herzegovina"), ("USA", "USA"),
])
def test_nation_aliases(fbref, asa_name):
    assert xref.canonical_nation(fbref) == xref.canonical_nation(asa_name)


# ------------------------------------------------------------------ clubs
MLS_TEAMS = pd.DataFrame([
    ("t1", "Atlanta United FC", "Atlanta", "ATL"), ("t2", "Inter Miami CF", "Miami", "MIA"),
    ("t3", "New England Revolution", "New England", "NER"), ("t4", "New York City FC", "New York City", "NYC"),
    ("t5", "Los Angeles FC", "Los Angeles FC", "LAFC"), ("t6", "Vancouver Whitecaps FC", "Vancouver", "VAN"),
    ("t7", "Sporting Kansas City", "Kansas City", "SKC"), ("t8", "Minnesota United FC", "Minnesota", "MIN"),
], columns=["team_id", "team_name", "team_short_name", "team_abbreviation"])


@pytest.mark.parametrize("club, team_id", [
    ("Atlanta Utd", "t1"), ("Inter Miami", "t2"), ("NE Revolution", "t3"), ("NYCFC", "t4"),
    ("LAFC", "t5"), ("Vancouver", "t6"), ("Minnesota Utd", "t8"), ("Sporting KC", "t7"),
    ("Miami FC", None),          # USL Championship club: must not become Inter Miami
    ("Sporting CP", None),       # Portugal: must not become Sporting KC
])
def test_resolve_club(club, team_id):
    assert xref.resolve_club(club, MLS_TEAMS) == team_id


# ------------------------------------------------------------------ matching
def _match(wc_rows, asa_rows, team_rows):
    wc = pd.DataFrame(wc_rows, columns=["fbref_player_id", "player", "team", "club",
                                        "club_country", "club_tier", "birth_year"])
    ap = pd.DataFrame(asa_rows, columns=["league", "player_id", "player_name", "nationality", "birth_date"])
    pt = pd.DataFrame(team_rows, columns=["league", "player_id", "team_id"])
    teams = MLS_TEAMS.assign(league="mls")
    return xref.match_players(wc, ap, pt, teams).set_index("fbref_player_id")


def test_same_name_different_player_is_not_accepted():
    # two different Luis Suárez: Colombia (b. 1997, Sporting CP) vs Inter Miami's Uruguayan (b. 1987)
    x = _match([("c1", "Luis Suárez", "Colombia", "Sporting CP", "Portugal", 1, 1997)],
               [("mls", "a1", "Luis Suárez", "Uruguay", "1987-01-24")],
               [("mls", "a1", "t2")])
    assert not x.loc["c1", "is_accepted"]
    assert x.loc["c1", "match_method"] == "name_only"


def test_club_match_accepts_and_ambiguity_blocks():
    x = _match([("w1", "Lionel Messi", "Argentina", "Inter Miami", "United States", 1, 1987),
                ("w2", "John Smith", "USA", "Sporting CP", "Portugal", 1, 2000)],
               [("mls", "a1", "Lionel Messi", "Argentina", "1987-06-24"),
                ("mls", "a2", "John Smith", "USA", "2000-01-01"),
                ("mls", "a3", "John Smith", "USA", "2000-05-05")],
               [("mls", "a1", "t2"), ("mls", "a2", "t1"), ("mls", "a3", "t6")])
    assert x.loc["w1", "is_accepted"] and x.loc["w1", "match_method"] == "name_club"
    assert x.loc["w2", "ambiguous"] and not x.loc["w2", "is_accepted"]
    assert x.loc["w2", "needs_review"]


def test_override_reject_wins():
    ov = pd.DataFrame([{"fbref_player_id": "w1", "asa_player_id": "", "decision": "reject", "note": ""}])
    wc = pd.DataFrame([("w1", "Lionel Messi", "Argentina", "Inter Miami", "United States", 1, 1987)],
                      columns=["fbref_player_id", "player", "team", "club", "club_country", "club_tier",
                               "birth_year"])
    ap = pd.DataFrame([("mls", "a1", "Lionel Messi", "Argentina", "1987-06-24")],
                      columns=["league", "player_id", "player_name", "nationality", "birth_date"])
    pt = pd.DataFrame([("mls", "a1", "t2")], columns=["league", "player_id", "team_id"])
    x = xref.match_players(wc, ap, pt, MLS_TEAMS.assign(league="mls"), ov)
    assert not x.iloc[0]["is_accepted"]


def _wc_row(fid="c1"):
    return pd.DataFrame([(fid, "Luis Suárez", "Colombia", "Sporting CP", "Portugal", 1, 1997)],
                        columns=["fbref_player_id", "player", "team", "club", "club_country", "club_tier",
                                 "birth_year"])


def _asa(pid="a1"):
    ap = pd.DataFrame([("mls", pid, "Luis Suárez", "Uruguay", "1987-01-24")],
                      columns=["league", "player_id", "player_name", "nationality", "birth_date"])
    pt = pd.DataFrame([("mls", pid, "t2")], columns=["league", "player_id", "team_id"])
    return ap, pt


def test_reject_only_applies_to_the_rejected_candidate():
    ov = pd.DataFrame([{"fbref_player_id": "c1", "asa_player_id": "a1", "decision": "reject", "note": ""}])
    teams = MLS_TEAMS.assign(league="mls")
    same = xref.match_players(_wc_row(), *_asa("a1"), teams, ov).iloc[0]
    assert same["override"] == "reject" and not same["needs_review"]
    other = xref.match_players(_wc_row(), *_asa("a2"), teams, ov).iloc[0]   # new candidate
    assert pd.isna(other["override"]) and other["needs_review"]


def test_dismiss_hides_an_unmatched_player_until_a_candidate_appears():
    ov = pd.DataFrame([{"fbref_player_id": "c1", "asa_player_id": "", "decision": "dismiss", "note": ""}])
    wc = _wc_row().assign(club="Miami FC", club_country="United States", club_tier=2)
    teams = MLS_TEAMS.assign(league="mls")
    empty_ap = pd.DataFrame(columns=["league", "player_id", "player_name", "nationality", "birth_date"])
    empty_pt = pd.DataFrame(columns=["league", "player_id", "team_id"])
    none = xref.match_players(wc, empty_ap, empty_pt, teams, ov).iloc[0]
    assert none["override"] == "dismiss" and not none["needs_review"] and not none["is_accepted"]
    later = xref.match_players(wc, *_asa("a1"), teams, ov).iloc[0]          # a candidate shows up
    assert pd.isna(later["override"]) and later["needs_review"]


def test_override_accept_keeps_note_and_clears_evidence_for_a_different_player():
    ov = pd.DataFrame([{"fbref_player_id": "c1", "asa_player_id": "a9", "decision": "accept",
                        "note": "hand-verified"}])
    x = _match([("c1", "Luis Suárez", "Colombia", "Sporting CP", "Portugal", 1, 1997)],
               [("mls", "a1", "Luis Suárez", "Uruguay", "1987-01-24")],
               [("mls", "a1", "t2")])
    x = xref._finalize(x.reset_index().drop(columns=["ambiguous", "conflict", "override", "override_note",
                                                      "is_accepted", "review_reason", "needs_review"]), ov)
    r = x.iloc[0]
    assert r["is_accepted"] and r["asa_player_id"] == "a9" and r["override_note"] == "hand-verified"
    assert r["match_method"] == "manual_override" and pd.isna(r["asa_player_name"])
    assert not r["needs_review"]


def test_review_file_keeps_hand_decisions_across_reruns(tmp_path):
    x = _match([("c1", "Luis Suárez", "Colombia", "Sporting CP", "Portugal", 1, 1997)],
               [("mls", "a1", "Luis Suárez", "Uruguay", "1987-01-24")],
               [("mls", "a1", "t2")]).reset_index()
    ev = pd.DataFrame({"asa_league": ["mls"], "asa_player_id": ["a1"],
                       "mls_pre_minutes": [900], "mls_post_minutes": [1000]})
    path = tmp_path / "review.csv"
    xref.write_review(x, ev, path)
    first = pd.read_csv(path, dtype=str).fillna("")
    assert list(first.columns[:3]) == ["decision", "note", "review_reason"]
    first.loc[0, ["decision", "note"]] = ["reject", "different player, born 1987"]
    first.to_csv(path, index=False)
    xref.write_review(x, ev, path)
    again = pd.read_csv(path, dtype=str).fillna("")
    assert again.loc[0, "decision"] == "reject"
    assert again.loc[0, "note"] == "different player, born 1987"
    assert again.loc[0, "mls_post_minutes"] == "1000"


# ------------------------------------------------------------------ windows
D = dt.date


def test_windows_use_league_break_when_it_overlaps_world_cup():
    dates = [D(2026, 2, 21), D(2026, 5, 25), D(2026, 7, 16), D(2026, 11, 8)]
    w = asa.compute_windows(dates, D(2026, 6, 11), D(2026, 7, 19))
    assert w["split_method"] == "league_break"
    assert (w["break_last_game"], w["break_first_game"], w["break_days"]) == (D(2026, 5, 25), D(2026, 7, 16), 52)
    assert w["pre_end"] < w["post_start"]          # padding never makes windows overlap
    assert w["pre_start"] <= dates[0] and w["post_end"] >= dates[-1]


def test_windows_fall_back_to_world_cup_dates_without_a_break():
    dates = [D(2026, 3, 1) + dt.timedelta(days=5 * i) for i in range(40)]   # never > 5-day gaps
    w = asa.compute_windows(dates, D(2026, 6, 11), D(2026, 7, 19))
    assert w["split_method"] == "world_cup_dates"
    assert w["pre_end"] == D(2026, 6, 10) and w["post_start"] == D(2026, 7, 20)


def test_long_gap_outside_world_cup_is_not_the_break():
    dates = [D(2026, 2, 1), D(2026, 3, 30), D(2026, 4, 2), D(2026, 6, 15), D(2026, 6, 20), D(2026, 9, 1)]
    w = asa.compute_windows(dates, D(2026, 6, 11), D(2026, 7, 19))
    assert w["split_method"] == "league_break"
    assert w["break_last_game"] == D(2026, 6, 20)  # the 73-day gap that spans the tournament


# ------------------------------------------------------------------ loading
def test_upsert_partitions_keeps_other_seasons_and_widens_schema():
    con = duckdb.connect()
    old = pd.DataFrame({"season": ["2018", "2022"], "player": ["a", "b"], "goals": [1, 2]})
    load.load_dataframe(old, "t", con)
    new = pd.DataFrame({"season": ["2022", "2026"], "player": ["b2", "c"], "club": ["x", "y"]})
    assert load.upsert_partitions(new, "t", con) == 3
    rows = con.execute("SELECT season, player, goals, club FROM t ORDER BY season").fetchall()
    assert rows == [("2018", "a", 1, None), ("2022", "b2", None, "x"), ("2026", "c", None, "y")]


def test_goals_added_flattening_sums_action_types():
    df = pd.DataFrame({"player_id": ["p"], "data": [[
        {"action_type": "Passing", "goals_added_raw": 0.5, "goals_added_above_avg": 0.2, "count_actions": 10},
        {"action_type": "Shooting", "goals_added_raw": 0.1, "goals_added_above_avg": -0.05, "count_actions": 3},
    ]]})
    out = asa._flatten_goals_added(df)
    assert out.loc[0, "goals_added_above_avg"] == pytest.approx(0.15)
    assert out.loc[0, "ga_passing_actions"] == 10
