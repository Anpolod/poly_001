"""
Telegram Command Handler — background task for bot control commands.

Supported commands:
    /status     — CLOB balance + open exposure + position count
    /positions  — full table of open positions with fill status + unrealized P&L
    /pnl        — P&L summary (today / all-time)
    /cancel <id> — cancel a specific position by DB id
    /digest     — send the daily summary on demand

Also runs a daily_digest() task that fires automatically at 08:00 UTC every day.

Runs as asyncio.create_task() in bot_main.run_loop().
Uses its own update offset (allowed_updates: ["message"]) so it doesn't
interfere with wait_for_callback which polls callback_query events.

Usage:
    asyncio.create_task(handle_commands(pool, executor, tg_token, tg_chat_id))
    asyncio.create_task(daily_digest(pool, executor, tg_token, tg_chat_id))
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
import asyncpg

from collector.network import make_session
from trading.clob_executor import ClobExecutor
from trading.position_manager import get_open_positions, mark_position_cancelled

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=10)
_POLL_TIMEOUT = aiohttp.ClientTimeout(total=35)
_POLL_INTERVAL_SEC = 5   # command responsiveness

_cmd_offset: int = 0     # separate from telegram_confirm._last_update_id


def _tg_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


_MAX_MSG = 4000   # Telegram limit is 4096; leave headroom for HTML overhead


async def _send(token: str, chat_id: str, text: str) -> None:
    async with make_session(timeout=_TIMEOUT) as s:
        await s.post(_tg_url(token, "sendMessage"), json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })


async def _send_long(token: str, chat_id: str, text: str) -> None:
    """Send text that may exceed Telegram's 4096-char limit by splitting on newlines."""
    while text:
        if len(text) <= _MAX_MSG:
            await _send(token, chat_id, text)
            break
        # Find last newline within the safe window so we never cut inside an HTML tag
        split_at = text.rfind("\n", 0, _MAX_MSG)
        if split_at <= 0:
            split_at = _MAX_MSG   # no newline found — hard split as last resort
        chunk, text = text[:split_at], text[split_at:].lstrip("\n")
        await _send(token, chat_id, chunk)


async def _fetch_updates(token: str) -> list[dict]:
    global _cmd_offset
    try:
        async with make_session(timeout=_POLL_TIMEOUT) as s:
            resp = await s.get(_tg_url(token, "getUpdates"), params={
                "offset": _cmd_offset + 1,
                "timeout": 5,
                "allowed_updates": ["message"],
            })
            data = await resp.json()
        if data.get("ok"):
            return data.get("result", [])
    except Exception as exc:
        logger.debug("Command poller getUpdates error: %s", exc)
    return []


# ── Command handlers ──────────────────────────────────────────────────────────

async def _cmd_status(
    pool: asyncpg.Pool,
    executor: ClobExecutor,
    token: str,
    chat_id: str,
) -> None:
    try:
        balance = await executor.get_balance()
    except Exception:
        balance = -1.0

    positions = await get_open_positions(pool)
    row = await pool.fetchrow(
        "SELECT COALESCE(SUM(size_usd), 0) AS exp FROM open_positions WHERE status='open'"
    )
    exposure = float(row["exp"] or 0)

    lines = [
        "📊 <b>Bot Status</b>",
        f"Balance: <b>${balance:.2f}</b> USDC",
        f"Open positions: <b>{len(positions)}</b>",
        f"Open exposure: <b>${exposure:.2f}</b>",
        f"Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
    ]
    await _send(token, chat_id, "\n".join(lines))


async def _cmd_positions(
    pool: asyncpg.Pool,
    executor: ClobExecutor,
    token: str,
    chat_id: str,
) -> None:
    positions = await get_open_positions(pool)
    if not positions:
        await _send(token, chat_id, "No open positions.")
        return

    lines = ["📋 <b>Open Positions</b>", ""]
    now = datetime.now(timezone.utc)
    for p in positions:
        fill = (p.get("fill_status") or "pending").upper()
        game_str = "—"
        if p["game_start"]:
            gs = p["game_start"].replace(tzinfo=timezone.utc) if p["game_start"].tzinfo is None else p["game_start"]
            diff_h = (gs - now).total_seconds() / 3600
            game_str = f"{diff_h:.1f}h" if diff_h > 0 else "started"

        # Unrealized P&L for confirmed fills only
        upnl_str = ""
        if fill == "FILLED" and p.get("token_id") and p.get("size_shares"):
            try:
                bid = await executor.get_best_bid(p["token_id"])
                entry = float(p["entry_price"] or 0)
                shares = float(p["size_shares"] or 0)
                if bid > 0 and entry > 0 and shares > 0:
                    upnl = (bid - entry) * shares
                    sign = "+" if upnl >= 0 else ""
                    upnl_str = f"\n  Unrealized P&L: <b>{sign}${upnl:.2f}</b> (bid {bid:.3f})"
            except Exception:
                pass

        lines.append(
            f"<b>#{p['id']}</b> {(p['slug'] or '')[:30]}\n"
            f"  {p['signal_type']} | {fill} | ${float(p['size_usd']):.2f} @ {float(p['entry_price']):.3f}\n"
            f"  Game in: {game_str}{upnl_str}"
        )

    await _send_long(token, chat_id, "\n".join(lines))


