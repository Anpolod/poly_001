# Деплой на Mac Mini

> Полная пошаговая инструкция по установке Polymarket Trading Bot на Mac Mini.
> Актуально на: 2026-04-12

---

## Что потребуется

- Mac Mini с macOS (всегда подключён к сети)
- Доступ по SSH с MacBook
- `POLYGON_PRIVATE_KEY` (приватный ключ кошелька Polygon)
- Telegram Bot Token + Chat ID (уже в `config/settings.yaml`)

---

## Шаг 1 — Homebrew

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

После установки добавь в `~/.zprofile`:

```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
source ~/.zprofile
```

---

## Шаг 2 — Python 3.12

```bash
brew install python@3.12
python3 --version   # → Python 3.12.x
```

---

## Шаг 3 — PostgreSQL 16 + TimescaleDB

```bash
brew install postgresql@16
brew services start postgresql@16

brew install timescaledb
timescaledb-tune --quiet --yes

brew services restart postgresql@16
```

Добавь `postgresql@16` в PATH:

```bash
echo 'export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"' >> ~/.zprofile
source ~/.zprofile
```

---

## Шаг 4 — Создать базу данных

```bash
psql postgres -c "CREATE DATABASE polymarket_sports;"
psql polymarket_sports -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
```

Проверь:

```bash
psql polymarket_sports -U postgres -c "SELECT extname FROM pg_extension;"
# должен вывести timescaledb
```

---

## Шаг 5 — Перенести код

### Вариант A — через Git (рекомендуется)

```bash
# На Mac Mini:
git clone git@github.com:ВАШ_ЛОГИН/polymarket-sports.git
cd polymarket-sports
```

### Вариант B — через rsync напрямую с MacBook

```bash
# Выполнять на MacBook:
rsync -av \
  --exclude='venv' \
  --exclude='__pycache__' \
  --exclude='logs' \
  --exclude='.env' \
  ~/Documents/dev/projects/polymarket-sports/ \
  USERNAME@MAC_MINI_IP:~/polymarket-sports/
```

---

## Шаг 6 — Виртуальное окружение и зависимости

```bash
cd ~/polymarket-sports
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Шаг 7 — Файл .env с приватным ключом

```bash
echo "POLYGON_PRIVATE_KEY=0x_ВАШ_КЛЮЧ" > .env
chmod 600 .env
```

> ⚠️ `.env` никогда не коммитится в git — он в `.gitignore`.

---

## Шаг 8 — Настроить config/settings.yaml

```bash
cp config/settings.example.yaml config/settings.yaml
nano config/settings.yaml
```

Что изменить обязательно:

```yaml
database:
  host: localhost
  port: 5432               # brew postgres — порт 5432 (не 5433 как в Docker)
  name: polymarket_sports
  user: postgres
  password: ""             # пустой — brew postgres без пароля по умолчанию

trading:
  enabled: true
  dry_run: true            # сначала true — убедиться что всё работает
```

---

## Шаг 9 — Накатить схему БД

```bash
source venv/bin/activate
python db/init_schema.py
```

Проверь таблицы:

```bash
psql polymarket_sports -U postgres -c "\dt"
```

Ожидаемые таблицы: `markets`, `price_snapshots`, `trades`, `open_positions`, `order_log`, `cost_analysis`, `tanking_signals`, `prop_scan_log`, `pitcher_signals`, `data_gaps`.

---

## Шаг 10 — Перенос данных из старой БД (опционально)

Если хочешь сохранить историю цен и маркеты:

```bash
# На MacBook (Docker, порт 5433):
docker exec polymarket-timescale pg_dump \
  -U postgres -d polymarket_sports \
  --table=markets \
  --table=price_snapshots \
  --table=cost_analysis \
  --table=tanking_signals \
  --table=pitcher_signals \
  -Fc > polymarket_backup.dump

# Скопировать файл на Mac Mini:
scp polymarket_backup.dump USERNAME@MAC_MINI_IP:~/

# На Mac Mini:
pg_restore -h localhost -U postgres -d polymarket_sports \
  --no-owner --no-privileges \
  ~/polymarket_backup.dump
```

---

## Шаг 11 — Запуск всех сервисов

```bash
cd ~/polymarket-sports
make start
```

Это запустит:
- **Trading bot** → `logs/bot.log`
- **Streamlit dashboard** → `http://localhost:8501`
- **Watchdog** → автоматический перезапуск бота при падении

Проверить статус:

```bash
make status   # PIDs + uptime
make logs     # tail -f всех логов
```

---

## Шаг 12 — Доступ с MacBook

### Вариант A — SSH туннель (если Mac Mini не в локальной сети)

```bash
# На MacBook:
ssh -L 8501:localhost:8501 USERNAME@MAC_MINI_IP
# Потом открой браузер: http://localhost:8501
```

### Вариант B — прямой доступ (если в одной WiFi сети)

```bash
open http://MAC_MINI_IP:8501
```

Узнать IP Mac Mini:

```bash
# На Mac Mini:
ipconfig getifaddr en0
```

---

## Шаг 13 — Перейти в live режим

Когда убедился что бот работает в dry_run:

```yaml
# config/settings.yaml
trading:
  dry_run: false
```

```bash
make stop && make start
```

---

## Управление ботом

| Команда | Что делает |
|---|---|
| `make start` | Запустить бот + dashboard + watchdog |
| `make stop` | Остановить всё |
| `make status` | PIDs и последние логи |
| `make logs` | Стриминг всех логов |
| `make trading` | Запустить только бот (foreground) |
| `make dashboard` | Запустить только dashboard |
| `make mlb` | Разовый скан MLB pitcher mismatches |
| `make mlb-watch` | MLB scanner в watch mode (30 мин) + save to DB |
| `make tanking` | Разовый скан NBA tanking (сезонный) |

---

## Telegram команды

| Команда | Описание |
|---|---|
| `/status` | Баланс, открытые позиции, экспозиция |
| `/positions` | Список позиций с unrealized P&L |
| `/pnl` | P&L сегодня / всё время по стратегиям |
| `/week` | P&L по дням за последние 7 дней |
| `/cancel <id>` | Отменить позицию вручную |

---

## Автозапуск при перезагрузке Mac Mini

Чтобы бот стартовал автоматически после reboot:

```bash
# Создать LaunchAgent:
cat > ~/Library/LaunchAgents/com.polybot.trading.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.polybot.trading</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/Users/USERNAME/polymarket-sports/scripts/start_bot.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>WorkingDirectory</key>
  <string>/Users/USERNAME/polymarket-sports</string>
  <key>StandardOutPath</key>
  <string>/Users/USERNAME/polymarket-sports/logs/launchd.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/USERNAME/polymarket-sports/logs/launchd.log</string>
</dict>
</plist>
EOF

# Заменить USERNAME на реальное имя пользователя, потом:
launchctl load ~/Library/LaunchAgents/com.polybot.trading.plist
```

---

## Возможные проблемы

### `psql: command not found`
```bash
export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
```

### `timescaledb-tune not found`
```bash
brew link timescaledb
```

### `POLYGON_PRIVATE_KEY not set`
```bash
cat .env   # проверить что файл существует и не пустой
```

### Dashboard не открывается с MacBook
```bash
# Проверить что порт 8501 слушается:
ssh USERNAME@MAC_MINI_IP "lsof -i:8501"
```

### Бот падает сразу после запуска
```bash
tail -50 logs/bot.log   # смотреть последнюю ошибку
```
