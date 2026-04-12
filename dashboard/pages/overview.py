"""
dashboard/pages/overview.py — Portfolio overview page.

Shows: total markets by sport, cost verdict breakdown, snapshot rate,
and top 10 movers in the last 24 hours.
"""

import sys
from pathlib import Path
_root = Path(__file__).parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import streamlit as st
import pandas as pd
from dashboard import db, charts


def render() -> None:
    st.title("Overview")
    st.caption("Polymarket Sports — market universe at a glance")

    # ── Snapshot heartbeat ────────────────────────────────────────────────
    rate = db.snapshot_rate_last_hour()
    if rate:
        last_ts = rate.get("last_ts")
        ts_str = last_ts.strftime("%Y-%m-%d %H:%M UTC") if last_ts else "—"
        col1, col2, col3 = st.columns(3)
        col1.metric("Snapshots (last 1h)", int(rate["snapshots"] or 0))
        col2.metric("Markets tracked (1h)", int(rate["markets"] or 0))
        col3.metric("Last snapshot", ts_str)
    else:
        st.warning("No snapshot data — is the collector running?")

    st.divider()

    # ── Markets by sport ──────────────────────────────────────────────────
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("Markets by sport")
        sport_rows = db.market_counts_by_sport()
        if sport_rows:
            df_sport = pd.DataFrame(sport_rows)
            st.dataframe(
                df_sport.rename(columns={"sport": "Sport", "total": "Total", "active": "Active"}),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No markets in DB yet.")

    with col_right:
        st.subheader("Cost verdict breakdown")
        verdict_rows = db.verdict_counts()
        if verdict_rows:
            st.plotly_chart(
                charts.verdict_pie(verdict_rows),
                use_container_width=True,
            )
        else:
            st.info("No cost analysis data yet — run `python cost_analyzer.py`")

    st.divider()

    # ── Top movers ────────────────────────────────────────────────────────
    st.subheader("Top movers — 24h absolute price change")
    movers = db.top_movers(limit=15)
    if movers:
        st.plotly_chart(charts.movers_bar(movers), use_container_width=True)
        with st.expander("Raw data"):
            df_movers = pd.DataFrame(movers)
            df_movers["price_now"] = df_movers["price_now"].astype(float).round(4)
            df_movers["price_24h_ago"] = df_movers["price_24h_ago"].astype(float).round(4)
            df_movers["move_abs"] = df_movers["move_abs"].astype(float).round(4)
            st.dataframe(
                df_movers[["market_id", "question", "sport", "price_now", "price_24h_ago", "move_abs"]],
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.info("No price snapshot data yet — collector may just be starting.")
