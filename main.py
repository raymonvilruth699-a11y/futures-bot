import os
import requests
import pandas as pd
import ta
import time
from datetime import datetime

OANDA_API_KEY = os.getenv("OANDA_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "EUR_JPY", "GBP_JPY"]

TIMEFRAME = "M5"
MIN_SCORE = 75

active_trades = []
wins = 0
losses = 0
paper_balance = 1000.00

def send_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    response = requests.post(url, data=payload)
    print("TELEGRAM STATUS:", response.text)

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

    response = requests.get(url, headers=headers, params=params)
    data = response.json()

    candles = []

    for candle in data["candles"]:
        candles.append({
            "close": float(candle["mid"]["c"]),
            "high": float(candle["mid"]["h"]),
            "low": float(candle["mid"]["l"])
        })

    return pd.DataFrame(candles)

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

    volatility = df["high"].iloc[-1] - df["low"].iloc[-1]

    score = 0

    if trend != "NONE":
        score += 30

    if trend == "BUY" and latest["rsi"] > 55:
        score += 20

    if trend == "SELL" and latest["rsi"] < 45:
        score += 20

    if volatility > 0.0008:
        score += 20

    hour = datetime.utcnow().hour

    if 7 <= hour <= 16:
        score += 15

    if volatility > 0.0012:
        score += 15

    return {
        "pair": pair,
        "trend": trend,
        "score": score,
        "price": latest["close"],
        "rsi": round(latest["rsi"], 2),
        "volatility": round(volatility, 5)
    }

def check_active_trades():
    global wins
    global losses
    global paper_balance

    completed = []

    for trade in active_trades:
        df = get_candles(trade["pair"])
        current_price = df.iloc[-1]["close"]

        if trade["direction"] == "BUY":
            if current_price >= trade["tp"]:
                wins += 1
                paper_balance += trade["profit"]
                completed.append(trade)

                send_message(f"""✅ PAPER TP HIT

Pair: {trade['pair']}
Direction: BUY
Entry: {round(trade['entry'], 5)}
Exit: {round(current_price, 5)}

Result: WIN
Profit: +${trade['profit']}

Wins: {wins}
Losses: {losses}
Win Rate: {round((wins / max(wins + losses, 1)) * 100, 2)}%
Paper Balance: ${round(paper_balance, 2)}
""")

            elif current_price <= trade["sl"]:
                losses += 1
                paper_balance -= trade["risk"]
                completed.append(trade)

                send_message(f"""❌ PAPER SL HIT

Pair: {trade['pair']}
Direction: BUY
Entry: {round(trade['entry'], 5)}
Exit: {round(current_price, 5)}

Result: LOSS
Loss: -${trade['risk']}

Wins: {wins}
Losses: {losses}
Win Rate: {round((wins / max(wins + losses, 1)) * 100, 2)}%
Paper Balance: ${round(paper_balance, 2)}
""")

        elif trade["direction"] == "SELL":
            if current_price <= trade["tp"]:
                wins += 1
                paper_balance += trade["profit"]
                completed.append(trade)

                send_message(f"""✅ PAPER TP HIT

Pair: {trade['pair']}
Direction: SELL
Entry: {round(trade['entry'], 5)}
Exit: {round(current_price, 5)}

Result: WIN
Profit: +${trade['profit']}

Wins: {wins}
Losses: {losses}
Win Rate: {round((wins / max(wins + losses, 1)) * 100, 2)}%
Paper Balance: ${round(paper_balance, 2)}
""")

            elif current_price >= trade["sl"]:
                losses += 1
                paper_balance -= trade["risk"]
                completed.append(trade)

                send_message(f"""❌ PAPER SL HIT

Pair: {trade['pair']}
Direction: SELL
Entry: {round(trade['entry'], 5)}
Exit: {round(current_price, 5)}

Result: LOSS
Loss: -${trade['risk']}

Wins: {wins}
Losses: {losses}
Win Rate: {round((wins / max(wins + losses, 1)) * 100, 2)}%
Paper Balance: ${round(paper_balance, 2)}
""")

    for trade in completed:
        active_trades.remove(trade)

send_message("🤖 Forex AI Paper Bot Started — 75+ Score Mode")

while True:
    try:
        print("Scanning forex market...")

        check_active_trades()

        for pair in PAIRS:
            signal = analyze_pair(pair)

            print(signal)

            if signal["score"] >= MIN_SCORE and signal["trend"] != "NONE":
                price = signal["price"]

                risk = round(paper_balance * 0.01, 2)
                profit = round(risk * 2, 2)

                if signal["trend"] == "BUY":
                    tp = price * 1.003
                    sl = price * 0.997
                else:
                    tp = price * 0.997
                    sl = price * 1.003

                trade = {
                    "pair": pair,
                    "direction": signal["trend"],
                    "entry": price,
                    "tp": tp,
                    "sl": sl,
                    "risk": risk,
                    "profit": profit
                }

                active_trades.append(trade)

                send_message(f"""🚨 A+ PAPER TRADE OPENED

Pair: {pair}
Direction: {signal['trend']}
Score: {signal['score']}

Entry: {round(price, 5)}
TP: {round(tp, 5)}
SL: {round(sl, 5)}

RSI: {signal['rsi']}
Volatility: {signal['volatility']}

Risk: ${risk}
Target Profit: ${profit}

Mode: PAPER TRADING ONLY
""")

        time.sleep(300)

    except Exception as e:
        print("ERROR:", e)
        send_message(f"⚠️ BOT ERROR:\n{e}")
        time.sleep(30)