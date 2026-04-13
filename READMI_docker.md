# Deploy `luxalgo_bot.py` в Docker на VPS (без перезагрузки сервера)

Ни одна команда из этого гайда **не требует reboot** VPS.

Текущая ситуация по вашим логам:
- Docker установлен и работает.
- Порт `80` свободен.
- Порт `5001` свободен.
- Это отлично для запуска webhook-бота.

---

## 0) Что получится в конце

После выполнения:
- бот будет работать в контейнере Python `3.12`;
- webhook будет доступен на VPS:
  - `http://5.187.5.121/tradingview_webhook`
- в TradingView нужно будет заменить URL с `87.251.86.53` на IP VPS.

---

## 1) Подключиться к серверу и перейти в папку проекта

```bash
ssh root@5.187.5.121
cd ~/luxalgo_bot
pwd
```

Ожидаемый результат:
- `pwd` вернет примерно `/root/luxalgo_bot`.

Если путь другой — это не ошибка, главное быть в нужной директории проекта.

---

## 2) Быстрая проверка Docker и свободных портов

```bash
docker --version
docker ps -a
ss -lntp | grep ':80'
ss -lntp | grep 5001
```

Ожидаемый результат:
- `docker --version` покажет версию (у вас уже `26.1.3`).
- `docker ps -a` покажет список контейнеров.
- Команды `ss ... | grep` могут вернуть **пусто**.  
  Это нормально: значит на этих портах никто не слушает, и мы можем их использовать.

---

## 3) Создать файл с кодом бота на сервере

Если файл еще не загружен на VPS, создайте его через heredoc:

```bash
cat > luxalgo_bot.py << 'PYEOF'
# ВСТАВЬТЕ СЮДА ПОЛНЫЙ КОД ИЗ ЛОКАЛЬНОГО luxalgo_bot.py
PYEOF
```

Проверка:

```bash
ls -l luxalgo_bot.py
```

Ожидаемый результат:
- появится строка с файлом и его размером (`> 0` байт).

---

## 4) Создать `.env` с токеном

```bash
cat > .env << 'EOF'
LUXALGO_TG_BOT_TOKEN='PASTE_YOUR_TELEGRAM_BOT_TOKEN'
EOF
```

Проверка:

```bash
sed -n '1,2p' .env
```

Ожидаемый результат:
- строка `LUXALGO_TG_BOT_TOKEN='...'`.

Важно:
- токен должен быть валидный;
- кавычки допустимы.

---

## 5) Создать `requirements.txt` (фиксируем зависимости)

```bash
cat > requirements.txt << 'EOF'
pyTelegramBotAPI==4.18.0
Flask==3.0.3
requests==2.32.3
python-dotenv==1.0.1
EOF
```

Пояснение:
- это набор для Python `3.12`;
- ваш код из `luxalgo_bot.py` использует именно эти библиотеки.

---

## 6) Создать `Dockerfile`

```bash
cat > Dockerfile << 'EOF'
FROM python:3.12-slim

WORKDIR /app

# Небольшие runtime-настройки Python
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY luxalgo_bot.py /app/luxalgo_bot.py

EXPOSE 5001

CMD ["python", "luxalgo_bot.py"]
EOF
```

Проверка:

```bash
sed -n '1,120p' Dockerfile
```

Ожидаемый результат:
- видно содержимое Dockerfile, `FROM python:3.12-slim`, `EXPOSE 5001`, `CMD ...`.

---

## 7) Собрать Docker image

```bash
docker build -t luxalgo-bot:latest .
```

Ожидаемый результат:
- много строк сборки;
- в конце что-то вроде `Successfully tagged luxalgo-bot:latest`.

Если ошибка:
- чаще всего синтаксис в Dockerfile или нет файла `luxalgo_bot.py`.

---

## 8) Запустить контейнер с пробросом `80 -> 5001`

Сначала удалим старый контейнер (если уже был):

```bash
docker rm -f luxalgo-bot 2>/dev/null || true
```

