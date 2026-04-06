# 24.03.26-tg-bot-bingx

Телеграм-бот для отслеживания фандинга определённых монет с биржи BingX.

Отдельно: бот **LuxAlgo** (`luxalgo_bot_test.py`) — вебхук TradingView → Telegram.

## Запуск бота на Linux через `screen`

Чтобы процесс не останавливался после выхода из SSH:

1. Установка (если нет): `sudo apt update && sudo apt install -y screen`
2. Сессия и запуск:

```bash
cd /opt/luxalgo_bot   # или путь к проекту
screen -S luxalgo
source venv/bin/activate
python luxalgo_bot.py
```

3. **Отключиться**, не останавливая бота: **`Ctrl+A`**, затем **`D`** (detach). Сессию можно закрыть — бот продолжит работу.
4. **Вернуться** к логам в консоли: `screen -r luxalgo`
5. Список сессий: `screen -ls`
6. Если сессия «занята»: `screen -d -r luxalgo`
7. **Прокрутка** буфера: **`Ctrl+A`** → **`[`** (выход из режима прокрутки — **Esc**)
8. **Остановить** бота: подключиться (`screen -r luxalgo`), **`Ctrl+C`**, `exit`; либо снаружи: `screen -X -S luxalgo quit`

Кратко: `screen -S luxalgo` → venv → `python ...` → **`Ctrl+A` `D`**.
