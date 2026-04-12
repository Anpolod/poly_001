"""CLI tool to re-run Phase 0 analysis on live Polymarket data."""

import argparse
import asyncio
import csv
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import yaml

from analytics.cost_analyzer import analyze_market
from collector.market_discovery import MarketDiscovery
from collector.rest_client import RestClient

logger = logging.getLogger(__name__)

# Phase 0 CSV column order (must match phase0_results.csv exactly)
CSV_COLUMNS = [
    "market_id",
    "slug",
    "sport",
    "league",
    "event_start",
    "best_bid",
    "best_ask",
    "spread",
    "spread_pct",
    "bid_depth",
    "ask_depth",
    "volume_24h",
    "fee_rate",
    "taker_rt_cost",
    "maker_rt_cost",
    "move_1h",
    "move_6h",
    "move_24h",
    "move_48h",
    "move_72h",
    "ratio_24h",
    "ratio_48h",
    "verdict",
]

# Flag thresholds
FLAG_LOW_VOL_THRESHOLD = 10_000
FLAG_EXTREME_ODDS_LOW = 0.15
FLAG_EXTREME_ODDS_HIGH = 0.85
FLAG_THIN_DEPTH_THRESHOLD = 1_000
FLAG_RATIO_OUTLIER_THRESHOLD = 20

# Gate thresholds
GATE_GO_PCT = 25.0
GATE_KILL_CLEAN_GO_PCT = 15.0
GATE_KILL_NO_DATA_PCT = 50.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Phase 0 cost analysis CLI — re-run analysis on live data"
    )
    parser.add_argument(
        "--sport",
        default=None,
        help="Filter by sport (optional, default: all sports)",
    )
    parser.add_argument(
        "--min-volume",
        type=float,
        default=5000.0,
        help="Minimum 24h volume filter (default: 5000)",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        metavar="N",
        help="Analyze only first N markets after filtering",
    )
    parser.add_argument(
        "--output",
        default="phase0_results.csv",
        help="Output CSV path (default: phase0_results.csv)",
    )
    parser.add_argument(
        "--compare-to",
        default=None,
        metavar="PATH",
        help="Existing CSV to compare verdict counts against",
    )
    parser.add_argument(
        "--config",
        default="config/settings.yaml",
        help="Path to settings YAML (default: config/settings.yaml)",
    )
    return parser.parse_args()


def load_config(config_path: str) -> dict:
    with open(config_path) as fh:
        return yaml.safe_load(fh)


def compute_flags(result: dict) -> list[str]:
    """Compute flag list for a single market result."""
    flags = []
    volume = result.get("volume_24h", 0) or 0
    mid_price = result.get("best_bid", 0)
    if result.get("best_bid") and result.get("best_ask"):
        mid_price = (result["best_bid"] + result["best_ask"]) / 2

    bid_depth = result.get("bid_depth", 0) or 0
    ask_depth = result.get("ask_depth", 0) or 0
    ratio_24h = result.get("ratio_24h")

    if volume < FLAG_LOW_VOL_THRESHOLD:
        flags.append("LOW_VOL")
    if mid_price < FLAG_EXTREME_ODDS_LOW or mid_price > FLAG_EXTREME_ODDS_HIGH:
        flags.append("EXTREME_ODDS")
    if min(bid_depth, ask_depth) < FLAG_THIN_DEPTH_THRESHOLD:
        flags.append("THIN_DEPTH")
    if ratio_24h is not None and ratio_24h > FLAG_RATIO_OUTLIER_THRESHOLD:
        flags.append("RATIO_OUTLIER")

    return flags


