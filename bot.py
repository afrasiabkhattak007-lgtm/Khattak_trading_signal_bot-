# ---------------- CANDLE PATTERNS ----------------

def candle_pattern(df):
    if len(df) < 3:
        return "No clear pattern"

    a = df.iloc[-1]  # aaj wali candle
    b = df.iloc[-2]  # kal wali candle
    c = df.iloc[-3]  # parson wali candle

    def body(x):
        return abs(x["close"] - x["open"])
    def candle_range(x):
        return max(x["high"] - x["low"], 1e-12)
    def upper_wick(x):
        return x["high"] - max(x["open"], x["close"])
    def lower_wick(x):
        return min(x["open"], x["close"]) - x["low"]
    def is_bullish(x):
        return x["close"] > x["open"]
    def is_bearish(x):
        return x["close"] < x["open"]

    # --- 1. Engulfing ---
    bullish_engulfing = (
        is_bearish(b) and is_bullish(a)
        and a["open"] <= b["close"]
        and a["close"] >= b["open"]
    )
    bearish_engulfing = (
        is_bullish(b) and is_bearish(a)
        and a["open"] >= b["close"]
        and a["close"] <= b["open"]
    )
    if bullish_engulfing:
        return "Bullish Engulfing"
    if bearish_engulfing:
        return "Bearish Engulfing"

    # --- 2. Harami ---
    bullish_harami = (
        is_bearish(b) and is_bullish(a)
        and a["open"] > b["close"]
        and a["close"] < b["open"]
        and body(a) < body(b)
    )
    bearish_harami = (
        is_bullish(b) and is_bearish(a)
        and a["open"] < b["close"]
        and a["close"] > b["open"]
        and body(a) < body(b)
    )
    if bullish_harami:
        return "Bullish Harami"
    if bearish_harami:
        return "Bearish Harami"

    # --- 3. Morning Star / Evening Star ---
    morning_star = (
        is_bearish(c) and body(c) > candle_range(c) * 0.6
        and body(b) < candle_range(b) * 0.3
        and is_bullish(a) and a["close"] > (c["open"] + c["close"]) / 2
    )
    evening_star = (
        is_bullish(c) and body(c) > candle_range(c) * 0.6
        and body(b) < candle_range(b) * 0.3
        and is_bearish(a) and a["close"] < (c["open"] + c["close"]) / 2
    )
    if morning_star:
        return "Morning Star"
    if evening_star:
        return "Evening Star"

    # --- 4. Three White Soldiers / Three Black Crows ---
    d = df.iloc[-4] if len(df) >= 4 else b
    three_white = (
        is_bullish(c) and is_bullish(b) and is_bullish(a)
        and b["close"] > c["close"] and a["close"] > b["close"]
    )
    three_black = (
        is_bearish(c) and is_bearish(b) and is_bearish(a)
        and b["close"] < c["close"] and a["close"] < b["close"]
    )
    if three_white:
        return "Three White Soldiers"
    if three_black:
        return "Three Black Crows"

    # --- 5. Piercing Line / Dark Cloud Cover ---
    piercing = (
        is_bearish(b) and is_bullish(a)
        and a["open"] < b["low"]
        and a["close"] > (b["open"] + b["close"]) / 2
    )
    dark_cloud = (
        is_bullish(b) and is_bearish(a)
        and a["open"] > b["high"]
        and a["close"] < (b["open"] + b["close"]) / 2
    )
    if piercing:
        return "Piercing Line"
    if dark_cloud:
        return "Dark Cloud Cover"

    # --- 6. Hammer / Hanging Man / Inverted Hammer / Shooting Star ---
    # Hammer aur Hanging Man ki shakal ek jaisi hoti hai, farq trend se pata chalta hai
    # Neeche wali lambi wick + chhota body
    if lower_wick(a) > body(a) * 2 and upper_wick(a) < body(a):
        # Agar pehle downtrend tha to Hammer, uptrend tha to Hanging Man
        if c["close"] > b["close"] > a["close"]:
            return "Hammer"
        elif c["close"] < b["close"] < a["close"]:
            return "Hanging Man"
        else:
            return "Hammer / Hanging Man"

    if upper_wick(a) > body(a) * 2 and lower_wick(a) < body(a):
        if c["close"] < b["close"] < a["close"]:
            return "Inverted Hammer"
        elif c["close"] > b["close"] > a["close"]:
            return "Shooting Star"
        else:
            return "Shooting Star / Inverted Hammer"

    # --- 7. Doji Types ---
    if body(a) / candle_range(a) < 0.1:
        # Gravestone - upar lambi wick, neeche kuch nahi
        if upper_wick(a) > candle_range(a) * 0.7 and lower_wick(a) < candle_range(a) * 0.1:
            return "Gravestone Doji"
        # Dragonfly - neeche lambi wick, upar kuch nahi
        if lower_wick(a) > candle_range(a) * 0.7 and upper_wick(a) < candle_range(a) * 0.1:
            return "Dragonfly Doji"
        return "Doji"

    return "No clear pattern"
