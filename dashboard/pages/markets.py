"""
dashboard/pages/markets.py — Markets browser page.

Filterable table of all markets joined with their latest cost_analysis verdict.
Selecting a market shows its price_snapshots history chart.
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
    st.title("Markets")
    st.caption("Browse all markets with cost analysis results")

    # ── Filters ───────────────────────────────────────────────────────────
    col1, col2, col3 = st.columns([2, 2, 2])
    with col1:
        sports = ["(all)"] + db.all_sports()
        sport_sel = st.selectbox("Sport", sports)
    with col2:
        verdict_sel = st.selectbox("Verdict", ["(all)", "GO", "MARGINAL", "NO_GO"])
    with col3:
        show_hours = st.slider("Price history (hours)", 6, 168, 48, step=6)

    sport_arg = "" if sport_sel == "(all)" else sport_sel
    verdict_arg = "" if verdict_sel == "(all)" else verdict_sel

    # ── Market table ──────────────────────────────────────────────────────
    rows = db.markets_with_latest_analysis(sport_arg, verdict_arg)
    if not rows:
        st.info("No markets match the current filters.")
        return

    df = pd.DataFrame(rows)

    # Friendly display columns
    display_cols = ["market_id", "question", "sport", "league", "status",
                    "event_start", "verdict", "spread_pct", "taker_rt_cost", "ratio_24h", "volume_24h"]
    display_cols = [c for c in display_cols if c in df.columns]
    df_display = df[display_cols].copy()

    for col in ["spread_pct", "taker_rt_cost", "ratio_24h"]:
        if col in df_display.columns:
            df_display[col] = pd.to_numeric(df_display[col], errors="coerce").round(4)
    if "volume_24h" in df_display.columns:
        df_display["volume_24h"] = pd.to_numeric(df_display["volume_24h"], errors="coerce").round(0)
    if "event_start" in df_display.columns:
        df_display["event_start"] = pd.to_datetime(df_display["event_start"]).dt.strftime("%Y-%m-%d %H:%M")

    # Color verdict column — use apply() (stable across all pandas versions)
    def _color_verdict_col(series):
        colors = {"GO": "background-color: #1B5E20; color: #A5D6A7",
                  "MARGINAL": "background-color: #F57F17; color: #FFF9C4",
                  "NO_GO": "background-color: #B71C1C; color: #FFCDD2"}
        return [colors.get(v, "") for v in series]

    styled = df_display.style
    if "verdict" in df_display.columns:
        styled = styled.apply(_color_verdict_col, subset=["verdict"])

    st.write(f"**{len(df)} markets** found")
    event = st.dataframe(
        styled,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
    )

    # ── Price history drilldown ───────────────────────────────────────────
    selected_rows = event.selection.get("rows", []) if hasattr(event, "selection") else []
    if selected_rows:
        idx = selected_rows[0]
        row = rows[idx]
        market_id = row["market_id"]
        question = row.get("question", market_id)[:80]
        st.divider()
        st.subheader(f"Price history: {question}")
        history = db.price_history(market_id, hours=show_hours)
        if history:
            st.plotly_chart(
                charts.price_history_chart(history, market_label=question),
                use_container_width=True,
            )
            col_a, col_b = st.columns(2)
            with col_a:
                st.metric("Data points", len(history))
            with col_b:
                latest = history[-1]
                st.metric("Latest mid price", f"{float(latest['mid_price']):.4f}")
        else:
            st.info(f"No price snapshot data for this market in the last {show_hours}h.")

        st.subheader("Spread distribution across filtered markets")
        all_rows_for_spread = db.markets_with_latest_analysis(sport_arg, verdict_arg)
        st.plotly_chart(
            charts.spread_dist_chart(all_rows_for_spread),
            use_container_width=True,
        )
