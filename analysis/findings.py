"""Headline findings for the World Cup -> MLS dashboard, computed from the warehouse.

    python -m analysis.findings          # prints and writes docs/findings.md

Every number in docs/findings.md (and the README / resume bullet that quote it) comes from
this script. Uncertainty: 95% bootstrap intervals (resampling players, 5,000 draws) and
two-sided permutation p-values (10,000 shuffles), seeded for reproducibility. Half-season
windows are small samples, so results are described as "associated with", never "caused".
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

import config

SEED = 2026
KPIS = [("xg", "xG"), ("xa", "xA"), ("goals_added", "g+"), ("shots", "shots"),
        ("goals", "goals"), ("assists", "assists")]
OUT = config.PROJECT_ROOT / "docs" / "findings.md"
POSITION_NAMES = {"GK": "goalkeepers", "CB": "centre-backs", "FB": "full-backs", "DM": "defensive midfielders",
                  "CM": "central midfielders", "AM": "attacking midfielders", "W": "wingers", "ST": "strikers"}


def _spearman(a, b) -> float:
    return float(np.corrcoef(pd.Series(a).rank(), pd.Series(b).rank())[0, 1])


def _perm_p(stat, a, b, rng, n=10_000) -> float:
    obs, b = stat(a, b), np.asarray(b)
    return float(np.mean([abs(stat(a, rng.permutation(b))) >= abs(obs) for _ in range(n)]))


def compute(con: duckdb.DuckDBPyConnection) -> dict:
    rng = np.random.default_rng(SEED)
    mm = config.MIN_MINUTES_WINDOW
    res: dict = {"min_minutes": mm}

    res["counts"] = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in (
        "players", "stg_schedule", "stg_asa_games", "stg_asa_players", "player_xref",
        "mls_player_window", "mls_player_change", "mls_player_percentiles", "mls_team_window")}
    res["wc_2026"] = con.execute("""SELECT count(*) AS players, count(DISTINCT team) AS nations
                                    FROM players WHERE season = ?""", [config.CURRENT_SEASON]).df().iloc[0].to_dict()
    res["windows"] = con.execute("""SELECT * FROM stg_asa_windows WHERE league = 'mls'""").df()
    x = con.execute("SELECT * FROM player_xref").df()
    mls_club = x[(x.fbref_club_country == "United States") & (x.fbref_club_tier == 1)]
    res["match"] = {
        "fbref_mls_club_players": len(mls_club),
        "fbref_mls_club_accepted": int(mls_club.is_accepted.sum()),
        "accepted_total": int(x.is_accepted.sum()),
        "hand_reviewed": int(x.override.notna().sum()),
    }
    ch = con.execute("SELECT * FROM mls_player_change WHERE league = 'mls'").df()
    res["coverage"] = ch[ch.world_cup_player].window_coverage.value_counts().to_dict()
    both = ch[ch.meets_min_minutes_both]
    wc, non = both[both.world_cup_player], both[~both.world_cup_player]
    res["n"] = {"wc": len(wc), "non": len(non),
                "wc_minutes_pre": int(wc.pre_minutes.sum()), "wc_minutes_post": int(wc.post_minutes.sum()),
                "non_minutes_pre": int(non.pre_minutes.sum()), "non_minutes_post": int(non.post_minutes.sum())}

    # pooled (minutes-weighted) per-90 by group x window, same population
    w = con.execute(f"""SELECT w.* FROM mls_player_window w JOIN mls_player_change c USING (league, asa_player_id)
                        WHERE w.league = 'mls' AND c.meets_min_minutes_both""").df()
    pooled = {}
    for (is_wc, win), g in w.groupby(["world_cup_player", "window"]):
        pooled[(bool(is_wc), win)] = {k: g[k].sum() * 90 / g["minutes"].sum() for k, _ in KPIS}
    res["pooled"] = pooled

    # player-level change: difference in mean delta (World Cup - non), bootstrap CI + permutation p
    diffs = []
    for k, label in KPIS:
        a, b = wc[f"delta_{k}_p90"].to_numpy(), non[f"delta_{k}_p90"].to_numpy()
        boots = [rng.choice(a, len(a)).mean() - rng.choice(b, len(b)).mean() for _ in range(5_000)]
        allv, n = np.concatenate([a, b]), len(a)
        perm = []
        for _ in range(10_000):
            rng.shuffle(allv)
            perm.append(allv[:n].mean() - allv[n:].mean())
        obs = a.mean() - b.mean()
        diffs.append({"kpi": label, "wc_mean_change": a.mean(), "non_mean_change": b.mean(),
                      "difference": obs, "ci_low": np.percentile(boots, 2.5), "ci_high": np.percentile(boots, 97.5),
                      "p": float(np.mean(np.abs(perm) >= abs(obs))),
                      "wc_share_up": float((a > 0).mean()), "non_share_up": float((b > 0).mean())})
    res["diffs"] = pd.DataFrame(diffs)

    # rest and World Cup load (World Cup players with both windows >= min minutes)
    res["rest"] = {
        "median": float(wc.days_from_wc_exit_to_first_mls_game.median()),
        "min": int(wc.days_from_wc_exit_to_first_mls_game.min()),
        "max": int(wc.days_from_wc_exit_to_first_mls_game.max()),
        "rho_rest_ga": _spearman(wc.days_from_wc_exit_to_first_mls_game, wc.delta_goals_added_p90),
        "p_rest_ga": _perm_p(_spearman, wc.days_from_wc_exit_to_first_mls_game.to_numpy(float),
                             wc.delta_goals_added_p90.to_numpy(), rng),
        "rho_load_ga": _spearman(wc.wc_minutes, wc.delta_goals_added_p90),
        "p_load_ga": _perm_p(_spearman, wc.wc_minutes.to_numpy(float), wc.delta_goals_added_p90.to_numpy(), rng),
    }
    res["usage"] = {
        "wc_min_per_game_pre": float((wc.pre_minutes / wc.pre_games).median()),
        "non_min_per_game_pre": float((non.pre_minutes / non.pre_games).median()),
    }

    # clubs
    t = con.execute("""SELECT club, any_value(wc_players_sent) AS sent, any_value(wc_minutes_sent) AS wc_min,
                              any_value(xgd_per_game_change) AS d_xgd,
                              max(xgd_per_game) FILTER ("window" = 'pre') AS pre_xgd,
                              max(xgd_per_game) FILTER ("window" = 'post') AS post_xgd
                       FROM mls_team_window WHERE league = 'mls' GROUP BY club""").df()
    s = (t.sent > 0).to_numpy()
    d = t.d_xgd.to_numpy()
    grp_diff = d[s].mean() - d[~s].mean()
    grp_p = float(np.mean([abs((lambda m: d[m].mean() - d[~m].mean())(rng.permutation(s))) >= abs(grp_diff)
                           for _ in range(10_000)]))
    res["clubs"] = {
        "n": len(t), "senders": int(s.sum()),
        "rho_sent_xgd": _spearman(t.sent, t.d_xgd), "p_sent_xgd": _perm_p(_spearman, t.sent.to_numpy(float), d, rng),
        "group_diff": grp_diff, "group_p": grp_p,
        "top_minutes": t.sort_values("wc_min", ascending=False).head(1).iloc[0].to_dict(),
        "most_sent": t.sort_values(["sent", "wc_min"], ascending=False).head(1).iloc[0].to_dict(),
    }
    m = con.execute("""SELECT * FROM mls_player_change WHERE league = 'mls' AND player_name = 'Lionel Messi'""").df()
    res["messi"] = m.iloc[0].to_dict() if len(m) else None
    if res["messi"]:
        res["messi_pct"] = con.execute("""SELECT "window", percentile, peer_count, position FROM mls_player_percentiles
                                          WHERE league = 'mls' AND player_name = 'Lionel Messi' AND kpi = 'xg'
                                          ORDER BY window_order""").fetchall()
    return res


def render(r: dict) -> str:
    w = r["windows"].set_index("window")
    n, m, c, rest = r["n"], r["match"], r["clubs"], r["rest"]
    P = r["pooled"]

    def f(v, nd=3):
        return f"{v:+.{nd}f}" if isinstance(v, float) else str(v)

    rows = "\n".join(
        f"| {d.kpi} | {d.wc_mean_change:+.3f} | {d.non_mean_change:+.3f} | {d.difference:+.3f} "
        f"| [{d.ci_low:+.3f}, {d.ci_high:+.3f}] | {d.p:.2f} | {d.wc_share_up:.0%} / {d.non_share_up:.0%} |"
        for d in r["diffs"].itertuples())
    pooled_rows = "\n".join(
        f"| {label} | {P[(True, 'pre')][k]:.3f} → {P[(True, 'post')][k]:.3f} "
        f"| {P[(False, 'pre')][k]:.3f} → {P[(False, 'post')][k]:.3f} |" for k, label in KPIS)
    cov = r["coverage"]
    messi = r.get("messi")
    messi_line = ""
    if messi:
        pct = r["messi_pct"]
        messi_line = (
            f"5. **Lionel Messi** (Inter Miami, {messi['wc_minutes']} World Cup minutes, {messi['wc_goals']} goals, "
            f"Argentina {messi['wc_round_reached'].lower()}) was back in MLS "
            f"{messi['days_from_wc_exit_to_first_mls_game']} days after the final. His xG/90 went "
            f"{messi['pre_xg_p90']:.2f} → {messi['post_xg_p90']:.2f} ({messi['pre_minutes']:,} → "
            f"{messi['post_minutes']:,} minutes) and stayed at the "
            + " / ".join(f"{p:.0f}th" for _, p, _, _ in pct)
            + f" percentile for xG/90 among {POSITION_NAMES.get(pct[0][3], pct[0][3])} with >= {r['min_minutes']} "
            f"minutes ({pct[0][2]} pre / {pct[-1][2]} post).\n")
    return f"""# Findings: did the 2026 World Cup help or hurt MLS players?

