"""
Telegram Confirmation Flow

Sends signal messages with [✅ Approve] [❌ Skip] inline keyboard buttons.
Polls getUpdates to catch callback_query responses.

Uses raw Bot API via aiohttp — no third-party library needed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import aiohttp

from collector.network import make_session

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=10)
_POLL_TIMEOUT = aiohttp.ClientTimeout(total=35)  # long-poll timeout
_BASE = "https://api.telegram.org/bot{token}/{method}"

# Shared offset tracker so we never re-process old updates
_last_update_id: int = 0


def _url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


async def _post(token: str, method: str, payload: dict) -> dict:
    async with make_session(timeout=_TIMEOUT) as s:
        resp = await s.post(_url(token, method), json=payload)
        data = await resp.json()
        if not data.get("ok"):
            logger.warning("Telegram %s failed: %s", method, data)
        return data


async def _answer_callback(token: str, callback_query_id: str, text: str) -> None:
    await _post(token, "answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text,
    })


async def send_signal_confirm(
    token: str,
    chat_id: str,
    market_id: str,
    team_or_player: str,
    signal_type: str,
    price: float,
    size_usd: float,
    size_shares: float,
    extra_info: str,
    timeout_sec: int,
) -> int:
    """Send signal message with Approve / Skip inline keyboard.

    Returns message_id of the sent message.
    """
    if signal_type == "tanking":
        emoji = "🏀"
        title = f"BUY {team_or_player}"
    else:
        emoji = "⚡"
        title = team_or_player

    text = (
        f"{emoji} <b>NEW SIGNAL — {title}</b>\n"
        f"Price: <b>{price:.3f}</b>  |  Size: <b>${size_usd:.0f}</b> ({size_shares:.0f} shares)\n"
        f"{extra_info}\n"
        f"⏱ Auto-skip in {timeout_sec // 60} min"
    )

    keyboard = {
        "inline_keyboard": [[
            {"text": f"✅ Approve ${size_usd:.0f}", "callback_data": f"approve:{market_id}"},
            {"text": "❌ Skip", "callback_data": f"skip:{market_id}"},
        ]]
    }

    result = await _post(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": keyboard,
    })

    return result.get("result", {}).get("message_id", 0)


async def send_signal_alert(
    token: str,
    chat_id: str,
    team_or_player: str,
    signal_type: str,
    price: float,
    size_usd: float,
    size_shares: float,
    extra_info: str,
) -> None:
    """Send a notification-only signal message (no Approve/Skip buttons).

    Used in auto-trade mode — bot executes immediately, Telegram is just a log.
    """
    if signal_type == "tanking":
        emoji = "🏀"
        title = f"BUY {team_or_player}"
    else:
        emoji = "⚡"
        title = team_or_player

    text = (
        f"{emoji} <b>SIGNAL — {title}</b>\n"
        f"Price: <b>{price:.3f}</b>  |  Size: <b>${size_usd:.0f}</b> ({size_shares:.0f} shares)\n"
        f"{extra_info}\n"
        f"🤖 Executing automatically…"
    )

    await _post(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


async def wait_for_callback(
    token: str,
    market_id: str,
    timeout_sec: int,
) -> bool:
    """Poll getUpdates until user clicks Approve/Skip or timeout.

    Returns True if approved, False if skipped or timed out.
    """
    global _last_update_id
    deadline = time.monotonic() + timeout_sec
    approve_data = f"approve:{market_id}"
    skip_data = f"skip:{market_id}"

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        poll_secs = min(30, max(1, int(remaining)))

        try:
            async with make_session(timeout=_POLL_TIMEOUT) as s:
                resp = await s.get(_url(token, "getUpdates"), params={
                    "offset": _last_update_id + 1,
                    "timeout": poll_secs,
                    "allowed_updates": ["callback_query"],
                })
                data = await resp.json()
        except asyncio.TimeoutError:
            continue
        except Exception as exc:
            logger.warning("getUpdates error: %s", exc)
            await asyncio.sleep(2)
            continue

        if not data.get("ok"):
            await asyncio.sleep(2)
            continue

        for update in data.get("result", []):
            _last_update_id = max(_last_update_id, update["update_id"])
            cq = update.get("callback_query")
            if not cq:
                continue

            cb_data = cq.get("data", "")
            cq_id = cq["id"]

            if cb_data == approve_data:
                await _answer_callback(token, cq_id, "✅ Order approved!")
                return True
            elif cb_data == skip_data:
                await _answer_callback(token, cq_id, "❌ Signal skipped.")
                return False
            # else: callback for a different market — keep waiting

    logger.info("Confirm timeout for market %s", market_id)
    return False


async def send_order_confirmation(
    token: str,
    chat_id: str,
    team_or_player: str,
    price: float,
    size_shares: float,
    size_usd: float,
    order_id: str,
    status: str,
    side: str = "YES",
) -> None:
    """Send order placement result to Telegram.

    T-42: `side` (YES/NO) must be rendered accurately. Earlier versions
    hardcoded 'YES' in the message body, so every NO-side MLB fill silently
    misrepresented the live portfolio — confusing both humans trying to hedge
    and the audit trail during incidents.
    """
    if status in ("matched", "delayed", "live"):
        emoji = "✅"
        result_text = "Order placed!"
    else:
        emoji = "⚠️"
        result_text = f"Order status: {status}"

    side_label = side if side in ("YES", "NO") else "YES"
    text = (
        f"{emoji} <b>{result_text}</b>\n"
        f"<b>{team_or_player}</b> {side_label} @ {price:.3f}\n"
        f"{size_shares:.0f} shares · ${size_usd:.2f}\n"
        f"Order ID: <code>{order_id[:20]}</code>\n"
        f"⏰ Auto-exit 30 min before game start."
    )
    await _post(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    })


async def send_exit_notification(
    token: str,
    chat_id: str,
    slug: str,
    exit_price: float,
    size_shares: float,
    pnl_usd: float,
) -> None:
    """Send auto-exit result to Telegram."""
    pnl_emoji = "🟢" if pnl_usd >= 0 else "🔴"
    text = (
        f"⏰ <b>Auto-exit:</b> {slug}\n"
        f"Sold {size_shares:.0f} shares @ {exit_price:.3f}\n"
        f"{pnl_emoji} P&L: <b>{pnl_usd:+.2f} USD</b>\n"
        f"Position closed."
    )
    await _post(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    })


async def send_error_alert(token: str, chat_id: str, message: str) -> None:
    """Send a generic error alert to Telegram."""
    await _post(token, "sendMessage", {
        "chat_id": chat_id,
        "text": f"⚠️ <b>Bot error:</b> {message}",
        "parse_mode": "HTML",
    })
