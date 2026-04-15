"""
dashboard/pages/signals.py — Consolidated trade entry signals page.

Aggregates actionable opportunities from three sources:
  1. Prop scanner (prop_scan_log)  — positive-EV NBA player props
  2. Tanking scanner (tanking_signals) — BUY/SELL recommendations
  3. Cost analysis (cost_analysis)  — GO markets with recent price drift
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

from dashboard import db


# ── DB queries ────────────────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def _prop_opportunities() -> list[dict]:
    """Active prop scanner hits: unresolved, from last 24h, sorted by ROI."""
    return db.query_df("""
        SELECT player_name, prop_type, threshold, yes_price,
               model_win, ev_per_unit, roi_pct,
               hours_until, game_start, scanned_at,
               market_id, slug
        FROM prop_scan_log
        WHERE outcome IS NULL
          AND scanned_at > NOW() - INTERVAL '24 hours'
          AND hours_until > 0
        ORDER BY roi_pct DESC NULLS LAST
        LIMIT 50
    """)


@st.cache_data(ttl=60)
def _tanking_opportunities() -> list[dict]:
    """Tanking signals with BUY or SELL action, last 48h."""
    return db.query_df("""
        SELECT ts.scanned_at, ts.market_id, m.question,
               ts.motivated_team, ts.tanking_team,
               ts.motivation_differential, ts.current_price,
               ts.drift_24h, ts.pattern_strength, ts.action,
               ts.game_start, ts.lineup_notes
        FROM tanking_signals ts
        LEFT JOIN markets m ON m.id = ts.market_id
        WHERE ts.action IN ('BUY', 'SELL', 'CLOSE')
          AND ts.scanned_at > NOW() - INTERVAL '48 hours'
          AND ts.game_start > NOW()
        ORDER BY ts.motivation_differential DESC, ts.scanned_at DESC
        LIMIT 30
    """)


@st.cache_data(ttl=60)
def _pitcher_opportunities() -> list[dict]:
    """MLB pitcher signals with BUY action, last 48h, game not yet started."""
    return db.query_df("""
        SELECT ps.favored_team, ps.underdog_team,
               ps.home_pitcher, ps.home_era,
               ps.away_pitcher, ps.away_era,
               ps.era_differential, ps.quality_differential,
               ps.current_price, ps.drift_24h,
               ps.signal_strength, ps.action,
               ps.game_start, ps.scanned_at, ps.market_id
        FROM pitcher_signals ps
        WHERE ps.action IN ('BUY', 'SELL')
          AND ps.scanned_at > NOW() - INTERVAL '48 hours'
          AND ps.game_start > NOW()
        ORDER BY ps.era_differential DESC, ps.scanned_at DESC
        LIMIT 20
    """)


@st.cache_data(ttl=120)
def _go_markets_with_drift() -> list[dict]:
    """GO-verdict markets that have moved significantly in last 6h."""
    return db.query_df("""
        WITH latest_ca AS (
            SELECT DISTINCT ON (market_id)
                   market_id, verdict, taker_rt_cost, ratio_24h,
                   spread_pct, volume_24h
            FROM cost_analysis
            WHERE verdict = 'GO'
            ORDER BY market_id, scanned_at DESC
        ),
        price_bounds AS (
            SELECT market_id,
                   MAX(mid_price) AS price_max,
                   MIN(mid_price) AS price_min,
                   (MAX(mid_price) - MIN(mid_price)) AS drift_6h
            FROM price_snapshots
            WHERE ts > NOW() - INTERVAL '6 hours'
            GROUP BY market_id
        )
        SELECT ca.market_id, m.question, m.sport, m.event_start,
               ca.verdict, ca.taker_rt_cost, ca.ratio_24h,
               ca.spread_pct, ca.volume_24h,
               pb.drift_6h, pb.price_max, pb.price_min,
               snap.mid_price AS price_now
        FROM latest_ca ca
        JOIN markets m ON m.id = ca.market_id
        JOIN price_bounds pb ON pb.market_id = ca.market_id
        LEFT JOIN LATERAL (
            SELECT mid_price FROM price_snapshots
            WHERE market_id = ca.market_id
            ORDER BY ts DESC LIMIT 1
        ) snap ON TRUE
        WHERE pb.drift_6h > 0.02
          AND m.event_start > NOW()
        ORDER BY pb.drift_6h DESC
        LIMIT 30
    """)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hours_until(ts) -> str:
    if ts is None:
        return "—"
    if hasattr(ts, "tzinfo") and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    h = (ts - datetime.now(timezone.utc)).total_seconds() / 3600
    if h < 0:
        return "started"
    if h < 1:
        return f"{int(h*60)}m"
    return f"{h:.1f}h"


def _color_roi(series):
    return ["color: #4CAF50; font-weight: bold" if float(v or 0) > 10
            else "color: #8BC34A" if float(v or 0) > 5
            else "" for v in series]


def _color_action(series):
    colors = {"BUY": "color: #4CAF50; font-weight: bold",
              "SELL": "color: #F44336; font-weight: bold",
              "CLOSE": "color: #FF9800"}
    return [colors.get(v, "") for v in series]


def _color_strength(series):
    return ["color: #F44336; font-weight: bold" if v == "HIGH"
            else "color: #FFC107" if v == "MODERATE"
            else "" for v in series]


def _color_drift(series):
    return ["color: #4CAF50" if float(v or 0) > 0.05
            else "color: #FFC107" if float(v or 0) > 0.02
            else "" for v in series]


# ── Render ────────────────────────────────────────────────────────────────────

def render() -> None:
    st.title("Trade Signals")
    st.caption("Consolidated entry opportunities from prop scanner, tanking scanner, and cost analysis")

    if st.button("🔄 Refresh signals", key="signals_refresh"):
        st.cache_data.clear()
        st.rerun()

    prop_rows = _prop_opportunities()
    tank_rows = _tanking_opportunities()
    go_rows = _go_markets_with_drift()
    pitcher_rows = _pitcher_opportunities()

    # ── Summary banner ────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Prop opportunities", len(prop_rows),
              help="Positive-EV player props (unresolved, last 24h)")
    c2.metric("Tanking BUY/SELL", len(tank_rows),
              help="Tanking signals with actionable recommendation (last 48h)")
    c3.metric("MLB Pitcher BUY/SELL", len(pitcher_rows),
              help="MLB ERA mismatch signals with BUY/SELL action (last 48h)")
    c4.metric("GO markets moving", len(go_rows),
              help="GO-verdict markets with >2% drift in last 6h")

    no_signals = not prop_rows and not tank_rows and not go_rows and not pitcher_rows
    if no_signals:
        st.warning(
            "No active signals right now.\n\n"
            "To generate:\n"
            "- **Props**: `venv/bin/python -m analytics.prop_scanner --daemon --once`\n"
            "- **Tanking**: `venv/bin/python -m analytics.tanking_scanner --save`\n"
            "- **GO markets**: run `python cost_analyzer.py` then let collector gather snapshots"
        )
        return

    st.divider()

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 1: PROP SCANNER
    # ══════════════════════════════════════════════════════════════════════
    st.subheader(f"🏀 Player Prop Opportunities ({len(prop_rows)})")

    if not prop_rows:
        st.info("No active prop signals. Run: `make scanner-once`")
    else:
        df_prop = pd.DataFrame(prop_rows)
        for col in ["yes_price", "model_win", "ev_per_unit", "roi_pct", "hours_until"]:
            df_prop[col] = pd.to_numeric(df_prop[col], errors="coerce")

        df_prop["time_to_game"] = df_prop["game_start"].apply(_hours_until)
        df_prop["yes_price_pct"] = (df_prop["yes_price"] * 100).round(1).astype(str) + "¢"
        df_prop["model_win_pct"] = (df_prop["model_win"] * 100).round(1).astype(str) + "%"
        df_prop["roi_pct_fmt"] = df_prop["roi_pct"].round(1).astype(str) + "%"
        df_prop["ev_fmt"] = df_prop["ev_per_unit"].round(3).astype(str)

        show = df_prop[[
            "player_name", "prop_type", "threshold",
            "yes_price_pct", "model_win_pct", "roi_pct",
            "roi_pct_fmt", "ev_fmt", "time_to_game"
        ]].rename(columns={
            "player_name": "Player", "prop_type": "Type",
            "threshold": "Line", "yes_price_pct": "Price",
            "model_win_pct": "Model win", "roi_pct": "_roi_raw",
            "roi_pct_fmt": "ROI%", "ev_fmt": "EV/unit",
            "time_to_game": "Time to game"
        })

        display = show.drop(columns=["_roi_raw"])
        styled = display.style.apply(_color_roi, subset=["ROI%"])
        st.dataframe(styled, use_container_width=True, hide_index=True)

        # Best signal callout
        best = df_prop.iloc[0]
        st.success(
            f"**Best prop:** {best['player_name']} — {best['prop_type']} "
            f"{best['threshold']} | Price: {best['yes_price']*100:.0f}¢ | "
            f"Model: {best['model_win']*100:.0f}% | ROI: **{best['roi_pct']:.1f}%** | "
            f"Game: {_hours_until(best['game_start'])}"
        )

    st.divider()

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 2: TANKING SIGNALS
    # ══════════════════════════════════════════════════════════════════════
    st.subheader(f"📉 Tanking Signals — BUY / SELL ({len(tank_rows)})")

    if not tank_rows:
        st.info("No actionable tanking signals. Run: `make tanking` then click Save.")
    else:
        df_tank = pd.DataFrame(tank_rows)
        for col in ["motivation_differential", "current_price", "drift_24h"]:
            df_tank[col] = pd.to_numeric(df_tank[col], errors="coerce")

        df_tank["time_to_game"] = df_tank["game_start"].apply(_hours_until)
        df_tank["price_pct"] = (df_tank["current_price"] * 100).round(1).astype(str) + "¢"
        df_tank["drift_fmt"] = (df_tank["drift_24h"] * 100).round(2).astype(str) + "%"
        df_tank["diff_fmt"] = df_tank["motivation_differential"].round(2)
        df_tank["matchup"] = df_tank["motivated_team"] + " vs " + df_tank["tanking_team"]

        show = df_tank[[
            "action", "pattern_strength", "matchup",
            "price_pct", "diff_fmt", "drift_fmt", "time_to_game"
        ]].rename(columns={
            "action": "Action", "pattern_strength": "Strength",
            "matchup": "Matchup", "price_pct": "Price",
            "diff_fmt": "Motivation Δ", "drift_fmt": "Drift 24h",
            "time_to_game": "Time to game"
        })

        styled = show.style \
            .apply(_color_action, subset=["Action"]) \
            .apply(_color_strength, subset=["Strength"])
        st.dataframe(styled, use_container_width=True, hide_index=True)

        buy_rows = [r for r in tank_rows if r.get("action") == "BUY"]
        if buy_rows:
            best = buy_rows[0]
            st.success(
                f"**Best BUY:** {best.get('motivated_team')} vs {best.get('tanking_team')} | "
                f"Price: {float(best.get('current_price',0))*100:.0f}¢ | "
                f"Differential: {float(best.get('motivation_differential',0)):.2f} | "
                f"Strength: **{best.get('pattern_strength')}** | "
                f"Game: {_hours_until(best.get('game_start'))}"
            )

    st.divider()

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 3: MLB PITCHER SIGNALS
    # ══════════════════════════════════════════════════════════════════════
    st.subheader(f"⚾ MLB Pitcher ERA Mismatches ({len(pitcher_rows)})")

    if not pitcher_rows:
        st.info("No actionable MLB pitcher signals. Run: `make mlb` then click Re-scan on the MLB page.")
    else:
        df_pitch = pd.DataFrame(pitcher_rows)
        for col in ["era_differential", "quality_differential", "current_price", "drift_24h"]:
            df_pitch[col] = pd.to_numeric(df_pitch[col], errors="coerce")

        df_pitch["time_to_game"] = df_pitch["game_start"].apply(_hours_until)
        df_pitch["price_pct"] = (df_pitch["current_price"] * 100).round(1).astype(str) + "¢"
        df_pitch["era_diff_fmt"] = df_pitch["era_differential"].round(2)
        df_pitch["matchup"] = df_pitch["favored_team"] + " vs " + df_pitch["underdog_team"]
        df_pitch["pitchers"] = (
            df_pitch["home_pitcher"].fillna("?") + " vs " + df_pitch["away_pitcher"].fillna("?")
        )

        show = df_pitch[[
            "action", "signal_strength", "matchup", "pitchers",
            "era_diff_fmt", "price_pct", "time_to_game"
        ]].rename(columns={
            "action": "Action", "signal_strength": "Strength",
            "matchup": "Matchup", "pitchers": "Pitchers",
            "era_diff_fmt": "ERA Δ", "price_pct": "Price",
            "time_to_game": "Time to game",
        })

        styled = show.style \
            .apply(_color_action, subset=["Action"]) \
            .apply(_color_strength, subset=["Strength"])
        st.dataframe(styled, use_container_width=True, hide_index=True)

        buy_pitcher = [r for r in pitcher_rows if r.get("action") == "BUY"]
        if buy_pitcher:
            best = buy_pitcher[0]
            st.success(
                f"**Best MLB BUY:** {best.get('favored_team')} vs {best.get('underdog_team')} | "
                f"ERA diff: {float(best.get('era_differential') or 0):.2f} | "
                f"Price: {float(best.get('current_price',0))*100:.0f}¢ | "
                f"Strength: **{best.get('signal_strength')}** | "
                f"Game: {_hours_until(best.get('game_start'))}"
            )

    st.divider()

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 4: GO MARKETS WITH DRIFT
    # ══════════════════════════════════════════════════════════════════════
    st.subheader(f"📈 GO Markets Moving ({len(go_rows)})")
    st.caption("GO-verdict markets with >2% price drift in last 6h — potential momentum entries")

    if not go_rows:
        st.info("No GO markets with significant recent drift.")
    else:
        df_go = pd.DataFrame(go_rows)
        for col in ["drift_6h", "price_now", "ratio_24h", "taker_rt_cost", "spread_pct"]:
            df_go[col] = pd.to_numeric(df_go[col], errors="coerce")

        df_go["drift_pct"] = (df_go["drift_6h"] * 100).round(2)
        df_go["price_pct"] = (df_go["price_now"] * 100).round(1)
        df_go["time_to_game"] = df_go["event_start"].apply(_hours_until)
        df_go["question_short"] = df_go["question"].str[:55]

        show = df_go[[
            "question_short", "sport", "price_pct", "drift_pct",
            "ratio_24h", "taker_rt_cost", "spread_pct", "time_to_game"
        ]].rename(columns={
            "question_short": "Market", "sport": "Sport",
            "price_pct": "Price (¢)", "drift_pct": "Drift 6h%",
            "ratio_24h": "Ratio 24h", "taker_rt_cost": "Cost%",
            "spread_pct": "Spread%", "time_to_game": "Time to game"
        })

        styled = show.style.apply(_color_drift, subset=["Drift 6h%"])
        st.dataframe(styled, use_container_width=True, hide_index=True)
