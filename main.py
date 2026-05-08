import os
import time
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

# ==========================================
# ENV VARIABLES
# ==========================================

OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ==========================================
# LIVE / PAPER SETTINGS
# ==========================================

LIVE_TRADING = False  # Change to True when ready for real money
PAPER_TRADING = not LIVE_TRADING

TRADE_UNITS = 250  # Updated live size

PAIRS = [
    "USD_JPY",
    "EUR_USD",
    "GBP_USD",
    "GBP_CAD"
]

TIMEFRAME = "M5"
CANDLE_COUNT = 100
MIN_SCORE = 65

STOP_LOSS_PERCENT = -30.0
PROFIT_PROTECTION_TRIGGER = 20.0
TRAILING_PROFIT_GIVEBACK = 10.0
MIN_PROTECTED_EXIT = 15.0

MAX_ACTIVE_TRADES = 3
MAX_SAME_CURRENCY_TRADES = 1

SCAN_SECONDS = 60

STOP_LOSS_WINDOW_MINUTES = 15
STOP_LOSS_LIMIT_IN_WINDOW = 2
COOLDOWN_MINUTES = 45

cooldown_until = None
recent_stop_losses = []

OANDA_URL = "https://api-fxtrade.oanda.com/v3"

HEADERS = {
    "Authorization": OANDA_API_KEY if OANDA_API_KEY and OANDA_API_KEY.startswith("Bearer ") else f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json"
}

active_trades = {}

# ==========================================
# TELEGRAM
# ==========================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured.")
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print("Telegram Error:", e)

# ==========================================
# WEEKEND MARKET FILTER
# ==========================================

def market_is_closed():
    now = datetime.now(timezone.utc)

    # Friday after 22:00 UTC
    if now.weekday() == 4 and now.hour >= 22:
        return True

    # Saturday
    if now.weekday() == 5:
        return True

    # Sunday before 22:00 UTC
    if now.weekday() == 6 and now.hour < 22:
        return True

    return False

# ==========================================
# OANDA DATA
# ==========================================

def get_candles(pair):
    try:
        url = f"{OANDA_URL}/instruments/{pair}/candles"

        params = {
            "granularity": TIMEFRAME,
            "count": CANDLE_COUNT,
            "price": "M"
        }

        response = requests.get(url, headers=HEADERS, params=params, timeout=15)

        print(f"{pair} STATUS:", response.status_code)

        if response.status_code != 200:
            print(f"OANDA ERROR for {pair}: {response.text}")
            return pd.DataFrame()

        data = response.json()
        candles = []

        for candle in data.get("candles", []):
            if candle.get("complete"):
                candles.append({
                    "time": candle["time"],
                    "open": float(candle["mid"]["o"]),
                    "high": float(candle["mid"]["h"]),
                    "low": float(candle["mid"]["l"]),
                    "close": float(candle["mid"]["c"]),
                    "volume": int(candle["volume"])
                })

        return pd.DataFrame(candles)

    except Exception as e:
        print(f"Error loading {pair}: {e}")
        return pd.DataFrame()

# ==========================================
# LIVE ORDER FUNCTIONS
# ==========================================

def place_live_order(pair, direction):
    units = TRADE_UNITS if direction == "BUY" else -TRADE_UNITS

    url = f