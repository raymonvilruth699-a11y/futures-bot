import os
import requests
import time

API_KEY = os.getenv("OANDA_API_KEY")
ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

pairs = [
    "EUR_USD",
    "GBP_USD",
    "USD_JPY",
    "AUD_USD",
    "USD_CAD",
    "EUR_JPY",
    "GBP_JPY",
    "NZD_USD"
]

headers = {
    "Authorization": f"Bearer {API_KEY}"
}

def send_telegram(message):

    telegram_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    requests.post(telegram_url, data=data)

def get_price(pair):

    url = f"https://api-fxtrade.oanda.com/v3/accounts/{ACCOUNT_ID}/pricing?instruments={pair}"

    response = requests.get(url, headers=headers)

    if response.status_code == 200:

        data = response.json()

        bid = float(data["prices"][0]["bids"][0]["price"])
        ask = float(data["prices"][0]["asks"][0]["price"])

        return (bid + ask) / 2

    return None

print("✅ Forex bot running...")

while True:

    for pair in pairs:

        try:

            price = get_price(pair)

            if price:

                print(f"{pair}: {price}")

                if price > 1:

                    send_telegram(
                        f"🚨 Trade Setup Found\n\nPair: {pair}\nPrice: {price}"
                    )

            time.sleep(5)

        except Exception as e:

            print("Error:", e)

    time.sleep(60)