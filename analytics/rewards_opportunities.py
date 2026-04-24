"""Polymarket LP Rewards — opportunity scanner.

Queries Gamma API for all active rewards-enabled markets and ranks them by
expected return for a given capital amount. Standalone CLI utility and
reusable module for the future market_maker agent (see plan T-58).

Each reward-enabled market publishes:
  - rewardsDailyRate: USDC emission rate per day
  - rewardsMinSize: minimum order size in shares to qualify
  - rewardsMaxSpread: max % from mid that still earns (usually 3.5)
  - liquidityClob: total existing maker liquidity competing for share

Our share of the daily emission is roughly
  share ≈ our_capital / (liquidityClob + our_capital)
(actual formula weights by tightness² and time-in-book, so this is an
upper-bound estimate).

Usage:
    python -m analytics.rewards_opportunities                   # list top 20
    python -m analytics.rewards_opportunities --capital 5000    # model $5k deployment
    python -m analytics.rewards_opportunities --min-rate 5      # only rate >= $5/day
    python -m analytics.rewards_opportunities --json            # machine-readable
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

import httpx

_GAMMA_URL = "https://gamma-api.polymarket.com/markets"
_DEFAULT_CAPITAL_USD = 20.0       # tier 1 pilot default
_DEFAULT_MIN_DAILY_RATE = 1.0     # skip dust markets
_DEFAULT_TOP_N = 20


@dataclass
class RewardOpportunity:
    slug: str
    question: str
    daily_rate_usd: float
    liquidity_clob: float
    volume_24h: float
    market_spread: float           # current best-bid to best-ask
    rewards_max_spread_pct: float  # qualification envelope
    rewards_min_size: float        # min shares per quote
    end_date: str
    condition_id: str

    def share_pct(self, capital_usd: float) -> float:
        """Upper-bound estimate of our share of the reward pool."""
        total = self.liquidity_clob + capital_usd
        return (capital_usd / total) if total > 0 else 0.0

    def projected_daily_usd(self, capital_usd: float) -> float:
        return self.daily_rate_usd * self.share_pct(capital_usd)

    def annualized_apr_pct(self, capital_usd: float) -> float:
        if capital_usd <= 0:
            return 0.0
        return (self.projected_daily_usd(capital_usd) * 365 / capital_usd) * 100


def fetch_markets(limit: int = 500, timeout: int = 30) -> list[dict]:
    """Fetch all active rewards-enabled markets from Gamma API."""
    resp = httpx.get(
        _GAMMA_URL,
        params={
            "limit": limit,
            "rewards_enabled": "true",
            "active": "true",
            "closed": "false",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def extract_opportunities(markets: list[dict]) -> list[RewardOpportunity]:
    """Parse Gamma response into RewardOpportunity rows. Skips markets
    with zero daily rate — rewards_enabled=true doesn't guarantee active emission."""
    opps: list[RewardOpportunity] = []
    for m in markets:
        clob_rewards = m.get("clobRewards") or []
        if not clob_rewards:
            continue
        # A market may have multiple emission configs (rare); take the max rate
        daily_rate = max(
            (float(r.get("rewardsDailyRate") or 0) for r in clob_rewards),
            default=0.0,
        )
        if daily_rate <= 0:
            continue
        opps.append(RewardOpportunity(
            slug=m.get("slug", ""),
            question=m.get("question", ""),
            daily_rate_usd=daily_rate,
            liquidity_clob=float(m.get("liquidityClob") or m.get("liquidityNum") or 0),
            volume_24h=float(m.get("volume24hr") or 0),
            market_spread=float(m.get("spread") or 0),
            rewards_max_spread_pct=float(m.get("rewardsMaxSpread") or 0),
            rewards_min_size=float(m.get("rewardsMinSize") or 0),
            end_date=m.get("endDate", ""),
            condition_id=clob_rewards[0].get("conditionId", ""),
        ))
    return opps


def rank_for_capital(
    opps: list[RewardOpportunity],
    capital_usd: float,
    min_daily_rate: float = _DEFAULT_MIN_DAILY_RATE,
) -> list[RewardOpportunity]:
    """Return opportunities sorted by projected daily USD, filtered by min rate."""
    filtered = [o for o in opps if o.daily_rate_usd >= min_daily_rate]
    return sorted(filtered, key=lambda o: o.projected_daily_usd(capital_usd), reverse=True)


