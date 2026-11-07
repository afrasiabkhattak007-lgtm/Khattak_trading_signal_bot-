import os, json, time, threading, logging, requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PORT = int(os.getenv("PORT", "10000"))
# Render variable is used if present; otherwise the key below is used.
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "") or "02cc28c647014ed390683041910bb568"

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_FILE = "coins.json"
WAITS_FILE = "waits.json"
BINANCE_SPOT = "https://data-api.binance.vision"
BYBIT = "https://api.bybit.com"
TWELVE_DATA = "https://api.twelvedata.com"

TIMEFRAMES = {"1d": "1day", "4h": "4h", "1h": "1h"}
MIN_SCORE = 5   # kam karoge to zyada BUY/SELL, zyada karoge to zyada WAIT
MIN_MARGIN = 3  # BUY aur SELL ke score me kam az kam itna farq
RR = 2.0
ZONE_ATR = 0.5
SL_BUFFER_ATR = 0.8
MAX_RISK_ATR = 3.0
MAX_MOVE_ATR = 1.0
TD_MIN_GAP = 8.0
WAIT_MAX_HOURS = 24

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

app = Flask(__name__)
session = requests.Session()
session.headers.update({"User-Agent": "Khattak-Live-Scanner/5.2"})

DEFAULT_COINS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "BNB": "BNBUSDT",
    "SOL": "SOLUSDT", "XRP": "XRPUSDT", "TRX": "TRXUSDT",
    "TON": "TONUSDT", "DOGE": "DOGEUSDT", "SUI": "SUIUSDT",
    "GRAM": "GRAMUSDT"
}
METALS = {"GOLD": {"symbol": "XAU/USD", "provider": "Twelve Data"}}
BLOCKED = {"XAUUSDT", "XAUUSD", "XAGUSDT", "XAGUSD", "SILVER", "XAG/USD", "XAG"}


def safe(msg):
    """Never let the API key appear in Telegram messages or logs."""
    s = str(msg)
    if TWELVE_DATA_API_KEY:
        s = s.replace(TWELVE_DATA_API_KEY, "***")
    return s


def send_message(m, chat_id=None):
    t = chat_id or CHAT_ID
    if not BOT_TOKEN or not t:
        return False
    try:
        r = session.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": t, "text": m, "disable_web_page_preview": True},
            timeout=20
        )
        r.raise_for_status()
        return True
    except Exception:
        logging.error("Telegram send failed")
        return False


def send_long(m, chat_id=None):
    lim = 3900
    while m:
        if len(m) <= lim:
            send_message(m, chat_id)
            break
        c = m.rfind("\n", 0, lim)
        if c < 1:
            c = lim
        send_message(m[:c], chat_id)
        m = m[c:].lstrip()


def save_coins(c):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2)


def load_coins():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return DEFAULT_COINS.copy()
        cl = {
            k: v for k, v in d.items()
            if k not in METALS and k not in BLOCKED and v not in BLOCKED
        }
        if cl != d:
            save_coins(cl)
        return cl if cl else DEFAULT_COINS.copy()
    except Exception:
        return DEFAULT_COINS.copy()


COINS = load_coins()

# ---------------- WAIT TRACKING ----------------
_waits_lock = threading.RLock()


def load_waits():
    with _waits_lock:
        try:
            with open(WAITS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}


def save_waits(d):
    with _waits_lock:
        try:
            with open(WAITS_FILE, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2)
        except Exception:
            logging.exception("Could not save waits file")


def upsert_wait(name, info):
    """Save a WAIT. If the same direction is already waiting, keep its original start time."""
    with _waits_lock:
        w = load_waits()
        old = w.get(name)
        if old and old.get("direction") == info.get("direction"):
            info["created"] = old.get("created", info["created"])
            info["time"] = old.get("time", info["time"])
        w[name] = info
        save_waits(w)


def remove_wait(name):
    with _waits_lock:
        w = load_waits()
        if name in w:
            del w[name]
            save_waits(w)


def handle_wait_result(name, res, chat_id=None):
    """Compare a fresh analysis with a pending WAIT: confirm, cancel, or keep waiting."""
    info = load_waits().get(name)
    if not info:
        return
    sig = res.get("signal")

    if sig in ("BUY", "SELL"):
        if sig == info.get("direction"):
            send_message(
                f"✅ PREVIOUS WAIT NOW CONFIRMED — {name}\n"
                f"(WAIT was sent: {info.get('time')})\n\n" + format_result(res),
                chat_id=chat_id
            )
        remove_wait(name)
        return

    if sig == "DATA ERROR" or res.get("closed"):
        return

    try:
        created = datetime.fromisoformat(info.get("created", ""))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except Exception:
        created = datetime.now(timezone.utc)

    if datetime.now(timezone.utc) - created > timedelta(hours=WAIT_MAX_HOURS):
        remove_wait(name)
        send_message(
            f"❌ WAIT CANCELLED — {name}\n"
            f"Reason: {WAIT_MAX_HOURS} ghante tak confirm nahi hua.",
            chat_id=chat_id
        )
        return

    live, sl = res.get("price"), info.get("pot_sl")
    if live and sl:
        if info.get("direction") == "BUY" and live < sl:
            remove_wait(name)
            send_message(
                f"❌ WAIT CANCELLED — {name}\n"
                f"Reason: Price SL {sl:.8g} se neeche chali gayi.",
                chat_id=chat_id
            )
        elif info.get("direction") == "SELL" and live > sl:
            remove_wait(name)
            send_message(
                f"❌ WAIT CANCELLED — {name}\n"
                f"Reason: Price SL {sl:.8g} se upar chali gayi.",
                chat_id=chat_id
            )


