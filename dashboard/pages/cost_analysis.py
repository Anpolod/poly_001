"""
dashboard/pages/cost_analysis.py — Phase 0 cost analysis results.

Visualizes GO / MARGINAL / NO_GO tradability verdicts, taker cost vs.
price-move ratio scatter, and exports filterable table to CSV.

Degrades gracefully if the cost_analysis table is empty or absent.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
_root = Path(__file__).parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from dashboard import db


@st.cache_data(ttl=300)
def _load_cost_data(sport_filter: str, verdict_filter: str) -> list[dict]:
    return db.markets_with_latest_analysis(sport_filter, verdict_filter)


def render() -> None:
    st.title("Cost Analysis")
    st.caption("Phase 0 tradability verdicts — taker cost, spread, and price-move ratio")

    # ── Filters ───────────────────────────────────────────────────────────
    col1, col2 = st.columns(2)
    with col1:
        sports = ["(all)"] + db.all_sports()
        sport_sel = st.selectbox("Sport", sports)
    with col2:
        verdict_sel = st.selectbox("Verdict", ["(all)", "GO", "MARGINAL", "NO_GO"])

    sport_arg = "" if sport_sel == "(all)" else sport_sel
    verdict_arg = "" if verdict_sel == "(all)" else verdict_sel

    rows = _load_cost_data(sport_arg, verdict_arg)
    df_all = pd.DataFrame(rows)

    if df_all.empty or "verdict" not in df_all.columns or df_all["verdict"].isna().all():
        st.warning(
            "No cost analysis data available. "
            "Run `python cost_analyzer.py` to populate the `cost_analysis` table."
        )
        return

    # Keep only rows that have been analysed
    df = df_all.dropna(subset=["verdict"]).copy()

    for col in ["spread_pct", "taker_rt_cost", "ratio_24h", "volume_24h"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # ── KPI cards ─────────────────────────────────────────────────────────
    verdict_counts = df["verdict"].value_counts()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Analysed markets", len(df))
    c2.metric("GO", int(verdict_counts.get("GO", 0)))
    c3.metric("MARGINAL", int(verdict_counts.get("MARGINAL", 0)))
    c4.metric("NO_GO", int(verdict_counts.get("NO_GO", 0)))

    st.divider()

    col_left, col_right = st.columns(2)

    # ── Verdict pie ────────────────────────────────────────────────────────
    with col_left:
        verdict_rows = [{"verdict": k, "cnt": v} for k, v in verdict_counts.items()]
        from dashboard.charts import verdict_pie
        st.plotly_chart(verdict_pie(verdict_rows), use_container_width=True)

    # ── Spread distribution ───────────────────────────────────────────────
    with col_right:
        from dashboard.charts import spread_dist_chart
        st.plotly_chart(spread_dist_chart(df.to_dict("records")), use_container_width=True)

    st.divider()

    # ── Scatter: taker cost vs ratio ──────────────────────────────────────
    scatter_df = df.dropna(subset=["taker_rt_cost", "ratio_24h"])
    if not scatter_df.empty:
        color_map = {"GO": "#4CAF50", "MARGINAL": "#FFC107", "NO_GO": "#F44336"}
        fig = px.scatter(
            scatter_df,
            x="taker_rt_cost",
            y="ratio_24h",
            color="verdict",
            color_discrete_map=color_map,
            size="volume_24h" if "volume_24h" in scatter_df.columns else None,
            size_max=25,
            hover_data=["question", "sport", "spread_pct"],
            title="Taker cost % vs Price-move ratio (24h)",
        )
        fig.add_hline(y=2.0, line_dash="dash", line_color="#4CAF50",
                      annotation_text="GO threshold (2.0)")
        fig.add_hline(y=1.5, line_dash="dot", line_color="#FFC107",
                      annotation_text="MARGINAL threshold (1.5)")
        fig.update_layout(
            xaxis_title="Taker round-trip cost %",
            yaxis_title="Price-move / taker-cost ratio",
            plot_bgcolor="#0E1117",
            paper_bgcolor="#0E1117",
            font={"color": "#FAFAFA"},
            height=420,
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Not enough data for scatter plot (need taker_rt_cost and ratio_24h).")

    st.divider()

    # ── Filterable table ──────────────────────────────────────────────────
    st.subheader("Market table")
    show_cols = ["market_id", "question", "sport", "league", "verdict",
                 "spread_pct", "taker_rt_cost", "ratio_24h", "volume_24h",
                 "event_start", "status"]
    show_cols = [c for c in show_cols if c in df.columns]
    df_show = df[show_cols].copy()

    for col in ["spread_pct", "taker_rt_cost", "ratio_24h"]:
        if col in df_show.columns:
            df_show[col] = df_show[col].round(4)
    if "volume_24h" in df_show.columns:
        df_show["volume_24h"] = df_show["volume_24h"].round(0)
    if "event_start" in df_show.columns:
        df_show["event_start"] = pd.to_datetime(df_show["event_start"]).dt.strftime("%Y-%m-%d %H:%M")

    def _color_verdict_col(series):
        colors = {"GO": "color: #4CAF50; font-weight: bold",
                  "MARGINAL": "color: #FFC107",
                  "NO_GO": "color: #F44336"}
        return [colors.get(v, "") for v in series]

    styled = df_show.style
    if "verdict" in df_show.columns:
        styled = styled.apply(_color_verdict_col, subset=["verdict"])
    st.dataframe(styled, use_container_width=True, hide_index=True)

    # ── CSV export ────────────────────────────────────────────────────────
    csv_buf = io.StringIO()
    df_show.to_csv(csv_buf, index=False)
    st.download_button(
        "Download CSV",
        data=csv_buf.getvalue(),
        file_name="cost_analysis_export.csv",
        mime="text/csv",
    )
