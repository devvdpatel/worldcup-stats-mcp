"""Link 2026 World Cup players (fbref) to 2026 MLS / MLS NEXT Pro players (ASA).

    python -m etl.xref

Signals, per (World Cup player, ASA player) candidate pair:
  * name similarity on normalized names (accents, case and punctuation stripped;
    token order ignored, so "Son Heung-min" == "Heung-Min Son"),
  * club: the fbref club at tournament time resolves to an ASA team the player
    appeared for in the 2026 regular season (tier-1 US clubs -> MLS, lower US tiers ->
    MLS NEXT Pro; "Miami FC" can therefore never become Inter Miami),
  * nation: fbref national team vs ASA nationality, through an alias map (ASA lists
    one nationality, so dual nationals can legitimately differ: supporting signal only),
  * birth year: fbref `born` vs ASA birth_date.

Outputs:
  player_xref (DuckDB) - the best candidate per World Cup player with match_method,
                         confidence, the individual signals, and `is_accepted`
                         (confidence >= ACCEPT_AT, unambiguous, not overridden).
                         Only accepted rows drive the world_cup_player flag.
  data/review/player_match_review.csv - unmatched World Cup players at US clubs plus
                         every low-confidence / ambiguous / conflicting link, for hand review,
                         with World Cup and pre/post MLS minutes as evidence. `decision` and
                         `note` typed into it survive re-runs.
  reference/player_match_overrides.csv (tracked, optional) - hand decisions:
                         fbref_player_id, asa_player_id, decision (accept|reject|dismiss), note.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from collections import defaultdict

import duckdb
import pandas as pd

import config

ACCEPT_AT = 0.85          # minimum confidence for a link to set world_cup_player
# hand decisions (reference/player_match_overrides.csv `decision` column):
#   accept  - link this World Cup player to asa_player_id (or to the matcher's candidate)
#   reject  - not this candidate (asa_player_id); a different future candidate is reviewed again
#   dismiss - reviewed, no ASA match exists; drop from the review file while still unmatched
OVERRIDE_DECISIONS = ("accept", "reject", "dismiss")
REVIEW_BELOW = 0.95       # links below this confidence are written to the review file
MIN_NAME_SIM = 0.80       # candidates below this name similarity are never linked

# characters NFKD does not decompose
_TRANSLIT = str.maketrans({"ø": "o", "Ø": "o", "ß": "ss", "æ": "ae", "Æ": "ae", "œ": "oe",
                           "đ": "d", "Đ": "d", "ł": "l", "Ł": "l", "ı": "i", "þ": "th",
                           "ð": "d", "ħ": "h"})

# fbref national-team names / ASA nationalities -> one canonical form (after normalize())
NATION_ALIASES = {
    "usa": "united states", "united states of america": "united states",
    "korea republic": "south korea", "republic of korea": "south korea",
    "ir iran": "iran", "cote d ivoire": "ivory coast", "cote divoire": "ivory coast",
    "turkiye": "turkey", "czechia": "czech republic",
    "bosnia herz": "bosnia and herzegovina", "bosnia herzegovina": "bosnia and herzegovina",
    "cabo verde": "cape verde", "republic of cape verde": "cape verde",
    "congo dr": "dr congo", "democratic republic of the congo": "dr congo",
    "republic of ireland": "ireland", "trinidad and tobago": "trinidad and tobago",
    "north macedonia": "macedonia", "curacao": "curacao",
    "st kitts and nevis": "saint kitts and nevis", "china pr": "china",
}

# fbref club abbreviations -> words used in ASA team names (applied to normalized text)
CLUB_WORD_ALIASES = [
    (r"\butd\b", "united"), (r"^ne\b", "new england"), (r"^ny\b", "new york"),
    (r"^nycfc$", "new york city"), (r"^nyrb$", "new york red bulls"),
    (r"^sporting kc$", "sporting kansas city"), (r"^rsl$", "real salt lake"),
    (r"^la galaxy$", "la galaxy"), (r"^dc united$", "d c united"),
    (r"^cf montreal$", "cf montreal"), (r"^st louis$", "st louis city"),
]
_CLUB_SUFFIX = re.compile(r"\b(fc|sc|cf)\b")


def normalize(text: str | None) -> str:
    """Lowercase, strip accents/punctuation, collapse spaces: "Jürgen Locadia" -> "jurgen locadia"."""
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return ""
    t = unicodedata.normalize("NFKD", str(text).translate(_TRANSLIT))
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).lower()
    t = t.replace("&", " and ")
    t = re.sub(r"['’`]", "", t)          # O'Neill -> oneill
    t = re.sub(r"[^a-z0-9]+", " ", t)    # hyphens, dots, en dashes -> space
    return re.sub(r"\s+", " ", t).strip()


def canonical_nation(name: str | None) -> str:
    n = normalize(name)
    return NATION_ALIASES.get(n, n)


def name_similarity(a: str, b: str) -> float:
    """1.0 exact; 0.98 same tokens in another order; 0.92 one name's tokens contain the
    other's (>= 2 tokens, e.g. a dropped second surname); else a sequence ratio."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = a.split(), b.split()
    if sorted(ta) == sorted(tb):
        return 0.98
    sa, sb = set(ta), set(tb)
    if min(len(sa), len(sb)) >= 2 and (sa <= sb or sb <= sa):
        return 0.92
    return round(difflib.SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio(), 3)


