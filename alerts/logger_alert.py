"""Алерти через логування (консоль + файл)"""

import logging

logger = logging.getLogger("alerts")


class LoggerAlert:
    def __init__(self, config: dict):
        self.config = config

    async def send(self, message: str, level: str = "info"):
        getattr(logger, level, logger.info)(message)

    async def phase0_complete(self, total: int, go: int, marginal: int, no_go: int):
        await self.send(
            f"\n{'='*60}\n"
            f"  ФАЗА 0 ЗАВЕРШЕНА\n"
            f"  Всього ринків: {total}\n"
            f"  GO: {go}  |  MARGINAL: {marginal}  |  NO_GO: {no_go}\n"
            f"{'='*60}"
        )

    async def collector_started(self, market_count: int):
        await self.send(
            f"Collector запущено. Відстежується {market_count} ринків."
        )

    async def snapshot_saved(self, market_id: str, mid_price: float, spread: float):
        await self.send(
            f"Snapshot: {market_id[:16]}... mid={mid_price:.4f} spread={spread:.4f}",
            "debug",
        )

    async def trade_saved(self, market_id: str, price: float, size: float, side: str):
        await self.send(
            f"Trade: {market_id[:16]}... {side} {size:.2f} @ {price:.4f}",
            "debug",
        )

    async def gap_detected(self, market_id: str, minutes: float):
        await self.send(
            f"⚠ GAP: {market_id[:16]}... пропуск {minutes:.1f} хв",
            "warning",
        )

    async def market_settled(self, market_id: str, slug: str):
        await self.send(f"Market settled: {slug}")
