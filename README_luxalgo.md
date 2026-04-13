# LuxAlgo Bot Deploy (Python 3.6.8, без nano/screen)

## 1) Подготовка директории

```bash
mkdir -p ~/luxalgo_bot
cd ~/luxalgo_bot
python -m venv venv
source venv/bin/activate
python --version
```

Должно быть `Python 3.6.8`.

## 2) Установка библиотек (совместимые версии для 3.6.8)

```bash
pip install --upgrade pip==21.3.1
pip install \
  pyTelegramBotAPI==4.4.0 \
  Flask==2.0.3 \
  Werkzeug==2.0.3 \
  requests==2.27.1 \
  urllib3==1.26.18 \
  charset-normalizer==2.0.12 \
  certifi==2023.7.22 \
  idna==3.10
```

## 3) Создание `.env` (с токеном)

Команда:

```bash
cat > .env << 'EOF'
LUXALGO_TG_BOT_TOKEN='PASTE_YOUR_BOT_TOKEN_HERE'
EOF
```

Проверить:

```bash
python - << 'PYEOF'
import os
env = open('.env','r',encoding='utf-8').read().strip()
print(env)
PYEOF
```

## 4) Создание файла бота без nano

Скопируй содержимое локального `luxalgo_bot_prod.py` на сервер:

```bash
cat > luxalgo_bot_prod.py << 'PYEOF'
# ВСТАВЬ СЮДА ПОЛНЫЙ КОД ИЗ luxalgo_bot_prod.py
PYEOF
```

## 5) Запуск в фоне (native)

```bash
cd ~/luxalgo_bot
source venv/bin/activate
nohup python luxalgo_bot_prod.py > luxalgo_bot.log 2>&1 &
echo $! > luxalgo_bot.pid
```

Проверка:

```bash
cat luxalgo_bot.pid
ps -fp "$(cat luxalgo_bot.pid)"
```

Логи:

```bash
tail -n 100 luxalgo_bot.log
```

## 6) Остановка / перезапуск

Остановка:

```bash
kill "$(cat luxalgo_bot.pid)"
rm -f luxalgo_bot.pid
```

Перезапуск:

```bash
cd ~/luxalgo_bot
source venv/bin/activate
kill "$(cat luxalgo_bot.pid)" 2>/dev/null || true
nohup python luxalgo_bot_prod.py > luxalgo_bot.log 2>&1 &
echo $! > luxalgo_bot.pid
```

## 7) Первичная настройка в Telegram

1. Напиши боту `/start`
2. В нужном чате/топике отправь `/connect_topic`
3. (Опционально) выставь интервал: `/set_notif_time 30`
4. Проверь: `/settings`

## 8) Endpoint для TradingView

Webhook URL:

```text
http://<SERVER_IP>:5001/tradingview_webhook
```

Тест curl:

```bash
curl -X POST "http://127.0.0.1:5001/tradingview_webhook" \
  -H "Content-Type: application/json" \
  -d '{"message":"🟢 BTC Buy\nLuxAlgo test alert"}'
```

## 9) Нужен 80 порт для TradingView (без ребута и без установки пакетов)

Если TradingView должен бить именно в `http://<SERVER_IP>/...` на `80` порту:

### 9.1 Проверка, кто слушает 80 порт

Используй те команды, которые есть в системе (любая из них):

```bash
ss -lntp | grep ':80'
netstat -lntp | grep ':80'
lsof -iTCP:80 -sTCP:LISTEN -P -n
```

Если ничего не найдено — порт 80 сейчас не занят.

### 9.2 Проверка доступности с самой VPS

```bash
curl -I http://127.0.0.1
curl -I http://<SERVER_IP>
curl -I http://5.187.5.121
```

Если ответов нет/таймаут — смотри firewall ниже.

### 9.2.1 С чего начать: 9.3 или 9.4?

Это **разные задачи**:

