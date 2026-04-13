import os
import json
import threading
import time
import logging
import traceback
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from typing import Dict, List, Optional

from telegram import Update, ParseMode, Bot, Message
from telegram.ext import Updater, CommandHandler, CallbackContext

# ═══════════════════════════════════════
# Monkey-patch: forum topic support для PTB 13.6
# ═══════════════════════════════════════
_orig_msg_init = Message.__init__


def _patched_msg_init(self, *args, **kwargs):
    _thread_id = kwargs.pop("message_thread_id", None)
    _is_topic = kwargs.pop("is_topic_message", None)
    for _key in (
        "has_protected_content", "forum_topic_created",
        "forum_topic_closed", "forum_topic_reopened",
        "forum_topic_edited", "general_forum_topic_hidden",
        "general_forum_topic_unhidden", "write_access_allowed",
        "has_media_spoiler", "web_app_data", "users_shared",
        "chat_shared", "story", "giveaway", "giveaway_completed",
        "giveaway_created", "giveaway_winners",
        "external_reply", "quote", "link_preview_options",
        "reply_markup",  # иногда дублируется
    ):
        kwargs.pop(_key, None)
    _orig_msg_init(self, *args, **kwargs)
    self.message_thread_id = _thread_id
    self.is_topic_message = _is_topic


Message.__init__ = _patched_msg_init

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
    "alert_minute": 50,
    "chat_dev_id": None,
}


def load_config():
    # type: () -> Dict
    try:
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    except (FileNotFoundError, json.JSONDecodeError):
        return DEFAULT_CONFIG.copy()


def save_config(cfg):
    # type: (Dict) -> None
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


config = load_config()

# ═══════════════════════════════════════
# Глобальные переменные
# ═══════════════════════════════════════
BOT_TOKEN = None
bot_instance = None
scheduler_thread = None
stop_scheduler = False

# Переменная для предотвращения дублирования
last_alert_timestamp = 0
alert_lock = threading.Lock()

# ═══════════════════════════════════════
# Telegram API — прямой HTTP (topic support)
# ═══════════════════════════════════════

def send_tg_message(chat_id, text,
                    topic_id=None,
                    parse_mode=None,
                    disable_web_page_preview=False):
    """
    Отправка через HTTP POST — единственный способ передать
    message_thread_id на PTB < 13.13.
    """
    url = "https://api.telegram.org/bot{}/sendMessage".format(BOT_TOKEN)
    payload = {"chat_id": chat_id, "text": text}

    if topic_id is not None:
        payload["message_thread_id"] = topic_id
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if disable_web_page_preview:
        payload["disable_web_page_preview"] = True

    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, headers={"Content-Type": "application/json"})

    try:
        with urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
    except HTTPError as e:
        raw = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError("Telegram HTTP {}: {}".format(e.code, raw[:300]))
    except URLError as e:
        raise RuntimeError("Telegram network error: {}".format(e.reason))

    if not result.get("ok"):
        raise RuntimeError(
            "Telegram API: {}".format(result.get("description", str(result)))
        )
    return result


