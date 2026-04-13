"""
BingX Funding Rate + Short Trap (Long Squeeze) Telegram Bot
Python 3.12+ + python-telegram-bot v22.x

Планировщик без JobQueue: asyncio в post_init (extras [job-queue] не нужен).

pip install -U "python-telegram-bot[http2]" httpx aiofiles python-dotenv
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Tuple

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("fundbot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("fundbot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logging.getLogger("telegram.ext").setLevel(logging.INFO)

CONFIG_FILE = "fundbot_config.json"

DEFAULT_CONFIG = {
    "chat_id": None,
    "topic_id": None,
    "filter_long": None,
    "filter_short": None,
    "blocked": [],
    "alert_minute": 50,
    "short_trap_enabled": True,
    "funding_threshold": -0.10,
    "delta_funding_drop": 0.04,
    "price_pump_min": 2.0,
    "price_pump_max": 5.0,
    "chat_dev_id": None,
}


def load_config() -> Dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception:
        return DEFAULT_CONFIG.copy()


def save_config(cfg: Dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


config = load_config()

BOT_TOKEN: Optional[str] = None
bot_app: Optional[Application] = None

_TRAP_HISTORY: Dict[str, Deque[Tuple[float, float, float]]] = {}
_HISTORY_MAXLEN = 14
_SHORT_TRAP_INTERVAL_SEC = 300
_HOURLY_POLL_SEC = 30
_last_hourly_fired_key: Optional[Tuple[int, int, int, int]] = None


async def http_get_json(url: str, params: Optional[Dict] = None, timeout: int = 12) -> Dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            url,
            params=params,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; BingXFundBot/2.0)",
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        return resp.json()


async def fetch_all_funding() -> List[Dict]:
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex"
    data = await http_get_json(url)
    if data.get("code") != 0:
        raise RuntimeError(f"BingX API error: {data}")
    result = data.get("data", [])
    return result if isinstance(result, list) else [result]


def bingx_url(symbol: str) -> str:
    return f"https://bingx.com/ru/perpetual/{symbol}/"


def _history_append(sym: str, ts: float, funding_pct: float, mark_price: float) -> None:
    dq = _TRAP_HISTORY.setdefault(sym, deque(maxlen=_HISTORY_MAXLEN))
    dq.append((ts, funding_pct, mark_price))


def _sample_30m_ago(sym: str) -> Optional[Tuple[float, float]]:
    now = time.time()
    cutoff = now - 30 * 60
    dq = _TRAP_HISTORY.get(sym)
    if not dq:
        return None
    best: Optional[Tuple[float, float, float]] = None
    for ts, fund, price in dq:
        if ts <= cutoff:
            if best is None or ts > best[0]:
                best = (ts, fund, price)
    if best is None:
        return None
    return best[1], best[2]


async def get_short_trap_alerts() -> List[Dict]:
    if not config.get("short_trap_enabled"):
        return []

    threshold = float(config["funding_threshold"])
    delta_drop = float(config["delta_funding_drop"])
    p_min = float(config["price_pump_min"])
    p_max = float(config["price_pump_max"])
    blocked = set(config.get("blocked", []))

    all_data = await fetch_all_funding()
    now = time.time()

    for item in all_data:
        sym = item.get("symbol", "")
        if not sym or sym in blocked:
            continue
        raw = item.get("lastFundingRate")
        if raw is None:
            continue
        try:
            fund_pct = float(raw) * 100.0
        except (TypeError, ValueError):
            continue
        mark = item.get("markPrice") or item.get("indexPrice") or item.get("lastPrice")
        try:
            price = float(mark) if mark is not None else 0.0
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            continue
        _history_append(sym, now, fund_pct, price)

    alerts: List[Dict] = []
    for item in all_data:
        sym = item.get("symbol", "")
        if not sym or sym in blocked:
            continue
        raw = item.get("lastFundingRate")
        if raw is None:
            continue
        try:
            current_pct = float(raw) * 100.0
        except (TypeError, ValueError):
            continue
        if current_pct > threshold:
            continue

        past = _sample_30m_ago(sym)
        if past is None:
            continue
        fund_old, price_old = past
        mark_now = item.get("markPrice") or item.get("indexPrice") or item.get("lastPrice")
        try:
            price_now = float(mark_now) if mark_now is not None else 0.0
        except (TypeError, ValueError):
            price_now = 0.0
        if price_now <= 0 or price_old <= 0:
            continue

        funding_delta = current_pct - fund_old
        if funding_delta > -delta_drop:
            continue

        growth = (price_now - price_old) / price_old * 100.0
        if not (p_min <= growth <= p_max):
            continue

        next_ft = item.get("nextFundingTime")
        mins_to_fund: Optional[int] = None
        if next_ft:
            try:
                nt = int(next_ft) / 1000.0
                mins_to_fund = max(0, int((nt - now) // 60))
            except Exception:
                pass

        alerts.append(
            {
                "symbol": sym,
                "current_funding": current_pct,
                "funding_30min_ago": fund_old,
                "funding_delta": funding_delta,
                "price_growth": growth,
                "mins_to_funding": mins_to_fund,
            }
        )

    logger.info("Short Trap: сигналов=%s", len(alerts))
    return alerts


def format_short_trap_alerts(alerts: List[Dict]) -> str:
    if not alerts:
        return ""

    lines: List[str] = ["🟢 <b>Short Trap (Long Squeeze)</b>\n"]
    for a in alerts:
        url = bingx_url(a["symbol"])
        sym = a["symbol"]
        cur = a["current_funding"]
        old = a["funding_30min_ago"]
        d = a["funding_delta"]
        imp = abs(d)
        g = a["price_growth"]
        pmin = config["price_pump_min"]
        pmax = config["price_pump_max"]
        dmin = config["delta_funding_drop"]
        mtf = a.get("mins_to_funding")
        mtf_s = f"\n⏱ До фандинга: ~<b>{mtf}</b> мин" if mtf is not None else ""

        lines.append(
            f'<a href="{url}">{sym}</a>\n\n'
            f"📉 Фандинг: <b>{cur:+.4f}%</b>\n"
            f"   30 мин назад: <b>{old:+.4f}%</b>\n"
            f"   ↓ усилился на <b>{imp:.4f}%</b> (&gt; {dmin}%)\n\n"
            f"📈 Цена <b>+{g:.2f}%</b> за 30 мин (в диапазоне {pmin}–{pmax}%)\n"
            f"{mtf_s}\n\n"
            f"🔥 Ловушка для шортистов — отличный момент для лонга\n"
            f"{'─' * 28}\n"
        )
    return "\n".join(lines).rstrip()


async def send_tg_message(text: str, topic_id: Optional[int] = None) -> None:
    if not bot_app or not config.get("chat_id") or not text:
        return
    try:
        await bot_app.bot.send_message(
            chat_id=config["chat_id"],
            text=text,
            message_thread_id=topic_id or config.get("topic_id"),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error("Telegram send error: %s", e)


async def short_trap_loop(application: Application) -> None:
    await asyncio.sleep(10)
    while True:
        try:
            if config.get("short_trap_enabled") and config.get("chat_id"):
                alerts = await get_short_trap_alerts()
                if alerts:
                    msg = format_short_trap_alerts(alerts)
                    if msg:
                        await send_tg_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Short Trap loop: %s", e)
            dev_id = config.get("chat_dev_id")
            if dev_id and application.bot:
                try:
                    await application.bot.send_message(
                        chat_id=dev_id,
                        text=f"Short Trap error:\n<code>{e}</code>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
        await asyncio.sleep(_SHORT_TRAP_INTERVAL_SEC)


async def hourly_alert_loop(application: Application) -> None:
    global _last_hourly_fired_key
    while True:
        try:
            now = datetime.now(timezone.utc)
            minute_target = int(config.get("alert_minute", 50))
            slot = (now.year, now.month, now.day, now.hour)
            if now.minute == minute_target and now.second <= 5:
                if slot != _last_hourly_fired_key:
                    _last_hourly_fired_key = slot
                    await regular_alert_job(application)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Hourly loop: %s", e)
        await asyncio.sleep(_HOURLY_POLL_SEC)


async def regular_alert_job(application: Application) -> None:
    if not config.get("chat_id"):
        return
    if config.get("filter_long") is None and config.get("filter_short") is None:
        return
    logger.info("Регулярный часовой алерт (заглушка — см. main_tg.py)")


async def post_init(application: Application) -> None:
    asyncio.create_task(short_trap_loop(application))
    asyncio.create_task(hourly_alert_loop(application))
    logger.info(
        "Планировщики: Short Trap каждые %s с; часовой UTC :%02d",
        _SHORT_TRAP_INTERVAL_SEC,
        int(config.get("alert_minute", 50)),
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 <b>BingX Funding + Short Trap</b>\n\n"
        "• /fundbot_connect — привязать чат/топик\n"
        "• /fundbot_status — настройки\n"
        "• /fundbot_short_trap on|off\n"
        "• /fundbot_funding_threshold &lt;число&gt; (напр. -0.12)\n"
        "• /fundbot_delta_funding &lt;число&gt; (напр. 0.04)\n"
        "• /fundbot_price_pump_min / fundbot_price_pump_max — %% роста за 30 мин\n"
        "• Short Trap: опрос каждые 5 мин; кэш ~30+ мин после старта.\n",
        parse_mode=ParseMode.HTML,
    )


async def cmd_fundbot_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global config
    config["chat_id"] = update.effective_chat.id
    tid = update.message.message_thread_id if update.message else None
    config["topic_id"] = tid
    save_config(config)
    await update.message.reply_text(
        f"✅ Чат: <code>{config['chat_id']}</code>\n"
        f"Топик: <code>{tid if tid is not None else 'нет'}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⚙️ <b>Настройки</b>\n\n"
        f"Short Trap: {'✅' if config.get('short_trap_enabled') else '❌'}\n"
        f"Порог фандинга ≤ <b>{config['funding_threshold']}</b> %\n"
        f"Δ фандинга за 30 мин ≥ <b>{config['delta_funding_drop']}</b> п.п.\n"
        f"Рост цены 30 мин: <b>{config['price_pump_min']}</b> – <b>{config['price_pump_max']}</b> %\n"
        f"Часовой отчёт UTC, минута <b>{int(config.get('alert_minute', 50))}</b>\n"
        f"chat_id: <code>{config.get('chat_id')}</code>",
        parse_mode=ParseMode.HTML,
    )


def _parse_on_off(arg: str) -> Optional[bool]:
    a = arg.strip().lower()
    if a in ("on", "1", "true", "yes", "вкл"):
        return True
    if a in ("off", "0", "false", "no", "выкл"):
        return False
    return None


async def cmd_short_trap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global config
    if not context.args:
        await update.message.reply_text("Использование: /fundbot_short_trap on|off")
        return
    v = _parse_on_off(context.args[0])
    if v is None:
        await update.message.reply_text("Укажите on или off")
        return
    config["short_trap_enabled"] = v
    save_config(config)
    await update.message.reply_text("✅ Short Trap: " + ("включён" if v else "выключен"))


async def cmd_funding_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global config
    if not context.args:
        await update.message.reply_text("Использование: /fundbot_funding_threshold -0.12")
        return
    try:
        config["funding_threshold"] = float(context.args[0].replace("%", ""))
        save_config(config)
        await update.message.reply_text(f"✅ Порог: ≤ {config['funding_threshold']}%")
    except ValueError:
        await update.message.reply_text("Нужно число, напр. -0.12")


async def cmd_delta_funding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global config
    if not context.args:
        await update.message.reply_text("Использование: /fundbot_delta_funding 0.05")
        return
    try:
        config["delta_funding_drop"] = float(context.args[0].replace("%", ""))
        save_config(config)
        await update.message.reply_text(f"✅ Δ фандинга: {config['delta_funding_drop']} п.п.")
    except ValueError:
        await update.message.reply_text("Нужно число")


async def cmd_pump_min(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global config
    if not context.args:
        await update.message.reply_text("Использование: /fundbot_price_pump_min 1.8")
        return
    try:
        config["price_pump_min"] = float(context.args[0].replace("%", ""))
        save_config(config)
        await update.message.reply_text(f"✅ Мин. рост: {config['price_pump_min']}%")
    except ValueError:
        await update.message.reply_text("Нужно число")


async def cmd_pump_max(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global config
    if not context.args:
        await update.message.reply_text("Использование: /fundbot_price_pump_max 6")
        return
    try:
        config["price_pump_max"] = float(context.args[0].replace("%", ""))
        save_config(config)
        await update.message.reply_text(f"✅ Макс. рост: {config['price_pump_max']}%")
    except ValueError:
        await update.message.reply_text("Нужно число")


def main() -> None:
    """
    Синхронный run_polling — свой event loop внутри PTB.
    Не использовать asyncio.run(main)+await run_polling: на Py3.10+ это даёт
    «event loop is already running».
    """
    global bot_app, BOT_TOKEN

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    BOT_TOKEN = (
        os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("FUNDING_BOT_TOKEN") or ""
    ).strip().strip('"').strip("'")
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        logger.error("Задайте TELEGRAM_BOT_TOKEN или FUNDING_BOT_TOKEN в .env")
        return

    logger.info("Запуск BingX Funding + Short Trap (asyncio-планировщик в post_init)")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )
    bot_app = application

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("fundbot_connect", cmd_fundbot_connect))
    application.add_handler(CommandHandler("fundbot_status", cmd_status))
    application.add_handler(CommandHandler("fundbot_short_trap", cmd_short_trap))
    application.add_handler(CommandHandler("fundbot_funding_threshold", cmd_funding_threshold))
    application.add_handler(CommandHandler("fundbot_delta_funding", cmd_delta_funding))
    application.add_handler(CommandHandler("fundbot_price_pump_min", cmd_pump_min))
    application.add_handler(CommandHandler("fundbot_price_pump_max", cmd_pump_max))

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
