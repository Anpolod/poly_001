"""
Phase 0 Analyzer for Polymarket Sports Markets
===============================================

Kill / Proceed Condition:
- PROCEED to Phase 1 if: ratio_24h > 1.5 on the clean GO subset (GO markets with zero flags)
- STOP if: clean_go_pct < 15% of all markets with data (i.e. non-NO_DATA rows)

Verdicts: GO / MARGINAL / NO_GO / NO_DATA
Flags applied to GO markets only:
  - LOW_VOL: volume_24h < 10000
  - EXTREME_ODDS: mid_price < 0.15 or mid_price > 0.85
  - THIN_DEPTH: min(bid_depth, ask_depth) < 1000
  - MOVE_EXCEEDS_HALF_PRICE: move_24h > mid_price * 0.5
  - RATIO_OUTLIER: ratio_24h > 20

"Clean GO" = GO with zero flags. These are the actionable candidates.
Markets with ratio_24h > 20 are separately noted as potential data artifacts.

Usage:
    python -m analytics.phase0_analyzer [csv_path]
    python analytics/phase0_analyzer.py [csv_path]

csv_path defaults to phase0_results.csv (relative to cwd or project root).
"""

from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pct(n: int, total: int) -> float:
    if total == 0:
        return 0.0
    return round(100.0 * n / total, 2)


def _safe_stats(series: pd.Series, label: str) -> dict[str, Any]:
    """Return avg/median/p75/p90 for a series, handling all-NaN gracefully."""
    s = series.dropna()
    if s.empty:
        return {f"{label}_avg": None, f"{label}_median": None,
                f"{label}_p75": None, f"{label}_p90": None}
    return {
        f"{label}_avg": round(float(s.mean()), 6),
        f"{label}_median": round(float(s.median()), 6),
        f"{label}_p75": round(float(s.quantile(0.75)), 6),
        f"{label}_p90": round(float(s.quantile(0.90)), 6),
    }


def _cost_stats(series: pd.Series, label: str) -> dict[str, Any]:
    s = series.dropna()
    if s.empty:
        return {f"{label}_avg": None, f"{label}_min": None, f"{label}_max": None}
    return {
        f"{label}_avg": round(float(s.mean()), 6),
        f"{label}_min": round(float(s.min()), 6),
        f"{label}_max": round(float(s.max()), 6),
    }


def _ratio_stats(series: pd.Series) -> dict[str, Any]:
    s = series.dropna()
    if s.empty:
        return {"ratio_mean": None, "ratio_median": None,
                "pct_gt_2": None, "pct_gt_1_5": None}
    n = len(s)
    return {
        "ratio_mean": round(float(s.mean()), 6),
        "ratio_median": round(float(s.median()), 6),
        "pct_gt_2": round(_pct(int((s > 2.0).sum()), n), 2),
        "pct_gt_1_5": round(_pct(int((s > 1.5).sum()), n), 2),
    }


try:
    from tabulate import tabulate as _tabulate

    def _print_table(rows: list[list], headers: list[str], title: str = "") -> None:
        if title:
            print(f"\n{'='*60}")
            print(f"  {title}")
            print(f"{'='*60}")
        print(_tabulate(rows, headers=headers, tablefmt="rounded_outline"))

except ImportError:
    def _print_table(rows: list[list], headers: list[str], title: str = "") -> None:
        if title:
            print(f"\n{'='*60}")
            print(f"  {title}")
            print(f"{'='*60}")
        col_widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0))
                      for i, h in enumerate(headers)]
        fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
        print(fmt.format(*headers))
        print("  ".join("-" * w for w in col_widths))
        for row in rows:
            print(fmt.format(*[str(v) for v in row]))


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

MOVE_COLS = ["move_1h", "move_6h", "move_24h", "move_48h", "move_72h"]
COST_COLS = ["taker_rt_cost", "spread_pct", "maker_rt_cost"]

FLAGS = {
    "LOW_VOL": lambda df: df["volume_24h"] < 10_000,
    "EXTREME_ODDS": lambda df: (df["mid_price"] < 0.15) | (df["mid_price"] > 0.85),
    "THIN_DEPTH": lambda df: df[["bid_depth", "ask_depth"]].min(axis=1) < 1_000,
    "MOVE_EXCEEDS_HALF_PRICE": lambda df: df["move_24h"] > df["mid_price"] * 0.5,
    "RATIO_OUTLIER": lambda df: df["ratio_24h"] > 20,
}


