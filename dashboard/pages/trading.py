"""
dashboard/pages/trading.py — Trading bot positions page.

Shows:
  - CLOB balance, open exposure, today's P&L
  - Open positions table (fill status, game time, current price, unrealised P&L)
  - Closed positions (last 7 days)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_root = Path(__file__).parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from dashboard import charts, db


# ── DB helpers ────────────────────────────────────────────────────────────────

_STATE_FILE = _root / "logs" / "bot_state.json"


def _read_bot_state() -> dict:
    """Read cached bot state written by the bot every scan cycle."""
    try:
        return json.loads(_STATE_FILE.read_text())
    except Exception:
        return {}


def _open_positions(has_fill_col: bool = False, has_bid_col: bool = False) -> list[dict]:
    fill_col = ", fill_status" if has_fill_col else ""
    bid_col = ", current_bid" if has_bid_col else ""
    return db.query_df(
        f"""
        SELECT id, slug, signal_type, size_usd, size_shares, entry_price,
               entry_ts, game_start, clob_order_id{fill_col}{bid_col}, notes
        FROM open_positions
        WHERE status = 'open'
        ORDER BY entry_ts DESC
        """
    )


def _closed_positions(days: int = 7) -> list[dict]:
    return db.query_df(
        """
        SELECT id, slug, signal_type, size_usd, size_shares,
               entry_price, exit_price, entry_ts, exit_ts, pnl_usd
        FROM open_positions
        WHERE status = 'closed'
          AND exit_ts >= NOW() - (%s * INTERVAL '1 day')
        ORDER BY exit_ts DESC
        """,
        (days,),
    )


def _today_pnl() -> float:
    row = db.query_one(
        """
        SELECT COALESCE(SUM(pnl_usd), 0) AS total
        FROM open_positions
        WHERE status = 'closed'
          AND exit_ts >= CURRENT_DATE
        """
    )
    return float(row["total"]) if row else 0.0


def _alltime_stats() -> dict:
    row = db.query_one(
        """
        SELECT COALESCE(SUM(pnl_usd), 0)          AS total_pnl,
               COUNT(*)                             AS total_cnt,
               COUNT(*) FILTER (WHERE pnl_usd > 0) AS wins,
               COALESCE(AVG(pnl_usd), 0)           AS avg_pnl
        FROM open_positions
        WHERE status = 'closed'
        """
    )
    if not row:
        return {"total_pnl": 0.0, "total_cnt": 0, "wins": 0, "avg_pnl": 0.0}
    cnt = int(row["total_cnt"] or 0)
    wins = int(row["wins"] or 0)
    return {
        "total_pnl": float(row["total_pnl"] or 0),
        "total_cnt": cnt,
        "wins": wins,
        "win_rate": f"{wins/cnt*100:.0f}%" if cnt else "—",
        "avg_pnl": float(row["avg_pnl"] or 0),
    }


def _total_open_exposure() -> float:
    row = db.query_one(
        "SELECT COALESCE(SUM(size_usd), 0) AS total FROM open_positions WHERE status='open'"
    )
    return float(row["total"]) if row else 0.0


def _has_column(column: str) -> bool:
    """Check if a column exists in open_positions using pg_attribute (reliable, schema-aware)."""
    row = db.query_one(
        """
        SELECT 1 FROM pg_attribute
        WHERE attrelid = 'open_positions'::regclass
          AND attname = %s
          AND NOT attisdropped
        """,
        (column,),
    )
    return row is not None


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fill_badge(fill_status: str | None) -> str:
    status = (fill_status or "pending").lower()
    return {
        "filled":         "✅ Filled",
        "pending":        "⏳ Pending",
        "force_cancelled": "⏰ Cancelled",
        "cancelled":      "❌ Cancelled",
        "stale":          "🗑 Stale",
        "no_order":       "⚠️ No order",
    }.get(status, f"❓ {status}")


def _hours_to(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    now = datetime.now(timezone.utc)
    ts = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    diff = (ts - now).total_seconds() / 3600
    if diff < 0:
        return "started"
    if diff < 1:
        return f"{int(diff * 60)}m"
    return f"{diff:.1f}h"


# ── Render ────────────────────────────────────────────────────────────────────

def render() -> None:
    st.title("Trading")
    st.caption("Live bot positions — order fill status + P&L")

    # ── Sidebar: auto-refresh ────────────────────────────────────────────────
    with st.sidebar:
        st.subheader("Refresh")
        auto = st.checkbox("Auto-refresh (30s)", value=False)
        if st.button("Refresh now"):
            st.rerun()

    has_fill_col = _has_column("fill_status")
    has_bid_col = _has_column("current_bid")
    bot_state = _read_bot_state()

    # ── Top metrics ──────────────────────────────────────────────────────────
    exposure = _total_open_exposure()
    today_pnl = _today_pnl()

    col1, col2, col3, col4, col5 = st.columns(5)
    balance = bot_state.get("balance_usd")
    col1.metric("CLOB Balance", f"${balance:.2f}" if balance is not None else "—")
    col2.metric("Open Exposure", f"${exposure:.2f}")
    col3.metric("P&L Today", f"${today_pnl:+.2f}")

    try:
        import yaml
        cfg = yaml.safe_load((_root / "config" / "settings.yaml").read_text())
        budget = cfg["trading"]["budget_usd"]
        max_exp = budget * cfg["trading"]["max_total_exposure_pct"] / 100
        col4.metric("Budget", f"${budget:.0f}")
        col5.metric("Max Exposure", f"${max_exp:.0f}")
    except Exception:
        pass

    updated = bot_state.get("updated_at", "")
    if updated:
        st.caption(f"Bot state last updated: {updated[:19].replace('T', ' ')} UTC")

    # ── All-time stats ────────────────────────────────────────────────────────
    stats = _alltime_stats()
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("All-time P&L", f"${stats['total_pnl']:+.2f}")
    s2.metric("Win Rate", stats["win_rate"])
    s3.metric("Total Trades", stats["total_cnt"])
    s4.metric("Avg P&L / trade", f"${stats['avg_pnl']:+.2f}")

    st.divider()

    # ── Open positions ────────────────────────────────────────────────────────
    st.subheader("Open Positions")
    open_pos = _open_positions(has_fill_col=has_fill_col, has_bid_col=has_bid_col)

    if not open_pos:
        st.info("No open positions.")
    else:
        rows = []
        for p in open_pos:
            fill_col = p.get("fill_status") if has_fill_col else None
            entry = float(p["entry_price"] or 0)
            shares = float(p["size_shares"] or 0)
            current_bid = p.get("current_bid")

            if current_bid is not None and entry > 0 and shares > 0:
                upnl = (float(current_bid) - entry) * shares
            else:
                upnl = None

            rows.append({
                "ID": p["id"],
                "Market": (p["slug"] or "")[:40],
                "Signal": p["signal_type"] or "—",
                "Fill": _fill_badge(fill_col),
                "Entry $": entry,
                "Bid $": float(current_bid) if current_bid is not None else None,
                "Size USD": float(p["size_usd"] or 0),
                "Unrealized": upnl if upnl is not None else float("nan"),
                "Game in": _hours_to(p["game_start"]),
                "Order ID": (p["clob_order_id"] or "—")[:12],
            })

        df = pd.DataFrame(rows)

        def _upnl_color(val: float) -> str:
            if pd.isna(val):
                return ""
            return "color: green" if val >= 0 else "color: red"

        st.dataframe(
            df.style.map(_upnl_color, subset=["Unrealized"]),
            use_container_width=True,
            hide_index=True,
        )
        st.caption(f"{len(open_pos)} open position(s)  |  Total: ${exposure:.2f}")

    st.divider()

    # ── Closed positions (last 7 days) ────────────────────────────────────────
    st.subheader("Closed Positions — last 7 days")
    closed = _closed_positions(days=7)

    if not closed:
        st.info("No closed positions in the last 7 days.")
    else:
        rows = []
        for p in closed:
            pnl = float(p["pnl_usd"] or 0)
            rows.append({
                "Market": (p["slug"] or "")[:40],
                "Signal": p["signal_type"] or "—",
                "Entry $": float(p["entry_price"] or 0),
                "Exit $": float(p["exit_price"] or 0),
                "Size USD": float(p["size_usd"] or 0),
                "P&L": pnl,
                "Closed": p["exit_ts"].strftime("%m-%d %H:%M") if p["exit_ts"] else "—",
            })
        df = pd.DataFrame(rows)

        def _pnl_color(val: float) -> str:
            return "color: green" if val >= 0 else "color: red"

        st.dataframe(
            df.style.map(_pnl_color, subset=["P&L"]),
            use_container_width=True,
            hide_index=True,
        )

        total_pnl = sum(float(p["pnl_usd"] or 0) for p in closed)
        wins = sum(1 for p in closed if float(p["pnl_usd"] or 0) >= 0)
        st.caption(
            f"{len(closed)} closed  |  Win rate: {wins}/{len(closed)}  |  Total P&L: ${total_pnl:+.2f}"
        )

    st.divider()

    # ── P&L equity curve ──────────────────────────────────────────────────────
    st.subheader("P&L Equity Curve")
    days_back = st.select_slider(
        "Window", options=[7, 14, 30, 60, 90], value=30, key="eq_days"
    )
    curve_data = db.pnl_equity_curve(days=days_back)
    st.plotly_chart(charts.equity_curve(curve_data), use_container_width=True)

    st.divider()

    # ── Order history ─────────────────────────────────────────────────────────
    st.subheader("Order History")
    order_limit = st.select_slider("Show last N orders", options=[20, 50, 100], value=50, key="ord_limit")
    orders = db.order_history(limit=order_limit)

    if not orders:
        st.info("No orders in the audit log yet.")
    else:
        action_emoji = {
            "buy": "🟢", "sell": "🔴", "cancel": "⚫",
            "stop_loss": "🛑", "take_profit": "💰",
            "reprice": "🔄", "buy_retry": "🔁",
        }
        rows = []
        for o in orders:
            action = o["action"] or "—"
            rows.append({
                "Time": o["created_at"].strftime("%m-%d %H:%M") if o["created_at"] else "—",
                "Action": f"{action_emoji.get(action, '❓')} {action}",
                "Market": (o["slug"] or "")[:35],
                "Signal": o["signal_type"] or "—",
                "Price $": float(o["price"] or 0) if o["price"] else None,
                "Size USD": float(o["size_usd"] or 0) if o["size_usd"] else None,
                "Status": o["status"] or "—",
                "Order ID": (o["clob_order_id"] or "—")[:14],
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.caption(f"{len(orders)} orders shown")

    # ── Auto-refresh (must be last — sleeps then reruns) ──────────────────────
    if auto:
        time.sleep(30)
        st.rerun()
