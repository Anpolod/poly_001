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


def load_aliases() -> dict[str, str]:
    """Load team alias map from config/nba_team_aliases.yaml.
    Returns {alias_lowercase: canonical_display_name}.
    """
    with open(_ALIASES_PATH) as f:
        raw = yaml.safe_load(f)
    # Keys in YAML are strings; ensure all lowercase for matching
    return {k.lower(): v for k, v in raw.items()}


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
        conf_short = "East" if "east" in conf_name.lower() else "West"
        entries = conference.get("standings", {}).get("entries", [])

        for entry in entries:
            team = entry.get("team", {})
            stats_list = entry.get("stats", [])
            stats = {s["name"]: s["value"] for s in stats_list if "name" in s and "value" in s}

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


def load_standings_from_fallback() -> list[TeamStanding]:
    """Load standings from config/nba_standings.yaml (manually maintained)."""
    try:
        with open(_FALLBACK_STANDINGS_PATH) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Fallback standings missing: {_FALLBACK_STANDINGS_PATH}. "
            "Create it or ensure ESPN API is reachable."
        )

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


def _gb_from_seed(
    team: TeamStanding, seed_rank: int, conf_teams: list[TeamStanding]
) -> float:
    """Compute games back from a specific conference seed. Returns 0.0 if ahead."""
    ref = next((t for t in conf_teams if t.rank == seed_rank), None)
    if ref is None:
        logger.warning("Seed %d not found in %s conference — GB defaulting to 0.0", seed_rank, team.conference)
        return 0.0
    return max(0.0, (ref.wins - team.wins + team.losses - ref.losses) / 2.0)


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
        for team in conf_teams:
            team.games_back_from_6th = _gb_from_seed(team, 6, conf_teams)
            team.games_back_from_10th = _gb_from_seed(team, 10, conf_teams)


def compute_motivation_score(team: TeamStanding) -> float:
    """Return motivation_score in range [-0.3, 1.0].

    1.0  = direct playoff spot locked or within reach (GB <= 3 from 6th seed)
    0.7  = in play-in or close (GB <= 3 from 10th seed)
    0.0  = eliminated (no path to play-in)
   -0.3  = deeply eliminated with active tanking incentive (>= 5 GB from 10th, eliminated)
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
        # Deep elimination = active tanking incentive (no rem >= 10 guard —
        # this scanner runs specifically at end of season when rem is small)
        if gb10 >= 5:
            return -0.3
        return 0.0

    return 0.0


def build_standings(standings: list[TeamStanding]) -> dict[str, TeamStanding]:
    """Compute margins and motivation scores; return {display_name: TeamStanding}."""
    _compute_gb_margins(standings)
    for team in standings:
        team.motivation_score = compute_motivation_score(team)
    return {t.display_name: t for t in standings}


async def get_standings(session: aiohttp.ClientSession) -> dict[str, TeamStanding]:
    """Fetch standings from ESPN; fall back to config/nba_standings.yaml on error."""
    try:
        raw = await fetch_standings_from_espn(session)
        logger.info(f"Fetched {len(raw)} teams from ESPN standings API")
    except Exception as exc:
        logger.warning(f"ESPN standings API failed ({exc}); using fallback YAML")
        raw = load_standings_from_fallback()

    return build_standings(raw)


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