Ожидаемый результат:
- либо имя/ID удаленного контейнера;
- либо пусто (это нормально, значит контейнера не было).

Теперь запуск:

```bash
docker run -d \
  --name luxalgo-bot \
  --restart unless-stopped \
  -p 80:5001 \
  --env-file .env \
  luxalgo-bot:latest
```

Ожидаемый результат:
- одна длинная строка с ID контейнера.

Пояснение:
- `-p 80:5001` значит: внешний `80` на VPS -> порт `5001` внутри контейнера (Flask).
- reboot не нужен.

---

## 9) Проверить, что контейнер действительно запущен

```bash
docker ps --filter "name=luxalgo-bot"
```

Ожидаемый результат:
- статус `Up ...`;
- в колонке `PORTS`: `0.0.0.0:80->5001/tcp`.

Логи:

```bash
docker logs --tail 100 luxalgo-bot
```

Ожидаемый результат:
- строки вида `Flask webhook запущен...` и `Бот запущен...`.

Если лог пустой:
- иногда сразу после старта это нормально;
- повторите через 3-5 секунд.

---

## 10) Локальный тест webhook на VPS

```bash
curl -X POST "http://127.0.0.1/tradingview_webhook" \
  -H "Content-Type: application/json" \
  -d '{"message":"🧪 BTC Buy test from VPS"}'
```

Ожидаемый результат:
- JSON-ответ, например `{"status":"accepted"}` или `{"status":"not_connected"}`.

Расшифровка:
- `accepted` — сигнал принят (отлично);
- `not_connected` — нужно в Telegram выполнить `/connect_topic` в нужном чате;
- `cooldown` — сработала защита интервала (нормально).

---

## 11) Проверка доступности снаружи (самое важное)

С вашей локальной машины (не с VPS):

Для Linux/macOS/Git Bash:

```bash
curl -X POST "http://5.187.5.121/tradingview_webhook" \
  -H "Content-Type: application/json" \
  -d '{"message":"🌍 external webhook test"}'
```

Для Windows PowerShell (обычно именно так):

```powershell
$body = @{ message = "external webhook test" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://5.187.5.121/tradingview_webhook" -Method Post -ContentType "application/json" -Body $body
```

Короткий вариант через `curl.exe` (именно `curl.exe`, не `curl`):

```powershell
curl.exe -X POST "http://5.187.5.121/tradingview_webhook" -H "Content-Type: application/json" -d "{\"message\":\"external webhook test\"}"
```

Ожидаемый результат:
- такой же JSON, как при локальном тесте.

Если таймаут:
- проверьте firewall у VPS/провайдера (в панели хостинга может быть отдельный ACL/security group).

---

## 12) Что поставить в TradingView

Сейчас у вас URL на ПК:
- `http://87.251.86.53/tradingview_webhook`

Нужно заменить на VPS:
- `http://5.187.5.121/tradingview_webhook`

После замены отправьте тестовый alert из TradingView и проверьте:

```bash
docker logs --tail 200 luxalgo-bot
```

---

## 13) Управление контейнером (без reboot)

Остановить:

```bash
docker stop luxalgo-bot
```

Запустить снова:

```bash
docker start luxalgo-bot
```

Перезапустить контейнер:

```bash
docker restart luxalgo-bot
```

Смотреть логи в реальном времени:

```bash
docker logs -f luxalgo-bot
```

---

## 14) Обновить код бота: удалить старый `.py`, положить новый и пересобрать образ

Имя файла в командах ниже должно **совпадать** с тем, что в вашем `Dockerfile` в строках `COPY ...` и `CMD ...` (в этом гайде везде используется `luxalgo_bot.py`; если у вас `luxalgo_bot_test.py` — замените имя везде одинаково).

### 14.1 Зайти в каталог проекта на VPS

```bash
cd ~/luxalgo_bot
pwd
```

Ожидаемый результат: путь к папке проекта (например `/root/luxalgo_bot`).

### 14.2 (Опционально) Резервная копия старого файла

