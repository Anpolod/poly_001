"""
Trading Bot — main loop

Scans for signals, sends Telegram confirmations, executes CLOB orders,
and auto-exits positions before game start.

Usage:
    python -m trading.bot_main

Requirements:
    - POLYGON_PRIVATE_KEY in .env or environment
    - trading.enabled: true in config/settings.yaml
    - Telegram bot token + chat_id in config alerts section
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

from collector.network import make_session
import yaml

# ── project root on sys.path ──────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# ── load .env if present ──────────────────────────────────────────────────────
_ENV_FILE = _ROOT / ".env"
if _ENV_FILE.exists():
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from analytics.prop_scanner import PropOpportunity, scan as prop_scan  # noqa: E402
from analytics.tanking_scanner import (  # noqa: E402
    TankingSignal,
    enrich_with_lineup_news,
    get_standings,
    load_aliases,
    scan_tanking_patterns,
)
from analytics.injury_scanner import (  # noqa: E402
    InjurySignal,
    build_injury_signals,
    persist_signals as persist_injury_signals,
)
from analytics.calibration_signal import (  # noqa: E402
    CalibrationSignal,
    scan as calibration_scan,
)
from analytics.drift_monitor import (  # noqa: E402
    DriftSignal,
    persist_signals as persist_drift_signals,
    scan as drift_scan,
)
from analytics.spike_signal import (  # noqa: E402
    SpikeSignal,
    persist_signals as persist_spike_signals,
    scan as spike_scan,
)
from analytics.mlb_pitcher_scanner import (  # noqa: E402
    PitcherSignal,
    load_mlb_aliases,
    scan_pitcher_patterns,
)
from collector.mlb_data import MLBDataFetcher  # noqa: E402
from trading.clob_executor import ClobExecutor  # noqa: E402
from trading.entry_filter import check_entry  # noqa: E402
from trading.exit_monitor import check_and_exit, check_stagnation_exit  # noqa: E402
from trading.order_poller import poll_order_fills  # noqa: E402
from trading.risk_guard import circuit_breaker_check, correlation_check, stop_loss_monitor  # noqa: E402
from trading.telegram_commands import daily_digest, handle_commands, heartbeat  # noqa: E402
from trading.position_manager import (  # noqa: E402
    get_total_exposure,
    has_position,
    log_order,
    open_position,
    resolve_team_token_side,
)
from trading.risk_manager import can_open, position_size_by_ev, tanking_roi_estimate  # noqa: E402
from trading.telegram_confirm import (  # noqa: E402
    send_error_alert,
    send_order_confirmation,
    send_signal_alert,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger().setLevel(logging.INFO)

# Rotating file handler — 20 MB per file, keep 5 files (~100 MB total)
_log_dir = _ROOT / "logs"
_log_dir.mkdir(exist_ok=True)
_file_handler = logging.handlers.RotatingFileHandler(
    _log_dir / "bot.log", maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.getLogger().addHandler(_file_handler)

logger = logging.getLogger(__name__)

# ── Signal decay cache ────────────────────────────────────────────────────────
# Markets skipped (user declined or timed-out) are suppressed for N hours so
# the bot doesn't re-alert on the same opportunity every scan cycle.
_skip_cache: dict[str, datetime] = {}
_SKIP_TTL_HOURS = 4.0


def _is_skipped(market_id: str) -> bool:
    """Return True if this market is still within its decay window."""
    exp = _skip_cache.get(market_id)
    if exp is None:
        return False
    if datetime.now(timezone.utc) >= exp:
        del _skip_cache[market_id]
        return False
    return True


def _mark_skipped(market_id: str, hours: float = _SKIP_TTL_HOURS) -> None:
    """Suppress this market for `hours` hours."""
    _skip_cache[market_id] = datetime.now(timezone.utc) + timedelta(hours=hours)


_STATE_FILE = _ROOT / "logs" / "bot_state.json"
_last_low_balance_alert: datetime | None = None
_LOW_BALANCE_ALERT_INTERVAL_H = 2.0   # don't spam more than once per 2 hours

# Injury scanner runs on its own cadence (default 10 min) inside the main loop
_last_injury_scan: datetime | None = None
_injury_alerted: dict[str, datetime] = {}  # market_id -> last alert ts (Telegram dedupe)

# Calibration trader cadence (default 60 min) + per-market dedupe for alerts
_last_calibration_scan: datetime | None = None
_calibration_alerted: dict[str, datetime] = {}

# Drift monitor cadence (default 15 min) + per-market 6h dedupe
_last_drift_scan: datetime | None = None
_drift_alerted: dict[str, datetime] = {}

# Spike follow cadence (default 5 min) + per-spike-event dedupe (one alert per event)
_last_spike_scan: datetime | None = None
_spike_alerted: set[int] = set()


def _write_bot_state(balance: float, trading_enabled: bool) -> None:
    """Write current bot state to logs/bot_state.json for the dashboard to read."""
    try:
        _STATE_FILE.parent.mkdir(exist_ok=True)
        _STATE_FILE.write_text(json.dumps({
            "balance_usd": round(balance, 4),
            "trading_enabled": trading_enabled,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }))
    except Exception:
        pass


async def _check_low_balance(
    balance: float,
    config: dict,
    tg_token: str,
    tg_chat_id: str,
) -> None:
    """Alert via Telegram if CLOB balance is below the configured threshold."""
    global _last_low_balance_alert
    threshold = float(config["trading"].get("low_balance_threshold_usd", 20.0))
    if balance >= threshold:
        return

    now = datetime.now(timezone.utc)
    if _last_low_balance_alert is not None:
        hours_since = (now - _last_low_balance_alert).total_seconds() / 3600
        if hours_since < _LOW_BALANCE_ALERT_INTERVAL_H:
            return

    _last_low_balance_alert = now
    logger.warning("Low CLOB balance: $%.2f (threshold $%.2f)", balance, threshold)
    try:
        from trading.telegram_confirm import _post  # noqa: PLC0415
        await _post(tg_token, "sendMessage", {
            "chat_id": tg_chat_id,
            "text": (
                f"⚠️ <b>Low CLOB Balance</b>\n"
                f"Balance: <b>${balance:.2f}</b> USDC\n"
                f"Fund your Polygon wallet to continue trading."
            ),
            "parse_mode": "HTML",
        })
    except Exception:
        pass


def _load_config() -> dict:
    with (_ROOT / "config" / "settings.yaml").open() as f:
        return yaml.safe_load(f)


async def _create_pool(config: dict) -> asyncpg.Pool:
    db = config["database"]
    return await asyncpg.create_pool(
        host=db["host"],
        port=db["port"],
        database=db["name"],
        user=db["user"],
        password=str(db["password"]),
        min_size=1,
        max_size=5,
    )


async def _get_token_id(pool: asyncpg.Pool, market_id: str) -> str:
    """Look up YES token ID from the markets table."""
    row = await pool.fetchrow(
        "SELECT token_id_yes FROM markets WHERE id=$1", market_id
    )
    return row["token_id_yes"] if row and row["token_id_yes"] else ""


def _is_buy_rejected(order: dict) -> tuple[bool, str]:
    """Round-5 guard: detect a CLOB BUY rejection (no exchange order id).

    Returns (rejected, reason). Callers must NOT persist an open_position row
    when rejected=True — the order_poller treats empty clob_order_id as
    "retry the buy", which would turn a hard rejection (insufficient balance,
    auth error, venue reject) into a ghost position plus infinite retries.
    """
    order_id = order.get("order_id") or ""
    if order_id:
        return False, ""
    reason = order.get("error") or order.get("status") or "unknown"
    return True, str(reason)


async def _process_tanking_signal(
    signal: TankingSignal,
    pool: asyncpg.Pool,
    executor: ClobExecutor,
    config: dict,
    tg_token: str,
    tg_chat_id: str,
    total_exposure: float,
    aliases: dict[str, str] | None = None,
) -> float:
    """Handle one tanking signal: risk check → confirm → execute. Returns added exposure.

    T-36: resolve the motivated team's actual Polymarket side before trading.
    Earlier versions always bought token_id_yes — on NO-side motivated markets
    that silently took the opposite side of the signal. Mirror of the T-35
    MLB fix. If aliases is None (backward-compat for any legacy callers), we
    fall back to the scanner's `motivated_side` and YES-token semantics.
    """
    # T-36: resolve the motivated team's side + correct token id. We prefer
    # the live DB lookup over `signal.motivated_side` so a remap between
    # scan-time and execution-time is caught rather than silently traded on.
    if aliases is not None:
        exec_token_id, resolved_side = await resolve_team_token_side(
            pool, signal.market_id, signal.motivated_team, aliases
        )
        if exec_token_id is None or resolved_side is None:
            logger.warning(
                "Skipping tanking %s — could not resolve motivated-team side for %s",
                signal.motivated_team, signal.market_id,
            )
            return 0.0
        if signal.motivated_side and signal.motivated_side != resolved_side:
            logger.warning(
                "Tanking side mismatch for %s on %s: scanner=%s vs live=%s — skipping",
                signal.motivated_team, signal.market_id,
                signal.motivated_side, resolved_side,
            )
            return 0.0
        token_id = exec_token_id
        motivated_side = signal.motivated_side or resolved_side
    else:
        # Legacy fallback (e.g. prop/drift callers never hit here; kept for
        # test harnesses that construct signals without aliases).
        token_id = await _get_token_id(pool, signal.market_id)
        motivated_side = signal.motivated_side or "YES"

    min_depth = config["trading"].get("min_ask_depth_usd", 50)

    # Fetch live market state for entry filter — against the motivated-side token
    bid, ask, ask_depth_usd = 0.0, 0.0, 0.0
    if token_id:
        try:
            info = await executor.get_market_info(token_id)
            bid, ask, ask_depth_usd = info["bid"], info["ask"], info["ask_depth_usd"]
        except Exception:
            pass

    # Correlation guard
    corr_blocked, corr_reason = await correlation_check(
        pool, config, signal.game_start, signal.market_id
    )
    if corr_blocked:
        logger.info("Skipping %s — %s", signal.motivated_team, corr_reason)
        return 0.0

    # Entry filter — hard skip if conditions are unworkable
    entry_decision, entry_reason, entry_emoji = check_entry(
        bid=bid,
        ask=ask,
        signal_price=signal.current_price,
        ask_depth_usd=ask_depth_usd,
        hours_to_game=signal.hours_to_game,
        min_depth_usd=min_depth,
    )
    if entry_decision == "skip":
        logger.info("Skipping %s — entry filter: %s", signal.motivated_team, entry_reason)
        return 0.0

    ok, reason = can_open(config, total_exposure, ask_depth_usd if ask_depth_usd > 0 else min_depth)
    if not ok:
        logger.info("Skipping %s: %s", signal.motivated_team, reason)
        return 0.0

    roi_est = tanking_roi_estimate(signal.motivation_differential, signal.actual_drift)
    size_usd, size_shares = position_size_by_ev(config, signal.current_price, roi_est)

    drift_str = f"Drift 24h: {signal.actual_drift:+.3f} ↑\n" if signal.actual_drift else ""
    market_line = f"Market: {entry_emoji} {entry_reason}\n"
    extra = (
        f"{market_line}"
        f"Side: {motivated_side}  |  Game in {signal.hours_to_game:.1f}h  |  diff={signal.motivation_differential:.1f}\n"
        f"{drift_str}"
        f"vs {signal.tanking_team}"
    )

    await send_signal_alert(
        tg_token, tg_chat_id,
        team_or_player=signal.motivated_team,
        signal_type="tanking",
        price=signal.current_price,
        size_usd=size_usd,
        size_shares=size_shares,
        extra_info=extra,
    )

    if not config["trading"].get("enabled", False):
        logger.info("DRY-RUN: would BUY %s @ %.3f  $%.2f (trading disabled)",
                    signal.motivated_team, signal.current_price, size_usd)
        return 0.0

    # token_id already fetched above; re-check in case it was empty
    if not token_id:
        token_id = await _get_token_id(pool, signal.market_id)
    if not token_id:
        msg = f"No token_id for market {signal.market_id}. Cannot trade."
        logger.error(msg)
        await send_error_alert(tg_token, tg_chat_id, msg)
        return 0.0

    # Trade price: take live ask for liquid markets, signal_price as limit for thin ones
    if entry_decision == "enter" and ask > 0:
        trade_price = ask   # liquid — take the ask immediately
    else:
        trade_price = signal.current_price  # thin — GTC limit, wait for fill

    # Place order
    order = await executor.buy(token_id, trade_price, size_usd)

    rejected, reason = _is_buy_rejected(order)
    if rejected:
        await log_order(pool, signal.market_id, None, "buy_rejected", order)
        logger.error("BUY rejected for %s: %s", signal.motivated_team, reason)
        await send_error_alert(
            tg_token, tg_chat_id,
            f"BUY rejected for {signal.motivated_team}: {reason}",
        )
        return 0.0

    order_id_str = order["order_id"]
    actual_shares = order.get("size_shares", size_shares)

    # Persist — T-36: real side (not hardcoded YES) so DB audit matches execution
    position_id = await open_position(
        pool,
        market_id=signal.market_id,
        slug=signal.slug,
        signal_type="tanking",
        token_id=token_id,
        size_usd=size_usd,
        size_shares=actual_shares,
        entry_price=trade_price,
        game_start=signal.game_start,
        clob_order_id=order_id_str,
        notes=f"motivated={signal.motivated_team}({motivated_side}) tanking={signal.tanking_team}",
        side=motivated_side,
    )
    await log_order(pool, signal.market_id, position_id, "buy", order)

    await send_order_confirmation(
        tg_token, tg_chat_id,
        team_or_player=signal.motivated_team,
        price=trade_price,
        size_shares=actual_shares,
        size_usd=size_usd,
        order_id=order.get("order_id", ""),
        side=motivated_side,
        status=order.get("status", ""),
    )

    return size_usd


async def _process_pitcher_signal(
    signal: PitcherSignal,
    pool: asyncpg.Pool,
    executor: ClobExecutor,
    config: dict,
    tg_token: str,
    tg_chat_id: str,
    total_exposure: float,
    mlb_aliases: dict[str, str],
) -> float:
    """Handle one MLB pitcher mismatch signal. Returns added exposure.

    T-35 P1.3 — must select the favored team's actual token side. Earlier
    versions always bought token_id_yes, which silently took the opposite side
    of the signal whenever the favored team happened to be on Polymarket's NO
    side. Now: resolve_team_token_side() runs first, and ALL downstream prices
    (entry filter, sizing, alerts, execution) use the favored-side price.
    """
    if not signal.market_id:
        return 0.0

    # ─── T-35: resolve which side the favored team is on (for exec_token_id) ──
    # We still call resolve_team_token_side here because the scanner only stores
    # `favored_side` (YES/NO) but not the token id — we need the token id to
    # submit the order against the correct CLOB side.
    exec_token_id, resolved_side = await resolve_team_token_side(
        pool, signal.market_id, signal.favored_team, mlb_aliases
    )
    if exec_token_id is None or resolved_side is None:
        logger.warning(
            "Skipping MLB %s — could not resolve favored_team side for %s",
            signal.favored_team, signal.market_id,
        )
        return 0.0

    # T-42: signal.current_price is ALREADY side-correct (scanner inverts to
    # 1 - yes_mid for NO-side favorites before storing). A prior version of
    # this function flipped it a second time, which produced the underdog's
    # YES-token price on NO-side markets — wrong for entry filter, sizing,
    # alerts, and the thin-market fallback order price. Use the signal value
    # directly. We still verify resolved_side matches signal.favored_side so
    # a mismatch between scanner-time and execution-time side (e.g. token
    # remap) is never silently traded on.
    favored_side = signal.favored_side or resolved_side
    if signal.favored_side and signal.favored_side != resolved_side:
        logger.warning(
            "MLB side mismatch for %s on %s: scanner=%s vs live=%s — skipping",
            signal.favored_team, signal.market_id,
            signal.favored_side, resolved_side,
        )
        return 0.0
    favored_price = signal.current_price

    min_depth = config["trading"].get("min_ask_depth_usd", 50)

    # Fetch live bid/ask/depth for the FAVORED-side token (not always YES)
    bid, ask, ask_depth_usd = 0.0, 0.0, 0.0
    try:
        info = await executor.get_market_info(exec_token_id)
        bid, ask, ask_depth_usd = info["bid"], info["ask"], info["ask_depth_usd"]
    except Exception:
        pass

    corr_blocked, corr_reason = await correlation_check(
        pool, config, signal.game_start, signal.market_id
    )
    if corr_blocked:
        logger.info("Skipping MLB %s — %s", signal.favored_team, corr_reason)
        return 0.0

    entry_decision, entry_reason, entry_emoji = check_entry(
        bid=bid, ask=ask, signal_price=favored_price,
        ask_depth_usd=ask_depth_usd, hours_to_game=signal.hours_to_game,
        min_depth_usd=min_depth,
    )
    if entry_decision == "skip":
        logger.info("Skipping MLB %s — entry filter: %s", signal.favored_team, entry_reason)
        return 0.0

    ok, reason = can_open(config, total_exposure, ask_depth_usd if ask_depth_usd > 0 else min_depth)
    if not ok:
        logger.info("Skipping MLB %s: %s", signal.favored_team, reason)
        return 0.0

    # Conservative ROI estimate for pitcher signals (lower confidence than tanking).
    # Units must be ROI percent (5.0 = 5%) — position_size_by_ev expects percent,
    # not fraction. Scale ~10× lower than tanking_roi_estimate (15× per diff unit)
    # so a 2-point ERA mismatch ≈ 3% and we cap at 15% (half of tanking's 30%).
    roi_est = min(15.0, max(2.0, signal.era_differential * 1.5))
    size_usd, size_shares = position_size_by_ev(config, favored_price, roi_est)

    h_era = f"{signal.home_pitcher_era:.2f}" if signal.home_pitcher_era else "?"
    a_era = f"{signal.away_pitcher_era:.2f}" if signal.away_pitcher_era else "?"
    extra = (
        f"Market: {entry_emoji} {entry_reason}\n"
        f"Side: {favored_side}  |  Game in {signal.hours_to_game:.1f}h  |  ERA diff={signal.era_differential:.1f}\n"
        f"Home SP: {signal.home_pitcher_name} ({h_era})\n"
        f"Away SP: {signal.away_pitcher_name} ({a_era})\n"
        f"vs {signal.underdog_team}"
    )

    await send_signal_alert(
        tg_token, tg_chat_id,
        team_or_player=signal.favored_team,
        signal_type="pitcher",
        price=favored_price,
        size_usd=size_usd,
        size_shares=size_shares,
        extra_info=extra,
    )

    if not config["trading"].get("enabled", False):
        logger.info("DRY-RUN: would BUY MLB %s (%s side) @ %.3f  $%.2f (trading disabled)",
                    signal.favored_team, favored_side, favored_price, size_usd)
        return 0.0

    trade_price = ask if (entry_decision == "enter" and ask > 0) else favored_price
    order = await executor.buy(exec_token_id, trade_price, size_usd)

    rejected, reason = _is_buy_rejected(order)
    if rejected:
        await log_order(pool, signal.market_id, None, "buy_rejected", order)
        logger.error("MLB BUY rejected for %s (%s side): %s", signal.favored_team, favored_side, reason)
        await send_error_alert(
            tg_token, tg_chat_id,
            f"MLB BUY rejected for {signal.favored_team} ({favored_side} side): {reason}",
        )
        return 0.0

    order_id_str = order["order_id"]
    actual_shares = order.get("size_shares", size_shares)

    position_id = await open_position(
        pool,
        market_id=signal.market_id,
        slug=signal.slug,
        signal_type="pitcher",
        token_id=exec_token_id,
        size_usd=size_usd,
        size_shares=actual_shares,
        entry_price=trade_price,
        game_start=signal.game_start,
        clob_order_id=order_id_str,
        notes=f"favored={signal.favored_team}({favored_side}) SP={signal.home_pitcher_name}vs{signal.away_pitcher_name}",
        side=favored_side,   # T-38: persist real side (YES or NO) instead of hardcoded YES
    )
    await log_order(pool, signal.market_id, position_id, "buy", order)
    await send_order_confirmation(
        tg_token, tg_chat_id,
        team_or_player=signal.favored_team,
        price=trade_price,
        size_shares=actual_shares,
        size_usd=size_usd,
        order_id=order.get("order_id", ""),
        status=order.get("status", ""),
        side=favored_side,   # T-42: real side in message body, not hardcoded YES
    )
    return size_usd


async def _run_injury_scan(
    pool: asyncpg.Pool,
    http_session,
    aliases: dict,
    config: dict,
    tg_token: str,
    tg_chat_id: str,
) -> int:
    """Scan Rotowire for injuries, persist signals, alert via Telegram.

    Returns number of new BUY signals produced this cycle. Alert-only: does
    NOT auto-execute (YES/NO mapping to the healthy team varies per market;
    user confirms manually from the Telegram digest).
    """
    global _last_injury_scan

    inj_cfg = config.get("injury_scanner", {})
    if not inj_cfg.get("enabled", False):
        return 0

    interval_sec = float(inj_cfg.get("scan_interval_min", 10)) * 60.0
    now = datetime.now(timezone.utc)
    if _last_injury_scan is not None:
        elapsed = (now - _last_injury_scan).total_seconds()
        if elapsed < interval_sec:
            return 0
    _last_injury_scan = now

    statuses_raw = str(inj_cfg.get("statuses", "OUT,DOUBTFUL"))
    statuses = {s.strip().upper() for s in statuses_raw.split(",") if s.strip()}

    try:
        signals = await asyncio.wait_for(
            build_injury_signals(
                pool, http_session, aliases,
                hours_window=float(inj_cfg.get("hours_window", 24)),
                statuses=statuses,
                max_entry_price=float(inj_cfg.get("max_entry_price", 0.85)),
            ),
            timeout=45,
        )
    except asyncio.TimeoutError:
        logger.warning("Injury scan timed out — skipping this cycle")
        return 0
    except Exception as exc:
        logger.warning("Injury scan failed: %s", exc)
        return 0

    if not signals:
        return 0

    inserted = await persist_injury_signals(pool, signals)

    # Telegram alert — one digest per cycle, dedupe by market_id for 6h
    buy_signals = [s for s in signals if s.action == "BUY"]
    fresh: list[InjurySignal] = []
    cutoff = now - timedelta(hours=6)
    for s in buy_signals:
        last = _injury_alerted.get(s.market_id)
        if last and last > cutoff:
            continue
        fresh.append(s)
        _injury_alerted[s.market_id] = now

    logger.info(
        "Injury scan: %d signals, %d inserted, %d fresh BUY alerts",
        len(signals), inserted, len(fresh),
    )

    if fresh:
        lines = ["🏥 <b>NBA Injury Scanner</b>"]
        for s in fresh[:10]:
            drift = f"{s.drift_24h:+.3f}" if s.drift_24h is not None else "n/a"
            lines.append(
                f"• <b>{s.healthy_team}</b> vs {s.injured_team}\n"
                f"    {s.player_name} <b>{s.status}</b>  |  "
                f"price {s.current_price:.3f}  drift {drift}  "
                f"T-{s.hours_to_game:.1f}h"
            )
        try:
            from trading.telegram_confirm import _post  # noqa: PLC0415
            await _post(tg_token, "sendMessage", {
                "chat_id": tg_chat_id,
                "text": "\n".join(lines),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
        except Exception as exc:
            logger.warning("Failed to send injury alert to Telegram: %s", exc)

    return len(fresh)


async def _run_calibration_scan(
    pool: asyncpg.Pool,
    config: dict,
    tg_token: str,
    tg_chat_id: str,
) -> int:
    """Scan active markets against calibration_edges; alert via Telegram.

    Alert-only — same reasoning as injury_scanner. Mapping an edge direction
    (YES/NO) to the correct Polymarket token requires per-market verification
    the bot doesn't currently do, so the user confirms from the digest.

    Returns number of fresh BUY alerts sent this cycle.
    """
    global _last_calibration_scan

    cal_cfg = config.get("calibration_trader", {})
    if not cal_cfg.get("enabled", False):
        return 0

    interval_sec = float(cal_cfg.get("scan_interval_min", 60)) * 60.0
    now = datetime.now(timezone.utc)
    if _last_calibration_scan is not None:
        elapsed = (now - _last_calibration_scan).total_seconds()
        if elapsed < interval_sec:
            return 0
    _last_calibration_scan = now

    try:
        signals = await asyncio.wait_for(
            calibration_scan(
                pool,
                min_edge_pct=float(cal_cfg.get("min_edge_pct", 5.0)),
                min_confidence=str(cal_cfg.get("min_confidence", "HIGH")),
                hours_window=float(cal_cfg.get("hours_window", 48)),
                max_signals=int(cal_cfg.get("max_signals", 10)),
            ),
            timeout=30,
        )
    except asyncio.TimeoutError:
        logger.warning("Calibration scan timed out — skipping this cycle")
        return 0
    except Exception as exc:
        logger.warning("Calibration scan failed: %s", exc)
        return 0

    if not signals:
        logger.info("Calibration scan: 0 signals")
        return 0

    # Dedupe Telegram alerts for 12h per market_id (edge doesn't change rapidly)
    fresh: list[CalibrationSignal] = []
    cutoff = now - timedelta(hours=12)
    for s in signals:
        last = _calibration_alerted.get(s.market_id)
        if last and last > cutoff:
            continue
        fresh.append(s)
        _calibration_alerted[s.market_id] = now

    logger.info(
        "Calibration scan: %d signals, %d fresh alerts", len(signals), len(fresh)
    )

    if fresh:
        lines = ["📐 <b>Calibration Trader</b>"]
        for s in fresh[:10]:
            lines.append(
                f"• <b>{s.action}</b>  {s.slug}\n"
                f"    sport={s.sport}  price={s.current_price:.3f}  "
                f"edge={s.edge.edge_pct:+.2f}%  n={s.edge.n} ({s.edge.confidence})"
            )
        try:
            from trading.telegram_confirm import _post  # noqa: PLC0415
            await _post(tg_token, "sendMessage", {
                "chat_id": tg_chat_id,
                "text": "\n".join(lines),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
        except Exception as exc:
            logger.warning("Failed to send calibration alert to Telegram: %s", exc)

    return len(fresh)


async def _run_drift_scan(
    pool: asyncpg.Pool,
    config: dict,
    tg_token: str,
    tg_chat_id: str,
) -> int:
    """T-30 — Cross-sport real-time drift monitor. Alert-only, 15 min cadence.

    Drift alone isn't tradeable; the scan surfaces "something's happening" so a
    human can correlate against news/scoreboards. has_spike candidates are
    sorted lower (likely microstructure noise, not real news).
    """
    global _last_drift_scan

    cfg = config.get("drift_monitor", {})
    if not cfg.get("enabled", False):
        return 0

    interval_sec = float(cfg.get("scan_interval_min", 15)) * 60.0
    now = datetime.now(timezone.utc)
    if _last_drift_scan is not None:
        elapsed = (now - _last_drift_scan).total_seconds()
        if elapsed < interval_sec:
            return 0
    _last_drift_scan = now

    try:
        signals = await asyncio.wait_for(
            drift_scan(
                pool,
                drift_threshold_pct=float(cfg.get("drift_threshold_pct", 4.0)),
                lookback_hours=float(cfg.get("lookback_hours", 6.0)),
                upcoming_hours=float(cfg.get("upcoming_hours", 48)),
                max_signals=int(cfg.get("max_signals", 20)),
            ),
            timeout=30,
        )
    except asyncio.TimeoutError:
        logger.warning("Drift scan timed out — skipping cycle")
        return 0
    except Exception as exc:
        logger.warning("Drift scan failed: %s", exc)
        return 0

    if not signals:
        return 0

    inserted = await persist_drift_signals(pool, signals)

    # Telegram dedupe: 6h per market (drift is slow-moving)
    fresh: list[DriftSignal] = []
    cutoff = now - timedelta(hours=6)
    for s in signals:
        if s.has_spike:
            continue  # noisy — don't alert, just log to DB
        last = _drift_alerted.get(s.market_id)
        if last and last > cutoff:
            continue
        fresh.append(s)
        _drift_alerted[s.market_id] = now

    logger.info(
        "Drift scan: %d signals, %d inserted, %d fresh alerts",
        len(signals), inserted, len(fresh),
    )

    if fresh:
        lines = ["📈 <b>Drift Monitor</b>"]
        for s in fresh[:10]:
            arrow = "↑" if s.direction == "UP" else "↓"
            lines.append(
                f"• {arrow} <b>{s.drift_pct:+.2f}%</b>  {s.sport}  "
                f"{s.past_price:.3f}→{s.current_price:.3f}\n"
                f"    {s.slug}"
            )
        try:
            from trading.telegram_confirm import _post  # noqa: PLC0415
            await _post(tg_token, "sendMessage", {
                "chat_id": tg_chat_id,
                "text": "\n".join(lines),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
        except Exception as exc:
            logger.warning("Failed to send drift alert: %s", exc)

    return len(fresh)


async def _run_spike_scan(
    pool: asyncpg.Pool,
    config: dict,
    tg_token: str,
    tg_chat_id: str,
) -> int:
    """T-31 — Spike follow. Reads spike_events fired by ws_client SpikeTracker.

    Alert-only in v1. Direction is correct (from spike_events.direction) but
    YES/NO mapping to the right Polymarket token is blocked by T-35.
    """
    global _last_spike_scan

    cfg = config.get("spike_follow", {})
    if not cfg.get("enabled", False):
        return 0

    interval_sec = float(cfg.get("scan_interval_min", 5)) * 60.0
    now = datetime.now(timezone.utc)
    if _last_spike_scan is not None:
        elapsed = (now - _last_spike_scan).total_seconds()
        if elapsed < interval_sec:
            return 0
    _last_spike_scan = now

    try:
        signals = await asyncio.wait_for(
            spike_scan(
                pool,
                since_minutes=int(cfg.get("since_minutes", 30)),
                min_magnitude=float(cfg.get("min_magnitude", 0.05)),
                min_steps=int(cfg.get("min_steps", 4)),
                min_hours_to_game=float(cfg.get("min_hours_to_game", 1.0)),
                upcoming_hours=float(cfg.get("upcoming_hours", 48)),
                max_signals=int(cfg.get("max_signals", 10)),
            ),
            timeout=20,
        )
    except asyncio.TimeoutError:
        logger.warning("Spike scan timed out — skipping cycle")
        return 0
    except Exception as exc:
        logger.warning("Spike scan failed: %s", exc)
        return 0

    if not signals:
        return 0

    inserted = await persist_spike_signals(pool, signals)

    # Dedupe by spike_event_id — never alert twice on the same event
    fresh: list[SpikeSignal] = []
    for s in signals:
        if s.spike_event_id in _spike_alerted:
            continue
        fresh.append(s)
        _spike_alerted.add(s.spike_event_id)

    # Cap the dedupe set so it doesn't grow forever (last ~500 events)
    if len(_spike_alerted) > 500:
        # Drop oldest half — rough approximation, set has no order
        for sid in list(_spike_alerted)[:250]:
            _spike_alerted.discard(sid)

    logger.info(
        "Spike scan: %d signals, %d inserted, %d fresh alerts",
        len(signals), inserted, len(fresh),
    )

    if fresh:
        lines = ["⚡ <b>Spike Follow</b>"]
        for s in fresh[:10]:
            arrow = "↑" if s.direction == "up" else "↓"
            lines.append(
                f"• {arrow} <b>{s.magnitude:.3f}</b>  {s.n_steps} steps  "
                f"@ {s.entry_price:.3f}\n"
                f"    {s.sport}  T-{s.hours_to_game:.1f}h  {s.slug}"
            )
        try:
            from trading.telegram_confirm import _post  # noqa: PLC0415
            await _post(tg_token, "sendMessage", {
                "chat_id": tg_chat_id,
                "text": "\n".join(lines),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
        except Exception as exc:
            logger.warning("Failed to send spike alert: %s", exc)

    return len(fresh)


async def _process_prop_signal(
    opp: PropOpportunity,
    pool: asyncpg.Pool,
    executor: ClobExecutor,
    config: dict,
    tg_token: str,
    tg_chat_id: str,
    total_exposure: float,
) -> float:
    """Handle one prop opportunity: risk check → confirm → execute. Returns added exposure."""
    min_depth = config["trading"].get("min_ask_depth_usd", 50)

    # Correlation guard (game_start may be None for some prop markets)
    game_start = getattr(opp, "game_start", None)
    corr_blocked, corr_reason = await correlation_check(
        pool, config, game_start, opp.market_id
    )
    if corr_blocked:
        logger.info("Skipping %s %s — %s", opp.player_name, opp.prop_type, corr_reason)
        return 0.0

    # Fetch token_id early so entry filter can check live market state
    token_id = await _get_token_id(pool, opp.market_id)
    bid, live_ask, live_depth = 0.0, 0.0, 0.0
    if token_id:
        try:
            info = await executor.get_market_info(token_id)
            bid, live_ask, live_depth = info["bid"], info["ask"], info["ask_depth_usd"]
        except Exception:
            pass

    ask_depth = live_depth if live_depth > 0 else opp.ask_depth_usd

    # Entry filter — hard skip if conditions are unworkable
    entry_decision, entry_reason, entry_emoji = check_entry(
        bid=bid,
        ask=live_ask if live_ask > 0 else opp.yes_price,
        signal_price=opp.yes_price,
        ask_depth_usd=ask_depth,
        hours_to_game=opp.hours_until_game,
        min_depth_usd=min_depth,
    )
    if entry_decision == "skip":
        logger.info("Skipping %s %s — entry filter: %s", opp.player_name, opp.prop_type, entry_reason)
        return 0.0

    ok, reason = can_open(config, total_exposure, ask_depth)
    if not ok:
        logger.info("Skipping %s %s: %s", opp.player_name, opp.prop_type, reason)
        return 0.0

    size_usd, size_shares = position_size_by_ev(config, opp.yes_price, opp.roi_pct)

    market_line = f"Market: {entry_emoji} {entry_reason}\n"
    extra = (
        f"{market_line}"
        f"Model: {opp.model_win_rate:.2f}¢  |  ROI: <b>{opp.roi_pct:+.1f}%</b>\n"
        f"Ask depth: ${ask_depth:.0f}  |  Game in {opp.hours_until_game:.1f}h\n"
        f"{opp.prop_type.upper()} {'>'} {opp.threshold}"
    )
    label = f"{opp.player_name} {opp.prop_type} {opp.threshold}"

    await send_signal_alert(
        tg_token, tg_chat_id,
        team_or_player=label,
        signal_type="prop",
        price=opp.yes_price,
        size_usd=size_usd,
        size_shares=size_shares,
        extra_info=extra,
    )

    if not config["trading"].get("enabled", False):
        logger.info("DRY-RUN: would BUY %s %s %s @ %.3f  $%.2f (trading disabled)",
                    opp.player_name, opp.prop_type, opp.threshold, opp.yes_price, size_usd)
        return 0.0

    # token_id already fetched above; verify it's available
    if not token_id:
        msg = f"No token_id for prop market {opp.market_id}"
        await send_error_alert(tg_token, tg_chat_id, msg)
        return 0.0

    order = await executor.buy(token_id, opp.yes_price, size_usd)

    rejected, reason = _is_buy_rejected(order)
    if rejected:
        await log_order(pool, opp.market_id, None, "buy_rejected", order)
        logger.error("PROP BUY rejected for %s: %s", label, reason)
        await send_error_alert(
            tg_token, tg_chat_id,
            f"PROP BUY rejected for {label}: {reason}",
        )
        return 0.0

    order_id_str = order["order_id"]
    actual_shares = order.get("size_shares", size_shares)

    position_id = await open_position(
        pool,
        market_id=opp.market_id,
        slug=opp.slug,
        signal_type="prop",
        token_id=token_id,
        size_usd=size_usd,
        size_shares=actual_shares,
        entry_price=opp.yes_price,
        game_start=None,  # prop markets don't always have game_start
        clob_order_id=order_id_str,
        notes=f"player={opp.player_name} type={opp.prop_type} threshold={opp.threshold}",
    )
    await log_order(pool, opp.market_id, position_id, "buy", order)

    await send_order_confirmation(
        tg_token, tg_chat_id,
        team_or_player=label,
        price=opp.yes_price,
        size_shares=actual_shares,
        size_usd=size_usd,
        order_id=order.get("order_id", ""),
        status=order.get("status", ""),
    )

    return size_usd


async def run_loop(config: dict) -> None:
    """Main bot loop. Runs until interrupted."""
    tg_cfg = config.get("alerts", {})
    tg_token = tg_cfg.get("telegram_bot_token", "")
    tg_chat_id = tg_cfg.get("telegram_chat_id", "")
    trading_cfg = config["trading"]
    scan_interval = trading_cfg.get("scan_interval_sec", 300)

    if not tg_token or not tg_chat_id:
        logger.error("Telegram not configured. Set alerts.telegram_bot_token and telegram_chat_id.")
        sys.exit(1)

    private_key = os.environ.get("POLYGON_PRIVATE_KEY", "")
    if not private_key:
        logger.error("POLYGON_PRIVATE_KEY not set. Create a .env file with POLYGON_PRIVATE_KEY=0x...")
        sys.exit(1)

    trading_enabled = trading_cfg.get("enabled", False)
    if not trading_enabled:
        logger.warning(
            "Trading is DISABLED (trading.enabled=false). "
            "Bot will scan and send Telegram alerts but NOT place orders. "
            "Set trading.enabled: true in settings.yaml to enable real trading."
        )

    dry_run = config["trading"].get("dry_run", False)
    if dry_run:
        logger.warning("DRY RUN mode — no real orders will be placed.")

    pool = await _create_pool(config)
    executor = ClobExecutor(private_key, dry_run=dry_run)

    # Verify CLOB connection
    try:
        balance = await executor.get_balance()
        logger.info("CLOB connected. Balance: $%.2f USDC", balance)
        if trading_enabled and balance < 10:
            logger.warning("Low USDC balance ($%.2f) — trades may fail. Fund your Polygon wallet.", balance)
        _write_bot_state(balance, trading_enabled)
    except Exception as exc:
        logger.error("CLOB connection failed: %s", exc)
        await send_error_alert(tg_token, tg_chat_id, f"Bot startup failed: {exc}")
        sys.exit(1)

    aliases = load_aliases()
    logger.info("Trading bot started. scan_interval=%ds  enabled=%s",
                scan_interval, trading_enabled)

    # Start background tasks
    poller_task = asyncio.create_task(
        poll_order_fills(pool, executor, tg_token, tg_chat_id, config)
    )
    cmd_task = asyncio.create_task(
        handle_commands(pool, executor, tg_token, tg_chat_id)
    )
    sl_task = asyncio.create_task(
        stop_loss_monitor(pool, executor, config, tg_token, tg_chat_id)
    )
    digest_task = asyncio.create_task(
        daily_digest(pool, executor, tg_token, tg_chat_id)
    )
    hb_interval = float(trading_cfg.get("heartbeat_interval_hours", 6.0))
    heartbeat_task = asyncio.create_task(
        heartbeat(pool, executor, tg_token, tg_chat_id, hb_interval)
    )

    async with make_session() as http_session:
        while True:
            try:
                now = datetime.now(timezone.utc)
                logger.info("=== Scan cycle %s ===", now.strftime("%H:%M UTC"))

                # 1. Auto-exit: time-based (game start) + stagnation (price flat)
                await check_and_exit(pool, executor, config, tg_token, tg_chat_id)
                await check_stagnation_exit(pool, executor, config, tg_token, tg_chat_id)

                # 2. Scan tanking signals
                standings = await get_standings(http_session)
                tanking_signals = await scan_tanking_patterns(
                    pool, standings, aliases,
                    min_differential=0.4,
                    hours=trading_cfg.get("hours_window", 24),
                )
                high_signals = [s for s in tanking_signals
                                if s.pattern_strength == "HIGH" and s.recommended_action == "BUY"]
                if high_signals:
                    await enrich_with_lineup_news(high_signals, http_session)

                # 3. Scan MLB pitcher signals
                mlb_cfg = config.get("mlb_pitcher_scanner", {})
                pitcher_signals: list[PitcherSignal] = []
                mlb_aliases: dict[str, str] = {}  # T-35: lifted out so _process_pitcher_signal can see it
                if mlb_cfg.get("enabled", False):
                    try:
                        mlb_aliases = load_mlb_aliases()
                        mlb_fetcher = MLBDataFetcher(http_session)
                        mlb_games = await asyncio.wait_for(
                            mlb_fetcher.get_upcoming_games(
                                hours=mlb_cfg.get("hours_window", 48)
                            ),
                            timeout=60,
                        )
                        if mlb_games:
                            await mlb_fetcher.enrich_all_pitchers(mlb_games)
                            all_pitcher = await scan_pitcher_patterns(
                                pool, mlb_games, mlb_aliases,
                                min_era_diff=mlb_cfg.get("min_era_differential", 1.0),
                                hours=mlb_cfg.get("hours_window", 48),
                            )
                            pitcher_signals = [
                                s for s in all_pitcher
                                if s.signal_strength in ("HIGH", "MODERATE")
                                and s.recommended_action == "BUY"
                                and s.market_id  # has a Polymarket market
                            ]
                    except asyncio.TimeoutError:
                        logger.warning("MLB pitcher scan timed out — skipping this cycle")
                    except Exception as exc:
                        logger.warning("MLB pitcher scan failed: %s", exc)

                # 3b. Scan Rotowire injuries (alert-only, cadence controlled inside)
                try:
                    await _run_injury_scan(
                        pool, http_session, aliases, config, tg_token, tg_chat_id
                    )
                except Exception as exc:
                    logger.warning("Injury scan wrapper failed: %s", exc)

                # 3c. Calibration trader — match active markets against historical edges
                try:
                    await _run_calibration_scan(pool, config, tg_token, tg_chat_id)
                except Exception as exc:
                    logger.warning("Calibration scan wrapper failed: %s", exc)

                # 3d. T-30 — cross-sport drift monitor (alert-only, 15-min cadence)
                try:
                    await _run_drift_scan(pool, config, tg_token, tg_chat_id)
                except Exception as exc:
                    logger.warning("Drift scan wrapper failed: %s", exc)

                # 3e. T-31 — spike follow (alert-only, 5-min cadence, dedupe by event id)
                try:
                    await _run_spike_scan(pool, config, tg_token, tg_chat_id)
                except Exception as exc:
                    logger.warning("Spike scan wrapper failed: %s", exc)

                # 4. Scan prop opportunities (capped at 90s to avoid hanging)
                scanner_cfg = config.get("prop_scanner", {})
                try:
                    prop_opps = await asyncio.wait_for(
                        prop_scan(
                            config,
                            prop_types=["points", "rebounds", "assists"],
                            price_min=scanner_cfg.get("price_min", 0.25),
                            price_max=scanner_cfg.get("price_max", 0.58),
                            min_ev=scanner_cfg.get("alert_min_roi", 5.0) / 100,
                            hours_window=scanner_cfg.get("hours_window", 24),
                        ),
                        timeout=90,
                    )
                except asyncio.TimeoutError:
                    logger.warning("prop_scan timed out after 90s — skipping this cycle")
                    prop_opps = []

                logger.info("Found: %d HIGH tanking BUY, %d MLB pitcher, %d prop opps",
                            len(high_signals), len(pitcher_signals), len(prop_opps))

                # 4. Refresh cached balance for dashboard + low-balance alert
                try:
                    live_balance = await executor.get_balance()
                    _write_bot_state(live_balance, trading_enabled)
                    await _check_low_balance(live_balance, config, tg_token, tg_chat_id)
                except Exception:
                    pass

                # 5. Process each signal: risk check → confirm → execute
                cb_blocked, cb_reason = await circuit_breaker_check(pool, config)
                if cb_blocked:
                    logger.warning("Circuit breaker active: %s", cb_reason)
                    await send_error_alert(tg_token, tg_chat_id,
                                          f"🚨 Circuit breaker: {cb_reason}\nNo new positions until tomorrow.")
                    await asyncio.sleep(scan_interval)
                    continue

                total_exp = await get_total_exposure(pool)

                for signal in high_signals:
                    if await has_position(pool, signal.market_id):
                        continue
                    if _is_skipped(signal.market_id):
                        logger.debug("Decay cache: suppressing %s", signal.motivated_team)
                        continue
                    added = await _process_tanking_signal(
                        signal, pool, executor, config, tg_token, tg_chat_id, total_exp,
                        aliases=aliases,
                    )
                    total_exp += added

                for psig in pitcher_signals:
                    if await has_position(pool, psig.market_id):
                        continue
                    if _is_skipped(psig.market_id):
                        logger.debug("Decay cache: suppressing MLB %s", psig.favored_team)
                        continue
                    added = await _process_pitcher_signal(
                        psig, pool, executor, config, tg_token, tg_chat_id, total_exp,
                        mlb_aliases,
                    )
                    total_exp += added

                for opp in prop_opps:
                    if await has_position(pool, opp.market_id):
                        continue
                    if _is_skipped(opp.market_id):
                        logger.debug("Decay cache: suppressing %s %s", opp.player_name, opp.prop_type)
                        continue
                    added = await _process_prop_signal(
                        opp, pool, executor, config, tg_token, tg_chat_id, total_exp
                    )
                    total_exp += added

            except asyncio.CancelledError:
                logger.info("Bot loop cancelled.")
                break
            except Exception as exc:
                logger.exception("Unexpected error in scan cycle: %s", exc)
                try:
                    await send_error_alert(tg_token, tg_chat_id, f"Scan cycle error: {exc}")
                except Exception:
                    pass

            await asyncio.sleep(scan_interval)

    poller_task.cancel()
    cmd_task.cancel()
    sl_task.cancel()
    digest_task.cancel()
    heartbeat_task.cancel()
    await pool.close()
    logger.info("Bot stopped.")


def main() -> None:
    config = _load_config()
    from config.validate import validate_config  # noqa: PLC0415
    validate_config(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    main_task: asyncio.Task | None = None

    def _shutdown(sig_name: str) -> None:
        logger.info("Received %s — shutting down gracefully…", sig_name)
        if main_task and not main_task.done():
            main_task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown, signal.Signals(sig).name)

    try:
        main_task = loop.create_task(run_loop(config))
        loop.run_until_complete(main_task)
    except asyncio.CancelledError:
        pass   # clean shutdown via signal handler
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