async def _cmd_pnl(pool: asyncpg.Pool, token: str, chat_id: str) -> None:
    today = await pool.fetchrow(
        """
        SELECT COALESCE(SUM(pnl_usd), 0) AS pnl, COUNT(*) AS cnt
        FROM open_positions
        WHERE status='closed' AND exit_ts >= CURRENT_DATE
        """
    )
    alltime = await pool.fetchrow(
        """
        SELECT COALESCE(SUM(pnl_usd), 0) AS pnl,
               COUNT(*) AS closed,
               COUNT(*) FILTER (WHERE pnl_usd > 0) AS wins
        FROM open_positions
        WHERE status = 'closed'
        """
    )
    by_type = await pool.fetch(
        """
        SELECT signal_type,
               COALESCE(SUM(pnl_usd), 0)          AS pnl,
               COUNT(*)                             AS cnt,
               COUNT(*) FILTER (WHERE pnl_usd > 0) AS wins
        FROM open_positions
        WHERE status = 'closed'
        GROUP BY signal_type
        ORDER BY signal_type
        """
    )

    today_pnl = float(today["pnl"] or 0)
    all_pnl = float(alltime["pnl"] or 0)
    closed = int(alltime["closed"] or 0)
    wins = int(alltime["wins"] or 0)
    wr = f"{wins/closed*100:.0f}%" if closed else "—"

    emoji = "🟢" if all_pnl >= 0 else "🔴"
    lines = [
        f"{emoji} <b>P&L Summary</b>",
        f"Today: <b>${today_pnl:+.2f}</b> ({int(today['cnt'] or 0)} closed)",
        f"All-time: <b>${all_pnl:+.2f}</b>  |  Win rate: {wr} ({wins}/{closed})",
    ]

    if by_type:
        lines.append("")
        lines.append("<b>By strategy:</b>")
        for row in by_type:
            stype = (row["signal_type"] or "unknown").capitalize()
            spnl = float(row["pnl"] or 0)
            scnt = int(row["cnt"] or 0)
            swins = int(row["wins"] or 0)
            swr = f"{swins/scnt*100:.0f}%" if scnt else "—"
            sign = "🟢" if spnl >= 0 else "🔴"
            lines.append(f"  {sign} {stype}: <b>${spnl:+.2f}</b>  {swr} ({swins}/{scnt})")

    await _send(token, chat_id, "\n".join(lines))


async def _cmd_week(pool: asyncpg.Pool, token: str, chat_id: str) -> None:
    """7-day P&L broken down by calendar day (UTC)."""
    rows = await pool.fetch(
        """
        SELECT DATE(exit_ts AT TIME ZONE 'UTC') AS day,
               COALESCE(SUM(pnl_usd), 0)          AS pnl,
               COUNT(*)                             AS cnt,
               COUNT(*) FILTER (WHERE pnl_usd > 0) AS wins,
               string_agg(DISTINCT signal_type, '/') AS types
        FROM open_positions
        WHERE status = 'closed'
          AND exit_ts >= NOW() - INTERVAL '7 days'
        GROUP BY day
        ORDER BY day
        """
    )

    if not rows:
        await _send(token, chat_id, "No closed trades in the last 7 days.")
        return

    total_pnl = sum(float(r["pnl"] or 0) for r in rows)
    total_cnt = sum(int(r["cnt"] or 0) for r in rows)
    total_wins = sum(int(r["wins"] or 0) for r in rows)
    wr = f"{total_wins/total_cnt*100:.0f}%" if total_cnt else "—"

    lines = ["📅 <b>7-Day Breakdown</b>", ""]
    for r in rows:
        day = r["day"]
        pnl = float(r["pnl"] or 0)
        cnt = int(r["cnt"] or 0)
        wins = int(r["wins"] or 0)
        types = r["types"] or "?"
        bar = "🟢" if pnl >= 0 else "🔴"
        day_str = day.strftime("%a %b %d") if hasattr(day, "strftime") else str(day)
        lines.append(f"{bar} <b>{day_str}</b>  ${pnl:+.2f}  ({wins}/{cnt}, {types})")

    lines += [
        "",
        f"Total: <b>${total_pnl:+.2f}</b>  |  Win rate: {wr} ({total_wins}/{total_cnt})",
    ]
    await _send(token, chat_id, "\n".join(lines))


