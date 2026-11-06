
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
# KHATTAK LIVE SCANNER - V4 (Retest + Bounce)
# Main Trend: 1D + 4H | Entry: 1H retest + bounce
# Crypto: Binance Spot -> Bybit Linear
# Gold/Silver: OANDA -> FOREX fallback
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

# Retest settings
ZONE_ATR = 0.5
SL_BUFFER_ATR = 0.3
MAX_RISK_ATR = 3.0
RR = 2.0
MAX_MOVE_ATR = 1.0
TD_MIN_GAP = 8.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
app = Flask(__name__)
session = requests.Session()
session.headers.update({"User-Agent": "Khattak-Live-Scanner/4.0"})

DEFAULT_COINS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "BNB": "BNBUSDT",
    "SOL": "SOLUSDT"
}

METALS = {
    "GOLD": {
        "symbol": "XAU/USD",
        "provider": "Twelve Data OANDA -> FOREX"
    },
    "SILVER": {
        "symbol": "XAG/USD",
        "provider": "Twelve Data OANDA -> FOREX"
    },
}

BLOCKED = {"XAUUSDT", "XAGUSDT", "XAUUSD", "XAGUSD"}


# ---------------- TELEGRAM ----------------

def send_message(message, chat_id=None):
    target = chat_id or CHAT_ID
    if not BOT_TOKEN or not target:
        return False
    try:
        r = session.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": target,
                "text": message,
                "disable_web_page_preview": True
            },
            timeout=20,
        )
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


# ---------------- COIN LIST ----------------

def save_coins(coins):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(coins, f, indent=2)


def load_coins():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return DEFAULT_COINS.copy()
        cleaned = {
            k: v for k, v in data.items()
            if k not in METALS and v not in BLOCKED
        }
        if cleaned != data:
            save_coins(cleaned)
        return cleaned if cleaned else DEFAULT_COINS.copy()
    except Exception:
        return DEFAULT_COINS.copy()


COINS = load_coins()


# ---------------- DATA ----------------

def get_json(url, params=None):
    r = session.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(data.get("message", "Provider error"))
    return data


def resolve_binance_symbol(symbol):
    data = get_json(BINANCE_SPOT + "/api/v3/exchangeInfo")
    return any(
        x.get("symbol") == symbol and x.get("status") == "TRADING"
        for x in data.get("symbols", [])
    )


def resolve_bybit_symbol(symbol):
    data = get_json(
        BYBIT + "/v5/market/instruments-info",
        {"category": "linear", "symbol": symbol}
    )
    items = data.get("result", {}).get("list", [])
    return bool(items and items[0].get("status") == "Trading")


def get_live_price(symbol):
    try:
        data = get_json(
            BINANCE_SPOT + "/api/v3/ticker/price",
            {"symbol": symbol}
        )
        return float(data["price"]), "Binance Spot"
    except Exception:
        pass

    try:
        data = get_json(
            BYBIT + "/v5/market/tickers",
            {"category": "linear", "symbol": symbol}
        )
        items = data.get("result", {}).get("list", [])
        if items:
            return float(items[0]["lastPrice"]), "Bybit Linear"
    except Exception:
        pass

    return None, "Live ticker unavailable"


_td_lock = threading.Lock()
_td_last = [0.0]


def twelve_data_request(endpoint, params):
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError("TWELVE_DATA_API_KEY missing in Render.")

    p = dict(params)
    p["apikey"] = TWELVE_DATA_API_KEY

    with _td_lock:
        wait = TD_MIN_GAP - (time.time() - _td_last[0])
        if wait > 0:
            time.sleep(wait)

        try:
            return get_json(f"{TWELVE_DATA}/{endpoint}", p)
        finally:
            _td_last[0] = time.time()


# ============================================================
# GOLD / SILVER DATA
# FIRST: OANDA
# SECOND: FOREX
# ============================================================

