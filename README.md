# Polymarket Sports — Фаза 0 + Фаза 1

Автоматичний збір даних зі спортивних ринків Polymarket для cost analysis і prematch trading research.

---

## Що це робить

**Фаза 0 (cost_analyzer.py):** Сканує всі спортивні ринки Polymarket, збирає orderbook, рахує costs, видає таблицю з вердиктом GO / MARGINAL / NO_GO.

**Фаза 1 (main.py):** Безперервний збір даних 24/7 — snapshot'и цін кожні 5 хвилин + всі трейди через WebSocket. Пише в PostgreSQL.

---

## Встановлення

### 1. PostgreSQL + TimescaleDB

```bash
# Ubuntu/Debian
sudo apt install postgresql postgresql-contrib
# TimescaleDB: https://docs.timescale.com/install/latest/

# macOS
brew install postgresql
brew install timescaledb
timescaledb-tune
brew services restart postgresql

# Створити БД
sudo -u postgres createdb polymarket_sports
sudo -u postgres psql -c "CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;" polymarket_sports
```

### 2. Python залежності

```bash
cd polymarket-sports
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# або venv\Scripts\activate  # Windows

pip install -r requirements.txt
```

### 3. Конфігурація

```bash
cp config/settings.example.yaml config/settings.yaml
# Відредагуй config/settings.yaml — вкажи свої параметри PostgreSQL
```

### 4. Ініціалізація БД

```bash
python db/init_schema.py
```

---

## Запуск

### Фаза 0 — Cost Analysis (одноразово, ~3-5 хвилин)

```bash
python cost_analyzer.py
```

Результат: таблиця в консолі + файл `phase0_results.csv` + дані в таблиці `markets` в БД.

Дивишся на таблицю. Якщо avg_ratio > 1.5 по цільовій лізі → переходиш до Фази 1.

### Фаза 1 — Data Collection (24/7, 2 тижні)

```bash
python main.py
```

Працює безперервно. Логи в консоль + `logs/collector.log`. Зупинити: `Ctrl+C`.

Для запуску у фоні:
```bash
nohup python main.py > logs/stdout.log 2>&1 &
```

---

## Структура проекту

```
polymarket-sports/
├── config/
│   ├── settings.example.yaml  # Шаблон конфігу
│   └── settings.yaml          # Твій конфіг (gitignored)
├── collector/
│   ├── __init__.py
│   ├── ws_client.py           # WebSocket збір даних
│   ├── rest_client.py         # REST API клієнт
│   ├── market_discovery.py    # Пошук і фільтрація ринків
│   └── normalizer.py          # Нормалізація даних
├── db/
│   ├── __init__.py
│   ├── schema.sql             # SQL схема
│   ├── init_schema.py         # Створення таблиць
│   └── repository.py          # Запити до БД
├── analytics/
│   ├── __init__.py
│   └── cost_analyzer.py       # Логіка Фази 0
├── alerts/
│   ├── __init__.py
│   └── logger_alert.py        # Логування алертів
├── tests/
├── logs/
├── cost_analyzer.py           # Entry point Фази 0
├── main.py                    # Entry point Фази 1
├── requirements.txt
└── README.md
```
