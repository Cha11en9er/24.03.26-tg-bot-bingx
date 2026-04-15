"""
BingX: фандинг + сигналы «выдавливание лонгов» (рост цены при усилении минусового funding).
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
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import Conflict
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
    "funding_threshold": -0.005,
    "funding_range_min": -0.60,
    "funding_range_max": -0.005,
    "delta_funding_drop": 0.0005,
    "price_pump_min": 0.25,
    "price_pump_max": 12.0,
    # Окно сравнения с историческими 3m-срезами из prices.csv (сигнал ближе к «минуте 0»).
    "lookback_minutes": 15,
    "mention_cooldown_min": 0,
    # Минимум минут между повторными сигналами по одному тикеру (даже если mention_cooldown_min = 0).
    "repeat_signal_cooldown_min": 45,
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

VOLUME_PRICE_FUNDING_TEST_BOT_TOKEN: Optional[str] = None
bot_app: Optional[Application] = None

_TRAP_HISTORY: Dict[str, Deque[Tuple[float, float, float]]] = {}
# ~2 ч истории при опросе раз в 3 мин (хватает для lookback и запаса).
_HISTORY_MAXLEN = 45
_SHORT_TRAP_INTERVAL_SEC = 180
_HOURLY_POLL_SEC = 30
_last_hourly_fired_key: Optional[Tuple[int, int, int, int]] = None
_last_mentioned_at: Dict[str, float] = {}
_bg_tasks: List[asyncio.Task] = []
_DEBUG_DIR = Path("debug_short_trap")
_DEBUG_KEEP_FILES = 240


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


def _sample_n_minutes_ago(sym: str, lookback_minutes: int) -> Optional[Tuple[float, float]]:
    """Ближайшая точка истории не новее чем lookback_minutes назад (для сравнения цены и funding)."""
    if lookback_minutes <= 0:
        return None
    now = time.time()
    cutoff = now - lookback_minutes * 60
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


def _float_to_str(v: Optional[float], ndigits: int = 6) -> str:
    if v is None:
        return ""
    return f"{v:.{ndigits}f}"


def _ms_to_utc_str(ms: Optional[int]) -> str:
    if not ms:
        return ""
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return ""


def _cleanup_old_debug_files() -> None:
    if not _DEBUG_DIR.exists():
        return
    files = sorted(
        (p for p in _DEBUG_DIR.iterdir() if p.is_file()),
        key=lambda p: p.name,
        reverse=True,
    )
    for old_file in files[_DEBUG_KEEP_FILES:]:
        try:
            old_file.unlink()
        except Exception:
            logger.warning("Не удалось удалить старый debug файл: %s", old_file)


def _write_debug_file(run_key: str, suffix: str, lines: List[str]) -> None:
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path = _DEBUG_DIR / f"{run_key}_{suffix}"
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
    except Exception as e:
        logger.warning("Ошибка записи debug файла (%s): %s", suffix, e)


def _write_short_trap_debug(
    run_key: str,
    parse_started_at: datetime,
    all_rows: List[str],
    funding_rows: List[str],
    price_rows: List[str],
    alert_rows: List[str],
    total_count: int,
) -> None:
    range_min = float(config.get("funding_range_min", -0.20))
    range_max = float(config.get("funding_range_max", -0.10))
    if range_min > range_max:
        range_min, range_max = range_max, range_min
    delta_drop = float(config.get("delta_funding_drop", 0.04))
    p_min = float(config.get("price_pump_min", 2.0))
    p_max = float(config.get("price_pump_max", 5.0))
    lookback = int(config.get("lookback_minutes", 15) or 15)
    blocked = sorted(config.get("blocked", []))
    eff_cd = _effective_symbol_cooldown_minutes()

    overview = [
        f"parse_started_utc={parse_started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"symbols_total={total_count}",
        f"lookback_minutes={lookback}",
        f"poll_interval_sec={_SHORT_TRAP_INTERVAL_SEC}",
        f"mention_cooldown_min={int(config.get('mention_cooldown_min', 0) or 0)}",
        f"repeat_signal_cooldown_min={int(config.get('repeat_signal_cooldown_min', 45) or 0)}",
        f"effective_symbol_cooldown_min={eff_cd}",
        f"funding_range_min={range_min}",
        f"funding_range_max={range_max}",
        f"delta_funding_drop={delta_drop}",
        f"price_pump_min={p_min}",
        f"price_pump_max={p_max}",
        f"blocked_count={len(blocked)}",
        f"blocked_symbols={','.join(blocked)}",
        f"history_size={len(_TRAP_HISTORY)}",
        f"all_rows={max(0, len(all_rows) - 1)}",
        f"funding_rows={max(0, len(funding_rows) - 1)}",
        f"price_rows={max(0, len(price_rows) - 1)}",
        f"alerts_rows={max(0, len(alert_rows) - 1)}",
    ]

    _write_debug_file(run_key, "00_overview.txt", overview)
    _write_debug_file(run_key, "01_all_coins.tsv", all_rows)
    _write_debug_file(run_key, "02_funding_filter.tsv", funding_rows)
    _write_debug_file(run_key, "03_price_filter.tsv", price_rows)
    _write_debug_file(run_key, "04_alerts.tsv", alert_rows)
    _cleanup_old_debug_files()


async def get_short_trap_alerts() -> List[Dict]:
    if not config.get("short_trap_enabled"):
        return []

    range_min = float(config.get("funding_range_min", -0.20))
    range_max = float(config.get("funding_range_max", -0.10))
    if range_min > range_max:
        range_min, range_max = range_max, range_min
    delta_drop = float(config["delta_funding_drop"])
    p_min = float(config["price_pump_min"])
    p_max = float(config["price_pump_max"])
    lookback_minutes = int(config.get("lookback_minutes", 15) or 15)
    blocked = set(config.get("blocked", []))

    all_data = await fetch_all_funding()
    now = time.time()
    parse_started_at = datetime.now(timezone.utc)
    run_key = parse_started_at.strftime("%Y%m%d_%H%M%S")

    all_rows: List[str] = [
        "symbol\tblocked\tfunding_pct\tprice\tnext_funding_time_utc\tstatus"
    ]
    funding_rows: List[str] = [
        (
            "symbol\tcurrent_funding_pct\tfunding_past_pct\tfunding_delta_pct\t"
            "abs_growth_pct\tin_range\tabs_growth_ok\treason"
        )
    ]
    price_rows: List[str] = [
        (
            "symbol\tprice_now\tprice_past\tprice_growth_pct\tprice_range_ok\t"
            "reason"
        )
    ]
    alert_rows: List[str] = [
        "symbol\tcurrent_funding_pct\tfunding_past_pct\tprice_growth_pct\tmins_to_funding"
    ]

    for item in all_data:
        sym = item.get("symbol", "")
        is_blocked = bool(sym and sym in blocked)
        raw = item.get("lastFundingRate")
        mark = item.get("markPrice") or item.get("indexPrice") or item.get("lastPrice")
        next_ft_raw = item.get("nextFundingTime")

        funding_pct: Optional[float] = None
        if raw is not None:
            try:
                funding_pct = float(raw) * 100.0
            except (TypeError, ValueError):
                funding_pct = None

        price: Optional[float] = None
        if mark is not None:
            try:
                price = float(mark)
            except (TypeError, ValueError):
                price = None

        status = "ok"
        if not sym:
            status = "skip:no_symbol"
        elif is_blocked:
            status = "skip:blocked"
        elif funding_pct is None:
            status = "skip:bad_funding"
        elif price is None:
            status = "skip:bad_price"
        elif price <= 0:
            status = "skip:price_le_0"

        next_ft_ms: Optional[int] = None
        if next_ft_raw is not None:
            try:
                next_ft_ms = int(next_ft_raw)
            except (TypeError, ValueError):
                next_ft_ms = None

        all_rows.append(
            (
                f"{sym}\t{int(is_blocked)}\t{_float_to_str(funding_pct)}\t{_float_to_str(price)}\t"
                f"{_ms_to_utc_str(next_ft_ms)}\t{status}"
            )
        )

        if not sym or is_blocked:
            continue
        if funding_pct is None:
            continue
        if price is None or price <= 0:
            continue
        _history_append(sym, now, funding_pct, price)

    alerts: List[Dict] = []
    for item in all_data:
        sym = item.get("symbol", "")
        if not sym or sym in blocked:
            continue
        raw = item.get("lastFundingRate")
        if raw is None:
            funding_rows.append(f"{sym}\t\t\t\t\t0\t0\tskip:empty_funding")
            continue
        try:
            current_pct = float(raw) * 100.0
        except (TypeError, ValueError):
            funding_rows.append(f"{sym}\t\t\t\t\t0\t0\tskip:bad_funding")
            continue
        # Рабочая зона раннего сигнала: funding в диапазоне, а не "чем глубже, тем лучше".
        if not (range_min <= current_pct <= range_max):
            funding_rows.append(
                (
                    f"{sym}\t{_float_to_str(current_pct)}\t\t\t\t0\t0\t"
                    "skip:out_of_funding_range"
                )
            )
            continue

        past = _sample_n_minutes_ago(sym, lookback_minutes)
        if past is None:
            funding_rows.append(
                (
                    f"{sym}\t{_float_to_str(current_pct)}\t\t\t\t1\t0\t"
                    "skip:no_lookback_sample"
                )
            )
            continue
        fund_old, price_old = past
        mark_now = item.get("markPrice") or item.get("indexPrice") or item.get("lastPrice")
        try:
            price_now = float(mark_now) if mark_now is not None else 0.0
        except (TypeError, ValueError):
            price_now = 0.0
        if price_now <= 0 or price_old <= 0:
            funding_rows.append(
                (
                    f"{sym}\t{_float_to_str(current_pct)}\t{_float_to_str(fund_old)}\t\t\t1\t0\t"
                    "skip:bad_price_for_compare"
                )
            )
            continue

        funding_delta = current_pct - fund_old
        # funding должен заметно уйти дальше от нуля по модулю.
        abs_growth = abs(current_pct) - abs(fund_old)
        if abs_growth < delta_drop:
            funding_rows.append(
                (
                    f"{sym}\t{_float_to_str(current_pct)}\t{_float_to_str(fund_old)}\t"
                    f"{_float_to_str(funding_delta)}\t{_float_to_str(abs_growth)}\t1\t0\t"
                    "skip:delta_too_small"
                )
            )
            continue
        funding_rows.append(
            (
                f"{sym}\t{_float_to_str(current_pct)}\t{_float_to_str(fund_old)}\t"
                f"{_float_to_str(funding_delta)}\t{_float_to_str(abs_growth)}\t1\t1\tpass"
            )
        )

        growth = (price_now - price_old) / price_old * 100.0
        if not (p_min <= growth <= p_max):
            price_rows.append(
                (
                    f"{sym}\t{_float_to_str(price_now)}\t{_float_to_str(price_old)}\t"
                    f"{_float_to_str(growth)}\t0\tskip:out_of_price_range"
                )
            )
            continue
        price_rows.append(
            (
                f"{sym}\t{_float_to_str(price_now)}\t{_float_to_str(price_old)}\t"
                f"{_float_to_str(growth)}\t1\tpass"
            )
        )

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
                "funding_past": fund_old,
                "funding_delta": funding_delta,
                "price_growth": growth,
                "mins_to_funding": mins_to_fund,
                "lookback_minutes": lookback_minutes,
            }
        )
        alert_rows.append(
            (
                f"{sym}\t{_float_to_str(current_pct)}\t{_float_to_str(fund_old)}\t"
                f"{_float_to_str(growth)}\t{mins_to_fund if mins_to_fund is not None else ''}"
            )
        )

    logger.info("Выдавливание лонгов: сигналов=%s", len(alerts))
    _write_short_trap_debug(
        run_key=run_key,
        parse_started_at=parse_started_at,
        all_rows=all_rows,
        funding_rows=funding_rows,
        price_rows=price_rows,
        alert_rows=alert_rows,
        total_count=len(all_data),
    )
    return alerts


def format_short_trap_alerts(alerts: List[Dict]) -> str:
    if not alerts:
        return ""

    lines: List[str] = ["🟢 <b>Выдавливание лонгов</b>\n<i>фандинг в минус усиливается, цена растёт</i>\n"]
    for a in alerts:
        url = bingx_url(a["symbol"])
        sym = a["symbol"]
        cur = a["current_funding"]
        old = a.get("funding_past", a.get("funding_30min_ago", 0.0))
        lb = int(a.get("lookback_minutes", config.get("lookback_minutes", 15)) or 15)
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
            f"   {lb} мин назад: <b>{old:+.4f}%</b>\n"
            f"   ↓ усилился на <b>{imp:.4f}%</b> (|f| вырос ≥ {dmin} п.п.)\n\n"
            f"📈 Цена <b>+{g:.2f}%</b> за {lb} мин (допустимо {pmin}–{pmax}%)\n"
            f"{mtf_s}\n"
        )
    return "\n".join(lines).rstrip()


def _effective_symbol_cooldown_minutes() -> int:
    """Итоговая пауза по символу: max(ручной cooldown, анти-спам повтора)."""
    manual = int(config.get("mention_cooldown_min", 0) or 0)
    repeat = int(config.get("repeat_signal_cooldown_min", 45) or 0)
    return max(manual, repeat)


def apply_mention_cooldown(alerts: List[Dict]) -> List[Dict]:
    cooldown_min = _effective_symbol_cooldown_minutes()
    if cooldown_min <= 0:
        return alerts

    now = time.time()
    cooldown_sec = cooldown_min * 60
    filtered: List[Dict] = []
    for a in alerts:
        sym = a.get("symbol", "")
        last_ts = _last_mentioned_at.get(sym, 0.0)
        if now - last_ts < cooldown_sec:
            continue
        filtered.append(a)
    return filtered


def mark_alerts_mentioned(alerts: List[Dict]) -> None:
    if not alerts:
        return
    now = time.time()
    for a in alerts:
        sym = a.get("symbol", "")
        if sym:
            _last_mentioned_at[sym] = now


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
                    alerts = apply_mention_cooldown(alerts)
                    msg = format_short_trap_alerts(alerts)
                    if msg:
                        await send_tg_message(msg)
                        mark_alerts_mentioned(alerts)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Цикл «выдавливание лонгов»: %s", e)
            dev_id = config.get("chat_dev_id")
            if dev_id and application.bot:
                try:
                    await application.bot.send_message(
                        chat_id=dev_id,
                        text=f"Ошибка «выдавливание лонгов»:\n<code>{e}</code>",
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
    _bg_tasks.append(asyncio.create_task(short_trap_loop(application), name="short_trap_loop"))
    _bg_tasks.append(asyncio.create_task(hourly_alert_loop(application), name="hourly_alert_loop"))
    logger.info(
        "Планировщики: выдавливание лонгов каждые %s с (lookback %s мин); часовой UTC :%02d",
        _SHORT_TRAP_INTERVAL_SEC,
        int(config.get("lookback_minutes", 15) or 15),
        int(config.get("alert_minute", 50)),
    )


async def post_shutdown(application: Application) -> None:
    for task in _bg_tasks:
        if not task.done():
            task.cancel()
    if _bg_tasks:
        await asyncio.gather(*_bg_tasks, return_exceptions=True)
    _bg_tasks.clear()


async def app_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, Conflict):
        logger.error(
            "Telegram Conflict: уже запущен другой экземпляр бота с этим токеном. "
            "Остановите второй процесс и запустите только один instance."
        )
        if context.application and context.application.running:
            await context.application.stop()
        return
    logger.exception("Unhandled application error: %s", err)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 <b>BingX: фандинг + выдавливание лонгов</b>\n\n"
        "• /fundbot_connect — привязать чат/топик\n"
        "• /fundbot_status — настройки\n"
        "• /fundbot_short_trap on|off — сигналы «выдавливание лонгов»\n"
        "• /fundbot_funding_range &lt;min&gt; &lt;max&gt; (напр. -0.2 -0.1)\n"
        "• /fundbot_funding_threshold &lt;число&gt; — legacy-команда (верхняя граница)\n"
        "• /fundbot_delta_funding &lt;число&gt; — мин. рост |funding| за окно lookback (п.п.)\n"
        "• /fundbot_price_pump_min / fundbot_price_pump_max — %% роста цены за lookback\n"
        "• /fundbot_lookback_minutes &lt;мин&gt; — окно сравнения с прошлым срезом (по умолч. 15)\n"
        "• /fundbot_mention_cooldown &lt;мин&gt; — пауза повторного упоминания тикера\n"
        "• /fundbot_repeat_signal_cooldown &lt;мин&gt; — мин. интервал между сигналами по одному тикеру (0 = выкл)\n"
        "• Выдавливание лонгов: опрос каждые 3 мин; нужен запас истории ≥ lookback после старта.\n",
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
        f"Выдавливание лонгов: {'✅' if config.get('short_trap_enabled') else '❌'}\n"
        f"Диапазон funding: <b>{config.get('funding_range_min', -0.2)}</b> .. <b>{config.get('funding_range_max', -0.1)}</b> %\n"
        f"Lookback: <b>{int(config.get('lookback_minutes', 15) or 15)}</b> мин\n"
        f"Рост |funding| за окно ≥ <b>{config['delta_funding_drop']}</b> п.п.\n"
        f"Рост цены за окно: <b>{config['price_pump_min']}</b> – <b>{config['price_pump_max']}</b> %\n"
        f"Cooldown упоминаний: <b>{int(config.get('mention_cooldown_min', 0))}</b> мин\n"
        f"Пауза повтора сигнала по тикеру: <b>{int(config.get('repeat_signal_cooldown_min', 45))}</b> мин "
        f"(эффективно max с cooldown: <b>{_effective_symbol_cooldown_minutes()}</b> мин)\n"
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
        await update.message.reply_text(
            "Использование: /fundbot_short_trap on|off\n(вкл/выкл сигналы «выдавливание лонгов»)"
        )
        return
    v = _parse_on_off(context.args[0])
    if v is None:
        await update.message.reply_text("Укажите on или off")
        return
    config["short_trap_enabled"] = v
    save_config(config)
    await update.message.reply_text(
        "✅ Сигналы «выдавливание лонгов»: " + ("включены" if v else "выключены")
    )


async def cmd_funding_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global config
    if not context.args:
        await update.message.reply_text("Использование: /fundbot_funding_threshold -0.12")
        return
    try:
        val = float(context.args[0].replace("%", ""))
        config["funding_threshold"] = val
        config["funding_range_max"] = val
        # Legacy: автоматически держим окно шириной 0.1 п.п. ниже верхней границы.
        config["funding_range_min"] = min(config.get("funding_range_min", val - 0.1), val - 0.1)
        save_config(config)
        await update.message.reply_text(
            "✅ Обновлено (legacy):\n"
            f"Диапазон funding: {config['funding_range_min']} .. {config['funding_range_max']}%"
        )
    except ValueError:
        await update.message.reply_text("Нужно число, напр. -0.12")


async def cmd_funding_range(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global config
    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование: /fundbot_funding_range <min> <max>\n"
            "Пример: /fundbot_funding_range -0.2 -0.1"
        )
        return
    try:
        rmin = float(context.args[0].replace("%", ""))
        rmax = float(context.args[1].replace("%", ""))
        if rmin > rmax:
            rmin, rmax = rmax, rmin
        config["funding_range_min"] = rmin
        config["funding_range_max"] = rmax
        # Сохраняем также legacy-поле как верхнюю границу.
        config["funding_threshold"] = rmax
        save_config(config)
        await update.message.reply_text(f"✅ Диапазон funding: {rmin} .. {rmax}%")
    except ValueError:
        await update.message.reply_text("Нужны два числа, пример: -0.2 -0.1")


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


async def cmd_lookback_minutes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global config
    if not context.args:
        cur = int(config.get("lookback_minutes", 15) or 15)
        await update.message.reply_text(
            "Использование: /fundbot_lookback_minutes <минуты>\n"
            f"Сейчас: {cur} мин (сравнение цены и funding с прошлым срезом не новее этого окна)"
        )
        return
    try:
        m = int(context.args[0])
        if m < 3 or m > 120:
            await update.message.reply_text("Допустимо от 3 до 120 минут")
            return
        config["lookback_minutes"] = m
        save_config(config)
        await update.message.reply_text(f"✅ Lookback: {m} мин")
    except ValueError:
        await update.message.reply_text("Нужно целое число минут, например: 15")


async def cmd_mention_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global config
    if not context.args:
        current = int(config.get("mention_cooldown_min", 0))
        await update.message.reply_text(
            "Использование: /fundbot_mention_cooldown <минуты>\n"
            f"Сейчас: {current} мин"
        )
        return
    arg = context.args[0].strip().lower()
    if arg in ("off", "0"):
        config["mention_cooldown_min"] = 0
        save_config(config)
        eff = _effective_symbol_cooldown_minutes()
        extra = (
            f"\n(Повтор по тикеру всё ещё не чаще {eff} мин — см. /fundbot_repeat_signal_cooldown)"
            if eff > 0
            else ""
        )
        await update.message.reply_text("✅ Cooldown упоминаний выключен" + extra)
        return
    try:
        minutes = int(arg)
        if minutes < 0:
            raise ValueError
        config["mention_cooldown_min"] = minutes
        save_config(config)
        await update.message.reply_text(f"✅ Cooldown упоминаний: {minutes} мин")
    except ValueError:
        await update.message.reply_text("Нужно целое число минут, например: 30")


async def cmd_repeat_signal_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global config
    if not context.args:
        cur = int(config.get("repeat_signal_cooldown_min", 45) or 0)
        await update.message.reply_text(
            "Использование: /fundbot_repeat_signal_cooldown <минуты>\n"
            "Минимальный интервал между сигналами по одному тикеру (анти-спам при стабильном совпадении условий).\n"
            f"Сейчас: {cur} мин (0 = отключить этот пол; тогда действует только /fundbot_mention_cooldown)\n"
            f"Эффективно по символу: max(mention, repeat) = {_effective_symbol_cooldown_minutes()} мин"
        )
        return
    arg = context.args[0].strip().lower()
    if arg in ("off", "0"):
        config["repeat_signal_cooldown_min"] = 0
        save_config(config)
        await update.message.reply_text(
            "✅ Пауза повтора сигнала по тикеру отключена "
            "(остаётся только /fundbot_mention_cooldown, если он > 0)"
        )
        return
    try:
        minutes = int(arg)
        if minutes < 0 or minutes > 1440:
            raise ValueError
        config["repeat_signal_cooldown_min"] = minutes
        save_config(config)
        await update.message.reply_text(
            f"✅ Пауза повтора сигнала по тикеру: {minutes} мин "
            f"(итого max с mention: {_effective_symbol_cooldown_minutes()} мин)"
        )
    except ValueError:
        await update.message.reply_text("Нужно целое число минут 0–1440, например: 45")


def main() -> None:
    """
    Синхронный run_polling — свой event loop внутри PTB.
    Не использовать asyncio.run(main)+await run_polling: на Py3.10+ это даёт
    «event loop is already running».
    """
    global bot_app, VOLUME_PRICE_FUNDING_TEST_BOT_TOKEN

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    VOLUME_PRICE_FUNDING_TEST_BOT_TOKEN = (
        os.getenv("VOLUME_PRICE_FUNDING_TEST_BOT_TOKEN")
        or os.getenv("TELEGRAM_VOLUME_PRICE_FUNDING_TEST_BOT_TOKEN")
        or os.getenv("FUNDING_VOLUME_PRICE_FUNDING_TEST_BOT_TOKEN")
        or os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("FUNDING_BOT_TOKEN")
        or ""
    ).strip().strip('"').strip("'")
    if not VOLUME_PRICE_FUNDING_TEST_BOT_TOKEN or ":" not in VOLUME_PRICE_FUNDING_TEST_BOT_TOKEN:
        logger.error("Задайте TELEGRAM_VOLUME_PRICE_FUNDING_TEST_BOT_TOKEN или FUNDING_VOLUME_PRICE_FUNDING_TEST_BOT_TOKEN в .env")
        return

    logger.info("Запуск BingX: фандинг + выдавливание лонгов (asyncio-планировщик в post_init)")

    application = (
        ApplicationBuilder()
        .token(VOLUME_PRICE_FUNDING_TEST_BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    bot_app = application
    application.add_error_handler(app_error_handler)

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("fundbot_connect", cmd_fundbot_connect))
    application.add_handler(CommandHandler("fundbot_status", cmd_status))
    application.add_handler(CommandHandler("fundbot_short_trap", cmd_short_trap))
    application.add_handler(CommandHandler("fundbot_funding_range", cmd_funding_range))
    application.add_handler(CommandHandler("fundbot_funding_threshold", cmd_funding_threshold))
    application.add_handler(CommandHandler("fundbot_delta_funding", cmd_delta_funding))
    application.add_handler(CommandHandler("fundbot_price_pump_min", cmd_pump_min))
    application.add_handler(CommandHandler("fundbot_price_pump_max", cmd_pump_max))
    application.add_handler(CommandHandler("fundbot_lookback_minutes", cmd_lookback_minutes))
    application.add_handler(CommandHandler("fundbot_mention_cooldown", cmd_mention_cooldown))
    application.add_handler(CommandHandler("fundbot_repeat_signal_cooldown", cmd_repeat_signal_cooldown))

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