def get_metal_klines(metal_name, interval, outputsize=250):
    symbol = METALS[metal_name]["symbol"]
    errors = []

    # OANDA first, FOREX second
    for exchange in ["OANDA", "FOREX"]:
        try:
            logging.info(
                "%s %s: Trying Twelve Data %s",
                metal_name, interval, exchange
            )

            data = twelve_data_request(
                "time_series",
                {
                    "symbol": symbol,
                    "interval": TIMEFRAMES[interval],
                    "outputsize": outputsize,
                    "timezone": "UTC",
                    "format": "JSON",
                    "exchange": exchange,
                }
            )

            if not data.get("values"):
                raise RuntimeError(
                    data.get("message", "No candle values returned")
                )

            df = pd.DataFrame(data["values"]).rename(
                columns={"datetime": "time"}
            )

            if "volume" not in df.columns:
                df["volume"] = np.nan

            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")

            df["time"] = pd.to_datetime(
                df["time"], utc=True, errors="coerce"
            )

            df = (
                df.dropna(subset=["time", "open", "high", "low", "close"])
                .sort_values("time")
                .reset_index(drop=True)
            )

            if len(df) > 2:
                df = df.iloc[:-1].copy()

            if len(df) < 100:
                raise RuntimeError(
                    f"Insufficient {metal_name} {interval} candles: {len(df)}"
                )

            logging.info(
                "%s %s: Successfully received candles from %s",
                metal_name, interval, exchange
            )

            return df.reset_index(drop=True), f"Twelve Data ({exchange})"

        except Exception as e:
            errors.append(f"{exchange}: {str(e)}")
            logging.warning(
                "%s %s failed on %s: %s",
                metal_name, interval, exchange, e
            )

    raise RuntimeError(
        f"{metal_name} {interval}: OANDA and FOREX both failed. "
        + " | ".join(errors)
    )


def get_metal_live_price(metal_name):
    symbol = METALS[metal_name]["symbol"]
    errors = []

    # OANDA first, FOREX second
    for exchange in ["OANDA", "FOREX"]:
        try:
            logging.info(
                "%s live price: Trying Twelve Data %s",
                metal_name, exchange
            )

            data = twelve_data_request(
                "price",
                {
                    "symbol": symbol,
                    "exchange": exchange,
                }
            )

            if "price" not in data:
                raise RuntimeError(
                    data.get("message", "Live price unavailable")
                )

            price = float(data["price"])

            if not np.isfinite(price) or price <= 0:
                raise RuntimeError("Invalid live price returned")

            logging.info(
                "%s live price received from %s: %s",
                metal_name, exchange, price
            )

            return price, f"Twelve Data live ({exchange})"

        except Exception as e:
            errors.append(f"{exchange}: {str(e)}")
            logging.warning(
                "%s live price failed on %s: %s",
                metal_name, exchange, e
            )

    raise RuntimeError(
        f"{metal_name} live price unavailable from OANDA and FOREX. "
        + " | ".join(errors)
    )


# ---------------- CRYPTO KLINES ----------------

def get_klines(symbol, interval, limit=250):
    if symbol in BLOCKED:
        raise RuntimeError("This asset is excluded.")

    for source_name, source_type in [
        ("Binance Spot", "binance"),
        ("Bybit Linear", "bybit")
    ]:
        try:
            if source_type == "binance":
                data = get_json(
                    BINANCE_SPOT + "/api/v3/klines",
                    {
                        "symbol": symbol,
                        "interval": interval,
                        "limit": limit
                    }
                )

                df = pd.DataFrame(
                    data,
                    columns=[
                        "time", "open", "high", "low", "close",
                        "volume", "close_time", "quote_volume",
                        "trades", "taker_buy", "taker_quote", "ignore"
                    ]
                )[["time", "open", "high", "low", "close", "volume"]]

            else:
                bi = {"1h": "60", "4h": "240", "1d": "D"}[interval]

                data = get_json(
                    BYBIT + "/v5/market/kline",
                    {
                        "category": "linear",
                        "symbol": symbol,
                        "interval": bi,
                        "limit": limit
                    }
                )

                rows = data.get("result", {}).get("list", [])
                if not rows:
                    continue

                rows.reverse()

                df = pd.DataFrame(
                    rows,
                    columns=[
                        "time", "open", "high", "low",
                        "close", "volume", "turnover"
                    ]
                )[["time", "open", "high", "low", "close", "volume"]]

            for c in ["time", "open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")

            df = df.dropna().reset_index(drop=True)

            if len(df) > 2:
                df = df.iloc[:-1].copy()

            if len(df) >= 100:
                return df.reset_index(drop=True), source_name

        except Exception as e:
            logging.warning(
                "%s failed %s %s: %s",
                source_name, symbol, interval, e
            )

    raise RuntimeError("No reliable market data source.")