_Generated by `python -m analysis.findings` on {dt.date.today().isoformat()} from the DuckDB warehouse.
Do not edit by hand; re-run the script after a refresh._

**Read this first.** Each window is half a season (MLS pre-break: {w.loc['pre', 'season_first_game']:%Y-%m-%d} to
{w.loc['pre', 'break_last_game']:%Y-%m-%d}; post-break: {w.loc['post', 'break_first_game']:%Y-%m-%d} to
{w.loc['post', 'season_last_game']:%Y-%m-%d}; break = {w.loc['pre', 'break_days']} days, World Cup
{w.loc['pre', 'world_cup_start']:%Y-%m-%d} to {w.loc['pre', 'world_cup_end']:%Y-%m-%d}). Only
**{n['wc']} World Cup players** and **{n['non']} other MLS players** have >= {r['min_minutes']} minutes in both
windows. Differences below are *associated with* playing at the World Cup; they are not causal estimates
(World Cup players are better, play more minutes and are picked non-randomly).

## Sample
- {m['accepted_total']} 2026 World Cup players matched to 2026 MLS players ({m['fbref_mls_club_accepted']} of the
  {m['fbref_mls_club_players']} that fbref lists at MLS clubs; {m['hand_reviewed']} candidate rows hand-reviewed).
