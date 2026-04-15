"""
dashboard/pages/mlb.py — MLB Pitcher Scanner page.

Shows pitcher_signals from the DB: ERA mismatches, market price,
signal strength, and a "Re-scan now" button.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_root = Path(__file__).parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import pandas as pd
import streamlit as st

from dashboard import db, charts

PROJECT_ROOT = Path(__file__).parent.parent.parent


def render() -> None:
    st.title("MLB Pitcher Scanner")
    st.caption("Starting pitcher ERA mismatch signals — favored vs underdog matchups")

    # ── Controls ──────────────────────────────────────────────────────────
    col1, col2 = st.columns([3, 1])
    with col1:
        hours = st.slider("Show signals from last N hours", 12, 120, 48, step=12)
    with col2:
        st.write("")
        st.write("")
        rescan = st.button("Re-scan now", type="primary")

    if rescan:
        with st.spinner("Running MLB pitcher scanner (single cycle)…"):
            result = subprocess.run(
                [sys.executable, "-m", "analytics.mlb_pitcher_scanner", "--save"],
                capture_output=True, text=True,
                cwd=str(PROJECT_ROOT),
                timeout=120,
            )
        if result.returncode == 0:
            st.success("Scan complete — table below will refresh on next rerun.")
            with st.expander("Scanner output"):
                st.code(result.stdout or "(no output)")
        else:
            st.error("Scanner failed")
            st.code(result.stderr or result.stdout or "(no output)")

    st.divider()

    # ── Signals table ─────────────────────────────────────────────────────
    rows = db.pitcher_signals_recent(hours=hours)
    if not rows:
        st.info(
            f"No pitcher signals in the last {hours}h. "
            "Run the scanner: `python -m analytics.mlb_pitcher_scanner --save`"
        )
        return

    st.write(f"**{len(rows)} signals** in the last {hours}h")

    # ── Summary metrics ───────────────────────────────────────────────────
    df = pd.DataFrame(rows)
    for col in ["era_differential", "quality_differential", "current_price", "drift_24h"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    high_count = int((df["signal_strength"] == "HIGH").sum())
    mod_count = int((df["signal_strength"] == "MODERATE").sum())
    buy_count = int((df["action"] == "BUY").sum())
    avg_era_diff = df["era_differential"].abs().mean()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("HIGH signals", high_count)
    c2.metric("MODERATE signals", mod_count)
    c3.metric("BUY recommendations", buy_count)
    c4.metric("Avg |ERA diff|", f"{avg_era_diff:.2f}" if not pd.isna(avg_era_diff) else "—")

    # ── Scatter chart ─────────────────────────────────────────────────────
    st.plotly_chart(charts.pitcher_scatter(rows), use_container_width=True)

    # ── Detail table ──────────────────────────────────────────────────────
    st.subheader("Signal detail")

    df["matchup"] = df["favored_team"] + " vs " + df["underdog_team"]
    df["pitchers"] = df["home_pitcher"].fillna("?") + " (H) vs " + df["away_pitcher"].fillna("?") + " (A)"
    df["era_diff_fmt"] = df["era_differential"].round(2)
    df["price_pct"] = (df["current_price"] * 100).round(1)
    df["drift_fmt"] = (df["drift_24h"] * 100).round(2)

    display_cols = [
        "scanned_at", "signal_strength", "action",
        "matchup", "pitchers", "era_diff_fmt",
        "price_pct", "drift_fmt", "game_start",
    ]
    display_cols = [c for c in display_cols if c in df.columns]
    df_show = df[display_cols].rename(columns={
        "scanned_at": "Scanned",
        "signal_strength": "Strength",
        "action": "Action",
        "matchup": "Matchup",
        "pitchers": "Pitchers",
        "era_diff_fmt": "ERA Δ",
        "price_pct": "Price (¢)",
        "drift_fmt": "Drift 24h%",
        "game_start": "Game start",
    })

    for col in ["Scanned", "Game start"]:
        if col in df_show.columns:
            df_show[col] = pd.to_datetime(df_show[col]).dt.strftime("%m-%d %H:%M")

    def _color_strength(series):
        colors = {"HIGH": "color: #F44336; font-weight: bold", "MODERATE": "color: #FFC107"}
        return [colors.get(str(v), "") for v in series]

    def _color_action(series):
        colors = {"BUY": "color: #4CAF50; font-weight: bold",
                  "SELL": "color: #F44336", "WATCH": "color: #888"}
        return [colors.get(str(v), "") for v in series]

    styled = df_show.style
    if "Strength" in df_show.columns:
        styled = styled.apply(_color_strength, subset=["Strength"])
    if "Action" in df_show.columns:
        styled = styled.apply(_color_action, subset=["Action"])

    st.dataframe(styled, use_container_width=True, hide_index=True)

    # ── HIGH signal detail cards ──────────────────────────────────────────
    high_rows = [r for r in rows if r.get("signal_strength") == "HIGH"]
    if high_rows:
        st.subheader("HIGH signal details")
        for row in high_rows[:5]:
            with st.expander(
                f"{row.get('favored_team','?')} vs {row.get('underdog_team','?')} "
                f"— {row.get('action','?')} @ {float(row.get('current_price') or 0):.2f}"
            ):
                col_a, col_b = st.columns(2)
                col_a.write(f"**Favored:** {row.get('favored_team')}")
                col_a.write(f"**Underdog:** {row.get('underdog_team')}")
                col_a.write(f"**Home pitcher:** {row.get('home_pitcher','?')} (ERA: {row.get('home_era','?')})")
                col_a.write(f"**Away pitcher:** {row.get('away_pitcher','?')} (ERA: {row.get('away_era','?')})")
                col_b.write(f"**ERA differential:** {float(row.get('era_differential') or 0):.2f}")
                col_b.write(f"**Quality diff:** {float(row.get('quality_differential') or 0):.2f}")
                col_b.write(f"**Action:** `{row.get('action')}`")
                col_b.write(f"**Price:** {float(row.get('current_price') or 0):.4f}")
                col_b.write(f"**Drift 24h:** {float(row.get('drift_24h') or 0):.4f}")
                if row.get("question"):
                    st.write(f"**Market:** {row['question']}")