# ---------------- INDICATORS ----------------

def calculate_indicators(df):
    df = df.copy()

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    df["EMA20"] = close.ewm(span=20, adjust=False).mean()
    df["EMA50"] = close.ewm(span=50, adjust=False).mean()
    df["EMA200"] = close.ewm(span=200, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()

    rs = gain / loss.replace(0, np.nan)
    df["RSI"] = 100 - (100 / (1 + rs))

    df.loc[(loss == 0) & (gain > 0), "RSI"] = 100
    df.loc[(loss == 0) & (gain == 0), "RSI"] = 50

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()

    df["MACD"] = ema12 - ema26
    df["MACD_SIGNAL"] = df["MACD"].ewm(span=9, adjust=False).mean()

    mid = close.rolling(20).mean()
    std = close.rolling(20).std()

    df["BB_UPPER"] = mid + 2 * std
    df["BB_LOWER"] = mid - 2 * std

    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ],
        axis=1
    ).max(axis=1)

    df["ATR"] = tr.ewm(alpha=1/14, adjust=False).mean()

    if volume.notna().sum() >= 25 and volume.fillna(0).sum() > 0:
        df["OBV"] = (
            np.sign(close.diff()).fillna(0) * volume.fillna(0)
        ).cumsum()
        df["OBV_MA"] = df["OBV"].rolling(20).mean()
    else:
        df["OBV"] = np.nan
        df["OBV_MA"] = np.nan

    up = high.diff()
    down = -low.diff()

    pdm = up.where((up > down) & (up > 0), 0)
    mdm = down.where((down > up) & (down > 0), 0)

    atr_safe = df["ATR"].replace(0, np.nan)

    pdi = 100 * pdm.ewm(alpha=1/14, adjust=False).mean() / atr_safe
    mdi = 100 * mdm.ewm(alpha=1/14, adjust=False).mean() / atr_safe

    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)

    df["ADX"] = dx.ewm(alpha=1/14, adjust=False).mean()

    df["VOL_AVG"] = (
        volume.rolling(20).mean()
        if volume.notna().sum() >= 20
        else np.nan
    )

    return df


