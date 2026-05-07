import os
import requests
import pandas as pd
import ta
import time
from datetime import datetime

# =========================
# ENV VARIABLES
# =========================

OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# =========================
# SETTINGS
# =========================

PAIRS = [
    "EUR_USD",
    "GBP_USD",
    "USD_JPY",
    "AUD_USD",
    "USD_CAD",
    "EUR_JPY",
    "GBP_JPY"
]

TIMEFRAME = "M5"

MIN_SCORE = 80

cooldowns = {}

# =========================
# GET CHART DATA
# =========================

def get_candles(pair):

    url = f"https://api-fxtrade.oanda.com/v3/instruments/{pair}/candles"

    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}"
    }

    params = {
        "granularity": TIMEFRAME,
        "count": 100,
        "price": "M"
    }

    r = requests.get(url, headers=headers, params=params)

    data = r.json()

    candles = []

    for c in data["candles"]:
        candles.append({
            "close": float(c["mid"]["c"]),
            "high": float(c["mid"]["h"]),
            "low": float(c["mid"]["l"])
        })

    return pd.DataFrame(candles)

# =========================
# ANALYSIS ENGINE
# =========================

def analyze_pair(pair):

    df = get_candles(pair)

    df["ema20"] = ta.trend.ema_indicator(df["close"], window=20)
    df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)

    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

    latest = df.iloc[-1]

    trend = "NONE"

    if latest["ema20"] > latest["ema50"]:
        trend = "BUY"

    elif latest["ema20"] < latest["ema50"]:
        trend = "SELL"

    score = 0

    # Trend strength
    if trend != "NONE":
        score += 30

    # RSI filter
    if trend == "BUY" and latest["rsi"] > 55:
        score += 20

    if trend == "SELL" and latest["rsi"] < 45:
        score += 20

    # Volatility
    volatility = df["high"].iloc[-1] - df["low"].iloc[-1]

    if volatility > 0.0008:
        score += 20

    # Session bonus
    hour = datetime.utcnow().hour

    if 7 <= hour <= 16:
        score += 15

    # Whale/smart-money simulation
    if volatility > 0.0012:
        score += 15

    return {
        "pair": pair,
        "trend": trend,
        "score": score,
        "rsi": round(latest["rsi"], 2),
        "volatility": round(volatility, 5)
    }

# =========================
# TELEGRAM ALERT
# =========================

def send_alert(signal):

    message = f"""
🚨 FOREX SIGNAL

Pair: {signal['pair']}
Direction: {signal['trend']}
Score: {signal['score']}

RSI: {signal['rsi']}
Volatility: {signal['volatility']}

📈 AI Setup Approved
"""

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    requests.post(url, data=payload)

# =========================
# MAIN LOOP
# =========================

while True:

    print("Scanning forex market...")

    for pair in PAIRS:

        try:

            signal = analyze_pair(pair)

            print(signal)

            if signal["score"] >= MIN_SCORE:

                last_trade = cooldowns.get(pair)

                if not last_trade or time.time() - last_trade > 3600:

                    send_alert(signal)

                    cooldowns[pair] = time.time()

        except Exception as e:
            print("ERROR:", e)

    time.sleep(300)