"""
Signal Digest — Telegram sender

Runs prop scanner + tanking scanner and sends a formatted digest to Telegram.

Usage:
    python scripts/send_signals.py
    python scripts/send_signals.py --dry-run      # print to stdout only
    python scripts/send_signals.py --hours 12     # only games in next 12h
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_PYTHON = str(_ROOT / "venv" / "bin" / "python")
_TELEGRAM_TIMEOUT = aiohttp.ClientTimeout(total=15)


def _load_config() -> dict:
    with (_ROOT / "config" / "settings.yaml").open() as f:
        return yaml.safe_load(f)


def _run_scanner(module: str, extra_args: list[str]) -> str:
    """Run an analytics module as subprocess, return its stdout."""
    cmd = [_PYTHON, "-m", module] + extra_args
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(_ROOT),
        timeout=120,
    )
    return result.stdout.strip()


def _parse_tanking_output(raw: str) -> list[dict]:
    """Parse tabulate table from tanking_scanner stdout into list of dicts."""
    signals = []
    lines = raw.splitlines()
    # Find data rows: lines with real team names (skip headers/separators/empty)
    in_table = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("HIGH") or stripped.startswith("MODERATE"):
            in_table = True
        if not in_table:
            continue
        if not stripped or stripped.startswith("=") or stripped.startswith("-"):
            continue
        parts = [p.strip() for p in stripped.split("  ") if p.strip()]
        if len(parts) >= 8:
            try:
                signals.append({
                    "strength": parts[0],
                    "action": parts[1],
                    "motivated": parts[2],
                    "tanking": parts[3],
                    "diff": parts[4],
                    "price": float(parts[5]),
                    "price_24h": parts[6] if parts[6] != "—" else None,
                    "drift": parts[7] if parts[7] != "—" else None,
                    "hours": parts[8] if len(parts) > 8 else "?",
                })
            except (ValueError, IndexError):
                continue
    return signals


def _format_tanking_telegram(signals: list[dict]) -> str:
    if not signals:
        return ""

    buy = [s for s in signals if s["action"] == "BUY"]
    lines = [f"🏀 <b>NBA TANKING — {len(buy)} BUY / {len(signals) - len(buy)} WATCH</b>\n"]

    for s in signals:
        action = s["action"]
        price = s["price"]
        drift = s.get("drift", "—") or "—"
        hours = s.get("hours", "?")

        if action == "BUY":
            emoji = "✅"
            tip = (
                f"  ↳ Вход: купи <b>YES</b> @ {price:.3f}\n"
                f"  ↳ Выход: за 30 мин до старта или +3–5% profit\n"
                f"  ↳ Риск: max 2–3% депозита"
            )
        elif action in ("SELL", "CLOSE"):
            emoji = "🔴"
            tip = f"  ↳ Закрой позицию {s['motivated']} если держишь"
        else:
            emoji = "👀"
            tip = "  ↳ Следи, не входить пока"

        drift_str = f"  drift {drift}" if drift != "—" else ""
        lines.append(
            f"{emoji} <b>{s['motivated']}</b> vs {s['tanking']}\n"
            f"  Цена: {price:.3f}{drift_str} | diff={s['diff']} | через {hours}\n"
            f"{tip}\n"
        )

    return "\n".join(lines)


def _format_props_telegram(raw: str) -> str:
    if "No opportunities" in raw or not raw:
        return (
            "⚡ <b>PLAYER PROPS</b>\n"
            "Сейчас нет сигналов.\n"
            "Запускай повторно за 6–12ч до tip-off."
        )

    return f"⚡ <b>PLAYER PROPS</b>\n<pre>{raw[:2000]}</pre>"


async def _send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    async with aiohttp.ClientSession(timeout=_TELEGRAM_TIMEOUT) as session:
        resp = await session.post(url, json=payload)
        if resp.status != 200:
            body = await resp.text()
            print(f"[Telegram ERROR {resp.status}] {body[:300]}", file=sys.stderr)
        else:
            print("[Telegram] ✓ Сообщение отправлено")


async def main(args: argparse.Namespace) -> None:
    cfg = _load_config()
    tg_cfg = cfg.get("alerts", {})
    token = tg_cfg.get("telegram_bot_token") or ""
    chat_id = tg_cfg.get("telegram_chat_id") or ""

    if not args.dry_run and (not token or not chat_id):
        print(
            "[ERROR] Telegram не настроен в config/settings.yaml\n"
            "  alerts:\n"
            "    telegram_bot_token: \"YOUR_TOKEN\"\n"
            "    telegram_chat_id: \"YOUR_CHAT_ID\"",
            file=sys.stderr,
        )
        sys.exit(1)

    now_utc = datetime.now(timezone.utc)
    print(f"[{now_utc.strftime('%H:%M UTC')}] Запускаю сканеры...")

    # --- Tanking scanner ---
    print("  tanking_scanner...")
    tanking_raw = _run_scanner(
        "analytics.tanking_scanner",
        ["--hours", str(int(args.hours))],
    )
    tanking_signals = _parse_tanking_output(tanking_raw)

    # --- Prop scanner ---
    print("  prop_scanner...")
    props_raw = _run_scanner(
        "analytics.prop_scanner",
        ["--min-ev", "0.03", "--hours", str(int(args.hours))],
    )

    # --- Build message ---
    header = (
        f"📊 <b>POLYMARKET SIGNALS</b>\n"
        f"🕐 {now_utc.strftime('%Y-%m-%d %H:%M')} UTC\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )

    tanking_block = _format_tanking_telegram(tanking_signals)
    props_block = _format_props_telegram(props_raw)

    risk_note = (
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <b>Риск</b>: не больше 2–3% депозита на сигнал.\n"
        "Tanking: проверь ликвидность перед входом."
    )

    message = header + tanking_block + "\n" + props_block + risk_note

    # --- Output ---
    print("\n" + "=" * 60)
    # Show plain version in terminal
    for line in tanking_raw.splitlines():
        print(line)
    print("=" * 60)

    if not args.dry_run:
        await _send_telegram(token, chat_id, message)
    else:
        print("\n[dry-run] Telegram не отправлен.")
        print("\n--- Telegram message preview ---")
        print(message)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print only, don't send")
    parser.add_argument("--hours", type=float, default=24.0, help="Games within N hours")
    parsed = parser.parse_args()
    asyncio.run(main(parsed))
