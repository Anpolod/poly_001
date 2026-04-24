"""Maker pricing — pure math helpers for the T-58 LP rewards MM.

No I/O, no DB, no network. Just the arithmetic of turning (mid, config)
into (bid_price, ask_price, shares). The upcoming agent composes these
with maker_ledger + clob_executor to place/update quotes.

Why a separate module:
  - Eminently testable: all inputs are scalars, all outputs deterministic.
  - Easy to tune without touching the async agent (e.g., experimenting
    with asymmetric half-spreads when inventory is skewed).
  - Keeps the agent itself focused on orchestration, not arithmetic.
"""

from __future__ import annotations

import math

_TICK = 0.001   # Polymarket CLOB price granularity
_EPS = 1e-9     # float-precision tolerance on envelope boundary checks


def compute_quote_prices(
    mid: float,
    target_half_spread_pct: float,
    max_spread_pct: float,
) -> tuple[float, float]:
    """Return (bid_price, ask_price) centered around `mid` with half-spread
    `target_half_spread_pct`. Both prices stay within the `max_spread_pct`
    envelope around mid — a hard requirement for the quote to qualify
    for LP rewards on Polymarket.

    target_half_spread_pct and max_spread_pct are in PERCENT of mid (e.g.,
    0.5 means 0.5%), matching Polymarket's rewardsMaxSpread convention.

    Prices are aligned to the 0.001 tick. Bid is rounded DOWN (we pay
    less) and ask is rounded UP (we receive more) UNLESS that rounding
    would push the quote OUT of the rewards envelope — which happens for
    small mids where the envelope delta (e.g. 1.5% of 0.1 = 0.0015) is
    smaller than a single tick. In that case we reverse the direction
    (ceil bid / floor ask) so the quote still earns rewards. We'd rather
    give up one tick of price than the whole weekly emission.

    Raises ValueError if mid is outside (0, 1) — Polymarket markets are
    binary and prices must be strictly between 0 and 1.
    """
    if not 0 < mid < 1:
        raise ValueError(f"mid must be in (0, 1), got {mid}")
    if target_half_spread_pct <= 0 or max_spread_pct <= 0:
        raise ValueError("spread percentages must be positive")

    effective = min(target_half_spread_pct, max_spread_pct)
    delta = mid * (effective / 100.0)
    env_delta = mid * (max_spread_pct / 100.0)
    min_bid = mid - env_delta
    max_ask = mid + env_delta

    # Preferred rounding: bid DOWN, ask UP (better price for us)
    bid = math.floor((mid - delta) / _TICK) * _TICK
    ask = math.ceil((mid + delta) / _TICK) * _TICK

    # If rounding pushed us outside the rewards envelope, reverse direction
    # so we stay in range. This trades one tick of price for the whole
    # rewards stream — always a good trade when emission > 0.
    if bid < min_bid - _EPS:
        bid = math.ceil(min_bid / _TICK) * _TICK
    if ask > max_ask + _EPS:
        ask = math.floor(max_ask / _TICK) * _TICK

    # Final clamp to the CLOB's global (0.001, 0.999) range
    bid = max(_TICK, bid)
    ask = min(1.0 - _TICK, ask)
    return round(bid, 4), round(ask, 4)


def size_from_capital(
    price: float,
    capital_usd: float,
    min_shares: float,
    max_shares: float = 1_000_000.0,
) -> float:
    """Compute shares to post at `price` given capital. Clamped to
    [min_shares, max_shares]. Rounded DOWN to 2 decimals (Polymarket
    share granularity).

    Returns 0 if capital can't even cover min_shares at this price — the
    caller must treat 0 as "skip this market, doesn't qualify for rewards"
    because Polymarket rejects orders below rewardsMinSize.
    """
    if price <= 0 or capital_usd <= 0 or min_shares <= 0:
        return 0.0

    raw_shares = capital_usd / price
    if raw_shares < min_shares:
        return 0.0   # can't afford minimum — skip

    clamped = min(raw_shares, max_shares)
    # Round down to 0.01 tick so we never over-allocate capital
    return int(clamped * 100) / 100.0


def needs_reprice(
    existing_price: float,
    current_mid: float,
    reprice_threshold_pct: float,
) -> bool:
    """Return True if existing quote is more than `reprice_threshold_pct`
    away from current mid and should be cancelled + re-posted.

    This is the trigger for quote refresh: without it the agent would
    either (a) spam the CLOB with new orders every tick (wasteful,
    exchange-rate-limit risk) or (b) let stale quotes drift far enough
    from fair value that they fall out of the rewards envelope.

    threshold is in PERCENT of mid (e.g., 0.5 means "reprice when quote
    is more than 0.5% from mid").
    """
    if current_mid <= 0 or reprice_threshold_pct <= 0:
        return False
    drift_pct = abs(existing_price - current_mid) / current_mid * 100.0
    return drift_pct > reprice_threshold_pct


def quote_is_within_rewards_envelope(
    price: float,
    mid: float,
    max_spread_pct: float,
) -> bool:
    """True if a quote at `price` is close enough to `mid` to earn LP
    rewards. Polymarket zeroes out reward weight for orders outside
    `max_spread_pct` (the `rewardsMaxSpread` field per market).

    Use this to validate after compute_quote_prices in case of rounding
    edge cases at the boundary (theoretically impossible given our
    clamp in compute_quote_prices, but cheap defensive check)."""
    if mid <= 0 or max_spread_pct <= 0:
        return False
    drift_pct = abs(price - mid) / mid * 100.0
    # _EPS tolerance so float-rounding at the boundary doesn't flip True→False
    return drift_pct <= max_spread_pct + _EPS
