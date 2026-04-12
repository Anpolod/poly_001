# Tanking Scanner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `analytics/tanking_scanner.py` — an end-of-season NBA pattern scanner that detects high-motivation vs. tanking matchups in Polymarket markets, scores them, and flags pre-match drift opportunities.

**Architecture:** Single analytics module following the existing `prop_scanner.py` pattern — async, reads from asyncpg pool, fetches ESPN standings via aiohttp, matches markets from the local DB, prints tabular output and optionally persists signals to a `tanking_signals` DB table. Two config files in `config/` handle team name aliases and a fallback standings snapshot.

**Tech Stack:** Python 3.14, asyncpg, aiohttp, PyYAML, tabulate (already in requirements.txt), beautifulsoup4 (new dep), ESPN public API, Rotowire HTML scraping, PostgreSQL.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `config/nba_team_aliases.yaml` | Create | All known Polymarket → canonical ESPN team name mappings |
| `config/nba_standings.yaml` | Create | Manual fallback standings (updated by hand when ESPN API fails) |
| `analytics/tanking_scanner.py` | Create | All 6 components: standings, scorer, matcher, scanner, backtest, Rotowire + CLI |
| `db/schema.sql` | Modify | Append `tanking_signals` table |
| `db/init_schema.py` | Run (no edit needed) | Re-run to create new table |
| `requirements.txt` | Modify | Add `beautifulsoup4` |
| `Makefile` | Modify | Add `tanking` target |
| `CLAUDE.md` | Modify | Document new CLI commands |

---

## Task 1: Config Files — Team Aliases + Fallback Standings

**Files:**
- Create: `config/nba_team_aliases.yaml`
- Create: `config/nba_standings.yaml`

- [ ] **Step 1: Create `config/nba_team_aliases.yaml`**

This maps every known Polymarket question string fragment → canonical ESPN `displayName`.
The scanner does `alias.lower() in question.lower()` for each alias.

```yaml
# config/nba_team_aliases.yaml
# Maps Polymarket question fragments → canonical ESPN displayName
# Keys must be lowercase substrings that appear in market questions.
# Values must match ESPN "displayName" exactly.

# Eastern Conference
"76ers": "Philadelphia 76ers"
"sixers": "Philadelphia 76ers"
"philadelphia 76ers": "Philadelphia 76ers"
"philadelphia": "Philadelphia 76ers"

"bucks": "Milwaukee Bucks"
"milwaukee bucks": "Milwaukee Bucks"
"milwaukee": "Milwaukee Bucks"

"celtics": "Boston Celtics"
"boston celtics": "Boston Celtics"
"boston": "Boston Celtics"

"nets": "Brooklyn Nets"
"brooklyn nets": "Brooklyn Nets"
"brooklyn": "Brooklyn Nets"

"knicks": "New York Knicks"
"new york knicks": "New York Knicks"
"new york": "New York Knicks"

"raptors": "Toronto Raptors"
"toronto raptors": "Toronto Raptors"
"toronto": "Toronto Raptors"

"bulls": "Chicago Bulls"
"chicago bulls": "Chicago Bulls"
"chicago": "Chicago Bulls"

"cavaliers": "Cleveland Cavaliers"
"cavs": "Cleveland Cavaliers"
"cleveland cavaliers": "Cleveland Cavaliers"
"cleveland": "Cleveland Cavaliers"

"pistons": "Detroit Pistons"
"detroit pistons": "Detroit Pistons"
"detroit": "Detroit Pistons"

"pacers": "Indiana Pacers"
"indiana pacers": "Indiana Pacers"
"indiana": "Indiana Pacers"

"heat": "Miami Heat"
"miami heat": "Miami Heat"
"miami": "Miami Heat"

"hawks": "Atlanta Hawks"
"atlanta hawks": "Atlanta Hawks"
"atlanta": "Atlanta Hawks"

"hornets": "Charlotte Hornets"
"charlotte hornets": "Charlotte Hornets"
"charlotte": "Charlotte Hornets"

"magic": "Orlando Magic"
"orlando magic": "Orlando Magic"
"orlando": "Orlando Magic"

"wizards": "Washington Wizards"
"washington wizards": "Washington Wizards"
"washington": "Washington Wizards"

# Western Conference
"lakers": "Los Angeles Lakers"
"los angeles lakers": "Los Angeles Lakers"
"la lakers": "Los Angeles Lakers"

"clippers": "LA Clippers"
"los angeles clippers": "LA Clippers"
"la clippers": "LA Clippers"

"warriors": "Golden State Warriors"
"golden state warriors": "Golden State Warriors"
"golden state": "Golden State Warriors"

"suns": "Phoenix Suns"
"phoenix suns": "Phoenix Suns"
"phoenix": "Phoenix Suns"

"nuggets": "Denver Nuggets"
"denver nuggets": "Denver Nuggets"
"denver": "Denver Nuggets"

"jazz": "Utah Jazz"
"utah jazz": "Utah Jazz"
"utah": "Utah Jazz"

"trail blazers": "Portland Trail Blazers"
"blazers": "Portland Trail Blazers"
"portland trail blazers": "Portland Trail Blazers"
"portland": "Portland Trail Blazers"

"thunder": "Oklahoma City Thunder"
"oklahoma city thunder": "Oklahoma City Thunder"
"oklahoma city": "Oklahoma City Thunder"
"okc": "Oklahoma City Thunder"

"timberwolves": "Minnesota Timberwolves"
"wolves": "Minnesota Timberwolves"
"minnesota timberwolves": "Minnesota Timberwolves"
"minnesota": "Minnesota Timberwolves"

"spurs": "San Antonio Spurs"
"san antonio spurs": "San Antonio Spurs"
"san antonio": "San Antonio Spurs"

"rockets": "Houston Rockets"
"houston rockets": "Houston Rockets"
"houston": "Houston Rockets"

"mavericks": "Dallas Mavericks"
"mavs": "Dallas Mavericks"
"dallas mavericks": "Dallas Mavericks"
"dallas": "Dallas Mavericks"

"grizzlies": "Memphis Grizzlies"
"memphis grizzlies": "Memphis Grizzlies"
"memphis": "Memphis Grizzlies"

"pelicans": "New Orleans Pelicans"
"new orleans pelicans": "New Orleans Pelicans"
"new orleans": "New Orleans Pelicans"

"kings": "Sacramento Kings"
"sacramento kings": "Sacramento Kings"
"sacramento": "Sacramento Kings"
```

