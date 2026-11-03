import os, requests
from flask import Flask, request, jsonify
app = Flask(__name__)
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

def set_webhook():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if url and BOT_TOKEN:
        try:
            requests.get(f"{API}/setWebhook?url={url}/webhook&drop_pending_updates=True", timeout=15)
        except: pass
set_webhook()

def send(chat_id, text):
    requests.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)

# --- 16 COINS FINAL LIST ---
SYMBOL_MAP = {
    "ETH/USDT": "ETHUSDT", "ETH": "ETHUSDT",
    "BNB/USDT": "BNBUSDT", "BNB": "BNBUSDT",
    "SOL/USDT": "SOLUSDT", "SOL": "SOLUSDT",
    "TRX/USDT": "TRXUSDT", "TRX": "TRXUSDT",
    "TON/USDT": "TONUSDT", "TON": "TONUSDT",
    "TRUMP/USDT": "TRUMPUSDT", "TRUMP": "TRUMPUSDT",
    "BANANA GUN/USDT": "BANANAUSDT", "BANANA GUN": "BANANAUSDT", "BANANA": "BANANAUSDT",
    "BTC/USDT": "BTCUSDT", "BTC": "BTCUSDT",
    "GRAM/USDT": "GRAMUSDT", "GRAM": "GRAMUSDT",
    "SUI/USDT": "SUIUSDT", "SUI": "SUIUSDT",
    "DOGE/USDT": "DOGEUSDT", "DOGE": "DOGEUSDT",
    "LA/USDT": "LAUSDT", "LA": "LAUSDT",
    "SHIB/USDT": "SHIBUSDT", "SHIB": "SHIBUSDT",
    "NOT/USDT": "NOTUSDT", "NOT": "NOTUSDT",
    "SILVER": "SILVER", "XAG": "SILVER",
    "GOLD": "GOLD", "XAU": "GOLD",
}

def get_signal_and_price(sym):
    try:
        # GOLD / SILVER
        if sym == "GOLD":
            r = requests.get("https://api.gold-api.com/price/XAU", timeout=5).json()
            price = float(r['price'])
            change = float(r.get('ch', 0))
            if change > 5: signal, color = "BUY", "🟢 GREEN"
            elif change < -5: signal, color = "SELL", "🔴 RED"
            else: signal, color = "WAIT", "🟡 YELLOW"
            return price, signal, color
            
        if sym == "SILVER":
            r = requests.get("https://api.gold-api.com/price/XAG", timeout=5).json()
            price = float(r['price'])
            change = float(r.get('ch', 0))
            if change > 0.2: signal, color = "BUY", "🟢 GREEN"
            elif change < -0.2: signal, color = "SELL", "🔴 RED"
            else: signal, color = "WAIT", "🟡 YELLOW"
            return price, signal, color

        # CRYPTO - Binance se EMA Signal
        price_data = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={sym}", timeout=5).json()
        price = float(price_data['price'])
        
        klines = requests.get(f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1h&limit=50", timeout=5).json()
        closes = [float(k[4]) for k in klines]
        
        # EMA 20 vs EMA 50 ka simple formula
        ema20 = sum(closes[-20:]) / 20
        ema50 = sum(closes[-50:]) / 50
        
        if price > ema20 and ema20 > ema50:
            signal, color = "BUY", "🟢 GREEN LIGHT"
        elif price < ema20 and ema20 < ema50:
            signal, color = "SELL", "🔴 RED LIGHT"
        else:
            signal, color = "WAIT", "🟡 YELLOW LIGHT"
            
        return price, signal, color
    except Exception as e:
        print(e)
        return None, None, None

def find_symbol(user_text):
    txt = user_text.upper().replace("/CHECK", "").strip()
    txt_ns = txt.replace(" ", "").replace("/", "")
    for key, val in SYMBOL_MAP.items():
        key_ns = key.replace(" ", "").replace("/", "")
        if key in txt or key_ns == txt_ns:
            return val, key
    return None, None

@app.route('/')
def home():
    return "✅ Khattak Bot LIVE - 16 Coins + Traffic Lights"

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    if not data or "message" not in data:
        return jsonify(ok=True)
    chat_id = data["message"]["chat"]["id"]
    txt = data["message"].get("text","").strip()
    
    if "/start" in txt.lower():
        send(chat_id, """✅ <b>Khattak Bot - 16 Coins + Signals</b>
💰 Capital: 35 Lakh

<b>Commands:</b>
/check ETH/USDT
/check BNB/USDT
/check SOL/USDT
/check TRX/USDT
/check TON/USDT
/check TRUMP/USDT
/check BANANA GUN/USDT
/check BTC/USDT
/check GRAM/USDT
/check SUI/USDT
/check DOGE/USDT
/check LA/USDT
/check SHIB/USDT
/check NOT/USDT
/check SILVER
/check GOLD

🟢 BUY = Green Light
🔴 SELL = Red Light
🟡 WAIT = Yellow Light""")
        
    elif "/check" in txt.lower():
        bin_sym, orig = find_symbol(txt)
        if not bin_sym:
            send(chat_id, f"❌ {txt} nahi mila. /start likho.")
        else:
            price, signal, color = get_signal_and_price(bin_sym)
            if not price:
                send(chat_id, f"⚠️ {orig} ka price nahi mil raha.")
            else:
                sl = price * 0.98
                tp1 = price * 1.02
                tp2 = price * 1.04
                msg = (
                    f"📊 <b>{orig}</b>\n"
                    f"Price: {price:.4f}\n\n"
                    f"{color}\n"
                    f"<b>Signal: {signal}</b>\n\n"
                    f"SL: {sl:.4f}\n"
                    f"TP1: {tp1:.4f}\n"
                    f"TP2: {tp2:.4f}\n"
                )
                send(chat_id, msg)
    return jsonify(ok=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT",10000)))