```bash
cp -a luxalgo_bot.py luxalgo_bot.py.bak.$(date +%Y%m%d_%H%M%S)
ls -l luxalgo_bot.py*
```

Ожидаемый результат: рядом с текущим файлом появится копия с датой в имени.

### 14.3 Удалить старый `.py`

```bash
rm -f luxalgo_bot.py
ls -l luxalgo_bot.py
```

Ожидаемый результат:
- команда `ls` выдаст `No such file or directory` или пусто — **файла больше нет**, это нормально.

### 14.4 Создать новый файл с обновлённым кодом

Через heredoc (удобно на сервере без `nano`):

```bash
cat > luxalgo_bot.py << 'PYEOF'
# ВСТАВЬТЕ СЮДА ПОЛНЫЙ НОВЫЙ КОД ИЗ ЛОКАЛЬНОГО ФАЙЛА
PYEOF
```

Проверка:

```bash
ls -l luxalgo_bot.py
head -n 5 luxalgo_bot.py
```

Ожидаемый результат:
- размер файла > 0;
- в `head` видны первые строки нового кода (не пустой файл).

Если код копируете с Windows, следите за **UTF-8** и окончаниями строк; при проблемах проще скопировать файл через `scp` с ПК на VPS в `~/luxalgo_bot/luxalgo_bot.py`, затем снова `ls`/`head`.

### 14.5 Остановить и удалить старый контейнер

```bash
docker stop luxalgo-bot 2>/dev/null || true
docker rm -f luxalgo-bot 2>/dev/null || true
docker ps -a --filter "name=luxalgo-bot"
```

Ожидаемый результат: контейнера `luxalgo-bot` в списке нет (или пустой вывод фильтра).

### 14.6 Пересобрать образ

Обычная пересборка:

```bash
docker build -t luxalgo-bot:latest .
```

Если нужно гарантированно подтянуть изменения в слое `COPY` (на всякий случай):

```bash
docker build --no-cache -t luxalgo-bot:latest .
```

Ожидаемый результат: в конце `Successfully tagged luxalgo-bot:latest`.

### 14.7 Запустить новый контейнер (те же порты и `.env`)

```bash
docker run -d \
  --name luxalgo-bot \
  --restart unless-stopped \
  -p 80:5001 \
  --env-file .env \
  luxalgo-bot:latest
```

Ожидаемый результат: печатается длинный ID контейнера.

### 14.8 Проверка

```bash
docker ps --filter "name=luxalgo-bot"
docker logs --tail 80 luxalgo-bot
```

Ожидаемый результат:
- `STATUS` — `Up ...`;
- в логах Flask и Telegram бота без ошибок.

### 14.9 После обновления кода в Telegram

Если менялась логика привязки чата или вы пересоздали контейнер с «чистой» памятью — снова выполните **`/connect_topic` в нужной теме** супергруппы.

---

**Кратко:** старый `luxalgo_bot.py` удаляем → новый создаём с тем же именем → `docker build` → `docker run`. Перезагрузка VPS не нужна.

---

## 15) Частые ошибки и быстрые решения

1. `Bind for 0.0.0.0:80 failed: port is already allocated`  
   Значит порт 80 уже занят.  
   Проверьте:
   ```bash
   ss -lntp | grep ':80'
   ```
   Решения:
   - освободить `80`, или
   - временно запустить `-p 8080:5001` и использовать `http://IP:8080/tradingview_webhook`.

2. `{"status":"not_connected"}`  
   В Telegram выполните `/connect_topic` в целевом чате/топике.

3. Контейнер сразу падает  
   Проверьте:
   ```bash
   docker logs --tail 200 luxalgo-bot
   ```
   Часто причина — неверный токен в `.env`.

---

## 16) Почему VPS не перезагрузится от этих действий

- `docker build/run/stop/restart` управляют только контейнерами;
- не вызываются команды `reboot`, `shutdown`, `systemctl reboot`;
- изменения применяются онлайн в текущей сессии.

