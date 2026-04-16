"""
Unit tests for trading/position_manager.py — T-35 YES/NO side resolver.

Tests cover:
  - _resolve_yes_no_teams_from_text (pure-logic position parser)
  - resolve_team_token_side (async DB lookup wrapper, pool mocked)

Run with:
    pytest tests/test_resolve_team_token.py -v
    venv/bin/python -m pytest tests/test_resolve_team_token.py -v
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from trading.position_manager import (
    _resolve_yes_no_teams_from_text,
    resolve_team_token_side,
)

# Minimal alias map — same shape as config/nba_team_aliases.yaml
_NBA_ALIASES: dict[str, str] = {
    # Boston
    "celtics": "Boston Celtics",
    "boston celtics": "Boston Celtics",
    "boston": "Boston Celtics",
    # Lakers
    "lakers": "Los Angeles Lakers",
    "los angeles lakers": "Los Angeles Lakers",
    # Warriors
    "warriors": "Golden State Warriors",
    "golden state warriors": "Golden State Warriors",
    "golden state": "Golden State Warriors",
    # Clippers
    "clippers": "LA Clippers",
    "la clippers": "LA Clippers",
    # The classic substring trap: "nets" is inside "hornets"
    "nets": "Brooklyn Nets",
    "brooklyn": "Brooklyn Nets",
    "hornets": "Charlotte Hornets",
    "charlotte": "Charlotte Hornets",
    # Sixers / Magic for slug parsing test
    "76ers": "Philadelphia 76ers",
    "sixers": "Philadelphia 76ers",
    "philadelphia": "Philadelphia 76ers",
    "magic": "Orlando Magic",
    "orlando": "Orlando Magic",
}


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_yes_no_teams_from_text — pure logic
# ─────────────────────────────────────────────────────────────────────────────


def test_resolve_question_full_names() -> None:
    """Polymarket convention: 'Warriors vs. Clippers' → YES=Warriors, NO=Clippers."""
    yes, no = _resolve_yes_no_teams_from_text("Warriors vs. Clippers", _NBA_ALIASES)
    assert yes == "Golden State Warriors"
    assert no == "LA Clippers"


def test_resolve_question_reversed() -> None:
    """Order in question matters — 'Clippers vs. Warriors' must flip the result."""
    yes, no = _resolve_yes_no_teams_from_text("Clippers vs. Warriors", _NBA_ALIASES)
    assert yes == "LA Clippers"
    assert no == "Golden State Warriors"


def test_resolve_lakers_celtics_alias_length_does_not_override_position() -> None:
    """`match_teams_in_question` would put Lakers first (longer alias), but
    position parser must respect actual order: Celtics appears first → YES."""
    yes, no = _resolve_yes_no_teams_from_text("Celtics vs Lakers", _NBA_ALIASES)
    assert yes == "Boston Celtics"
    assert no == "Los Angeles Lakers"


def test_resolve_substring_trap_nets_vs_hornets() -> None:
    """`nets` is a substring of `hornets`. Longest-alias-first iteration must
    match `hornets` to Charlotte, not steal its position for Brooklyn Nets."""
    yes, no = _resolve_yes_no_teams_from_text("Hornets vs. Nets", _NBA_ALIASES)
    assert yes == "Charlotte Hornets"
    assert no == "Brooklyn Nets"


def test_resolve_full_question_form() -> None:
    """Handles 'Will the X beat the Y?' phrasing — X still appears first."""
    yes, no = _resolve_yes_no_teams_from_text(
        "Will the Boston Celtics beat the Los Angeles Lakers?", _NBA_ALIASES
    )
    assert yes == "Boston Celtics"
    assert no == "Los Angeles Lakers"


def test_resolve_slug_form_lowercase_dashes() -> None:
    """Slug-style text 'nba-magic-76ers-2026-04-15' should still parse."""
    yes, no = _resolve_yes_no_teams_from_text("nba-magic-76ers-2026-04-15", _NBA_ALIASES)
    assert yes == "Orlando Magic"
    assert no == "Philadelphia 76ers"


def test_resolve_empty_text_returns_none() -> None:
    yes, no = _resolve_yes_no_teams_from_text("", _NBA_ALIASES)
    assert yes is None
    assert no is None


def test_resolve_single_team_text_returns_none() -> None:
    """Need both teams to be confident — a single-team text is ambiguous."""
    yes, no = _resolve_yes_no_teams_from_text("Boston Celtics season opener", _NBA_ALIASES)
    assert yes is None
    assert no is None


def test_resolve_no_known_teams_returns_none() -> None:
    yes, no = _resolve_yes_no_teams_from_text("Random Team A vs. Random Team B", _NBA_ALIASES)
    assert yes is None
    assert no is None


# ─────────────────────────────────────────────────────────────────────────────
# resolve_team_token_side — async wrapper with mocked pool
# ─────────────────────────────────────────────────────────────────────────────


def _mock_pool_returning(row: dict | None) -> AsyncMock:
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=row)
    return pool


def test_resolve_token_side_yes_team_returns_yes_token() -> None:
    pool = _mock_pool_returning({
        "slug": "nba-warriors-clippers-2026-04-15",
        "question": "Warriors vs. Clippers",
        "token_id_yes": "TOK_YES_GSW",
        "token_id_no": "TOK_NO_LAC",
    })
    token, side = asyncio.run(
        resolve_team_token_side(pool, "mid", "Golden State Warriors", _NBA_ALIASES)
    )
    assert token == "TOK_YES_GSW"
    assert side == "YES"


def test_resolve_token_side_no_team_returns_no_token() -> None:
    pool = _mock_pool_returning({
        "slug": "nba-warriors-clippers-2026-04-15",
        "question": "Warriors vs. Clippers",
        "token_id_yes": "TOK_YES_GSW",
        "token_id_no": "TOK_NO_LAC",
    })
    token, side = asyncio.run(
        resolve_team_token_side(pool, "mid", "LA Clippers", _NBA_ALIASES)
    )
    assert token == "TOK_NO_LAC"
    assert side == "NO"


def test_resolve_token_side_market_missing_returns_none() -> None:
    pool = _mock_pool_returning(None)
    token, side = asyncio.run(
        resolve_team_token_side(pool, "missing", "Boston Celtics", _NBA_ALIASES)
    )
    assert token is None
    assert side is None


def test_resolve_token_side_missing_token_columns_returns_none() -> None:
    pool = _mock_pool_returning({
        "slug": "nba-warriors-clippers-2026-04-15",
        "question": "Warriors vs. Clippers",
        "token_id_yes": "",       # market not yet enriched with token ids
        "token_id_no": None,
    })
    token, side = asyncio.run(
        resolve_team_token_side(pool, "mid", "Golden State Warriors", _NBA_ALIASES)
    )
    assert token is None
    assert side is None


def test_resolve_token_side_falls_back_to_slug_when_question_unparseable() -> None:
    """If question text doesn't carry recognizable team aliases (e.g. set to
    a generic title), the slug should still parse."""
    pool = _mock_pool_returning({
        "slug": "nba-celtics-lakers-2026-04-15",
        "question": "Tonight's marquee matchup",  # zero alias matches
        "token_id_yes": "TOK_YES",
        "token_id_no": "TOK_NO",
    })
    token, side = asyncio.run(
        resolve_team_token_side(pool, "mid", "Boston Celtics", _NBA_ALIASES)
    )
    assert token == "TOK_YES"
    assert side == "YES"


def test_resolve_token_side_unknown_team_in_market_returns_none() -> None:
    """If caller passes a team that isn't in this market at all."""
    pool = _mock_pool_returning({
        "slug": "nba-warriors-clippers-2026-04-15",
        "question": "Warriors vs. Clippers",
        "token_id_yes": "TOK_YES",
        "token_id_no": "TOK_NO",
    })
    token, side = asyncio.run(
        resolve_team_token_side(pool, "mid", "Boston Celtics", _NBA_ALIASES)
    )
    assert token is None
    assert side is None


if __name__ == "__main__":
    # Allow standalone run: `python tests/test_resolve_team_token.py`
    pytest.main([__file__, "-v"])
