"""
dashboard/main_dashboard.py — Streamlit entry point.

Run with:
    streamlit run dashboard/main_dashboard.py
    make dashboard

Uses sidebar radio for navigation (compatible with all Streamlit ≥1.0).
Each page is a module in dashboard/pages/ that exposes a render() function.
"""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import time
from datetime import datetime, timezone

import streamlit as st

st.set_page_config(
    page_title="Polymarket Sports",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

from dashboard.pages import (
    cost_analysis,
    health,
    live_monitor,
    markets,
    mlb,
    movements,
    overview,
    signals,
    tanking,
    trading,
)

# ── Page registry ─────────────────────────────────────────────────────────────
PAGES: dict[str, tuple[str, object]] = {
    "Trading":              ("🏦", trading.render),
    "Signals":              ("🎯", signals.render),
    "Overview":             ("📊", overview.render),
    "Live Monitor":         ("📡", live_monitor.render),
    "Markets":              ("📈", markets.render),
    "Movement Alerts":      ("🎯", movements.render),
    "Cost Analysis":        ("🔬", cost_analysis.render),
    "NBA Tanking":          ("🏀", tanking.render),
    "MLB Pitcher Signals":  ("⚾", mlb.render),
    "System Health":        ("⚙️", health.render),
}

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 📊 Polymarket Sports")
    st.markdown("---")

    selected = st.radio(
        "Navigate",
        list(PAGES.keys()),
        format_func=lambda k: f"{PAGES[k][0]}  {k}",
        label_visibility="collapsed",
    )

    st.markdown("---")

    # Auto-refresh toggle
    auto_refresh = st.toggle("Auto-refresh (5 min)", value=False)

    # Manual refresh
    if st.button("🔄 Refresh now", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.caption(f"Last refresh: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")

    st.markdown("---")
    st.caption("Collector: `python main.py`")
    st.caption("Scanner: `make scanner-daemon`")
    st.caption("Reports: `make obsidian`")

# ── Auto-refresh logic ────────────────────────────────────────────────────────
# Store last-refresh time in session state to avoid hammering the DB
if "last_auto_refresh" not in st.session_state:
    st.session_state["last_auto_refresh"] = time.monotonic()

if auto_refresh:
    elapsed = time.monotonic() - st.session_state["last_auto_refresh"]
    if elapsed >= 300:  # 5 minutes
        st.cache_data.clear()
        st.session_state["last_auto_refresh"] = time.monotonic()
        st.rerun()

# ── Render selected page ──────────────────────────────────────────────────────
_, render_fn = PAGES[selected]
render_fn()  # type: ignore[operator]
