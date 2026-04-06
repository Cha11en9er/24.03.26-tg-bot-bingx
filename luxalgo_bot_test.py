import json
import re
import telebot
import threading
import time
from html import escape
from flask import Flask, request, jsonify
import requests
import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ========================= НАСТРОЙКИ =========================
_raw_token = os.getenv("LUXALGO_TG_BOT_TOKEN")
BOT_TOKEN = (_raw_token or "").strip().strip('"').strip("'")
FLASK_PORT = 5001

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise SystemExit(
        "Не задан токен бота. Варианты:\n"
        "  1) pip install python-dotenv — тогда .env с LUXALGO_TG_BOT_TOKEN=... подхватится автоматически;\n"
        "  2) либо в PowerShell: $env:LUXALGO_TG_BOT_TOKEN='123456:ABC...'; python luxalgo_bot_test.py"
    )

# Глобальное состояние
CHAT_ID = None
TOPIC_ID = None
COOLDOWN_SECONDS = 30 * 60          # по умолчанию 30 минут (можно менять командой)
LAST_ALERT_TIME = 0
_webhook_lock = threading.Lock()

app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN)

# BingX перп BTC-USDT, локаль ru (как в ТЗ)
BINGX_BTC_PERP_URL = "https://bingx.com/ru/perpetual/BTC-USDT/"


# ======================= ПОЛУЧЕНИЕ ЦЕНЫ BTC (OKX, как BTCUSDT.P на графике) =======================
def get_btc_data():
    """
    Последняя цена (float), строка цены, ~24h от open24h по перпетуалу BTC-USDT-SWAP (OKX).
    При ошибке: (None, "N/A", "N/A").
    """
    try:
        url = "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP"
        r = requests.get(url, timeout=5)
        j = r.json()
        if j.get("code") != "0" or not j.get("data"):
            raise ValueError(j.get("msg") or "okx empty data")
        d = j["data"][0]
        last = float(d["last"])
        open24h = float(d.get("open24h") or 0)
        if open24h:
            change_pct = (last - open24h) / open24h * 100.0
        else:
            change_pct = 0.0
        price_str = f"{last:,.2f}"
        change_str = f"{'+ ' if change_pct > 0 else ''}{change_pct:.2f}"
        return last, price_str, change_str
    except Exception as e:
        print("❌ Ошибка получения цены BTC (OKX):", e)
        return None, "N/A", "N/A"


def _is_long_signal(alert_text: str) -> bool:
    """Long: Buy; Short: Sell. По тексту алерта LuxAlgo."""
    s = alert_text.lower()
    has_buy = bool(re.search(r"\bbuy\b", s))
    has_sell = bool(re.search(r"\bsell\b", s))
    if has_sell and not has_buy:
        return False
    if has_buy and not has_sell:
        return True
    if has_sell:
        return False
    return True


def _fmt_usd(n: float) -> str:
    return f"{n:,.2f}"


def _sl_tp_block(entry: float, is_long: bool) -> str:
    """
    Long: SL −5%; TP +2.5%, +5%, +10%.
    Short: SL +5%; TP −2.5%, −5%, −10%.
    """
    if is_long:
        sl = entry * 0.95
        tp1 = entry * 1.025
        tp2 = entry * 1.05
        tp3 = entry * 1.10
    else:
        sl = entry * 1.05
        tp1 = entry * 0.975
        tp2 = entry * 0.95
        tp3 = entry * 0.90
    return (
        f"SL: {_fmt_usd(sl)} $\n"
        f"TP1: {_fmt_usd(tp1)}$\n"
        f"TP2: {_fmt_usd(tp2)}$\n"
        f"TP3: {_fmt_usd(tp3)}$"
    )