- Of the {m['accepted_total']}: {cov.get('both windows', 0)} played in both windows,
  {cov.get('post only (arrived after the break)', 0)} arrived after the break,
  {cov.get('pre only (no minutes after the break)', 0)} had no minutes after it.
- Comparison population (>= {r['min_minutes']} min in both windows): World Cup {n['wc']} players
  ({n['wc_minutes_pre']:,} pre / {n['wc_minutes_post']:,} post minutes); others {n['non']} players
  ({n['non_minutes_pre']:,} / {n['non_minutes_post']:,}).

## Pooled per-90 (minutes-weighted), pre → post

| KPI | World Cup players (n={n['wc']}) | Other MLS players (n={n['non']}) |
|---|---|---|
{pooled_rows}

## Player-level change (post − pre per 90): World Cup minus others

| KPI | WC mean change | Others mean change | Difference | 95% bootstrap CI | permutation p | share who improved (WC / others) |
|---|---|---|---|---|---|---|
{rows}

## Headline findings
1. **No clear World Cup effect.** For every KPI the 95% interval for the difference in per-90 change includes
   zero (smallest p = {r['diffs'].p.min():.2f}, {r['diffs'].loc[r['diffs'].p.idxmin(), 'kpi']}). Directionally,
   World Cup players' pooled xG/90 rose {P[(True, 'pre')]['xg']:.3f} → {P[(True, 'post')]['xg']:.3f} while others
   were flat ({P[(False, 'pre')]['xg']:.3f} → {P[(False, 'post')]['xg']:.3f}), and their assists/90 fell
   ({P[(True, 'pre')]['assists']:.3f} → {P[(True, 'post')]['assists']:.3f}).
2. **Rest and tournament load are not associated with the change.** World Cup players returned a median
   {rest['median']:.0f} days after their nation's last match (range {rest['min']}–{rest['max']}). Spearman ρ between
   rest days and g+/90 change = {rest['rho_rest_ga']:+.2f} (p = {rest['p_rest_ga']:.2f}); between World Cup minutes
   and g+/90 change = {rest['rho_load_ga']:+.2f} (p = {rest['p_load_ga']:.2f}); n = {n['wc']}.
3. **Clubs: players sent is not associated with the change in xG difference.** Across {c['n']} clubs, ρ(World Cup
   players sent, change in xGD/game) = {c['rho_sent_xgd']:+.2f} (p = {c['p_sent_xgd']:.2f}). The {c['senders']}
   clubs that sent at least one player changed by {c['group_diff']:+.2f} xGD/game more than the
   {c['n'] - c['senders']} that sent none (p = {c['group_p']:.2f}).
4. **The biggest load fell on {c['top_minutes']['club']}**: {c['top_minutes']['wc_min']:,} World Cup minutes from
   {c['top_minutes']['sent']} players; its xGD/game went {c['top_minutes']['pre_xgd']:+.2f} →
   {c['top_minutes']['post_xgd']:+.2f}. {c['most_sent']['club']} sent the most players ({c['most_sent']['sent']}).
   Single clubs are anecdotes, not evidence.
{messi_line}
Also: World Cup players were already heavier-used regulars before the break (median
{r['usage']['wc_min_per_game_pre']:.0f} vs {r['usage']['non_min_per_game_pre']:.0f} minutes per appearance), which is
why raw comparisons between the two groups are confounded.
"""


def main() -> None:
    with duckdb.connect(str(config.DUCKDB_PATH), read_only=True) as con:
        r = compute(con)
    text = render(r)
    Path(OUT).parent.mkdir(parents=True, exist_ok=True)
    Path(OUT).write_text(text)
    print(text)
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
