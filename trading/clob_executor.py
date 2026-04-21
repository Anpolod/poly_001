"""
CLOB Executor — wraps py-clob-client for async use.

py-clob-client is synchronous internally (uses requests). We run each call
in a thread-pool executor so we don't block the asyncio event loop.

Usage:
    executor = ClobExecutor(private_key)
    balance = await executor.get_balance()
    order = await executor.buy(token_id, price=0.185, size_usd=15.0)
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams, OrderArgs, OrderType
from py_clob_client.constants import POLYGON

logger = logging.getLogger(__name__)

_CLOB_HOST = "https://clob.polymarket.com"
_CHAIN_ID = POLYGON  # 137


class ClobExecutor:
    """Async wrapper around py-clob-client for placing and managing orders."""

    def __init__(self, private_key: str, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self._client = ClobClient(
            host=_CLOB_HOST,
            key=private_key,
            chain_id=_CHAIN_ID,
            signature_type=0,  # EOA (Externally Owned Account)
        )
        creds = self._client.create_or_derive_api_creds()
        self._client.set_api_creds(creds)
        logger.info("ClobExecutor initialized. Address: %s  dry_run=%s",
                    self._client.get_address(), dry_run)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run(self, fn, *args, **kwargs) -> Any:
        """Run a synchronous py-clob-client call in the default threadpool."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(fn, *args, **kwargs))

    # ------------------------------------------------------------------
    # Read-only
    # ------------------------------------------------------------------

    async def get_balance(self) -> float:
        """Return current USDC balance (collateral) in dollars."""
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        result = await self._run(self._client.get_balance_allowance, params)
        raw = float(result.get("balance", 0))
        # CLOB returns balance in micro-USDC (6 decimals) — convert to dollars
        return raw / 1e6 if raw > 1000 else raw

    async def get_best_bid(self, token_id: str) -> float:
        """Return current best bid price for a token (for exit pricing)."""
        ob = await self._run(self._client.get_order_book, token_id)
        bids = ob.bids if hasattr(ob, "bids") else (ob.get("bids") or [])
        if not bids:
            return 0.0
        # bids are sorted best-first by py-clob-client
        top = bids[0]
        price = top.price if hasattr(top, "price") else top.get("price", 0)
        return float(price)

    async def get_best_ask(self, token_id: str) -> float:
        """Return current best ask price for a token (for entry pricing)."""
        ob = await self._run(self._client.get_order_book, token_id)
        asks = ob.asks if hasattr(ob, "asks") else (ob.get("asks") or [])
        if not asks:
            return 0.0
        top = asks[0]
        price = top.price if hasattr(top, "price") else top.get("price", 0)
        return float(price)

    async def get_market_info(self, token_id: str) -> dict:
        """Return bid, ask, and ask-side depth (USD) in one order book fetch.

        Returns:
            {"bid": float, "ask": float, "ask_depth_usd": float}
        """
        ob = await self._run(self._client.get_order_book, token_id)
        bids = ob.bids if hasattr(ob, "bids") else (ob.get("bids") or [])
        asks = ob.asks if hasattr(ob, "asks") else (ob.get("asks") or [])

        def _price(level) -> float:
            return float(level.price if hasattr(level, "price") else level.get("price", 0))

        def _size(level) -> float:
            return float(level.size if hasattr(level, "size") else level.get("size", 0))

        bid = _price(bids[0]) if bids else 0.0
        ask = _price(asks[0]) if asks else 0.0

        # Sum ask-side depth (shares × price) across top levels up to 10 levels
        ask_depth_usd = sum(_price(l) * _size(l) for l in asks[:10])

        return {"bid": bid, "ask": ask, "ask_depth_usd": ask_depth_usd}

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def buy(self, token_id: str, price: float, size_usd: float) -> dict:
        """Place a GTC limit BUY order (no-op in dry_run mode).

        T-52: in dry_run, rejects unreachable prices so the fake fill doesn't
        lie. If the live ask is far above the requested price, a live order
        would sit unfilled — recording it as a fake fill at `price` produces
        phantom entry_price that corrupts P&L downstream. Belt-and-suspenders
        to entry_filter.ask_looks_orphan.
        """
        size_shares = round(size_usd / price, 2)
        if self.dry_run:
            # T-56: must fail-closed when we cannot verify the live ask.
            # Previous logic silently ignored get_market_info failures and
            # fake-filled anyway, which is the exact degraded-dependency
            # corner T-52 tried to protect against. If CLOB is flaky and
            # the fetch raises / returns no ask, refuse to record a fill —
            # otherwise paper P&L gets corrupted with entries that had no
            # way to actually execute.
            try:
                info = await self.get_market_info(token_id)
                live_ask = float(info.get("ask") or 0.0)
            except Exception as exc:
                logger.warning(
                    "[DRY RUN] BUY rejected: could not verify live ask (%s) — %s",
                    exc, token_id[:16],
                )
                return {
                    "order_id": "",
                    "status": "rejected",
                    "error": f"dry_run: live ask lookup failed ({exc})",
                    "raw": {},
                }
            if live_ask <= 0:
                logger.warning(
                    "[DRY RUN] BUY rejected: no ask on book for %s", token_id[:16],
                )
                return {
                    "order_id": "",
                    "status": "rejected",
                    "error": "dry_run: no live ask (empty book)",
                    "raw": {},
                }
            # Reject if our price sits > 20¢ below live ask — at that gap
            # a GTC limit essentially never matches on thin pre-game books,
            # so fake-filling at `price` would produce fictional accounting.
            # Normal spreads (< 20¢) still pass through.
            if price < live_ask - 0.20:
                logger.warning(
                    "[DRY RUN] BUY rejected: price %.4f unreachable (live ask %.4f) — %s",
                    price, live_ask, token_id[:16],
                )
                return {
                    "order_id": "",
                    "status": "rejected",
                    "error": f"dry_run: price {price:.4f} far below live ask {live_ask:.4f}",
                    "raw": {},
                }
            fake_id = f"DRY_{token_id[:8]}_{int(price*1000)}"
            logger.info("[DRY RUN] BUY skipped: %s  %s shares @ %.4f  fake_id=%s",
                        token_id[:16], size_shares, price, fake_id)
            return {
                "order_id": fake_id,
                "status": "dry_run",
                "price": price,
                "size_shares": size_shares,
                "size_usd": size_usd,
                "raw": {},
            }
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size_shares,
            side="BUY",
        )
        try:
            signed = await self._run(self._client.create_order, order_args)
            resp = await self._run(self._client.post_order, signed, OrderType.GTC)
            order_id = resp.get("orderID") or resp.get("id") or ""
            status = resp.get("status") or "unknown"
            logger.info("BUY order placed: %s  %s shares @ %.4f  id=%s  status=%s",
                        token_id[:16], size_shares, price, order_id[:16], status)
            return {
                "order_id": order_id,
                "status": status,
                "price": price,
                "size_shares": size_shares,
                "size_usd": size_usd,
                "raw": resp,
            }
        except Exception as exc:
            logger.error("BUY order failed for %s: %s", token_id[:16], exc)
            return {"order_id": "", "status": "rejected", "error": str(exc), "raw": {}}

    async def sell(self, token_id: str, price: float, size_shares: float) -> dict:
        """Place a GTC limit SELL order (no-op in dry_run mode)."""
        if self.dry_run:
            fake_id = f"DRY_SELL_{token_id[:8]}"
            logger.info("[DRY RUN] SELL skipped: %s  %s shares @ %.4f",
                        token_id[:16], size_shares, price)
            return {"order_id": fake_id, "status": "dry_run",
                    "price": price, "size_shares": size_shares, "raw": {}}
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size_shares,
            side="SELL",
        )
        try:
            signed = await self._run(self._client.create_order, order_args)
            resp = await self._run(self._client.post_order, signed, OrderType.GTC)
            order_id = resp.get("orderID") or resp.get("id") or ""
            status = resp.get("status") or "unknown"
            logger.info("SELL order placed: %s  %s shares @ %.4f  id=%s  status=%s",
                        token_id[:16], size_shares, price, order_id[:16], status)
            return {
                "order_id": order_id,
                "status": status,
                "price": price,
                "size_shares": size_shares,
                "raw": resp,
            }
        except Exception as exc:
            logger.error("SELL order failed for %s: %s", token_id[:16], exc)
            return {"order_id": "", "status": "rejected", "error": str(exc), "raw": {}}

    async def cancel(self, order_id: str) -> bool:
        """Cancel an open order by order ID. Returns True on success."""
        try:
            await self._run(self._client.cancel, order_id)
            logger.info("Order cancelled: %s", order_id[:16])
            return True
        except Exception as exc:
            logger.warning("Cancel failed for %s: %s", order_id[:16], exc)
            return False

    async def get_order(self, order_id: str) -> dict:
        """Fetch a single order's current state from CLOB.

        Returns dict with at least:
            status: "LIVE" | "MATCHED" | "CANCELLED" | "UNMATCHED"
            size_matched: shares actually filled (float)
        """
        try:
            result = await self._run(self._client.get_order, order_id)
            if isinstance(result, dict):
                return result
            # Some versions return an object
            return vars(result) if hasattr(result, "__dict__") else {}
        except Exception as exc:
            logger.warning("get_order failed for %s: %s", order_id[:16], exc)
            return {}

    async def get_open_orders(self) -> list[dict]:
        """Fetch all currently open orders from CLOB (across all markets)."""
        try:
            results = await self._run(self._client.get_orders)
            if isinstance(results, list):
                return results
            return []
        except Exception as exc:
            logger.warning("get_open_orders failed: %s", exc)
            return []
