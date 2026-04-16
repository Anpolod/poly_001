# Перенос проекта на Mac Mini

> Пошаговая инструкция. Актуально на: 2026-04-14
> Время выполнения: ~30-40 минут

---

## Что переносим

- Код проекта (git или rsync)
- PostgreSQL + TimescaleDB (установка с нуля + схема)
- Python окружение (venv + зависимости)
- Конфиг с секретами (`.env`, `config/settings.yaml`)
- Историческую базу данных (опционально — тяжёлый дамп)

---

## Часть 1 — Подготовка Mac Mini

### Шаг 1. Установи Homebrew

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

После установки добавь в `~/.zprofile`:

```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
source ~/.zprofile
```

Проверка:
```bash
brew --version
```

---

### Шаг 2. Установи Python 3.12

```bash
brew install python@3.12
```

Добавь в PATH:
```bash
echo 'export PATH="/opt/homebrew/opt/python@3.12/bin:$PATH"' >> ~/.zprofile
source ~/.zprofile
```

Проверка:
```bash
python3.12 --version
# → Python 3.12.x
```

---

### Шаг 3. Установи PostgreSQL 16 + TimescaleDB

```bash
brew install postgresql@16
brew install timescaledb
```

Добавь PostgreSQL в PATH:
```bash
echo 'export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"' >> ~/.zprofile
source ~/.zprofile
```

Настрой TimescaleDB:
```bash
timescaledb-tune --quiet --yes
```

Запусти PostgreSQL:
```bash
brew services start postgresql@16
```

Проверка:
```bash
psql postgres -c "SELECT version();"
# → PostgreSQL 16.x ...
```

---

### Шаг 4. Создай базу данных

```bash
psql postgres -c "CREATE DATABASE polymarket_sports;"
psql polymarket_sports -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
```

Проверка:
```bash
psql polymarket_sports -U postgres -c "SELECT extname FROM pg_extension;"
# → timescaledb
```

---

## Часть 2 — Перенос кода

Выбери один из двух способов:

---

### Способ A — через Git (рекомендуется)

**Шаг 1.** На MacBook убедись что всё закоммичено:
```bash
cd ~/Documents/dev/projects/polymarket-sports
git status
git add -A && git commit -m "pre-transfer snapshot"
git push
```

**Шаг 2.** На Mac Mini:
```bash
cd ~
git clone git@github.com:ВАШ_ЛОГИН/polymarket-sports.git
cd polymarket-sports
```

> Если репо приватное — настрой SSH ключ:
> ```bash
> ssh-keygen -t ed25519 -C "macmini"
> cat ~/.ssh/id_ed25519.pub
> # Добавь ключ в GitHub → Settings → SSH Keys
> ```

> ⚠️ **`.env` и `config/settings.yaml` не попадают в git** (оба в `.gitignore`).
> После клонирования их нужно скопировать вручную — см. Шаг "Перенос секретов" ниже.

---

### Перенос секретов (только при Способе A — git)

После `git clone` на Mac Mini нужно вручную скопировать два файла с MacBook:

```bash
# Запускать на MacBook:
scp ~/Documents/dev/projects/polymarket-sports/.env \
    ВАШ_USER@IP_MAC_MINI:~/polymarket-sports/.env

scp ~/Documents/dev/projects/polymarket-sports/config/settings.yaml \
    ВАШ_USER@IP_MAC_MINI:~/polymarket-sports/config/settings.yaml
```

Что в этих файлах и почему они не в git:

| Файл | Содержит | Защита |
|---|---|---|
| `.env` | `POLYGON_PRIVATE_KEY` — приватный ключ кошелька | `.gitignore` |
| `config/settings.yaml` | Telegram token, пароль БД, параметры трейдинга | `.gitignore` |
| `config/settings.example.yaml` | Шаблон без реальных данных | коммитится в git |

После копирования на Mac Mini обязательно обнови порт БД в `settings.yaml`:
```yaml
database:
  port: 5432   # brew postgres (НЕ 5433 как в Docker на MacBook)
```

---

### Способ B — через rsync (без git)

