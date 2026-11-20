# Dashboard build spec: "World Cup → MLS: did the 2026 World Cup help or hurt MLS players?"

A step-by-step build for **Tableau Public Desktop** (free; it reads text files and Excel, not DuckDB).
Three dashboards: **A. World Cup effect**, **B. Player profile**, **C. Club view**.

- Data: `data/exports/*.csv` (or the identical `*.xlsx`), produced by `python -m etl.mls_model --export`.
  Column meanings: [data_dictionary.md](data_dictionary.md). Numbers to tie out against:
  [findings.md](findings.md).
- Honesty rules that the build must keep: show **players (n) and minutes** next to every comparison; captions
  say **"associated with"**, never "caused" or "effect of"; never filter out low-minute rows silently (use the
  minimum-minutes control so the reader can see what is excluded).

---

## 0. Data sources, relationships and shared setup

### 0.1 Connect
1. *Connect → To a File → Text file* → `data/exports/mls_player_change.csv`
   (Excel alternative: *Microsoft Excel* → `mls_player_change.xlsx`; sheet names equal the file names).
2. Drag the other two player tables onto the canvas next to it, creating **relationships** (not joins):

| Data source name | Logical tables | Relationship (both clauses) | Cardinality |
|---|---|---|---|
| **MLS players** | `mls_player_change` (anchor, 1 row per player) | n/a | n/a |
| | `mls_player_window` (1 row per player per window) | `league = league` AND `asa_player_id = asa_player_id` | change 1 : many window |
| | `mls_player_percentiles` (1 row per player per window per KPI) | `league = league` AND `asa_player_id = asa_player_id` | change 1 : many percentiles |
| **MLS clubs** | `mls_team_window` (1 row per club per window) | n/a (single table) | n/a |

Open *Performance Options* on each relationship and set the cardinality shown, with *Referential integrity:
All records match* for window and percentiles (every player there exists in `mls_player_change`).

3. **Data source filter** (both sources): `league` = `mls`. MLS NEXT Pro rows are in the files, but no
   World Cup player was matched there, so the comparison would be empty.
4. **Data types to check after connecting:** booleans `world_cup_player`, `meets_min_minutes*` → *Boolean*
   (CSV values are `true`/`false`; if Tableau reads them as strings, change the type in the data pane);
   dates `window_start`, `window_end`, `first_game_date`, `wc_nation_last_match`, `first_post_break_game` → *Date*;
   `asa_player_id`, `asa_team_id`, `fbref_player_id` → *String* (never numbers).
5. **Default properties** (right-click a field → *Default Properties*):
   `*_p90`, `xg*`, `xa*`, `goals_added*`, `delta_*` → Number (decimal), 3 places; `xgd_per_game*`,
   `xg_for_per_game`, `xg_against_per_game` → 2 places, show `+` for positives (custom format `+0.00;-0.00;0.00`);
   minutes → Number (custom) `#,##0`; `percentile` → 0 places.

### 0.2 Parameters (workbook-level, used by all dashboards)

| Name | Data type | Allowable values | Default | Shown as |
|---|---|---|---|---|
| `p_KPI` | String | List: `xG`, `xA`, `Goals added (g+)`, `Shots`, `Goals`, `Assists` | `xG` | Single-value dropdown |
| `p_MinMinutes` | Integer | Range 0 to 1,800, step 90 | 450 | Slider (label "Min. minutes in each window") |
| `p_Player` | String | List → *When workbook opens* → field `mls_player_change.player_label` | `Lionel Messi (Inter Miami CF)` | Type-in / dropdown |
| `p_ClubX` | String | List: `World Cup players sent`, `World Cup minutes played` | `World Cup players sent` | Single-value dropdown |

### 0.3 Calculated fields
Create each on the logical table named in brackets. Tableau row-level calcs may reference only one logical
table, so the names are table-specific.

