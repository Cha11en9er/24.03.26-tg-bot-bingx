import argparse
import json
import gzip
import time
import threading
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import websocket


def parse_args():
    parser = argparse.ArgumentParser(description="Фандинг BingX Futures")
    parser.add_argument("--base", "-b", required=True, help="Базовый актив, напр. ETH, BTC")
    return parser.parse_args()


args = parse_args()
BASE_ASSET = args.base.upper().replace("-", "").replace("/", "").replace("USDT", "")
SYMBOL = f"{BASE_ASSET}-USDT"
PING_INTERVAL = 20.0
RECONNECT_DELAY = 5.0

stop_event = threading.Event()


# ==============================================
# HTTP-УТИЛИТА
# ==============================================
def http_get_json(url: str, timeout: int = 8):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="ignore"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        try:
            return json.loads(body)
        except Exception:
            raise RuntimeError(f"HTTP {e.code}: {body[:200] or e.reason}")
    except URLError as e:
        raise RuntimeError(f"Сетевая ошибка: {e.reason}")


# ==============================================
# ПРОВЕРКА ТИКЕРА НА BINGX
# ==============================================
def check_bingx_symbol(symbol: str) -> tuple[bool, str | None]:
    url = (
        "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
        + f"?{urlencode({'symbol': symbol})}"
    )
    try:
        data = http_get_json(url)
    except Exception as e:
        return False, str(e)

    if isinstance(data, dict):
        if data.get("code") == 0:
            return True, None
        return False, data.get("msg") or data.get("message") or f"code={data.get('code')}"
    return False, "неизвестный ответ"


# ==============================================
# ПОЛУЧЕНИЕ ФАНДИНГА ЧЕРЕЗ REST (один раз + раз в N секунд)
# ==============================================
def fetch_funding_rate(symbol: str) -> dict | None:
    """
    Возвращает словарь с данными о фандинге или None при ошибке.
    Используем endpoint /openApi/swap/v2/quote/premiumIndex
    """
    url = (
        "https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex"
        + f"?{urlencode({'symbol': symbol})}"
    )
    try:
        data = http_get_json(url)
    except Exception as e:
        print(f"[REST] Ошибка получения фандинга: {e}")
        return None

    if isinstance(data, dict) and data.get("code") == 0:
        return data.get("data", {})
    print(f"[REST] Неожиданный ответ: {data}")
    return None