def _bare(text: str) -> str:
    return re.sub(r"\s+", " ", _CLUB_SUFFIX.sub("", text)).strip()


def resolve_club(club: str | None, teams: pd.DataFrame) -> str | None:
    """Map an fbref club name to one ASA team_id within `teams` (one league), else None.

    Strict on purpose: the fbref name (after abbreviation aliases) must equal the ASA team
    name, short name or abbreviation, or equal the team name once FC/SC/CF are dropped
    from both. No loose word overlap, so "Miami FC" (USL) never becomes an Inter Miami
    team. An unresolved club just means club_match is False and the row is reviewed.
    """
    c = normalize(club)
    if not c:
        return None
    for pat, rep in CLUB_WORD_ALIASES:
        c = re.sub(pat, rep, c)
    hits = set()
    for t in teams.itertuples():
        full = {normalize(t.team_name), normalize(t.team_short_name), normalize(t.team_abbreviation)}
        if c in full or (_bare(c) and _bare(c) == _bare(normalize(t.team_name))):
            hits.add(t.team_id)
    return hits.pop() if len(hits) == 1 else None


def _score(name_sim: float, club: bool, nation: bool, by: bool) -> tuple[str, float]:
    """Rule ladder -> (match_method, confidence). Order = most to least trustworthy."""
    exact = name_sim >= 0.98
    if exact and club:
        return "name_club", 0.99 if (by or nation) else 0.97
    if name_sim >= 0.90 and club and (by or nation):
        return "fuzzy_name_club", 0.95
    if exact and by and nation:
        return "name_nation_birthyear", 0.93
    if name_sim >= 0.90 and club:
        return "fuzzy_name_club", 0.87
    if exact and by:
        return "name_birthyear", 0.87
    if name_sim >= 0.90 and by and nation:
        return "fuzzy_name_nation_birthyear", 0.88
    if exact and nation:
        return "name_nation", 0.80
    if name_sim >= MIN_NAME_SIM and by and nation:
        return "weak_fuzzy_nation_birthyear", 0.75
    if name_sim >= MIN_NAME_SIM and (club or (by and name_sim >= 0.9)):
        return "weak_fuzzy", 0.60
    if exact:
        return "name_only", 0.50
    return "none", 0.0