async def _cmd_cancel(
    pool: asyncpg.Pool,
    executor: ClobExecutor,
    token: str,
    chat_id: str,
    position_id_str: str,
) -> None:
    try:
        position_id = int(position_id_str.strip())
    except ValueError:
        await _send(token, chat_id, f"❌ Invalid ID: <code>{position_id_str}</code>\nUsage: /cancel &lt;id&gt;")
        return

    row = await pool.fetchrow(
        "SELECT * FROM open_positions WHERE id=$1 AND status='open'", position_id
    )
    if not row:
        await _send(token, chat_id, f"❌ No open position with id={position_id}")
        return

    order_id = row["clob_order_id"] or ""
    slug = row["slug"] or str(position_id)

    if order_id:
        cancelled = await executor.cancel(order_id)
        if not cancelled:
            await _send(token, chat_id, f"⚠️ CLOB cancel failed for {slug} — marking cancelled in DB anyway.")

    await mark_position_cancelled(pool, position_id)
    await _send(token, chat_id, f"✅ Position #{position_id} ({slug}) cancelled.")


# ── Daily digest ──────────────────────────────────────────────────────────────

async def _build_digest(
    pool: asyncpg.Pool,
    executor: ClobExecutor,
) -> str:
    """Build the daily portfolio summary string."""
    now = datetime.now(timezone.utc)

    try:
        balance = await executor.get_balance()
        balance_str = f"${balance:.2f}"
    except Exception:
        balance_str = "—"

    positions = await get_open_positions(pool)
    filled = [p for p in positions if (p.get("fill_status") or "") == "filled"]
    pending = [
        p for p in positions
        if (p.get("fill_status") or "pending")
           not in ("filled", "cancelled", "force_cancelled", "timed_out")
    ]

    pnl_today = await pool.fetchrow(
        """
        SELECT COALESCE(SUM(pnl_usd), 0) AS pnl, COUNT(*) AS cnt
        FROM open_positions
        WHERE status='closed' AND exit_ts >= CURRENT_DATE
        """
    )
    pnl_week = await pool.fetchrow(
        """
        SELECT COALESCE(SUM(pnl_usd), 0) AS pnl, COUNT(*) AS cnt
        FROM open_positions
        WHERE status='closed' AND exit_ts >= CURRENT_DATE - INTERVAL '7 days'
        """
    )
    today_pnl = float(pnl_today["pnl"] or 0)
    week_pnl = float(pnl_week["pnl"] or 0)

    exposure = await pool.fetchval(
        "SELECT COALESCE(SUM(size_usd), 0) FROM open_positions WHERE status='open'"
    )
    exposure = float(exposure or 0)

    # Positions approaching game start in next 6h
    soon = []
    for p in positions:
        if p["game_start"]:
            gs = p["game_start"].replace(tzinfo=timezone.utc) if p["game_start"].tzinfo is None else p["game_start"]
            h = (gs - now).total_seconds() / 3600
            if 0 < h < 6:
                soon.append((p["slug"] or str(p["id"]), h))

    pnl_emoji = "🟢" if today_pnl >= 0 else "🔴"
    lines = [
        f"☀️ <b>Daily Digest — {now.strftime('%b %d, %H:%M UTC')}</b>",
        "",
        f"💰 Balance: <b>{balance_str}</b>  |  Exposure: <b>${exposure:.2f}</b>",
        f"{pnl_emoji} P&amp;L today: <b>${today_pnl:+.2f}</b>  |  7d: <b>${week_pnl:+.2f}</b>",
        "",
        f"📋 Positions: <b>{len(positions)}</b> open  ({len(filled)} filled, {len(pending)} pending)",
    ]

    if soon:
        lines.append("")
        lines.append("⏰ <b>Games starting soon:</b>")
        for slug, h in sorted(soon, key=lambda x: x[1]):
            lines.append(f"  • {slug[:35]} — {h:.1f}h")

    return "\n".join(lines)


