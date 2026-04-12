"""
dashboard/pages/tanking.py — NBA Tanking Scanner page.

Shows tanking_signals from the DB and offers a "Re-scan now" button
that invokes the tanking_scanner CLI in a subprocess.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Ensure project root is on sys.path when Streamlit loads this page directly
_root = Path(__file__).parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import pandas as pd
import streamlit as st

from dashboard import db, charts

PROJECT_ROOT = Path(__file__).parent.parent.parent


def render() -> None:
    st.title("NBA Tanking Scanner")
    st.caption("End-of-season motivated vs. tanking team matchup signals")

    # ── Controls ──────────────────────────────────────────────────────────
    col1, col2 = st.columns([3, 1])
    with col1:
        hours = st.slider("Show signals from last N hours", 12, 120, 48, step=12)
    with col2:
        st.write("")
        st.write("")
        rescan = st.button("Re-scan now", type="primary")

    if rescan:
        with st.spinner("Running tanking scanner (single cycle)…"):
            result = subprocess.run(
                [sys.executable, "-m", "analytics.tanking_scanner", "--save"],
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
    rows = db.tanking_signals_recent(hours=hours)
    if not rows:
        st.info(
            f"No tanking signals in the last {hours}h. "
            "Run the scanner: `python -m analytics.tanking_scanner --save`"
        )
        return

    st.write(f"**{len(rows)} signals** in the last {hours}h")

    # ── Summary metrics ───────────────────────────────────────────────────
    df = pd.DataFrame(rows)
    high_count = int((df["pattern_strength"] == "HIGH").sum())
    mod_count = int((df["pattern_strength"] == "MODERATE").sum())
    buy_count = int((df["action"] == "BUY").sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("HIGH signals", high_count)
    c2.metric("MODERATE signals", mod_count)
    c3.metric("BUY recommendations", buy_count)
    avg_diff = df["motivation_differential"].astype(float).abs().mean()
    c4.metric("Avg |differential|", f"{avg_diff:.2f}")

    # ── Scatter chart ─────────────────────────────────────────────────────
    st.plotly_chart(charts.tanking_scatter(rows), use_container_width=True)

    # ── Detail table ──────────────────────────────────────────────────────
    st.subheader("Signal detail")
    display_cols = [
        "scanned_at", "pattern_strength", "action",
        "motivated_team", "tanking_team", "motivation_differential",
        "current_price", "drift_24h", "game_start", "question",
    ]
    display_cols = [c for c in display_cols if c in df.columns]
    df_show = df[display_cols].copy()

    for col in ["motivation_differential", "current_price", "drift_24h"]:
        if col in df_show.columns:
            df_show[col] = pd.to_numeric(df_show[col], errors="coerce").round(4)
    for col in ["scanned_at", "game_start"]:
        if col in df_show.columns:
            df_show[col] = pd.to_datetime(df_show[col]).dt.strftime("%m-%d %H:%M")

    def _row_color(row):
        if row.get("pattern_strength") == "HIGH":
            return ["background-color: #1A2A1A"] * len(row)
        if row.get("pattern_strength") == "MODERATE":
            return ["background-color: #2A2A1A"] * len(row)
        return [""] * len(row)

    def _color_strength_col(series):
        colors = {"HIGH": "color: #F44336; font-weight: bold", "MODERATE": "color: #FFC107"}
        return [colors.get(v, "") for v in series]

    def _color_action_col(series):
        colors = {"BUY": "color: #4CAF50; font-weight: bold",
                  "SELL": "color: #F44336", "CLOSE": "color: #F44336"}
        return [colors.get(v, "") for v in series]

    styled = df_show.style
    if "pattern_strength" in df_show.columns:
        styled = styled.apply(_color_strength_col, subset=["pattern_strength"])
    if "action" in df_show.columns:
        styled = styled.apply(_color_action_col, subset=["action"])

    st.dataframe(styled, use_container_width=True, hide_index=True)

    # ── HIGH signal detail cards ──────────────────────────────────────────
    high_rows = [r for r in rows if r.get("pattern_strength") == "HIGH"]
    if high_rows:
        st.subheader("HIGH signal details")
        for row in high_rows[:5]:
            with st.expander(
                f"{row.get('motivated_team','?')} vs {row.get('tanking_team','?')} "
                f"— {row.get('action','?')} @ {float(row.get('current_price', 0)):.2f}"
            ):
                col_a, col_b = st.columns(2)
                col_a.write(f"**Motivated team:** {row.get('motivated_team')}")
                col_a.write(f"**Tanking team:** {row.get('tanking_team')}")
                col_a.write(f"**Differential:** {float(row.get('motivation_differential', 0)):.2f}")
                col_b.write(f"**Action:** `{row.get('action')}`")
                col_b.write(f"**Price:** {float(row.get('current_price', 0)):.4f}")
                col_b.write(f"**Drift 24h:** {float(row.get('drift_24h') or 0):.4f}")
                if row.get("lineup_notes"):
                    import json
                    try:
                        notes = json.loads(row["lineup_notes"])
                        if notes:
                            st.write("**Lineup notes:**")
                            for note in notes:
                                st.write(f"- {note}")
                    except (json.JSONDecodeError, TypeError):
                        st.write(f"**Lineup notes:** {row['lineup_notes']}")
                if row.get("question"):
                    st.write(f"**Market:** {row['question']}")