На MacBook:
```bash
rsync -avz --progress \
  --exclude='venv/' \
  --exclude='__pycache__/' \
  --exclude='logs/' \
  --exclude='*.pyc' \
  ~/Documents/dev/projects/polymarket-sports/ \
  ВАШ_USER@IP_MAC_MINI:~/polymarket-sports/
```

> При rsync `.env` и `settings.yaml` копируются автоматически (они не в `--exclude`).
> После копирования обнови порт в `settings.yaml` на Mac Mini: `port: 5432`

Узнать IP Mac Mini:
```bash
# На Mac Mini:
ipconfig getifaddr en0
```

---

## Часть 3 — Настройка окружения на Mac Mini

### Шаг 5. Создай виртуальное окружение

```bash
cd ~/polymarket-sports
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Проверка (установи все зависимости):
```bash
python -c "import asyncpg, aiohttp, streamlit, psycopg2; print('OK')"
```

---

### Шаг 6. Создай файл .env

```bash
cat > ~/polymarket-sports/.env << 'EOF'
POLYGON_PRIVATE_KEY=0x_ВАШ_ПРИВАТНЫЙ_КЛЮЧ_POLYGON
EOF
chmod 600 ~/polymarket-sports/.env
```

> ⚠️ Приватный ключ — это ключ кошелька Polygon (MetaMask → Export Private Key).
> Никогда не коммить в git, не отправляй никому.

---

### Шаг 7. Настрой config/settings.yaml

```bash
cp ~/polymarket-sports/config/settings.example.yaml \
   ~/polymarket-sports/config/settings.yaml
nano ~/polymarket-sports/config/settings.yaml
```

Обязательно изменить:
```yaml
database:
  host: localhost
  port: 5432          # brew postgres — порт 5432 (НЕ 5433 как в Docker)
  name: polymarket_sports
  user: postgres
  password: ""        # пустой пароль — brew postgres по умолчанию без пароля

alerts:
  telegram_bot_token: "ТОТ_ЖЕ_ЧТО_НА_MACBOOK"
  telegram_chat_id: "ТОТ_ЖЕ_ЧТО_НА_MACBOOK"

trading:
  enabled: true
  dry_run: true        # сначала true — убедиться что всё работает
```

---

## Часть 4 — Инициализация базы данных

### Шаг 8. Накати схему

```bash
cd ~/polymarket-sports
source venv/bin/activate
python db/init_schema.py
```

Проверка таблиц:
```bash
psql polymarket_sports -U postgres -c "\dt"
```

Ожидаемые таблицы:
```
markets, price_snapshots, trades, open_positions, order_log,
cost_analysis, tanking_signals, prop_scan_log, data_gaps, spike_events
```

---

### Шаг 9. Перенос данных из старой БД (опционально)

Если нужна история цен и маркетов:

**На MacBook** (Docker, порт 5433, БД `polymarket_sports_sports`, user `postgres`):
```bash
docker exec polymarket_sports-timescale pg_dump \
  -U postgres -d polymarket_sports_sports \
  --table=markets \
  --table=price_snapshots \
  --table=cost_analysis \
  --table=tanking_signals \
  --table=pitcher_signals \
  --table=prop_scan_log \
  -Fc > ~/polymarket_sports_backup.dump

# Скопировать на Mac Mini:
scp ~/polymarket_sports_backup.dump ВАШ_USER@IP_MAC_MINI:~/
```

**На Mac Mini** (нативный PostgreSQL, порт 5432, БД `polymarket_sports`, user `postgres`):
```bash
pg_restore -h localhost -p 5432 -U postgres -d polymarket_sports \
  --no-owner --no-privileges \
  ~/polymarket_sports_backup.dump
```

> Если ошибки при restore — добавь флаг `--exit-on-error` чтобы увидеть причину,
> или убери его чтобы пропускать конфликты.

---

## Часть 5 — Запуск и проверка

### Шаг 10. Проверь подключение к БД и CLOB

```bash
cd ~/polymarket-sports
source venv/bin/activate