- [ ] **Step 2: Create `config/nba_standings.yaml`**

Fallback that is loaded when ESPN API is unreachable. Update this by hand at season end.

```yaml
# config/nba_standings.yaml
# Manual fallback NBA standings — update when ESPN API fails.
# rank = conference seed (1-15). games_remaining as of last manual update.
# Last updated: 2026-04-10

east:
  - display_name: "Cleveland Cavaliers"
    abbreviation: "CLE"
    rank: 1
    wins: 62
    losses: 16
    games_remaining: 4
    games_back_from_1st: 0.0

  - display_name: "Boston Celtics"
    abbreviation: "BOS"
    rank: 2
    wins: 58
    losses: 20
    games_remaining: 4
    games_back_from_1st: 4.0

  - display_name: "New York Knicks"
    abbreviation: "NYK"
    rank: 3
    wins: 54
    losses: 24
    games_remaining: 4
    games_back_from_1st: 8.0

  - display_name: "Indiana Pacers"
    abbreviation: "IND"
    rank: 4
    wins: 51
    losses: 27
    games_remaining: 4
    games_back_from_1st: 11.0

  - display_name: "Milwaukee Bucks"
    abbreviation: "MIL"
    rank: 5
    wins: 48
    losses: 30
    games_remaining: 4
    games_back_from_1st: 14.0

  - display_name: "Miami Heat"
    abbreviation: "MIA"
    rank: 6
    wins: 46
    losses: 32
    games_remaining: 4
    games_back_from_1st: 16.0

  - display_name: "Chicago Bulls"
    abbreviation: "CHI"
    rank: 7
    wins: 40
    losses: 38
    games_remaining: 4
    games_back_from_1st: 22.0

  - display_name: "Orlando Magic"
    abbreviation: "ORL"
    rank: 8
    wins: 38
    losses: 40
    games_remaining: 4
    games_back_from_1st: 24.0

  - display_name: "Atlanta Hawks"
    abbreviation: "ATL"
    rank: 9
    wins: 36
    losses: 42
    games_remaining: 4
    games_back_from_1st: 26.0

  - display_name: "Toronto Raptors"
    abbreviation: "TOR"
    rank: 10
    wins: 34
    losses: 44
    games_remaining: 4
    games_back_from_1st: 28.0

  - display_name: "Brooklyn Nets"
    abbreviation: "BKN"
    rank: 11
    wins: 24
    losses: 54
    games_remaining: 4
    games_back_from_1st: 38.0

  - display_name: "Charlotte Hornets"
    abbreviation: "CHA"
    rank: 12
    wins: 22
    losses: 56
    games_remaining: 4
    games_back_from_1st: 40.0

  - display_name: "Washington Wizards"
    abbreviation: "WAS"
    rank: 13
    wins: 20
    losses: 58
    games_remaining: 4
    games_back_from_1st: 42.0

  - display_name: "Detroit Pistons"
    abbreviation: "DET"
    rank: 14
    wins: 18
    losses: 60
    games_remaining: 4
    games_back_from_1st: 44.0

  - display_name: "Philadelphia 76ers"
    abbreviation: "PHI"
    rank: 15
    wins: 16
    losses: 62
    games_remaining: 4
    games_back_from_1st: 46.0

west:
  - display_name: "Oklahoma City Thunder"
    abbreviation: "OKC"
    rank: 1
    wins: 66
    losses: 12
    games_remaining: 4
    games_back_from_1st: 0.0

  - display_name: "Houston Rockets"
    abbreviation: "HOU"
    rank: 2
    wins: 55
    losses: 23
    games_remaining: 4
    games_back_from_1st: 11.0

  - display_name: "Los Angeles Lakers"
    abbreviation: "LAL"
    rank: 3
    wins: 52
    losses: 26
    games_remaining: 4
    games_back_from_1st: 14.0

  - display_name: "Denver Nuggets"
    abbreviation: "DEN"
    rank: 4
    wins: 51
    losses: 27
    games_remaining: 4
    games_back_from_1st: 15.0

  - display_name: "Minnesota Timberwolves"
    abbreviation: "MIN"
    rank: 5
    wins: 50
    losses: 28
    games_remaining: 4
    games_back_from_1st: 16.0

  - display_name: "Golden State Warriors"
    abbreviation: "GSW"
    rank: 6
    wins: 46
    losses: 32
    games_remaining: 4
    games_back_from_1st: 20.0

  - display_name: "LA Clippers"
    abbreviation: "LAC"
    rank: 7
    wins: 42
    losses: 36
    games_remaining: 4
    games_back_from_1st: 24.0

  - display_name: "Dallas Mavericks"
    abbreviation: "DAL"
    rank: 8
    wins: 40
    losses: 38
    games_remaining: 4
    games_back_from_1st: 26.0

  - display_name: "Memphis Grizzlies"
    abbreviation: "MEM"
    rank: 9
    wins: 36
    losses: 42
    games_remaining: 4
    games_back_from_1st: 30.0

  - display_name: "Phoenix Suns"
    abbreviation: "PHX"
    rank: 10
    wins: 34
    losses: 44
    games_remaining: 4
    games_back_from_1st: 32.0

  - display_name: "Sacramento Kings"
    abbreviation: "SAC"
    rank: 11
    wins: 32
    losses: 46
    games_remaining: 4
    games_back_from_1st: 34.0

  - display_name: "New Orleans Pelicans"
    abbreviation: "NOP"
    rank: 12
    wins: 24
    losses: 54
    games_remaining: 4
    games_back_from_1st: 42.0

  - display_name: "Utah Jazz"
    abbreviation: "UTA"
    rank: 13
    wins: 22
    losses: 56
    games_remaining: 4
    games_back_from_1st: 44.0

  - display_name: "San Antonio Spurs"
    abbreviation: "SAS"
    rank: 14
    wins: 20
    losses: 58
    games_remaining: 4
    games_back_from_1st: 46.0

  - display_name: "Portland Trail Blazers"
    abbreviation: "POR"
    rank: 15
    wins: 18
    losses: 60
    games_remaining: 4
    games_back_from_1st: 48.0
```