def _inject_bingx_btc_link(alert_text: str) -> str:
    """После эмодзи: BTC Buy/Sell → ссылка BTC-USDT.P (BingX ru), остальное экранируем."""
    parts = alert_text.strip().split("\n", 1)
    first = parts[0]
    rest = parts[1] if len(parts) > 1 else None

    safe_first = escape(first)
    url = BINGX_BTC_PERP_URL
    linked, n = re.subn(
        r"\bBTC\s+(Buy|Sell)",
        rf'<a href="{url}">BTC-USDT.P</a> \1',
        safe_first,
        count=1,
        flags=re.IGNORECASE,
    )
    if n == 0:
        linked = safe_first
    if rest is None:
        return linked
    return linked + "\n" + escape(rest)


def build_settings_text() -> str:
    """Текущие настройки для /settings и /status."""
    cooldown_min = max(1, COOLDOWN_SECONDS // 60)
    if COOLDOWN_SECONDS % 60:
        cooldown_human = f"{COOLDOWN_SECONDS // 60} мин {COOLDOWN_SECONDS % 60} с"
    else:
        cooldown_human = f"{cooldown_min} мин"

    if CHAT_ID is None:
        chat_block = "Чат: <i>не привязан</i> — отправьте /connect_topic в нужном чате или топике."
        topic_block = "Топик: —"
    else:
        chat_block = f"Chat ID: <code>{CHAT_ID}</code>"
        if TOPIC_ID is not None:
            topic_block = f"Topic ID: <code>{TOPIC_ID}</code>"
        else:
            topic_block = "Топик: <i>нет</i> (сообщения в основной чат)"

    now = time.time()
    if LAST_ALERT_TIME > 0 and (now - LAST_ALERT_TIME) < COOLDOWN_SECONDS:
        left = int(COOLDOWN_SECONDS - (now - LAST_ALERT_TIME))
        cool_line = f"Заморозка: <b>{cooldown_human}</b> между сигналами.\nСледующий сигнал через: <b>~{left // 60} мин {left % 60} с</b>."
    else:
        cool_line = f"Заморозка: <b>{cooldown_human}</b> между сигналами (лимит не активен)."

    return (
        "⚙️ <b>Текущие настройки</b>\n\n"
        f"{chat_block}\n"
        f"{topic_block}\n\n"
        f"{cool_line}"
    )


# ======================= КОМАНДЫ БОТА =======================
@bot.message_handler(commands=["help"])
def handle_help(message):
    text = (
        "📖 <b>Команды</b>\n\n"
        "/start — подсказка по командам и привязке.\n"
        "/connect_topic — привязать этот чат и топик (если команда из темы) для сигналов.\n"
        "/settings — чат, топик, заморозка между сигналами.\n"
        "/set_notif_time &lt;минуты&gt; — минимальный интервал между сигналами (для всех алёртов).\n"
        "/help — это сообщение."
    )
    bot.reply_to(message, text, parse_mode="HTML")


@bot.message_handler(commands=["settings", "status"])
def handle_settings(message):
    bot.reply_to(message, build_settings_text(), parse_mode="HTML")


@bot.message_handler(commands=["start"])
def handle_start(message):
    bot.reply_to(
        message,
        "Команды: /help\nПривязка чата для сигналов: /connect_topic",
        parse_mode="HTML",
    )


@bot.message_handler(commands=["connect_topic", "connect"])
def handle_connect_topic(message):
    global CHAT_ID, TOPIC_ID
    CHAT_ID = message.chat.id
    TOPIC_ID = message.message_thread_id

    reply = (
        "✅ <b>Привязка сохранена.</b>\n"
        f"Chat ID: <code>{CHAT_ID}</code>\n"
        f"Topic ID: <code>{TOPIC_ID if TOPIC_ID is not None else 'нет (основной чат)'}</code>\n\n"
        "Сигналы с вебхука будут приходить сюда."
    )
    bot.reply_to(message, reply, parse_mode="HTML")

@bot.message_handler(commands=['set_notif_time'])
def handle_set_time(message):
    global COOLDOWN_SECONDS
    try:
        minutes = int(message.text.split()[1])
        if minutes < 1:
            raise ValueError
        COOLDOWN_SECONDS = minutes * 60
        bot.reply_to(
            message,
            f"✅ Задержка установлена: <b>{minutes}</b> минут между сигналами.",
            parse_mode="HTML",
        )
    except:
        bot.reply_to(message, "❌ Использование: /set_notif_time 30\n(число минут)")


def parse_tradingview_body() -> str:
    """
    TradingView шлёт тело по-разному: JSON, plain text. get_json(force=True) на «не-JSON» даёт 400.
    """
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        t = data.get("message") or data.get("text")
        if t is not None:
            return str(t)
    if isinstance(data, str) and data.strip():
        return data.strip()

    raw = (request.get_data(as_text=True) or "").strip()
    if not raw:
        return "No message"
    if raw.startswith("{") or raw.startswith("["):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                t = obj.get("message") or obj.get("text")
                if t is not None:
                    return str(t)
        except json.JSONDecodeError:
            pass
    return raw


def _deliver_alert_to_telegram(alert_text: str, chat_id: int, topic_id: int | None) -> None:
    """OKX + Telegram вне HTTP-запроса — иначе TradingView успевает оборвать соединение по таймауту."""
    try:
        entry_f, price_str, change_24h = get_btc_data()
        is_long = _is_long_signal(alert_text)
        header = _inject_bingx_btc_link(alert_text.strip())

        if entry_f is None:
            entry_line = "Entry: ±N/A USDT"
            sl_tp = "<i>SL/TP недоступны — нет цены входа.</i>"
        else:
            entry_line = f"Entry: ±{price_str} USDT"
            sl_tp = _sl_tp_block(entry_f, is_long)

        full_text = (
            f"{header}\n\n"
            f"{entry_line}\n"
            f"24h ~ <b>{change_24h}%</b> (от open24h)\n\n"
            f"{sl_tp}"
        )
        bot.send_message(
            chat_id=chat_id,
            text=full_text,
            message_thread_id=topic_id,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        print(f"✅ Сигнал отправлен в чат {chat_id}")
    except Exception as e:
        print("❌ Ошибка доставки в Telegram/OKX:", e)


# ======================= ВЕБХУК ОТ TRADINGVIEW =======================
@app.route('/tradingview_webhook', methods=['POST'])
def tradingview_webhook():
    global LAST_ALERT_TIME

    if CHAT_ID is None:
        print("⚠️ Чат не привязан. Отправьте /connect_topic в нужный чат или топик.")
        return jsonify({'status': 'not_connected'}), 200

    try:
        alert_text = parse_tradingview_body()
    except Exception as e:
        print("❌ Ошибка разбора тела вебхука:", e)
        return jsonify({'status': 'bad_body'}), 200

    current_time = time.time()
    with _webhook_lock:
        if current_time - LAST_ALERT_TIME < COOLDOWN_SECONDS:
            print(
                f"⏳ Cooldown активен ({int((COOLDOWN_SECONDS - (current_time - LAST_ALERT_TIME))/60)} мин). Сигнал пропущен."
            )
            return jsonify({'status': 'cooldown'}), 200
        LAST_ALERT_TIME = current_time

    threading.Thread(
        target=_deliver_alert_to_telegram,
        args=(alert_text, CHAT_ID, TOPIC_ID),
        daemon=True,
    ).start()
    # Ответ сразу: у TradingView короткий таймаут; иначе «timed out», хотя обработка потом всё же доигрывает.
    return jsonify({'status': 'accepted'}), 200

# ======================= ЗАПУСК =======================
def run_bot_polling():
    print("🤖 Бот запущен (polling)...")
    bot.infinity_polling()

if __name__ == '__main__':
    # Запускаем бота в отдельном потоке
    threading.Thread(target=run_bot_polling, daemon=True).start()
    
    # Запускаем Flask
    print(f"🌐 Flask webhook запущен на http://0.0.0.0:{FLASK_PORT}/tradingview_webhook")
    app.run(host='0.0.0.0', port=FLASK_PORT)