def match_players(wc: pd.DataFrame, asa_players: pd.DataFrame, asa_player_teams: pd.DataFrame,
                  asa_teams: pd.DataFrame, overrides: pd.DataFrame | None = None) -> pd.DataFrame:
    """Return one row per World Cup player: the best ASA candidate and its signals.

    wc:               fbref_player_id, player, team (nation), club, club_country, club_tier, birth_year
    asa_players:      league, player_id, player_name, nationality, birth_date
    asa_player_teams: league, player_id, team_id   (teams appeared for in the season)
    asa_teams:        league, team_id, team_name, team_short_name, team_abbreviation
    """
    ap = asa_players.copy()
    ap["norm"] = ap["player_name"].map(normalize)
    ap["nation_c"] = ap["nationality"].map(canonical_nation)
    ap["birth_year"] = pd.to_datetime(ap["birth_date"], errors="coerce").dt.year.astype("Int64")
    teams_of = asa_player_teams.groupby(["league", "player_id"])["team_id"].agg(set).to_dict()
    team_name = dict(zip(zip(asa_teams.league, asa_teams.team_id), asa_teams.team_name))

    index: dict[str, list[int]] = defaultdict(list)   # name token -> asa row positions
    for pos, toks in enumerate(ap["norm"].str.split()):
        for tok in set(toks):
            if len(tok) >= 3:
                index[tok].append(pos)
    by_team: dict[tuple, list[int]] = defaultdict(list)
    for pos, (lg, pid) in enumerate(zip(ap.league, ap.player_id)):
        for tid in teams_of.get((lg, pid), ()):
            by_team[(lg, tid)].append(pos)

    rows = []
    for w in wc.itertuples(index=False):
        wnorm, wnat = normalize(w.player), canonical_nation(w.team)
        us_club = w.club_country == "United States"
        club_league = ("mls" if w.club_tier == 1 else "mlsnp") if us_club and pd.notna(w.club_tier) else None
        club_team = None
        if club_league:
            club_team = resolve_club(w.club, asa_teams[asa_teams.league == club_league])
        cand = {p for tok in set(wnorm.split()) if len(tok) >= 3 for p in index.get(tok, ())}
        if club_team:
            cand |= set(by_team.get((club_league, club_team), ()))
        scored = []
        for pos in cand:
            a = ap.iloc[pos]
            sim = name_similarity(wnorm, a.norm)
            club_ok = bool(club_team) and a.league == club_league and \
                club_team in teams_of.get((a.league, a.player_id), set())
            nation_ok = bool(wnat) and wnat == a.nation_c
            by_ok = pd.notna(w.birth_year) and pd.notna(a.birth_year) and int(w.birth_year) == int(a.birth_year)
            method, conf = _score(sim, club_ok, nation_ok, by_ok)
            if conf > 0:
                scored.append((conf, sim, pos, method, club_ok, nation_ok, by_ok))
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        base = {
            "fbref_player_id": w.fbref_player_id, "fbref_player": w.player, "nation": w.team,
            "fbref_club": w.club, "fbref_club_country": w.club_country,
            "fbref_club_tier": w.club_tier, "fbref_birth_year": w.birth_year,
            "fbref_club_asa_team_id": club_team, "fbref_club_league": club_league,
            "wc_minutes": getattr(w, "minutes", None),
        }
        if not scored:
            rows.append({**base, "match_method": "unmatched", "confidence": 0.0, "n_candidates": 0})
            continue
        conf, sim, pos, method, club_ok, nation_ok, by_ok = scored[0]
        a = ap.iloc[pos]
        runner_up = scored[1][0] if len(scored) > 1 else None
        rows.append({
            **base,
            "asa_league": a.league, "asa_player_id": a.player_id, "asa_player_name": a.player_name,
            "asa_nationality": a.nationality, "asa_birth_year": a.birth_year,
            "asa_teams_2026": ", ".join(sorted(team_name.get((a.league, t), t)
                                               for t in teams_of.get((a.league, a.player_id), ()))),
            "name_similarity": sim, "club_match": club_ok, "nation_match": nation_ok,
            "birth_year_match": by_ok, "match_method": method, "confidence": conf,
            "n_candidates": len(scored), "runner_up_confidence": runner_up,
        })
    return _finalize(pd.DataFrame(rows), overrides)