# Проверка БД
python -c "
import asyncio, yaml, asyncpg
async def t():
    cfg = yaml.safe_load(open('config/settings.yaml'))['database']
    conn = await asyncpg.connect(host=cfg['host'], port=cfg['port'],
        database=cfg['name'], user=cfg['user'], password=str(cfg['password']))
    print('DB OK, tables:', [r['tablename'] for r in await conn.fetch(\"SELECT tablename FROM pg_tables WHERE schemaname='public'\")])
    await conn.close()
asyncio.run(t())
"
```

---

### Шаг 11. Запусти все сервисы

```bash
cd ~/polymarket-sports
make start
```

Это запустит:
- **Trading bot** → логи в `logs/bot.log`
- **Streamlit dashboard** → `http://localhost:8501`
- **Watchdog** → авторестарт бота при падении

Проверка:
```bash
make status
```

---

### Шаг 12. Проверь Telegram

Напиши боту `/status` — должен ответить с балансом и 0 открытых позиций.

---

### Шаг 13. Включи live-режим

После 1-2 часов наблюдения в dry_run:

```bash
# Редактируй settings.yaml:
nano ~/polymarket-sports/config/settings.yaml
# Изменить: dry_run: false

make stop && make start
```

---

## Часть 6 — Автозапуск при перезагрузке

### Шаг 14. LaunchAgent (авторестарт при reboot)

```bash
# Замени USERNAME на твоё реальное имя пользователя
USERNAME=$(whoami)

cat > ~/Library/LaunchAgents/com.postgres.trading.plist << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.postgres.trading</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/Users/$USERNAME/polymarket-sports/scripts/start_bot.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <false/>
  <key>WorkingDirectory</key>
  <string>/Users/$USERNAME/polymarket-sports</string>
  <key>StandardOutPath</key>
  <string>/Users/$USERNAME/polymarket-sports/logs/launchd.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/$USERNAME/polymarket-sports/logs/launchd.log</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.postgres.trading.plist
```

Проверка:
```bash
launchctl list | grep postgres
```

---

## Доступ с MacBook

### В одной WiFi сети (проще всего)
```bash
# Узнать IP Mac Mini:
# На Mac Mini → System Settings → Wi-Fi → Details → IP Address
open http://IP_MAC_MINI:8501
```

### SSH туннель (если разные сети)
```bash
ssh -L 8501:localhost:8501 USERNAME@IP_MAC_MINI
# Затем: http://localhost:8501
```

### Добавить в ~/.ssh/config на MacBook (удобнее)
```
Host macmini
  HostName IP_MAC_MINI
  User ВАШ_USER
  LocalForward 8501 localhost:8501
```
Тогда просто: `ssh macmini`

---

## Команды управления

| Команда | Что делает |
|---|---|
| `make start` | Запустить бот + dashboard + watchdog |
| `make stop` | Остановить всё |
| `make status` | Показать PID и последние логи |
| `make logs` | Стриминг всех логов |
| `make trading` | Запустить только бот (foreground) |
| `make dashboard` | Запустить только dashboard |

---

## Типичные проблемы

### `psql: command not found`
```bash
export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
```

### `timescaledb-tune not found`
```bash
brew link timescaledb
# или
/opt/homebrew/bin/timescaledb-tune --quiet --yes
```

### `connection refused` к PostgreSQL
```bash
brew services list | grep postgresql
brew services restart postgresql@16
```

### Бот стартует и сразу падает
```bash
tail -50 logs/bot.log
# Чаще всего: неверный пароль БД, нет .env файла, нет POLYGON_PRIVATE_KEY
```

### Dashboard не открывается по IP
```bash
# Проверить что Streamlit слушает 0.0.0.0, не только localhost
# В scripts/start_bot.sh должна быть строка:
# --server.address 0.0.0.0
```

Если нет — добавь `--server.address 0.0.0.0` в `scripts/start_bot.sh`:
```bash
nohup "$PYTHON" -m streamlit run "$REPO/dashboard/main_dashboard.py" \
  --server.port 8501 \
  --server.headless true \
  --server.address 0.0.0.0 \   # ← добавить эту строку
  >> "$LOG_DIR/dashboard.log" 2>&1 &
```
