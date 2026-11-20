# World Cup Stats MCP ⚽ + World Cup → MLS dashboard

An automated Python **ETL pipeline** that pulls **FIFA World Cup** stats from [fbref.com](https://fbref.com) and
the **2026 MLS season** from [American Soccer Analysis](https://www.americansocceranalysis.com) into a local
**DuckDB** warehouse. It links the two at player level, exports tidy marts for a **Tableau Public** dashboard,
exposes everything through a **Model Context Protocol (MCP) server**, and ships a **Claude-powered CLI
assistant** for plain-English questions.

**Dashboard question: did the 2026 World Cup help or hurt MLS players?** It compares MLS players who played at
the 2026 World Cup with MLS players who didn't, before vs after the league's World Cup break, on per-90 xG, xA,
goals added (g+), shots, goals and assists.

**Live dashboard:** _Tableau Public link coming soon._ <!-- TODO: paste the Tableau Public URL here -->

## What's in the warehouse
- **World Cups 2010, 2014, 2018, 2022, 2026** (fbref): 3,529 player-tournament rows; schedules for 360 matches.
- **MLS 2026 regular season** (ASA): 510 games, 878 players; **MLS NEXT Pro 2026** (optional league): 416
  games, 1,087 players. Each is split into a pre- and post-World Cup window.
- **`player_xref`**: every 2026 World Cup player matched (or explicitly not matched) to an MLS player, with
  `match_method`, `confidence` and hand-review overrides.

Headline result: no clear World Cup effect; every 95% interval includes zero. Full numbers, sample sizes and
intervals are in [docs/findings.md](docs/findings.md), which is generated from the data.

## Architecture

```
 fbref.com: World Cups 2010–2026                 American Soccer Analysis API: MLS + MLS NEXT Pro 2026
   │ soccerdata (headless browser, 7 s/page,        │ itscalledsoccer client + JSON response cache,
   │ disk cache, stops on the first block)          │ ≥ 1 s between requests, offset paging past caps
   ▼                                                ▼
 etl.pipeline → stg_* (upsert by season)         etl.asa → stg_asa_* (season · pre · post windows;
   │ etl.model                                      │        break dates derived from the game calendar)
   ▼                                                │
 players · team_tournament · top_scorers            │
   └──────────────────────┬─────────────────────────┘
                          ▼
   etl.xref → player_xref   (normalized name + club + nation + birth year;
                             review file + reference/player_match_overrides.csv)
                          ▼
   etl.mls_model → mls_player_window · mls_player_change · mls_player_percentiles · mls_team_window
                          │                       │                         │
                          ▼                       ▼                         ▼
   etl.export → data/exports/*.csv|xlsx   MCP server (FastMCP, 9 tools)   analysis.findings → docs/findings.md
                          ▼                       ▼
                   Tableau Public          Claude-API CLI assistant

   tests/: unit tests + warehouse data-quality checks (pytest, run in CI)
```

## Features
- **Resilient fbref scrape**: soccerdata with rate limiting and caching; seasons are **upserted**, so refreshing
  2026 never wipes 2010–2022. The mart SQL adapts to the columns fbref still serves after it removed its
  Opta-sourced stats in January 2026. A circuit breaker switches the run to cache-only the moment fbref
  refuses a page, and `--cache-only` never contacts fbref at all.
- **MLS loader** (`etl/asa.py`): the ASA API via `itscalledsoccer`, with a JSON response cache and polite
  request spacing. Players and teams are fetched per league, and responses are paged past the API's
  10,000-row cap (the client would silently truncate there).
- **Data-driven windows**: the World Cup dates come from the fbref schedule. Each league's break is the gap in
  its game calendar that covers the tournament (MLS 2026: 2026-05-25 → 2026-07-16). MLS NEXT Pro had no
  break, so it is split on the tournament dates.
- **Player matching as a data-quality showcase** (`etl/xref.py`): names are normalized (accents, case,
  punctuation, token order) and backed by club-at-tournament, nation aliases and birth year. Ambiguous or
  conflicting links are never accepted, low-confidence rows go to a review file, and hand decisions are
  versioned in `reference/player_match_overrides.csv`.
- **Marts for Tableau**: player × window, player change, positional percentiles and club × window, exported as
  CSV and Excel. Nothing is filtered on minutes; a `meets_min_minutes` flag is used instead.
- **MCP server**: 9 read-only tools; `run_sql` rejects anything that isn't a `SELECT`.
- **Conversational assistant**: a custom MCP client that wires the tools into Claude's tool-use loop.

## Tech stack
Python 3.13 · [soccerdata](https://github.com/probberechts/soccerdata) ·
[itscalledsoccer](https://github.com/American-Soccer-Analysis/itscalledsoccer) · DuckDB · pandas · pytest ·
[MCP](https://modelcontextprotocol.io) / FastMCP · Anthropic Claude API (Sonnet 4.6) · prompt caching ·
Tableau Public

## Project layout
```
etl/          extract (soccerdata) → load → pipeline (CLI) → model (World Cup marts)
              fbref_meta (fbref ids + club) · asa (MLS loader) · xref (player matching)
              mls_model (MLS marts) · export (CSV / xlsx)
analysis/     findings.py → docs/findings.md (every quoted number)
mcp_server/   FastMCP server exposing the warehouse as MCP tools
assistant/    custom Claude-API CLI that connects to the MCP server
tests/        unit tests + data-quality tests (skip without a warehouse)
reference/    player_match_overrides.csv (hand-review decisions, tracked)
docs/         dashboard_spec.md (Tableau build) · data_dictionary.md · findings.md
config.py     paths, seasons, leagues, thresholds
data/         DuckDB warehouse, caches, review file, exports (gitignored)
```

## Setup
```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install --prefer-binary -r requirements.txt   # --prefer-binary avoids a cryptography source build
cp .env.example .env                               # add your ANTHROPIC_API_KEY (for the assistant only)
```
> The fbref scraper drives a headless Chrome via `soccerdata`, so Google Chrome must be installed.

## Refresh the data
```bash
python -m etl.pipeline                 # World Cups 2010–2026 from fbref (upserts by season)
python -m etl.model                    # players / team_tournament / top_scorers
python -m etl.asa                      # MLS + MLS NEXT Pro 2026 from ASA (cached; --refresh to re-download)
python -m etl.xref                     # World Cup ↔ MLS matching → data/review/player_match_review.csv
python -m etl.mls_model --export       # MLS marts → data/exports/*.csv and *.xlsx
python -m analysis.findings            # regenerate docs/findings.md
python -m pytest                       # unit + data-quality tests
```
Useful variants: `python -m etl.pipeline --seasons 2026` (refresh one season),
`--cache-only` (no fbref requests), `--replace` (rebuild tables from only the given seasons).
`WC_DUCKDB_PATH=/path/to/copy.duckdb` points every step at another warehouse file.

**Reviewing matches:** after `etl.xref`, rows that need a decision are in
`data/review/player_match_review.csv`. Record decisions (`accept` / `reject` / `dismiss`, with a note) in
`reference/player_match_overrides.csv` and re-run `etl.xref` and `etl.mls_model --export`.

## Usage

**Explore the data directly** (no API key needed):
```bash
python -c "import duckdb,config; print(duckdb.connect(str(config.DUCKDB_PATH),read_only=True).sql('SELECT player,team,goals FROM players WHERE season=2026 ORDER BY goals DESC LIMIT 10'))"
```

**Run the MCP server** (stdio transport):
```bash
python -m mcp_server.server
```

**Chat with the assistant** (requires `ANTHROPIC_API_KEY`):
```bash
python -m assistant.cli
```
Then ask things like:
- *"Who were the top 5 scorers at the 2026 World Cup?"*
- *"Did World Cup players' xG per 90 change after the MLS break compared with everyone else?"*
- *"Show Son Heung-min's MLS profile before and after the World Cup."*

## MCP tools
`list_tables` · `list_seasons` · `top_scorers` · `player_stats` · `compare_players` · `team_summary` ·
`world_cup_effect` · `mls_player_profile` · `run_sql` (read-only)

## Tableau dashboard
Build steps: [docs/dashboard_spec.md](docs/dashboard_spec.md). It has three dashboards (World Cup effect,
Player profile, Club view), each with its data source, calculated fields, parameters, colours, tooltips,
layout and actions. Columns: [docs/data_dictionary.md](docs/data_dictionary.md).

## Sources and attribution
- **fbref.com** (Sports Reference): World Cup schedules and player/team stats, used for personal,
  non-commercial, educational purposes through soccerdata's rate limiting and caching.
  fbref removed its Opta-sourced advanced stats in January 2026; the pipeline uses what remains.
- **American Soccer Analysis** (americansocceranalysis.com): MLS and MLS NEXT Pro xG, xA, goals added, team
  xG and games, via the public API and the `itscalledsoccer` client. Please credit ASA when sharing the
  dashboard.

## Data notes
- fbref has **no expected goals (xG)** for the World Cup. Shot counts are unavailable for 2010 and 2014, and
  2010 assists are blank on fbref. Full coverage table: [docs/data_dictionary.md](docs/data_dictionary.md#known-data-issues).
- MLS windows are **half-season samples**. The dashboard shows minutes and player counts everywhere and
  describes differences as *associated with* the World Cup, not caused by it.

## License
MIT
