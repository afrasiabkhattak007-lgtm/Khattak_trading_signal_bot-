
def candle_pattern(df):
    """
    Detects 20 candlestick patterns using completed candles only.
    Requires OHLC columns: open, high, low, close.
    """

    if df is None or len(df) < 5:
        return "Insufficient Candle Data"

    # Last three completed candles
    a = df.iloc[-1]
    b = df.iloc[-2]
    c = df.iloc[-3]

    def candle_parts(x):
        body = abs(x["close"] - x["open"])
        rng = max(x["high"] - x["low"], 1e-12)
        upper = x["high"] - max(x["open"], x["close"])
        lower = min(x["open"], x["close"]) - x["low"]

        return body, rng, upper, lower

    body, rng, upper, lower = candle_parts(a)
    b_body, b_rng, b_upper, b_lower = candle_parts(b)
    c_body, c_rng, c_upper, c_lower = candle_parts(c)

    bullish = a["close"] > a["open"]
    bearish = a["close"] < a["open"]

    b_bullish = b["close"] > b["open"]
    b_bearish = b["close"] < b["open"]

    c_bullish = c["close"] > c["open"]
    c_bearish = c["close"] < c["open"]

    # Previous trend approximation
    previous = df.iloc[-5:-2]
    previous_down = (
        previous["close"].iloc[-1] < previous["close"].iloc[0]
    )
    previous_up = (
        previous["close"].iloc[-1] > previous["close"].iloc[0]
    )

    # 1. Hammer
    hammer = (
        lower >= max(body * 2, rng * 0.55)
        and upper <= rng * 0.15
        and body / rng <= 0.35
        and previous_down
    )

    # 2. Hanging Man
    hanging_man = (
        lower >= max(body * 2, rng * 0.55)
        and upper <= rng * 0.15
        and body / rng <= 0.35
        and previous_up
    )

    # 3. Inverted Hammer
    inverted_hammer = (
        upper >= max(body * 2, rng * 0.55)
        and lower <= rng * 0.15
        and body / rng <= 0.35
        and previous_down
    )

    # 4. Shooting Star
    shooting_star = (
        upper >= max(body * 2, rng * 0.55)
        and lower <= rng * 0.15
        and body / rng <= 0.35
        and previous_up
    )

    # 5. Doji
    doji = body / rng <= 0.10

    # 6. Gravestone Doji
    gravestone_doji = (
        body / rng <= 0.10
        and upper / rng >= 0.65
        and lower / rng <= 0.10
    )

    # 7. Dragonfly Doji
    dragonfly_doji = (
        body / rng <= 0.10
        and lower / rng >= 0.65
        and upper / rng <= 0.10
    )

    # 8. Spinning Top
    spinning_top = (
        body / rng <= 0.35
        and upper > body
        and lower > body
        and not doji
    )

    # 9. Bullish Engulfing
    bullish_engulfing = (
        b_bearish and bullish
        and a["open"] <= b["close"]
        and a["close"] >= b["open"]
        and body > b_body
    )

    # 10. Bearish Engulfing
    bearish_engulfing = (
        b_bullish and bearish
        and a["open"] >= b["close"]
        and a["close"] <= b["open"]
        and body > b_body
    )

    # 11. Bullish Harami
    bullish_harami = (
        b_bearish and bullish
        and a["open"] > b["close"]
        and a["close"] < b["open"]
        and body < b_body
    )

    # 12. Bearish Harami
    bearish_harami = (
        b_bullish and bearish
        and a["open"] > b["close"]
        and a["close"] < b["open"]
        and body < b_body
    )

    # 13. Morning Star
    morning_star = (
        c_bearish
        and c_body / c_rng >= 0.50
        and b_body / b_rng <= 0.30
        and bullish
        and a["close"] > (c["open"] + c["close"]) / 2
    )

    # 14. Evening Star
    evening_star = (
        c_bullish
        and c_body / c_rng >= 0.50
        and b_body / b_rng <= 0.30
        and bearish
        and a["close"] < (c["open"] + c["close"]) / 2
    )

    # 15. Three White Soldiers
    three_white_soldiers = (
        len(df) >= 3
        and all(df["close"].iloc[-i] > df["open"].iloc[-i]
                for i in [1, 2, 3])
        and df["close"].iloc[-1] > df["close"].iloc[-2]
        and df["close"].iloc[-2] > df["close"].iloc[-3]
    )

    # 16. Three Black Crows
    three_black_crows = (
        len(df) >= 3
        and all(df["close"].iloc[-i] < df["open"].iloc[-i]
                for i in [1, 2, 3])
        and df["close"].iloc[-1] < df["close"].iloc[-2]
        and df["close"].iloc[-2] < df["close"].iloc[-3]
    )

    # 17. Piercing Line
    piercing_line = (
        b_bearish and bullish
        and a["open"] < b["close"]
        and a["close"] > (b["open"] + b["close"]) / 2
        and a["close"] < b["open"]
    )

    # 18. Dark Cloud Cover
    dark_cloud_cover = (
        b_bullish and bearish
        and a["open"] > b["close"]
        and a["close"] < (b["open"] + b["close"]) / 2
        and a["close"] > b["open"]
    )

    # 19. Tweezer Top
    tweezer_top = (
        b_bullish and bearish
        and abs(a["high"] - b["high"]) <= b_rng * 0.10
        and previous_up
    )

    # 20. Tweezer Bottom
    tweezer_bottom = (
        b_bearish and bullish
        and abs(a["low"] - b["low"]) <= b_rng * 0.10
        and previous_down
    )

    patterns = []

    if hammer:
        patterns.append("Hammer - Bullish")

    if hanging_man:
        patterns.append("Hanging Man - Bearish")

    if inverted_hammer:
        patterns.append("Inverted Hammer - Bullish")

    if shooting_star:
        patterns.append("Shooting Star - Bearish")

    if gravestone_doji:
        patterns.append("Gravestone Doji - Bearish")

    if dragonfly_doji:
        patterns.append("Dragonfly Doji - Bullish")

    if doji and not gravestone_doji and not dragonfly_doji:
        patterns.append("Doji - Neutral")

    if spinning_top:
        patterns.append("Spinning Top - Neutral")

    if bullish_engulfing:
        patterns.append("Bullish Engulfing")

    if bearish_engulfing:
        patterns.append("Bearish Engulfing")

    if bullish_harami:
        patterns.append("Bullish Harami")

    if bearish_harami:
        patterns.append("Bearish Harami")

    if morning_star:
        patterns.append("Morning Star - Bullish")

    if evening_star:
        patterns.append("Evening Star - Bearish")

    if three_white_soldiers:
        patterns.append("Three White Soldiers - Bullish")

    if three_black_crows:
        patterns.append("Three Black Crows - Bearish")

    if piercing_line:
        patterns.append("Piercing Line - Bullish")

    if dark_cloud_cover:
        patterns.append("Dark Cloud Cover - Bearish")

    if tweezer_top:
        patterns.append("Tweezer Top - Bearish")

    if tweezer_bottom:
        patterns.append("Tweezer Bottom - Bullish")

    if patterns:
        return " | ".join(patterns)

    return "No Clear Pattern"
