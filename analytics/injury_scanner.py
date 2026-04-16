"""
NBA Injury Scanner — detect impactful OUT/DOUBTFUL reports from Rotowire.

When a key player is ruled OUT/DOUBTFUL close to tip-off, the opponent's
moneyline market often takes a few minutes to reprice. We generate a BUY
signal on the healthy team (opponent) and alert via Telegram.

Usage:
    python -m analytics.injury_scanner                    # scan once, print
    python -m analytics.injury_scanner --hours 24         # only next 24h
    python -m analytics.injury_scanner --statuses OUT     # stricter filter
    python -m analytics.injury_scanner --save             # persist to DB
    python -m analytics.injury_scanner --dry-run          # no DB writes
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

# Reuse building blocks from the tanking scanner so Rotowire parsing and
# NBA market lookup stay consistent across both scanners.
from analytics.tanking_scanner import (  # noqa: E402
    _ROTOWIRE_URL,
    find_upcoming_nba_markets,
    load_aliases,
    match_teams_in_question,
)
from trading.position_manager import resolve_team_token_side  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent

# Severity ordering used to pick the most impactful injury when a team has multiple
_STATUS_PRIORITY = {"OUT": 4, "DOUBTFUL": 3, "QUESTIONABLE": 2, "GTD": 1}
_DEFAULT_STATUSES = {"OUT", "DOUBTFUL"}

# Rotowire uses a `title` attribute on <li class="lineup__player"> for the
# humanised play probability. We map it to our 4 status codes and ignore the
# "likely to play" / "expected to play" variants.
_ROTOWIRE_TITLE_STATUS = {
    "out": "OUT",
    "very unlikely to play": "OUT",
    "doubtful": "DOUBTFUL",
    "very doubtful to play": "DOUBTFUL",
    "questionable": "QUESTIONABLE",
    "toss up to play": "QUESTIONABLE",
    "game time decision": "GTD",
    "gtd": "GTD",
}

# Rotowire matchup containers only expose the 3-letter team abbreviation (e.g.
# "ORL", "PHI"), so we need a local map back to the canonical ESPN display name
# that the rest of the codebase (tanking_scanner, nba_team_aliases) expects.
_NBA_ABBR_TO_CANONICAL = {
    "ATL": "Atlanta Hawks",       "BOS": "Boston Celtics",
    "BKN": "Brooklyn Nets",       "CHA": "Charlotte Hornets",
    "CHI": "Chicago Bulls",       "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks",    "DEN": "Denver Nuggets",
    "DET": "Detroit Pistons",     "GSW": "Golden State Warriors",
    "HOU": "Houston Rockets",     "IND": "Indiana Pacers",
    "LAC": "LA Clippers",         "LAL": "Los Angeles Lakers",
    "MEM": "Memphis Grizzlies",   "MIA": "Miami Heat",
    "MIL": "Milwaukee Bucks",     "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans", "NYK": "New York Knicks",
    "OKC": "Oklahoma City Thunder", "ORL": "Orlando Magic",
    "PHI": "Philadelphia 76ers",  "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings",
    "SAS": "San Antonio Spurs",   "TOR": "Toronto Raptors",
    "UTA": "Utah Jazz",           "WAS": "Washington Wizards",
}


@dataclass
class InjuryReport:
    team: str          # canonical team name (from aliases)
    player_name: str   # "Jayson Tatum"
    status: str        # OUT / DOUBTFUL / QUESTIONABLE / GTD
    raw_text: str      # full Rotowire line for debugging


@dataclass
class InjurySignal:
    market_id: str
    slug: str
    game_start: datetime
    hours_to_game: float
    injured_team: str              # team with the OUT/DOUBTFUL player
    healthy_team: str              # opponent — the BUY side
    player_name: str
    status: str
    current_price: float           # T-35: now ALWAYS the healthy_team's contract price
    price_24h_ago: Optional[float] # T-35: also healthy_team-side
    drift_24h: Optional[float]     # signed; positive = healthy_team got cheaper→stronger
    action: str                    # BUY / WATCH
    healthy_side: Optional[str] = None  # T-35: 'YES' or 'NO' — which Polymarket side
    notes: str = ""


def _abbr_to_canonical(abbr: str) -> Optional[str]:
    """Map a Rotowire 3-letter NBA abbreviation to the ESPN canonical team name."""
    if not abbr:
        return None
    return _NBA_ABBR_TO_CANONICAL.get(abbr.strip().upper())


async def fetch_rotowire_injuries(
    session: aiohttp.ClientSession,
    aliases: Optional[dict[str, str]] = None,  # kept for backward-compat, unused
) -> list[InjuryReport]:
    """Fetch Rotowire NBA lineups page, extract all OUT/DOUBTFUL/GTD entries.

    Parses the current DOM layout (verified against live Rotowire 2026-04-15):
      <div class="lineup is-nba">          -- one per matchup (game)
        <div class="lineup__box">
          <div class="lineup__main">
            <ul class="lineup__list is-visit">
              <li class="lineup__player is-pct-play-0" title="Very Unlikely To Play">
                <div class="lineup__pos">PG</div>
                <a title="Player Name">Player Name</a>
              </li>
              ...
            </ul>
            <ul class="lineup__list is-home"> ... </ul>
          </div>
          <div class="lineup__team"> visit_abbr </div>
          <div class="lineup__team"> home_abbr </div>
    """
    del aliases  # no longer needed — we use hard-coded abbr map

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("beautifulsoup4 not installed; run: pip install beautifulsoup4")
        return []

    try:
        async with session.get(
            _ROTOWIRE_URL,
            timeout=aiohttp.ClientTimeout(total=20),
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        ) as r:
            if r.status != 200:
                logger.warning(f"Rotowire HTTP {r.status}")
                return []
            html = await r.text()
    except Exception as exc:
        logger.warning(f"Rotowire fetch failed: {exc}")
        return []

    reports: list[InjuryReport] = []
    try:
        soup = BeautifulSoup(html, "html.parser")

        # Each matchup is a <div class="lineup ...">. Decorative divs that share
        # the "lineup" class but have no <ul class="lineup__list"> inside are
        # filtered out below.
        for matchup in soup.find_all("div", class_="lineup"):
            lists = matchup.find_all("ul", class_="lineup__list")
            if not lists:
                continue

            # Rotowire convention: first lineup__abbr is the visitor, second the
            # home team. Verified live against ORL @ PHI on 2026-04-15.
            abbr_els = matchup.find_all(class_="lineup__abbr")
            if len(abbr_els) < 2:
                continue
            visit_team = _abbr_to_canonical(abbr_els[0].get_text(strip=True))
            home_team = _abbr_to_canonical(abbr_els[1].get_text(strip=True))

            for ul in lists:
                ul_classes = ul.get("class", [])
                if "is-home" in ul_classes:
                    team = home_team
                elif "is-visit" in ul_classes:
                    team = visit_team
                else:
                    continue
                if not team:
                    continue

                for li in ul.find_all("li", class_="lineup__player"):
                    title_raw = (li.get("title") or "").strip().lower()
                    status = _ROTOWIRE_TITLE_STATUS.get(title_raw)
                    if not status:
                        continue

                    anchor = li.find("a")
                    if anchor is None:
                        continue
                    name = anchor.get("title") or anchor.get_text(strip=True)
                    if not name or len(name) < 3:
                        continue

                    reports.append(InjuryReport(
                        team=team,
                        player_name=name.strip(),
                        status=status,
                        raw_text=f"{name} [{title_raw}]",
                    ))
    except Exception as exc:
        logger.warning(f"Rotowire parse error: {exc}")
        return []

    return reports


async def build_injury_signals(
    pool: asyncpg.Pool,
    session: aiohttp.ClientSession,
    aliases: dict[str, str],
    hours_window: float = 24.0,
    statuses: Optional[set[str]] = None,
    max_entry_price: float = 0.85,
) -> list[InjurySignal]:
    """Cross-reference Rotowire injury reports with upcoming NBA markets.

    Produces at most one InjurySignal per (market, injured_team). If the
    injured team has multiple reported players, picks the highest-severity one.
    """
    statuses = statuses or _DEFAULT_STATUSES

    reports = await fetch_rotowire_injuries(session, aliases)
    logger.info("Rotowire: %d raw injury reports parsed", len(reports))
    reports = [r for r in reports if r.status in statuses]
    if not reports:
        return []

    by_team: dict[str, list[InjuryReport]] = {}
    for r in reports:
        by_team.setdefault(r.team, []).append(r)

    markets = await find_upcoming_nba_markets(pool, hours=hours_window)
    if not markets:
        return []

    now = datetime.now(tz=timezone.utc)
    signals: list[InjurySignal] = []

    for m in markets:
        teams_in_q = match_teams_in_question(m.get("question") or "", aliases)
        if len(teams_in_q) != 2:
            continue
        t1, t2 = teams_in_q

        if t1 in by_team:
            injured, healthy = t1, t2
        elif t2 in by_team:
            injured, healthy = t2, t1
        else:
            continue

        injured_players = by_team[injured]
        top = max(injured_players, key=lambda p: _STATUS_PRIORITY.get(p.status, 0))

        yes_mid = m.get("current_price")
        if yes_mid is None:
            continue
        yes_mid = float(yes_mid)

        # T-35 P1.2 — resolve which side of the binary market the HEALTHY team
        # sits on. price_snapshots.mid_price is always the YES-token mid; if
        # healthy_team is on the NO side we must invert to (1 - yes_mid) so the
        # signal reflects the contract we'd actually buy.
        _, healthy_side = await resolve_team_token_side(
            pool, m["id"], healthy, aliases
        )
        if healthy_side is None:
            # Can't determine which side healthy_team is on → skip rather than
            # alert with an unverified price. T-35 makes this a hard guard.
            logger.debug(
                "Skipping %s (%s vs %s): could not resolve healthy_team side",
                m.get("slug"), healthy, injured,
            )
            continue

        if healthy_side == "YES":
            healthy_price = yes_mid
        else:  # 'NO' — invert
            healthy_price = 1.0 - yes_mid

        yes_24h = m.get("price_24h_ago")
        if yes_24h is not None:
            yes_24h = float(yes_24h)
            past_price = yes_24h if healthy_side == "YES" else (1.0 - yes_24h)
        else:
            past_price = None

        drift = (healthy_price - past_price) if past_price is not None else None

        event_start: datetime = m["event_start"]
        if event_start.tzinfo is None:
            event_start = event_start.replace(tzinfo=timezone.utc)
        hours_to_game = (event_start - now).total_seconds() / 3600

        # BUY only if healthy team's contract is cheap enough and there is time
        # for the market to reprice pre-tip.
        action = "BUY" if (healthy_price < max_entry_price and hours_to_game > 0.5) else "WATCH"

        notes = "; ".join(
            f"{p.player_name} {p.status}" for p in injured_players[:4]
        )

        signals.append(InjurySignal(
            market_id=m["id"],
            slug=m.get("slug") or m["id"],
            game_start=event_start,
            hours_to_game=round(hours_to_game, 1),
            injured_team=injured,
            healthy_team=healthy,
            player_name=top.player_name,
            status=top.status,
            current_price=round(healthy_price, 4),
            price_24h_ago=round(past_price, 4) if past_price is not None else None,
            drift_24h=round(drift, 4) if drift is not None else None,
            action=action,
            healthy_side=healthy_side,
            notes=notes,
        ))

    # Sort: BUY first, then by highest severity
    signals.sort(key=lambda s: (
        0 if s.action == "BUY" else 1,
        -_STATUS_PRIORITY.get(s.status, 0),
        s.hours_to_game,
    ))
    return signals


async def persist_signals(pool: asyncpg.Pool, signals: list[InjurySignal]) -> int:
    """Insert signals into injury_signals table; skip dupes in last 24h."""
    inserted = 0
    for s in signals:
        exists = await pool.fetchval(
            """
            SELECT 1 FROM injury_signals
            WHERE market_id = $1 AND player_name = $2 AND status = $3
              AND scanned_at > NOW() - INTERVAL '24 hours'
            LIMIT 1
            """,
            s.market_id, s.player_name, s.status,
        )
        if exists:
            continue
        await pool.execute(
            """
            INSERT INTO injury_signals
                (market_id, game_start, injured_team, healthy_team,
                 player_name, status, current_price, drift_24h, action, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            s.market_id, s.game_start, s.injured_team, s.healthy_team,
            s.player_name, s.status, s.current_price, s.drift_24h,
            s.action, s.notes,
        )
        inserted += 1
    return inserted


