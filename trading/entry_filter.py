"""
Entry Filter — decides whether market conditions justify entering now.

Pure functions, no I/O. Called before placing each buy order.

Criteria for entry:
  1. bid > 0            — at least one buyer exists
  2. ask < 0.95         — not a dead market (ask at 0.99 = no sellers)
  3. ask - bid < 0.20   — spread ≤ 20 cents (reasonable liquidity)
  4. ask ≤ signal_price × 1.15  — live price not >15% above our signal
  5. ask_depth_usd ≥ min_depth  — enough in ask side to fill our size
  6. hours_to_game > min_hours  — not entering at the last minute

Returns (is_liquid: bool, reason: str, emoji: str)
  🟢 — enter now at live ask
  🟡 — place limit order at signal_price and wait
  🔴 — skip (game too close, price too far from signal)
"""

from __future__ import annotations


_MAX_SPREAD = 0.20          # cents between bid and ask
_MAX_ASK_RATIO = 1.15       # ask must be ≤ signal_price × this
_MAX_DEAD_ASK = 0.95        # ask ≥ this → no real sellers
_MIN_HOURS_TO_GAME = 1.0    # don't enter if game starts in < 1 hour


def check_entry(
    bid: float,
    ask: float,
    signal_price: float,
    ask_depth_usd: float,
    hours_to_game: float,
    min_depth_usd: float = 20.0,
) -> tuple[str, str, str]:
    """Evaluate market conditions for entry.

    Returns:
        (decision, reason, emoji)
        decision: "enter" | "limit" | "skip"
        emoji:    "🟢"    | "🟡"    | "🔴"
    """
    # Hard skips — conditions where we should not enter at all
    if hours_to_game < _MIN_HOURS_TO_GAME:
        return "skip", f"game starts in {hours_to_game:.1f}h (< {_MIN_HOURS_TO_GAME}h)", "🔴"

    if ask > signal_price * _MAX_ASK_RATIO:
        over_pct = (ask / signal_price - 1) * 100
        return "skip", f"ask {ask:.3f} is {over_pct:.0f}% above signal {signal_price:.3f}", "🔴"

    # Thin market — place GTC limit at signal_price instead
    if ask >= _MAX_DEAD_ASK:
        return "limit", f"ask {ask:.3f} ≥ {_MAX_DEAD_ASK} (dead market — limit order)", "🟡"

    if bid <= 0:
        return "limit", "no bids — limit order at signal price", "🟡"

    if ask - bid > _MAX_SPREAD:
        spread = ask - bid
        return "limit", f"spread {spread:.3f} > {_MAX_SPREAD} — limit order", "🟡"

    if ask_depth_usd < min_depth_usd:
        return "limit", f"ask depth ${ask_depth_usd:.0f} < ${min_depth_usd:.0f} — limit order", "🟡"

    # All checks passed — enter at live ask
    return "enter", f"liquid (bid={bid:.3f} ask={ask:.3f} depth=${ask_depth_usd:.0f})", "🟢"


def format_market_status(
    bid: float,
    ask: float,
    signal_price: float,
    ask_depth_usd: float,
    hours_to_game: float,
    min_depth_usd: float = 20.0,
) -> str:
    """Return a one-line Telegram-ready status string for this market."""
    decision, reason, emoji = check_entry(
        bid, ask, signal_price, ask_depth_usd, hours_to_game, min_depth_usd
    )
    return f"{emoji} {reason}"