def candle_pattern(df):
    if df is None or len(df) < 5:
        return "Insufficient Candle Data"

    a = df.iloc[-1]
    b = df.iloc[-2]
    c = df.iloc[-3]

    def parts(row):
        body = abs(row["close"] - row["open"])
        rng = max(row["high"] - row["low"], 1e-12)
        upper = row["high"] - max(row["open"], row["close"])
        lower = min(row["open"], row["close"]) - row["low"]
        return body, rng, upper, lower

    body, rng, upper, lower = parts(a)
    b_body, b_rng, _, _ = parts(b)
    c_body, c_rng, _, _ = parts(c)

    bullish = a["close"] > a["open"]
    bearish = a["close"] < a["open"]
    b_bullish = b["close"] > b["open"]
    b_bearish = b["close"] < b["open"]
    c_bullish = c["close"] > c["open"]
    c_bearish = c["close"] < c["open"]

    prev = df.iloc[-5:-2]
    prev_down = prev["close"].iloc[-1] < prev["close"].iloc[0]
    prev_up = prev["close"].iloc[-1] > prev["close"].iloc[0]

    small_body = body / rng <= 0.35

    hammer = lower >= max(body * 2, rng * 0.55) and upper <= rng * 0.15 and small_body and prev_down
    hanging = lower >= max(body * 2, rng * 0.55) and upper <= rng * 0.15 and small_body and prev_up
    inv_hammer = upper >= max(body * 2, rng * 0.55) and lower <= rng * 0.15 and small_body and prev_down
    shooting = upper >= max(body * 2, rng * 0.55) and lower <= rng * 0.15 and small_body and prev_up

    doji = body / rng <= 0.10
    grave = doji and upper / rng >= 0.65 and lower / rng <= 0.10
    dragon = doji and lower / rng >= 0.65 and upper / rng <= 0.10
    spin = small_body and upper > body and lower > body and not doji

    bull_eng = b_bearish and bullish and a["open"] <= b["close"] and a["close"] >= b["open"] and body > b_body
    bear_eng = b_bullish and bearish and a["open"] >= b["close"] and a["close"] <= b["open"] and body > b_body

    bull_harami = b_bearish and bullish and a["open"] > b["close"] and a["close"] < b["open"] and body < b_body
    bear_harami = b_bullish and bearish and a["open"] < b["close"] and a["close"] > b["open"] and body < b_body

    morning = c_bearish and c_body / c_rng >= 0.50 and b_body / b_rng <= 0.30 and bullish and a["close"] > (c["open"] + c["close"]) / 2
    evening = c_bullish and c_body / c_rng >= 0.50 and b_body / b_rng <= 0.30 and bearish and a["close"] < (c["open"] + c["close"]) / 2

    white_sold = all(
        df["close"].iloc[-i] > df["open"].iloc[-i]
        for i in [1, 2, 3]
    ) and (
        df["close"].iloc[-1] > df["close"].iloc[-2] > df["close"].iloc[-3]
    )

    black_crows = all(
        df["close"].iloc[-i] < df["open"].iloc[-i]
        for i in [1, 2, 3]
    ) and (
        df["close"].iloc[-1] < df["close"].iloc[-2] < df["close"].iloc[-3]
    )

    piercing = b_bearish and bullish and a["open"] < b["close"] and a["close"] > (b["open"] + b["close"]) / 2 and a["close"] < b["open"]
    dark = b_bullish and bearish and a["open"] > b["close"] and a["close"] < (b["open"] + b["close"]) / 2 and a["close"] > b["open"]

    tweezer_top = b_bullish and bearish and abs(a["high"] - b["high"]) <= b_rng * 0.10 and prev_up
    tweezer_bot = b_bearish and bullish and abs(a["low"] - b["low"]) <= b_rng * 0.10 and prev_down

    checks = [
        (hammer, "Hammer - Bullish"),
        (hanging, "Hanging Man - Bearish"),
        (inv_hammer, "Inverted Hammer - Bullish"),
        (shooting, "Shooting Star - Bearish"),
        (grave, "Gravestone Doji - Bearish"),
        (dragon, "Dragonfly Doji - Bullish"),
        (doji and not grave and not dragon, "Doji - Neutral"),
        (spin, "Spinning Top - Neutral"),
        (bull_eng, "Bullish Engulfing"),
        (bear_eng, "Bearish Engulfing"),
        (bull_harami, "Bullish Harami"),
        (bear_harami, "Bearish Harami"),
        (morning, "Morning Star - Bullish"),
        (evening, "Evening Star - Bearish"),
        (white_sold, "Three White Soldiers - Bullish"),
        (black_crows, "Three Black Crows - Bearish"),
        (piercing, "Piercing Line - Bullish"),
        (dark, "Dark Cloud Cover - Bearish"),
        (tweezer_top, "Tweezer Top - Bearish"),
        (tweezer_bot, "Tweezer Bottom - Bullish"),
    ]

    pats = [label for cond, label in checks if cond]
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
    r = df.tail(30)
    prev = df.iloc[-2]
    cur = df.iloc[-1]

    ph = float(r["high"].iloc[:-1].max())
    pl = float(r["low"].iloc[:-1].min())

    if cur["close"] > ph:
        return "Bullish BOS"
    if cur["close"] < pl:
        return "Bearish BOS"
    if cur["close"] > prev["high"]:
        return "Possible Bullish CHoCH"
    if cur["close"] < prev["low"]:
        return "Possible Bearish CHoCH"

    return "No confirmed break"


