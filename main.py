import os
import requests
import time

API_KEY = os.getenv("OANDA_API_KEY")
ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")

url = f"https://api-fxtrade.oanda.com/v3/accounts/{ACCOUNT_ID}"

headers = {
    "Authorization": f"Bearer {API_KEY}"
}

while True:
    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        print("✅ OANDA bot connected")
        print(response.json())
    else:
        print("❌ Error:", response.text)

    time.sleep(30)