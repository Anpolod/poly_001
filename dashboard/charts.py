"""
dashboard/charts.py — Plotly chart builders for the Streamlit dashboard.

All functions accept a list of dicts (from dashboard/db.py) and return
a Plotly figure object ready to pass to st.plotly_chart().
"""

from __future__ import annotations

import plotly.express as px
import plotly.graph_objects as go
import pandas as pd


def price_history_chart(rows: list[dict], market_label: str = "") -> go.Figure:
    """Line chart of mid_price over time for a single market."""
    if not rows:
        return go.Figure().update_layout(title="No data")
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"])
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["ts"], y=df["mid_price"].astype(float),
        mode="lines", name="Mid price",
        line={"color": "#4CAF50", "width": 2},
    ))
    if "spread" in df.columns:
        df["upper"] = (df["mid_price"] + df["spread"] / 2).astype(float)
        df["lower"] = (df["mid_price"] - df["spread"] / 2).astype(float)
        fig.add_trace(go.Scatter(
            x=df["ts"], y=df["upper"],
            mode="lines", name="Ask", line={"color": "#888", "width": 1, "dash": "dot"},
        ))
        fig.add_trace(go.Scatter(
            x=df["ts"], y=df["lower"],
            mode="lines", name="Bid", line={"color": "#888", "width": 1, "dash": "dot"},
            fill="tonexty", fillcolor="rgba(100,100,100,0.1)",
        ))
    fig.update_layout(
        title=f"Price history — {market_label}" if market_label else "Price history",
        xaxis_title="Time (UTC)",
        yaxis_title="Price",
        yaxis={"range": [0, 1]},
        plot_bgcolor="#0E1117",
        paper_bgcolor="#0E1117",
        font={"color": "#FAFAFA"},
        legend={"orientation": "h", "y": -0.2},
        height=360,
    )
    return fig


def spread_dist_chart(rows: list[dict]) -> go.Figure:
    """Histogram of spread_pct values across markets."""
    if not rows:
        return go.Figure().update_layout(title="No data")
    df = pd.DataFrame(rows)
    if "spread_pct" not in df.columns:
        return go.Figure().update_layout(title="No spread data")
    fig = px.histogram(
        df.dropna(subset=["spread_pct"]),
        x="spread_pct",
        nbins=40,
        title="Spread distribution (%)",
        color_discrete_sequence=["#4CAF50"],
    )
    fig.update_layout(
        xaxis_title="Spread %",
        yaxis_title="Markets",
        plot_bgcolor="#0E1117",
        paper_bgcolor="#0E1117",
        font={"color": "#FAFAFA"},
        height=300,
    )
    return fig


def snapshot_count_bar(rows: list[dict], top_n: int = 20) -> go.Figure:
    """Horizontal bar chart of snapshot counts per market (last 24h)."""
    if not rows:
        return go.Figure().update_layout(title="No snapshot data")
    df = pd.DataFrame(rows[:top_n])
    label = df["question"].str[:50] if "question" in df.columns else df["market_id"]
    fig = go.Figure(go.Bar(
        x=df["snapshots"].astype(int),
        y=label,
        orientation="h",
        marker_color="#4CAF50",
    ))
    fig.update_layout(
        title=f"Snapshot counts — top {top_n} markets (last 24h)",
        xaxis_title="Snapshots",
        yaxis={"autorange": "reversed"},
        plot_bgcolor="#0E1117",
        paper_bgcolor="#0E1117",
        font={"color": "#FAFAFA"},
        height=max(300, top_n * 22),
    )
    return fig


def movers_bar(rows: list[dict]) -> go.Figure:
    """Bar chart of top movers sorted by absolute 24h price move."""
    if not rows:
        return go.Figure().update_layout(title="No movers data")
    df = pd.DataFrame(rows)
    df["move_abs"] = df["move_abs"].astype(float)
    df["label"] = df["question"].str[:45]
    colors = ["#4CAF50" if (r["price_now"] or 0) > (r["price_24h_ago"] or 0) else "#F44336"
              for _, r in df.iterrows()]
    fig = go.Figure(go.Bar(
        x=df["move_abs"],
        y=df["label"],
        orientation="h",
        marker_color=colors,
    ))
    fig.update_layout(
        title="Top movers — 24h absolute price change",
        xaxis_title="Δ price",
        yaxis={"autorange": "reversed"},
        plot_bgcolor="#0E1117",
        paper_bgcolor="#0E1117",
        font={"color": "#FAFAFA"},
        height=max(280, len(rows) * 26),
    )
    return fig


def verdict_pie(rows: list[dict]) -> go.Figure:
    """Pie chart of GO / MARGINAL / NO_GO verdicts."""
    if not rows:
        return go.Figure().update_layout(title="No verdict data")
    df = pd.DataFrame(rows)
    colors = {"GO": "#4CAF50", "MARGINAL": "#FFC107", "NO_GO": "#F44336", None: "#888"}
    fig = go.Figure(go.Pie(
        labels=df["verdict"],
        values=df["cnt"].astype(int),
        marker_colors=[colors.get(v, "#888") for v in df["verdict"]],
        hole=0.4,
    ))
    fig.update_layout(
        title="Cost verdict distribution",
        plot_bgcolor="#0E1117",
        paper_bgcolor="#0E1117",
        font={"color": "#FAFAFA"},
        height=300,
    )
    return fig


def tanking_scatter(rows: list[dict]) -> go.Figure:
    """Scatter: motivation_differential vs current_price, coloured by pattern_strength."""
    if not rows:
        return go.Figure().update_layout(title="No tanking signals")
    df = pd.DataFrame(rows)
    color_map = {"HIGH": "#F44336", "MODERATE": "#FFC107"}
    fig = px.scatter(
        df,
        x="motivation_differential",
        y="current_price",
        color="pattern_strength",
        color_discrete_map=color_map,
        hover_data=["motivated_team", "tanking_team", "action"],
        title="Tanking signals — differential vs price",
    )
    fig.add_hline(y=0.5, line_dash="dot", line_color="#888", annotation_text="50¢")
    fig.update_layout(
        xaxis_title="Motivation differential",
        yaxis_title="Current YES price",
        yaxis={"range": [0, 1]},
        plot_bgcolor="#0E1117",
        paper_bgcolor="#0E1117",
        font={"color": "#FAFAFA"},
        height=380,
    )
    return fig
