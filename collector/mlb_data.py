"""
MLB Data Fetcher — ESPN public API client for game schedules and pitcher stats.

Provides:
  - Upcoming MLB games with probable starting pitchers
  - Per-pitcher season stats (ERA, WHIP, W-L, K/9, IP)
  - Bullpen usage approximation (games played recent)

All data from ESPN public API (no auth required).

Usage:
    from collector.mlb_data import MLBDataFetcher
    async with aiohttp.ClientSession() as session:
        fetcher = MLBDataFetcher(session)
        games = await fetcher.get_upcoming_games(hours=48)
        stats = await fetcher.get_pitcher_stats(athlete_id)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_ESPN_STANDINGS = "https://site.api.espn.com/apis/v2/sports/baseball/mlb/standings"

# MLB Stats API (official) — schedule + pitcher stats, no auth required
_MLB_SCHEDULE = "https://statsapi.mlb.com/api/v1/schedule"
_MLB_PERSON_STATS = "https://statsapi.mlb.com/api/v1/people/{person_id}"

_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


@dataclass
class PitcherInfo:
    """Probable starting pitcher for a game."""
    athlete_id: str
    full_name: str
    team_abbreviation: str
    handedness: str = ""         # "L" / "R"
    # Season stats (populated by get_pitcher_stats)
    era: Optional[float] = None
    whip: Optional[float] = None
    wins: int = 0
    losses: int = 0
    innings_pitched: float = 0.0
    strikeouts: int = 0
    walks: int = 0
    games_started: int = 0
    # Derived
    k_per_9: float = 0.0
    bb_per_9: float = 0.0

    @property
    def has_stats(self) -> bool:
        return self.era is not None

    @property
    def record_str(self) -> str:
        return f"{self.wins}-{self.losses}"

    @property
    def quality_score(self) -> float:
        """Lower is better. Composite of ERA + WHIP, weighted.
        Returns 99.0 if no stats available."""
        if self.era is None or self.whip is None:
            return 99.0
        # ERA weight 0.6, WHIP weight 0.4 (normalized to similar scale)
        return self.era * 0.6 + self.whip * 4.0 * 0.4


@dataclass
class MLBGame:
    """Upcoming MLB game with probable starters."""
    espn_event_id: str
    game_start: datetime
    home_team: str              # display name: "New York Yankees"
    home_abbreviation: str      # "NYY"
    away_team: str
    away_abbreviation: str
    home_record: str = ""       # "10-5"
    away_record: str = ""
    home_pitcher: Optional[PitcherInfo] = None
    away_pitcher: Optional[PitcherInfo] = None
    venue: str = ""
    status: str = "pre"         # "pre" / "in" / "post"

    @property
    def pitcher_differential(self) -> Optional[float]:
        """Difference in quality_score (higher = bigger mismatch).
        Positive means away pitcher is worse (home has advantage).
        Negative means home pitcher is worse (away has advantage)."""
        if not self.home_pitcher or not self.away_pitcher:
            return None
        if not self.home_pitcher.has_stats or not self.away_pitcher.has_stats:
            return None
        return self.away_pitcher.quality_score - self.home_pitcher.quality_score

    @property
    def favored_team(self) -> Optional[str]:
        """Team with the better starting pitcher."""
        diff = self.pitcher_differential
        if diff is None:
            return None
        if diff > 0:
            return self.home_team
        elif diff < 0:
            return self.away_team
        return None

    @property
    def underdog_team(self) -> Optional[str]:
        diff = self.pitcher_differential
        if diff is None:
            return None
        if diff > 0:
            return self.away_team
        elif diff < 0:
            return self.home_team
        return None


class MLBDataFetcher:
    """Async ESPN MLB data client."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session

    async def _get(self, url: str, params: dict = None) -> Optional[dict]:
        """GET with retry and timeout."""
        for attempt in range(3):
            try:
                async with self._session.get(
                    url,
                    params=params,
                    timeout=_REQUEST_TIMEOUT,
                    headers={"User-Agent": _USER_AGENT},
                ) as r:
                    if r.status == 200:
                        return await r.json(content_type=None)
                    if r.status == 429:
                        wait = 10 * (attempt + 1)
                        logger.warning("ESPN rate-limited, waiting %ds", wait)
                        await asyncio.sleep(wait)
                    else:
                        logger.warning("ESPN HTTP %d for %s", r.status, url)
                        return None
            except Exception as exc:
                logger.warning("ESPN request failed (attempt %d): %s", attempt + 1, exc)
                if attempt < 2:
                    await asyncio.sleep(2)
        return None

    # ------------------------------------------------------------------
    # Game Schedule + Probable Pitchers
    # ------------------------------------------------------------------

    async def get_upcoming_games(self, hours: float = 48.0) -> list[MLBGame]:
        """Fetch MLB games starting within `hours` from now via MLB Stats API.

        Uses statsapi.mlb.com which returns MLB person IDs for probable pitchers —
        the same IDs work for fetching stats in get_pitcher_stats().
        """
        now = datetime.now(tz=timezone.utc)
        cutoff = now + timedelta(hours=hours)

        # Query each date in the window
        dates = []
        for offset in range(3):
            d = now + timedelta(days=offset)
            dates.append(d.strftime("%Y-%m-%d"))

        games: list[MLBGame] = []
        seen_ids: set[str] = set()

        for date_str in dates:
            data = await self._get(_MLB_SCHEDULE, params={
                "sportId": 1,
                "date": date_str,
                "hydrate": "probablePitcher,linescore,team,lineups",
            })
            if not data:
                continue

            for date_obj in data.get("dates", []):
                for game_obj in date_obj.get("games", []):
                    game = self._parse_mlb_game(game_obj)
                    if game is None:
                        continue
                    if game.espn_event_id in seen_ids:
                        continue
                    if game.status != "pre":
                        continue
                    if game.game_start < now or game.game_start > cutoff:
                        continue
                    seen_ids.add(game.espn_event_id)
                    games.append(game)

            await asyncio.sleep(0.3)

        games.sort(key=lambda g: g.game_start)
        logger.info("Found %d upcoming MLB games in next %.0fh", len(games), hours)
        return games

    def _parse_mlb_game(self, game_obj: dict) -> Optional[MLBGame]:
        """Parse a single MLB Stats API game object into an MLBGame."""
        try:
            status = game_obj.get("status", {}).get("abstractGameState", "")
            if status == "Final":
                mapped_status = "post"
            elif status == "Live":
                mapped_status = "in"
            else:
                mapped_status = "pre"

            date_str = game_obj.get("gameDate", "")
            game_start = datetime.fromisoformat(date_str.replace("Z", "+00:00"))

            teams = game_obj.get("teams", {})
            home_data = teams.get("home", {})
            away_data = teams.get("away", {})
            home_team = home_data.get("team", {})
            away_team = away_data.get("team", {})

            home_abbrev = home_team.get("abbreviation", "???")
            away_abbrev = away_team.get("abbreviation", "???")

            home_rec = home_data.get("leagueRecord", {})
            away_rec = away_data.get("leagueRecord", {})
            home_record = f"{home_rec.get('wins', 0)}-{home_rec.get('losses', 0)}"
            away_record = f"{away_rec.get('wins', 0)}-{away_rec.get('losses', 0)}"

            home_pitcher = self._parse_mlb_probable(
                game_obj.get("teams", {}).get("home", {}).get("probablePitcher"),
                home_abbrev,
            )
            away_pitcher = self._parse_mlb_probable(
                game_obj.get("teams", {}).get("away", {}).get("probablePitcher"),
                away_abbrev,
            )

            return MLBGame(
                espn_event_id=str(game_obj.get("gamePk", "")),
                game_start=game_start,
                home_team=home_team.get("name", "Unknown"),
                home_abbreviation=home_abbrev,
                away_team=away_team.get("name", "Unknown"),
                away_abbreviation=away_abbrev,
                home_record=home_record,
                away_record=away_record,
                home_pitcher=home_pitcher,
                away_pitcher=away_pitcher,
                venue=game_obj.get("venue", {}).get("name", ""),
                status=mapped_status,
            )
        except (KeyError, IndexError, ValueError) as exc:
            logger.debug("Failed to parse MLB game: %s", exc)
            return None

    def _parse_mlb_probable(self, pitcher_data: Optional[dict], team_abbrev: str) -> Optional[PitcherInfo]:
        """Parse probable pitcher from MLB Stats API game object."""
        if not pitcher_data:
            return None
        try:
            person_id = str(pitcher_data.get("id", ""))
            if not person_id:
                return None
            full_name = pitcher_data.get("fullName", pitcher_data.get("nameFirstLast", "Unknown"))
            return PitcherInfo(
                athlete_id=person_id,
                full_name=full_name,
                team_abbreviation=team_abbrev,
            )
        except (KeyError, ValueError):
            return None


    # ------------------------------------------------------------------
    # Pitcher Season Stats
    # ------------------------------------------------------------------

    async def get_pitcher_stats(self, pitcher: PitcherInfo) -> PitcherInfo:
        """Enrich a PitcherInfo with season stats from MLB Stats API.

        athlete_id holds the MLB Stats API person ID (populated by _parse_mlb_probable).
        Mutates and returns the same PitcherInfo object.
        """
        url = _MLB_PERSON_STATS.format(person_id=pitcher.athlete_id)
        data = await self._get(url, params={"hydrate": "stats(group=[pitching],type=[season])"})
        if not data:
            return pitcher

        try:
            people = data.get("people", [])
            if not people:
                return pitcher
            person = people[0]

            for stat_group in person.get("stats", []):
                group = stat_group.get("group", {}).get("displayName", "")
                if group.lower() != "pitching":
                    continue
                splits = stat_group.get("splits", [])
                if not splits:
                    continue
                # Last split = most recent season
                stat_map = splits[-1].get("stat", {})
                self._apply_stats(pitcher, stat_map)
                return pitcher

        except Exception as exc:
            logger.warning("Failed to parse stats for %s (ID %s): %s",
                           pitcher.full_name, pitcher.athlete_id, exc)

        return pitcher

    def _apply_stats(self, pitcher: PitcherInfo, stat_map: dict) -> None:
        """Apply a stat name→value map to a PitcherInfo."""
        def _float(key: str, default: float = 0.0) -> float:
            val = stat_map.get(key, default)
            try:
                return float(val)
            except (ValueError, TypeError):
                return default

        def _int(key: str, default: int = 0) -> int:
            val = stat_map.get(key, default)
            try:
                return int(float(val))
            except (ValueError, TypeError):
                return default

        # MLB Stats API uses camelCase; keep short aliases as fallback
        pitcher.era = _float("era", _float("ERA", pitcher.era))
        pitcher.whip = _float("whip", _float("WHIP", pitcher.whip))
        pitcher.wins = _int("wins", _int("W", pitcher.wins))
        pitcher.losses = _int("losses", _int("L", pitcher.losses))
        pitcher.innings_pitched = _float("inningsPitched", _float("IP", pitcher.innings_pitched))
        pitcher.strikeouts = _int("strikeOuts", _int("SO", _int("K", pitcher.strikeouts)))
        pitcher.walks = _int("baseOnBalls", _int("BB", _int("walks", pitcher.walks)))
        pitcher.games_started = _int("gamesStarted", _int("GS", pitcher.games_started))

        # Derived per-9 stats
        if pitcher.innings_pitched > 0:
            pitcher.k_per_9 = round(pitcher.strikeouts / pitcher.innings_pitched * 9, 2)
            pitcher.bb_per_9 = round(pitcher.walks / pitcher.innings_pitched * 9, 2)

    async def enrich_all_pitchers(self, games: list[MLBGame]) -> None:
        """Fetch stats for all probable pitchers in a list of games.
        Mutates PitcherInfo objects in-place.
        """
        pitchers_to_fetch: list[PitcherInfo] = []
        seen_ids: set[str] = set()

        for game in games:
            for p in (game.home_pitcher, game.away_pitcher):
                if p and p.athlete_id not in seen_ids:
                    seen_ids.add(p.athlete_id)
                    pitchers_to_fetch.append(p)

        if not pitchers_to_fetch:
            return

        logger.info("Fetching stats for %d pitchers...", len(pitchers_to_fetch))

        # Batch with concurrency limit to avoid rate-limiting
        sem = asyncio.Semaphore(5)

        async def _fetch(p: PitcherInfo):
            async with sem:
                await self.get_pitcher_stats(p)
                await asyncio.sleep(0.3)

        await asyncio.gather(*[_fetch(p) for p in pitchers_to_fetch], return_exceptions=True)

        enriched = sum(1 for p in pitchers_to_fetch if p.has_stats)
        logger.info("Enriched %d/%d pitchers with season stats", enriched, len(pitchers_to_fetch))