# ---------------- RETEST + BOUNCE ----------------

def retest_state(direction, h1, support, resistance, live_price):
    last = h1.iloc[-1]
    prev = h1.iloc[-2]

    atr = float(last["ATR"])
    close = float(last["close"])
    tol = ZONE_ATR * atr
    recent = h1.tail(3)

    if direction == "BUY":
        levels = {
            "1H EMA20": float(last["EMA20"]),
            "1H EMA50": float(last["EMA50"]),
            "4H Support": support
        }
        extreme = float(recent["low"].min())
        candle_ok = last["close"] > last["open"] and last["close"] > prev["close"]

    else:
        levels = {
            "1H EMA20": float(last["EMA20"]),
            "1H EMA50": float(last["EMA50"]),
            "4H Resistance": resistance
        }
        extreme = float(recent["high"].max())
        candle_ok = last["close"] < last["open"] and last["close"] < prev["close"]

    confirmed = []
    in_zone = []

    for name, lvl in levels.items():
        if direction == "BUY":
            touched = (lvl - 1.5 * atr) <= extreme <= (lvl + tol)
            ok = touched and candle_ok and close > lvl
        else:
            touched = (lvl - tol) <= extreme <= (lvl + 1.5 * atr)
            ok = touched and candle_ok and close < lvl

        if ok:
            confirmed.append((abs(close - lvl), name, lvl))

        if abs(live_price - lvl) <= tol:
            in_zone.append((abs(live_price - lvl), name, lvl))

    out = {
        "state": "FAR",
        "level_name": None,
        "level": None,
        "zone_low": None,
        "zone_high": None,
        "entry": None,
        "sl": None,
        "tp": None
    }

    if confirmed:
        _, name, lvl = min(confirmed)

        out.update(
            level_name=name,
            level=lvl,
            zone_low=lvl - tol,
            zone_high=lvl + tol
        )

        entry = close

        if direction == "BUY":
            sl = extreme - SL_BUFFER_ATR * atr
            risk = entry - sl
        else:
            sl = extreme + SL_BUFFER_ATR * atr
            risk = sl - entry

        if risk <= 0 or risk > MAX_RISK_ATR * atr:
            out["state"] = "RISK"
            return out

        if abs(live_price - entry) > MAX_MOVE_ATR * atr:
            out["state"] = "MOVED"
            return out

        tp = entry + RR * risk if direction == "BUY" else entry - RR * risk

        out.update(
            state="CONFIRMED",
            entry=entry,
            sl=sl,
            tp=tp
        )
        return out

    if in_zone:
        _, name, lvl = min(in_zone)

        out.update(
            state="ZONE",
            level_name=name,
            level=lvl,
            zone_low=lvl - tol,
            zone_high=lvl + tol
        )
        return out

    name, lvl = min(
        levels.items(),
        key=lambda kv: abs(live_price - kv[1])
    )

    out.update(
        level_name=name,
        level=lvl,
        zone_low=lvl - tol,
        zone_high=lvl + tol
    )

    return out


# ---------------- ANALYSIS ----------------

