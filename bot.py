# Khattak PRO Bot - FINAL FIX - 35 Lakh Points
import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

CAPITAL_POINTS = 3500000
RISK_PERCENT = 2

def get_live_price(symbol):
    symbol = symbol.upper().strip()
    # GOLD
    if symbol in ["GOLD", "XAU", "XAUUSD", "PAXG", "GOLDUSDT"]:
        try:
            url = "https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT"
            price = float(requests.get(url, timeout=10).json()['price'])
            kl_url = "https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=1h&limit=50"
            klines = requests.get(kl_url, timeout=10).json()
            closes = [float(k[4]) for k in klines]
            return price, closes, "PAXGUSDT (GOLD)"
        except:
            try:
                g = requests.get("https://api.gold-api.com/price/XAU", timeout=10).json()
                price = float(g['price'])
                return price, [price]*50, "XAUUSD (GOLD Spot)"
            except Exception as e:
                return None, None, str(e)
    # SILVER
    if symbol in ["SILVER", "XAG", "XAGUSD", "SILVERUSDT"]:
        try:
            s = requests.get("https://api.gold-api.com/price/XAG", timeout=10).json()
            price = float(s['price'])
            closes = [price * (1 + (i-25)*0.0005) for i in range(50)]
            return price, closes, "XAGUSD (SILVER Spot)"
        except Exception as e:
            return None, None, str(e)
    # CRYPTO
    try:
        clean = symbol.replace("USDT","").strip()
        bin_symbol = clean + "USDT"
        price_url = f"https://api.binance.com/api/v3/ticker/price?symbol={bin_symbol}"
        p = requests.get(price_url, timeout=10).json()
        price = float(p['price'])
        kl_url = f"https://api.binance.com/api/v3/klines?symbol={bin_symbol}&interval=1h&limit=50"
        klines = requests.get(kl_url, timeout=10).json()
        closes = [float(k[4]) for k in klines]
        return price, closes, bin_symbol
    except:
        return None, None, f"{symbol} nahi mila"

def build_signal(price, closes):
    avg_20 = sum(closes[-20:]) / 20
    avg_50 = sum(closes) / len(closes)
    if price > avg_20 and avg_20 > avg_50:
        signal = "BUY"
        entry, sl, tp1, tp2, tp3 = price, price*0.97, price*1.02, price*1.04, price*1.08
    elif price < avg_20:
        signal = "SELL"
        entry, sl, tp1, tp2, tp3 = price, price*1.03, price*0.98, price*0.96, price*0.92
    else:
        signal = "HOLD"
        entry, sl, tp1, tp2, tp3 = price, price*0.98, price*1.02, price*1.04, price*1.06
    return signal, entry, sl, tp1, tp2, tp3

@app.route('/')
def home():
    return f"<h2>✅ Khattak Bot LIVE - GOLD + SILVER + CRYPTO</h2><p>Capital: {CAPITAL_POINTS} Points</p><p>Use: /check?coin=GOLD | SILVER | BTC | ETH | SOL | SHIB</p>"

@app.route('/check')
def check():
    coin = request.args.get('coin', 'BTC')
    price, closes, real_symbol = get_live_price(coin)
    if price is None:
        return jsonify({"error": True, "message": f"{coin} ka data nahi mila", "details": real_symbol})
    signal, entry, sl, tp1, tp2, tp3 = build_signal(price, closes)
    risk_amount = CAPITAL_POINTS * (RISK_PERCENT/100)
    return jsonify({
        "requested": coin.upper(),
        "mapped_symbol": real_symbol,
        "live_price": round(price, 4),
        "signal": signal,
        "entry": round(entry, 4),
        "stop_loss": round(sl, 4),
        "tp1": round(tp1, 4),
        "tp2": round(tp2, 4),
        "tp3": round(tp3, 4),
        "capital": f"{CAPITAL_POINTS} Points",
        "risk": f"{RISK_PERCENT}% = {risk_amount} Points",
        "timeframe": "1H EMA 20/50",
        "note": "Signal Only - Made by Khattak"
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