def load_csv(csv_path: str | Path) -> pd.DataFrame:
    path = Path(csv_path)
    if not path.exists():
        # try relative to project root (two levels up from this file)
        project_root = Path(__file__).parent.parent
        alt = project_root / csv_path
        if alt.exists():
            path = alt
        else:
            raise FileNotFoundError(
                f"CSV not found at '{csv_path}' or '{alt}'"
            )
    df = pd.read_csv(path)
    return df


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["mid_price"] = (df["best_bid"] + df["best_ask"]) / 2.0
    # volume tier
    def vol_tier(v):
        if pd.isna(v):
            return "unknown"
        if v < 10_000:
            return "<10K"
        if v <= 50_000:
            return "10K-50K"
        return ">50K"
    df["vol_tier"] = df["volume_24h"].apply(vol_tier)
    # price regime
    df["price_regime"] = np.where(
        (df["mid_price"] < 0.15) | (df["mid_price"] > 0.85),
        "extreme", "mid-range"
    )
    return df


def apply_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Add flag columns and flag_list; only meaningful for GO markets."""
    go_mask = df["verdict"] == "GO"
    for flag_name, fn in FLAGS.items():
        col = f"flag_{flag_name}"
        df[col] = False
        df.loc[go_mask, col] = fn(df[go_mask])
    flag_cols = [f"flag_{f}" for f in FLAGS]
    df["flag_count"] = df[flag_cols].sum(axis=1)
    df["flag_list"] = df.apply(
        lambda row: [f for f in FLAGS if row.get(f"flag_{f}", False)], axis=1
    )
    df["is_clean_go"] = (df["verdict"] == "GO") & (df["flag_count"] == 0)
    return df


def verdict_distribution(df: pd.DataFrame) -> dict:
    total = len(df)
    verdicts = ["GO", "MARGINAL", "NO_GO", "NO_DATA"]
    dist: dict[str, Any] = {}
    for v in verdicts:
        n = int((df["verdict"] == v).sum())
        dist[v] = {"count": n, "pct": _pct(n, total)}
    dist["total"] = total
    return dist


def cost_structure(df: pd.DataFrame) -> dict:
    data_df = df[df["verdict"] != "NO_DATA"]
    result = {}
    for col in COST_COLS:
        result.update(_cost_stats(data_df[col], col))
    return result


def move_stats(df: pd.DataFrame) -> dict:
    data_df = df[df["verdict"] != "NO_DATA"]
    result = {}
    for col in MOVE_COLS:
        result.update(_safe_stats(data_df[col], col))
    return result


def ratio_stats(df: pd.DataFrame) -> dict:
    data_df = df[df["verdict"] != "NO_DATA"]
    return _ratio_stats(data_df["ratio_24h"])


def segment_by(df: pd.DataFrame, col: str) -> dict:
    result = {}
    for val, grp in df.groupby(col, dropna=False):
        key = str(val) if not pd.isna(val) else "unknown"
        data_grp = grp[grp["verdict"] != "NO_DATA"]
        result[key] = {
            "count": len(grp),
            "go_count": int((grp["verdict"] == "GO").sum()),
            "clean_go_count": int(grp.get("is_clean_go", pd.Series(False)).sum()),
            "ratio": _ratio_stats(data_grp["ratio_24h"]),
            "cost": {c: _cost_stats(data_grp[c], c) for c in COST_COLS},
            "moves": {c: _safe_stats(data_grp[c], c) for c in MOVE_COLS},
        }
    return result


def flag_summary(df: pd.DataFrame) -> dict:
    go_df = df[df["verdict"] == "GO"]
    total_go = len(go_df)
    flags_detail = {}
    for flag_name in FLAGS:
        col = f"flag_{flag_name}"
        n = int(go_df[col].sum()) if col in go_df.columns else 0
        flags_detail[flag_name] = {"count": n, "pct_of_go": _pct(n, total_go)}
    clean_go = int(df["is_clean_go"].sum())
    total_with_data = int((df["verdict"] != "NO_DATA").sum())
    return {
        "total_go": total_go,
        "clean_go": clean_go,
        "clean_go_pct_of_go": _pct(clean_go, total_go),
        "clean_go_pct_of_data_markets": _pct(clean_go, total_with_data),
        "flags": flags_detail,
    }


def artifact_markets(df: pd.DataFrame) -> list[dict]:
    """Markets with ratio_24h > 20 — potential data artifacts."""
    artifacts = df[df["ratio_24h"] > 20].copy()
    cols = ["market_id", "slug", "sport", "league", "verdict",
            "ratio_24h", "volume_24h", "mid_price"]
    cols_present = [c for c in cols if c in artifacts.columns]
    return artifacts[cols_present].to_dict(orient="records")


def recommendation(flag_s: dict, ratio_s: dict) -> dict:
    """Derive proceed/stop recommendation."""
    clean_go_pct = flag_s["clean_go_pct_of_data_markets"]
    stop_reason = None
    proceed = True

    if clean_go_pct < 15.0:
        proceed = False
        stop_reason = f"clean_go_pct ({clean_go_pct:.1f}%) < 15% threshold"

    return {
        "recommendation": "PROCEED_TO_PHASE_1" if proceed else "STOP",
        "stop_reason": stop_reason,
        "clean_go_pct_of_data_markets": clean_go_pct,
        "note": (
            "Proceed if ratio_24h > 1.5 on clean GO subset; "
            "Stop if clean_go_pct < 15% of all markets with data."
        ),
    }


def run_analysis(df: pd.DataFrame) -> dict:
    df = prepare(df)
    df = apply_flags(df)

    clean_go_df = df[df["is_clean_go"]]

    verdict_dist = verdict_distribution(df)
    cost = cost_structure(df)
    moves = move_stats(df)
    ratios = ratio_stats(df)
    ratios_clean_go = _ratio_stats(clean_go_df["ratio_24h"])

    flag_s = flag_summary(df)
    artifacts = artifact_markets(df)
    rec = recommendation(flag_s, ratios_clean_go)

    seg_sport = segment_by(df, "sport")
    seg_verdict = segment_by(df, "verdict")
    seg_vol = segment_by(df, "vol_tier")
    seg_regime = segment_by(df, "price_regime")

    return {
        "verdict_distribution": verdict_dist,
        "cost_structure": cost,
        "move_stats": moves,
        "ratio_stats": ratios,
        "ratio_stats_clean_go": ratios_clean_go,
        "flag_summary": flag_s,
        "recommendation": rec,
        "artifacts_ratio_gt20": artifacts,
        "segments": {
            "by_sport": seg_sport,
            "by_verdict": seg_verdict,
            "by_vol_tier": seg_vol,
            "by_price_regime": seg_regime,
        },
        "_df": df,  # internal; stripped before JSON export
    }


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def print_summary(analysis: dict) -> None:
    vd = analysis["verdict_distribution"]
    fs = analysis["flag_summary"]
    rec = analysis["recommendation"]

    print("\n" + "=" * 60)
    print("  PHASE 0 ANALYSIS SUMMARY")
    print("=" * 60)

    # Verdict distribution
    rows = []
    for v in ["GO", "MARGINAL", "NO_GO", "NO_DATA"]:
        d = vd[v]
        rows.append([v, d["count"], f"{d['pct']}%"])
    rows.append(["TOTAL", vd["total"], "100%"])
    _print_table(rows, ["Verdict", "Count", "%"], "Verdict Distribution")

    # Cost structure
    cs = analysis["cost_structure"]
    cost_rows = []
    for col in COST_COLS:
        cost_rows.append([
            col,
            cs.get(f"{col}_avg", "N/A"),
            cs.get(f"{col}_min", "N/A"),
            cs.get(f"{col}_max", "N/A"),
        ])
    _print_table(cost_rows, ["Metric", "Avg", "Min", "Max"], "Cost Structure")

    # Move stats
    ms = analysis["move_stats"]
    move_rows = []
    for col in MOVE_COLS:
        move_rows.append([
            col,
            ms.get(f"{col}_avg", "N/A"),
            ms.get(f"{col}_median", "N/A"),
            ms.get(f"{col}_p75", "N/A"),
            ms.get(f"{col}_p90", "N/A"),
        ])
    _print_table(move_rows, ["Move", "Avg", "Median", "P75", "P90"], "Move Stats")

    # Ratio stats
    rs = analysis["ratio_stats"]
    rsc = analysis["ratio_stats_clean_go"]
    ratio_rows = [
        ["All data markets", rs.get("ratio_mean"), rs.get("ratio_median"),
         f"{rs.get('pct_gt_2')}%", f"{rs.get('pct_gt_1_5')}%"],
        ["Clean GO only", rsc.get("ratio_mean"), rsc.get("ratio_median"),
         f"{rsc.get('pct_gt_2')}%", f"{rsc.get('pct_gt_1_5')}%"],
    ]
    _print_table(ratio_rows, ["Subset", "Mean", "Median", ">2.0%", ">1.5%"], "Ratio Stats (ratio_24h)")

    # Flag summary
    flag_rows = []
    for fname, fd in fs["flags"].items():
        flag_rows.append([fname, fd["count"], f"{fd['pct_of_go']}%"])
    flag_rows.append(["CLEAN GO (no flags)", fs["clean_go"],
                       f"{fs['clean_go_pct_of_go']}% of GO"])
    _print_table(flag_rows, ["Flag", "Count", "% of GO markets"], "GO Flag Summary")

    # Segment by sport
    seg_sport = analysis["segments"]["by_sport"]
    sport_rows = []
    for sport, sd in sorted(seg_sport.items()):
        r = sd["ratio"]
        sport_rows.append([
            sport, sd["count"], sd["go_count"], sd["clean_go_count"],
            r.get("ratio_median", "N/A"), f"{r.get('pct_gt_1_5', 'N/A')}%"
        ])
    _print_table(sport_rows,
                 ["Sport", "Markets", "GO", "Clean GO", "Ratio Med", ">1.5%"],
                 "Segment: by Sport")

    # Segment by vol tier
    seg_vol = analysis["segments"]["by_vol_tier"]
    vol_rows = []
    for tier in ["<10K", "10K-50K", ">50K", "unknown"]:
        if tier not in seg_vol:
            continue
        sd = seg_vol[tier]
        r = sd["ratio"]
        vol_rows.append([
            tier, sd["count"], sd["go_count"], sd["clean_go_count"],
            r.get("ratio_median", "N/A"), f"{r.get('pct_gt_1_5', 'N/A')}%"
        ])
    _print_table(vol_rows,
                 ["Vol Tier", "Markets", "GO", "Clean GO", "Ratio Med", ">1.5%"],
                 "Segment: by Volume Tier")

    # Segment by price regime
    seg_regime = analysis["segments"]["by_price_regime"]
    regime_rows = []
    for regime in ["mid-range", "extreme"]:
        if regime not in seg_regime:
            continue
        sd = seg_regime[regime]
        r = sd["ratio"]
        regime_rows.append([
            regime, sd["count"], sd["go_count"], sd["clean_go_count"],
            r.get("ratio_median", "N/A"), f"{r.get('pct_gt_1_5', 'N/A')}%"
        ])
    _print_table(regime_rows,
                 ["Price Regime", "Markets", "GO", "Clean GO", "Ratio Med", ">1.5%"],
                 "Segment: by Price Regime")

    # Artifacts
    artifacts = analysis["artifacts_ratio_gt20"]
    if artifacts:
        print(f"\n  WARNING: {len(artifacts)} market(s) with ratio_24h > 20 (potential artifacts)")
        art_rows = [[
            a.get("market_id", "?"), a.get("slug", "?"), a.get("sport", "?"),
            a.get("verdict", "?"), a.get("ratio_24h", "?"),
            a.get("volume_24h", "?"), round(a.get("mid_price", 0), 4)
        ] for a in artifacts]
        _print_table(art_rows,
                     ["market_id", "slug", "sport", "verdict", "ratio_24h", "volume_24h", "mid_price"],
                     "Potential Artifacts (ratio_24h > 20)")

    # Recommendation
    print(f"\n{'='*60}")
    print(f"  RECOMMENDATION: {rec['recommendation']}")
    if rec["stop_reason"]:
        print(f"  Reason: {rec['stop_reason']}")
    print(f"  Clean GO: {fs['clean_go']} markets ({fs['clean_go_pct_of_data_markets']}% of data markets)")
    print(f"  Clean GO ratio_24h median: {rsc.get('ratio_median', 'N/A')}")
    print(f"  {rec['note']}")
    print(f"{'='*60}\n")


def export_json(analysis: dict, out_path: Path) -> None:
    export = {k: v for k, v in analysis.items() if k != "_df"}
    # Artifacts may contain NaN — clean
    clean_artifacts = []
    for a in export.get("artifacts_ratio_gt20", []):
        clean_artifacts.append(
            {k: (None if (isinstance(v, float) and np.isnan(v)) else v)
             for k, v in a.items()}
        )
    export["artifacts_ratio_gt20"] = clean_artifacts

    def _default(obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return None if np.isnan(obj) else float(obj)
        if isinstance(obj, (np.ndarray,)):
            return obj.tolist()
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    with open(out_path, "w") as f:
        json.dump(export, f, indent=2, default=_default)
    print(f"  JSON written to: {out_path}")


def export_clean_go_csv(df: pd.DataFrame, out_path: Path) -> None:
    clean = df[df["is_clean_go"]].drop(
        columns=[c for c in df.columns if c.startswith("flag_") or c in ("flag_count", "flag_list", "is_clean_go")],
        errors="ignore"
    )
    clean.to_csv(out_path, index=False)
    print(f"  Clean GO CSV written to: {out_path} ({len(clean)} rows)")


def export_markdown(analysis: dict, out_path: Path) -> None:
    vd = analysis["verdict_distribution"]
    fs = analysis["flag_summary"]
    rec = analysis["recommendation"]
    rs = analysis["ratio_stats"]
    rsc = analysis["ratio_stats_clean_go"]
    cs = analysis["cost_structure"]
    artifacts = analysis["artifacts_ratio_gt20"]

    lines = [
        "# Phase 0 Analysis Report",
        "",
        "> **Kill / Proceed Condition:** Proceed to Phase 1 if `ratio_24h > 1.5` on the clean GO subset. "
        "Recommend STOP if `clean_go_pct < 15%` of all markets with data.",
        "",
        "---",
        "",
        "## Verdict Distribution",
        "",
        "| Verdict | Count | % |",
        "|---------|-------|---|",
    ]
    for v in ["GO", "MARGINAL", "NO_GO", "NO_DATA"]:
        d = vd[v]
        lines.append(f"| {v} | {d['count']} | {d['pct']}% |")
    lines += [
        f"| **TOTAL** | **{vd['total']}** | 100% |",
        "",
        "---",
        "",
        "## Cost Structure (non-NO_DATA markets)",
        "",
        "| Metric | Avg | Min | Max |",
        "|--------|-----|-----|-----|",
    ]
    for col in COST_COLS:
        lines.append(
            f"| {col} | {cs.get(f'{col}_avg', 'N/A')} | {cs.get(f'{col}_min', 'N/A')} | {cs.get(f'{col}_max', 'N/A')} |"
        )
    lines += [
        "",
        "---",
        "",
        "## Ratio Stats (ratio_24h)",
        "",
        "| Subset | Mean | Median | % > 2.0 | % > 1.5 |",
        "|--------|------|--------|---------|---------|",
        f"| All data markets | {rs.get('ratio_mean')} | {rs.get('ratio_median')} | {rs.get('pct_gt_2')}% | {rs.get('pct_gt_1_5')}% |",  # noqa: E501
        f"| Clean GO only | {rsc.get('ratio_mean')} | {rsc.get('ratio_median')} | {rsc.get('pct_gt_2')}% | {rsc.get('pct_gt_1_5')}% |",  # noqa: E501
        "",
        "---",
        "",
        "## GO Flag Summary",
        "",
        f"Total GO markets: **{fs['total_go']}**  ",
        f"Clean GO (zero flags): **{fs['clean_go']}** ({fs['clean_go_pct_of_go']}% of GO, {fs['clean_go_pct_of_data_markets']}% of data markets)",  # noqa: E501
        "",
        "| Flag | Count | % of GO |",
        "|------|-------|---------|",
    ]
    for fname, fd in fs["flags"].items():
        lines.append(f"| {fname} | {fd['count']} | {fd['pct_of_go']}% |")

    lines += [
        "",
        "---",
        "",
        "## Segments",
        "",
        "### By Sport",
        "",
        "| Sport | Markets | GO | Clean GO | Ratio Median | % >1.5 |",
        "|-------|---------|----|---------:|-----------:|--------|",
    ]
    for sport, sd in sorted(analysis["segments"]["by_sport"].items()):
        r = sd["ratio"]
        lines.append(
            f"| {sport} | {sd['count']} | {sd['go_count']} | {sd['clean_go_count']} "
            f"| {r.get('ratio_median', 'N/A')} | {r.get('pct_gt_1_5', 'N/A')}% |"
        )

    lines += [
        "",
        "### By Volume Tier",
        "",
        "| Vol Tier | Markets | GO | Clean GO | Ratio Median | % >1.5 |",
        "|----------|---------|----|---------:|------------:|--------|",
    ]
    for tier in ["<10K", "10K-50K", ">50K", "unknown"]:
        sd = analysis["segments"]["by_vol_tier"].get(tier)
        if sd is None:
            continue
        r = sd["ratio"]
        lines.append(
            f"| {tier} | {sd['count']} | {sd['go_count']} | {sd['clean_go_count']} "
            f"| {r.get('ratio_median', 'N/A')} | {r.get('pct_gt_1_5', 'N/A')}% |"
        )

    lines += [
        "",
        "### By Price Regime",
        "",
        "| Price Regime | Markets | GO | Clean GO | Ratio Median | % >1.5 |",
        "|-------------|---------|----|---------:|------------:|--------|",
    ]
    for regime in ["mid-range", "extreme"]:
        sd = analysis["segments"]["by_price_regime"].get(regime)
        if sd is None:
            continue
        r = sd["ratio"]
        lines.append(
            f"| {regime} | {sd['count']} | {sd['go_count']} | {sd['clean_go_count']} "
            f"| {r.get('ratio_median', 'N/A')} | {r.get('pct_gt_1_5', 'N/A')}% |"
        )

    if artifacts:
        lines += [
            "",
            "---",
            "",
            f"## Potential Artifacts (ratio_24h > 20) — {len(artifacts)} market(s)",
            "",
            "> These markets have unusually high ratio values and may reflect data errors.",
            "> They are NOT automatically discarded — investigate before excluding.",
            "",
            "| market_id | slug | sport | verdict | ratio_24h | volume_24h | mid_price |",
            "|-----------|------|-------|---------|-----------|------------|-----------|",
        ]
        for a in artifacts:
            lines.append(
                f"| {a.get('market_id','?')} | {a.get('slug','?')} | {a.get('sport','?')} "
                f"| {a.get('verdict','?')} | {a.get('ratio_24h','?')} "
                f"| {a.get('volume_24h','?')} | {round(a.get('mid_price', 0), 4)} |"
            )

    lines += [
        "",
        "---",
        "",
        "## Recommendation",
        "",
        f"**{rec['recommendation']}**",
        "",
    ]
    if rec["stop_reason"]:
        lines.append(f"**Reason:** {rec['stop_reason']}")
        lines.append("")
    lines.append(f"*{rec['note']}*")
    lines.append("")

    with open(out_path, "w") as f:
        f.write("\n".join(lines))
    print(f"  Markdown report written to: {out_path}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "phase0_results.csv"

    print(f"Loading: {csv_path}")
    df = load_csv(csv_path)
    print(f"  {len(df)} rows loaded, columns: {list(df.columns)}")

    analysis = run_analysis(df)
    enriched_df: pd.DataFrame = analysis["_df"]

    print_summary(analysis)

    out_dir = Path(csv_path).parent if Path(csv_path).exists() else Path(".")
    export_json(analysis, out_dir / "phase0_analysis.json")
    export_clean_go_csv(enriched_df, out_dir / "phase0_clean_go.csv")
    export_markdown(analysis, out_dir / "phase0_report.md")

    print("\nDone.")


if __name__ == "__main__":
    main()
