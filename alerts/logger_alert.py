"""Alerts via logging (console + file) with optional Slack and Telegram notifications."""

import logging

import aiohttp

from collector.network import make_session

logger = logging.getLogger("alerts")

# POST timeout — keep short so a slow webhook never blocks the collector
_SLACK_TIMEOUT = aiohttp.ClientTimeout(total=5)
_TELEGRAM_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Only escalate WS reconnect attempts to Slack/Telegram once this many consecutive
# failures have occurred (avoids noise from single transient disconnects)
_WS_RECONNECT_SLACK_THRESHOLD = 3


class LoggerAlert:
    """Alert dispatcher: always logs; optionally posts to Slack and/or Telegram."""

    def __init__(self, config: dict):
        self.config = config
        alerts_cfg = config.get("alerts", {})
        self._slack_url: str | None = alerts_cfg.get("slack_webhook_url") or None
        self._tg_token: str | None = alerts_cfg.get("telegram_bot_token") or None
        self._tg_chat_id: str | None = alerts_cfg.get("telegram_chat_id") or None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _telegram(self, text: str, parse_mode: str = "HTML") -> None:
        """POST message to Telegram via Bot API.

        Failures are logged as warnings and never propagate.
        """
        if not self._tg_token or not self._tg_chat_id:
            return
        url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
        payload = {
            "chat_id": self._tg_chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        try:
            async with make_session(timeout=_TELEGRAM_TIMEOUT) as session:
                resp = await session.post(url, json=payload)
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"Telegram API returned {resp.status}: {body[:200]}")
        except Exception as exc:
            logger.warning(f"Telegram alert failed: {exc}")

    async def _slack(self, text: str) -> None:
        """POST text to the configured Slack Incoming Webhook.

        Failures are logged as warnings and never propagate — a broken Slack
        webhook must never crash the data collector.
        """
        if not self._slack_url:
            return
        try:
            async with make_session(timeout=_SLACK_TIMEOUT) as session:
                resp = await session.post(self._slack_url, json={"text": text})
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"Slack webhook returned {resp.status}: {body[:200]}")
        except Exception as exc:
            logger.warning(f"Slack alert failed: {exc}")

    async def send(self, message: str, level: str = "info"):
        """Log message at the given level (default: info)."""
        getattr(logger, level, logger.info)(message)

    # ------------------------------------------------------------------
    # Alert events
    # ------------------------------------------------------------------

    async def phase0_complete(self, total: int, go: int, marginal: int, no_go: int):
        await self.send(
            f"\n{'='*60}\n"
            f"  PHASE 0 COMPLETE\n"
            f"  Total markets: {total}\n"
            f"  GO: {go}  |  MARGINAL: {marginal}  |  NO_GO: {no_go}\n"
            f"{'='*60}"
        )
        await self._slack(
            f"*Phase 0 complete* — {total} markets scanned\n"
            f"GO: {go}  |  MARGINAL: {marginal}  |  NO_GO: {no_go}"
        )

    async def collector_started(self, market_count: int):
        await self.send(f"Collector started. Tracking {market_count} markets.")
        await self._slack(f"*Collector started* — tracking {market_count} markets")

    async def snapshot_saved(self, market_id: str, mid_price: float, spread: float):
        # Debug-only; not sent to Slack to avoid noise
        await self.send(
            f"Snapshot: {market_id[:16]}... mid={mid_price:.4f} spread={spread:.4f}",
            "debug",
        )

    async def trade_saved(self, market_id: str, price: float, size: float, side: str):
        # Debug-only; not sent to Slack to avoid noise
        await self.send(
            f"Trade: {market_id[:16]}... {side} {size:.2f} @ {price:.4f}",
            "debug",
        )

    async def gap_detected(self, market_id: str, minutes: float):
        await self.send(
            f"⚠ GAP: {market_id[:16]}... {minutes:.1f} min gap",
            "warning",
        )
        await self._slack(
            f"⚠ *Data gap* detected\n"
            f"Market: `{market_id[:32]}`  |  Duration: {minutes:.1f} min"
        )

    async def spike_detected(
        self, market_id: str, direction: str, magnitude: float, n_steps: int
    ):
        """Alert on a finalized price spike detected by the real-time SpikeTracker."""
        await self.send(
            f"SPIKE {direction.upper()} {market_id[:32]} "
            f"magnitude={magnitude:.4f} steps={n_steps}"
        )
        await self._slack(
            f"📈 *Price spike detected*\n"
            f"Market: `{market_id[:32]}`\n"
            f"Direction: {direction.upper()}  |  Magnitude: {magnitude:.4f}  |  Steps: {n_steps}"
        )

    async def ws_reconnect(self, attempt: int, delay: float):
        """Alert when the WebSocket enters a reconnect cycle.

        Slack notifications are suppressed for the first few attempts to avoid
        noise from brief connectivity blips.
        """
        await self.send(
            f"WS reconnecting (attempt #{attempt}, retry in {delay:.0f}s)",
            "warning",
        )
        if attempt >= _WS_RECONNECT_SLACK_THRESHOLD:
            await self._slack(
                f"⚡ *WebSocket reconnect loop*\n"
                f"Attempt #{attempt} — next retry in {delay:.0f}s"
            )

    async def market_settled(self, market_id: str, slug: str):
        await self.send(f"Market settled: {slug}")

    async def prop_opportunity(self, opportunities: list) -> None:
        """Alert for new positive-EV player prop opportunities found by the scanner.

        Logs each opportunity individually; sends a single Slack + Telegram summary.
        """
        for opp in opportunities:
            await self.send(
                f"PROP {opp.prop_type.upper()} {opp.player_name} "
                f"{opp.threshold} @ {opp.yes_price:.3f} "
                f"ROI={opp.roi_pct:+.1f}% game_in={opp.hours_until_game:.1f}h"
            )

        if not opportunities:
            return

        best = opportunities[0]
        slack_lines = [
            f"🏀 *{len(opportunities)} new prop {'opportunity' if len(opportunities) == 1 else 'opportunities'}*",
            f"Best: *{best.player_name}* {best.prop_type} {best.threshold} "
            f"@ {best.yes_price:.3f} → ROI *{best.roi_pct:+.1f}%* "
            f"(game in {best.hours_until_game:.1f}h)",
        ]
        if len(opportunities) > 1:
            slack_lines.append(
                "Others: " + "  |  ".join(
                    f"{o.player_name} {o.prop_type} {o.threshold} {o.roi_pct:+.0f}%"
                    for o in opportunities[1:4]
                )
            )
        await self._slack("\n".join(slack_lines))

        tg_lines = [
            f"🏀 <b>{len(opportunities)} prop opportunit{'y' if len(opportunities) == 1 else 'ies'}</b>",
            f"Best: <b>{best.player_name}</b> {best.prop_type} {best.threshold} "
            f"@ {best.yes_price:.2f}¢ → ROI <b>{best.roi_pct:+.1f}%</b> "
            f"(game in {best.hours_until_game:.1f}h)",
        ]
        if len(opportunities) > 1:
            for o in opportunities[1:4]:
                tg_lines.append(f"  • {o.player_name} {o.prop_type} {o.threshold} {o.roi_pct:+.0f}%")
        await self._telegram("\n".join(tg_lines))

    async def tanking_signals(self, signals: list) -> None:
        """Alert for tanking pattern signals. Sends Slack + Telegram summary."""
        if not signals:
            return

        buy_signals = [s for s in signals if s.recommended_action == "BUY"]

        await self.send(
            f"TANKING: {len(signals)} signals, {len(buy_signals)} BUY "
            f"({', '.join(s.motivated_team for s in buy_signals[:3])})"
        )

        tg_lines = [f"🏀 <b>NBA Tanking Signals — {len(buy_signals)} BUY</b>"]
        for s in signals[:6]:
            action_emoji = "✅ BUY" if s.recommended_action == "BUY" else "👀 WATCH"
            drift_str = ""
            if s.actual_drift is not None:
                drift_str = f"  drift {s.actual_drift:+.3f}"
            tg_lines.append(
                f"{action_emoji} <b>{s.motivated_team}</b> vs {s.tanking_team} "
                f"@ {s.current_price:.3f}{drift_str} "
                f"(in {s.hours_to_game:.0f}h)"
            )
        await self._telegram("\n".join(tg_lines))
