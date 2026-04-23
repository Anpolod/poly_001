"""
dashboard/pages/movements.py — Movement Alerts page.

Ranks markets by recent price movement. Classifies each as SPIKE, DRIFT, or FLAT
using a lightweight inline heuristic (no dependency on spike_events table).
"""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard import db


def _classify_movement(move_abs: float, n_steps: int) -> str:
    """Lightweight classification without spike_events table dependency.

    SPIKE  — large move concentrated in few steps (fast)
    DRIFT  — sustained move over many steps (slow)
    FLAT   — negligible movement
    """
    if move_abs < 0.01:
        return "FLAT"
    if n_steps == 0:
        return "SPIKE"
    rate = move_abs / max(n_steps, 1)
    if rate > 0.005:
        return "SPIKE"
    return "DRIFT"


@st.cache_data(ttl=300)
def _load_movers(hours: int, min_move: float, sport_filter: str) -> list[dict]:
    sport_clause = "AND m.sport = %(sport)s" if sport_filter else ""
    return db.query_df(f"""
        WITH bounds AS (
            SELECT ps.market_id,
                   MAX(ps.mid_price) FILTER (WHERE ps.ts > NOW() - INTERVAL '%(hours)s hours') AS price_max,
                   MIN(ps.mid_price) FILTER (WHERE ps.ts > NOW() - INTERVAL '%(hours)s hours') AS price_min,
                   (MAX(ps.ts) - MIN(ps.ts)) AS window_span,
                   COUNT(*) FILTER (WHERE ps.ts > NOW() - INTERVAL '%(hours)s hours') AS n_steps,
                   MAX(ps.mid_price) AS price_now,
                   MIN(ps.ts) FILTER (WHERE ps.ts > NOW() - INTERVAL '%(hours)s hours') AS first_ts,
                   MAX(ps.ts) AS last_ts
            FROM price_snapshots ps
            JOIN markets m ON m.id = ps.market_id
            WHERE ps.ts > NOW() - INTERVAL '%(hours)s hours'
            {sport_clause}
            GROUP BY ps.market_id
        )
        SELECT b.market_id, m.question, m.sport, m.league, m.event_start,
               b.price_now, b.price_max, b.price_min,
               (b.price_max - b.price_min) AS move_abs,
               b.n_steps, b.first_ts, b.last_ts
        FROM bounds b
        JOIN markets m ON m.id = b.market_id
        WHERE (b.price_max - b.price_min) >= %(min_move)s
        ORDER BY (b.price_max - b.price_min) DESC
        LIMIT 100
    """, {"hours": hours, "sport": sport_filter or "", "min_move": min_move})


def render() -> None:
    st.title("Movement Alerts")
    st.caption("Markets with the strongest recent price movement")

    # ── Controls ──────────────────────────────────────────────────────────
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        hours = st.selectbox("Time window", [6, 12, 24, 48, 72], index=2)
    with col2:
        min_move = st.slider("Min |Δ price|", 0.01, 0.20, 0.03, step=0.01)
    with col3:
        sports = ["(all)"] + db.all_sports()
        sport_sel = st.selectbox("Sport", sports)
    with col4:
        top_n = st.slider("Show top N", 5, 50, 20, step=5)

    sport_arg = "" if sport_sel == "(all)" else sport_sel

    rows = _load_movers(hours, min_move, sport_arg)
    if not rows:
        st.info(f"No markets moved more than {min_move:.2f} in the last {hours}h. Try a smaller threshold.")
        return

    df = pd.DataFrame(rows[:top_n])
    for col in ["move_abs", "price_now", "price_max", "price_min"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["n_steps"] = pd.to_numeric(df["n_steps"], errors="coerce").fillna(0).astype(int)
    df["type"] = df.apply(lambda r: _classify_movement(float(r["move_abs"]), int(r["n_steps"])), axis=1)

    # ── Summary metrics ───────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Markets moving", len(rows))
    c2.metric("SPIKE", int((df["type"] == "SPIKE").sum()))
    c3.metric("DRIFT", int((df["type"] == "DRIFT").sum()))
    c4.metric("Max move", f"{df['move_abs'].max():.4f}")

    # ── Movement type filter ───────────────────────────────────────────────
    type_filter = st.multiselect("Movement type", ["SPIKE", "DRIFT", "FLAT"], default=["SPIKE", "DRIFT"])
    if type_filter:
        df = df[df["type"].isin(type_filter)]

    # ── Bar chart ─────────────────────────────────────────────────────────
    color_map = {"SPIKE": "#F44336", "DRIFT": "#FFC107", "FLAT": "#888"}
    fig = go.Figure()
    for mtype, grp in df.groupby("type"):
        label = grp["question"].str[:45]
        fig.add_trace(go.Bar(
            x=grp["move_abs"],
            y=label,
            orientation="h",
            name=mtype,
            marker_color=color_map.get(str(mtype), "#888"),
        ))
    fig.update_layout(
        title=f"Top movers — last {hours}h (|Δ price| ≥ {min_move})",
        xaxis_title="|Δ price|",
        yaxis={"autorange": "reversed"},
        barmode="stack",
        plot_bgcolor="#0E1117",
        paper_bgcolor="#0E1117",
        font={"color": "#FAFAFA"},
        height=max(300, len(df) * 24),
        legend={"orientation": "h", "y": -0.15},
    )
    st.plotly_chart(fig, use_container_width=True)

    # ── Detail table ──────────────────────────────────────────────────────
    show_cols = ["type", "market_id", "question", "sport", "price_now",
                 "price_max", "price_min", "move_abs", "n_steps", "event_start"]
    show_cols = [c for c in show_cols if c in df.columns]
    df_show = df[show_cols].copy()

    for col in ["price_now", "price_max", "price_min", "move_abs"]:
        if col in df_show.columns:
            df_show[col] = df_show[col].round(4)
    if "event_start" in df_show.columns:
        df_show["event_start"] = pd.to_datetime(df_show["event_start"]).dt.strftime("%m-%d %H:%M")

    def _color_type_col(series):
        colors = {"SPIKE": "color: #F44336", "DRIFT": "color: #FFC107", "FLAT": "color: #888"}
        return [colors.get(v, "") for v in series]

    styled = df_show.style
    if "type" in df_show.columns:
        styled = styled.apply(_color_type_col, subset=["type"])
    st.dataframe(styled, use_container_width=True, hide_index=True)
