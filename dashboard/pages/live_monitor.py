"""
dashboard/pages/live_monitor.py — Live data collection monitor.

Shows real-time snapshot ingestion, recent trades, per-market activity,
and collector process health. Auto-refreshes every 15 seconds.
"""

from __future__ import annotations

import sys
from pathlib import Path
_root = Path(__file__).parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import subprocess
import time
from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard import db


# ── Helpers ───────────────────────────────────────────────────────────────────

def _collector_running() -> tuple[bool, list[str]]:
    """Check if main.py collector process is running. Returns (running, pids).

    Uses case-insensitive match (-i) because macOS launches Python as
    'Python' (capital P) from the framework path, not 'python'.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-if", "main\\.py"],
            capture_output=True, text=True,
        )
        pids = [p.strip() for p in result.stdout.strip().splitlines() if p.strip()]
        return bool(pids), pids
    except Exception:
        return False, []


@st.cache_data(ttl=15)
def _recent_snapshots(minutes: int = 5) -> list[dict]:
    return db.query_df("""
        SELECT ps.ts, ps.market_id, m.question, m.sport,
               ps.mid_price, ps.spread, ps.bid_depth, ps.ask_depth, ps.volume_24h
        FROM price_snapshots ps
        LEFT JOIN markets m ON m.id = ps.market_id
        WHERE ps.ts > NOW() - INTERVAL '%s minutes'
        ORDER BY ps.ts DESC
        LIMIT 200
    """ % minutes)


@st.cache_data(ttl=15)
def _recent_trades(minutes: int = 10) -> list[dict]:
    return db.query_df("""
        SELECT t.ts, t.market_id, m.question, m.sport,
               t.price, t.size, t.side
        FROM trades t
        LEFT JOIN markets m ON m.id = t.market_id
        WHERE t.ts > NOW() - INTERVAL '%s minutes'
        ORDER BY t.ts DESC
        LIMIT 100
    """ % minutes)


@st.cache_data(ttl=15)
def _snapshot_rate_history() -> list[dict]:
    """Snapshots per minute for the last 30 minutes."""
    return db.query_df("""
        SELECT date_trunc('minute', ts) AS minute,
               COUNT(*) AS snapshots,
               COUNT(DISTINCT market_id) AS markets
        FROM price_snapshots
        WHERE ts > NOW() - INTERVAL '30 minutes'
        GROUP BY date_trunc('minute', ts)
        ORDER BY minute ASC
    """)


@st.cache_data(ttl=15)
def _per_market_last_seen() -> list[dict]:
    return db.query_df("""
        SELECT ps.market_id, m.question, m.sport,
               MAX(ps.ts) AS last_ts,
               COUNT(*) FILTER (WHERE ps.ts > NOW() - INTERVAL '10 minutes') AS snaps_10m,
               AVG(ps.mid_price) FILTER (WHERE ps.ts > NOW() - INTERVAL '10 minutes') AS avg_price_10m
        FROM price_snapshots ps
        LEFT JOIN markets m ON m.id = ps.market_id
        WHERE ps.ts > NOW() - INTERVAL '1 hour'
        GROUP BY ps.market_id, m.question, m.sport
        ORDER BY last_ts DESC
        LIMIT 50
    """)


def _age_str(ts) -> str:
    if ts is None:
        return "—"
    if hasattr(ts, "tzinfo") and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age < 60:
        return f"{int(age)}s ago"
    if age < 3600:
        return f"{int(age/60)}m ago"
    return f"{int(age/3600)}h ago"


def _snapshots_per_min_chart(rows: list[dict]) -> go.Figure:
    if not rows:
        return go.Figure().update_layout(title="No data yet")
    df = pd.DataFrame(rows)
    df["minute"] = pd.to_datetime(df["minute"])
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=df["minute"], y=df["snapshots"].astype(int),
        name="Snapshots", marker_color="#4CAF50",
    ))
    fig.add_trace(go.Scatter(
        x=df["minute"], y=df["markets"].astype(int),
        name="Markets", mode="lines+markers",
        line={"color": "#FFC107", "width": 2},
        yaxis="y2",
    ))
    fig.update_layout(
        title="Snapshot ingestion — last 30 min",
        xaxis_title="Time (UTC)",
        yaxis={"title": "Snapshots / min"},
        yaxis2={"title": "Markets", "overlaying": "y", "side": "right",
                "showgrid": False},
        plot_bgcolor="#0E1117",
        paper_bgcolor="#0E1117",
        font={"color": "#FAFAFA"},
        legend={"orientation": "h", "y": -0.2},
        height=280,
        bargap=0.1,
    )
    return fig


# ── Main render ───────────────────────────────────────────────────────────────

def render() -> None:
    st.title("Live Monitor")
    st.caption("Real-time data collection stream — auto-refreshes every 15 seconds")

    # ── Auto-refresh control ──────────────────────────────────────────────
    col_ctrl1, col_ctrl2, col_ctrl3 = st.columns([2, 2, 4])
    with col_ctrl1:
        live = st.toggle("Live (15s refresh)", value=True, key="live_monitor_toggle")
    with col_ctrl2:
        if st.button("🔄 Refresh now", key="live_refresh_btn"):
            st.cache_data.clear()
            st.rerun()
    with col_ctrl3:
        st.caption(f"Last refresh: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")

    # Schedule next rerun if live mode is on
    if live:
        if "live_last_refresh" not in st.session_state:
            st.session_state["live_last_refresh"] = time.monotonic()
        if time.monotonic() - st.session_state["live_last_refresh"] >= 15:
            st.cache_data.clear()
            st.session_state["live_last_refresh"] = time.monotonic()
            st.rerun()
        # Schedule rerun via fragment trick — show countdown
        remaining = max(0, 15 - int(time.monotonic() - st.session_state["live_last_refresh"]))
        st.caption(f"Next refresh in {remaining}s")

    st.divider()

    # ── Collector status ──────────────────────────────────────────────────
    running, pids = _collector_running()
    rate = db.snapshot_rate_last_hour()
    last_ts = rate.get("last_ts") if rate else None

    if last_ts and hasattr(last_ts, "tzinfo") and last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    age_sec = (datetime.now(timezone.utc) - last_ts).total_seconds() if last_ts else 9999

    if running:
        status_icon, status_text = "🟢", f"Running (PID {', '.join(pids)})"
    elif age_sec < 120:
        status_icon, status_text = "🟡", "Process not found but data is fresh"
    else:
        status_icon, status_text = "🔴", "Not running — no recent snapshots"

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Collector", f"{status_icon} {status_text[:30]}")
    c2.metric("Last snapshot", _age_str(last_ts))
    c3.metric("Snapshots (1h)", int(rate["snapshots"] or 0) if rate else 0)
    c4.metric("Markets (1h)", int(rate["markets"] or 0) if rate else 0)

    if not running and age_sec > 300:
        st.error(
            "Collector appears stopped. Restart with: `nohup python main.py > logs/collector.log 2>&1 &`"
        )

    st.divider()

    # ── Ingestion rate chart ──────────────────────────────────────────────
    rate_history = _snapshot_rate_history()
    st.plotly_chart(_snapshots_per_min_chart(rate_history), use_container_width=True)

    st.divider()

    # ── Recent snapshot feed ──────────────────────────────────────────────
    col_left, col_right = st.columns([3, 2])

    with col_left:
        st.subheader("Recent snapshots (last 5 min)")
        snap_rows = _recent_snapshots(minutes=5)
        if snap_rows:
            df_snap = pd.DataFrame(snap_rows)
            df_snap["ts"] = pd.to_datetime(df_snap["ts"]).dt.strftime("%H:%M:%S")
            df_snap["mid_price"] = pd.to_numeric(df_snap["mid_price"], errors="coerce").round(4)
            df_snap["spread"] = pd.to_numeric(df_snap["spread"], errors="coerce").round(4)
            df_snap["question_short"] = df_snap["question"].str[:50] if "question" in df_snap.columns else df_snap["market_id"]
            show = df_snap[["ts", "sport", "question_short", "mid_price", "spread"]].rename(
                columns={"ts": "Time", "sport": "Sport", "question_short": "Market",
                         "mid_price": "Price", "spread": "Spread"}
            )
            st.dataframe(show, use_container_width=True, hide_index=True, height=340)
            st.caption(f"{len(snap_rows)} snapshots in last 5 min")
        else:
            st.info("No snapshots in last 5 min — collector may not be running.")

    with col_right:
        st.subheader("Recent trades (last 10 min)")
        trade_rows = _recent_trades(minutes=10)
        if trade_rows:
            df_trade = pd.DataFrame(trade_rows)
            df_trade["ts"] = pd.to_datetime(df_trade["ts"]).dt.strftime("%H:%M:%S")
            df_trade["price"] = pd.to_numeric(df_trade["price"], errors="coerce").round(4)
            df_trade["size"] = pd.to_numeric(df_trade["size"], errors="coerce").round(2)
            df_trade["q"] = df_trade["question"].str[:30] if "question" in df_trade.columns else df_trade["market_id"]

            def _side_color(series):
                return ["color: #4CAF50" if v == "buy" else "color: #F44336" if v == "sell" else ""
                        for v in series]

            show_t = df_trade[["ts", "q", "price", "size", "side"]].rename(
                columns={"ts": "Time", "q": "Market", "price": "Price",
                         "size": "Size", "side": "Side"}
            )
            styled = show_t.style.apply(_side_color, subset=["Side"])
            st.dataframe(styled, use_container_width=True, hide_index=True, height=340)
            st.caption(f"{len(trade_rows)} trades in last 10 min")
        else:
            st.info("No trades recorded yet (WebSocket trade subscription may be inactive).")

    st.divider()

    # ── Per-market activity table ─────────────────────────────────────────
    st.subheader("Per-market activity (last 1h)")
    market_rows = _per_market_last_seen()
    if market_rows:
        df_m = pd.DataFrame(market_rows)
        df_m["last_seen"] = df_m["last_ts"].apply(_age_str)
        df_m["avg_price_10m"] = pd.to_numeric(df_m["avg_price_10m"], errors="coerce").round(4)
        df_m["snaps_10m"] = pd.to_numeric(df_m["snaps_10m"], errors="coerce").fillna(0).astype(int)
        df_m["question_short"] = df_m["question"].str[:60] if "question" in df_m.columns else df_m["market_id"]

        def _stale_color(_series):
            # Color is derived from raw timestamps, not the formatted "Last seen" strings
            result = []
            for ts in df_m["last_ts"]:
                if ts is None:
                    result.append("color: #F44336")
                    continue
                if hasattr(ts, "tzinfo") and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - ts).total_seconds()
                result.append("color: #F44336" if age > 600 else "color: #4CAF50" if age < 120 else "color: #FFC107")
            return result

        show_m = df_m[["sport", "question_short", "last_seen", "snaps_10m", "avg_price_10m"]].rename(
            columns={"sport": "Sport", "question_short": "Market", "last_seen": "Last seen",
                     "snaps_10m": "Snaps (10m)", "avg_price_10m": "Avg price (10m)"}
        )
        styled_m = show_m.style.apply(_stale_color, subset=["Last seen"])
        st.dataframe(styled_m, use_container_width=True, hide_index=True)
    else:
        st.info("No market activity in the last hour.")

    # ── Live refresh footer ───────────────────────────────────────────────
    if live:
        # Trigger rerun after 15s using a hidden time check
        st.session_state["live_last_refresh"] = st.session_state.get("live_last_refresh", time.monotonic())