- **9.4** — на VPS **никто не слушает 80**, а бот на **5001**. Редирект `80 → 5001` заставляет запросы на `:80` попадать в Flask. Это нужно, если хочешь URL без `:5001`.
- **9.3** — **файрвол** разрешает **входящий** трафик на порт **80** (и иногда его нужно дублировать правилом у хостера в панели).

**Практический порядок:**

1. Убедиться, что бот слушает нужный порт: `ss -lntp | grep 5001` (или тот порт, что задан в `FLASK_PORT` в `luxalgo_bot_prod.py`).
2. Выполнить **9.4** (редирект на этот же порт).
3. Проверить с VPS: `curl -I http://127.0.0.1` и POST на `/tradingview_webhook`.
4. Если с **интернета** не доходит, а локально ок — тогда **9.3** и проверка firewall у провайдера/панели.

`Connection refused` на `:80` до редиректа — нормально: **слушателя на 80 не было**. После **9.4** локальный `curl` на `http://127.0.0.1` должен перестать отдавать refused (если на целевом порту реально крутится бот).

**Если порт 5001 уже занят** другим процессом: не ставь второй сервис на тот же порт. Либо останови чужой процесс, либо смени в коде `FLASK_PORT` (например на `5002`) и в **9.4** везде подставь **тот же** порт в `--to-ports`.

### 9.3 Разрешить 80 в firewall (варианты)

Выбирай только тот инструмент, который уже установлен.

#### Вариант A: firewalld (CentOS/RHEL)

```bash
firewall-cmd --state
firewall-cmd --permanent --add-service=http
firewall-cmd --reload
firewall-cmd --list-services
```

Без перезагрузки сервера.

#### Вариант B: UFW (Ubuntu)

```bash
ufw status
ufw allow 80/tcp
ufw status
```

Без перезагрузки сервера.

#### Вариант C: iptables напрямую

```bash
iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport 80 -j ACCEPT
iptables -S | grep -- '--dport 80'
```

Это применится сразу, но после reboot правило может пропасть (временное).

### 9.4 Проброс 80 -> 5001 (если бот слушает 5001)

`luxalgo_bot_prod.py` слушает `5001`. Чтобы снаружи был `80`, можно сделать NAT-редирект:

```bash
iptables -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-ports 5001 2>/dev/null || \
iptables -t nat -I PREROUTING -p tcp --dport 80 -j REDIRECT --to-ports 5001

iptables -t nat -C OUTPUT -p tcp -d 127.0.0.1 --dport 80 -j REDIRECT --to-ports 5001 2>/dev/null || \
iptables -t nat -I OUTPUT -p tcp -d 127.0.0.1 --dport 80 -j REDIRECT --to-ports 5001

iptables -t nat -S | grep 5001
```

После этого webhook можно ставить так:

```text
http://<SERVER_IP>/tradingview_webhook
```

### 9.5 Если на 80 уже занят другим сервисом

- Если это нужный сервис (например, nginx/apache) — не останавливай.
- Тогда делай прокси/роут на `5001` в существующем веб-сервере (если он уже есть).
- Либо используй прямой URL `http://<SERVER_IP>:5001/tradingview_webhook` (если TradingView это принимает в твоём случае).

### 9.6 Проверка снаружи после настройки

Из внешней машины:

```bash
curl -X POST "http://<SERVER_IP>/tradingview_webhook" \
  -H "Content-Type: application/json" \
  -d '{"message":"🧪 TV external test"}'
```

И смотри лог на VPS:

```bash
tail -n 100 ~/luxalgo_bot/luxalgo_bot.log
```

## 10) Что создаст бот в папке

- `luxalgo_bot_prod.py` — основной скрипт
- `.env` — токен
- `luxalgo_config.json` — chat/topic/cooldown/last_alert_time
- `luxalgo_bot.log` — логи
- `luxalgo_bot.pid` — PID процесса

