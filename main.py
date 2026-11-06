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

# ============================================================
# KHATTAK LIVE SCANNER - FINAL
# Main Trend: 1D + 4H
# Entry Timing: 1H only
# Crypto: Binance Spot -> Bybit Linear
# Gold/Silver: Twelve Data (XAU/USD, XAG/USD) - ON, lock nahi hai
# Analysis only: NO automatic trading
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PORT = int(os.getenv("PORT", "10000"))
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_FILE = "coins.json"
BINANCE_SPOT = "https://data-api.binance.vision"
BYBIT = "https://api.bybit.com"
TWELVE_DATA = "https://api.twelvedata.com"
TIMEFRAMES = {"1d": "1day", "4h": "4h", "1h": "1h"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
app = Flask(__name__)
session = requests.Session()
session.headers.update({"User-Agent": "Khattak-Live-Scanner/3.0"})

DEFAULT_COINS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "BNB": "BNBUSDT", "SOL": "SOLUSDT"}
METALS = {"GOLD": {"symbol": "XAU/USD", "provider": "Twelve Data"}, "SILVER": {"symbol": "XAG/USD", "provider": "Twelve Data"}}
BLOCKED = {"XAUUSDT", "XAGUSDT", "XAUUSD", "XAGUSD"}

def send_message(message, chat_id=None):
    target = chat_id or CHAT_ID
    if not BOT_TOKEN or not target:
        return False
    try:
        r = session.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": target, "text": message, "disable_web_page_preview": True}, timeout=20)
        r.raise_for_status()
        return r.json().get("ok", False)
    except Exception:
        logging.exception("Telegram send error")
        return False

def send_long_message(message, chat_id=None):
    limit = 3900
    while message:
        if len(message) <= limit:
            send_message(message, chat_id)
            break
        cut = message.rfind("\n", 0, limit)
        if cut < 1:
            cut = limit
        send_message(message[:cut], chat_id)
        message = message[cut:].lstrip()

def load_coins():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return DEFAULT_COINS.copy()
        cleaned = {k: v for k, v in data.items() if k not in METALS and v not in BLOCKED}
        if cleaned != data:
            save_coins(cleaned)
        return cleaned if cleaned else DEFAULT_COINS.copy()
    except Exception:
        return DEFAULT_COINS.copy()

def save_coins(coins):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(coins, f, indent=2)

COINS = load_coins()

def get_json(url, params=None):
    r = session.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(data.get("message", "Provider error"))
    return data

def resolve_binance_symbol(symbol):
    data = get_json(BINANCE_SPOT + "/api/v3/exchangeInfo")
    return any(x.get("symbol") == symbol and x.get("status") == "TRADING" for x in data.get("symbols", []))

def resolve_bybit_symbol(symbol):
    data = get_json(BYBIT + "/v5/market/instruments-info", {"category": "linear", "symbol": symbol})
    items = data.get("result", {}).get("list", [])
    return bool(items and items[0].get("status") == "Trading")

def get_live_price(symbol):
    try:
        data = get_json(BINANCE_SPOT + "/api/v3/ticker/price", {"symbol": symbol})
        return float(data["price"]), "Binance Spot"
    except Exception:
        pass
    try:
        data = get_json(BYBIT + "/v5/market/tickers", {"category": "linear", "symbol": symbol})
        items = data.get("result", {}).get("list", [])
        if items:
            return float(items[0]["lastPrice"]), "Bybit Linear"
    except Exception:
        pass
    return None, "Live ticker unavailable"

def twelve_data_request(endpoint, params):
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError("TWELVE_DATA_API_KEY missing in Render.")
    p = dict(params); p["apikey"] = TWELVE_DATA_API_KEY
    return get_json(f"{TWELVE_DATA}/{endpoint}", p)

def get_metal_klines(metal_name, interval, outputsize=250):
    symbol = METALS[metal_name]["symbol"]
    td_interval = TIMEFRAMES[interval]
    data = twelve_data_request("time_series", {"symbol": symbol, "interval": td_interval, "outputsize": outputsize, "timezone": "UTC", "format": "JSON"})
    if not data.get("values"):
        raise RuntimeError(f"Twelve Data {symbol}: {data.get('message','No values')}")
    df = pd.DataFrame(data["values"])
    df = df.rename(columns={"datetime": "time"})
    if "volume" not in df.columns:
        df["volume"] = np.nan
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
    df = df.dropna(subset=["time", "open", "high", "low", "close"]).sort_values("time").reset_index(drop=True)
    if len(df) > 2:
        df = df.iloc[:-1].copy()
    if len(df) < 100:
        raise RuntimeError(f"Insufficient {metal_name} {interval}: {len(df)}")
    return df.reset_index(drop=True), "Twelve Data"