def _finalize(x: pd.DataFrame, overrides: pd.DataFrame | None = None) -> pd.DataFrame:
    """Flag ambiguity / conflicts, apply hand overrides, set is_accepted + review reasons."""
    for col in ("asa_league", "asa_player_id", "runner_up_confidence"):
        if col not in x:
            x[col] = None
    x["ambiguous"] = x["runner_up_confidence"].notna() & (x["runner_up_confidence"] >= x["confidence"])
    linked = x["asa_player_id"].notna() & (x["confidence"] >= ACCEPT_AT)
    dup = x[linked].groupby(["asa_league", "asa_player_id"])["fbref_player_id"].transform("count")
    x["conflict"] = False
    x.loc[dup.index, "conflict"] = dup > 1
    x["override"] = None
    x["override_note"] = None
    if overrides is not None and not overrides.empty:
        for o in overrides.itertuples():
            decision = str(o.decision).strip().lower()
            if decision not in OVERRIDE_DECISIONS:
                continue
            new_id = o.asa_player_id if isinstance(o.asa_player_id, str) else ""
            m = x["fbref_player_id"].eq(o.fbref_player_id)
            if decision == "reject" and new_id:
                # a reject is about one candidate: if a rebuild proposes someone else, review again
                m &= x["asa_player_id"].eq(new_id)
            elif decision == "dismiss":
                # "no ASA match, stop asking": only while the matcher still finds no candidate
                m &= x["asa_player_id"].isna() if not new_id else x["asa_player_id"].eq(new_id)
            x.loc[m, "override"] = decision
            x.loc[m, "override_note"] = getattr(o, "note", None) or None
            if decision == "accept" and new_id and m.any() and (x.loc[m, "asa_player_id"] != new_id).any():
                # hand-picked a different ASA player: the matcher's evidence no longer applies
                x.loc[m, "asa_player_id"] = new_id
                x.loc[m, "match_method"] = "manual_override"
                for col in ("asa_player_name", "asa_nationality", "asa_birth_year", "asa_teams_2026",
                            "name_similarity", "club_match", "nation_match", "birth_year_match"):
                    if col in x:
                        x[col] = x[col].astype(object)   # pandas 3: typed columns reject None
                        x.loc[m, col] = None
    x["is_accepted"] = (linked & ~x["ambiguous"] & ~x["conflict"]
                        & ~x["override"].isin(["reject", "dismiss"])) | x["override"].eq("accept")

    def reason(r) -> str | None:
        us_club = r.fbref_club_country == "United States"
        mls_club = us_club and pd.notna(r.fbref_club_tier) and int(r.fbref_club_tier) == 1
        if r.override in OVERRIDE_DECISIONS:
            return None
        if r.match_method == "unmatched" or pd.isna(r.asa_player_id):
            if mls_club:
                return "unmatched: fbref lists an MLS club but no 2026 ASA player matches"
            if us_club:
                return "unmatched: US club outside MLS / MLS NEXT Pro (not covered by ASA load)"
            return None
        out = []
        if r.ambiguous:
            out.append("ambiguous: another candidate scores the same")
        if r.conflict:
            out.append("conflict: ASA player linked to more than one World Cup player")
        if r.confidence < REVIEW_BELOW and (r.confidence >= 0.5 or us_club):
            out.append(f"low confidence ({r.match_method}, {r.confidence:.2f})")
        if mls_club and not r.is_accepted:
            out.append("not accepted although fbref lists an MLS club")
        return "; ".join(out) or None

    x["review_reason"] = x.apply(reason, axis=1)
    x["needs_review"] = x["review_reason"].notna()
    return x


# --------------------------------------------------------------------------- DuckDB
WC_SQL = """
SELECT p.fbref_player_id, p.player, p.team, p.club, p.club_country, p.club_tier,
       TRY_CAST(p.born AS INTEGER) AS birth_year, p.minutes
FROM players p WHERE p.season = ?
"""


REVIEW_PATH = config.REVIEW_DIR / "player_match_review.csv"

# hand-review file: what to decide first, then the evidence, then the ids
REVIEW_COLUMNS = [
    "decision", "note", "review_reason",
    "fbref_player", "nation", "fbref_club", "fbref_birth_year", "wc_minutes",
    "asa_player_name", "asa_nationality", "asa_birth_year", "asa_teams_2026",
    "mls_pre_minutes", "mls_post_minutes",
    "match_method", "confidence", "is_accepted",
    "name_similarity", "club_match", "nation_match", "birth_year_match",
    "fbref_player_id", "asa_league", "asa_player_id",
]


def load_overrides() -> pd.DataFrame:
    path = config.MATCH_OVERRIDES_PATH
    if not path.exists():
        return pd.DataFrame(columns=["fbref_player_id", "asa_player_id", "decision", "note"])
    return pd.read_csv(path, dtype=str).fillna("")


def _review_key(df: pd.DataFrame) -> pd.Series:
    return df["fbref_player_id"].astype(str) + "|" + df["asa_player_id"].fillna("").astype(str)


def write_review(x: pd.DataFrame, evidence: pd.DataFrame, path=None) -> pd.DataFrame:
    """Write rows needing review. Decisions/notes already typed into an earlier version of
    the file are carried over (matched on fbref_player_id + asa_player_id), so re-running
    the pipeline never wipes hand review."""
    path = path or REVIEW_PATH
    review = x[x["needs_review"]].merge(evidence, on=["asa_league", "asa_player_id"], how="left")
    review["decision"], review["note"] = "", ""
    if path.exists():
        old = pd.read_csv(path, dtype=str).fillna("")
        if {"decision", "fbref_player_id", "asa_player_id"} <= set(old.columns):
            if "note" not in old:
                old["note"] = ""
            kept = old[(old["decision"] != "") | (old["note"] != "")]
            prev = dict(zip(_review_key(kept), zip(kept["decision"], kept["note"])))
            keys = _review_key(review)
            review["decision"] = [prev.get(k, ("", ""))[0] for k in keys]
            review["note"] = [prev.get(k, ("", ""))[1] for k in keys]
    review = review.sort_values(["is_accepted", "confidence"], ascending=[False, False])
    review[REVIEW_COLUMNS].to_csv(path, index=False)
    return review


