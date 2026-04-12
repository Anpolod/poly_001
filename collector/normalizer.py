"""Data normalisation — computation of derived fields."""

from datetime import datetime, timezone


def compute_time_to_event(event_start: datetime) -> float:
    """Return hours remaining until the event starts."""
    now = datetime.now(timezone.utc)
    delta = (event_start - now).total_seconds() / 3600
    return round(delta, 2)


def compute_spread_pct(best_bid: float, best_ask: float) -> float | None:
    """Return the bid-ask spread as a percentage of the mid price."""
    if not best_bid or not best_ask or best_bid <= 0:
        return None
    mid = (best_bid + best_ask) / 2
    if mid <= 0:
        return None
    return round((best_ask - best_bid) / mid * 100, 4)


def compute_price_move(history: list, hours: float) -> float | None:
    """
    Compute the absolute price move over the last N hours.
    Supports formats:
      [{"t": ts, "p": price}, ...]
      [{"timestamp": ts, "price": price}, ...]
      [[ts, price], ...]
    """
    if not history or len(history) < 2:
        return None

    now_ts = datetime.now(timezone.utc).timestamp()
    target_ts = now_ts - (hours * 3600)

    # Normalise to (timestamp, price) tuples
    points = []
    for item in history:
        if isinstance(item, dict):
            t = float(item.get("t") or item.get("timestamp") or item.get("time") or 0)
            p = float(item.get("p") or item.get("price") or item.get("mid") or 0)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            t, p = float(item[0]), float(item[1])
        else:
            continue
        if t > 0 and p > 0:
            points.append((t, p))

    if len(points) < 2:
        return None

    # Current price = last data point
    current_price = points[-1][1]

    # Find the closest point to target_ts
    closest_price = None
    closest_diff = float("inf")
    for t, p in points:
        diff = abs(t - target_ts)
        if diff < closest_diff:
            closest_diff = diff
            closest_price = p

    if closest_price is None or closest_price == 0:
        return None

    return round(abs(current_price - closest_price), 4)


def normalize_snapshot(market_id: str, orderbook: dict, event_start: datetime) -> dict:
    """Build a normalised snapshot dict ready for DB insertion."""
    now = datetime.now(timezone.utc)
    mid = orderbook.get("mid_price")
    return {
        "ts": now,
        "market_id": market_id,
        "best_bid": orderbook.get("best_bid"),
        "best_ask": orderbook.get("best_ask"),
        "mid_price": mid,
        "spread": orderbook.get("spread"),
        "bid_depth": orderbook.get("bid_depth"),
        "ask_depth": orderbook.get("ask_depth"),
        "volume_24h": orderbook.get("volume_24h"),
        "time_to_event_h": compute_time_to_event(event_start),
    }