def analyze_coin(name, symbol=None):
    try:
        is_metal = name in METALS
        display_symbol = METALS[name]["symbol"] if is_metal else symbol

        frames = {}
        sources = []

        for tf in ["1d", "4h", "1h"]:
            if is_metal:
                raw, src = get_metal_klines(name, tf)
            else:
                raw, src = get_klines(symbol, tf)

            frames[tf] = calculate_indicators(raw)
            sources.append(src)

        daily = frames["1d"].iloc[-1]
        four = frames["4h"].iloc[-1]
        hourly = frames["1h"].iloc[-1]

        t1d = trend_of(daily)
        t4h = trend_of(four)
        t1h = trend_of(hourly)

        candle_price = float(hourly["close"])
        atr = float(hourly["ATR"])
        rsi = float(hourly["RSI"])
        adx = float(hourly["ADX"])

        if not all(np.isfinite(v) for v in [candle_price, atr, rsi, adx]) or atr <= 0:
            raise RuntimeError("Indicator data incomplete")

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # Gold/Silver weekend / closed-market check
        if is_metal:
            age = pd.Timestamp.now(tz="UTC") - frames["1h"]["time"].iloc[-1]

            if age > pd.Timedelta(hours=4):
                return {
                    "name": name,
                    "symbol": display_symbol,
                    "signal": "WAIT",
                    "closed": True,
                    "reason": "Market closed / data stale - no signal",
                    "timestamp": timestamp
                }

        support, resistance = support_resistance(frames["4h"])

        pattern = candle_pattern(frames["1h"])
        structure = market_structure(frames["1h"])

        if is_metal:
            live_price, live_source = get_metal_live_price(name)
        else:
            live_price, live_source = get_live_price(symbol)

            if live_price is None:
                live_price = candle_price
                live_source = "Last closed 1H candle (ticker unavailable)"

        # Main trend: 1D + 4H
        direction = "NONE"

        if t1d == "UPTREND" and t4h == "UPTREND":
            direction = "BUY"
        elif t1d == "DOWNTREND" and t4h == "DOWNTREND":
            direction = "SELL"

        signal = "WAIT"
        plan = {"state": "NONE"}

        if direction == "NONE":
            reason = "1D and 4H trends not aligned - Main Trend WAIT"

        elif adx < 18:
            reason = f"Main Trend {direction}, but ADX {adx:.1f} below 18"

        elif not (25 < rsi < 75):
            reason = f"Main Trend {direction}, but RSI {rsi:.1f} outside 25-75"

        else:
            plan = retest_state(
                direction,
                frames["1h"],
                support,
                resistance,
                live_price
            )

            lv = f"{plan['level_name']} ({plan['level']:.8g})"
            st = plan["state"]

            if st == "CONFIRMED":
                signal = direction
                reason = (
                    f"Main Trend {direction}. Retest of {lv} "
                    f"+ bounce confirmed. RR 1:{RR:g}"
                )

            elif st == "ZONE":
                signal = "ALERT"
                reason = (
                    f"Main Trend {direction}. Price in retest zone "
                    f"of {lv} - waiting for bounce candle"
                )

            elif st == "RISK":
                reason = (
                    f"Bounce at {lv} confirmed, but SL wider than "
                    f"{MAX_RISK_ATR:g} ATR - skip"
                )

            elif st == "MOVED":
                reason = (
                    f"Bounce at {lv} confirmed, but price already "
                    f"moved over {MAX_MOVE_ATR:g} ATR - skip"
                )

            else:
                reason = f"Main Trend {direction}. Waiting for retest of {lv}"

        macd_state = (
            "Bullish"
            if hourly["MACD"] > hourly["MACD_SIGNAL"]
            else "Bearish"
        )

        have_obv = pd.notna(hourly["OBV"]) and pd.notna(hourly["OBV_MA"])

        obv_state = (
            "Rising" if hourly["OBV"] > hourly["OBV_MA"] else "Falling"
        ) if have_obv else "Unavailable"

        have_vol = pd.notna(hourly["VOL_AVG"])

        vol_state = (
            "Above Average" if hourly["volume"] > hourly["VOL_AVG"]
            else "Below Average"
        ) if have_vol else "Unavailable"

        boll = (
            "Upper Band" if live_price >= hourly["BB_UPPER"]
            else "Lower Band" if live_price <= hourly["BB_LOWER"]
            else "Inside Bands"
        )

        source = sources[0] if len(set(sources)) == 1 else "Mixed sources"

        return {
            "name": name,
            "symbol": display_symbol,
            "signal": signal,
            "direction": direction,
            "price": live_price,
            "candle_price": candle_price,
            "live_source": live_source,
            "plan": plan,
            "trend_1d": t1d,
            "trend_4h": t4h,
            "trend_1h": t1h,
            "rsi": rsi,
            "adx": adx,
            "macd": macd_state,
            "obv": obv_state,
            "volume": vol_state,
            "bollinger": boll,
            "pattern": pattern,
            "structure": structure,
            "support": support,
            "resistance": resistance,
            "atr": atr,
            "reason": reason,
            "source": source,
            "timestamp": timestamp,
        }

    except Exception as exc:
        logging.exception("Analysis failed for %s", name)

        return {
            "name": name,
            "symbol": symbol or METALS.get(name, {}).get("symbol", "N/A"),
            "signal": "DATA ERROR",
            "error": str(exc)
        }