def build(con: duckdb.DuckDBPyConnection | None = None, season: str = config.CURRENT_SEASON) -> pd.DataFrame:
    own = con is None
    if own:
        con = duckdb.connect(str(config.DUCKDB_PATH))
    try:
        wc = con.execute(WC_SQL, [season]).df()
        asa_players = con.execute(
            "SELECT league, player_id, player_name, nationality, birth_date FROM stg_asa_players").df()
        asa_player_teams = con.execute("""
            SELECT DISTINCT league, player_id, team_id FROM stg_asa_player_xgoals WHERE "window" = 'season'
            UNION
            SELECT DISTINCT league, player_id, team_id FROM stg_asa_goalkeeper_goals_added WHERE "window" = 'season'
        """).df()
        asa_teams = con.execute(
            "SELECT league, team_id, team_name, team_short_name, team_abbreviation FROM stg_asa_teams").df()
        x = match_players(wc, asa_players, asa_player_teams, asa_teams, load_overrides())
        x.insert(0, "season", season)
        # explicit types: all-empty columns would otherwise be inferred as INTEGER
        for col in ("review_reason", "override", "override_note", "match_method", "asa_league",
                    "asa_player_id", "asa_player_name", "asa_nationality", "asa_teams_2026",
                    "fbref_club_asa_team_id", "fbref_club_league"):
            if col in x:
                x[col] = x[col].astype("string")
        for col in ("fbref_club_tier", "fbref_birth_year", "asa_birth_year", "wc_minutes", "n_candidates"):
            if col in x:
                x[col] = pd.to_numeric(x[col], errors="coerce").astype("Int64")
        con.register("_x", x)
        con.execute("CREATE OR REPLACE TABLE player_xref AS SELECT * FROM _x")
        con.unregister("_x")
        evidence = con.execute("""
            SELECT league AS asa_league, player_id AS asa_player_id,
                   sum(minutes_played) FILTER ("window" = 'pre')  AS mls_pre_minutes,
                   sum(minutes_played) FILTER ("window" = 'post') AS mls_post_minutes
            FROM stg_asa_player_xgoals GROUP BY ALL""").df()
        write_review(x, evidence)
        return x
    finally:
        if own:
            con.close()


def _at_mls_club(df: pd.DataFrame) -> pd.Series:
    return (df.fbref_club_country.eq("United States") & df.fbref_club_tier.eq(1)).fillna(False).astype(bool)


def summary(x: pd.DataFrame) -> dict:
    mls_club = x[_at_mls_club(x)]
    acc = x[x.is_accepted]
    return {
        "world_cup_players": len(x),
        "fbref_mls_club_players": len(mls_club),
        "fbref_mls_club_players_accepted": int(mls_club.is_accepted.sum()),
        "accepted_links_total": len(acc),
        "accepted_mls": int((acc.asa_league == "mls").sum()),
        "accepted_mlsnp": int((acc.asa_league == "mlsnp").sum()),
        "accepted_not_at_fbref_mls_club": int((~_at_mls_club(acc)).sum()),
        "needs_review": int(x.needs_review.sum()),
        "methods": acc.match_method.value_counts().to_dict(),
    }


if __name__ == "__main__":
    out = build()
    s = summary(out)
    rate = s["fbref_mls_club_players_accepted"] / max(s["fbref_mls_club_players"], 1)
    print(f"player_xref: {len(out)} World Cup players; {s['accepted_links_total']} accepted links "
          f"({s['accepted_mls']} MLS, {s['accepted_mlsnp']} MLS NEXT Pro)")
    print(f"  match rate, World Cup players fbref lists at MLS clubs: "
          f"{s['fbref_mls_club_players_accepted']}/{s['fbref_mls_club_players']} ({rate:.0%})")
    print(f"  accepted links whose fbref club is not an MLS club: {s['accepted_not_at_fbref_mls_club']}")
    print(f"  methods: {s['methods']}")
    print(f"  {s['needs_review']} rows -> {config.REVIEW_DIR / 'player_match_review.csv'}")
