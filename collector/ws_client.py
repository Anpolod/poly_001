"""WebSocket client for real-time data collection (Phase 1)"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

import websockets

from collector.spike_tracker import SpikeTracker

logger = logging.getLogger(__name__)

# Flush buffer when it reaches this many items
_BUFFER_FLUSH_SIZE = 10

# Flush buffer at least every this many seconds
_BUFFER_FLUSH_INTERVAL = 30.0


class WsClient:
    """WebSocket client for real-time Polymarket market data with exponential-backoff reconnection."""

    def __init__(
        self,
        config: dict,
        on_trade: Optional[Callable] = None,
        on_gap: Optional[Callable] = None,
        on_spike: Optional[Callable] = None,
        on_reconnect: Optional[Callable] = None,
    ):
        coll = config["collector"]
        self.ws_url = config["api"]["ws_url"]
        self.base_delay = coll["ws_reconnect_base_delay"]
        self.max_delay = coll["ws_reconnect_max_delay"]
        self.backoff = coll["ws_reconnect_backoff"]
        self._silence_threshold: float = float(coll.get("ws_silence_threshold_sec", 60.0))
        self._heartbeat_interval: float = float(coll.get("heartbeat_interval", 60.0))
        self._gap_threshold_sec: float = float(coll.get("gap_threshold_minutes", 5)) * 60.0
        self.on_trade = on_trade
        self.on_gap = on_gap          # optional callable(downtime_seconds: float)
        self.on_spike = on_spike      # optional callable(spike_event: dict)
        self.on_reconnect = on_reconnect  # optional callable(attempt: int, delay: float)
        self._spike_detection: bool = coll.get("spike_detection_realtime", False)
        self._ws = None
        self._subscribed_markets: dict[str, dict] = {}  # token_id -> market info
        self._spike_trackers: dict[str, SpikeTracker] = {}  # market_id -> tracker
        self._running = False

        # Timestamp of the last received WS message (used for REST fallback)
        self._last_message_ts: Optional[float] = None

        # In-memory snapshot buffer
        self._buffer: list[dict] = []
        self._flush_callback: Optional[Callable] = None
        self._last_flush_ts: float = 0.0

        # Message counter for heartbeat logging
        self._snapshot_count: int = 0

        # Track when a connection was lost to compute downtime
        self._disconnect_ts: Optional[float] = None

        # Consecutive reconnect attempt counter (reset to 0 on successful connect)
        self._reconnect_attempt: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_market(self, token_id: str, market_id: str, side: str = "yes"):
        """Register a market for WebSocket subscription."""
        self._subscribed_markets[token_id] = {
            "market_id": market_id,
            "side": side,
        }
        if self._spike_detection and market_id not in self._spike_trackers:
            self._spike_trackers[market_id] = SpikeTracker(market_id)

    def remove_market(self, token_id: str):
        """Unsubscribe and remove a market."""
        info = self._subscribed_markets.pop(token_id, None)
        if info:
            self._spike_trackers.pop(info["market_id"], None)

    def set_flush_callback(self, fn: Callable):
        """
        Register a callback that receives the buffered snapshot list when flushed.

        The callback signature is: fn(buffer: list[dict]) -> None (or coroutine).
        The buffer is flushed either when it reaches _BUFFER_FLUSH_SIZE items or
        every _BUFFER_FLUSH_INTERVAL seconds, whichever comes first.
        """
        self._flush_callback = fn

    async def start(self):
        """Start the WebSocket data collection loop with exponential backoff reconnection."""
        self._running = True
        self._last_flush_ts = asyncio.get_event_loop().time()
        delay = self.base_delay

        # Start background tasks
        heartbeat_task = asyncio.ensure_future(self._heartbeat())
        flush_task = asyncio.ensure_future(self._flush_loop())

        while self._running:
            connect_ts = asyncio.get_event_loop().time()
            try:
                # If we had a prior disconnect, compute downtime
                if self._disconnect_ts is not None:
                    downtime = connect_ts - self._disconnect_ts
                    if downtime > self._gap_threshold_sec:
                        logger.warning(
                            f"WS reconnect after {downtime:.0f}s downtime (gap > {self._gap_threshold_sec:.0f}s)"
                        )
                        if self.on_gap is not None:
                            try:
                                result = self.on_gap(downtime)
                                if asyncio.iscoroutine(result):
                                    await result
                            except Exception as gap_err:
                                logger.error(f"on_gap callback error: {gap_err}")
                    self._disconnect_ts = None

                async with websockets.connect(
                    self.ws_url,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    delay = self.base_delay  # reset delay on success
                    self._reconnect_attempt = 0
                    self._last_message_ts = asyncio.get_event_loop().time()
                    logger.info("WebSocket connected")

                    # Subscribe to all active markets
                    if self._subscribed_markets:
                        await self._subscribe(ws)

                    async for msg in ws:
                        await self._handle_message(msg)

            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"WS closed: {e}")
            except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                logger.warning(f"WS error: {e}")
            except Exception as e:
                logger.error(f"WS unexpected: {e}")

            # Record disconnect time for gap detection on next connect
            self._disconnect_ts = asyncio.get_event_loop().time()
            self._ws = None

            if self._running:
                self._reconnect_attempt += 1
                logger.info(f"Reconnecting in {delay}s")
                if self.on_reconnect is not None:
                    try:
                        result = self.on_reconnect(self._reconnect_attempt, delay)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as rc_err:
                        logger.error(f"on_reconnect callback error: {rc_err}")
                await asyncio.sleep(delay)
                delay = min(delay * self.backoff, self.max_delay)

        # Clean up background tasks
        heartbeat_task.cancel()
        flush_task.cancel()
        for t in (heartbeat_task, flush_task):
            try:
                await t
            except asyncio.CancelledError:
                pass

    async def stop(self):
        """
        Gracefully stop the WebSocket client.

        Sets _running to False, flushes any buffered snapshots, and closes
        the active WebSocket connection.  Safe to call from a SIGTERM handler.
        """
        self._running = False
        await self._flush_buffer()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # REST fallback
    # ------------------------------------------------------------------

    async def _rest_fallback_poll(self, rest_client, snapshot_callback: Callable):
        """
        Poll orderbooks via REST for all active markets when the WebSocket has
        been silent for longer than _WS_SILENCE_THRESHOLD seconds.

        Parameters
        ----------
        rest_client:
            An instance of RestClient that exposes get_orderbook(token_id).
        snapshot_callback:
            Async (or sync) callable with signature: callback(orderbook, token_id).
            Called once per market with the fetched orderbook dict.
        """
        now = asyncio.get_event_loop().time()
        if self._last_message_ts is not None:
            silence = now - self._last_message_ts
            if silence < self._silence_threshold:
                return  # WS is still active

        logger.warning(
            f"WS silent for >{self._silence_threshold:.0f}s — falling back to REST polling"
        )

        for token_id in list(self._subscribed_markets.keys()):
            try:
                orderbook = await rest_client.get_orderbook(token_id)
                if orderbook:
                    result = snapshot_callback(orderbook, token_id)
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as e:
                logger.error(f"REST fallback error for token {token_id}: {e}")

    # ------------------------------------------------------------------
    # Buffer management
    # ------------------------------------------------------------------

    def _buffer_snapshot(self, snapshot: dict):
        """
        Add a snapshot dict to the in-memory buffer and flush if the size
        threshold (_BUFFER_FLUSH_SIZE) has been reached.
        """
        self._buffer.append(snapshot)
        if len(self._buffer) >= _BUFFER_FLUSH_SIZE:
            asyncio.ensure_future(self._flush_buffer())

    async def _flush_buffer(self):
        """
        Flush the current buffer by invoking the registered flush_callback.

        If no callback has been registered the buffer is simply cleared.
        """
        if not self._buffer:
            return

        batch = self._buffer[:]
        self._buffer = []
        self._last_flush_ts = asyncio.get_event_loop().time()

        if self._flush_callback is not None:
            try:
                result = self._flush_callback(batch)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"flush_callback error: {e}")

    async def _flush_loop(self):
        """
        Background coroutine that flushes the buffer every _BUFFER_FLUSH_INTERVAL
        seconds regardless of how many items have accumulated.
        """
        while self._running:
            await asyncio.sleep(_BUFFER_FLUSH_INTERVAL)
            elapsed = asyncio.get_event_loop().time() - self._last_flush_ts
            if elapsed >= _BUFFER_FLUSH_INTERVAL and self._buffer:
                await self._flush_buffer()

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat(self):
        """
        Background coroutine that logs a liveness message every
        heartbeat_interval seconds, including the total number of WS messages
        received since the client was started.
        """
        while self._running:
            await asyncio.sleep(self._heartbeat_interval)
            logger.info(f"WS alive: {self._snapshot_count} messages received")

    # ------------------------------------------------------------------
    # Internal helpers (kept from original)
    # ------------------------------------------------------------------

    async def _subscribe(self, ws):
        """Send a subscription request for all active markets."""
        token_ids = list(self._subscribed_markets.keys())
        if not token_ids:
            return

        # Polymarket WS subscription message
        sub_msg = {
            "type": "subscribe",
            "channel": "market",
            "assets_ids": token_ids,
        }
        await ws.send(json.dumps(sub_msg))
        logger.info(f"Subscribed to {len(token_ids)} tokens")

    async def _handle_message(self, raw_msg: str):
        """Parse and dispatch an incoming WebSocket message."""
        # Update liveness tracking
        self._last_message_ts = asyncio.get_event_loop().time()
        self._snapshot_count += 1

        try:
            data = json.loads(raw_msg)
        except json.JSONDecodeError:
            return

        # Initial subscribe response is a list of orderbook snapshots — skip
        if isinstance(data, list):
            return

        msg_type = data.get("type", "")

        if msg_type == "trade":
            await self._handle_trade(data)
        elif msg_type == "book":
            pass  # orderbook updates — can be added later
        elif msg_type == "price_change":
            pass
        elif "price_changes" in data:
            # Polymarket sends {"market": ..., "price_changes": [...]} format
            for change in data.get("price_changes", []):
                await self._handle_trade(change)

    async def _handle_trade(self, data: dict):
        """Process a single trade event and invoke the on_trade callback."""
        asset_id = data.get("asset_id", "")
        market_info = self._subscribed_markets.get(asset_id)

        if not market_info:
            return

        ts = datetime.now(timezone.utc)
        market_id = market_info["market_id"]
        price = float(data.get("price", 0))

        trade = {
            "ts": ts,
            "market_id": market_id,
            "trade_id": data.get("id", str(ts.timestamp())),
            "price": price,
            "size": float(data.get("size", 0)),
            "side": data.get("side", "unknown"),
        }

        if self.on_trade:
            await self.on_trade(trade)

        # Spike detection — only when enabled and price is valid
        if self._spike_detection and price > 0 and self.on_spike:
            tracker = self._spike_trackers.get(market_id)
            if tracker:
                event = tracker.update(price, ts)
                if event and event["type"] == "spike_finalized":
                    try:
                        result = self.on_spike(event)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as e:
                        logger.error(f"on_spike callback error for {market_id}: {e}")
