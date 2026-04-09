"""
Phase 1 — Data Collection (24/7)
Continuous collection of price snapshots and trades from Polymarket sports markets.

Usage: python main.py
Stop: Ctrl+C
Background mode: nohup python main.py > logs/stdout.log 2>&1 &
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from alerts.logger_alert import LoggerAlert
from collector.market_discovery import MarketDiscovery
from collector.normalizer import compute_time_to_event, normalize_snapshot
from collector.rest_client import RestClient
from collector.ws_client import WsClient
from config.validate import validate_config
from db.repository import Repository

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/collector.log", mode="a"),
    ],
)
logger = logging.getLogger(__name__)


class Collector:
    """Orchestrates Phase 1 data collection: snapshot loop, WebSocket trades, market rescan, and alerting."""

    def __init__(self, config: dict):
        self.config = config
        self.rest = RestClient(config)
        self.repo: Repository = None
        self.alert = LoggerAlert(config)
        self.ws: WsClient = None
        self.active_markets: dict[str, dict] = {}  # market_id -> market info
        self._running = False
        self._last_snapshots: dict[str, datetime] = {}  # market_id -> last snapshot ts

    async def start(self):
        """Start the collector: discover markets, configure WebSocket, and run collection loops."""
        await self.rest.start()
        self.repo = await Repository.create(self.config)

        # Discover markets
        await self._discover_markets()

        if not self.active_markets:
            logger.error("No markets to collect. Run cost_analyzer.py first.")
            return

        # Configure WebSocket
        self.ws = WsClient(
            self.config,
            on_trade=self._on_trade,
            on_spike=self._on_spike,
            on_reconnect=self._on_ws_reconnect,
        )
        for mid, info in self.active_markets.items():
            token_yes = info.get("token_id_yes")
            token_no = info.get("token_id_no")
            if token_yes:
                self.ws.add_market(token_yes, mid, "yes")
            if token_no:
                self.ws.add_market(token_no, mid, "no")

        await self.alert.collector_started(len(self.active_markets))

        self._running = True

        # Launch parallel tasks — wrap each so exceptions don't kill the group
        tasks = [
            asyncio.create_task(self._snapshot_loop(), name="snapshot"),
            asyncio.create_task(self._market_rescan_loop(), name="rescan"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self.ws.start(), name="websocket"),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

    async def stop(self):
        """Gracefully stop the collector and close all connections."""
        if not self._running:
            return
        self._running = False
        logger.info("Stopping collector...")
        # Give loops a moment to notice _running=False and exit
        await asyncio.sleep(1)
        if self.ws:
            await self.ws.stop()
        await self.rest.close()
        if self.repo:
            await self.repo.close()
        logger.info("Collector stopped")

    async def _discover_markets(self):
        """Discover, filter, and upsert tradeable markets; settle any that have ended."""
        discovery = MarketDiscovery(self.rest, self.config)
        all_markets = await discovery.discover_all_sports_markets()

        # Pre-filter by volume + sports before fetching orderbooks (avoids 8h scan)
        candidates = discovery.filter_for_phase0(all_markets)

        # Fetch orderbooks only for candidates (avoids scanning every market)
        orderbooks = {}
        for m in candidates:
            token_id = m.get("token_id_yes")
            if token_id:
                ob = await self.rest.get_orderbook(token_id)
                if ob:
                    orderbooks[m["id"]] = ob

        # Apply Phase 1 filters
        filtered = discovery.filter_for_phase1(candidates, orderbooks)

        # Persist to DB and memory
        for m in filtered:
            await self.repo.upsert_market(m)
            self.active_markets[m["id"]] = m

        # --- T-17: Lifecycle monitoring ---
        # Build set of market IDs currently visible from the API
        api_market_ids = {m["id"] for m in all_markets}
        now = datetime.now(timezone.utc)
        settled_count = 0

        for mid in list(self.active_markets.keys()):
            info = self.active_markets[mid]
            event_start = info.get("event_start")

            # Only settle markets whose event_start is in the past AND absent from API
            past_event = event_start is not None and event_start < now
            missing_from_api = mid not in api_market_ids

            if past_event and missing_from_api:
                logger.info(
                    f"[lifecycle] Settling market {info.get('slug', mid)!r} "
                    f"(event_start={event_start.isoformat()}, no longer in API)"
                )
                await self.repo.update_market_status(mid, "settled")
                await self.alert.market_settled(mid, info.get("slug", ""))
                del self.active_markets[mid]
                settled_count += 1

        if settled_count:
            logger.info(f"[lifecycle] Marked {settled_count} market(s) as settled")

        logger.info(f"Active markets for collection: {len(self.active_markets)}")

    async def _snapshot_loop(self):
        """Collect orderbook snapshots for all active markets every N seconds."""
        interval = self.config["phase1"]["snapshot_interval_sec"]
        gap_threshold = self.config["collector"]["gap_threshold_minutes"]

        while self._running:
            now = datetime.now(timezone.utc)

            for mid, info in list(self.active_markets.items()):
                if not self._running:
                    break

                # Skip events that have already started
                tte = compute_time_to_event(info["event_start"])
                if tte < 0:
                    await self.repo.update_market_status(mid, "settled")
                    await self.alert.market_settled(mid, info.get("slug", ""))
                    del self.active_markets[mid]
                    continue

                token_id = info.get("token_id_yes")
                if not token_id:
                    continue

                # Check for data gap
                last_snap = self._last_snapshots.get(mid)
                if last_snap:
                    gap_min = (now - last_snap).total_seconds() / 60
                    if gap_min > gap_threshold:
                        await self.alert.gap_detected(mid, gap_min)
                        await self.repo.insert_gap(mid, last_snap, "snapshot_delay")

                # Fetch orderbook
                orderbook = await self.rest.get_orderbook(token_id)
                if not orderbook:
                    continue

                # Attach volume from market info (REST doesn't always return it)
                orderbook["volume_24h"] = info.get("volume_24h", 0)

                # Normalise and save
                snapshot = normalize_snapshot(mid, orderbook, info["event_start"])
                await self.repo.insert_snapshot(snapshot)
                self._last_snapshots[mid] = now

                if orderbook.get("mid_price") and orderbook.get("spread"):
                    await self.alert.snapshot_saved(
                        mid, orderbook["mid_price"], orderbook["spread"]
                    )

            # Wait until the next cycle
            elapsed = (datetime.now(timezone.utc) - now).total_seconds()
            sleep_time = max(0, interval - elapsed)
            await asyncio.sleep(sleep_time)

    async def _market_rescan_loop(self):
        """Rescan and refresh active markets on every rescan interval."""
        interval = self.config["phase1"]["market_rescan_interval_sec"]

        while self._running:
            await asyncio.sleep(interval)
            logger.info("Rescanning markets...")
            try:
                await self._discover_markets()

                # Update WS subscriptions for newly discovered markets
                if self.ws:
                    for mid, info in self.active_markets.items():
                        token_yes = info.get("token_id_yes")
                        if token_yes and token_yes not in self.ws._subscribed_markets:
                            self.ws.add_market(token_yes, mid, "yes")

            except Exception as e:
                logger.error(f"Market rescan error: {e}")

    async def _heartbeat_loop(self):
        """Log a liveness status message every heartbeat_interval seconds."""
        interval = self.config["collector"]["heartbeat_interval"]

        while self._running:
            await asyncio.sleep(interval)
            active = len(self.active_markets)
            snaps = len(self._last_snapshots)
            logger.info(f"♥ Heartbeat: {active} markets, {snaps} with snapshots")

    async def _on_trade(self, trade: dict):
        """WebSocket trade callback — persists the trade and fires an alert."""
        await self.repo.insert_trade(trade)
        await self.alert.trade_saved(
            trade["market_id"],
            trade["price"],
            trade["size"],
            trade["side"],
        )

    async def _on_spike(self, event: dict):
        """Callback for real-time spike events detected by SpikeTracker."""
        await self.repo.insert_spike_event(event)
        await self.alert.spike_detected(
            event["market_id"],
            event["direction"],
            event["magnitude"],
            event["n_steps"],
        )

    async def _on_ws_reconnect(self, attempt: int, delay: float):
        """Callback fired each time the WebSocket attempts to reconnect."""
        await self.alert.ws_reconnect(attempt, delay)


async def main():
    config_path = Path("config/settings.yaml")
    if not config_path.exists():
        print("ERROR: config/settings.yaml not found.")
        print("Copy config/settings.example.yaml → config/settings.yaml")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    validate_config(config)
    collector = Collector(config)

    # Graceful shutdown
    loop = asyncio.get_event_loop()

    def shutdown_handler():
        logger.info("Shutdown signal received...")
        asyncio.ensure_future(collector.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_handler)
        except NotImplementedError:
            pass  # Windows

    try:
        await collector.start()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await collector.stop()


if __name__ == "__main__":
    Path("logs").mkdir(exist_ok=True)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