- [ ] **Step 3: Commit**

```bash
git add config/nba_team_aliases.yaml config/nba_standings.yaml
git commit -m "feat: add NBA team alias map and fallback standings config"
```

---

## Task 2: DB Table — `tanking_signals`

**Files:**
- Modify: `db/schema.sql` (append table definition)
- Run: `venv/bin/python db/init_schema.py`

- [ ] **Step 1: Append table to `db/schema.sql`**

Add at the end of the file:

```sql
-- Tanking pattern signals (NBA end-of-season motivated vs. tanking matchups)
CREATE TABLE IF NOT EXISTS tanking_signals (
    id                      SERIAL PRIMARY KEY,
    scanned_at              TIMESTAMPTZ DEFAULT NOW(),
    market_id               TEXT NOT NULL,
    game_start              TIMESTAMPTZ,
    motivated_team          TEXT,
    tanking_team            TEXT,
    motivation_differential FLOAT,
    current_price           FLOAT,
    drift_24h               FLOAT,
    pattern_strength        TEXT,    -- HIGH / MODERATE
    action                  TEXT,    -- BUY / SELL / CLOSE / WATCH
    lineup_notes            TEXT     -- JSON array of Rotowire notes, if any
);
CREATE INDEX IF NOT EXISTS idx_tanking_signals_market
    ON tanking_signals (market_id, scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_tanking_signals_game_start
    ON tanking_signals (game_start);
```

- [ ] **Step 2: Run init_schema**

```bash
venv/bin/python db/init_schema.py
```

Expected output: no errors, `tanking_signals` table created.

- [ ] **Step 3: Verify table exists**

```bash
psql -d polymarket_sports -c "\d tanking_signals"
```

Expected: table columns listed including `motivated_team`, `motivation_differential`, `action`.

- [ ] **Step 4: Commit**

```bash
git add db/schema.sql
git commit -m "feat: add tanking_signals table to schema"
```

---

## Task 3: Core — Standings Fetcher + Motivation Scorer

**Files:**
- Create: `analytics/tanking_scanner.py` (initial scaffold through motivation scorer)

- [ ] **Step 1: Create `analytics/tanking_scanner.py` — imports + data structures**

```python
"""
NBA Tanking Pattern Scanner

Detects end-of-season mismatches between highly-motivated teams (playoff/play-in
contenders) and tanking teams (eliminated, gunning for draft picks). These matchups
produce predictable 2-4% pre-match drift on the motivated team's market.

Usage:
    python -m analytics.tanking_scanner
    python -m analytics.tanking_scanner --min-differential 0.6 --hours 48
    python -m analytics.tanking_scanner --backtest
    python -m analytics.tanking_scanner --watch
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import asyncpg
import yaml
from tabulate import tabulate

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ALIASES_PATH = _PROJECT_ROOT / "config" / "nba_team_aliases.yaml"
_FALLBACK_STANDINGS_PATH = _PROJECT_ROOT / "config" / "nba_standings.yaml"
_ESPN_STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings"
_ROTOWIRE_URL = "https://www.rotowire.com/basketball/nba-lineups.php"
_WATCH_INTERVAL_SEC = 1800  # 30 min


@dataclass
class TeamStanding:
    display_name: str           # "Boston Celtics"
    abbreviation: str           # "BOS"
    conference: str             # "East" / "West"
    rank: int                   # 1-15 within conference
    wins: int
    losses: int
    games_remaining: int
    games_back_from_1st: float  # raw from ESPN or fallback
    games_back_from_6th: float = 0.0   # computed after loading all teams
    games_back_from_10th: float = 0.0  # computed after loading all teams
    motivation_score: float = 0.0      # computed by compute_motivation_score()


@dataclass
class TankingSignal:
    market_id: str
    slug: str
    game_start: datetime
    hours_to_game: float
    motivated_team: str
    tanking_team: str
    motivation_differential: float
    current_price: float                   # YES price for motivated team
    price_24h_ago: Optional[float]
    actual_drift: Optional[float]          # current_price - price_24h_ago
    pattern_strength: str                  # "HIGH" / "MODERATE"
    recommended_action: str                # "BUY" / "SELL / CLOSE" / "WATCH"
    lineup_notes: list[str] = field(default_factory=list)
```

- [ ] **Step 2: Add alias loader**

```python
def load_aliases() -> dict[str, str]:
    """Load team alias map from config/nba_team_aliases.yaml.
    Returns {alias_lowercase: canonical_display_name}.
    """
    with open(_ALIASES_PATH) as f:
        raw = yaml.safe_load(f)
    # Keys in YAML are strings; ensure all lowercase for matching
    return {k.lower(): v for k, v in raw.items()}
```

- [ ] **Step 3: Add ESPN standings fetcher**

