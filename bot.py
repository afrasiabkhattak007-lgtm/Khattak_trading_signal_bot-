# Khattak Trading Bot
import time
print("Khattak-_bot Started...")
symbol = "BTCUSDT"
buy_price = 60000
sell_price = 65000
def check_signal(current_price):
    if current_price <= buy_price:
        return "BUY SIGNAL"
    elif current_price >= sell_price:
        return "SELL SIGNAL"
    else:
        return "HOLD"
current_price = 62000
print(check_signal(current_price))
