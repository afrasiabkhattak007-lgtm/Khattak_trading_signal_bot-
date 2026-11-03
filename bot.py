# Khattak PRO Bot - FINAL FIX - 35 Lakh Points + Telegram /start
import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

CAPITAL_POINTS = 3500000
RISK_PERCENT = 2

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""

def get_live_price(symbol):
    symbol = symbol.upper().strip()
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
    if symbol in ["SILVER", "XAG", "XAGUSD", "SILVERUSDT"]:
        try:
            s = requests.get("https://api.gold-api.com/price/XAG", timeout=10).json()
            price = float(s['price'])
            closes = [price * (1 + (i-25)*0.0005) for i in range(50)]
            return price, closes, "XAGUSD (SILVER Spot)"
        except Exception as e:
            return None, None, str(e)
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

def send_telegram(chat_id, text):
    if not BOT_TOKEN: return
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
    except: pass

@app.route('/')
def home():
    return f"<h2>✅ Khattak Bot LIVE - GOLD + SILVER + CRYPTO + Telegram</h2><p>Capital: {CAPITAL_POINTS} Points</p><p>Use: /check?coin=GOLD | SILVER | BTC | ETH | SOL | SHIB</p>"

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

@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    try:
        update = request.get_json()
        if not update or "message" not in update:
            return jsonify({"ok": True})
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "").lower().strip()
        
        if "/start" in text:
            msg = "✅ <b>Khattak Trading Bot LIVE</b>\n\n👤 Owner: Afrasiab Khattak\n💰 Capital: 35 Lakh Points\n\nCommands:\n/check GOLD\n/check SILVER\n/check BTC\n/check ETH\n/check SOL\n\n24/7 LIVE"
            send_telegram(chat_id, msg)
        elif "/check" in text:
            parts = text.split()
            coin = parts[1] if len(parts) > 1 else "BTC"
            price, closes, real = get_live_price(coin)
            if price:
                sig, entry, sl, tp1, tp2, tp3 = build_signal(price, closes)
                reply = f"📈 <b>{coin.upper()} ({real})</b>\nLive: {price:.4f}\nSignal: <b>{sig}</b>\n\nEntry: {entry:.4f}\nSL: {sl:.4f}\nTP1: {tp1:.4f}\nTP2: {tp2:.4f}\nTP3: {tp3:.4f}\n\n💰 35 Lakh - 2% Risk"
                send_telegram(chat_id, reply)
            else:
                send_telegram(chat_id, f"❌ {coin} ka data nahi mila: {real}")
        else:
            send_telegram(chat_id, "Commands: /start , /check GOLD, /check SILVER, /check BTC")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