def get_time_until_next_funding(next_funding_time):
    """Возвращает время до следующего списания фандинга"""
    if not next_funding_time:
        return "N/A"
    
    try:
        from datetime import datetime, timezone
        next_time = datetime.fromtimestamp(int(next_funding_time) / 1000, tz=timezone.utc)
        current_time = datetime.now(timezone.utc)
        
        if next_time > current_time:
            time_diff = next_time - current_time
            hours, remainder = divmod(int(time_diff.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            
            if hours > 0:
                return f"{hours}ч {minutes}м {seconds}с"
            elif minutes > 0:
                return f"{minutes}м {seconds}с"
            else:
                return f"{seconds}с"
        else:
            return "Прошло"
    except Exception:
        return "N/A"


def print_funding(symbol: str):
    result = fetch_funding_rate(symbol)
    if result is None:
        print(f"[Фандинг] Не удалось получить данные для {symbol}")
        return

    funding_rate = result.get("lastFundingRate")
    next_funding_time = result.get("nextFundingTime")

    if funding_rate is not None:
        try:
            rate_pct = float(funding_rate) * 100
            rate_str = f"{rate_pct:.6f}%"
        except Exception:
            rate_str = str(funding_rate)
    else:
        rate_str = "N/A"

    time_until_next = get_time_until_next_funding(next_funding_time)

    print(
        f"[Фандинг {symbol}] "
        f"Ставка: {rate_str} | "
        f"До следующего: {time_until_next}"
    )


# ==============================================
# ПОТОК: периодическое обновление фандинга через REST
# ==============================================
def funding_poll_loop(symbol: str, interval_sec: float = 5.0):
    """Обновляет фандинг каждые interval_sec секунд."""
    while not stop_event.is_set():
        print_funding(symbol)
        stop_event.wait(interval_sec)


# ==============================================
# WEBSOCKET — проверяем наличие фандинговых данных
# ==============================================
def on_bingx_message(ws, message):
    try:
        if isinstance(message, bytes):
            try:
                message = gzip.decompress(message).decode("utf-8")
            except Exception as e:
                print(f"[WS] GZIP ошибка: {e}")
                return

        message = message.strip()
        if not message:
            return

        if message == "Ping":
            ws.send("Pong")
            return

        data = json.loads(message)
        code = data.get("code")
        data_type = data.get("dataType", "")

        # Подтверждение подписки
        if "id" in data:
            if code == 0:
                print(f"[WS] ✅ Подписка на {data_type or SYMBOL} подтверждена")
            else:
                print(f"[WS] ⚠️  Ошибка подписки (код {code}): {data.get('msg')}")
            return

        # Здесь можно добавить обработку других типов данных из WebSocket
        # В BingX WebSocket обычно нет прямой трансляции фандинговых данных через ticker
        # Поэтому используем REST API для получения актуальной информации

    except json.JSONDecodeError as e:
        print(f"[WS] JSON ошибка: {e}")
    except Exception as e:
        print(f"[WS] Ошибка обработки: {e}")


def start_bingx_ws(symbol: str):
    """
    WebSocket подключение для потенциального получения данных в реальном времени.
    В BingX фандинговые данные обычно не транслируются через WebSocket,
    поэтому основной источник данных - REST API с периодическими запросами.
    """
    url = "wss://open-api-swap.bingx.com/swap-market"
    data_type = f"{symbol}@ticker"

    def on_open(ws):
        print(f"[WS] ✅ Подключено к BingX | {symbol}")
        ws.send(json.dumps({"id": "1", "reqType": "sub", "dataType": data_type}))

    def on_error(ws, error):
        print(f"[WS] ⚠️  Ошибка: {error}")

    def on_close(ws, code, msg):
        if not stop_event.is_set():
            print(f"[WS] 🔄 Соединение закрыто, переподключение через {RECONNECT_DELAY}с...")

    while not stop_event.is_set():
        try:
            ws = websocket.WebSocketApp(
                url,
                on_open=on_open,
                on_message=on_bingx_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever()
        except Exception as e:
            print(f"[WS] ❌ Критическая ошибка: {e}")
        if not stop_event.is_set():
            time.sleep(RECONNECT_DELAY)


# ==============================================
# ТОЧКА ВХОДА
# ==============================================
def main():
    print("=" * 60)
    print(f"  📊 BingX Funding Rate | {SYMBOL}")
    print("=" * 60)

    # Проверяем тикер
    print(f"\n[Проверка] Ищем {SYMBOL} на BingX...")
    ok, reason = check_bingx_symbol(SYMBOL)
    if not ok:
        print(f"[Проверка] ❌ Тикер {SYMBOL} не найден: {reason}")
        print("Проверьте название монеты и попробуйте снова.")
        return
    print(f"[Проверка] ✅ Тикер {SYMBOL} найден\n")

    # Сразу выводим текущий фандинг
    print_funding(SYMBOL)

    # Запускаем поток с периодическим обновлением фандинга (каждые 5 сек)
    funding_thread = threading.Thread(
        target=funding_poll_loop,
        args=(SYMBOL, 5.0),
        daemon=True,
        name="FundingPoll",
    )
    funding_thread.start()

    # Запускаем WS (в основном для поддержания соединения, фандинг получаем через REST)
    ws_thread = threading.Thread(
        target=start_bingx_ws,
        args=(SYMBOL,),
        daemon=True,
        name="BingX-WS",
    )
    ws_thread.start()

    print("\nОбновление фандинга каждые 5 секунд...")
    print("Нажмите Ctrl+C для остановки.\n")

    try:
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n⚠️  Остановка...")
        stop_event.set()

    print("👋 Завершено.")


if __name__ == "__main__":
    main()