"""
Фаза 1 — Data Collection (24/7)
Безперервний збір snapshot'ів цін і трейдів зі спортивних ринків Polymarket.

Запуск: python main.py
Зупинити: Ctrl+C
Фоновий режим: nohup python main.py > logs/stdout.log 2>&1 &
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path
from datetime import datetime, timezone

import yaml

from collector.rest_client import RestClient
from collector.ws_client import WsClient
from collector.market_discovery import MarketDiscovery
from collector.normalizer import normalize_snapshot, compute_time_to_event
from db.repository import Repository
from alerts.logger_alert import LoggerAlert


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
        """Запустити collector"""
        await self.rest.start()
        self.repo = await Repository.create(self.config)

        # Знайти ринки
        await self._discover_markets()

        if not self.active_markets:
            logger.error("Немає ринків для збору. Спочатку запусти cost_analyzer.py")
            return

        # Налаштувати WebSocket
        self.ws = WsClient(self.config, on_trade=self._on_trade, on_spike=self._on_spike)
        for mid, info in self.active_markets.items():
            token_yes = info.get("token_id_yes")
            token_no = info.get("token_id_no")
            if token_yes:
                self.ws.add_market(token_yes, mid, "yes")
            if token_no:
                self.ws.add_market(token_no, mid, "no")

        await self.alert.collector_started(len(self.active_markets))

        self._running = True

        # Запустити паралельні задачі
        await asyncio.gather(
            self._snapshot_loop(),
            self._market_rescan_loop(),
            self._heartbeat_loop(),
            self.ws.start(),
        )

    async def stop(self):
        """Зупинити collector"""
        self._running = False
        if self.ws:
            await self.ws.stop()
        await self.rest.close()
        if self.repo:
            await self.repo.close()
        logger.info("Collector зупинено")

    async def _discover_markets(self):
        """Знайти і відфільтрувати ринки"""
        discovery = MarketDiscovery(self.rest, self.config)
        all_markets = await discovery.discover_all_sports_markets()

        # Pre-filter by volume + sports before fetching orderbooks (avoids 8h scan)
        candidates = discovery.filter_for_phase0(all_markets)

        # Зібрати orderbook тільки для candidates
        orderbooks = {}
        for m in candidates:
            token_id = m.get("token_id_yes")
            if token_id:
                ob = await self.rest.get_orderbook(token_id)
                if ob:
                    orderbooks[m["id"]] = ob

        # Фільтр Phase 1
        filtered = discovery.filter_for_phase1(candidates, orderbooks)

        # Зберегти в БД і в пам'ять
        for m in filtered:
            await self.repo.upsert_market(m)
            self.active_markets[m["id"]] = m

        logger.info(f"Активних ринків для збору: {len(self.active_markets)}")

    async def _snapshot_loop(self):
        """Цикл збору snapshot'ів кожні N секунд"""
        interval = self.config["phase1"]["snapshot_interval_sec"]
        gap_threshold = self.config["collector"]["gap_threshold_minutes"]

        while self._running:
            now = datetime.now(timezone.utc)

            for mid, info in list(self.active_markets.items()):
                # Пропустити events що вже почались
                tte = compute_time_to_event(info["event_start"])
                if tte < 0:
                    await self.repo.update_market_status(mid, "settled")
                    await self.alert.market_settled(mid, info.get("slug", ""))
                    del self.active_markets[mid]
                    continue

                token_id = info.get("token_id_yes")
                if not token_id:
                    continue

                # Перевірити gap
                last_snap = self._last_snapshots.get(mid)
                if last_snap:
                    gap_min = (now - last_snap).total_seconds() / 60
                    if gap_min > gap_threshold:
                        await self.alert.gap_detected(mid, gap_min)
                        await self.repo.insert_gap(mid, last_snap, "snapshot_delay")

                # Зібрати orderbook
                orderbook = await self.rest.get_orderbook(token_id)
                if not orderbook:
                    continue

                # Додати volume з market info (REST не завжди віддає)
                orderbook["volume_24h"] = info.get("volume_24h", 0)

                # Нормалізувати і зберегти
                snapshot = normalize_snapshot(mid, orderbook, info["event_start"])
                await self.repo.insert_snapshot(snapshot)
                self._last_snapshots[mid] = now

                if orderbook.get("mid_price") and orderbook.get("spread"):
                    await self.alert.snapshot_saved(
                        mid, orderbook["mid_price"], orderbook["spread"]
                    )

            # Чекати до наступного циклу
            elapsed = (datetime.now(timezone.utc) - now).total_seconds()
            sleep_time = max(0, interval - elapsed)
            await asyncio.sleep(sleep_time)

    async def _market_rescan_loop(self):
        """Пересканувати ринки кожну годину"""
        interval = self.config["phase1"]["market_rescan_interval_sec"]

        while self._running:
            await asyncio.sleep(interval)
            logger.info("Пересканування ринків...")
            try:
                await self._discover_markets()

                # Оновити WS підписки
                if self.ws:
                    for mid, info in self.active_markets.items():
                        token_yes = info.get("token_id_yes")
                        if token_yes and token_yes not in self.ws._subscribed_markets:
                            self.ws.add_market(token_yes, mid, "yes")

            except Exception as e:
                logger.error(f"Помилка пересканування: {e}")

    async def _heartbeat_loop(self):
        """Heartbeat — статус кожні 60 секунд"""
        interval = self.config["collector"]["heartbeat_interval"]

        while self._running:
            await asyncio.sleep(interval)
            active = len(self.active_markets)
            snaps = len(self._last_snapshots)
            logger.info(f"♥ Heartbeat: {active} ринків, {snaps} з даними")

    async def _on_trade(self, trade: dict):
        """Callback для WebSocket трейдів"""
        await self.repo.insert_trade(trade)
        await self.alert.trade_saved(
            trade["market_id"],
            trade["price"],
            trade["size"],
            trade["side"],
        )

    async def _on_spike(self, event: dict):
        """Callback for real-time spike events detected by SpikeTracker"""
        await self.repo.insert_spike_event(event)
        logger.info(
            f"SPIKE {event['direction'].upper()} {event['market_id']} "
            f"magnitude={event['magnitude']:.4f} steps={event['n_steps']}"
        )


async def main():
    config_path = Path("config/settings.yaml")
    if not config_path.exists():
        print("ERROR: config/settings.yaml не знайдено.")
        print("Скопіюй config/settings.example.yaml → config/settings.yaml")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    collector = Collector(config)

    # Graceful shutdown
    loop = asyncio.get_event_loop()

    def shutdown_handler():
        logger.info("Отримано сигнал зупинки...")
        asyncio.ensure_future(collector.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_handler)
        except NotImplementedError:
            pass  # Windows

    try:
        await collector.start()
    except KeyboardInterrupt:
        await collector.stop()


if __name__ == "__main__":
    Path("logs").mkdir(exist_ok=True)
    asyncio.run(main())
