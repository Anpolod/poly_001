"""
MLB Pitcher Differential Scanner

Detects upcoming MLB matchups where the starting pitcher differential creates
a prematch drift opportunity — analogous to the NBA tanking pattern.

When an ace (ERA < 3.0) faces a weak starter (ERA > 5.0), the Polymarket
moneyline typically drifts 2-5% toward the team with the better pitcher
in the 24 hours before first pitch.

Usage:
    python -m analytics.mlb_pitcher_scanner
    python -m analytics.mlb_pitcher_scanner --min-differential 1.5 --hours 48
    python -m analytics.mlb_pitcher_scanner --watch
    python -m analytics.mlb_pitcher_scanner --save
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import asyncpg
import yaml
from tabulate import tabulate

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ALIASES_PATH = _PROJECT_ROOT / "config" / "mlb_team_aliases.yaml"
_WATCH_INTERVAL_SEC = 1800  # 30 min

# Lazy import to avoid circular — mlb_data is in collector/
sys.path.insert(0, str(_PROJECT_ROOT))

# Round-5 fix: scanner must resolve favored-team side (YES or NO) and invert
# price when the favored team is on NO. Previously we stored raw YES-side
# price + action regardless, which misled operators and the dashboard on ~50%
# of MLB markets. Same helper the trading path uses in T-35.
from trading.position_manager import resolve_team_token_side  # noqa: E402


@dataclass
class PitcherSignal:
    """A detected pitcher mismatch opportunity."""
    market_id: str
    slug: str
    game_start: datetime
    hours_to_game: float
    favored_team: str           # team with better SP
    underdog_team: str          # team with worse SP
    home_pitcher_name: str
    home_pitcher_era: Optional[float]
    home_pitcher_whip: Optional[float]
    away_pitcher_name: str
    away_pitcher_era: Optional[float]
    away_pitcher_whip: Optional[float]
    era_differential: float     # abs(ERA_away - ERA_home) — higher = bigger mismatch
    quality_differential: float # composite quality score difference
    current_price: float        # Round-5 fix: now ALWAYS favored_team's contract price (YES or inverted)
    price_24h_ago: Optional[float]
    actual_drift: Optional[float]
    signal_strength: str        # "HIGH" / "MODERATE" / "WATCH"
    recommended_action: str     # "BUY" / "SELL / CLOSE" / "WATCH"
    favored_side: Optional[str] = None   # Round-5: 'YES' or 'NO' — which Polymarket side
    venue: str = ""
    home_record: str = ""
    away_record: str = ""


def load_mlb_aliases() -> dict[str, str]:
    """Load team alias map from config/mlb_team_aliases.yaml."""
    with open(_ALIASES_PATH) as f:
        raw = yaml.safe_load(f)
    return {k.lower(): v for k, v in raw.items()}


def match_teams_in_question(
    question: str,
    aliases: dict[str, str],
) -> list[str]:
    """Return canonical team names found in the market question.
    Longest-match-first to avoid partial overlaps.
    """
    q = question.lower()
    matched: list[str] = []
    seen: set[str] = set()

    for alias in sorted(aliases.keys(), key=len, reverse=True):
        canonical = aliases[alias]
        if alias in q and canonical not in seen:
            matched.append(canonical)
            seen.add(canonical)
        if len(matched) == 2:
            break

    return matched


# ---------------------------------------------------------------------------
# Market Matcher
# ---------------------------------------------------------------------------


async def find_upcoming_mlb_markets(
    pool: asyncpg.Pool,
    hours: float = 48.0,
) -> list[dict]:
    """Query DB for MLB markets starting within `hours` that have at least one snapshot."""
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
            m.league ILIKE '%mlb%'
            OR m.question ILIKE '%mlb%'
            OR (m.sport ILIKE '%baseball%')
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
# Signal Classification
# ---------------------------------------------------------------------------


def _signal_strength(era_diff: float, quality_diff: float) -> str:
    """Classify signal strength based on pitcher mismatch size."""
    # ERA differential alone
    if era_diff >= 2.5 and quality_diff >= 3.0:
        return "HIGH"
    if era_diff >= 1.5 and quality_diff >= 2.0:
        return "MODERATE"
    return "WATCH"


def _recommended_action(
    strength: str,
    current_price: float,
    hours_to_game: float,
) -> str:
    """Determine action based on signal strength, price, and timing."""
    if strength == "HIGH":
        if current_price < 0.65 and hours_to_game > 8:
            return "BUY"
        if hours_to_game < 3:
            return "SELL / CLOSE"
    if strength == "MODERATE":
        if current_price < 0.60 and hours_to_game > 12:
            return "BUY"
    return "WATCH"


# ---------------------------------------------------------------------------
# Pattern Scanner
# ---------------------------------------------------------------------------


async def scan_pitcher_patterns(
    pool: asyncpg.Pool,
    games: list,  # list[MLBGame] from mlb_data
    aliases: dict[str, str],
    min_era_diff: float = 1.0,
    hours: float = 48.0,
) -> list[PitcherSignal]:
    """Main scan: cross-reference ESPN games with Polymarket markets.

    For each ESPN game with a significant pitcher mismatch, find the
    corresponding Polymarket market and generate a signal.
    """

    markets = await find_upcoming_mlb_markets(pool, hours)
    if not markets:
        logger.info("No active MLB markets found in DB")
        return []

    now = datetime.now(tz=timezone.utc)
    signals: list[PitcherSignal] = []

    for game in games:
        # Skip games without both pitchers or stats
        if not game.home_pitcher or not game.away_pitcher:
            continue
        if not game.home_pitcher.has_stats or not game.away_pitcher.has_stats:
            continue

        era_diff = abs((game.home_pitcher.era or 0) - (game.away_pitcher.era or 0))
        quality_diff = abs(game.pitcher_differential or 0)

        if era_diff < min_era_diff:
            continue

        # Find matching Polymarket market
        favored = game.favored_team
        if not favored:
            continue

        matched_market = None
        for m in markets:
            question = (m["question"] or "").lower()
            teams = match_teams_in_question(question, aliases)
            if len(teams) >= 2:
                # Check if both teams match
                game_teams = {game.home_team, game.away_team}
                if set(teams) == game_teams or set(teams).issubset(game_teams):
                    matched_market = m
                    break

        if not matched_market:
            # No Polymarket market found — still report for awareness
            event_start = game.game_start
            hours_to_game = (event_start - now).total_seconds() / 3600

            strength = _signal_strength(era_diff, quality_diff)
            signals.append(PitcherSignal(
                market_id="",
                slug="",
                game_start=game.game_start,
                hours_to_game=round(hours_to_game, 1),
                favored_team=favored,
                underdog_team=game.underdog_team or "",
                home_pitcher_name=game.home_pitcher.full_name,
                home_pitcher_era=game.home_pitcher.era,
                home_pitcher_whip=game.home_pitcher.whip,
                away_pitcher_name=game.away_pitcher.full_name,
                away_pitcher_era=game.away_pitcher.era,
                away_pitcher_whip=game.away_pitcher.whip,
                era_differential=round(era_diff, 2),
                quality_differential=round(quality_diff, 2),
                current_price=0.0,
                price_24h_ago=None,
                actual_drift=None,
                signal_strength=strength,
                recommended_action="NO MARKET",
                venue=game.venue,
                home_record=game.home_record,
                away_record=game.away_record,
            ))
            continue

        # Market found — build full signal
        event_start = matched_market["event_start"]
        if event_start.tzinfo is None:
            event_start = event_start.replace(tzinfo=timezone.utc)
        hours_to_game = (event_start - now).total_seconds() / 3600

        yes_mid = float(matched_market["current_price"] or 0.0)
        yes_24h = float(matched_market["price_24h_ago"]) if matched_market["price_24h_ago"] is not None else None

        # Round-5 fix: resolve which side the favored team is on. price_snapshots
        # stores the YES-token mid, so for NO-side favored teams we must invert
        # to (1 - yes_mid) — otherwise dashboards and pitcher_signals rows show
        # the underdog's contract price and the BUY/WATCH action is computed
        # from the wrong series.
        _, favored_side = await resolve_team_token_side(
            pool, matched_market["id"], favored, aliases
        )
        if favored_side is None:
            logger.debug(
                "Skipping %s (%s vs %s): could not resolve favored_team side",
                matched_market.get("slug"), favored, game.underdog_team,
            )
            continue

        if favored_side == "YES":
            current_price = yes_mid
            price_24h = yes_24h
        else:  # 'NO' — invert
            current_price = 1.0 - yes_mid
            price_24h = (1.0 - yes_24h) if yes_24h is not None else None

        drift = (current_price - price_24h) if price_24h is not None else None

        strength = _signal_strength(era_diff, quality_diff)
        action = _recommended_action(strength, current_price, hours_to_game)

        signals.append(PitcherSignal(
            market_id=matched_market["id"],
            slug=matched_market.get("slug", ""),
            game_start=event_start,
            hours_to_game=round(hours_to_game, 1),
            favored_team=favored,
            underdog_team=game.underdog_team or "",
            home_pitcher_name=game.home_pitcher.full_name,
            home_pitcher_era=game.home_pitcher.era,
            home_pitcher_whip=game.home_pitcher.whip,
            away_pitcher_name=game.away_pitcher.full_name,
            away_pitcher_era=game.away_pitcher.era,
            away_pitcher_whip=game.away_pitcher.whip,
            era_differential=round(era_diff, 2),
            quality_differential=round(quality_diff, 2),
            current_price=round(current_price, 4),
            price_24h_ago=round(price_24h, 4) if price_24h is not None else None,
            actual_drift=round(drift, 4) if drift is not None else None,
            signal_strength=strength,
            recommended_action=action,
            favored_side=favored_side,
            venue=game.venue,
            home_record=game.home_record,
            away_record=game.away_record,
        ))

    # Sort: HIGH first, then by ERA differential descending
    rank = {"HIGH": 2, "MODERATE": 1, "WATCH": 0}
    signals.sort(key=lambda s: (-rank.get(s.signal_strength, 0), -s.era_differential))
    return signals


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def print_signals(signals: list[PitcherSignal], title: str = "") -> None:
    """Pretty-print scanner results."""
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    width = 130
    print("=" * width)
    print(f"  MLB PITCHER DIFFERENTIAL SCANNER  —  {now_str}")
    if title:
        print(f"  {title}")
    print("=" * width)

    if not signals:
        print("\n  No pitcher mismatch signals found.\n")
        print("=" * width)
        return

    rows = []
    for s in signals:
        drift_str = f"{s.actual_drift:+.3f}" if s.actual_drift is not None else "—"
        price_str = f"{s.current_price:.3f}" if s.current_price > 0 else "—"
        h_era = f"{s.home_pitcher_era:.2f}" if s.home_pitcher_era is not None else "?"
        a_era = f"{s.away_pitcher_era:.2f}" if s.away_pitcher_era is not None else "?"

        rows.append([
            s.signal_strength,
            s.recommended_action,
            s.favored_team[:20],
            f"{s.home_pitcher_name[:15]} ({h_era})",
            f"{s.away_pitcher_name[:15]} ({a_era})",
            f"{s.era_differential:.1f}",
            price_str,
            drift_str,
            f"{s.hours_to_game:.1f}h",
            s.game_start.strftime("%m-%d %H:%M"),
        ])

    headers = [
        "Strength", "Action", "Favored", "Home SP (ERA)",
        "Away SP (ERA)", "ERA Δ", "Price", "Drift", "Game in", "Start"
    ]
    print("\n" + tabulate(rows, headers=headers, tablefmt="simple") + "\n")

    # Detail section for HIGH signals
    high = [s for s in signals if s.signal_strength == "HIGH"]
    if high:
        print("  — HIGH Signal Details —")
        for s in high:
            h_whip = f"{s.home_pitcher_whip:.2f}" if s.home_pitcher_whip else "?"
            a_whip = f"{s.away_pitcher_whip:.2f}" if s.away_pitcher_whip else "?"
            print(f"  {s.favored_team} vs {s.underdog_team}")
            print(f"    Home: {s.home_pitcher_name} — ERA {s.home_pitcher_era}, WHIP {h_whip}")
            print(f"    Away: {s.away_pitcher_name} — ERA {s.away_pitcher_era}, WHIP {a_whip}")
            if s.venue:
                print(f"    Venue: {s.venue}")
            if s.home_record or s.away_record:
                print(f"    Records: {s.home_record} vs {s.away_record}")
            print()

    print("=" * width)


# ---------------------------------------------------------------------------
# DB Logging
# ---------------------------------------------------------------------------


async def log_signals_to_db(pool: asyncpg.Pool, signals: list[PitcherSignal]) -> int:
    """Insert signals into pitcher_signals table. Skips duplicates (same market in last 6h)."""
    inserted = 0
    async with pool.acquire() as conn:
        for s in signals:
            if not s.market_id:
                continue  # skip signals without a Polymarket market

            existing = await conn.fetchval(
                """
                SELECT id FROM pitcher_signals
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
                INSERT INTO pitcher_signals
                    (market_id, game_start, favored_team, underdog_team,
                     home_pitcher, home_era, away_pitcher, away_era,
                     era_differential, quality_differential,
                     current_price, drift_24h,
                     signal_strength, action)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                """,
                s.market_id,
                s.game_start,
                s.favored_team,
                s.underdog_team,
                s.home_pitcher_name,
                s.home_pitcher_era,
                s.away_pitcher_name,
                s.away_pitcher_era,
                s.era_differential,
                s.quality_differential,
                s.current_price,
                s.actual_drift,
                s.signal_strength,
                s.recommended_action,
            )
            inserted += 1

    return inserted


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run(
    config: dict,
    min_era_diff: float,
    hours: float,
    watch: bool,
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
        aliases = load_mlb_aliases()

        async with aiohttp.ClientSession() as session:
            from collector.mlb_data import MLBDataFetcher  # noqa: PLC0415
            fetcher = MLBDataFetcher(session)

            while True:
                # 1. Fetch games + pitchers from ESPN
                games = await fetcher.get_upcoming_games(hours)

                # 2. Enrich with pitcher stats
                await fetcher.enrich_all_pitchers(games)

                # 3. Cross-reference with Polymarket + generate signals
                signals = await scan_pitcher_patterns(
                    pool, games, aliases, min_era_diff, hours
                )

                # 4. Display
                print_signals(signals)

                # 5. Persist
                if save_to_db and signals:
                    n = await log_signals_to_db(pool, signals)
                    if n:
                        print(f"  Saved {n} new signal(s) to pitcher_signals table.")

                if not watch:
                    break

                print(f"  Refreshing in {_WATCH_INTERVAL_SEC // 60} min ... (Ctrl+C to exit)\n")
                await asyncio.sleep(_WATCH_INTERVAL_SEC)

    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="MLB Pitcher Differential Scanner — find starting pitcher mismatches"
    )
    p.add_argument(
        "--min-era-diff", type=float, default=1.0,
        help="Minimum ERA differential to include (default: 1.0)"
    )
    p.add_argument(
        "--hours", type=float, default=48.0,
        help="Only show games starting within N hours (default: 48)"
    )
    p.add_argument(
        "--watch", action="store_true",
        help=f"Auto-refresh every {_WATCH_INTERVAL_SEC // 60} min"
    )
    p.add_argument(
        "--save", action="store_true",
        help="Persist signals to pitcher_signals DB table"
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
            min_era_diff=args.min_era_diff,
            hours=args.hours,
            watch=args.watch,
            save_to_db=args.save,
        ))
    except KeyboardInterrupt:
        print("\nStopped.")
