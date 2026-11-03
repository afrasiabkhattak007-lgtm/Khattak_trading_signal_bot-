import os, requests
from flask import Flask, request, jsonify
app = Flask(__name__)
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

def set_webhook():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if url and BOT_TOKEN:
        try:
            requests.get(f"{TELEGRAM_API}/setWebhook?url={url}/webhook&drop_pending_updates=True", timeout=15)
        except: pass

def get_price(s):
    s=s.upper().strip()
    try:
        if s in ["GOLD","XAU"]:
            p=float(requests.get("https://api.gold-api.com/price/XAU", timeout=10).json()['price'])
            return p, [p]*50, "GOLD"
        c=s.replace("USDT","")+"USDT"
        price=float(requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={c}", timeout=10).json()['price'])
        kl=requests.get(f"https://api.binance.com/api/v3/klines?symbol={c}&interval=1h&limit=50", timeout=10).json()
        closes=[float(k[4]) for k in kl]
        return price, closes, c
    except:
        return None, None, "Error"

def build_sig(price, closes):
    a20=sum(closes[-20:])/20
    a50=sum(closes)/len(closes)
    if price>a20 and a20>a50: return "BUY", price, price*0.97, price*1.02, price*1.04, price*1.08
    elif price<a20: return "SELL", price, price*1.03, price*0.98, price*0.96, price*0.92
    else: return "HOLD", price, price*0.98, price*1.02, price*1.04, price*1.06

def send_msg(cid, txt):
    try: requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": cid, "text": txt, "parse_mode":"HTML"}, timeout=10)
    except: pass

@app.route('/')
def home(): return "✅ Khattak Bot LIVE"

@app.route('/webhook', methods=['POST'])
def webhook():
    d=request.get_json()
    if not d or "message" not in d: return jsonify({"ok":True})
    cid=d["message"]["chat"]["id"]
    txt=d["message"].get("text","").strip()
    low=txt.lower()
    if "/start" in low:
        send_msg(cid, "✅ <b>Khattak Bot LIVE</b>\n\n💰 35 Lakh Points\n\nCommands:\n/check GOLD\n/check BTC\n/check ETH\n/check SOL")
    elif "/check" in low:
        coin=txt.split()[1] if len(txt.split())>1 else "BTC"
        price, closes, real=get_price(coin)
        if price:
            sig, entry, sl, tp1, tp2, tp3=build_sig(price, closes)
            send_msg(cid, f"📈 <b>{coin.upper()} ({real})</b>\nPrice: {price:.2f}\nSignal: <b>{sig}</b>\n\nEntry: {entry:.2f}\nSL: {sl:.2f}\nTP1: {tp1:.2f}\nTP2: {tp2:.2f}")
        else:
            send_msg(cid, f"❌ {coin} nahi mila")
    return jsonify({"ok":True})

set_webhook()
if __name__=='__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT',10000)))