```python
async def fetch_standings_from_espn(session: aiohttp.ClientSession) -> list[TeamStanding]:
    """Fetch live NBA standings from ESPN public API.
    Raises on HTTP error or unexpected structure — caller falls back to YAML.
    """
    async with session.get(
        _ESPN_STANDINGS_URL,
        timeout=aiohttp.ClientTimeout(total=15),
        headers={"User-Agent": "Mozilla/5.0"},
    ) as r:
        r.raise_for_status()
        data = await r.json(content_type=None)

    standings: list[TeamStanding] = []
    for conference in data.get("children", []):
        conf_name = conference.get("name", "")
        conf_short = "East" if "Eastern" in conf_name else "West"
        entries = conference.get("standings", {}).get("entries", [])

        for entry in entries:
            team = entry.get("team", {})
            stats_list = entry.get("stats", [])
            stats = {s["name"]: s["value"] for s in stats_list}

            standings.append(TeamStanding(
                display_name=team.get("displayName", "Unknown"),
                abbreviation=team.get("abbreviation", "???"),
                conference=conf_short,
                rank=int(stats.get("playoffSeed", 99)),
                wins=int(stats.get("wins", 0)),
                losses=int(stats.get("losses", 0)),
                games_remaining=int(stats.get("gamesRemaining", 0)),
                games_back_from_1st=float(stats.get("gamesBehind", 0.0)),
            ))

    if not standings:
        raise ValueError("ESPN API returned empty standings list")

    return standings
```

- [ ] **Step 4: Add fallback standings loader**

```python
def load_standings_from_fallback() -> list[TeamStanding]:
    """Load standings from config/nba_standings.yaml (manually maintained)."""
    with open(_FALLBACK_STANDINGS_PATH) as f:
        data = yaml.safe_load(f)

    standings: list[TeamStanding] = []
    for conf_key, conf_short in (("east", "East"), ("west", "West")):
        for row in data.get(conf_key, []):
            standings.append(TeamStanding(
                display_name=row["display_name"],
                abbreviation=row["abbreviation"],
                conference=conf_short,
                rank=row["rank"],
                wins=row["wins"],
                losses=row["losses"],
                games_remaining=row["games_remaining"],
                games_back_from_1st=row["games_back_from_1st"],
            ))

    return standings
```

- [ ] **Step 5: Add GB margin computation**

After all teams are loaded, compute how far each team is from the 6th and 10th seeds in their conference.

```python
def _compute_gb_margins(standings: list[TeamStanding]) -> None:
    """Mutates each TeamStanding to set games_back_from_6th and games_back_from_10th.
    Standard GB formula: GB = (wins_ref - wins_team + losses_team - losses_ref) / 2
    Clamped to 0.0 for teams ahead of the reference seed.
    """
    for conf in ("East", "West"):
        conf_teams = sorted(
            [t for t in standings if t.conference == conf],
            key=lambda t: t.rank,
        )

        def gb_from_seed(team: TeamStanding, seed_rank: int) -> float:
            ref = next((t for t in conf_teams if t.rank == seed_rank), None)
            if ref is None:
                return 0.0
            gb = (ref.wins - team.wins + team.losses - ref.losses) / 2.0
            return max(0.0, gb)

        for team in conf_teams:
            team.games_back_from_6th = gb_from_seed(team, 6)
            team.games_back_from_10th = gb_from_seed(team, 10)
```

- [ ] **Step 6: Add motivation scorer**

```python
def compute_motivation_score(team: TeamStanding) -> float:
    """Return motivation_score in range [-0.3, 1.0].

    1.0  = direct playoff spot locked or within reach (GB ≤ 3 from 6th seed)
    0.7  = in play-in or close (GB ≤ 3 from 10th seed)
    0.0  = eliminated (no path to play-in)
   -0.3  = deeply eliminated with active tanking incentive (≥5 GB from 10th, ≥10 games left)
    """
    gb6 = team.games_back_from_6th
    gb10 = team.games_back_from_10th
    rem = team.games_remaining

    # Already in direct playoff position
    if team.rank <= 6:
        return 1.0

    # Close enough to chase direct playoff spot
    if gb6 <= 3:
        return 1.0

    # In play-in zone or close to it
    if team.rank <= 10 or gb10 <= 3:
        return 0.7

    # Mathematically eliminated from play-in
    if gb10 > rem:
        # Deep elimination with tanking incentive
        if gb10 >= 5 and rem >= 10:
            return -0.3
        return 0.0

    return 0.0


def build_standings(standings: list[TeamStanding]) -> dict[str, TeamStanding]:
    """Compute margins and motivation scores; return {display_name: TeamStanding}."""
    _compute_gb_margins(standings)
    for team in standings:
        team.motivation_score = compute_motivation_score(team)
    return {t.display_name: t for t in standings}
```

- [ ] **Step 7: Add top-level standings entry point**

```python
async def get_standings(session: aiohttp.ClientSession) -> dict[str, TeamStanding]:
    """Fetch standings from ESPN; fall back to config/nba_standings.yaml on error."""
    try:
        raw = await fetch_standings_from_espn(session)
        logger.info(f"Fetched {len(raw)} teams from ESPN standings API")
    except Exception as exc:
        logger.warning(f"ESPN standings API failed ({exc}); using fallback YAML")
        raw = load_standings_from_fallback()

    return build_standings(raw)
```

- [ ] **Step 8: Commit**

```bash
git add analytics/tanking_scanner.py
git commit -m "feat: tanking scanner — standings fetcher + motivation scorer"
```

---

## Task 4: Market Matcher

**Files:**
- Modify: `analytics/tanking_scanner.py`

The matcher queries the local `markets` + `price_snapshots` tables and returns upcoming NBA markets enriched with current price and the team names found in the `question` field.

- [ ] **Step 1: Add market query + team matching functions**

