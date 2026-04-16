"""
Risk Manager — pure functions, no I/O.

Computes position sizes and enforces exposure limits.
All decisions are based on config values + current exposure from DB.
"""

from __future__ import annotations


def max_position_usd(config: dict) -> float:
    """Maximum dollar size for a single position."""
    t = config["trading"]
    return t["budget_usd"] * t["max_position_pct"] / 100.0


def max_total_exposure_usd(config: dict) -> float:
    """Maximum total open exposure across all positions."""
    t = config["trading"]
    return t["budget_usd"] * t["max_total_exposure_pct"] / 100.0


def can_open(config: dict, total_exposure: float, ask_depth_usd: float) -> tuple[bool, str]:
    """Check if a new position can be opened.

    Returns:
        (True, "") if OK
        (False, reason) if blocked
    """
    max_exp = max_total_exposure_usd(config)
    if total_exposure >= max_exp:
        return False, f"max exposure reached (${total_exposure:.0f} / ${max_exp:.0f})"

    min_depth = config["trading"].get("min_ask_depth_usd", 50)
    if ask_depth_usd < min_depth:
        return False, f"ask depth ${ask_depth_usd:.0f} < min ${min_depth:.0f} (too thin)"

    return True, ""


def position_size(config: dict, price: float) -> tuple[float, float]:
    """Compute (size_usd, size_shares) for a new position.

    size_usd is capped at max_position_usd.
    size_shares = size_usd / price (rounded to 2 decimals).
    """
    size_usd = min(max_position_usd(config), config["trading"]["budget_usd"])
    size_shares = round(size_usd / price, 2) if price > 0 else 0.0
    return size_usd, size_shares


def within_slippage(config: dict, signal_price: float, live_price: float) -> bool:
    """Return True if live_price is within slippage tolerance of signal_price."""
    tol = config["trading"].get("price_slippage_tolerance", 0.03)
    return live_price <= signal_price + tol


def position_size_by_ev(config: dict, price: float, roi_pct: float) -> tuple[float, float]:
    """Scale position size proportionally to expected value (ROI).

    Between ev_min_roi_pct and ev_max_roi_pct the size scales linearly
    from ev_min_scale × max_position up to max_position.

    Config keys (all optional, have defaults):
        trading.ev_min_roi_pct  — ROI floor (default 5.0 %)
        trading.ev_max_roi_pct  — ROI ceiling (default 20.0 %)
        trading.ev_min_scale    — fraction of max_position at floor (default 0.4)

    Examples with budget=$100, max_position_pct=5% → max=$5, min_scale=0.4:
        roi= 5% → $2.00   (40% of $5)
        roi=12% → $3.50   (70% of $5)
        roi=20% → $5.00  (100% of $5)
        roi=30% → $5.00  (capped at 100%)
    """
    t = config["trading"]
    min_roi = float(t.get("ev_min_roi_pct", 5.0))
    max_roi = float(t.get("ev_max_roi_pct", 20.0))
    min_scale = float(t.get("ev_min_scale", 0.4))

    max_size = max_position_usd(config)

    # Normalise roi_pct to [0.0, 1.0] across the configured range
    span = max_roi - min_roi if max_roi > min_roi else 1.0
    frac = max(0.0, min(1.0, (roi_pct - min_roi) / span))

    # Linear interpolation: min_scale → 1.0
    size_factor = min_scale + frac * (1.0 - min_scale)
    size_usd = round(max_size * size_factor, 2)
    size_shares = round(size_usd / price, 2) if price > 0 else 0.0
    return size_usd, size_shares


def tanking_roi_estimate(
    motivation_differential: float,
    actual_drift: float | None,
) -> float:
    """Estimate ROI % for a tanking signal (no exact model, uses heuristics).

    Formula:
        base  = differential × 15   (0.5 diff → 7.5%, 1.0 diff → 15%)
        drift = drift × 20 if drift > 0 (price already moving our way)
        ROI   = base + drift, capped at 30%

    This is a rough signal-strength proxy, not a calibrated probability model.
    """
    base = motivation_differential * 15.0
    drift_bonus = (actual_drift * 20.0) if actual_drift and actual_drift > 0 else 0.0
    return min(base + drift_bonus, 30.0)