def fmt(v):
    return "Not available" if v is None else f"{v:.8g}"


def format_result(r):
    if r.get("error"):
        return (
            f"⚠️ DATA ERROR — {r['name']} ({r['symbol']})\n"
            f"Reason: {r['error']}\n"
            f"This is NOT a market WAIT signal."
        )

    if r.get("closed"):
        return (
            f"⚪ CLOSED — {r['name']} ({r['symbol']})\n"
            f"{r['reason']}\nUpdated: {r['timestamp']}"
        )

    signal = r["signal"]

    dot = {
        "BUY": "🟢",
        "SELL": "🔴",
        "ALERT": "🟠",
        "WAIT": "🟡"
    }.get(signal, "🟡")

    plan = r["plan"]

    if plan.get("zone_low") is not None:
        zone = (
            f"{plan['level_name']}: "
            f"{fmt(plan['zone_low'])} - {fmt(plan['zone_high'])}"
        )
    else:
        zone = "Not available"

    if plan.get("state") == "CONFIRMED":
        levels = (
            f"Entry: {fmt(plan['entry'])}\n"
            f"SL: {fmt(plan['sl'])}\n"
            f"TP: {fmt(plan['tp'])}"
        )
    else:
        levels = "Entry / SL / TP: only after retest + bounce is confirmed"

    return (
        f"{dot} {signal} — {r['name']} ({r['symbol']})\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Live Price: {fmt(r['price'])}\n"
        f"Last Closed 1H: {fmt(r['candle_price'])}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Main Direction: {r['direction']}\n"
        f"Retest Zone: {zone}\n"
        f"{levels}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"1D: {r['trend_1d']} (Main)\n"
        f"4H: {r['trend_4h']} (Main)\n"
        f"1H: {r['trend_1h']} (Entry)\n"
        f"RSI: {r['rsi']:.2f} | ADX: {r['adx']:.2f}\n"
        f"MACD: {r['macd']} | OBV: {r['obv']}\n"
        f"Volume: {r['volume']} | Bollinger: {r['bollinger']}\n"
        f"Candle: {r['pattern']}\n"
        f"Structure: {r['structure']}\n"
        f"4H Support: {fmt(r['support'])} | "
        f"Resistance: {fmt(r['resistance'])}\n"
        f"Data: {r['source']} | Live: {r['live_source']}\n"
        f"Reason: {r['reason']}\n"
        f"Updated: {r['timestamp']}\n"
        f"Preliminary scanner only — manual checklist review required.\n"
        f"No automatic order execution."
    )


def scan_all(chat_id=None):
    results = []

    for name, symbol in list(COINS.items()):
        if name in METALS or symbol in BLOCKED:
            continue

        results.append(format_result(analyze_coin(name, symbol)))
        time.sleep(0.15)

    for name in METALS.keys():
        results.append(format_result(analyze_coin(name, None)))

    header = (
        "📊 KHATTAK LIVE MARKET SCANNER\n"
        "Hourly market report\n"
        "Crypto + Gold + Silver — no automatic trading.\n\n"
    )

    if not results:
        results.append("No coins available.")

    send_long_message(header + "\n\n".join(results), chat_id=chat_id)


# ---------------- COIN MANAGEMENT ----------------

def find_symbol(ticker):
    ticker = ticker.upper().strip()

    if ticker in METALS or ticker in BLOCKED:
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

    COINS[ticker] = symbol
    save_coins(COINS)

    return f"✅ {ticker} added successfully as {symbol}."