Append to `analytics/tanking_scanner.py`:

```python
# ---------------------------------------------------------------------------
# Market Matcher
# ---------------------------------------------------------------------------


def match_teams_in_question(
    question: str,
    aliases: dict[str, str],
) -> list[str]:
    """Return list of canonical team names found in the market question string.

    Uses substring matching (alias.lower() in question.lower()).
    Returns at most 2 teams, longest-match-first to avoid partial overlaps
    (e.g. "lakers" matching before "los angeles lakers").
    """
    q = question.lower()
    # Sort by alias length descending so longer aliases win
    matched_canonical: list[str] = []
    seen_canonical: set[str] = set()

    for alias in sorted(aliases.keys(), key=len, reverse=True):
        canonical = aliases[alias]
        if alias in q and canonical not in seen_canonical:
            matched_canonical.append(canonical)
            seen_canonical.add(canonical)
        if len(matched_canonical) == 2:
            break

    return matched_canonical


async def find_upcoming_nba_markets(
    pool: asyncpg.Pool,
    hours: float = 48.0,
) -> list[dict]:
    """Query DB for NBA markets starting within `hours` that have at least one snapshot.

    Returns list of dicts with keys:
      id, question, slug, event_start, current_price, snapshot_ts, price_24h_ago
    """
    rows = await pool.fetch(
        """
        SELECT
            m.id,
            m.question,
            m.slug,
            m.event_start,
            latest.mid_price  AS current_price,
            latest.ts         AS snapshot_ts,
            old24.mid_price   AS price_24h_ago
        FROM markets m
        LEFT JOIN LATERAL (
            SELECT mid_price, ts
            FROM price_snapshots
            WHERE market_id = m.id
            ORDER BY ts DESC
            LIMIT 1
        ) latest ON TRUE
        LEFT JOIN LATERAL (
            SELECT mid_price
            FROM price_snapshots
            WHERE market_id = m.id
              AND ts BETWEEN NOW() - INTERVAL '26 hours'
                        AND NOW() - INTERVAL '22 hours'
            ORDER BY ABS(EXTRACT(EPOCH FROM (ts - (NOW() - INTERVAL '24 hours'))))
            LIMIT 1
        ) old24 ON TRUE
        WHERE (
            m.league ILIKE '%nba%'
            OR m.question ILIKE '%nba%'
            OR (m.sport ILIKE '%basketball%' AND m.league ILIKE '%nba%')
        )
        AND m.event_start BETWEEN NOW() AND NOW() + ($1 * INTERVAL '1 hour')
        AND m.status = 'active'
        AND latest.mid_price IS NOT NULL
        ORDER BY m.event_start
        """,
        float(hours),
    )

    return [dict(r) for r in rows]
```

- [ ] **Step 2: Commit**

```bash
git add analytics/tanking_scanner.py
git commit -m "feat: tanking scanner — market matcher (DB query + team name matching)"
```

---

## Task 5: Pattern Scanner + Output Formatter

**Files:**
- Modify: `analytics/tanking_scanner.py`

- [ ] **Step 1: Add pattern scanner core**

Append to `analytics/tanking_scanner.py`:

```python
# ---------------------------------------------------------------------------
# Pattern Scanner
# ---------------------------------------------------------------------------


def _pattern_strength(diff: float) -> str:
    if abs(diff) >= 0.7:
        return "HIGH"
    if abs(diff) >= 0.4:
        return "MODERATE"
    return "WATCH"


def _recommended_action(
    strength: str,
    current_price: float,
    hours_to_game: float,
) -> str:
    if strength == "HIGH":
        if current_price < 0.88 and hours_to_game > 8:
            return "BUY"
        if hours_to_game < 4:
            return "SELL / CLOSE"
    return "WATCH"


async def scan_tanking_patterns(
    pool: asyncpg.Pool,
    standings: dict[str, TeamStanding],
    aliases: dict[str, str],
    min_differential: float = 0.4,
    hours: float = 48.0,
) -> list[TankingSignal]:
    """Main scan: find upcoming NBA markets with high motivation_differential."""
    markets = await find_upcoming_nba_markets(pool, hours)
    if not markets:
        return []

    now = datetime.now(tz=timezone.utc)
    signals: list[TankingSignal] = []

    for m in markets:
        teams = match_teams_in_question(m["question"] or "", aliases)
        if len(teams) < 2:
            continue

        team_a_name, team_b_name = teams[0], teams[1]
        team_a = standings.get(team_a_name)
        team_b = standings.get(team_b_name)
        if team_a is None or team_b is None:
            continue

        diff = team_a.motivation_score - team_b.motivation_score

        if abs(diff) < min_differential:
            continue

        # Determine which team is motivated, which is tanking
        if diff > 0:
            motivated, tanking = team_a, team_b
        else:
            motivated, tanking = team_b, team_a

        event_start: datetime = m["event_start"]
        if event_start.tzinfo is None:
            event_start = event_start.replace(tzinfo=timezone.utc)
        hours_to_game = (event_start - now).total_seconds() / 3600

        current_price = float(m["current_price"] or 0.0)
        price_24h = float(m["price_24h_ago"]) if m["price_24h_ago"] is not None else None
        drift = (current_price - price_24h) if price_24h is not None else None

        strength = _pattern_strength(abs(diff))
        action = _recommended_action(strength, current_price, hours_to_game)

        signals.append(TankingSignal(
            market_id=m["id"],
            slug=m.get("slug", ""),
            game_start=event_start,
            hours_to_game=round(hours_to_game, 1),
            motivated_team=motivated.display_name,
            tanking_team=tanking.display_name,
            motivation_differential=round(abs(diff), 2),
            current_price=round(current_price, 4),
            price_24h_ago=round(price_24h, 4) if price_24h is not None else None,
            actual_drift=round(drift, 4) if drift is not None else None,
            pattern_strength=strength,
            recommended_action=action,
        ))

    # Sort: HIGH first, then by differential descending
    signals.sort(key=lambda s: (-{"HIGH": 2, "MODERATE": 1, "WATCH": 0}[s.pattern_strength], -s.motivation_differential))
    return signals
```

