import os
import json
import time
import threading
import logging
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_FILE = "coins.json"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
app = Flask(__name__)
session = requests.Session()
session.headers.update({"User-Agent": "Khattak-Live-Scanner/1.0"})

DEFAULT_COINS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "BNB": "BNBUSDT",
    "SOL": "SOLUSDT", "XRP": "XRPUSDT", "TRX": "TRXUSDT",
    "TON": "TONUSDT", "SUI": "SUIUSDT", "GRAM": "GRAMUSDT",
    "SHIB": "SHIBUSDT", "DOGE": "DOGEUSDT", "BANANA GUN": "BANANAUSDT",
    "GOLD": "XAUUSDT", "SILVER": "XAGUSDT",
}

def send_message(message):
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        r = session.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True}, timeout=20)
        r.raise_for_status()
        return r.json().get("ok", False)
    except Exception as exc:
        logging.exception("Telegram send error: %s", exc)
        return False

def send_long_message(message):
    limit = 3900
    while message:
        if len(message) <= limit:
            send_message(message)
            break
        cut = message.rfind("\n", 0, limit)
        if cut < 1:
            cut = limit
        send_message(message[:cut])
        message = message[cut:].lstrip()

def load_coins():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else DEFAULT_COINS.copy()
    except (OSError, ValueError):
        return DEFAULT_COINS.copy()

def save_coins(coins):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(coins, f, indent=2)

COINS = load_coins()
BINANCE_SPOT = "https://data-api.binance.vision"
BINANCE_FUTURES = "https://fapi.binance.com"
BYBIT = "https://api.bybit.com"

def get_json(url, params=None):
    r = session.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def resolve_binance_symbol(symbol, futures=False):
    base = BINANCE_FUTURES if futures else BINANCE_SPOT
    endpoint = "/fapi/v1/exchangeInfo" if futures else "/api/v3/exchangeInfo"
    data = get_json(base + endpoint)
    for item in data.get("symbols", []):
        if item.get("symbol") == symbol and item.get("status") == "TRADING":
            return True
    return False

def resolve_bybit_symbol(symbol):
    data = get_json(BYBIT + "/v5/market/instruments-info", {"category": "linear", "symbol": symbol})
    items = data.get("result", {}).get("list", [])
    return bool(items and items[0].get("status") == "Trading")