def remove_coin(ticker):
    ticker = ticker.upper().strip()

    if ticker in METALS:
        return "GOLD / SILVER are built-in and cannot be removed."

    for name, symbol in list(COINS.items()):
        if name == ticker or symbol == ticker:
            del COINS[name]
            save_coins(COINS)
            return f"✅ {name} removed."

    return f"{ticker} is not in the list."


# ---------------- COMMANDS ----------------

def handle_command(text, chat_id=None):
    parts = text.strip().split()

    if not parts:
        return "Use /help to see commands."

    cmd = parts[0].split("@")[0].lower()

    if cmd == "/start":
        return (
            "✅ KHATTAK LIVE SCANNER is online.\n"
            "Main Trend: 1D + 4H | Entry: 1H retest + bounce\n"
            "Crypto + Gold + Silver configured.\n"
            "Use /help for commands."
        )

    if cmd == "/help":
        return (
            "KHATTAK LIVE SCANNER COMMANDS\n\n"
            "/start — Bot status\n"
            "/scan — Scan all\n"
            "/scan BTC — Scan one coin\n"
            "/scan GOLD — Scan Gold\n"
            "/scan SILVER — Scan Silver\n"
            "/check BTC — Scan one coin\n"
            "/add PEPE — Add a coin\n"
            "/remove PEPE — Remove a coin\n"
            "/list — Show coin list\n"
            "/help — Help\n\n"
            "Signals: 🟢 BUY / 🔴 SELL = retest + bounce confirmed\n"
            "🟠 ALERT = price in retest zone, waiting for bounce\n"
            "🟡 WAIT = no setup\n"
            "Bot does not execute trades."
        )

    if cmd == "/list":
        lines = (
            ["📋 COIN LIST"]
            + [f"{k}: {v}" for k, v in COINS.items()]
            + [
                f"{k}: {v['symbol']} ({v['provider']})"
                for k, v in METALS.items()
            ]
        )
        return "\n".join(lines)

    if cmd == "/add":
        return add_coin(parts[1]) if len(parts) > 1 else "Usage: /add PEPE"

    if cmd == "/remove":
        return remove_coin(parts[1]) if len(parts) > 1 else "Usage: /remove PEPE"

    if cmd in ("/scan", "/check"):
        if cmd == "/check" and len(parts) < 2:
            return "Usage: /check BTC"

        if len(parts) >= 2:
            ticker = parts[1].upper().strip()

            if ticker in BLOCKED:
                return "Use GOLD / SILVER."

            def do_single():
                target = chat_id or CHAT_ID

                if ticker in METALS:
                    result = analyze_coin(ticker, None)
                else:
                    symbol = COINS.get(ticker) or find_symbol(ticker)

                    if not symbol:
                        send_message(
                            f"Could not verify a live USDT market for {ticker}.",
                            chat_id=target
                        )
                        return

                    result = analyze_coin(ticker, symbol)

                send_message(format_result(result), chat_id=target)

            threading.Thread(target=do_single, daemon=True).start()

            return f"🔄 Scanning {ticker}... Result will arrive shortly."

        threading.Thread(
            target=lambda: scan_all(chat_id),
            daemon=True
        ).start()

        return "🔄 Full market scan started. Gold/Silver take ~1 minute (API limit)."

    return "Unknown command. Use /help."


# ---------------- FLASK ----------------

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "ok",
        "service": "KHATTAK LIVE SCANNER",
        "crypto_count": len(COINS),
        "metals": list(METALS.keys())
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True, silent=True) or {}
        msg = data.get("message") or {}

        txt = msg.get("text", "")
        cid = (msg.get("chat") or {}).get("id")

        if txt and cid:
            send_message(handle_command(txt, cid), chat_id=cid)

        return jsonify({"ok": True}), 200

    except Exception:
        logging.exception("Webhook error")
        return jsonify({"ok": True}), 200


scheduler = BackgroundScheduler(timezone="UTC")

scheduler.add_job(
    scan_all,
    "interval",
    hours=1,
    id="hourly_market_scan",
    replace_existing=True,
    max_instances=1,
    coalesce=True
)

scheduler.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