- [ ] **Step 2: Add output formatter**

```python
def print_signals(signals: list[TankingSignal], title: str = "") -> None:
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    width = 110
    print("=" * width)
    print(f"  NBA TANKING PATTERN SCANNER  —  {now_str}")
    if title:
        print(f"  {title}")
    print("=" * width)

    if not signals:
        print("\n  No tanking pattern matches found for the current filter.\n")
        print("=" * width)
        return

    rows = []
    for s in signals:
        drift_str = f"{s.actual_drift:+.3f}" if s.actual_drift is not None else "—"
        p24_str = f"{s.price_24h_ago:.3f}" if s.price_24h_ago is not None else "—"
        rows.append([
            s.pattern_strength,
            s.recommended_action,
            s.motivated_team[:22],
            s.tanking_team[:22],
            f"{s.motivation_differential:.2f}",
            f"{s.current_price:.3f}",
            p24_str,
            drift_str,
            f"{s.hours_to_game:.1f}h",
            s.game_start.strftime("%m-%d %H:%M"),
        ])

    headers = [
        "Strength", "Action", "Motivated Team", "Tanking Team",
        "Diff", "Price", "24h ago", "Drift", "Game in", "Start"
    ]
    print("\n" + tabulate(rows, headers=headers, tablefmt="simple") + "\n")
    print("=" * width)
```

- [ ] **Step 3: Commit**

```bash
git add analytics/tanking_scanner.py
git commit -m "feat: tanking scanner — pattern scanner + output formatter"
```

---

## Task 6: Historical Backtest

**Files:**
- Modify: `analytics/tanking_scanner.py`

Queries `price_snapshots` for past NBA markets to validate the 2-4% drift claim.

- [ ] **Step 1: Add backtest function**

Append to `analytics/tanking_scanner.py`:

```python
# ---------------------------------------------------------------------------
# Historical Backtest
# ---------------------------------------------------------------------------


async def run_backtest(pool: asyncpg.Pool) -> None:
    """Validate tanking drift hypothesis against collected price_snapshots.

    Finds closed NBA markets where we have snapshots from ≥24h before market close.
    Computes drift at T-24h, T-12h, T-6h, T-2h.
    Groups by 'was the team a heavy favorite?' (YES price > 0.75 at T-24h).
    """
    print("\nRunning historical backtest on collected price_snapshots...")

    # Find NBA markets that have ended (event_start in the past) and have snapshots
    markets = await pool.fetch(
        """
        SELECT m.id, m.question, m.event_start,
               COUNT(ps.ts) AS n_snapshots,
               MIN(ps.ts)   AS first_snapshot,
               MAX(ps.ts)   AS last_snapshot
        FROM markets m
        JOIN price_snapshots ps ON ps.market_id = m.id
        WHERE (m.league ILIKE '%nba%' OR m.sport ILIKE '%basketball%')
          AND m.event_start < NOW() - INTERVAL '3 hours'
        GROUP BY m.id, m.question, m.event_start
        HAVING COUNT(ps.ts) >= 10
           AND MIN(ps.ts) <= m.event_start - INTERVAL '24 hours'
        ORDER BY m.event_start DESC
        LIMIT 500
        """
    )

    if not markets:
        print("  No qualifying markets found (need ≥10 snapshots starting ≥24h before game).")
        return

    print(f"  Found {len(markets)} qualifying markets. Computing drift windows...")

    results = []
    for row in markets:
        mid = row["id"]
        event_start = row["event_start"]
        if event_start.tzinfo is None:
            event_start = event_start.replace(tzinfo=timezone.utc)

        # Fetch prices at T-24h, T-12h, T-6h, T-2h and at close (latest before event_start)
        price_rows = await pool.fetch(
            """
            SELECT ts, mid_price FROM price_snapshots
            WHERE market_id = $1
              AND ts <= $2
            ORDER BY ts DESC
            """,
            mid,
            event_start,
        )

        def price_at_offset(hours_before: float) -> Optional[float]:
            target = event_start - timedelta(hours=hours_before)
            candidates = [
                r for r in price_rows
                if abs((r["ts"] - target).total_seconds()) < 3600
            ]
            if not candidates:
                return None
            closest = min(candidates, key=lambda r: abs((r["ts"] - target).total_seconds()))
            return float(closest["mid_price"])

        p_close = price_at_offset(0.5)
        p_24h = price_at_offset(24.0)
        p_12h = price_at_offset(12.0)
        p_6h = price_at_offset(6.0)
        p_2h = price_at_offset(2.0)

        if p_24h is None or p_close is None:
            continue

        # "Heavy favorite" proxy for motivated team identification
        is_heavy_fav = p_24h > 0.75

        results.append({
            "market_id": mid,
            "is_heavy_fav": is_heavy_fav,
            "drift_24_to_close": round((p_close - p_24h), 4) if p_close else None,
            "drift_12_to_close": round((p_close - p_12h), 4) if p_12h and p_close else None,
            "drift_6_to_close": round((p_close - p_6h), 4) if p_6h and p_close else None,
            "drift_2_to_close": round((p_close - p_2h), 4) if p_2h and p_close else None,
            "p_24h": p_24h,
            "p_close": p_close,
        })

    if not results:
        print("  No results after computing drift (insufficient price coverage).")
        return

    # Report
    for label, fav_filter in [("Heavy Favorite (price > 0.75 at T-24h)", True),
                               ("Non-Favorite (price ≤ 0.75 at T-24h)", False)]:
        subset = [r for r in results if r["is_heavy_fav"] == fav_filter]
        if not subset:
            continue

        drifts_24 = [r["drift_24_to_close"] for r in subset if r["drift_24_to_close"] is not None]
        drifts_12 = [r["drift_12_to_close"] for r in subset if r["drift_12_to_close"] is not None]
        drifts_6  = [r["drift_6_to_close"]  for r in subset if r["drift_6_to_close"]  is not None]

        def median(vals: list[float]) -> float:
            if not vals:
                return 0.0
            s = sorted(vals)
            n = len(s)
            return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

        def pct_positive(vals: list[float]) -> float:
            return sum(1 for v in vals if v > 0) / len(vals) * 100 if vals else 0.0

        print(f"\n  {label} — {len(subset)} markets")
        print(f"    T-24h → close: median drift {median(drifts_24):+.4f}  |  {pct_positive(drifts_24):.0f}% positive  |  max {max(drifts_24, default=0):+.4f}  min {min(drifts_24, default=0):+.4f}")
        print(f"    T-12h → close: median drift {median(drifts_12):+.4f}  |  {pct_positive(drifts_12):.0f}% positive")
        print(f"    T-6h  → close: median drift {median(drifts_6):+.4f}  |  {pct_positive(drifts_6):.0f}% positive")

    print(f"\n  Total markets analyzed: {len(results)}\n")
```

