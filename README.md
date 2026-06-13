# World Cup Stats MCP ⚽

An automated Python **ETL pipeline** that extracts **FIFA World Cup** player and team
statistics from [fbref.com](https://fbref.com) into a local **DuckDB** warehouse, exposes
it through a **Model Context Protocol (MCP) server**, and ships a custom **Claude-powered
CLI assistant** so you can ask questions about the data in plain English.

Covers the **2010, 2014, 2018, and 2022** World Cups — ~2,490 players with full scoring,
shooting, discipline, goalkeeping, and playing-time stats.

## Architecture

```
fbref.com ──(soccerdata: rate-limited, cached, resumable scrape)──▶  ETL pipeline (Python)
                                                                         │  clean / flatten / load
                                                                         ▼
                                                                 DuckDB warehouse
                                                                 (players, team_tournament, …)
                                                                         │
                                                       ┌─────────────────┴──────────────────┐
                                                       ▼                                      ▼
                                              MCP server (FastMCP)                 Custom Claude-API CLI client
                                       7 read-only tools over the data       "Compare Ronaldo & Messi across all 4 WCs"
```

## Features
- **Resilient scraper** — `soccerdata` over fbref with built-in rate-limiting, on-disk caching, and per-table fetching so a run is fully resumable.
- **Analytics warehouse** — staging tables modelled into clean marts: a wide `players` table (one row per player per World Cup) and `team_tournament` aggregates.
- **MCP server** — exposes the warehouse as 7 read-only tools (FastMCP); `run_sql` rejects anything that isn't a `SELECT`.
- **Conversational assistant** — a custom MCP client that wires the tools into Claude's tool-use loop for natural-language Q&A.

## Tech stack
Python 3.13 · [soccerdata](https://github.com/probberechts/soccerdata) · DuckDB ·
[MCP](https://modelcontextprotocol.io) / FastMCP · Anthropic Claude API (Sonnet 4.6) · prompt caching

## Project layout
```
etl/         extract (soccerdata) → load (DuckDB) → pipeline (CLI) → model (marts)
mcp_server/  FastMCP server exposing the warehouse as MCP tools
assistant/   custom Claude-API CLI that connects to the MCP server
config.py    paths, seasons, league, stat types
data/        DuckDB warehouse + scrape cache (gitignored)
```

## Setup
```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install --prefer-binary -r requirements.txt   # --prefer-binary avoids a cryptography source build
cp .env.example .env                               # add your ANTHROPIC_API_KEY (for the assistant only)
```
> The scraper drives a headless Chrome via `soccerdata`, so Google Chrome must be installed.

## Usage

**Build the warehouse** (scrape → load → model):
```bash
python -m etl.pipeline            # all configured World Cups
python -m etl.model               # build the players / team_tournament marts
```

**Explore the data directly** (no API key needed):
```bash
python -c "import duckdb,config; print(duckdb.connect(str(config.DUCKDB_PATH),read_only=True).sql('SELECT player,team,goals FROM players WHERE season=2018 ORDER BY goals DESC LIMIT 10'))"
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
- *"Who were the top 5 scorers in 2014?"*
- *"Compare Cristiano Ronaldo and Messi across all four World Cups."*
- *"Which goalkeeper kept the most clean sheets?"*

## MCP tools
`list_tables` · `list_seasons` · `top_scorers` · `player_stats` · `compare_players` · `team_summary` · `run_sql` (read-only)

## Data notes
- Source: fbref.com (Sports Reference), used for **personal, non-commercial, educational** purposes via soccerdata's rate-limiting and caching.
- The World Cup competition on fbref exposes `standard`, `keeper`, `shooting`, `playing_time`, and `misc` player tables (no passing/defense/possession — those are league-only).
- There is **no expected-goals (xG)** for the World Cup, and shot counts are unavailable for the 2010 and 2014 tournaments.

## License
MIT