def print_table(opps: list[RewardOpportunity], capital_usd: float, top_n: int) -> None:
    """Human-readable ranked table."""
    print(f"\nTop {top_n} rewards opportunities for ${capital_usd:,.0f} capital\n")
    header = (
        f"  {'$/day':>6}  {'share%':>7}  {'proj$/d':>8}  {'APR%':>7}  "
        f"{'liq$':>10}  {'vol24h$':>10}  {'spr':>5}  {'max%':>5}  {'minSz':>5}  slug"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for o in opps[:top_n]:
        share = o.share_pct(capital_usd) * 100
        proj = o.projected_daily_usd(capital_usd)
        apr = o.annualized_apr_pct(capital_usd)
        print(
            f"  ${o.daily_rate_usd:>5.0f}  {share:>6.2f}%  ${proj:>6.2f}  {apr:>6.1f}%  "
            f"${o.liquidity_clob:>9,.0f}  ${o.volume_24h:>9,.0f}  "
            f"{o.market_spread:>5.3f}  {o.rewards_max_spread_pct:>4.1f}%  "
            f"{o.rewards_min_size:>5.0f}  {o.slug[:50]}"
        )

    total_daily = sum(o.projected_daily_usd(capital_usd) for o in opps[:top_n])
    # Naive uniform allocation — real deployment would spread capital across top_n
    per_market = capital_usd / max(1, top_n)
    diversified_daily = sum(
        o.daily_rate_usd * (per_market / (o.liquidity_clob + per_market))
        for o in opps[:top_n]
    )
    print(
        f"\n  Single-market (concentrated) top pick: "
        f"${opps[0].projected_daily_usd(capital_usd):.2f}/day  "
        f"= {opps[0].annualized_apr_pct(capital_usd):.0f}% APR"
    )
    print(
        f"  Diversified across top {top_n} (${per_market:.0f}/market): "
        f"${diversified_daily:.2f}/day  "
        f"= {diversified_daily * 365 / capital_usd * 100:.0f}% APR"
    )
    print(
        f"  Concentrated sum (unrealistic — can't put full capital on each): "
        f"${total_daily:.2f}/day"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capital", type=float, default=_DEFAULT_CAPITAL_USD,
                    help=f"Capital in USD to model (default ${_DEFAULT_CAPITAL_USD:.0f})")
    ap.add_argument("--min-rate", type=float, default=_DEFAULT_MIN_DAILY_RATE,
                    help="Skip markets with daily rate below this ($/day)")
    ap.add_argument("--top", type=int, default=_DEFAULT_TOP_N,
                    help="How many top markets to show")
    ap.add_argument("--json", action="store_true",
                    help="Emit JSON array of opportunities instead of table")
    args = ap.parse_args()

    try:
        markets = fetch_markets()
    except httpx.HTTPError as exc:
        print(f"Gamma API error: {exc}", file=sys.stderr)
        return 1

    opps = extract_opportunities(markets)
    ranked = rank_for_capital(opps, args.capital, args.min_rate)

    if args.json:
        out = [
            {
                "slug": o.slug,
                "question": o.question,
                "daily_rate_usd": o.daily_rate_usd,
                "liquidity_clob": o.liquidity_clob,
                "share_pct": o.share_pct(args.capital) * 100,
                "projected_daily_usd": o.projected_daily_usd(args.capital),
                "annualized_apr_pct": o.annualized_apr_pct(args.capital),
                "rewards_max_spread_pct": o.rewards_max_spread_pct,
                "rewards_min_size": o.rewards_min_size,
                "market_spread": o.market_spread,
                "volume_24h": o.volume_24h,
                "end_date": o.end_date,
                "condition_id": o.condition_id,
            }
            for o in ranked[:args.top]
        ]
        print(json.dumps(out, indent=2))
    else:
        print(f"Fetched {len(markets)} rewards-enabled markets, "
              f"{len(opps)} with active daily emission, "
              f"{len(ranked)} passing rate filter >= ${args.min_rate}")
        print_table(ranked, args.capital, args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