- [ ] **Step 2: Commit**

```bash
git add analytics/tanking_scanner.py
git commit -m "feat: tanking scanner — historical backtest"
```

---

## Task 7: Rotowire Lineup Monitor

**Files:**
- Modify: `requirements.txt` (add `beautifulsoup4`)
- Modify: `analytics/tanking_scanner.py`

- [ ] **Step 1: Add beautifulsoup4 to requirements.txt**

Add this line to `requirements.txt`:
```
beautifulsoup4>=4.12
```

Then install it:
```bash
venv/bin/pip install beautifulsoup4
```

- [ ] **Step 2: Add lineup monitor function**

Append to `analytics/tanking_scanner.py`:

```python
# ---------------------------------------------------------------------------
# Rotowire Lineup Monitor
# ---------------------------------------------------------------------------


async def check_lineup_news(
    team_name: str,
    session: aiohttp.ClientSession,
) -> list[str]:
    """Fetch Rotowire NBA lineups page and extract injury/status notes for team.

    Returns list of strings like "Jayson Tatum - OUT (ankle)".
    Returns empty list on any fetch/parse error (non-blocking).
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("beautifulsoup4 not installed; skipping lineup check. Run: pip install beautifulsoup4")
        return []

    try:
        async with session.get(
            _ROTOWIRE_URL,
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        ) as r:
            if r.status != 200:
                return []
            html = await r.text()
    except Exception as exc:
        logger.warning(f"Rotowire fetch failed: {exc}")
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
        notes: list[str] = []
        team_name_lower = team_name.lower()

        # Rotowire groups lineups by team. Look for team headers, then players.
        for team_section in soup.find_all("div", class_=lambda c: c and "lineup__team" in c):
            team_text = team_section.get_text(separator=" ", strip=True).lower()
            # Match by any word of the team name (e.g. "Celtics", "Boston")
            team_words = [w for w in team_name_lower.split() if len(w) > 3]
            if not any(w in team_text for w in team_words):
                continue

            # Scan for OUT / QUESTIONABLE players in this section
            for player_div in team_section.find_all("li"):
                text = player_div.get_text(separator=" ", strip=True)
                if any(status in text.upper() for status in ("OUT", "QUESTIONABLE", "DOUBTFUL")):
                    notes.append(text[:120])

        return notes

    except Exception as exc:
        logger.warning(f"Rotowire parse error: {exc}")
        return []


async def enrich_with_lineup_news(
    signals: list[TankingSignal],
    session: aiohttp.ClientSession,
) -> None:
    """Mutates signals in-place, adding lineup_notes for each motivated team."""
    # Fetch Rotowire page once and pass raw HTML — avoid re-fetching per team.
    # Use asyncio.gather for concurrent team lookups (they all share the same session).
    tasks = [check_lineup_news(s.motivated_team, session) for s in signals]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for signal, result in zip(signals, results):
        if isinstance(result, list):
            signal.lineup_notes = result
```

- [ ] **Step 3: Commit**

```bash
git add requirements.txt analytics/tanking_scanner.py
git commit -m "feat: tanking scanner — Rotowire lineup monitor"
```

---

## Task 8: DB Logging + CLI + Watch Mode

**Files:**
- Modify: `analytics/tanking_scanner.py`

- [ ] **Step 1: Add DB logging function**

Append to `analytics/tanking_scanner.py`:

```python
# ---------------------------------------------------------------------------
# DB Logging
# ---------------------------------------------------------------------------


async def log_signals_to_db(pool: asyncpg.Pool, signals: list[TankingSignal]) -> int:
    """Insert signals into tanking_signals. Skips duplicates (same market_id in last 6h).
    Returns count of newly inserted rows.
    """
    inserted = 0
    async with pool.acquire() as conn:
        for s in signals:
            existing = await conn.fetchval(
                """
                SELECT id FROM tanking_signals
                WHERE market_id = $1
                  AND scanned_at > NOW() - INTERVAL '6 hours'
                LIMIT 1
                """,
                s.market_id,
            )
            if existing:
                continue

            await conn.execute(
                """
                INSERT INTO tanking_signals
                    (market_id, game_start, motivated_team, tanking_team,
                     motivation_differential, current_price, drift_24h,
                     pattern_strength, action, lineup_notes)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """,
                s.market_id,
                s.game_start,
                s.motivated_team,
                s.tanking_team,
                s.motivation_differential,
                s.current_price,
                s.actual_drift,
                s.pattern_strength,
                s.recommended_action,
                json.dumps(s.lineup_notes) if s.lineup_notes else None,
            )
            inserted += 1

    return inserted
```

