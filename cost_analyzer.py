"""
Phase 0 — Cost Analysis
Scans all Polymarket sports markets, computes trading costs, and emits a verdict table.

Usage: python cost_analyzer.py
Resume after failure: python cost_analyzer.py  (automatic — checkpoint is preserved)
Force full rescan: python cost_analyzer.py --fresh
Output: phase0_results.csv + console table + DB records
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

import pandas as pd
import yaml
from tabulate import tabulate

from alerts.logger_alert import LoggerAlert
from analytics.cost_analyzer import aggregate_by_league, analyze_market
from analytics.cost_backfill import run as run_backfill
from collector.market_discovery import MarketDiscovery
from collector.rest_client import RestClient
from config.validate import validate_config
from db.repository import Repository

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/phase0.log", mode="a"),
    ],
)
logger = logging.getLogger(__name__)

CHECKPOINT_FILE = Path(".phase0_checkpoint.json")


def _load_checkpoint() -> set[str]:
    """Return set of market IDs already processed in a previous run."""
    if not CHECKPOINT_FILE.exists():
        return set()
    try:
        data = json.loads(CHECKPOINT_FILE.read_text())
        ids = set(data.get("processed_ids", []))
        logger.info(f"Checkpoint: resuming — {len(ids)} markets already processed")
        return ids
    except (json.JSONDecodeError, KeyError):
        logger.warning("Checkpoint file is corrupt; starting fresh")
        return set()


def _save_checkpoint(processed_ids: set[str]) -> None:
    """Atomically persist processed market IDs to the checkpoint file."""
    tmp = CHECKPOINT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"processed_ids": list(processed_ids)}))
    tmp.rename(CHECKPOINT_FILE)


def _clear_checkpoint() -> None:
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()


async def run_phase0(fresh: bool = False, backfill: bool = False):
    # Load config
    config_path = Path("config/settings.yaml")
    if not config_path.exists():
        print("ERROR: config/settings.yaml not found.")
        print("Copy config/settings.example.yaml → config/settings.yaml")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    validate_config(config)

    # Init
    rest = RestClient(config)
    await rest.start()
    discovery = MarketDiscovery(rest, config)
    repo = await Repository.create(config)
    alert = LoggerAlert(config)

    try:
        # 0. Checkpoint
        if fresh:
            _clear_checkpoint()
            logger.info("--fresh: ignoring any previous checkpoint")
        processed_ids = _load_checkpoint()

        # 1. Discover all sports markets
        logger.info("Step 1: Discovering sports markets...")
        all_markets = await discovery.discover_all_sports_markets()

        if not all_markets:
            logger.error("No sports markets found")
            return

        # 2. Filter by volume
        markets = discovery.filter_for_phase0(all_markets)

        if not markets:
            logger.error("No markets passed the volume filter")
            return

        # 3. Save markets to DB
        logger.info("Step 2: Saving markets to DB...")
        for m in markets:
            try:
                await repo.upsert_market(m)
            except Exception as e:
                logger.error(f"upsert_market failed for {m.get('slug')}: {e}")

        # 4. Fetch orderbook + fees + history for each market
        pending = [m for m in markets if m.get("id") not in processed_ids]
        skipped = len(markets) - len(pending)
        if skipped:
            logger.info(f"Checkpoint: skipping {skipped} already-processed markets")

        logger.info(f"Step 3: Collecting orderbooks for {len(pending)} markets...")
        results = []

        for i, market in enumerate(pending):
            token_id = market.get("token_id_yes")
            if not token_id:
                processed_ids.add(market["id"])
                _save_checkpoint(processed_ids)
                continue

            logger.info(
                f"  [{i+1}/{len(pending)}] {market['slug'][:50]}..."
            )

            # Orderbook
            orderbook = await rest.get_orderbook(token_id)
            if not orderbook:
                logger.warning("    No orderbook available, skipping")
                processed_ids.add(market["id"])
                _save_checkpoint(processed_ids)
                continue

            # Fee rate
            fee_rate = await rest.get_fee_rate(token_id)

            # Price history
            history = await rest.get_price_history(token_id)

            if i < 3 and not skipped:  # debug first 3 (only on a fresh run, not resume)
                logger.info(f"    DEBUG orderbook: bid={orderbook.get('best_bid')} ask={orderbook.get('best_ask')} mid={orderbook.get('mid_price')}")  # noqa: E501
                logger.info(f"    DEBUG fee_rate: {fee_rate}")
                logger.info(f"    DEBUG history points: {len(history) if history else 0}")
                if history and len(history) > 0:
                    logger.info(f"    DEBUG first point: {history[0]}")
                    logger.info(f"    DEBUG last point: {history[-1]}")

            # Analyse
            result = analyze_market(market, orderbook, fee_rate, history, config)
            results.append(result)

            # Save to DB, then update checkpoint
            try:
                await repo.insert_cost_analysis(result)
            except Exception as e:
                logger.error(f"insert_cost_analysis failed for {market.get('slug')}: {e}")

            processed_ids.add(market["id"])
            _save_checkpoint(processed_ids)

        if not results:
            logger.error("No markets were analysed")
            return

        # 5. Print table
        logger.info(f"\nStep 4: Results ({len(results)} markets)\n")

        df = pd.DataFrame(results)

        # Main table — sorted by ratio
        display_cols = [
            "slug", "sport", "league", "best_bid", "best_ask",
            "spread_pct", "bid_depth", "ask_depth", "volume_24h",
            "fee_rate", "taker_rt_cost", "move_24h", "ratio_24h", "verdict",
        ]
        df_display = df[display_cols].copy()
        df_display["slug"] = df_display["slug"].str[:40]
        df_display = df_display.sort_values("ratio_24h", ascending=False, na_position="last")

        print("\n" + "=" * 100)
        print("  PHASE 0: COST ANALYSIS — RESULTS BY MARKET")
        print("=" * 100)
        print(tabulate(df_display, headers="keys", tablefmt="simple", showindex=False, floatfmt=".4f"))

        # 6. Aggregate by league
        league_summary = aggregate_by_league(results)

        print("\n" + "=" * 100)
        print("  AGGREGATION BY LEAGUE")
        print("=" * 100)
        if league_summary:
            df_leagues = pd.DataFrame(league_summary)
            print(tabulate(df_leagues, headers="keys", tablefmt="simple", showindex=False, floatfmt=".2f"))
        else:
            print("  No data to aggregate")

        # 7. Overall verdict
        go = sum(1 for r in results if r["verdict"] == "GO")
        marginal = sum(1 for r in results if r["verdict"] == "MARGINAL")
        no_go = sum(1 for r in results if r["verdict"] == "NO_GO")
        no_data = sum(1 for r in results if r["verdict"] == "NO_DATA")

        print("\n" + "=" * 100)
        print("  OVERALL VERDICT")
        print(f"  GO: {go}  |  MARGINAL: {marginal}  |  NO_GO: {no_go}  |  NO_DATA: {no_data}")

        if go > 0:
            print(f"\n  ✓ {go} markets with ratio > 2.0 — proceed to Phase 1")
        elif marginal > 0:
            print(f"\n  ⚠ {marginal} markets in the grey zone (ratio 1.5–2.0)")
            print("    Maker strategy may work. Taker — marginal.")
        else:
            print("\n  ✗ Edge not viable under current fee structure.")
            print("    Recommendation: STOP or wait for fee changes.")
        print("=" * 100)

        # 8. Save CSV
        output_file = config["phase0"]["output_file"]
        df.to_csv(output_file, index=False)
        logger.info(f"\nResults saved to: {output_file}")

        await alert.phase0_complete(len(results), go, marginal, no_go)

        # Scan completed cleanly — remove checkpoint
        _clear_checkpoint()
        logger.info("Checkpoint cleared (scan complete)")

        # Optional: backfill cost_estimates from CSV + snapshots
        if backfill:
            logger.info("Running cost backfill...")
            await run_backfill(config)

    finally:
        await rest.close()
        await repo.close()


if __name__ == "__main__":
    import argparse

    Path("logs").mkdir(exist_ok=True)

    parser = argparse.ArgumentParser(description="Phase 0 — Cost Analysis")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore checkpoint and scan all markets from scratch",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="After scan, import phase0_results.csv into cost_estimates and compute costs from snapshots",
    )
    args = parser.parse_args()

    asyncio.run(run_phase0(fresh=args.fresh, backfill=args.backfill))
