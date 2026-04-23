"""
dashboard/pages/health.py — System health page.

Shows: collector heartbeat (last snapshot age), per-market snapshot counts
for the last 24h, and recent data_gaps with their reasons.
"""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from dashboard import charts, db


def _heartbeat_age(last_ts) -> tuple[str, str]:
    """Return (age_str, status) for a last-seen timestamp."""
    if last_ts is None:
        return "never", "error"
    # Ensure timezone-aware
    if hasattr(last_ts, "tzinfo") and last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    age_sec = (datetime.now(timezone.utc) - last_ts).total_seconds()
    if age_sec < 120:
        return f"{int(age_sec)}s ago", "ok"
    if age_sec < 600:
        return f"{int(age_sec/60)}m ago", "warn"
    return f"{int(age_sec/60)}m ago", "error"


def render() -> None:
    st.title("System Health")
    st.caption("Collector heartbeat, snapshot coverage, and data gaps")

    # ── Heartbeat ─────────────────────────────────────────────────────────
    rate = db.snapshot_rate_last_hour()
    last_ts = rate.get("last_ts") if rate else None
    age_str, status = _heartbeat_age(last_ts)

    icon = {"ok": "🟢", "warn": "🟡", "error": "🔴"}[status]
    col1, col2, col3 = st.columns(3)
    col1.metric("Collector", f"{icon} {age_str}")
    col2.metric("Snapshots (last 1h)", int(rate["snapshots"] or 0) if rate else 0)
    col3.metric("Markets tracked (1h)", int(rate["markets"] or 0) if rate else 0)

    if status == "error":
        st.error(
            "Last snapshot is more than 10 minutes old. "
            "Check if the collector is running: `tail -f logs/collector.log`"
        )
    elif status == "warn":
        st.warning("Last snapshot is 2–10 minutes old — collector may be slow or reconnecting.")

    st.divider()

    # ── Snapshot counts ───────────────────────────────────────────────────
    st.subheader("Snapshot coverage — last 24h")
    snap_rows = db.snapshot_counts_per_market(hours=24)
    if snap_rows:
        st.plotly_chart(
            charts.snapshot_count_bar(snap_rows, top_n=25),
            use_container_width=True,
        )
        df_snap = pd.DataFrame(snap_rows)
        for col in ["last_ts"]:
            if col in df_snap.columns:
                df_snap[col] = pd.to_datetime(df_snap[col]).dt.strftime("%m-%d %H:%M")
        with st.expander("Full table"):
            st.dataframe(
                df_snap[["market_id", "question", "sport", "snapshots", "last_ts"]],
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.info("No price snapshots in the last 24h.")

    st.divider()

    # ── Data gaps ─────────────────────────────────────────────────────────
    st.subheader("Recent data gaps")
    gap_rows = db.data_gaps_recent(limit=50)
    if gap_rows:
        df_gaps = pd.DataFrame(gap_rows)
        for col in ["gap_start", "gap_end"]:
            if col in df_gaps.columns:
                df_gaps[col] = pd.to_datetime(df_gaps[col]).dt.strftime("%m-%d %H:%M")
        if "gap_minutes" in df_gaps.columns:
            df_gaps["gap_minutes"] = pd.to_numeric(df_gaps["gap_minutes"], errors="coerce").round(1)

        # Summary by reason
        if "reason" in df_gaps.columns:
            reason_counts = df_gaps["reason"].value_counts().reset_index()
            reason_counts.columns = ["reason", "count"]
            st.write("**By reason:**")
            st.dataframe(reason_counts, use_container_width=True, hide_index=True)

        st.write(f"**{len(df_gaps)} gaps** recorded")
        show_cols = ["market_id", "question", "gap_start", "gap_end", "gap_minutes", "reason"]
        show_cols = [c for c in show_cols if c in df_gaps.columns]
        st.dataframe(df_gaps[show_cols], use_container_width=True, hide_index=True)
    else:
        st.success("No data gaps recorded.")
