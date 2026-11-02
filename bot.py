# Khattak PRO Bot - Binance + MT5
import requests

symbol_binance = "BTCUSDT"
buy_price = 60000
sell_price = 65000

def get_binance_price(symbol="BTCUSDT"):
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        response = requests.get(url, timeout=10)
        data = response.json()
        return float(data['price'])
    except Exception as e:
        print(f"Binance Error: {e}")
        return None

def check_signal(current_price):
    if current_price <= buy_price:
        return f"BUY SIGNAL @ {current_price}"
    elif current_price >= sell_price:
        return f"SELL SIGNAL @ {current_price}"
    else:
        return f"HOLD @ {current_price}"

print("Khattak-_bot PRO Started...")
live_price = get_binance_price(symbol_binance)

if live_price:
    print(f"Binance Live Price: {live_price}")
    print(check_signal(live_price))
else:
    print("Live price nahi mili")
    print(check_signal(62000))