**On `mls_player_window`**
```
// KPI total (window)
CASE [p_KPI]
  WHEN "xG" THEN [xg]
  WHEN "xA" THEN [xa]
  WHEN "Goals added (g+)" THEN [goals_added]
  WHEN "Shots" THEN [shots]
  WHEN "Goals" THEN [goals]
  WHEN "Assists" THEN [assists]
END
```
```
// KPI per 90 (pooled)   -- minutes-weighted: total of the KPI divided by total minutes, x 90
SUM([KPI total (window)]) * 90 / SUM([minutes])
```
```
// KPI per 90 (player)   -- the player's own per-90 in this window
CASE [p_KPI]
  WHEN "xG" THEN [xg_p90]
  WHEN "xA" THEN [xa_p90]
  WHEN "Goals added (g+)" THEN [goals_added_p90]
  WHEN "Shots" THEN [shots_p90]
  WHEN "Goals" THEN [goals_p90]
  WHEN "Assists" THEN [assists_p90]
END
```
```
// Window label
IF [window] = "pre" THEN "Pre-break" ELSE "Post-break" END
```
```
// Group (window)
IF [world_cup_player] THEN "Played at the 2026 World Cup" ELSE "Did not play at the World Cup" END
```
```
// Selected player (window)
[player_label] = [p_Player]
```

**On `mls_player_change`**
```
// Eligible: both windows >= min minutes
[pre_minutes] >= [p_MinMinutes] AND [post_minutes] >= [p_MinMinutes]
```
```
// KPI change per 90   -- post minus pre for the selected KPI
CASE [p_KPI]
  WHEN "xG" THEN [delta_xg_p90]
  WHEN "xA" THEN [delta_xa_p90]
  WHEN "Goals added (g+)" THEN [delta_goals_added_p90]
  WHEN "Shots" THEN [delta_shots_p90]
  WHEN "Goals" THEN [delta_goals_p90]
  WHEN "Assists" THEN [delta_assists_p90]
END
```
```
// Group (change)
IF [world_cup_player] THEN "Played at the 2026 World Cup" ELSE "Did not play at the World Cup" END
```
```
// Selected player (change)
[player_label] = [p_Player]
```
```
// World Cup line
IF [world_cup_player] THEN
  [world_cup_nation] + " · " + STR([wc_minutes]) + " min in " + STR([wc_matches]) + " matches · "
  + STR([wc_goals]) + " G, " + STR([wc_assists]) + " A · " + [wc_round_reached]
  + IIF(ISNULL([days_from_wc_exit_to_first_mls_game]), " · no MLS minutes after the break",
        " · back in MLS " + STR([days_from_wc_exit_to_first_mls_game]) + " days after "
        + [world_cup_nation] + "'s last match")
ELSE "Did not play at the 2026 World Cup"
END
```
```
// Club move
IF ISNULL([club_pre]) THEN "Joined " + [club_post] + " after the break"
ELSEIF ISNULL([club_post]) THEN [club_pre] + " (no minutes after the break)"
ELSEIF [club_pre] <> [club_post] THEN [club_pre] + " → " + [club_post]
ELSE [club_post] END
```

**On `mls_player_percentiles`**
```
// KPI label
CASE [kpi]
  WHEN "xg" THEN "xG" WHEN "xa" THEN "xA" WHEN "goals_added" THEN "Goals added (g+)"
  WHEN "shots" THEN "Shots" WHEN "goals" THEN "Goals" WHEN "assists" THEN "Assists"
END
```
```
// Window label (pct)
IF [window] = "pre" THEN "Pre-break" ELSE "Post-break" END
```
```
// Selected player (pct)
[player_label] = [p_Player]
```

**On `mls_team_window` (MLS clubs)**
```
// Club X
CASE [p_ClubX]
  WHEN "World Cup players sent" THEN [wc_players_sent]
  WHEN "World Cup minutes played" THEN [wc_minutes_sent]
END
```
```
// Sent any World Cup player
IF [wc_players_sent] > 0 THEN "Sent ≥ 1 World Cup player" ELSE "Sent none" END
```
```
// Window label (club)
IF [window] = "pre" THEN "Pre-break" ELSE "Post-break" END
```

### 0.4 Colours (Okabe–Ito, colour-blind safe)

| Use | Value | Hex |
|---|---|---|
| Window | Pre-break | `#999999` (grey) |
| Window | Post-break | `#E69F00` (orange) |
| Group / club | Played at the World Cup · Sent ≥ 1 | `#D55E00` (vermillion) |
| Group / club | Did not play · Sent none | `#0072B2` (blue) |
| Dumbbell connector line | n/a | `#7F7F7F`, 2 px |
| Reference lines (0, 50th percentile) | n/a | `#4D4D4D`, dashed |
| Text / background | n/a | `#222222` on `#FFFFFF` |