# ---------------- DATA ----------------

def get_json(url, params=None):
    r = session.get(url, params=params, timeout=20)
    try:
        d = r.json()
    except Exception:
        d = None
    # show the provider's own message (e.g. Twelve Data plan / limit errors)
    if isinstance(d, dict) and d.get("status") == "error":
        raise RuntimeError(d.get("message", "Provider error"))
    r.raise_for_status()
    if d is None:
        d = r.json()
    if isinstance(d, dict) and d.get("retCode", 0) not in (0, "0"):
        raise RuntimeError(d.get("retMsg", "Bybit provider error"))
    return d


def resolve_binance(s):
    d = get_json(BINANCE_SPOT + "/api/v3/exchangeInfo")
    return any(
        x.get("symbol") == s and x.get("status") == "TRADING"
        for x in d.get("symbols", [])
    )


def resolve_bybit(s):
    d = get_json(
        BYBIT + "/v5/market/instruments-info",
        {"category": "linear", "symbol": s}
    )
    l = d.get("result", {}).get("list", [])
    return bool(l and l[0].get("status") == "Trading")


def get_live_price(s):
    try:
        d = get_json(BINANCE_SPOT + "/api/v3/ticker/price", {"symbol": s})
        return float(d["price"]), "Binance Spot"
    except Exception:
        pass
    try:
        d = get_json(
            BYBIT + "/v5/market/tickers",
            {"category": "linear", "symbol": s}
        )
        l = d.get("result", {}).get("list", [])
        if l:
            return float(l[0]["lastPrice"]), "Bybit Linear"
    except Exception:
        pass
    return None, "Live ticker unavailable"


_td_lock = threading.Lock()
_td_last = [0.0]


def td_req(ep, params):
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError("TWELVE_DATA_API_KEY missing in Render.")
    p = dict(params)
    p["apikey"] = TWELVE_DATA_API_KEY
    with _td_lock:
        w = TD_MIN_GAP - (time.time() - _td_last[0])
        if w > 0:
            time.sleep(w)
        try:
            return get_json(f"{TWELVE_DATA}/{ep}", p)
        finally:
            _td_last[0] = time.time()


def metal_tries():
    return [("XAU/USD", "OANDA"), ("XAU/USD", "FOREX"), ("XAU/USD", None)]


def get_metal_klines(tf, outputsize=250):
    errs = []
    for sym, ex in metal_tries():
        try:
            p = {
                "symbol": sym, "interval": TIMEFRAMES[tf],
                "outputsize": outputsize, "timezone": "UTC", "format": "JSON"
            }
            if ex:
                p["exchange"] = ex
            d = td_req("time_series", p)
            if not d.get("values"):
                raise RuntimeError(d.get("message", "No candle values"))
            df = pd.DataFrame(d["values"]).rename(columns={"datetime": "time"})
            if "volume" not in df.columns:
                df["volume"] = np.nan
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
            df = (
                df.dropna(subset=["time", "open", "high", "low", "close"])
                .sort_values("time").reset_index(drop=True)
            )
            if len(df) > 2:
                df = df.iloc[:-1].copy()
            if len(df) < 100:
                raise RuntimeError(f"Insufficient GOLD {tf}: {len(df)}")
            logging.info("GOLD %s: Success %s/%s", tf, sym, ex)
            return df.reset_index(drop=True), f"Twelve Data ({sym}/{ex})"
        except Exception as e:
            errs.append(f"{sym}/{ex}: {safe(e)[:150]}")
    raise RuntimeError(f"GOLD {tf}: All failed. " + " | ".join(errs))


def get_metal_live():
    errs = []
    for sym, ex in metal_tries():
        try:
            p = {"symbol": sym}
            if ex:
                p["exchange"] = ex
            d = td_req("price", p)
            if "price" not in d:
                raise RuntimeError(d.get("message", "Live price unavailable"))
            pr = float(d["price"])
            if not np.isfinite(pr) or pr <= 0:
                raise RuntimeError("Invalid live price")
            return pr, f"Twelve Data live ({sym}/{ex})"
        except Exception as e:
            errs.append(f"{sym}/{ex}: {safe(e)[:150]}")
    raise RuntimeError("GOLD live price unavailable. " + " | ".join(errs))


