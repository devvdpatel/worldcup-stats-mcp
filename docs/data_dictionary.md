# Data dictionary

Warehouse: `data/worldcup.duckdb` (gitignored; rebuild with the refresh steps in the README).
Exports for Tableau: `data/exports/<table>.csv` and `.xlsx`, for the four `mls_*` marts and `wc_nation_rounds`.

## Sources

| Source | What we use | Access | Attribution |
|---|---|---|---|
| **fbref.com** (Sports Reference) | FIFA World Cup 2010–2026 schedules, player and team season stats; for 2022+, each player's club at tournament time | `soccerdata` (headless browser, 7 s per page, on-disk cache) | Data from FBref.com. Used for personal, non-commercial, educational analysis. |
| **American Soccer Analysis (ASA)** | MLS and MLS NEXT Pro 2026 regular season: players, teams, games, player xG/xA, goals added (g+), team xG, per-game minutes | `itscalledsoccer` client over the public API (`app.americansocceranalysis.com/api/v1`), JSON cache, ≥ 1 s between requests | Data from American Soccer Analysis (americansocceranalysis.com). |

**Definitions (ASA):** `xg` = expected goals from shots (includes penalties); `xa` = expected assists
(xG of shots a player's passes set up); `assists` = primary assists; `goals_added` = **g+ above average**,
the change in a team's scoring/conceding probability credited to a player's on-ball actions, relative to an
average player at the same position (field players: dribbling, fouling, interrupting, passing, receiving,
shooting; goalkeepers: claiming, fielding, handling, passing, shot-stopping, sweeping).
**Per 90** = value × 90 ÷ minutes.

## Windows
`stg_asa_windows` holds the dates (`window` ∈ season, pre, post), all derived from data:
- World Cup dates = first and last match date in the fbref 2026 schedule (`world_cup_start`, `world_cup_end`).
- **League break** = the gap between consecutive game dates covering the most tournament days. It counts when
  both that overlap and the gap are ≥ 14 days (`config.BREAK_MIN_GAP_DAYS`); `split_method = league_break`.
  pre = first game → last game before the gap; post = first game after the gap → last game.
- Without such a gap (MLS NEXT Pro 2026: no gap > 5 days) the split is the tournament itself
  (`split_method = world_cup_dates`), and games played during the World Cup fall in neither window.
- `start_date` / `end_date` of pre and post are padded by one day (UTC vs local dates); the season window shows
  the exact first and last game.

## Marts (exported)

### `mls_player_window`: one row per player per window (pre / post)
Key: `league, window, asa_player_id`. Every player with minutes in a window; nothing is filtered on minutes.

| Column | Type | Meaning |
|---|---|---|
| `league` | text | `mls` or `mlsnp` (MLS NEXT Pro) |
| `window` | text | `pre` (before the World Cup break) or `post` |
| `window_order` | int | 1 = pre, 2 = post (sorting, dumbbell path) |
| `window_start`, `window_end` | date | Window bounds used for the API pull (padded ±1 day) |
| `split_method` | text | `league_break` or `world_cup_dates` (see Windows) |
| `asa_player_id` | text | ASA player id |
| `player_name` | text | ASA player name |
| `player_label` | text | `Name (club_latest)`, unique per league; use for pickers and parameters |
| `asa_team_id`, `club`, `club_abbreviation` | text | The club the player logged the most minutes for in this window |
| `club_latest` | text | The player's club in his latest window; identical on both rows (use for club filters) |
| `n_clubs` | int | Clubs played for inside the window (2 = traded) |
| `position` | text | ASA general position over the **season** (GK, CB, FB, DM, CM, AM, W, ST): the percentile peer group |
| `position_group` | text | Goalkeeper / Defender / Midfielder / Forward |
| `window_position` | text | ASA general position within this window |
| `nationality` | text | ASA nationality (one value; may differ from the World Cup nation for dual nationals) |
| `birth_date` | date | ASA birth date |
| `age` | int | Age in whole years at `window_start` |
| `minutes` | int | Minutes played in the window |
| `games` | int | Appearances (games with minutes > 0) from per-game data |
| `first_game_date`, `last_game_date` | date | First / last appearance in the window (UTC date) |
| `goals`, `assists`, `shots`, `shots_on_target`, `key_passes` | int | Totals (assists = primary assists) |
| `xg`, `xa` | float | Expected goals / expected assists, totals |
| `goals_added` | float | g+ above average, total (field + goalkeeper tables summed) |
| `goals_added_raw` | float | g+ raw (not relative to average), total |
| `goals_p90`, `assists_p90`, `shots_p90`, `xg_p90`, `xa_p90`, `goals_added_p90` | float | Per-90 rates |
| `world_cup_player` | bool | True if an accepted `player_xref` row links this player to a 2026 World Cup player |
| `fbref_player_id` | text | The linked fbref World Cup player id (null when not a World Cup player) |
| `min_minutes_threshold` | int | Threshold used for the flag (450) |
| `meets_min_minutes` | bool | `minutes >= min_minutes_threshold` |

### `mls_player_change`: one row per player
Key: `league, asa_player_id` (and `league, player_label`). Players with minutes in either window.

| Column | Type | Meaning |
|---|---|---|
| `league`, `asa_player_id`, `player_name`, `player_label` | text | As above |
| `position`, `position_group`, `nationality` | text | As above |
| `age_pre` | int | Age at the pre window start (post window if no pre minutes) |
| `club_pre`, `club_post` | text | Main club in each window (null if no minutes in that window) |
| `club`, `asa_team_id` | text | Latest club (post if present, else pre) |
| `window_coverage` | text | `both windows`, `post only (arrived after the break)`, `pre only (no minutes after the break)` |
| `pre_minutes`, `post_minutes`, `pre_games`, `post_games` | int | Minutes / appearances per window (0 if none) |
| `pre_meets_min_minutes`, `post_meets_min_minutes`, `meets_min_minutes_both` | bool | Threshold flags |
| `min_minutes_threshold` | int | 450 |
| `pre_<kpi>_p90`, `post_<kpi>_p90` | float | Per-90 in each window, `<kpi>` ∈ goals, assists, shots, xg, xa, goals_added |
| `delta_<kpi>_p90` | float | `post − pre` (null unless the player has both windows) |
| `world_cup_player` | bool | As above |
| `fbref_player_id` | text | Linked World Cup player |
| `world_cup_nation` | text | National team at the 2026 World Cup (fbref name, e.g. `Korea Republic`) |
| `wc_minutes`, `wc_matches`, `wc_starts`, `wc_goals`, `wc_assists` | int | 2026 World Cup totals (fbref) |
| `wc_round_reached` | text | Group stage, Round of 32, Round of 16, Quarter-finals, Fourth place, Third place, Runner-up, Champion |
| `wc_round_reached_rank` | int | 1 (group stage) to 8 (champion) |
| `wc_nation_last_match` | date | The nation's last World Cup match |
| `first_post_break_game` | date | First MLS appearance after the break |
| `days_from_wc_exit_to_first_mls_game` | int | `first_post_break_game − wc_nation_last_match` |

### `mls_player_percentiles`: one row per player per window per KPI (long)
Key: `league, window, asa_player_id, kpi`. Only rows with `meets_min_minutes` are ranked.

| Column | Type | Meaning |
|---|---|---|
| `league`, `window`, `window_order`, `asa_player_id`, `player_name`, `player_label` | various | As above |
| `club`, `club_latest`, `position`, `position_group`, `world_cup_player`, `minutes` | various | As above |
| `kpi` | text | goals, assists, shots, xg, xa, goals_added (goalkeepers: goals_added only) |
| `kpi_order` | int | 1–6 display order |
| `value_p90` | float | The player's per-90 for this KPI and window |
| `percentile` | float | `100 × percent_rank()` among peers: same league, window and season `position`, all ≥ 450 minutes (0 = lowest, 100 = highest, ties share the lower rank) |
| `peer_count` | int | Size of that peer pool (smallest MLS pool: attacking midfielders, pre-break) |

### `mls_team_window`: one row per club per window
Key: `league, window, asa_team_id`.

| Column | Type | Meaning |
|---|---|---|
| `league`, `window`, `window_order`, `asa_team_id`, `club`, `club_abbreviation` | various | As above |
| `games` | int | Games in the window |
| `goals_for`, `goals_against`, `points` | int | Totals |
| `xg_for`, `xg_against`, `xpoints` | float | ASA team xG for / against, expected points |
| `xg_for_per_game`, `xg_against_per_game`, `xgd_per_game` | float | Per game; `xgd` = xG for − xG against |
| `goals_for_per_game`, `goals_against_per_game`, `points_per_game` | float | Per game |
| `xgd_per_game_change` | float | Club-level `post − pre` xGD per game (same value on both rows) |
| `wc_players_sent` | int | World Cup players whose main **pre-break** club was this club (same value on both rows) |
| `wc_minutes_sent` | int | Their total 2026 World Cup minutes |
| `wc_players_sent_names` | text | Their names, most World Cup minutes first |
| `wc_players_arrived` | int | World Cup players with no pre-break MLS minutes who played for the club after the break |
| `wc_players_arrived_names` | text | Their names |

### `wc_nation_rounds`: one row per 2026 World Cup nation (helper)
| Column | Type | Meaning |
|---|---|---|
| `season` | text | `2026` |
| `nation` | text | fbref national-team name |
| `last_match_date` | date | Date of the nation's last match |
| `nation_matches` | int | Matches played |
| `round_reached`, `round_reached_rank` | text / int | As in `mls_player_change` (final and third-place results parsed from scores, shoot-outs included) |

## Matching: `player_xref` (warehouse only)
One row per 2026 World Cup player (fbref), holding the best ASA candidate.

| Column | Meaning |
|---|---|
| `season`, `fbref_player_id`, `fbref_player`, `nation` | World Cup player |
| `fbref_club`, `fbref_club_country`, `fbref_club_tier` | Club at tournament time (fbref), its country and league tier |
| `fbref_birth_year`, `wc_minutes` | fbref birth year, World Cup minutes |
| `fbref_club_asa_team_id`, `fbref_club_league` | The fbref club resolved to an ASA team (tier 1 US → `mls`, lower US tiers → `mlsnp`) |
| `asa_league`, `asa_player_id`, `asa_player_name`, `asa_nationality`, `asa_birth_year`, `asa_teams_2026` | Best ASA candidate |
| `name_similarity` | 1.0 exact, 0.98 same tokens in another order, 0.92 one name contains the other, else a sequence ratio |
| `club_match`, `nation_match`, `birth_year_match` | The individual signals |
| `match_method`, `confidence` | Rule-ladder result, e.g. `name_club` 0.99, `name_nation_birthyear` 0.93 |
| `n_candidates`, `runner_up_confidence`, `ambiguous`, `conflict` | Ambiguity checks (ambiguous / conflicting links are never accepted) |
| `override`, `override_note` | Hand decision from `reference/player_match_overrides.csv`: accept / reject / dismiss |
| `is_accepted` | Drives `world_cup_player` |
| `review_reason`, `needs_review` | Why a row goes to `data/review/player_match_review.csv` |

## Staging tables (warehouse only)
- fbref: `stg_schedule`, `stg_player_{standard,shooting,keeper,playing_time,misc}`, `stg_team_*`, `stg_player_meta`
  (fbref player id, club, club country / tier, birth year), feeding the `players`, `team_tournament` and
  `top_scorers` World Cup marts.
- ASA: `stg_asa_windows`, `stg_asa_teams`, `stg_asa_players`, `stg_asa_games`, `stg_asa_player_xgoals`,
  `stg_asa_player_goals_added`, `stg_asa_goalkeeper_goals_added`, `stg_asa_team_xgoals` (with `window`), and
  `stg_asa_player_games` (season, one row per player per game).

## Known data issues
- **fbref coverage by World Cup season** (share of rows with a value, `players` mart):
  shots: none for 2010 and 2014; assists: none for 2010 (blank on fbref); fouls, crosses, interceptions,
  tackles won, offsides: none for 2010 and 2014; penalties won / conceded: 2018 only; club at tournament
  time: 2022 and 2026 only. No xG for any World Cup on fbref.
- **fbref club can be stale.** Example: Juan Manuel Sanabria joined Real Salt Lake in Feb 2026, but fbref lists
  Atlético San Luis. Loans show the parent club (Stephen Eustáquio: Porto, on loan at LAFC Feb–Jun 2026).
  Such links rely on name + nation + birth year and were hand-reviewed.
- **ASA row cap.** The `itscalledsoccer` client only pages on exactly 1,000 rows. `players/xgoals` split by
  game caps at 10,000 rows, which the client would silently truncate (10,000 of 15,744 MLS rows), so the
  loader pages with `offset`. A test checks that per-game minutes add up to season minutes.
- **MLS NEXT Pro per-game minutes** are 4,241 short of the season aggregate (909,383 vs 913,624, 0.5%),
  concentrated in Minnesota United FC 2 and Sporting Kansas City II; `games` for those players can be low.
  MLS matches exactly.
- **g+ above average is not additive across windows** (it is relative to each window's average). Use
  `goals_added_raw` if you need pre + post = season.
- **Small samples.** Half-season windows: see `meets_min_minutes` and always show minutes. AM peer pools are
  the smallest (see `peer_count`).