# ═══════════════════════════════════════
# Dev-уведомления в ЛС
# ═══════════════════════════════════════
def notify_dev(bot, text):
    # type: (Bot, str) -> None
    dev_id = config.get("chat_dev_id")
    if not dev_id:
        return
    try:
        bot.send_message(
            chat_id=dev_id,
            text="🚨 <b>FundBot Error</b>\n\n{}".format(text),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("Failed to notify dev: %s", e)


# ═══════════════════════════════════════
# Глобальный error handler
# ═══════════════════════════════════════
def error_handler(update, context):
    # type: (object, CallbackContext) -> None
    err = context.error
    logger.error("Unhandled exception: %s", err, exc_info=err)
    try:
        tb = traceback.format_exception(type(err), err, err.__traceback__)
        short_tb = "".join(tb[-3:])[:800]
        notify_dev(context.bot, "<pre>{}</pre>".format(short_tb))
    except Exception:
        pass


# ═══════════════════════════════════════
# BingX API
# ═══════════════════════════════════════
def http_get_json(url, timeout=10):
    # type: (str, int) -> dict
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
            raise RuntimeError("HTTP {}: {}".format(e.code, body[:300]))
    except URLError as e:
        raise RuntimeError("Network error: {}".format(e.reason))


def fetch_all_funding():
    # type: () -> List[Dict]
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex"
    resp = http_get_json(url)

    if not isinstance(resp, dict) or resp.get("code") != 0:
        raise RuntimeError("Unexpected API response: {}".format(resp))

    data = resp.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


def normalize_symbol(raw):
    # type: (str) -> str
    s = raw.upper().strip().replace("/", "").replace(" ", "")
    if s.endswith("-USDT"):
        return s
    s = s.replace("-", "")
    if s.endswith("USDT"):
        base = s[:-4]
    else:
        base = s
    return "{}-USDT".format(base)


def minutes_until_funding(next_funding_time):
    # type: (object) -> Optional[int]
    if not next_funding_time:
        return None
    try:
        next_ts = int(next_funding_time) / 1000
        now_ts = datetime.now(timezone.utc).timestamp()
        diff = next_ts - now_ts
        return max(0, int(diff // 60))
    except Exception:
        return None


def bingx_url(symbol):
    # type: (str) -> str
    return "https://bingx.com/ru/perpetual/{}/".format(symbol)


# ═══════════════════════════════════════
# Фильтрация и форматирование
# ═══════════════════════════════════════
def get_filtered_alerts(mode="all"):
    # type: (str) -> List[Dict]
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


def format_alerts(alerts):
    # type: (List[Dict]) -> str
    if not alerts:
        return "📊 <b>Funding Alerts</b>\n\nНет монет, подходящих под фильтр."

    lines = ["📊 <b>Funding Alerts</b>\n"]
    for a in alerts:
        sym = a["symbol"]
        url = bingx_url(sym)
        rate = a["rate_pct"]
        mins = a["minutes"]
        mins_str = "{} min".format(mins) if mins is not None else "N/A"

        lines.append(
            '{e} <a href="{u}">{s}</a> → {r:+.4f}%  ( {m} )'.format(
                e=a["emoji"], u=url, s=sym, r=rate, m=mins_str
            )
        )

    return "\n".join(lines)


def split_message(text, max_len=4000):
    # type: (str, int) -> List[str]
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
# Планировщик (ИСПРАВЛЕННЫЙ)
# ═══════════════════════════════════════
def send_alert():
    global bot_instance, last_alert_timestamp
    
    if not bot_instance:
        return

    chat_id = config.get("chat_id")
    if not chat_id:
        logger.warning("chat_id not set — skipping alert")
        return

    if config.get("filter_long") is None and config.get("filter_short") is None:
        logger.info("No filters set — skipping")
        return

    # Получаем текущий час и минуту для создания уникального идентификатора
    now = datetime.now(timezone.utc)
    current_hour_minute = now.hour * 100 + now.minute
    
    # Проверяем, не отправляли ли мы уже алерт для этой минуты этого часа
    with alert_lock:
        if last_alert_timestamp == current_hour_minute:
            logger.info("Alert already sent for %02d:%02d - skipping", now.hour, now.minute)
            return
        last_alert_timestamp = current_hour_minute

    try:
        alerts = get_filtered_alerts()
    except Exception as e:
        err_msg = "BingX API error:\n<pre>{}</pre>".format(e)
        logger.error("Funding fetch error: %s", e)
        notify_dev(bot_instance, err_msg)
        return

    if not alerts:
        logger.info("No alerts match filters this hour")
        return

    msg = format_alerts(alerts)
    topic_id = config.get("topic_id")

    for chunk in split_message(msg):
        try:
            send_tg_message(
                chat_id=chat_id,
                text=chunk,
                topic_id=topic_id,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            err_msg = "Send failed → <code>{}</code>:\n<pre>{}</pre>".format(
                chat_id, e
            )
            logger.error("Send failed: %s", e)
            notify_dev(bot_instance, err_msg)

    logger.info("Alert sent: %d tickers (topic=%s) at %02d:%02d", 
               len(alerts), topic_id, now.hour, now.minute)


def scheduler_loop():
    global stop_scheduler
    
    logger.info("Scheduler loop started")

    while not stop_scheduler:
        try:
            now = datetime.now(timezone.utc)
            target_minute = config.get("alert_minute", 50)
            
            current_minute = now.minute
            
            # Если мы в целевой минуте — отправляем
            if current_minute == target_minute:
                logger.info("Target minute reached: %02d:%02d", now.hour, now.minute)
                send_alert()
                # Спим 55 минут — гарантированно пропустим текущий час
                time.sleep(55 * 60)
            else:
                # Считаем минуты до цели
                if target_minute > current_minute:
                    minutes_to_wait = target_minute - current_minute
                else:
                    minutes_to_wait = 60 - current_minute + target_minute
                
                # Спим (minutes_to_wait - 1) минут, но минимум 10 сек, максимум 5 минут
                sleep_seconds = max(10, (minutes_to_wait - 1) * 60)
                sleep_seconds = min(sleep_seconds, 300)
                
                time.sleep(sleep_seconds)

        except Exception as e:
            logger.error("Scheduler error: %s", e)
            time.sleep(60)


def start_scheduler():
    global scheduler_thread, stop_scheduler, last_alert_timestamp
    stop_scheduler = False
    last_alert_timestamp = 0  # Сбрасываем при старте
    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_thread.start()

    minute = config.get("alert_minute", 50)
    logger.info("Scheduler started - alerts every hour at XX:%02d", minute)


def restart_scheduler():
    global stop_scheduler, last_alert_timestamp
    stop_scheduler = True
    last_alert_timestamp = 0  # Сбрасываем при рестарте
    if scheduler_thread:
        scheduler_thread.join(timeout=5)
    start_scheduler()


# ═══════════════════════════════════════
# Команды бота (без изменений)
# ═══════════════════════════════════════
def cmd_start(update, context):
    # type: (Update, CallbackContext) -> None
    m = config.get("alert_minute", 50)
    text = (
        "🤖 <b>BingX Funding Rate Bot</b>\n\n"
        "Мониторит фандинг <b>всех</b> фьючерсных пар BingX\n"
        "и присылает алерты каждый час в XX:{m:02d}.\n\n"
        "<b>Настройка:</b>\n\n"
        "/fundbot_setchat\n"
        "  ↳ вызвать <b>в нужном топике</b>\n\n"
        "/fundbot_settopic 12345\n"
        "  ↳ задать topic вручную (или <code>off</code>)\n\n"
        "/fundbot_filter_long 1%\n"
        "  ↳ алерты для фандинга ≥ 1%\n\n"
        "/fundbot_filter_short -1%\n"
        "  ↳ алерты для фандинга ≤ -1%\n\n"
        "/fundbot_block APTUSDT\n"
        "  ↳ скрыть тикер\n\n"
        "/fundbot_unblock APTUSDT\n"
        "  ↳ вернуть тикер\n\n"
        "/fundbot_set_minute 40\n"
        "  ↳ алерты в XX:40 (сейчас: XX:{m:02d})\n\n"
        "/fundbot_blocklist — заблокированные\n"
        "/fundbot_status — текущие настройки\n"
        "/fundbot_test — тест отправки в настроенный чат/топик\n"
        "/fundbot_test_long — тест long-фильтра\n"
        "/fundbot_test_short — тест short-фильтра\n"
        "/fundbot_test_dev — проверить уведомления деву"
    ).format(m=m)
    update.message.reply_text(text, parse_mode=ParseMode.HTML)


def cmd_setchat(update, context):
    # type: (Update, CallbackContext) -> None
    thread_id = getattr(update.message, "message_thread_id", None)

    config["chat_id"] = update.effective_chat.id
    config["topic_id"] = thread_id
    save_config(config)

    if thread_id is not None:
        text = (
            "✅ Алерты будут приходить в этот топик\n"
            "🆔 Chat ID: <code>{cid}</code>\n"
            "💬 Topic ID: <code>{tid}</code>"
        ).format(cid=config["chat_id"], tid=thread_id)
    else:
        text = (
            "✅ Алерты будут приходить сюда\n"
            "🆔 Chat ID: <code>{cid}</code>\n"
            "ℹ️ Топик не обнаружен (General / обычный чат)"
        ).format(cid=config["chat_id"])

    update.message.reply_text(text, parse_mode=ParseMode.HTML)


def cmd_settopic(update, context):
    # type: (Update, CallbackContext) -> None
    """Ручная установка topic_id."""
    if not context.args:
        current = config.get("topic_id")
        if current is not None:
            status = "<code>{}</code>".format(current)
        else:
            status = "не задан"
        update.message.reply_text(
            "💬 Текущий Topic ID: {s}\n\n"
            "Установить: /fundbot_settopic 12345\n"
            "Сбросить: /fundbot_settopic off".format(s=status),
            parse_mode=ParseMode.HTML,
        )
        return

    arg = context.args[0].strip()
    if arg.lower() == "off":
        config["topic_id"] = None
        save_config(config)
        update.message.reply_text("✅ Topic ID сброшен (General / без топика)")
        return

    try:
        val = int(arg)
        config["topic_id"] = val
        save_config(config)
        update.message.reply_text(
            "✅ Topic ID установлен: <code>{}</code>".format(val),
            parse_mode=ParseMode.HTML,
        )
    except ValueError:
        update.message.reply_text(
            "⚠️ Введите числовой ID топика или <code>off</code>",
            parse_mode=ParseMode.HTML,
        )


def cmd_filter_long(update, context):
    # type: (Update, CallbackContext) -> None
    if not context.args:
        current = config.get("filter_long")
        status = "≥ {}%".format(current) if current is not None else "выкл"
        update.message.reply_text(
            "Текущий фильтр long: {}\n\n"
            "Установить: /fundbot_filter_long 1%\n"
            "Отключить: /fundbot_filter_long off".format(status)
        )
        return

    arg = context.args[0].replace("%", "").replace(",", ".").strip()
    if arg.lower() == "off":
        config["filter_long"] = None
        save_config(config)
        update.message.reply_text("✅ Фильтр long отключён")
        return

    try:
        val = float(arg)
        if val < 0:
            update.message.reply_text("⚠️ Для long фильтр должен быть ≥ 0")
            return
        config["filter_long"] = val
        save_config(config)
        update.message.reply_text(
            "✅ Фильтр long: алерт при фандинге ≥ {}%".format(val)
        )
    except ValueError:
        update.message.reply_text(
            "⚠️ Неверный формат. Пример: /fundbot_filter_long 1%"
        )


def cmd_filter_short(update, context):
    # type: (Update, CallbackContext) -> None
    if not context.args:
        current = config.get("filter_short")
        status = "≤ {}%".format(current) if current is not None else "выкл"
        update.message.reply_text(
            "Текущий фильтр short: {}\n\n"
            "Установить: /fundbot_filter_short -1%\n"
            "Отключить: /fundbot_filter_short off".format(status)
        )
        return

    arg = context.args[0].replace("%", "").replace(",", ".").strip()
    if arg.lower() == "off":
        config["filter_short"] = None
        save_config(config)
        update.message.reply_text("✅ Фильтр short отключён")
        return

    try:
        val = float(arg)
        if val > 0:
            val = -val
        config["filter_short"] = val
        save_config(config)
        update.message.reply_text(
            "✅ Фильтр short: алерт при фандинге ≤ {}%".format(val)
        )
    except ValueError:
        update.message.reply_text(
            "⚠️ Неверный формат. Пример: /fundbot_filter_short -1%"
        )


def cmd_set_minute(update, context):
    # type: (Update, CallbackContext) -> None
    if not context.args:
        current = config.get("alert_minute", 50)
        update.message.reply_text(
            "⏰ Сейчас алерты в XX:{c:02d}\n\n"
            "Изменить: /fundbot_set_minute 40\n"
            "Сбросить: /fundbot_set_minute 50".format(c=current)
        )
        return

    try:
        val = int(context.args[0].strip())
    except ValueError:
        update.message.reply_text("⚠️ Введите целое число от 0 до 59.")
        return

    if not (0 <= val <= 59):
        update.message.reply_text("⚠️ Минута должна быть от 0 до 59.")
        return

    config["alert_minute"] = val
    save_config(config)
    restart_scheduler()

    update.message.reply_text(
        "✅ Алерты теперь каждый час в XX:{:02d}".format(val)
    )


def cmd_block(update, context):
    # type: (Update, CallbackContext) -> None
    if not context.args:
        update.message.reply_text("Использование: /fundbot_block APTUSDT")
        return

    sym = normalize_symbol(context.args[0])
    blocked = config.get("blocked", [])
    if sym not in blocked:
        blocked.append(sym)
        config["blocked"] = blocked
        save_config(config)
    update.message.reply_text("🚫 {} заблокирован".format(sym))


def cmd_unblock(update, context):
    # type: (Update, CallbackContext) -> None
    if not context.args:
        update.message.reply_text("Использование: /fundbot_unblock APTUSDT")
        return

    sym = normalize_symbol(context.args[0])
    blocked = config.get("blocked", [])
    if sym in blocked:
        blocked.remove(sym)
        config["blocked"] = blocked
        save_config(config)
        update.message.reply_text("✅ {} разблокирован".format(sym))
    else:
        update.message.reply_text("ℹ️ {} не был в блок-листе".format(sym))


def cmd_blocklist(update, context):
    # type: (Update, CallbackContext) -> None
    blocked = config.get("blocked", [])
    if not blocked:
        update.message.reply_text("Блок-лист пуст.")
    else:
        text = "🚫 <b>Заблокированные:</b>\n" + ", ".join(sorted(blocked))
        update.message.reply_text(text, parse_mode=ParseMode.HTML)


def cmd_status(update, context):
    # type: (Update, CallbackContext) -> None
    fl = config.get("filter_long")
    fs = config.get("filter_short")
    blocked = config.get("blocked", [])
    chat_id = config.get("chat_id")
    topic_id = config.get("topic_id")
    minute = config.get("alert_minute", 50)
    dev_id = config.get("chat_dev_id")

    text = (
        "⚙️ <b>Настройки</b>\n\n"
        "📤 Chat: <code>{cid}</code>\n"
        "💬 Topic: <code>{tid}</code>\n\n"
        "🟢 Long: {fl}\n"
        "🔴 Short: {fs}\n\n"
        "⏰ Алерты: каждый час в XX:{m:02d}\n"
        "🚫 Заблокировано: {bl}\n"
        "🛠 Dev: <code>{dev}</code>"
    ).format(
        cid=chat_id or "не задан",
        tid=topic_id if topic_id is not None else "— (General)",
        fl="≥ {}%".format(fl) if fl is not None else "выкл",
        fs="≤ {}%".format(fs) if fs is not None else "выкл",
        m=minute,
        bl=len(blocked),
        dev=dev_id or "выкл",
    )
    update.message.reply_text(text, parse_mode=ParseMode.HTML)


def cmd_test(update, context):
    # type: (Update, CallbackContext) -> None
    """Тестовая отправка в настроенный чат/топик через send_tg_message."""
    chat_id = config.get("chat_id")
    topic_id = config.get("topic_id")

    if not chat_id:
        update.message.reply_text(
            "⚠️ Сначала вызови /fundbot_setchat в нужном чате/топике"
        )
        return

    test_text = (
        "✅ <b>FundBot Test</b>\n\n"
        "Сообщение отправлено через прямой API.\n"
        "Chat ID: <code>{cid}</code>\n"
        "Topic ID: <code>{tid}</code>\n"
        "Время: {t}"
    ).format(
        cid=chat_id,
        tid=topic_id if topic_id is not None else "—",
        t=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    )

    try:
        send_tg_message(
            chat_id=chat_id,
            text=test_text,
            topic_id=topic_id,
            parse_mode="HTML",
        )
        update.message.reply_text(
            "✅ Отправлено в chat=<code>{cid}</code> topic=<code>{tid}</code>".format(
                cid=chat_id,
                tid=topic_id if topic_id is not None else "—",
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        update.message.reply_text(
            "❌ Ошибка: <code>{}</code>".format(e),
            parse_mode=ParseMode.HTML,
        )


def cmd_test_long(update, context):
    # type: (Update, CallbackContext) -> None
    fl = config.get("filter_long")
    if fl is None:
        update.message.reply_text(
            "⚠️ Фильтр long не задан.\n"
            "Установить: /fundbot_filter_long 0.5%"
        )
        return

    update.message.reply_text(
        "⏳ Загружаю данные с BingX... (long ≥ {}%)".format(fl)
    )
    try:
        alerts = get_filtered_alerts("long")
    except Exception as e:
        update.message.reply_text("❌ Ошибка: {}".format(e))
        notify_dev(context.bot, "test_long error:\n<pre>{}</pre>".format(e))
        return

    msg = format_alerts(alerts)
    for chunk in split_message(msg):
        update.message.reply_text(
            chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )


def cmd_test_short(update, context):
    # type: (Update, CallbackContext) -> None
    fs = config.get("filter_short")
    if fs is None:
        update.message.reply_text(
            "⚠️ Фильтр short не задан.\n"
            "Установить: /fundbot_filter_short -0.5%"
        )
        return

    update.message.reply_text(
        "⏳ Загружаю данные с BingX... (short ≤ {}%)".format(fs)
    )
    try:
        alerts = get_filtered_alerts("short")
    except Exception as e:
        update.message.reply_text("❌ Ошибка: {}".format(e))
        notify_dev(context.bot, "test_short error:\n<pre>{}</pre>".format(e))
        return

    msg = format_alerts(alerts)
    for chunk in split_message(msg):
        update.message.reply_text(
            chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )


def cmd_test_dev(update, context):
    # type: (Update, CallbackContext) -> None
    dev_id = config.get("chat_dev_id")
    if not dev_id:
        update.message.reply_text(
            "⚠️ <code>chat_dev_id</code> не задан в конфиге.\n\n"
            "Добавь в <code>fundbot_config.json</code>:\n"
            '<code>"chat_dev_id": 123456789</code>\n\n'
            "Свой ID узнаешь через @userinfobot",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        context.bot.send_message(
            chat_id=dev_id,
            text=(
                "✅ <b>FundBot Test</b>\n\n"
                "Уведомления работают!\n"
                "Время: {}"
            ).format(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")),
            parse_mode=ParseMode.HTML,
        )
        update.message.reply_text(
            "✅ Тест отправлен → <code>{}</code>".format(dev_id),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        update.message.reply_text(
            "❌ Не удалось отправить: <code>{}</code>\n\n"
            "Убедись что ты написал боту /start в ЛС".format(e),
            parse_mode=ParseMode.HTML,
        )


# ═══════════════════════════════════════
# Точка входа
# ═══════════════════════════════════════
def main():
    global bot_instance, BOT_TOKEN

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
            logger.warning("Failed to load %s: %s", env_path, e)

    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not TOKEN:
        print("━" * 45)
        print("  Задай TELEGRAM_BOT_TOKEN в файле .env:")
        print("  TELEGRAM_BOT_TOKEN='7123...:AAH...'")
        print("━" * 45)
        return

    BOT_TOKEN = TOKEN

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher
    bot_instance = updater.bot

    dp.add_error_handler(error_handler)

    handlers = {
        "start": cmd_start,
        "help": cmd_start,
        "fundbot_setchat": cmd_setchat,
        "fundbot_settopic": cmd_settopic,
        "fundbot_filter_long": cmd_filter_long,
        "fundbot_filter_short": cmd_filter_short,
        "fundbot_set_minute": cmd_set_minute,
        "fundbot_block": cmd_block,
        "fundbot_unblock": cmd_unblock,
        "fundbot_blocklist": cmd_blocklist,
        "fundbot_status": cmd_status,
        "fundbot_test": cmd_test,
        "fundbot_test_long": cmd_test_long,
        "fundbot_test_short": cmd_test_short,
        "fundbot_test_dev": cmd_test_dev,
    }
    for cmd, handler in handlers.items():
        dp.add_handler(CommandHandler(cmd, handler))

    start_scheduler()

    logger.info("🚀 Bot is running  (PTB %s)", __import__("telegram").__version__)
    updater.start_polling(drop_pending_updates=True)
    updater.idle()


if __name__ == "__main__":
    main()