def print_signals(signals: list[InjurySignal]) -> None:
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print("=" * 100)
    print(f"  NBA INJURY SCANNER  —  {now_str}")
    print("=" * 100)

    if not signals:
        print("\n  No injury signals matched upcoming NBA markets.\n")
        print("=" * 100)
        return

    for s in signals:
        drift_str = f"{s.drift_24h:+.3f}" if s.drift_24h is not None else "  n/a"
        print()
        print(f"  [{s.action:5s}]  BUY {s.healthy_team}  vs  {s.injured_team}")
        print(f"           player : {s.player_name} ({s.status})")
        print(f"           price  : {s.current_price:.3f}   drift_24h: {drift_str}")
        print(f"           game   : {s.game_start.strftime('%Y-%m-%d %H:%M UTC')}  "
              f"(T-{s.hours_to_game:.1f}h)")
        if s.notes and s.notes != f"{s.player_name} {s.status}":
            print(f"           notes  : {s.notes}")

    print()
    print("=" * 100)


async def run(args: argparse.Namespace) -> None:
    with (_ROOT / "config" / "settings.yaml").open() as f:
        cfg = yaml.safe_load(f)

    db = cfg["database"]
    pool = await asyncpg.create_pool(
        host=db["host"], port=db["port"], database=db["name"],
        user=db["user"], password=str(db["password"]),
        min_size=1, max_size=3,
    )

    try:
        aliases = load_aliases()
        statuses = {s.strip().upper() for s in args.statuses.split(",") if s.strip()}
        async with aiohttp.ClientSession() as session:
            signals = await build_injury_signals(
                pool, session, aliases,
                hours_window=args.hours,
                statuses=statuses,
                max_entry_price=args.max_price,
            )

        print_signals(signals)

        if args.save and not args.dry_run and signals:
            n = await persist_signals(pool, signals)
            print(f"  Saved {n} new signal(s) to injury_signals table.\n")
    finally:
        await pool.close()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NBA Injury Scanner (Rotowire + Polymarket)")
    p.add_argument("--hours", type=float, default=24.0,
                   help="Scan games starting within N hours (default: 24)")
    p.add_argument("--statuses", type=str, default="OUT,DOUBTFUL",
                   help="Comma-separated status filter (default: OUT,DOUBTFUL)")
    p.add_argument("--max-price", type=float, default=0.85,
                   help="Only BUY if YES price is below this (default: 0.85)")
    p.add_argument("--save", action="store_true",
                   help="Persist signals to injury_signals table")
    p.add_argument("--dry-run", action="store_true",
                   help="Skip DB writes even if --save is set")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