def get_klines(symbol, interval, limit=250):
    is_metal = symbol in ("XAUUSDT", "XAGUSDT")
    sources = []
    if is_metal:
        sources.append(("Binance Futures", "futures"))
    else:
        sources.append(("Binance Spot", "spot"))
    sources.append(("Bybit Linear", "bybit"))
    for source_name, source_type in sources:
        try:
            if source_type == "spot":
                data = get_json(BINANCE_SPOT + "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
                df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades", "taker_buy", "taker_quote", "ignore"])
                df = df[["time", "open", "high", "low", "close", "volume"]]
            elif source_type == "futures":
                data = get_json(BINANCE_FUTURES + "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
                df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades", "taker_buy", "taker_quote", "ignore"])
                df = df[["time", "open", "high", "low", "close", "volume"]]
            else:
                bybit_interval = {"1h": "60", "4h": "240", "1d": "D"}[interval]
                data = get_json(BYBIT + "/v5/market/kline", {"category": "linear", "symbol": symbol, "interval": bybit_interval, "limit": limit})
                rows = data.get("result", {}).get("list", [])
                if not rows:
                    continue
                rows.reverse()
                df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume", "turnover"])
                df = df[["time", "open", "high", "low", "close", "volume"]]
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["time"] = pd.to_numeric(df["time"], errors="coerce")
            df = df.dropna().reset_index(drop=True)
            if len(df) > 2:
                df = df.iloc[:-1].copy()
            if len(df) >= 100:
                return df, source_name
        except Exception as exc:
            logging.warning("%s failed for %s %s: %s", source_name, symbol, interval, exc)
    raise RuntimeError("No reliable market data source available")

def calculate_indicators(df):
    close = df["close"]; high = df["high"]; low = df["low"]; volume = df["volume"]
    df = df.copy()
    df["SMA20"] = close.rolling(20).mean()
    df["SMA50"] = close.rolling(50).mean()
    df["EMA20"] = close.ewm(span=20, adjust=False).mean()
    df["EMA50"] = close.ewm(span=50, adjust=False).mean()
    df["EMA200"] = close.ewm(span=200, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df["RSI"] = 100 - (100 / (1 + rs))
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_SIGNAL"] = df["MACD"].ewm(span=9, adjust=False).mean()
    mid = close.rolling(20).mean(); std = close.rolling(20).std()
    df["BB_UPPER"] = mid + 2 * std
    df["BB_LOWER"] = mid - 2 * std
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    df["ATR"] = tr.ewm(alpha=1/14, adjust=False).mean()
    direction = np.sign(close.diff()).fillna(0)
    df["OBV"] = (direction * volume).cumsum()
    df["OBV_MA"] = df["OBV"].rolling(20).mean()
    up_move = high.diff(); down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0)
    atr_safe = df["ATR"].replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_safe
    minus_di = 100 * minus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_safe
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["ADX"] = dx.ewm(alpha=1/14, adjust=False).mean()
    df["PLUS_DI"] = plus_di; df["MINUS_DI"] = minus_di
    df["VOL_AVG"] = volume.rolling(20).mean()
    return df

def candle_pattern(df):
    if df is None or len(df) < 5:
        return "Insufficient Candle Data"
    a = df.iloc[-1]; b = df.iloc[-2]; c = df.iloc[-3]
    def parts(x):
        body = abs(x["close"] - x["open"]); rng = max(x["high"] - x["low"], 1e-12)
        upper = x["high"] - max(x["open"], x["close"]); lower = min(x["open"], x["close"]) - x["low"]
        return body, rng, upper, lower
    body, rng, upper, lower = parts(a)
    b_body, b_rng, b_upper, b_lower = parts(b)
    c_body, c_rng, c_upper, c_lower = parts(c)
    bullish = a["close"] > a["open"]; bearish = a["close"] < a["open"]
    b_bullish = b["close"] > b["open"]; b_bearish = b["close"] < b["open"]
    c_bullish = c["close"] > c["open"]; c_bearish = c["close"] < c["open"]
    prev = df.iloc[-5:-2]
    prev_down = prev["close"].iloc[-1] < prev["close"].iloc[0]
    prev_up = prev["close"].iloc[-1] > prev["close"].iloc[0]
    hammer = lower >= max(body*2, rng*0.55) and upper <= rng*0.15 and body/rng <= 0.35 and prev_down
    hanging = lower >= max(body*2, rng*0.55) and upper <= rng*0.15 and body/rng <= 0.35 and prev_up
    inv_hammer = upper >= max(body*2, rng*0.55) and lower <= rng*0.15 and body/rng <= 0.35 and prev_down
    shooting = upper >= max(body*2, rng*0.55) and lower <= rng*0.15 and body/rng <= 0.35 and prev_up
    doji = body/rng <= 0.10
    grave = doji and upper/rng >= 0.65 and lower/rng <= 0.10
    dragon = doji and lower/rng >= 0.65 and upper/rng <= 0.10
    spin = body/rng <= 0.35 and upper > body and lower > body and not doji
    bull_eng = b_bearish and bullish and a["open"] <= b["close"] and a["close"] >= b["open"] and body > b_body
    bear_eng = b_bullish and bearish and a["open"] >= b["close"] and a["close"] <= b["open"] and body > b_body
    bull_harami = b_bearish and bullish and a["open"] > b["close"] and a["close"] < b["open"] and body < b_body
    bear_harami = b_bullish and bearish and a["open"] < b["close"] and a["close"] > b["open"] and body < b_body
    morning = c_bearish and c_body/c_rng >= 0.50 and b_body/b_rng <= 0.30 and bullish and a["close"] > (c["open"]+c["close"])/2
    evening = c_bullish and c_body/c_rng >= 0.50 and b_body/b_rng <= 0.30 and bearish and a["close"] < (c["open"]+c["close"])/2
    white_sold = len(df) >= 3 and all(df["close"].iloc[-i] > df["open"].iloc[-i] for i in [1,2,3]) and df["close"].iloc[-1] > df["close"].iloc[-2] > df["close"].iloc[-3]
    black_crows = len(df) >= 3 and all(df["close"].iloc[-i] < df["open"].iloc[-i] for i in [1,2,3]) and df["close"].iloc[-1] < df["close"].iloc[-2] < df["close"].iloc[-3]
    piercing = b_bearish and bullish and a["open"] < b["close"] and a["close"] > (b["open"]+b["close"])/2 and a["close"] < b["open"]
    dark = b_bullish and bearish and a["open"] > b["close"] and a["close"] < (b["open"]+b["close"])/2 and a["close"] > b["open"]
    tweezer_top = b_bullish and bearish and abs(a["high"]-b["high"]) <= b_rng*0.10 and prev_up
    tweezer_bot = b_bearish and bullish and abs(a["low"]-b["low"]) <= b_rng*0.10 and prev_down
    p = []
    if hammer: p.append("Hammer - Bullish")
    if hanging: p.append("Hanging Man - Bearish")
    if inv_hammer: p.append("Inverted Hammer - Bullish")
    if shooting: p.append("Shooting Star - Bearish")
    if grave: p.append("Gravestone Doji - Bearish")
    if dragon: p.append("Dragonfly Doji - Bullish")
    if doji and not grave and not dragon: p.append("Doji - Neutral")
    if spin: p.append("Spinning Top - Neutral")
    if bull_eng: p.append("Bullish Engulfing")
    if bear_eng: p.append("Bearish Engulfing")
    if bull_harami: p.append("Bullish Harami")
    if bear_harami: p.append("Bearish Harami")
    if morning: p.append("Morning Star - Bullish")
    if evening: p.append("Evening Star - Bearish")
    if white_sold: p.append("Three White Soldiers - Bullish")
    if black_crows: p.append("Three Black Crows - Bearish")
    if piercing: p.append("Piercing Line - Bullish")
    if dark: p.append("Dark Cloud Cover - Bearish")
    if tweezer_top: p.append("Tweezer Top - Bearish")
    if tweezer_bot: p.append("Tweezer Bottom - Bullish")
    return " | ".join(p) if p else "No Clear Pattern"

def trend_of(row):
    if row["close"] > row["EMA50"] > row["EMA200"] and row["EMA20"] > row["EMA50"]:
        return "UPTREND"
    if row["close"] < row["EMA50"] < row["EMA200"] and row["EMA20"] < row["EMA50"]:
        return "DOWNTREND"
    return "SIDEWAYS"

def support_resistance(df):
    recent = df.tail(30)
    return float(recent["low"].min()), float(recent["high"].max())

def market_structure(df):
    recent = df.tail(30); prev = df.iloc[-2]
    prev_high = float(recent["high"].iloc[:-1].max()); prev_low = float(recent["low"].iloc[:-1].min())
    cur = df.iloc[-1]
    if cur["close"] > prev_high: return "Bullish BOS"
    if cur["close"] < prev_low: return "Bearish BOS"
    if cur["close"] > prev["high"]: return "Possible Bullish CHoCH"
    if cur["close"] < prev["low"]: return "Possible Bearish CHoCH"
    return "No confirmed break"

def analyze_coin(name, symbol):
    try:
        frames = {}; sources = []
        for tf in ["1d", "4h", "1h"]:
            raw, source = get_klines(symbol, tf)
            frames[tf] = calculate_indicators(raw); sources.append(source)
        daily = frames["1d"].iloc[-1]; four = frames["4h"].iloc[-1]; hourly = frames["1h"].iloc[-1]
        t1d = trend_of(daily); t4h = trend_of(four); t1h = trend_of(hourly)
        price = float(hourly["close"]); atr = float(hourly["ATR"]); rsi = float(hourly["RSI"]); adx = float(hourly["ADX"])
        if not all(np.isfinite(x) for x in [price, atr, rsi, adx]): raise RuntimeError("Indicator data incomplete")
        support, resistance = support_resistance(frames["4h"])
        pattern = candle_pattern(frames["1h"]); structure = market_structure(frames["1h"])
        signal = "WAIT"; reason = "Timeframes are not aligned"
        if t1d == "UPTREND" and t4h == "UPTREND" and t1h == "UPTREND" and adx >= 20 and 30 < rsi < 70:
            signal = "BUY"; reason = "1D, 4H and 1H trends aligned upward"
        elif t1d == "DOWNTREND" and t4h == "DOWNTREND" and t1h == "DOWNTREND" and adx >= 20 and 30 < rsi < 70:
            signal = "SELL"; reason = "1D, 4H and 1H trends aligned downward"
        if signal == "BUY": entry = price; sl = entry - (1.5*atr); tp = entry + (3.0*atr)
        elif signal == "SELL": entry = price; sl = entry + (1.5*atr); tp = entry - (3.0*atr)
        else: entry = price; sl = None; tp = None
        macd_state = "Bullish" if hourly["MACD"] > hourly["MACD_SIGNAL"] else "Bearish"
        obv_state = "Rising" if hourly["OBV"] > hourly["OBV_MA"] else "Falling"
        vol_state = "Above Average" if hourly["volume"] > hourly["VOL_AVG"] else "Below Average"
        boll = "Upper Band" if price >= hourly["BB_UPPER"] else "Lower Band" if price <= hourly["BB_LOWER"] else "Inside Bands"
        source = sources[0] if len(set(sources)) == 1 else "Mixed sources"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return {"name": name, "symbol": symbol, "signal": signal, "price": price, "entry": entry, "sl": sl, "tp": tp, "trend_1d": t1d, "trend_4h": t4h, "trend_1h": t1h, "rsi": rsi, "adx": adx, "macd": macd_state, "obv": obv_state, "volume": vol_state, "bollinger": boll, "pattern": pattern, "structure": structure, "support": support, "resistance": resistance, "atr": atr, "reason": reason, "source": source, "timestamp": ts}
    except Exception as exc:
        logging.exception("Analysis failed for %s", symbol)
        return {"name": name, "symbol": symbol, "signal": "WAIT", "error": str(exc)}

def format_result(result):
    signal = result["signal"]
    dot = {"BUY": "🟢", "SELL": "🔴", "WAIT": "🟡"}.get(signal, "🟡")
    if result.get("error"):
        return f"🟡 WAIT — {result['name']} ({result['symbol']})\nData unavailable: {result['error']}"
    def price(v):
        return "Not available" if v is None else f"{v:.8g}"
    return (f"{dot} {signal} — {result['name']} ({result['symbol']})\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Live Price: {price(result['price'])}\n"
        f"Entry: {price(result['entry'])}\n"
        f"SL: {price(result['sl'])}\n"
        f"TP: {price(result['tp'])}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"1D Trend: {result['trend_1d']}\n"
        f"4H Trend: {result['trend_4h']}\n"
        f"1H Trend: {result['trend_1h']}\n"
        f"RSI: {result['rsi']:.2f}\n"
        f"ADX: {result['adx']:.2f}\n"
        f"MACD: {result['macd']}\n"
        f"OBV: {result['obv']}\n"
        f"Volume: {result['volume']}\n"
        f"Bollinger: {result['bollinger']}\n"
        f"Candle: {result['pattern']}\n"
        f"Structure: {result['structure']}\n"
        f"Support: {price(result['support'])}\n"
        f"Resistance: {price(result['resistance'])}\n"
        f"Data Source: {result['source']}\n"
        f"Reason: {result['reason']}\n"
        f"Updated: {result['timestamp']}\n"
        f"Preliminary signal only — manual 36-point review required.")

def scan_all():
    results = []
    for name, symbol in list(COINS.items()):
        results.append(format_result(analyze_coin(name, symbol)))
        time.sleep(0.15)
    header = "📊 KHATTAK LIVE MARKET SCANNER\nHourly market report\nNo automatic trading or order execution.\n\n"
    send_long_message(header + "\n\n".join(results))

def find_symbol(ticker):
    ticker = ticker.upper().strip()
    if ticker in ("GOLD", "SILVER"):
        return "XAUUSDT" if ticker == "GOLD" else "XAGUSDT"
    symbol = ticker if ticker.endswith("USDT") else ticker + "USDT"
    try:
        if resolve_binance_symbol(symbol, futures=False): return symbol
    except Exception: pass
    try:
        if resolve_binance_symbol(symbol, futures=True): return symbol
    except Exception: pass
    try:
        if resolve_bybit_symbol(symbol): return symbol
    except Exception: pass
    return None

def add_coin(ticker):
    ticker = ticker.upper().strip()
    if not ticker or len(ticker) > 30: return "Invalid coin name."
    if ticker in COINS: return f"{ticker} is already in the list."
    symbol = find_symbol(ticker)
    if not symbol: return f"Could not verify a live USDT market for {ticker}. Coin was not added."
    try: get_klines(symbol, "1h", 120)
    except Exception: return f"Live chart data unavailable for {ticker}. Not added."
    COINS[ticker] = symbol; save_coins(COINS)
    return f"✅ {ticker} added successfully as {symbol}."

def remove_coin(ticker):
    ticker = ticker.upper().strip()
    if ticker not in COINS: return f"{ticker} is not in the list."
    del COINS[ticker]; save_coins(COINS)
    return f"✅ {ticker} removed."

def handle_command(text):
    parts = text.strip().split()
    if not parts: return "Use /help to see commands."
    cmd = parts[0].split("@")[0].lower()
    if cmd == "/start": return "✅ KHATTAK LIVE SCANNER is online.\nHourly scanner is configured.\nUse /help for commands."
    if cmd == "/help": return "KHATTAK LIVE SCANNER COMMANDS\n\n/start — Bot status\n/scan — Scan all coins now\n/check BTC — Scan one coin\n/add PEPE — Add a coin\n/remove PEPE — Remove a coin\n/list — Show coin list\n/help — Help\n\nBot does not execute trades."
    if cmd == "/list": return "📋 COIN LIST\n" + "\n".join(f"{n}: {s}" for n, s in COINS.items())
    if cmd == "/add":
        if len(parts) < 2: return "Usage: /add PEPE"
        return add_coin(parts[1])
    if cmd == "/remove":
        if len(parts) < 2: return "Usage: /remove PEPE"
        return remove_coin(parts[1])
    if cmd == "/scan":
        threading.Thread(target=scan_all, daemon=True).start()
        return "🔄 Full market scan started. Results will arrive shortly."
    if cmd == "/check":
        if len(parts) < 2: return "Usage: /check BTC"
        ticker = parts[1].upper().strip()
        symbol = COINS.get(ticker) or find_symbol(ticker)
        if not symbol: return f"Could not verify a live USDT market for {ticker}."
        return format_result(analyze_coin(ticker, symbol))
    return "Unknown command. Use /help to see commands."

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "ok", "service": "KHATTAK LIVE SCANNER"}), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True, silent=True) or {}
        msg = data.get("message") or {}
        text = msg.get("text", "")
        if text:
            reply = handle_command(text)
            send_message(reply)
        return jsonify({"ok": True}), 200
    except Exception as exc:
        logging.exception("Webhook error: %s", exc)
        return jsonify({"ok": False}), 200

scheduler = BackgroundScheduler()
scheduler.add_job(scan_all, "interval", hours=1)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
