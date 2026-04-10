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
