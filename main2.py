"""
BingX Funding Rate Monitor — batch version
Один REST-запрос → фандинг по всем тикерам.
"""

import json
import time
import threading
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


# ══════════════════════════════════════════════
# HTTP
# ══════════════════════════════════════════════
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


# ══════════════════════════════════════════════
# Получить ВСЕ фандинги за один запрос
# ══════════════════════════════════════════════
def fetch_all_funding() -> list[dict]:
    """
    GET /openApi/swap/v2/quote/premiumIndex  (без symbol)
    Возвращает список словарей по ВСЕМ фьючерсным парам BingX.
    """
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex"
    try:
        resp = http_get_json(url)
    except Exception as e:
        print(f"[ERROR] Не удалось получить фандинг: {e}")
        return []

    if not isinstance(resp, dict) or resp.get("code") != 0:
        print(f"[ERROR] Неожиданный ответ API: {resp}")
        return []

    data = resp.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


# ══════════════════════════════════════════════
# Нормализация имени тикера
# ══════════════════════════════════════════════
def normalize_symbol(raw: str) -> str:
    """
    Приводит к формату BingX: 'BTC-USDT'.
    Принимает: BTC, BTCUSDT, BTC-USDT, BTC/USDT
    """
    s = raw.upper().strip().replace("/", "").replace(" ", "")
    if s.endswith("-USDT"):
        return s
    s = s.replace("-", "")
    if s.endswith("USDT"):
        base = s[:-4]
    else:
        base = s
    return f"{base}-USDT"


# ══════════════════════════════════════════════
# Минуты до списания фандинга
# ══════════════════════════════════════════════
def minutes_until_funding(next_funding_time) -> int | None:
    if not next_funding_time:
        return None
    try:
        next_ts = int(next_funding_time) / 1000
        now_ts = datetime.now(timezone.utc).timestamp()
        diff = next_ts - now_ts
        if diff < 0:
            return 0
        return int(diff // 60)
    except Exception:
        return None


# ══════════════════════════════════════════════
# Основной класс мониторинга
# ══════════════════════════════════════════════
class FundingMonitor:
    def __init__(self, symbols: list[str], interval_sec: float = 300.0):
        """
        symbols     — список тикеров: ["BTC", "ETH", "SOL", "APT", ...]
        interval_sec — интервал обновления (по умолчанию 300 = 5 мин)
        """
        self._watched: set[str] = {normalize_symbol(s) for s in symbols}
        self._blocked: set[str] = set()
        self._interval = interval_sec
        self._stop = threading.Event()

        # Последний снимок: symbol -> {rate, minutes, raw}
        self.last_snapshot: dict[str, dict] = {}

    # ---------- управление списком ----------
    def add(self, symbol: str):
        self._watched.add(normalize_symbol(symbol))

    def remove(self, symbol: str):
        self._watched.discard(normalize_symbol(symbol))

    def block(self, symbol: str):
        self._blocked.add(normalize_symbol(symbol))

    def unblock(self, symbol: str):
        self._blocked.discard(normalize_symbol(symbol))

    @property
    def active_symbols(self) -> set[str]:
        return self._watched - self._blocked

    # ---------- один цикл опроса ----------
    def poll(self) -> dict[str, dict]:
        """
        Делает ОДИН запрос к API, фильтрует по отслеживаемым тикерам.
        Возвращает dict: symbol -> {rate_pct, minutes, emoji, raw}
        """
        all_data = fetch_all_funding()
        active = self.active_symbols
        result: dict[str, dict] = {}

        for item in all_data:
            sym = item.get("symbol", "")
            if sym not in active:
                continue

            rate_raw = item.get("lastFundingRate")
            next_time = item.get("nextFundingTime")

            if rate_raw is None:
                continue

            rate_pct = float(rate_raw) * 100
            mins = minutes_until_funding(next_time)
            emoji = "🟢" if rate_pct >= 0 else "🔴"

            result[sym] = {
                "rate_pct": rate_pct,
                "minutes": mins,
                "emoji": emoji,
                "raw": item,
            }

        self.last_snapshot = result
        return result

    # ---------- вывод в консоль ----------
    def print_snapshot(self, snapshot: dict[str, dict] | None = None):
        snap = snapshot or self.last_snapshot
        if not snap:
            print("[INFO] Нет данных для отображения.")
            return

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"\n{'═' * 55}")
        print(f"  📊 Funding Rates  |  {now_str}")
        print(f"{'═' * 55}")

        # Сортировка: сначала по rate_pct (от наименьшего к наибольшему)
        sorted_items = sorted(snap.items(), key=lambda x: x[1]["rate_pct"])

        for sym, info in sorted_items:
            rate = info["rate_pct"]
            mins = info["minutes"]
            emoji = info["emoji"]
            mins_str = f"{mins} min" if mins is not None else "N/A"
            # Убираем дефис для красивого вывода типа SOLUSDT
            display_sym = sym.replace("-", "")
            print(f"  {emoji} {display_sym:<14} {rate:>+10.4f}%   ({mins_str})")

        not_found = self.active_symbols - set(snap.keys())
        if not_found:
            print(f"\n  ⚠️  Не найдены: {', '.join(sorted(not_found))}")

        print(f"{'═' * 55}\n")

    # ---------- цикл мониторинга ----------
    def run(self):
        print(f"[START] Мониторинг {len(self.active_symbols)} тикеров, "
              f"интервал {self._interval}с")
        print(f"[WATCH] {', '.join(sorted(self.active_symbols))}")
        if self._blocked:
            print(f"[BLOCK] {', '.join(sorted(self._blocked))}")

        while not self._stop.is_set():
            try:
                snap = self.poll()
                self.print_snapshot(snap)
            except Exception as e:
                print(f"[ERROR] {e}")

            self._stop.wait(self._interval)

    def stop(self):
        self._stop.set()


# ══════════════════════════════════════════════
# Запуск
# ══════════════════════════════════════════════
if __name__ == "__main__":
    # ---------- задай свои тикеры ----------
    TICKERS = [
        "BTC", "ETH", "SOL", "APT", "LTC",
        "DOGE", "XRP", "ADA", "AVAX", "LINK",
        "DOT", "MATIC", "ARB", "OP", "SUI",
        "PEPE", "WIF", "BONK", "FET", "RNDR",
        "INJ", "TIA", "SEI", "JUP", "WLD",
        "NEAR", "FIL", "ATOM", "UNI", "AAVE",
        "MKR", "CRV", "RUNE", "STX", "IMX",
        "MANTA", "STRK", "PYTH", "JTO", "TRX",
    ]
    # 40 тикеров — один HTTP-запрос, без ограничений

    monitor = FundingMonitor(symbols=TICKERS, interval_sec=300)

    # Пример: заблокировать тикер
    # monitor.block("APT")

    try:
        monitor.run()
    except KeyboardInterrupt:
        print("\n⚠️  Остановка...")
        monitor.stop()
        print("👋 Завершено.")