def get_klines(sym, tf, limit=250):
    if sym in BLOCKED:
        raise RuntimeError("This asset is excluded.")
    for src, typ in [("Binance Spot", "binance"), ("Bybit Linear", "bybit")]:
        try:
            if typ == "binance":
                d = get_json(
                    BINANCE_SPOT + "/api/v3/klines",
                    {"symbol": sym, "interval": tf, "limit": limit}
                )
                df = pd.DataFrame(
                    d,
                    columns=[
                        "time", "open", "high", "low", "close", "volume",
                        "close_time", "quote_volume", "trades",
                        "taker_buy", "taker_quote", "ignore"
                    ]
                )[["time", "open", "high", "low", "close", "volume"]]
            else:
                bi = {"1h": "60", "4h": "240", "1d": "D"}[tf]
                d = get_json(
                    BYBIT + "/v5/market/kline",
                    {"category": "linear", "symbol": sym, "interval": bi, "limit": limit}
                )
                rows = d.get("result", {}).get("list", [])
                if not rows:
                    continue
                rows.reverse()
                df = pd.DataFrame(
                    rows,
                    columns=["time", "open", "high", "low", "close", "volume", "turnover"]
                )[["time", "open", "high", "low", "close", "volume"]]
            for c in ["time", "open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df.dropna().reset_index(drop=True)
            if len(df) > 2:
                df = df.iloc[:-1].copy()
            if len(df) >= 100:
                return df.reset_index(drop=True), src
        except Exception as e:
            logging.warning("%s failed %s %s: %s", src, sym, tf, e)
    raise RuntimeError("No reliable market data source.")


# ---------------- INDICATORS ----------------

def calc(df):
    df = df.copy()
    cl, hi, lo, vo = df["close"], df["high"], df["low"], df["volume"]
    df["EMA20"] = cl.ewm(span=20, adjust=False).mean()
    df["EMA50"] = cl.ewm(span=50, adjust=False).mean()
    df["EMA200"] = cl.ewm(span=200, adjust=False).mean()
    de = cl.diff()
    g = de.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    l = (-de.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = g / l.replace(0, np.nan)
    df["RSI"] = 100 - (100 / (1 + rs))
    df.loc[(l == 0) & (g > 0), "RSI"] = 100
    df.loc[(l == 0) & (g == 0), "RSI"] = 50
    e12 = cl.ewm(span=12, adjust=False).mean()
    e26 = cl.ewm(span=26, adjust=False).mean()
    df["MACD"] = e12 - e26
    df["MACD_SIGNAL"] = df["MACD"].ewm(span=9, adjust=False).mean()
    mid = cl.rolling(20).mean()
    sd = cl.rolling(20).std()
    df["BB_UPPER"] = mid + 2 * sd
    df["BB_LOWER"] = mid - 2 * sd
    pc = cl.shift(1)
    tr = pd.concat([hi-lo, (hi-pc).abs(), (lo-pc).abs()], axis=1).max(axis=1)
    df["ATR"] = tr.ewm(alpha=1/14, adjust=False).mean()
    if vo.notna().sum() >= 25 and vo.fillna(0).sum() > 0:
        df["OBV"] = (np.sign(cl.diff()).fillna(0) * vo.fillna(0)).cumsum()
        df["OBV_MA"] = df["OBV"].rolling(20).mean()
    else:
        df["OBV"] = np.nan
        df["OBV_MA"] = np.nan
    up, dn = hi.diff(), -lo.diff()
    pdm = up.where((up > dn) & (up > 0), 0)
    mdm = dn.where((dn > up) & (dn > 0), 0)
    ats = df["ATR"].replace(0, np.nan)
    pdi = 100 * pdm.ewm(alpha=1/14, adjust=False).mean() / ats
    mdi = 100 * mdm.ewm(alpha=1/14, adjust=False).mean() / ats
    dx = 100 * (pdi-mdi).abs() / (pdi+mdi).replace(0, np.nan)
    df["ADX"] = dx.ewm(alpha=1/14, adjust=False).mean()
    df["VOL_AVG"] = vo.rolling(20).mean() if vo.notna().sum() >= 20 else np.nan
    return df


def candle_pat(df):
    if df is None or len(df) < 5:
        return []
    a, b, c = df.iloc[-1], df.iloc[-2], df.iloc[-3]

    def pt(r):
        bd = abs(r["close"]-r["open"])
        rg = max(r["high"]-r["low"], 1e-12)
        up = r["high"]-max(r["open"], r["close"])
        lw = min(r["open"], r["close"])-r["low"]
        return bd, rg, up, lw

    bd, rg, up, lw = pt(a)
    bb, br, _, _ = pt(b)
    cb, cr, _, _ = pt(c)
    bull, bear = a["close"] > a["open"], a["close"] < a["open"]
    bbull, bbear = b["close"] > b["open"], b["close"] < b["open"]
    cbull, cbear = c["close"] > c["open"], c["close"] < c["open"]
    pv = df.iloc[-5:-2]
    pdown = pv["close"].iloc[-1] < pv["close"].iloc[0]
    pup = pv["close"].iloc[-1] > pv["close"].iloc[0]
    sm = bd/rg <= 0.35
    ch = [
        (lw >= max(bd*2, rg*0.55) and up <= rg*0.15 and sm and pdown, "Hammer - Bullish"),
        (lw >= max(bd*2, rg*0.55) and up <= rg*0.15 and sm and pup, "Hanging Man - Bearish"),
        (up >= max(bd*2, rg*0.55) and lw <= rg*0.15 and sm and pdown, "Inverted Hammer - Bullish"),
        (up >= max(bd*2, rg*0.55) and lw <= rg*0.15 and sm and pup, "Shooting Star - Bearish"),
        (bd/rg <= 0.10 and up/rg >= 0.65 and lw/rg <= 0.10, "Gravestone Doji - Bearish"),
        (bd/rg <= 0.10 and lw/rg >= 0.65 and up/rg <= 0.10, "Dragonfly Doji - Bullish"),
        (bd/rg <= 0.10, "Doji - Neutral"),
        (bbear and bull and a["open"] <= b["close"] and a["close"] >= b["open"] and bd > bb, "Bullish Engulfing"),
        (bbull and bear and a["open"] >= b["close"] and a["close"] <= b["open"] and bd > bb, "Bearish Engulfing"),
        (cbear and cb/cr >= 0.50 and bb/br <= 0.30 and bull and a["close"] > (c["open"]+c["close"])/2, "Morning Star - Bullish"),
        (cbull and cb/cr >= 0.50 and bb/br <= 0.30 and bear and a["close"] < (c["open"]+c["close"])/2, "Evening Star - Bearish"),
        (all(df["close"].iloc[-i] > df["open"].iloc[-i] for i in [1, 2, 3]), "Three White Soldiers - Bullish"),
        (all(df["close"].iloc[-i] < df["open"].iloc[-i] for i in [1, 2, 3]), "Three Black Crows - Bearish")
    ]
    return [x for ok, x in ch if ok]


def trend_of(r):
    if r["close"] > r["EMA50"] > r["EMA200"] and r["EMA20"] > r["EMA50"]:
        return "UPTREND"
    if r["close"] < r["EMA50"] < r["EMA200"] and r["EMA20"] < r["EMA50"]:
        return "DOWNTREND"
    return "SIDEWAYS"


def sr_levels(df):
    r = df.tail(30)
    lows = sorted(r["low"].tolist())
    highs = sorted(r["high"].tolist(), reverse=True)
    return float(lows[len(lows)//4]), float(lows[0]), float(highs[len(highs)//4]), float(highs[0])


def structure_signals(h1, h4):
    out = []
    r = h1.tail(30)
    prev, cur = h1.iloc[-2], h1.iloc[-1]
    ph = float(r["high"].iloc[:-1].max())
    pl = float(r["low"].iloc[:-1].min())
    if cur["close"] > ph:
        out.append("Bullish BOS")
    if cur["close"] < pl:
        out.append("Bearish BOS")
    if cur["close"] > prev["high"]:
        out.append("Possible Bullish CHoCH")
    if cur["close"] < prev["low"]:
        out.append("Possible Bearish CHoCH")
    if cur["high"] > ph and cur["close"] < ph:
        out.append("Liquidity Grab above high - Bearish")
    if cur["low"] < pl and cur["close"] > pl:
        out.append("Liquidity Grab below low - Bullish")
    if len(h1) >= 3:
        p2, p1 = h1.iloc[-3], h1.iloc[-2]
        if p1["low"] > p2["high"]:
            out.append("Bullish FVG")
        if p1["high"] < p2["low"]:
            out.append("Bearish FVG")
    if cur["close"] > ph and prev["close"] < prev["open"]:
        out.append("Bullish Order Block")
    if cur["close"] < pl and prev["close"] > prev["open"]:
        out.append("Bearish Order Block")
    seg = h1.tail(30)
    mx, mn = seg["high"].max(), seg["low"].min()
    if len(seg[seg["high"] >= mx*0.999]) >= 2:
        out.append("Double Top - Bearish")
    if len(seg[seg["low"] <= mn*1.001]) >= 2:
        out.append("Double Bottom - Bullish")
    return out


def retest(direction, h1, support, resistance, live):
    last, prev = h1.iloc[-1], h1.iloc[-2]
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
        ok_c = last["close"] > last["open"] and last["close"] > prev["close"]
    else:
        levels = {
            "1H EMA20": float(last["EMA20"]),
            "1H EMA50": float(last["EMA50"]),
            "4H Resistance": resistance
        }
        extreme = float(recent["high"].max())
        ok_c = last["close"] < last["open"] and last["close"] < prev["close"]

    conf, inzone = [], []
    for n, lvl in levels.items():
        if direction == "BUY":
            touched = (lvl-1.5*atr) <= extreme <= (lvl+tol)
            ok = touched and ok_c and close > lvl
        else:
            touched = (lvl-tol) <= extreme <= (lvl+1.5*atr)
            ok = touched and ok_c and close < lvl
        if ok:
            conf.append((abs(close-lvl), n, lvl))
        if abs(live-lvl) <= tol:
            inzone.append((abs(live-lvl), n, lvl))

    out = {
        "state": "FAR", "level_name": None, "level": None,
        "zone_low": None, "zone_high": None,
        "entry": None, "sl": None, "tp": None
    }

    if conf:
        _, n, lvl = min(conf)
        out.update(level_name=n, level=lvl, zone_low=lvl-tol, zone_high=lvl+tol)
        entry = close
        if direction == "BUY":
            sl = extreme-SL_BUFFER_ATR*atr
            risk = entry-sl
        else:
            sl = extreme+SL_BUFFER_ATR*atr
            risk = sl-entry
        if risk <= 0 or risk > MAX_RISK_ATR*atr:
            out["state"] = "RISK"
            return out
        if abs(live-entry) > MAX_MOVE_ATR*atr:
            out["state"] = "MOVED"
            return out
        tp = entry+RR*risk if direction == "BUY" else entry-RR*risk
        out.update(state="CONFIRMED", entry=entry, sl=sl, tp=tp)
        return out

    if inzone:
        _, n, lvl = min(inzone)
        out.update(state="ZONE", level_name=n, level=lvl, zone_low=lvl-tol, zone_high=lvl+tol)
        return out

    n, lvl = min(levels.items(), key=lambda kv: abs(live-kv[1]))
    out.update(level_name=n, level=lvl, zone_low=lvl-tol, zone_high=lvl+tol)
    return out


def trade_plan(direction, h1, live, atr):
    """Entry = current price. SL beyond the last 5 candles' extreme, kept between 1 and MAX_RISK_ATR ATR."""
    recent = h1.tail(5)
    entry = float(live)
    if direction == "BUY":
        risk = entry - (float(recent["low"].min()) - 0.3*atr)
    else:
        risk = (float(recent["high"].max()) + 0.3*atr) - entry
    risk = min(max(risk, 1.0*atr), MAX_RISK_ATR*atr)
    if direction == "BUY":
        return {"state": "TRADE", "entry": entry, "sl": entry-risk, "tp": entry+RR*risk}
    return {"state": "TRADE", "entry": entry, "sl": entry+risk, "tp": entry-RR*risk}


# ---------------- ANALYSIS ----------------

def analyze_coin(name, symbol=None):
    try:
        is_metal = name in METALS
        disp = METALS[name]["symbol"] if is_metal else symbol
        frames, sources = {}, []

        for tf in ["1d", "4h", "1h"]:
            raw, src = get_metal_klines(tf) if is_metal else get_klines(symbol, tf)
            frames[tf] = calc(raw)
            sources.append(src)

        d1, h4, h1 = frames["1d"].iloc[-1], frames["4h"].iloc[-1], frames["1h"].iloc[-1]
        t1d, t4h, t1h = trend_of(d1), trend_of(h4), trend_of(h1)
        candle, atr = float(h1["close"]), float(h1["ATR"])
        rsi, adx = float(h1["RSI"]), float(h1["ADX"])

        if not all(np.isfinite(v) for v in [candle, atr, rsi, adx]) or atr <= 0:
            raise RuntimeError("Indicator data incomplete")

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        if is_metal:
            age = pd.Timestamp.now(tz="UTC") - frames["1h"]["time"].iloc[-1]
            if age > pd.Timedelta(hours=4):
                return {
                    "name": name, "symbol": disp, "signal": "WAIT",
                    "closed": True,
                    "reason": "Market closed / data stale - no signal",
                    "timestamp": ts
                }

        s1, s2, r1, r2 = sr_levels(frames["4h"])
        support, resistance = s1, r1
        pats = candle_pat(frames["1h"])
        structs = structure_signals(frames["1h"], frames["4h"])

        if is_metal:
            live, lsrc = get_metal_live()
        else:
            live, lsrc = get_live_price(symbol)
            if live is None:
                live, lsrc = candle, "Last closed 1H candle (ticker unavailable)"

        buy, sell = 0, 0
        breasons, sreasons = [], []

        if t4h == "UPTREND":
            buy += 1
            breasons.append("4H Trend upar hai")
        if t4h == "DOWNTREND":
            sell += 1
            sreasons.append("4H Trend neeche hai")
        if t1d == "UPTREND":
            buy += 1
            breasons.append("1D Trend upar hai")
        if t1d == "DOWNTREND":
            sell += 1
            sreasons.append("1D Trend neeche hai")
        if t1h == "UPTREND":
            buy += 1
            breasons.append("1H Trend upar hai")
        if t1h == "DOWNTREND":
            sell += 1
            sreasons.append("1H Trend neeche hai")

        for p in pats:
            if "Bullish" in p:
                buy += 1
                breasons.append(f"Candle: {p}")
            if "Bearish" in p:
                sell += 1
                sreasons.append(f"Candle: {p}")

        for s in structs:
            if "Bullish" in s:
                buy += 1
                breasons.append(f"Structure: {s}")
            if "Bearish" in s:
                sell += 1
                sreasons.append(f"Structure: {s}")

        if rsi > 50:
            buy += 1
            breasons.append(f"RSI upar hai ({rsi:.1f})")
        if rsi < 50:
            sell += 1
            sreasons.append(f"RSI neeche hai ({rsi:.1f})")

        if h1["MACD"] > h1["MACD_SIGNAL"]:
            buy += 1
            breasons.append("MACD upar hai")
        else:
            sell += 1
            sreasons.append("MACD neeche hai")

        if pd.notna(h1["OBV"]) and pd.notna(h1["OBV_MA"]):
            if h1["OBV"] > h1["OBV_MA"]:
                buy += 1
                breasons.append("OBV upar hai")
            else:
                sell += 1
                sreasons.append("OBV neeche hai")

        if pd.notna(h1["VOL_AVG"]) and h1["volume"] > h1["VOL_AVG"]:
            breasons.append("Volume Average se upar hai")
        if live >= h1["BB_UPPER"]:
            sreasons.append("Price Bollinger Upper Band par hai")
        if live <= h1["BB_LOWER"]:
            breasons.append("Price Bollinger Lower Band par hai")

        direction = "BUY" if buy > sell else "SELL" if sell > buy else "NONE"
        margin = abs(buy-sell)
        score = max(buy, sell)
        conf = "High" if score >= 7 and margin >= 3 else "Medium" if score >= 5 else "Low"

        signal = "WAIT"
        plan = {"state": "NONE"}
        wait_dir = direction if direction != "NONE" else "NONE"
        opp = sell if direction == "BUY" else buy

        if direction == "NONE":
            reason = "WAIT - BUY aur SELL dono barabar hain, koi saaf direction nahi"
        else:
            plan = retest(direction, frames["1h"], support, resistance, live)
            lv = (
                f"{plan['level_name']} ({plan['level']:.8g})"
                if plan.get("level") is not None else "Level nahi mila"
            )
            st = plan["state"]
            score_ok = score >= MIN_SCORE and margin >= MIN_MARGIN
            adx_ok = adx >= 12
            rsi_ok = 20 < rsi < 80

            if score_ok and adx_ok and rsi_ok:
                # ---- enough evidence: give the signal (retest is a bonus, not a must) ----
                signal = direction
                if st == "CONFIRMED":
                    plan["retest_ok"] = True
                    reason = (
                        f"{direction} CONFIRMED - Retest {lv} se bounce confirm, "
                        f"RR 1:{RR:g}, Confidence {conf}"
                    )
                else:
                    plan = trade_plan(direction, frames["1h"], live, atr)
                    plan["retest_ok"] = False
                    reason = (
                        f"{direction} - Saboot kaafi hain (score {score} vs {opp}). "
                        f"Retest ka wait nahi kiya, Entry abhi ki price par, Confidence {conf}"
                    )
            else:
                # ---- not enough evidence: WAIT with a possible plan ----
                why = []
                if score < MIN_SCORE or margin < MIN_MARGIN:
                    why.append(f"{direction} ka score {score} vs {opp} hai (chahiye {MIN_SCORE}+ aur {MIN_MARGIN} ka farq)")
                if not adx_ok:
                    why.append(f"ADX me taqat nahi ({adx:.1f})")
                if not rsi_ok:
                    why.append(f"RSI bahut zyada upar/neeche ({rsi:.1f})")
                if st == "ZONE":
                    why.append(f"Price Retest Zone {lv} ke andar hai, bounce wali candle ka wait karo")
                elif plan.get("level") is not None:
                    why.append(f"Retest Point {lv} hai, Price abhi {live:.8g} par hai")
                reason = f"WAIT - Main Trend {direction}. " + " | ".join(why)
                if plan.get("level") is not None:
                    pe = plan["level"]
                    ps = pe-SL_BUFFER_ATR*atr if direction == "BUY" else pe+SL_BUFFER_ATR*atr
                    pr = abs(pe-ps)
                    pt = pe+RR*pr if direction == "BUY" else pe-RR*pr
                    plan.update(pot_entry=pe, pot_sl=ps, pot_tp=pt)
                    upsert_wait(name, {
                        "direction": direction,
                        "level": float(plan["level"]),
                        "zone_low": float(plan["zone_low"]) if plan.get("zone_low") is not None else None,
                        "zone_high": float(plan["zone_high"]) if plan.get("zone_high") is not None else None,
                        "pot_entry": float(pe),
                        "pot_sl": float(ps),
                        "pot_tp": float(pt),
                        "support": float(support),
                        "resistance": float(resistance),
                        "s1": float(s1), "s2": float(s2),
                        "r1": float(r1), "r2": float(r2),
                        "time": ts,
                        "created": datetime.now(timezone.utc).isoformat()
                    })

        macd_s = "Bullish" if h1["MACD"] > h1["MACD_SIGNAL"] else "Bearish"
        obv_s = (
            "Rising" if h1["OBV"] > h1["OBV_MA"] else "Falling"
        ) if pd.notna(h1["OBV"]) and pd.notna(h1["OBV_MA"]) else "Unavailable"
        vol_s = (
            "Above Average" if h1["volume"] > h1["VOL_AVG"] else "Below Average"
        ) if pd.notna(h1["VOL_AVG"]) else "Unavailable"
        boll = (
            "Upper Band" if live >= h1["BB_UPPER"]
            else "Lower Band" if live <= h1["BB_LOWER"]
            else "Inside Bands"
        )
        src = sources[0] if len(set(sources)) == 1 else "Mixed sources"

        return {
            "name": name, "symbol": disp, "signal": signal,
            "direction": direction, "wait_dir": wait_dir,
            "price": live, "candle_price": candle, "live_source": lsrc,
            "plan": plan, "trend_1d": t1d, "trend_4h": t4h, "trend_1h": t1h,
            "rsi": rsi, "adx": adx, "macd": macd_s, "obv": obv_s,
            "volume": vol_s, "bollinger": boll,
            "pattern": " | ".join(pats) if pats else "No Clear Pattern",
            "structure": " | ".join(structs) if structs else "No confirmed break",
            "support": support, "resistance": resistance,
            "s1": s1, "s2": s2, "r1": r1, "r2": r2,
            "atr": atr, "reason": reason,
            "buy_reasons": breasons, "sell_reasons": sreasons,
            "confidence": conf, "score": score, "margin": margin,
            "buy_score": buy, "sell_score": sell,
            "source": src, "timestamp": ts
        }

    except Exception as exc:
        logging.error("Analysis failed %s: %s", name, safe(exc))
        return {
            "name": name,
            "symbol": symbol or METALS.get(name, {}).get("symbol", "N/A"),
            "signal": "DATA ERROR",
            "error": safe(exc)
        }


def fmt(v):
    return "Not available" if v is None else f"{v:.8g}"


def format_result(r):
    if r.get("error"):
        return (
            f"⚠️ DATA ERROR — {r['name']} ({r['symbol']})\n"
            f"Reason: {r['error']}\n"
            "This is NOT a market WAIT signal."
        )

    if r.get("closed"):
        return (
            f"⚪ CLOSED — {r['name']} ({r['symbol']})\n"
            f"{r['reason']}\nUpdated: {r['timestamp']}"
        )

    sig = r["signal"]
    dot = {"BUY": "🟢", "SELL": "🔴", "WAIT": "🟡"}.get(sig, "🟡")
    plan = r.get("plan", {})

    zone = (
        f"{plan.get('level_name')}: {fmt(plan.get('zone_low'))} - {fmt(plan.get('zone_high'))}"
        if plan.get("zone_low") is not None else "Not available"
    )

    if sig in ("BUY", "SELL"):
        rs = r.get("buy_reasons") if sig == "BUY" else r.get("sell_reasons")
        rtxt = "\n".join([f"✅ {x}" for x in rs]) if rs else "No reasons"

        lv = (
            f"Abhi Price: {fmt(r['price'])}\n"
            f"Support S1: {fmt(r['s1'])} | S2: {fmt(r['s2'])}\n"
            f"Resistance R1: {fmt(r['r1'])} | R2: {fmt(r['r2'])}\n"
            + (f"Retest Level: {zone}\n" if plan.get("retest_ok") else "Retest: nahi hua (trend + indicators se signal)\n")
            + f"Entry: {fmt(plan.get('entry'))}\n"
            f"SL: {fmt(plan.get('sl'))}\n"
            f"TP: {fmt(plan.get('tp'))}\n"
            f"Confidence: {r.get('confidence')} | "
            f"Score BUY {r.get('buy_score')} - SELL {r.get('sell_score')}"
        )

    else:
        wd = r.get("wait_dir", "NONE")
        wtxt = "BUY KA WAIT" if wd == "BUY" else "SELL KA WAIT" if wd == "SELL" else "NEUTRAL WAIT"
        rs = r.get("buy_reasons") if wd == "BUY" else r.get("sell_reasons") if wd == "SELL" else []
        rtxt = "\n".join([f"✅ {x}" for x in rs]) if rs else "Koi saaf saboot nahi"
        pe = plan.get("pot_entry") or plan.get("level")
        ps = plan.get("pot_sl")
        pt = plan.get("pot_tp")

        lv = (
            f"Abhi Price: {fmt(r['price'])}\n"
            f"Support S1: {fmt(r['s1'])} | S2: {fmt(r['s2'])}\n"
            f"Resistance R1: {fmt(r['r1'])} | R2: {fmt(r['r2'])}\n"
            f"Retest Zone: {zone}\n"
            f"Possible Entry: {fmt(pe)}\n"
            f"Possible SL: {fmt(ps)}\n"
            f"Possible TP: {fmt(pt)}\n"
            f"Confidence: {r.get('confidence')} | "
            f"Score BUY {r.get('buy_score')} - SELL {r.get('sell_score')}\n"
            f"WAIT Type: {wtxt}"
        )

    return (
        f"{dot} {sig} — {r['name']} ({r['symbol']})\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{lv}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Wajah:\n{rtxt}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"1D: {r['trend_1d']} | 4H: {r['trend_4h']} | 1H: {r['trend_1h']}\n"
        f"RSI: {r['rsi']:.1f} | ADX: {r['adx']:.1f} | "
        f"MACD: {r['macd']} | OBV: {r['obv']}\n"
        f"Volume: {r['volume']} | Bollinger: {r['bollinger']}\n"
        f"Candle: {r['pattern']}\n"
        f"Structure: {r['structure']}\n"
        f"Data: {r['source']} | Live: {r['live_source']}\n"
        f"Reason: {r['reason']}\n"
        f"Updated: {r['timestamp']}\n"
        "Preliminary scanner only — manual checklist review required. "
        "No automatic order execution."
    )


# ---------------- SCANNING ----------------

def check_waits(chat_id=None):
    """Runs every 15 minutes: re-check each pending WAIT."""
    for name in list(load_waits().keys()):
        try:
            sym = COINS.get(name) if name not in METALS else None
            if name not in METALS and not sym:
                continue
            res = analyze_coin(name, sym)
            handle_wait_result(name, res, chat_id)
        except Exception:
            logging.exception("check_waits %s", name)


def scan_all(chat_id=None):
    results = []

    for name, sym in list(COINS.items()):
        if name in METALS or sym in BLOCKED:
            continue

        r = analyze_coin(name, sym)
        handle_wait_result(name, r, chat_id)

        if r.get("signal") == "DATA ERROR" and "GRAM" in name:
            results.append(
                f"⚠️ GRAM skipped — Binance market not verified / no data. "
                f"{r.get('error', '')}"
            )
        else:
            results.append(format_result(r))

        time.sleep(0.15)

    for name in METALS.keys():
        r = analyze_coin(name, None)
        handle_wait_result(name, r, chat_id)
        results.append(format_result(r))

    hdr = (
        "📊 KHATTAK LIVE MARKET SCANNER\n"
        "Hourly market report\n"
        "Crypto + Gold — no automatic trading.\n\n"
    )

    if not results:
        results.append("No coins available.")

    send_long(hdr + "\n\n".join(results), chat_id=chat_id)


def find_symbol(t):
    t = t.upper().strip()

    if t in METALS or t in BLOCKED:
        return None

    s = t if t.endswith("USDT") else t + "USDT"

    if s in BLOCKED:
        return None

    try:
        if resolve_binance(s):
            return s
    except Exception:
        pass

    try:
        if resolve_bybit(s):
            return s
    except Exception:
        pass

    return None


def add_coin(t):
    t = t.upper().strip()

    if not t or len(t) > 30:
        return "Invalid coin name."
    if t in METALS:
        return f"{t} already included as metal."
    if t in BLOCKED:
        return "Use GOLD for metals."
    if t in COINS:
        return f"{t} is already in the list."

    s = find_symbol(t)

    if not s:
        return f"Could not verify a live USDT market for {t}."

    try:
        get_klines(s, "1h", 120)
    except Exception:
        return f"Live chart data unavailable for {t}."

    COINS[t] = s
    save_coins(COINS)
    return f"✅ {t} added successfully as {s}."


def remove_coin(t):
    t = t.upper().strip()

    if t in METALS:
        return "GOLD is built-in and cannot be removed."

    for n, s in list(COINS.items()):
        if n == t or s == t:
            del COINS[n]
            save_coins(COINS)
            remove_wait(n)
            return f"✅ {n} removed."

    return f"{t} is not in the list."


def handle_command(text, chat_id=None):
    p = text.strip().split()

    if not p:
        return "Use /help to see commands."

    cmd = p[0].split("@")[0].lower()

    if cmd == "/start":
        return (
            "✅ KHATTAK LIVE SCANNER is online.\n"
            "Trend: 1D + 4H + 1H | S/R: 4H | Entry: 1H\n"
            "Crypto + Gold configured.\n"
            "Use /help for commands."
        )

    if cmd == "/help":
        return (
            "KHATTAK LIVE SCANNER COMMANDS\n\n"
            "/start — Bot status\n"
            "/scan — Scan all\n"
            "/scan BTC — Scan one coin\n"
            "/scan GOLD — Scan Gold\n"
            "/check BTC — Scan one coin\n"
            "/add PEPE — Add a coin\n"
            "/remove PEPE — Remove a coin\n"
            "/list — Show coin list\n"
            "/waits — Show pending WAITs\n"
            "/help — Help\n\n"
            "Signals: 🟢 BUY / 🔴 SELL = enough evidence (retest is a bonus)\n"
            "🟡 WAIT = not enough evidence, possible direction + zone given\n"
            "Bot does not execute trades."
        )

    if cmd == "/list":
        return "\n".join(
            ["📋 COIN LIST"]
            + [f"{k}: {v}" for k, v in COINS.items()]
            + [f"{k}: {v['symbol']} ({v['provider']})" for k, v in METALS.items()]
        )

    if cmd == "/waits":
        w = load_waits()
        if not w:
            return "No pending WAITs."
        return "\n\n".join([
            f"🟡 WAIT — {k}\n"
            f"Direction: {v.get('direction')}\n"
            f"Level: {v.get('level')}\n"
            f"Possible Entry: {v.get('pot_entry')} | "
            f"SL: {v.get('pot_sl')} | TP: {v.get('pot_tp')}\n"
            f"Since: {v.get('time')}"
            for k, v in w.items()
        ])

    if cmd == "/add":
        return add_coin(p[1]) if len(p) > 1 else "Usage: /add PEPE"

    if cmd == "/remove":
        return remove_coin(p[1]) if len(p) > 1 else "Usage: /remove PEPE"

    if cmd in ("/scan", "/check"):
        if cmd == "/check" and len(p) < 2:
            return "Usage: /check BTC"

        if len(p) >= 2:
            tk = p[1].upper().strip()

            if tk in BLOCKED:
                return "Use GOLD."

            def do_single():
                tg = chat_id or CHAT_ID

                if tk in METALS:
                    res = analyze_coin(tk, None)
                else:
                    s = COINS.get(tk) or find_symbol(tk)

                    if not s:
                        send_message(
                            f"Could not verify a live USDT market for {tk}.",
                            chat_id=tg
                        )
                        return

                    res = analyze_coin(tk, s)

                handle_wait_result(tk, res, tg)
                send_message(format_result(res), chat_id=tg)

            threading.Thread(target=do_single, daemon=True).start()
            return f"🔄 Scanning {tk}... Result will arrive shortly."

        threading.Thread(
            target=lambda: scan_all(chat_id),
            daemon=True
        ).start()

        return "🔄 Full market scan started. Gold takes ~1 minute (API limit)."

    return "Unknown command. Use /help."


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
        d = request.get_json(force=True, silent=True) or {}
        msg = d.get("message") or {}
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
    scan_all, "cron", minute=1,
    id="hourly_market_scan", replace_existing=True,
    max_instances=1, coalesce=True
)
scheduler.add_job(
    check_waits, "interval", minutes=15,
    id="check_waits", replace_existing=True,
    max_instances=1, coalesce=True
)
scheduler.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
