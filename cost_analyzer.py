"""
Фаза 0 — Cost Analysis
Сканує всі спортивні ринки Polymarket, рахує costs, видає таблицю з вердиктом.

Запуск: python cost_analyzer.py
Результат: phase0_results.csv + вивід в консоль + дані в БД
"""

import asyncio
import logging
import sys
from pathlib import Path

import pandas as pd
import yaml
from tabulate import tabulate

from collector.rest_client import RestClient
from collector.market_discovery import MarketDiscovery
from analytics.cost_analyzer import analyze_market, aggregate_by_league
from db.repository import Repository
from alerts.logger_alert import LoggerAlert
from config.validate import validate_config


# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/phase0.log", mode="a"),
    ],
)
logger = logging.getLogger(__name__)


async def run_phase0():
    # Load config
    config_path = Path("config/settings.yaml")
    if not config_path.exists():
        print("ERROR: config/settings.yaml не знайдено.")
        print("Скопіюй config/settings.example.yaml → config/settings.yaml")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    validate_config(config)

    # Init
    rest = RestClient(config)
    await rest.start()
    discovery = MarketDiscovery(rest, config)
    repo = await Repository.create(config)
    alert = LoggerAlert(config)

    try:
        # 1. Знайти всі спортивні ринки
        logger.info("Крок 1: Пошук спортивних ринків...")
        all_markets = await discovery.discover_all_sports_markets()

        if not all_markets:
            logger.error("Не знайдено жодного спортивного ринку")
            return

        # 2. Фільтр по volume
        markets = discovery.filter_for_phase0(all_markets)

        if not markets:
            logger.error("Жоден ринок не пройшов фільтр volume")
            return

        # 3. Зберегти ринки в БД
        logger.info("Крок 2: Збереження ринків в БД...")
        for m in markets:
            try:
                await repo.upsert_market(m)
            except Exception as e:
                logger.error(f"upsert_market failed for {m.get('slug')}: {e}")

        # 4. Зібрати orderbook + fees + history по кожному ринку
        logger.info(f"Крок 3: Збір orderbook для {len(markets)} ринків...")
        results = []

        for i, market in enumerate(markets):
            token_id = market.get("token_id_yes")
            if not token_id:
                continue

            logger.info(
                f"  [{i+1}/{len(markets)}] {market['slug'][:50]}..."
            )

            # Orderbook
            orderbook = await rest.get_orderbook(token_id)
            if not orderbook:
                logger.warning(f"    Немає orderbook, пропуск")
                continue

            # Fee rate
            fee_rate = await rest.get_fee_rate(token_id)

            # Price history
            history = await rest.get_price_history(token_id)

            if i < 3:  # debug перших 3
                logger.info(f"    DEBUG orderbook: bid={orderbook.get('best_bid')} ask={orderbook.get('best_ask')} mid={orderbook.get('mid_price')}")
                logger.info(f"    DEBUG fee_rate: {fee_rate}")
                logger.info(f"    DEBUG history points: {len(history) if history else 0}")
                if history and len(history) > 0:
                    logger.info(f"    DEBUG first point: {history[0]}")
                    logger.info(f"    DEBUG last point: {history[-1]}")

            # Аналіз
            result = analyze_market(market, orderbook, fee_rate, history, config)
            results.append(result)

            # Зберегти в БД
            try:
                await repo.insert_cost_analysis(result)
            except Exception as e:
                logger.error(f"insert_cost_analysis failed for {market.get('slug')}: {e}")

        if not results:
            logger.error("Жоден ринок не проаналізовано")
            return

        # 5. Вивести таблицю
        logger.info(f"\nКрок 4: Результати ({len(results)} ринків)\n")

        df = pd.DataFrame(results)

        # Основна таблиця — сортування по ratio
        display_cols = [
            "slug", "sport", "league", "best_bid", "best_ask",
            "spread_pct", "bid_depth", "ask_depth", "volume_24h",
            "fee_rate", "taker_rt_cost", "move_24h", "ratio_24h", "verdict",
        ]
        df_display = df[display_cols].copy()
        df_display["slug"] = df_display["slug"].str[:40]
        df_display = df_display.sort_values("ratio_24h", ascending=False, na_position="last")

        print("\n" + "=" * 100)
        print("  ФАЗА 0: COST ANALYSIS — РЕЗУЛЬТАТИ ПО РИНКАХ")
        print("=" * 100)
        print(tabulate(df_display, headers="keys", tablefmt="simple", showindex=False, floatfmt=".4f"))

        # 6. Агрегація по лігах
        league_summary = aggregate_by_league(results)

        print("\n" + "=" * 100)
        print("  АГРЕГАЦІЯ ПО ЛІГАХ")
        print("=" * 100)
        if league_summary:
            df_leagues = pd.DataFrame(league_summary)
            print(tabulate(df_leagues, headers="keys", tablefmt="simple", showindex=False, floatfmt=".2f"))
        else:
            print("  Немає даних для агрегації")

        # 7. Загальний verdict
        go = sum(1 for r in results if r["verdict"] == "GO")
        marginal = sum(1 for r in results if r["verdict"] == "MARGINAL")
        no_go = sum(1 for r in results if r["verdict"] == "NO_GO")
        no_data = sum(1 for r in results if r["verdict"] == "NO_DATA")

        print("\n" + "=" * 100)
        print(f"  ЗАГАЛЬНИЙ VERDICT")
        print(f"  GO: {go}  |  MARGINAL: {marginal}  |  NO_GO: {no_go}  |  NO_DATA: {no_data}")

        if go > 0:
            print(f"\n  ✓ Є {go} ринків з ratio > 2.0 — можна переходити до Фази 1")
        elif marginal > 0:
            print(f"\n  ⚠ Є {marginal} ринків в сірій зоні (ratio 1.5–2.0)")
            print(f"    Maker стратегія може працювати. Taker — сумнівно.")
        else:
            print(f"\n  ✗ Edge неможливий при поточній fee structure.")
            print(f"    Рекомендація: СТОП або чекати зміни комісій.")
        print("=" * 100)

        # 8. Зберегти CSV
        output_file = config["phase0"]["output_file"]
        df.to_csv(output_file, index=False)
        logger.info(f"\nРезультати збережено: {output_file}")

        await alert.phase0_complete(len(results), go, marginal, no_go)

    finally:
        await rest.close()
        await repo.close()


if __name__ == "__main__":
    Path("logs").mkdir(exist_ok=True)
    asyncio.run(run_phase0())
