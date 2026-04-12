"""Phase 0 analytics — cost calculation and verdict assignment."""

import logging
from typing import Optional

from collector.normalizer import compute_price_move, compute_spread_pct

logger = logging.getLogger(__name__)


def compute_taker_round_trip(
    fee_rate: float, spread_pct: float, est_slippage_pct: float
) -> float:
    """Round-trip cost as taker in %."""
    return (fee_rate * 100 * 2) + spread_pct + est_slippage_pct


def compute_maker_round_trip(
    spread_pct: float, adverse_selection_mult: float, fee_rate: float, rebate_pct: float
) -> float:
    """Round-trip cost as maker in %."""
    adverse_selection = spread_pct * adverse_selection_mult
    rebate = fee_rate * 100 * (rebate_pct / 100)
    return adverse_selection - rebate


def compute_ratio(move: Optional[float], mid_price: Optional[float], cost_pct: float) -> Optional[float]:
    """Compute move/cost ratio."""
    if move is None or mid_price is None or mid_price <= 0 or cost_pct <= 0:
        return None
    move_pct = (move / mid_price) * 100
    return round(move_pct / cost_pct, 2)


def compute_verdict(ratio: Optional[float], go_threshold: float, marginal_threshold: float) -> str:
    """Return GO / MARGINAL / NO_GO verdict based on ratio thresholds."""
    if ratio is None:
        return "NO_DATA"
    if ratio >= go_threshold:
        return "GO"
    if ratio >= marginal_threshold:
        return "MARGINAL"
    return "NO_GO"


def analyze_market(
    market: dict,
    orderbook: dict,
    fee_rate: Optional[float],
    price_history: Optional[list],
    config: dict,
) -> dict:
    """Run a full Phase 0 analysis for a single market and return a result dict."""
    cfg = config["phase0"]

    best_bid = orderbook.get("best_bid", 0)
    best_ask = orderbook.get("best_ask", 0)
    spread = orderbook.get("spread")
    mid_price = orderbook.get("mid_price")

    spread_pct = compute_spread_pct(best_bid, best_ask)
    if spread_pct is None:
        spread_pct = 0

    if fee_rate is None:
        fee_rate = 0.0075  # default peak rate

    est_slippage = cfg["est_slippage_pct"]
    taker_rt = compute_taker_round_trip(fee_rate, spread_pct, est_slippage)
    maker_rt = compute_maker_round_trip(
        spread_pct, cfg["adverse_selection_mult"], fee_rate, cfg["maker_rebate_pct"]
    )

    # Price moves
    move_1h = compute_price_move(price_history, 1) if price_history else None
    move_6h = compute_price_move(price_history, 6) if price_history else None
    move_24h = compute_price_move(price_history, 24) if price_history else None
    move_48h = compute_price_move(price_history, 48) if price_history else None
    move_72h = compute_price_move(price_history, 72) if price_history else None

    ratio_24h = compute_ratio(move_24h, mid_price, taker_rt)
    ratio_48h = compute_ratio(move_48h, mid_price, taker_rt)

    verdict = compute_verdict(
        ratio_24h, cfg["ratio_go_threshold"], cfg["ratio_marginal_threshold"]
    )

    return {
        "market_id": market["id"],
        "slug": market.get("slug", ""),
        "sport": market.get("sport", ""),
        "league": market.get("league", ""),
        "event_start": market.get("event_start"),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "spread_pct": spread_pct,
        "bid_depth": orderbook.get("bid_depth", 0),
        "ask_depth": orderbook.get("ask_depth", 0),
        "volume_24h": market.get("volume_24h", 0),
        "fee_rate": fee_rate,
        "taker_rt_cost": round(taker_rt, 4),
        "maker_rt_cost": round(maker_rt, 4),
        "move_1h": move_1h,
        "move_6h": move_6h,
        "move_24h": move_24h,
        "move_48h": move_48h,
        "move_72h": move_72h,
        "ratio_24h": ratio_24h,
        "ratio_48h": ratio_48h,
        "verdict": verdict,
    }


def aggregate_by_league(results: list[dict]) -> list[dict]:
    """Aggregate Phase 0 results by league, returning summary stats per league."""
    groups: dict[str, list[dict]] = {}
    for r in results:
        key = f"{r['sport']}/{r['league']}"
        groups.setdefault(key, []).append(r)

    summary = []
    for key, items in sorted(groups.items()):
        valid = [i for i in items if i["ratio_24h"] is not None]
        if not valid:
            continue

        avg_spread = sum(i["spread_pct"] for i in valid) / len(valid)
        avg_depth = sum((i["bid_depth"] + i["ask_depth"]) / 2 for i in valid) / len(valid)
        avg_volume = sum(i["volume_24h"] for i in valid) / len(valid)
        avg_cost = sum(i["taker_rt_cost"] for i in valid) / len(valid)
        avg_ratio = sum(i["ratio_24h"] for i in valid) / len(valid)

        go_count = sum(1 for i in valid if i["verdict"] == "GO")
        marginal_count = sum(1 for i in valid if i["verdict"] == "MARGINAL")

        summary.append({
            "league": key,
            "markets": len(items),
            "with_data": len(valid),
            "avg_spread_pct": round(avg_spread, 2),
            "avg_depth": round(avg_depth, 0),
            "avg_volume": round(avg_volume, 0),
            "avg_cost_pct": round(avg_cost, 2),
            "avg_ratio": round(avg_ratio, 2),
            "go_count": go_count,
            "marginal_count": marginal_count,
            "verdict": "GO" if avg_ratio >= 1.5 else "NO_GO",
        })

    return summary