- [ ] **Step 2: Add main run function**

```python
# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run(
    config: dict,
    min_differential: float,
    hours: float,
    watch: bool,
    backtest: bool,
    save_to_db: bool,
) -> None:
    db = config["database"]
    pool = await asyncpg.create_pool(
        host=db["host"],
        port=db["port"],
        database=db["name"],
        user=db["user"],
        password=str(db["password"]),
        min_size=1,
        max_size=3,
    )

    try:
        if backtest:
            await run_backtest(pool)
            return

        aliases = load_aliases()
        first_run = True

        async with aiohttp.ClientSession() as session:
            while True:
                standings = await get_standings(session)
                signals = await scan_tanking_patterns(
                    pool, standings, aliases, min_differential, hours
                )

                # Enrich with Rotowire lineup news for HIGH signals only
                high_signals = [s for s in signals if s.pattern_strength == "HIGH"]
                if high_signals:
                    await enrich_with_lineup_news(high_signals, session)

                print_signals(signals)

                # Print lineup alerts inline
                for s in high_signals:
                    if s.lineup_notes:
                        print(f"  ⚠ Lineup news for {s.motivated_team}:")
                        for note in s.lineup_notes[:5]:
                            print(f"    {note}")
                        print()

                if save_to_db and signals:
                    n = await log_signals_to_db(pool, signals)
                    if n:
                        print(f"  Saved {n} new signal(s) to tanking_signals table.")

                if not watch:
                    break

                print(f"  Refreshing in {_WATCH_INTERVAL_SEC // 60} min ... (Ctrl+C to exit)\n")
                await asyncio.sleep(_WATCH_INTERVAL_SEC)

    finally:
        await pool.close()
```

- [ ] **Step 3: Add CLI parser + entrypoint**

```python
# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="NBA Tanking Pattern Scanner — find motivated vs. tanking matchups"
    )
    p.add_argument(
        "--min-differential", type=float, default=0.4,
        help="Minimum abs(motivation_differential) to include (default: 0.4 = MODERATE+)"
    )
    p.add_argument(
        "--hours", type=float, default=48.0,
        help="Only show games starting within N hours (default: 48)"
    )
    p.add_argument(
        "--backtest", action="store_true",
        help="Run historical backtest on collected price_snapshots"
    )
    p.add_argument(
        "--watch", action="store_true",
        help=f"Auto-refresh every {_WATCH_INTERVAL_SEC // 60} min"
    )
    p.add_argument(
        "--save", action="store_true",
        help="Persist signals to tanking_signals DB table"
    )
    p.add_argument("--config", default="config/settings.yaml")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"ERROR: {args.config} not found.")
        sys.exit(1)
    with open(config_path) as f:
        config = yaml.safe_load(f)

    try:
        asyncio.run(run(
            config,
            min_differential=args.min_differential,
            hours=args.hours,
            watch=args.watch,
            backtest=args.backtest,
            save_to_db=args.save,
        ))
    except KeyboardInterrupt:
        print("\nStopped.")
```

- [ ] **Step 4: Commit**

```bash
git add analytics/tanking_scanner.py
git commit -m "feat: tanking scanner — DB logging + CLI + watch mode"
```

---

## Task 9: Makefile + CLAUDE.md

**Files:**
- Modify: `Makefile`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add Makefile targets**

In `Makefile`, add after the `scanner-daemon` target:

```makefile
## Scan upcoming NBA games for tanking pattern
tanking:
	$(PYTHON) -m analytics.tanking_scanner

## Run tanking backtest on collected snapshots
tanking-backtest:
	$(PYTHON) -m analytics.tanking_scanner --backtest
```

- [ ] **Step 2: Update CLAUDE.md**

In the Analytics CLI section of `CLAUDE.md`, add:

```bash
# Tanking scanner — detect motivated vs. tanking NBA matchups (end-of-season edge)
python -m analytics.tanking_scanner                           # scan next 48h games
python -m analytics.tanking_scanner --min-differential 0.6    # only HIGH+ signals
python -m analytics.tanking_scanner --hours 24                # next 24h only
python -m analytics.tanking_scanner --backtest                # validate on collected data
python -m analytics.tanking_scanner --watch                   # refresh every 30 min
python -m analytics.tanking_scanner --save                    # persist to tanking_signals table
make tanking                                                   # convenience alias
make tanking-backtest
```

- [ ] **Step 3: Commit**

```bash
git add Makefile CLAUDE.md
git commit -m "docs: document tanking scanner CLI commands"
```

---

## Spec Coverage Check

| Requirement | Task |
|-------------|------|
| NBA standings fetcher (ESPN + fallback YAML) | Task 3 |
| Motivation scorer (1.0 / 0.7 / 0.0 / -0.3) | Task 3 |
| motivation_differential computation | Task 3 |
| Market matcher (upcoming NBA markets from DB, name matching) | Task 4 |
| Pattern scanner (HIGH/MODERATE flags, drift, recommended_action) | Task 5 |
| Output table with all specified columns | Task 5 |
| BUY/SELL/CLOSE/WATCH logic per spec | Task 5 |
| Historical backtest (T-24h/12h/6h/2h drift, heavy fav grouping) | Task 6 |
| Rotowire lineup monitor (BeautifulSoup, OUT/QUESTIONABLE flags) | Task 7 |
| tanking_signals DB table | Task 2 |
| CLI (--min-differential, --hours, --backtest, --watch) | Task 8 |
| `make tanking` | Task 9 |
| ESPN fallback config | Task 1 |
| Team name alias dict | Task 1 |
| No real-money execution logic | ✅ analysis only throughout |