async def heartbeat(
    pool: asyncpg.Pool,
    executor: ClobExecutor,
    token: str,
    chat_id: str,
    interval_hours: float = 6.0,
) -> None:
    """Background task: send a brief 'bot alive' ping every interval_hours hours.

    interval_hours=0 disables the heartbeat entirely.
    """
    if interval_hours <= 0:
        logger.info("Heartbeat disabled (interval_hours=0)")
        return

    interval_sec = interval_hours * 3600
    logger.info("Heartbeat task started (every %.1fh)", interval_hours)

    while True:
        try:
            await asyncio.sleep(interval_sec)

            positions = await get_open_positions(pool)
            filled = sum(1 for p in positions if (p.get("fill_status") or "") == "filled")
            pending = len(positions) - filled

            try:
                balance = await executor.get_balance()
                bal_str = f"${balance:.2f}"
            except Exception:
                bal_str = "—"

            now = datetime.now(timezone.utc)
            await _send(token, chat_id, (
                f"💓 <b>Heartbeat</b> — {now.strftime('%H:%M UTC')}\n"
                f"Balance: {bal_str}  |  Open: {len(positions)} "
                f"({filled} filled, {pending} pending)"
            ))

        except asyncio.CancelledError:
            logger.info("Heartbeat task cancelled.")
            return
        except Exception as exc:
            logger.error("Heartbeat error: %s", exc)


async def daily_digest(
    pool: asyncpg.Pool,
    executor: ClobExecutor,
    token: str,
    chat_id: str,
    hour_utc: int = 8,
) -> None:
    """Background task: send a portfolio digest every day at hour_utc:00 UTC."""
    logger.info("Daily digest task started (fires at %02d:00 UTC)", hour_utc)

    while True:
        try:
            now = datetime.now(timezone.utc)
            next_fire = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
            if next_fire <= now:
                next_fire += timedelta(days=1)
            sleep_sec = (next_fire - now).total_seconds()
            logger.debug("Daily digest sleeping %.0fs until %s", sleep_sec, next_fire.strftime("%H:%M UTC"))
            await asyncio.sleep(sleep_sec)

            digest = await _build_digest(pool, executor)
            await _send(token, chat_id, digest)
            logger.info("Daily digest sent")

        except asyncio.CancelledError:
            logger.info("Daily digest task cancelled.")
            return
        except Exception as exc:
            logger.error("Daily digest error: %s", exc)
            await asyncio.sleep(3600)   # retry in 1h on error


# ── Main loop ─────────────────────────────────────────────────────────────────

async def handle_commands(
    pool: asyncpg.Pool,
    executor: ClobExecutor,
    token: str,
    chat_id: str,
) -> None:
    """Background task: poll Telegram for /commands and dispatch."""
    global _cmd_offset
    logger.info("Telegram command handler started")

    while True:
        try:
            updates = await _fetch_updates(token)

            for update in updates:
                _cmd_offset = max(_cmd_offset, update["update_id"])
                msg = update.get("message") or {}
                text = (msg.get("text") or "").strip()
                if not text.startswith("/"):
                    continue

                parts = text.split(maxsplit=1)
                cmd = parts[0].lower().split("@")[0]   # strip @botname suffix
                arg = parts[1] if len(parts) > 1 else ""

                logger.info("Telegram command: %s %s", cmd, arg)

                if cmd == "/status":
                    await _cmd_status(pool, executor, token, chat_id)
                elif cmd == "/positions":
                    await _cmd_positions(pool, executor, token, chat_id)
                elif cmd == "/pnl":
                    await _cmd_pnl(pool, token, chat_id)
                elif cmd == "/cancel":
                    await _cmd_cancel(pool, executor, token, chat_id, arg)
                elif cmd == "/week":
                    await _cmd_week(pool, token, chat_id)
                elif cmd == "/digest":
                    digest = await _build_digest(pool, executor)
                    await _send(token, chat_id, digest)
                elif cmd == "/help":
                    await _send(token, chat_id, (
                        "🤖 <b>Bot Commands</b>\n"
                        "/status — balance + exposure\n"
                        "/positions — open positions + unrealized P&amp;L\n"
                        "/pnl — all-time P&amp;L by strategy\n"
                        "/week — 7-day day-by-day breakdown\n"
                        "/digest — full portfolio summary now\n"
                        "/cancel &lt;id&gt; — cancel a position"
                    ))

        except asyncio.CancelledError:
            logger.info("Command handler cancelled.")
            return
        except Exception as exc:
            logger.error("Command handler error: %s", exc)

        await asyncio.sleep(_POLL_INTERVAL_SEC)