Never encode meaning with red vs green. Every colour is backed by a label or legend.

### 0.5 Fonts and formatting
Workbook → Format → Font: Tableau Book 10 pt (titles Tableau Semibold 14 pt). Remove row and column
dividers on text tables; light grey gridlines `#E6E6E6` only on value axes.

---

## A. World Cup effect

**Question:** Did MLS players who went to the World Cup change more (per 90) after the break than those who didn't?

### Sheet A1: `A1 Dumbbell` (data source: MLS players)
- **Rows:** `Group (window)` (sort manual: World Cup group first).
- **Columns:** `KPI per 90 (pooled)` twice → right-click the second pill → *Dual Axis* → *Synchronize Axis*;
  hide the top axis header.
- **Marks, first axis:** Circle, size step 7; **Color** `Window label` (Pre-break `#999999`,
  Post-break `#E69F00`); **Label** `KPI per 90 (pooled)` (3 decimals, label *Min/Max* off, *All* on).
- **Marks, second axis:** Line; **Path** `MIN(window_order)`; Color single `#7F7F7F`; drop `Window label`
  from this card's Color; Detail `Window label`.
- **Filters:**
  - `Eligible: both windows >= min minutes` = True (from `mls_player_change`, applied through the relationship).
  - `position_group` (multiple values dropdown, "All" default).
  - `position` (multiple values dropdown).
  - `club_latest` (multiple values dropdown, shows the player's latest club so a traded player stays whole).
  - Apply all three to *All using this data source* (A1, A2, A3).
- **Axis title:** `=[p_KPI] per 90 (pooled)`, edit the title to show the parameter value: `<Parameters.p_KPI> per 90`.
- **Tooltip:**
  ```
  <Group (window)> · <Window label>
  <AGG(KPI per 90 (pooled))> <Parameters.p_KPI> per 90 (pooled across players)
  <CNTD(asa_player_id)> players · <SUM(minutes)> minutes
  Players with ≥ <Parameters.p_MinMinutes> minutes in both windows
  ```

### Sheet A2: `A2 Sample size` (data source: MLS players)
- **Rows:** `Group (window)`; **Columns:** `Window label` (sort by `window_order`).
- **Text:** `CNTD(asa_player_id)` (label "players") and `SUM(minutes)` (label "minutes"), on two lines:
  `<CNTD(asa_player_id)> players` / `<SUM(minutes)> min`.
- Same filters as A1 (applied via *All using this data source*).
- Tie-out: with default settings (450 min, no position or club filter) this must show the
  "Comparison population" numbers in [findings.md](findings.md#sample).

### Sheet A3: `A3 Player changes` (data source: MLS players, table `mls_player_change`)
- **Columns:** `KPI change per 90` (continuous); **Rows:** `Group (change)`.
- **Marks:** Circle, opacity 45%, size small; **Color** `Group (change)` (vermillion / blue);
  **Detail** `player_label`.
- **Analytics:** Box Plot (per pane, whiskers to 1.5 IQR, hide underlying marks: off).
  Reference line: constant 0, dashed `#4D4D4D`, label "no change".
- **Filters:** `Eligible: both windows >= min minutes` = True; `position_group`, `position`; for club use
  `club` (this table's latest club, same values as `club_latest`).
- **Sort:** n/a (continuous axis).
- **Tooltip:**
  ```
  <player_label> · <position>
  <Parameters.p_KPI> per 90: pre <AGG of pre field> → post <AGG of post field>  (change <KPI change per 90>)
  Minutes: <pre_minutes> pre · <post_minutes> post
  <World Cup line>
  Click to open this player's profile
  ```
  (For the pre and post values in the tooltip, add two more CASE calcs on `mls_player_change` named
  `KPI pre per 90` and `KPI post per 90`, using `pre_*_p90` / `post_*_p90` the same way as `KPI change per 90`.)

### Dashboard A layout: `A · World Cup effect`
- **Size:** Fixed, 1200 × 820 px (Device layout: add a *Phone* layout that stacks A1, A2, A3 vertically).
- **Tiled layout:**
  - Title bar (top, full width, 64 px): "World Cup → MLS: did the 2026 World Cup help or hurt MLS players?"
  - Caption (below the title, 28 px, italic, #444): *How to read this* (see below).
  - Left column, 880 px wide: A1 (380 px tall) above A3 (300 px tall).
  - Right column, 320 px wide: parameter controls `p_KPI` and `p_MinMinutes`, the filters `position_group`,
    `position` and `club_latest`, then A2, then a text box with the method note.
- **Method note (text box):** "Windows: MLS 2026 regular season before vs after the World Cup break
  (dates from the ASA game schedule; tournament dates from fbref). Pooled per-90 = total ÷ minutes × 90.
  Half-season samples: treat gaps as associations, not effects. Sources: American Soccer Analysis, fbref."
- **Actions:**
  1. *Parameter action* `Pick player`: source A3, run on **Select**, target parameter `p_Player`, source field
     `player_label`, on clearing: keep current value.
  2. *Go to Sheet action* `Open profile`: source A3, run on **Menu**, target dashboard B.
  3. *Highlight action*: A1 ↔ A3 on `Group` fields (select a group in A1 to highlight its players in A3).
- **How to read this (caption):** *Each dot is a group's pooled per-90 rate before (grey) and after (orange)
  the World Cup break; n and minutes are on the right. Gaps between groups are associations from half-season
  samples, not effects.*

---

## B. Player profile

### Sheet B1: `B1 Header` (MLS players, table `mls_player_change`)
- **Filter:** `Selected player (change)` = True.
- **Text (one mark, left aligned):**
  ```
  <player_label>                           (Tableau Semibold 18)
  <position> · <Club move> · age <age_pre>
  <window_coverage>
  ```

### Sheet B2: `B2 World Cup line` (MLS players, table `mls_player_change`)
- **Filter:** `Selected player (change)` = True. **Text:** `World Cup line` (12 pt); light vermillion
  background `#FBE3D6` when `world_cup_player` is True (set via a colour on a dummy `MIN(1)` mark, or keep white).

### Sheet B3: `B3 Percentiles` (MLS players, table `mls_player_percentiles`)
- **Filter:** `Selected player (pct)` = True.
- **Rows:** `KPI label` (sort by field `kpi_order`, ascending, MIN), then `Window label (pct)` (sort by `window_order`).
- **Columns:** `AVG(percentile)`, axis fixed 0 to 100, ticks every 25.
- **Marks:** Bar, size 70%; **Color** `Window label (pct)` (grey / orange); **Label** `AVG(percentile)`
  formatted `0"th"`.
- **Reference line:** constant 50 on Columns, dashed, label "median peer".
- **Tooltip:**
  ```
  <KPI label> · <Window label (pct)>
  <AVG(value_p90)> per 90 → <AVG(percentile)>th percentile
  vs <AVG(peer_count)> MLS <position> with ≥ 450 minutes in this window
  <minutes> minutes played
  ```
- Peers are fixed at the 450-minute pool in the export; `p_MinMinutes` does **not** change B3. Goalkeepers
  have a g+ bar only. A window with no bar means the player had < 450 minutes in it.

### Sheet B4: `B4 KPI cards` (MLS players, table `mls_player_window`)
- **Filter:** `Selected player (window)` = True.
- **Rows:** `Window label` (sorted by `window_order`).
- **Columns:** `Measure Names`, filtered to `minutes`, `games`, `goals_p90`, `assists_p90`, `shots_p90`,
  `xg_p90`, `xa_p90`, `goals_added_p90`; **Text:** `Measure Values`.
- Rename the measure aliases: Minutes, Apps, Goals/90, Assists/90, Shots/90, xG/90, xA/90, g+/90.
- Format: minutes `#,##0`; the per-90s 2 decimals (g+/90 3 decimals).
- **Tooltip:** `<Window label>: <window_start> to <window_end> · <club>`

### Dashboard B layout: `B · Player profile`
- **Size:** Fixed, 1200 × 820 px.
- Top band (150 px): B1 (left, 700 px) + `p_Player` control and B2 (right, 500 px).
- Caption line (28 px): *How to read this*.
- Body: B3 (left, 640 px wide) | B4 (right, 560 px wide, top 260 px) with a text box under it:
  "Per-90 = value × 90 ÷ minutes. Assists = primary assists; xG includes penalties; g+ = goals added
  above average (ASA)."
- **Actions:** *Go to Sheet* button (navigation object) "← Back to the World Cup effect" → dashboard A.
- **How to read this (caption):** *Bars rank this player against MLS players at the same position with
  ≥ 450 minutes in that window (50 = median peer); the table shows the raw per-90s and the minutes behind them.*

---

## C. Club view

### Sheet C1: `C1 Clubs scatter` (MLS clubs)
- **Filter:** `window` = `post` (club-level fields repeat on both window rows; this keeps one mark per club).
- **Columns:** `AVG(Club X)`; **Rows:** `AVG(xgd_per_game_change)`.
- **Marks:** Circle, size step 6; **Color** `Sent any World Cup player` (vermillion / blue);
  **Label** `club_abbreviation` (all marks, 9 pt, avoid overlap on).
- **Reference lines:** Rows constant 0 ("no change"), dashed.
  No trend line: across 30 clubs there is no association (see [findings.md](findings.md), finding 3).
  If you add one, put its R² and p in the caption.
- **Axis titles:** X = `<Parameters.p_ClubX>`, Y = "Change in xG difference per game (post − pre)".
- **Tooltip:**
  ```
  <club>
  World Cup players sent: <wc_players_sent> (<wc_players_sent_names>) · <wc_minutes_sent> World Cup minutes
  Arrived after the World Cup: <wc_players_arrived> <wc_players_arrived_names>
  xG difference per game: change <AVG(xgd_per_game_change)>
  Games: see C2 for pre / post
  ```

### Sheet C2: `C2 Club xGD dumbbell` (MLS clubs)
- **Rows:** `club`, sorted by field `xgd_per_game_change`, descending (aggregation AVG).
- **Columns:** `AVG(xgd_per_game)` dual axis (circle + line, same build as A1); Circle colour `Window label (club)`;
  Line path `MIN(window_order)`.
- **Label (circles):** `AVG(xgd_per_game)` `+0.00`; **Tooltip:**
  ```
  <club> · <Window label (club)> (<games> games)
  xG for <AVG(xg_for_per_game)> · xG against <AVG(xg_against_per_game)> per game
  xG difference <AVG(xgd_per_game)> per game · points per game <AVG(points_per_game)>
  ```

### Sheet C3: `C3 Club players` (MLS players, table `mls_player_change`)
- **Rows:** `player_label`, `Group (change)`; sort by `post_minutes` descending.
- **Columns / Text:** `pre_minutes`, `post_minutes`, `KPI change per 90` (as Measure Values, text table).
- **Filter:** `club` (set by action C1 → C3).
- Shows the players behind a club's numbers, including World Cup players and arrivals.

### Dashboard C layout: `C · Club view`
- **Size:** Fixed, 1200 × 820 px.
- Title (64 px) + caption (28 px).
- Left (620 px): C1 (top, 440 px) with `p_ClubX` and `p_KPI` controls above it; C3 below (260 px).
- Right (580 px): C2 (full height; 30 clubs at ~22 px per row).
- **Actions:**
  1. *Filter action* `Club players`: source C1 and C2, run on **Select**, target C3,
     *Selected fields*: `club` (MLS clubs) → `club` (mls_player_change); clearing shows all values.
  2. *Highlight action*: C1 ↔ C2 on `club`.
- **How to read this (caption):** *Each dot is a club: further right = more World Cup players (or minutes) sent;
  above zero = its xG difference per game improved after the break. 30 clubs; this shows association, not cause.*

---

## D. Publishing checklist (Tableau Public)
1. *Data → Extract* both data sources (Tableau Public saves extracts automatically).
2. Hide unused fields (*Data pane → Hide all unused fields*) to keep the extract small.
3. Dashboard titles and captions as above; add the footer text box on every dashboard:
   "Data: American Soccer Analysis (app.americansocceranalysis.com) · fbref.com (World Cup) · Built by Dev Patel".
4. *File → Save to Tableau Public As…*; then paste the public URL into the README placeholder.
5. Tie-out before publishing: A2 matches the sample in [findings.md](findings.md); A1 matches its pooled
   per-90 table for each KPI; C1 shows 30 clubs.