def load_comparison_csv(path: str) -> dict[str, int]:
    """Load existing CSV and return verdict counts."""
    counts = {"GO": 0, "MARGINAL": 0, "NO_GO": 0}
    try:
        with open(path, newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                verdict = row.get("verdict", "")
                if verdict in counts:
                    counts[verdict] += 1
    except FileNotFoundError:
        logger.error(f"Comparison file not found: {path}")
    return counts


def save_csv(results: list[dict], output_path: str) -> None:
    """Save results to CSV with exact column order."""
    output_dir = os.path.dirname(output_path)
    if output_dir:
        Path(output_dir).mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    logger.info(f"Results saved to {output_path}")


def print_gate_decision(results: list[dict]) -> None:
    """Print Phase 0 gate decision and exit with appropriate code."""
    total = len(results)
    data_markets = [r for r in results if r.get("ratio_24h") is not None]
    data_count = len(data_markets)
    no_data_count = total - data_count

    # A "clean GO" market has verdict=GO and no flags
    clean_go_markets = [
        r for r in data_markets
        if r.get("verdict") == "GO" and len(compute_flags(r)) == 0
    ]
    clean_go_count = len(clean_go_markets)

    clean_go_pct = (clean_go_count / data_count * 100) if data_count > 0 else 0.0
    no_data_pct = (no_data_count / total * 100) if total > 0 else 0.0

    print()
    print("=== PHASE 0 GATE ===")
    print(f"Total markets analyzed: {total}")
    print(f"Markets with data: {data_count}")
    print(f"Clean GO markets (no flags): {clean_go_count}")
    print(f"Clean GO ratio: {clean_go_pct:.1f}%")
    print("Gate threshold: 25% clean GO")

    if total == 0:
        print("Decision: INSUFFICIENT_DATA")
        return

    # Kill conditions
    if no_data_pct > GATE_KILL_NO_DATA_PCT:
        print(f"KILL: data quality insufficient ({no_data_pct:.1f}% no-data markets)")
        sys.exit(1)

    if data_count > 0 and clean_go_pct < GATE_KILL_CLEAN_GO_PCT:
        print(f"KILL: edge space insufficient ({clean_go_pct:.1f}% clean GO < 15% threshold)")
        sys.exit(1)

    if data_count == 0:
        print("Decision: INSUFFICIENT_DATA")
        return

    if clean_go_pct >= GATE_GO_PCT:
        print("Decision: GO")
        print("GO: proceed to Phase 1 collection")
    else:
        print("Decision: NO_GO")


def print_comparison(old_counts: dict[str, int], new_results: list[dict]) -> None:
    """Print comparison between old and new verdict counts."""
    new_counts = {"GO": 0, "MARGINAL": 0, "NO_GO": 0}
    for r in new_results:
        verdict = r.get("verdict", "")
        if verdict in new_counts:
            new_counts[verdict] += 1

    source_label = "phase0_results.csv"
    print()
    print(f"=== COMPARISON vs {source_label} ===")
    for verdict in ["GO", "MARGINAL", "NO_GO"]:
        old_val = old_counts[verdict]
        new_val = new_counts[verdict]
        delta = new_val - old_val
        delta_str = f"+{delta}" if delta >= 0 else str(delta)
        print(f"{verdict:<10} old={old_val:<4} new={new_val:<4} delta={delta_str}")


async def fetch_market_data(
    rest: RestClient,
    market: dict,
) -> tuple[Optional[dict], Optional[float], Optional[list]]:
    """Fetch orderbook, fee_rate, and price_history for a single market."""
    token_yes = market.get("token_id_yes")
    if not token_yes:
        return None, None, None

    orderbook = await rest.get_orderbook(token_yes)
    fee_rate = await rest.get_fee_rate(token_yes) if orderbook else None
    price_history = await rest.get_price_history(token_yes) if orderbook else None

    return orderbook, fee_rate, price_history


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    args = parse_args()

    # 1. Load config
    logger.info(f"Loading config from {args.config}")
    config = load_config(args.config)

    # 2. Create RestClient and MarketDiscovery
    rest = RestClient(config)
    await rest.start()

    try:
        discovery = MarketDiscovery(rest, config)

        # 3. Discover all sports markets
        logger.info("Discovering sports markets...")
        all_markets = await discovery.discover_all_sports_markets()

        # Apply is_sports_market + volume filter
        markets = [
            m for m in all_markets
            if discovery.is_sports_market(m) and m.get("volume_24h", 0) >= args.min_volume
        ]
        logger.info(
            f"After sports + volume >= {args.min_volume} filter: {len(markets)} markets"
        )

        # 4. Optionally filter by --sport
        if args.sport:
            sport_lower = args.sport.lower()
            markets = [m for m in markets if (m.get("sport") or "").lower() == sport_lower]
            logger.info(f"After --sport={args.sport} filter: {len(markets)} markets")

        # 5. Optionally take first N markets
        if args.sample is not None:
            markets = markets[: args.sample]
            logger.info(f"Sampling first {args.sample} markets: {len(markets)} total")

        if not markets:
            logger.warning("No markets to analyze after filtering.")
            print("Decision: INSUFFICIENT_DATA")
            return

        # 6 & 7. Fetch data and analyze each market
        results = []
        for i, market in enumerate(markets, 1):
            slug = market.get("slug", market.get("id", "?"))
            logger.info(f"[{i}/{len(markets)}] Analyzing {slug} ...")

            orderbook, fee_rate, price_history = await fetch_market_data(rest, market)

            if orderbook is None:
                # Build a no-data result row
                result = {
                    "market_id": market.get("id", ""),
                    "slug": market.get("slug", ""),
                    "sport": market.get("sport", ""),
                    "league": market.get("league", ""),
                    "event_start": market.get("event_start"),
                    "best_bid": None,
                    "best_ask": None,
                    "spread": None,
                    "spread_pct": None,
                    "bid_depth": None,
                    "ask_depth": None,
                    "volume_24h": market.get("volume_24h", 0),
                    "fee_rate": None,
                    "taker_rt_cost": None,
                    "maker_rt_cost": None,
                    "move_1h": None,
                    "move_6h": None,
                    "move_24h": None,
                    "move_48h": None,
                    "move_72h": None,
                    "ratio_24h": None,
                    "ratio_48h": None,
                    "verdict": "NO_DATA",
                }
            else:
                result = analyze_market(market, orderbook, fee_rate, price_history, config)

            results.append(result)

        logger.info(f"Analysis complete: {len(results)} markets processed")

        # 8 & 9. Save CSV
        save_csv(results, args.output)

        # 10. Print summary
        verdict_counts: dict[str, int] = {}
        for r in results:
            v = r.get("verdict", "NO_DATA")
            verdict_counts[v] = verdict_counts.get(v, 0) + 1

        print()
        print("=== PHASE 0 SUMMARY ===")
        for verdict, count in sorted(verdict_counts.items()):
            pct = count / len(results) * 100
            print(f"  {verdict:<10} {count:>4}  ({pct:.1f}%)")

        data_results = [r for r in results if r.get("ratio_24h") is not None]
        if data_results:
            ratios = [r["ratio_24h"] for r in data_results]
            avg_ratio = sum(ratios) / len(ratios)
            min_ratio = min(ratios)
            max_ratio = max(ratios)
            print(f"\nRatio stats (ratio_24h) over {len(data_results)} markets with data:")
            print(f"  avg={avg_ratio:.2f}  min={min_ratio:.2f}  max={max_ratio:.2f}")

        # Gate decision
        print_gate_decision(results)

        # Compare-to output
        if args.compare_to:
            old_counts = load_comparison_csv(args.compare_to)
            print_comparison(old_counts, results)

    finally:
        await rest.close()


if __name__ == "__main__":
    asyncio.run(main())
