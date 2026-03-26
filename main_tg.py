"""
BingX Funding Rate Telegram Bot
pip install python-telegram-bot
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

# ═══════════════════════════════════════
# Logging
# ═══════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("fundbot")

# ═══════════════════════════════════════
# Config
# ═══════════════════════════════════════
CONFIG_FILE = "fundbot_config.json"
DEFAULT_CONFIG = {
    "chat_id": None,
    "topic_id": None,
    "filter_long": None,
    "filter_short": None,
    "blocked": [],
}


def load_config() -> dict:
    try:
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    except (FileNotFoundError, json.JSONDecodeError):
        return DEFAULT_CONFIG.copy()


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


config = load_config()


# ═══════════════════════════════════════
# BingX API
# ═══════════════════════════════════════
def http_get_json(url: str, timeout: int = 10):
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        try:
            return json.loads(body)
        except Exception:
            raise RuntimeError(f"HTTP {e.code}: {body[:300]}")
    except URLError as e:
        raise RuntimeError(f"Network error: {e.reason}")


def fetch_all_funding() -> list[dict]:
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex"
    try:
        resp = http_get_json(url)
    except Exception as e:
        logger.error(f"Funding fetch failed: {e}")
        return []

    if not isinstance(resp, dict) or resp.get("code") != 0:
        logger.error(f"Unexpected API response: {resp}")
        return []

    data = resp.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


def normalize_symbol(raw: str) -> str:
    s = raw.upper().strip().replace("/", "").replace(" ", "")
    if s.endswith("-USDT"):
        return s
    s = s.replace("-", "")
    if s.endswith("USDT"):
        base = s[:-4]
    else:
        base = s
    return f"{base}-USDT"


def minutes_until_funding(next_funding_time) -> int | None:
    if not next_funding_time:
        return None
    try:
        next_ts = int(next_funding_time) / 1000
        now_ts = datetime.now(timezone.utc).timestamp()
        diff = next_ts - now_ts
        return max(0, int(diff // 60))
    except Exception:
        return None


def bingx_url(symbol: str) -> str:
    display = symbol.replace("-", "")
    return f"https://bingx.com/ru-ru/futures/forward/{display}/"


# ═══════════════════════════════════════
# Фильтрация и форматирование
# ═══════════════════════════════════════
def get_filtered_alerts(mode: str = "all") -> list[dict]:
    """
    mode: "all" | "long" | "short"
    1 HTTP-запрос → все тикеры → фильтр по порогам и блок-листу.
    """
    all_data = fetch_all_funding()
    blocked = set(config.get("blocked", []))
    fl = config.get("filter_long")
    fs = config.get("filter_short")

    alerts = []
    for item in all_data:
        sym = item.get("symbol", "")
        if sym in blocked:
            continue

        rate_raw = item.get("lastFundingRate")
        if rate_raw is None:
            continue

        rate_pct = float(rate_raw) * 100
        mins = minutes_until_funding(item.get("nextFundingTime"))

        show = False
        if mode in ("all", "long") and rate_pct >= 0 and fl is not None and rate_pct >= fl:
            show = True
        if mode in ("all", "short") and rate_pct < 0 and fs is not None and rate_pct <= fs:
            show = True

        if show:
            alerts.append({
                "symbol": sym,
                "rate_pct": rate_pct,
                "minutes": mins,
                "emoji": "🟢" if rate_pct >= 0 else "🔴",
            })

    alerts.sort(key=lambda x: x["rate_pct"])
    return alerts


def format_alerts(alerts: list[dict]) -> str:
    if not alerts:
        return "📊 <b>Funding Alerts</b>\n\nНет монет, подходящих под фильтр."

    lines = ["📊 <b>Funding Alerts</b>\n"]
    for a in alerts:
        sym = a["symbol"]
        display = sym.replace("-", "")
        url = bingx_url(sym)
        rate = a["rate_pct"]
        mins = a["minutes"]
        mins_str = f"{mins} min" if mins is not None else "N/A"

        lines.append(
            f'{a["emoji"]} <a href="{url}">{display}</a>'
            f" → {rate:+.4f}%  ( {mins_str} )"
        )

    return "\n".join(lines)


def split_message(text: str, max_len: int = 4000) -> list[str]:
    if len(text) <= max_len:
        return [text]

    lines = text.split("\n")
    chunks, current, length = [], [], 0

    for line in lines:
        if length + len(line) + 1 > max_len and current:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1

    if current:
        chunks.append("\n".join(current))
    return chunks


# ═══════════════════════════════════════
# Отправка алерта (по расписанию)
# ═══════════════════════════════════════
async def send_alert(context: ContextTypes.DEFAULT_TYPE):
    chat_id = config.get("chat_id")
    if not chat_id:
        logger.warning("chat_id not set — skipping alert")
        return

    if config.get("filter_long") is None and config.get("filter_short") is None:
        logger.info("No filters set — skipping")
        return

    alerts = await asyncio.to_thread(get_filtered_alerts)
    if not alerts:
        logger.info("No alerts match filters this hour")
        return

    msg = format_alerts(alerts)
    topic_id = config.get("topic_id")

    for chunk in split_message(msg):
        kwargs = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": ParseMode.HTML,
            "disable_web_page_preview": True,
        }
        if topic_id:
            kwargs["message_thread_id"] = topic_id

        try:
            await context.bot.send_message(**kwargs)
        except Exception as e:
            logger.error(f"Send failed: {e}")

    logger.info(f"Alert sent: {len(alerts)} tickers")


# ═══════════════════════════════════════
# Расписание: каждый час в XX:50
# ═══════════════════════════════════════
def schedule_alerts(app: Application):
    now = datetime.now(timezone.utc)
    target = now.replace(minute=50, second=0, microsecond=0)
    if now.minute >= 50:
        target += timedelta(hours=1)

    first_sec = (target - now).total_seconds()
    logger.info(
        f"Next alert in {first_sec:.0f}s → {target.strftime('%H:%M UTC')}"
    )

    app.job_queue.run_repeating(
        send_alert,
        interval=3600,
        first=first_sec,
        name="hourly_alert",
    )


# ═══════════════════════════════════════
# Команды бота
# ═══════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 <b>BingX Funding Rate Bot</b>\n\n"
        "Мониторит фандинг <b>всех</b> фьючерсных пар BingX\n"
        "и присылает алерты каждый час в XX:50.\n\n"
        "<b>Настройка:</b>\n\n"
        "/fundbot_setchat\n"
        "  ↳ вызвать в нужном топике\n\n"
        "/fundbot_filter_long 1%\n"
        "  ↳ алерты для фандинга ≥ 1%\n\n"
        "/fundbot_filter_short -1%\n"
        "  ↳ алерты для фандинга ≤ -1%\n\n"
        "/fundbot_block APTUSDT\n"
        "  ↳ скрыть тикер\n\n"
        "/fundbot_unblock APTUSDT\n"
        "  ↳ вернуть тикер\n\n"
        "/fundbot_blocklist — заблокированные\n"
        "/fundbot_status — текущие настройки\n"
        "/fundbot_test_long — тест long-фильтра прямо сейчас\n"
        "/fundbot_test_short — тест short-фильтра прямо сейчас"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_setchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config["chat_id"] = update.effective_chat.id
    config["topic_id"] = update.message.message_thread_id
    save_config(config)

    topic = f"\n💬 Topic ID: <code>{config['topic_id']}</code>" if config["topic_id"] else ""
    await update.message.reply_text(
        f"✅ Алерты будут приходить сюда\n"
        f"🆔 Chat ID: <code>{config['chat_id']}</code>{topic}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_filter_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        current = config.get("filter_long")
        status = f"≥ {current}%" if current is not None else "выкл"
        await update.message.reply_text(
            f"Текущий фильтр long: {status}\n\n"
            f"Установить: /fundbot_filter_long 1%\n"
            f"Отключить: /fundbot_filter_long off"
        )
        return

    arg = context.args[0].replace("%", "").replace(",", ".").strip()
    if arg.lower() == "off":
        config["filter_long"] = None
        save_config(config)
        await update.message.reply_text("✅ Фильтр long отключён")
        return

    try:
        val = float(arg)
        if val < 0:
            await update.message.reply_text("⚠️ Для long фильтр должен быть ≥ 0")
            return
        config["filter_long"] = val
        save_config(config)
        await update.message.reply_text(f"✅ Фильтр long: алерт при фандинге ≥ {val}%")
    except ValueError:
        await update.message.reply_text("⚠️ Неверный формат. Пример: /fundbot_filter_long 1%")


async def cmd_filter_short(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        current = config.get("filter_short")
        status = f"≤ {current}%" if current is not None else "выкл"
        await update.message.reply_text(
            f"Текущий фильтр short: {status}\n\n"
            f"Установить: /fundbot_filter_short -1%\n"
            f"Отключить: /fundbot_filter_short off"
        )
        return

    arg = context.args[0].replace("%", "").replace(",", ".").strip()
    if arg.lower() == "off":
        config["filter_short"] = None
        save_config(config)
        await update.message.reply_text("✅ Фильтр short отключён")
        return

    try:
        val = float(arg)
        if val > 0:
            val = -val
        config["filter_short"] = val
        save_config(config)
        await update.message.reply_text(f"✅ Фильтр short: алерт при фандинге ≤ {val}%")
    except ValueError:
        await update.message.reply_text(
            "⚠️ Неверный формат. Пример: /fundbot_filter_short -1%"
        )


async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /fundbot_block APTUSDT")
        return

    sym = normalize_symbol(context.args[0])
    blocked = config.get("blocked", [])
    if sym not in blocked:
        blocked.append(sym)
        config["blocked"] = blocked
        save_config(config)
    await update.message.reply_text(f"🚫 {sym} заблокирован")


async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /fundbot_unblock APTUSDT")
        return

    sym = normalize_symbol(context.args[0])
    blocked = config.get("blocked", [])
    if sym in blocked:
        blocked.remove(sym)
        config["blocked"] = blocked
        save_config(config)
        await update.message.reply_text(f"✅ {sym} разблокирован")
    else:
        await update.message.reply_text(f"ℹ️ {sym} не был в блок-листе")


async def cmd_blocklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    blocked = config.get("blocked", [])
    if not blocked:
        await update.message.reply_text("Блок-лист пуст.")
    else:
        text = "🚫 <b>Заблокированные:</b>\n" + ", ".join(sorted(blocked))
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fl = config.get("filter_long")
    fs = config.get("filter_short")
    blocked = config.get("blocked", [])
    chat_id = config.get("chat_id")
    topic_id = config.get("topic_id")

    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"📤 Chat: <code>{chat_id or 'не задан'}</code>\n"
        f"💬 Topic: <code>{topic_id or '—'}</code>\n\n"
        f"🟢 Long: {f'≥ {fl}%' if fl is not None else 'выкл'}\n"
        f"🔴 Short: {f'≤ {fs}%' if fs is not None else 'выкл'}\n\n"
        f"🚫 Заблокировано: {len(blocked)}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_test_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fl = config.get("filter_long")
    if fl is None:
        await update.message.reply_text(
            "⚠️ Фильтр long не задан.\n"
            "Установить: /fundbot_filter_long 0.5%"
        )
        return

    await update.message.reply_text(f"⏳ Загружаю данные с BingX... (long ≥ {fl}%)")
    alerts = await asyncio.to_thread(get_filtered_alerts, "long")
    msg = format_alerts(alerts)

    for chunk in split_message(msg):
        await update.message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def cmd_test_short(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fs = config.get("filter_short")
    if fs is None:
        await update.message.reply_text(
            "⚠️ Фильтр short не задан.\n"
            "Установить: /fundbot_filter_short -0.5%"
        )
        return

    await update.message.reply_text(f"⏳ Загружаю данные с BingX... (short ≤ {fs}%)")
    alerts = await asyncio.to_thread(get_filtered_alerts, "short")
    msg = format_alerts(alerts)

    for chunk in split_message(msg):
        await update.message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


# ═══════════════════════════════════════
# Точка входа
# ═══════════════════════════════════════
def main():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.lower().startswith("export "):
                        line = line[7:].strip()
                    if "=" not in line:
                        continue

                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    if (
                        len(value) >= 2
                        and value[0] in ("'", '"')
                        and value[-1] == value[0]
                    ):
                        value = value[1:-1].strip()

                    os.environ[key] = value
        except Exception as e:
            logger.warning(f"Failed to load {env_path}: {e}")

    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not TOKEN:
        print("━" * 45)
        print("  Задай TELEGRAM_BOT_TOKEN в файле .env:")
        print("  TELEGRAM_BOT_TOKEN='7123...:AAH...'")
        print("━" * 45)
        return

    app = Application.builder().token(TOKEN).build()

    handlers = {
        "start": cmd_start,
        "help": cmd_start,
        "fundbot_setchat": cmd_setchat,
        "fundbot_filter_long": cmd_filter_long,
        "fundbot_filter_short": cmd_filter_short,
        "fundbot_block": cmd_block,
        "fundbot_unblock": cmd_unblock,
        "fundbot_blocklist": cmd_blocklist,
        "fundbot_status": cmd_status,
        "fundbot_test_long": cmd_test_long,
        "fundbot_test_short": cmd_test_short,
    }
    for cmd, handler in handlers.items():
        app.add_handler(CommandHandler(cmd, handler))

    schedule_alerts(app)

    logger.info("🚀 Bot is running")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()