def get_metal_live_price(metal_name):
    data = twelve_data_request("price", {"symbol": METALS[metal_name]["symbol"]})
    if "price" not in data:
        raise RuntimeError(data.get("message", "Live price unavailable"))
    return float(data["price"]), "Twelve Data live price"

def get_klines(symbol, interval, limit=250):
    if symbol in BLOCKED:
        raise RuntimeError("This asset is excluded.")
    for source_name, source_type in [("Binance Spot", "binance"), ("Bybit Linear", "bybit")]:
        try:
            if source_type == "binance":
                data = get_json(BINANCE_SPOT + "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
                df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades", "taker_buy", "taker_quote", "ignore"])[["time", "open", "high", "low", "close", "volume"]]
            else:
                bi = {"1h": "60", "4h": "240", "1d": "D"}[interval]
                data = get_json(BYBIT + "/v5/market/kline", {"category": "linear", "symbol": symbol, "interval": bi, "limit": limit})
                rows = data.get("result", {}).get("list", [])
                if not rows:
                    continue
                rows.reverse()
                df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume", "turnover"])[["time", "open", "high", "low", "close", "volume"]]
            for c in ["time", "open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df.dropna().reset_index(drop=True)
            if len(df) > 2:
                df = df.iloc[:-1].copy()
            if len(df) >= 100:
                return df.reset_index(drop=True), source_name
        except Exception as e:
            logging.warning("%s failed %s %s: %s", source_name, symbol, interval, e)
    raise RuntimeError("No reliable market data source.")

def calculate_indicators(df):
    df = df.copy()
    close = df["close"]; high = df["high"]; low = df["low"]; volume = df["volume"]
    df["SMA20"] = close.rolling(20).mean(); df["SMA50"] = close.rolling(50).mean()
    df["EMA20"] = close.ewm(span=20, adjust=False).mean(); df["EMA50"] = close.ewm(span=50, adjust=False).mean(); df["EMA200"] = close.ewm(span=200, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df["RSI"] = 100 - (100 / (1 + rs))
    df.loc[(loss == 0) & (gain > 0), "RSI"] = 100
    df.loc[(loss == 0) & (gain == 0), "RSI"] = 50
    ema12 = close.ewm(span=12, adjust=False).mean(); ema26 = close.ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26; df["MACD_SIGNAL"] = df["MACD"].ewm(span=9, adjust=False).mean()
    mid = close.rolling(20).mean(); std = close.rolling(20).std()
    df["BB_UPPER"] = mid + 2 * std; df["BB_LOWER"] = mid - 2 * std
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    df["ATR"] = tr.ewm(alpha=1/14, adjust=False).mean()
    if volume.notna().sum() >= 25 and volume.fillna(0).sum() > 0:
        df["OBV"] = (np.sign(close.diff()).fillna(0) * volume.fillna(0)).cumsum(); df["OBV_MA"] = df["OBV"].rolling(20).mean()
    else:
        df["OBV"] = np.nan; df["OBV_MA"] = np.nan
    up = high.diff(); down = -low.diff()
    pdm = up.where((up > down) & (up > 0), 0); mdm = down.where((down > up) & (down > 0), 0)
    atr_safe = df["ATR"].replace(0, np.nan)
    pdi = 100 * pdm.ewm(alpha=1/14, adjust=False).mean() / atr_safe
    mdi = 100 * mdm.ewm(alpha=1/14, adjust=False).mean() / atr_safe
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    df["ADX"] = dx.ewm(alpha=1/14, adjust=False).mean()
    df["PLUS_DI"] = pdi; df["MINUS_DI"] = mdi
    df["VOL_AVG"] = volume.rolling(20).mean() if volume.notna().sum() >= 20 else np.nan
    return df

def candle_pattern(df):
    if df is None or len(df) < 5:
        return "Insufficient Candle Data"
    a = df.iloc[-1]; b = df.iloc[-2]; c = df.iloc[-3]
    def parts(row):
        body = abs(row["close"] - row["open"]); rng = max(row["high"] - row["low"], 1e-12)
        upper = row["high"] - max(row["open"], row["close"]); lower = min(row["open"], row["close"]) - row["low"]
        return body, rng, upper, lower
    body, rng, upper, lower = parts(a); b_body, b_rng, b_upper, b_lower = parts(b); c_body, c_rng, c_upper, c_lower = parts(c)
    bullish = a["close"] > a["open"]; bearish = a["close"] < a["open"]
    b_bullish = b["close"] > b["open"]; b_bearish = b["close"] < b["open"]
    c_bullish = c["close"] > c["open"]; c_bearish = c["close"] < c["open"]
    prev = df.iloc[-5:-2]
    prev_down = prev["close"].iloc[-1] < prev["close"].iloc[0]; prev_up = prev["close"].iloc[-1] > prev["close"].iloc[0]
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
    white_sold = all(df["close"].iloc[-i] > df["open"].iloc[-i] for i in [1,2,3]) and (df["close"].iloc[-1] > df["close"].iloc[-2] > df["close"].iloc[-3])
    black_crows = all(df["close"].iloc[-i] < df["open"].iloc[-i] for i in [1,2,3]) and (df["close"].iloc[-1] < df["close"].iloc[-2] < df["close"].iloc[-3])
    piercing = b_bearish and bullish and a["open"] < b["close"] and a["close"] > (b["open"]+b["close"])/2 and a["close"] < b["open"]
    dark = b_bullish and bearish and a["open"] > b["close"] and a["close"] < (b["open"]+b["close"])/2 and a["close"] > b["open"]
    tweezer_top = b_bullish and bearish and abs(a["high"]-b["high"]) <= b_rng*0.10 and prev_up
    tweezer_bot = b_bearish and bullish and abs(a["low"]-b["low"]) <= b_rng*0.10 and prev_down
    pats = []
    checks = [(hammer,"Hammer - Bullish"),(hanging,"Hanging Man - Bearish"),(inv_hammer,"Inverted Hammer - Bullish"),(shooting,"Shooting Star - Bearish"),(grave,"Gravestone Doji - Bearish"),(dragon,"Dragonfly Doji - Bullish"),(doji and not grave and not dragon,"Doji - Neutral"),(spin,"Spinning Top - Neutral"),(bull_eng,"Bullish Engulfing"),(bear_eng,"Bearish Engulfing"),(bull_harami,"Bullish Harami"),(bear_harami,"Bearish Harami"),(morning,"Morning Star - Bullish"),(evening,"Evening Star - Bearish"),(white_sold,"Three White Soldiers - Bullish"),(black_crows,"Three Black Crows - Bearish"),(piercing,"Piercing Line - Bullish"),(dark,"Dark Cloud Cover - Bearish"),(tweezer_top,"Tweezer Top - Bearish"),(tweezer_bot,"Tweezer Bottom - Bullish")]
    for cond, label in checks:
        if cond: pats.append(label)
    return " | ".join(pats) if pats else "No Clear Pattern"

def trend_of(row):
    if row["close"] > row["EMA50"] > row["EMA200"] and row["EMA20"] > row["EMA50"]:
        return "UPTREND"
    if row["close"] < row["EMA50"] < row["EMA200"] and row["EMA20"] < row["EMA50"]:
        return "DOWNTREND"
    return "SIDEWAYS"

def support_resistance(df):
    r = df.tail(30)
    return float(r["low"].min()), float(r["high"].max())

def market_structure(df):
    r = df.tail(30); prev = df.iloc[-2]; cur = df.iloc[-1]
    ph = float(r["high"].iloc[:-1].max()); pl = float(r["low"].iloc[:-1].min())
    if cur["close"] > ph: return "Bullish BOS"
    if cur["close"] < pl: return "Bearish BOS"
    if cur["close"] > prev["high"]: return "Possible Bullish CHoCH"
    if cur["close"] < prev["low"]: return "Possible Bearish CHoCH"
    return "No confirmed break"

def build_retest_plan(direction, hourly, support, resistance):
    price = float(hourly["close"]); ema20 = float(hourly["EMA20"]); atr = float(hourly["ATR"])
    if direction == "BUY":
        entry = ema20; sl = min(support, entry - (1.5*atr)) - (0.20*atr); risk = entry - sl; tp = entry + (2*risk)
        return {"direction": "BUY", "entry": entry, "sl": sl, "tp": tp, "zone_low": entry - (0.15*atr), "zone_high": entry + (0.15*atr)}
    if direction == "SELL":
        entry = ema20; sl = max(resistance, entry + (1.5*atr)) + (0.20*atr); risk = sl - entry; tp = entry - (2*risk)
        return {"direction": "SELL", "entry": entry, "sl": sl, "tp": tp, "zone_low": entry - (0.15*atr), "zone_high": entry + (0.15*atr)}
    return {"direction": "NONE", "entry": price, "sl": None, "tp": None, "zone_low": None, "zone_high": None}

# ============ YAHAN MAIN CHANGE HAI ============
# Main Trend = 1D + 4H, 1H sirf Entry ke liye
def analyze_coin(name, symbol=None):
    try:
        is_metal = name in METALS
        display_symbol = METALS[name]["symbol"] if is_metal else symbol
        frames = {}; sources = []
        for tf in ["1d", "4h", "1h"]:
            if is_metal:
                raw, src = get_metal_klines(name, tf)
            else:
                raw, src = get_klines(symbol, tf)
            frames[tf] = calculate_indicators(raw); sources.append(src)
        daily = frames["1d"].iloc[-1]; four = frames["4h"].iloc[-1]; hourly = frames["1h"].iloc[-1]
        t1d = trend_of(daily); t4h = trend_of(four); t1h = trend_of(hourly)
        candle_price = float(hourly["close"])
        atr = float(hourly["ATR"]); rsi = float(hourly["RSI"]); adx = float(hourly["ADX"])
        if not all(np.isfinite(v) for v in [candle_price, atr, rsi, adx]):
            raise RuntimeError("Indicator data incomplete")
        support, resistance = support_resistance(frames["4h"])
        pattern = candle_pattern(frames["1h"]); structure = market_structure(frames["1h"])
        if is_metal:
            live_price, live_source = get_metal_live_price(name)
        else:
            live_price, live_source = get_live_price(symbol)
            if live_price is None:
                live_price = candle_price

        # --- MAIN TREND RULE: Sirf 1D + 4H ---
        signal = "WAIT"; reason_parts = []; direction = "NONE"
        if t1d == "UPTREND" and t4h == "UPTREND":
            direction = "BUY"
        elif t1d == "DOWNTREND" and t4h == "DOWNTREND":
            direction = "SELL"
        else:
            reason_parts.append("1D and 4H trends are not aligned - Main Trend WAIT")

        if adx < 18:
            reason_parts.append("ADX below 18")
        if not (25 < rsi < 75):
            reason_parts.append("RSI outside 25-75")

        # 1H ko signal rokne ke liye use NAHI kar rahe, sirf entry batane ke liye
        entry_note = ""
        if direction != "NONE":
            if t1h == direction.replace("BUY", "UPTREND").replace("SELL", "DOWNTREND"):
                entry_note = "1H bhi aligned hai - Entry abhi le sakte hain"
            else:
                entry_note = f"1H {t1h} hai - Retest Zone par Entry ka wait karein, Main Trend {direction} hai"

        if direction != "NONE" and adx >= 18 and 25 < rsi < 75:
            signal = direction
            reason = f"Main Trend 1D+4H {direction}. {entry_note}"
        else:
            reason = "; ".join(reason_parts) or "Conditions not confirmed"
            if direction != "NONE":
                reason += f" | Main Trend {direction} hai, lekin ADX/RSI filter se WAIT. {entry_note}"

        plan = build_retest_plan(direction if direction != "NONE" else "NONE", hourly, support, resistance)
        entry = plan["entry"]; sl = plan["sl"]; tp = plan["tp"]
        macd_state = "Bullish" if hourly["MACD"] > hourly["MACD_SIGNAL"] else "Bearish"
        obv_state = "Rising" if pd.notna(hourly["OBV"]) and pd.notna(hourly["OBV_MA"]) and hourly["OBV"] > hourly["OBV_MA"] else "Falling" if pd.notna(hourly["OBV"]) and pd.notna(hourly["OBV_MA"]) else "Unavailable"
        vol_state = "Above Average" if pd.notna(hourly["VOL_AVG"]) and hourly["volume"] > hourly["VOL_AVG"] else "Below Average" if pd.notna(hourly["VOL_AVG"]) else "Unavailable"
        boll = "Upper Band" if live_price >= hourly["BB_UPPER"] else "Lower Band" if live_price <= hourly["BB_LOWER"] else "Inside Bands"
        source = sources[0] if len(set(sources)) == 1 else "Mixed sources"
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return {"name": name, "symbol": display_symbol, "signal": signal, "price": live_price, "candle_price": candle_price, "live_source": live_source, "entry": entry, "sl": sl, "tp": tp, "retest_direction": plan["direction"], "retest_low": plan["zone_low"], "retest_high": plan["zone_high"], "trend_1d": t1d, "trend_4h": t4h, "trend_1h": t1h, "rsi": rsi, "adx": adx, "macd": macd_state, "obv": obv_state, "volume": vol_state, "bollinger": boll, "pattern": pattern, "structure": structure, "support": support, "resistance": resistance, "atr": atr, "reason": reason, "entry_note": entry_note, "source": source, "timestamp": timestamp}
    except Exception as exc:
        logging.exception("Analysis failed for %s", name)
        return {"name": name, "symbol": symbol or METALS.get(name, {}).get("symbol", "N/A"), "signal": "WAIT", "error": str(exc)}

def fmt(v):
    return "Not available" if v is None else f"{v:.8g}"

def format_result(r):
    signal = r["signal"]
    dot = {"BUY": "🟢", "SELL": "🔴", "WAIT": "🟡"}.get(signal, "🟡")
    if r.get("error"):
        return f"🟡 WAIT — {r['name']} ({r['symbol']})\nData unavailable: {r['error']}"
    label = "Watch-only Retest Plan" if signal == "WAIT" else "Preliminary Signal Levels"
    return (f"{dot} {signal} — {r['name']} ({r['symbol']})\n━━━━━━━━━━━━━━━━━━\nLive Price: {fmt(r['price'])}\nLast Closed 1H Candle: {fmt(r['candle_price'])}\n━━━━━━━━━━━━━━━━━━\n{label}\nRetest Direction: {r['retest_direction']}\nRetest Zone: {fmt(r['retest_low'])} - {fmt(r['retest_high'])}\nEntry Reference: {fmt(r['entry'])}\nSL Reference: {fmt(r['sl'])}\nTP Reference: {fmt(r['tp'])}\nEntry Note: {r.get('entry_note','')}\n━━━━━━━━━━━━━━━━━━\n1D Trend: {r['trend_1d']} (Main)\n4H Trend: {r['trend_4h']} (Main)\n1H Trend: {r['trend_1h']} (Entry only)\nRSI: {r['rsi']:.2f}\nADX: {r['adx']:.2f}\nMACD: {r['macd']}\nOBV: {r['obv']}\nVolume: {r['volume']}\nBollinger: {r['bollinger']}\nCandle: {r['pattern']}\nStructure: {r['structure']}\nSupport: {fmt(r['support'])}\nResistance: {fmt(r['resistance'])}\nData Source: {r['source']}\nLive Price Source: {r['live_source']}\nReason: {r['reason']}\nUpdated: {r['timestamp']}\nPreliminary scanner only — manual review required.\nNo automatic order execution.")

def scan_all(chat_id=None):
    results = []
    for name, symbol in list(COINS.items()):
        if name in METALS or symbol in BLOCKED:
            continue
        results.append(format_result(analyze_coin(name, symbol)))
        time.sleep(0.15)
    for name in METALS.keys():
        results.append(format_result(analyze_coin(name, None)))
        time.sleep(0.15)
    header = "📊 KHATTAK LIVE MARKET SCANNER\nHourly market report\nCrypto + Gold + Silver — no automatic trading.\n\n"
    if not results:
        results.append("No coins available.")
    send_long_message(header + "\n\n".join(results), chat_id=chat_id)

def find_symbol(ticker):
    ticker = ticker.upper().strip()
    if ticker in METALS:
        return None
    if ticker in BLOCKED:
        return None
    symbol = ticker if ticker.endswith("USDT") else ticker + "USDT"
    if symbol in BLOCKED:
        return None
    try:
        if resolve_binance_symbol(symbol):
            return symbol
    except Exception:
        pass
    try:
        if resolve_bybit_symbol(symbol):
            return symbol
    except Exception:
        pass
    return None

def add_coin(ticker):
    ticker = ticker.upper().strip()
    if not ticker or len(ticker) > 30:
        return "Invalid coin name."
    if ticker in METALS:
        return f"{ticker} already included as metal."
    if ticker in BLOCKED:
        return "Use GOLD / SILVER for metals."
    if ticker in COINS:
        return f"{ticker} is already in the list."
    symbol = find_symbol(ticker)
    if not symbol:
        return f"Could not verify a live USDT market for {ticker}."
    try:
        get_klines(symbol, "1h", 120)
    except Exception:
        return f"Live chart data unavailable for {ticker}."
    COINS[ticker] = symbol; save_coins(COINS)
    return f"✅ {ticker} added successfully as {symbol}."

def remove_coin(ticker):
    ticker = ticker.upper().strip()
    if ticker in METALS:
        return "GOLD / SILVER built-in hain. Hatane ke liye METALS list se hatayen."
    for name, symbol in list(COINS.items()):
        if name == ticker or symbol == ticker:
            del COINS[name]; save_coins(COINS); return f"✅ {name} removed."
    return f"{ticker} is not in the list."

def handle_command(text, chat_id=None):
    parts = text.strip().split()
    if not parts:
        return "Use /help to see commands."
    cmd = parts[0].split("@")[0].lower()
    if cmd == "/start":
        return "✅ KHATTAK LIVE SCANNER is online.\nMain Trend: 1D + 4H | Entry: 1H\nCrypto + Gold + Silver configured.\nUse /help for commands."
    if cmd == "/help":
        return "KHATTAK LIVE SCANNER COMMANDS\n\n/start — Bot status\n/scan — Scan all coins now\n/scan BTC — Scan one coin\n/scan GOLD — Scan Gold\n/scan SILVER — Scan Silver\n/check BTC — Scan one coin\n/add PEPE — Add a coin\n/remove PEPE — Remove a coin\n/list — Show coin list\n/help — Help\n\nMain Trend 1D+4H, Entry 1H. Bot does not execute trades."
    if cmd == "/list":
        lines = ["📋 COIN LIST"] + [f"{k}: {v}" for k, v in COINS.items()] + [f"{k}: {v['symbol']} ({v['provider']})" for k, v in METALS.items()]
        return "\n".join(lines)
    if cmd == "/add":
        if len(parts) < 2: return "Usage: /add PEPE"
        return add_coin(parts[1])
    if cmd == "/remove":
        if len(parts) < 2: return "Usage: /remove PEPE"
        return remove_coin(parts[1])
    if cmd in ("/scan", "/check"):
        if cmd == "/check" and len(parts) < 2: return "Usage: /check BTC"
        if len(parts) >= 2:
            ticker = parts[1].upper().strip()
            if ticker in BLOCKED:
                return "Fake Gold/Silver blocked. Use GOLD / SILVER."
            def do_single():
                target = chat_id or CHAT_ID
                if ticker in METALS:
                    result = analyze_coin(ticker, None)
                else:
                    symbol = COINS.get(ticker) or find_symbol(ticker)
                    if not symbol:
                        send_message(f"Could not verify a live USDT market for {ticker}.", chat_id=target); return
                    result = analyze_coin(ticker, symbol)
                send_message(format_result(result), chat_id=target)
            threading.Thread(target=do_single, daemon=True).start()
            return f"🔄 Scanning {ticker}... Result will arrive shortly."
        threading.Thread(target=lambda: scan_all(chat_id), daemon=True).start()
        return "🔄 Full market scan started."
    return "Unknown command. Use /help."

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "ok", "service": "KHATTAK LIVE SCANNER", "crypto_count": len(COINS), "metals": list(METALS.keys())}), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True, silent=True) or {}
        msg = data.get("message") or {}; txt = msg.get("text", ""); chat = msg.get("chat") or {}; cid = chat.get("id")
        if txt and cid:
            reply = handle_command(txt, cid)
            send_message(reply, chat_id=cid)
        return jsonify({"ok": True}), 200
    except Exception:
        logging.exception("Webhook error")
        return jsonify({"ok": True}), 200

scheduler = BackgroundScheduler()
scheduler.add_job(scan_all, "interval", hours=1, id="hourly_market_scan", replace_existing=True, max_instances=1)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
