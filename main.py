import os
import time
import requests
import pandas as pd
from datetime import datetime, timezone

# =========================
# ENVIRONMENT VARIABLES
# =========================

OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# =========================
# BOT SETTINGS
# =========================

PAPER_TRADING = True

PAIRS = [
    "EUR_USD",
    "GBP_USD",
    "USD_JPY",
    "GBP_JPY",
    "EUR_JPY",
    "GBP_CAD",
    "USD_CAD",
    "AUD_USD",
    "NZD_USD"
]

TIMEFRAME = "M5"
CANDLE_COUNT = 100

MIN_SCORE = 85

# New risk rules
STOP_LOSS_PERCENT = -20.0
PROFIT_PROTECTION_TRIGGER = 25.0
PROFIT_PROTECTION_EXIT = 10.0

SCAN_SECONDS = 300
UPDATE_SECONDS = 300

MAX_ACTIVE_TRADES = 5
MAX_SAME_CURRENCY_TRADES = 1

OANDA_URL = "https://api-fxpractice.oanda.com/v3"

HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json"
}

# =========================
# ACTIVE PAPER TRADES
# =========================

active_trades = {}


# =========================
# TELEGRAM
# =========================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured.")
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message
        }
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print("Telegram error:", e)


# =========================
# OANDA DATA
# =========================

def get_candles(pair):
    try:
        url = f"{OANDA_URL}/instruments/{pair}/candles"
        params = {
            "granularity": TIMEFRAME,
            "count": CANDLE_COUNT,
            "price": "M"
        }

        response = requests.get(url, headers=HEADERS, params=params, timeout=15)
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
        print(f"Error getting candles for {pair}:", e)
        return pd.DataFrame()


# =========================
# INDICATORS
# =========================

def add_indicators(df):
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    df["avg_volume"] = df["volume"].rolling(20).mean()
    df["volume_spike"] = df["volume"] > df["avg_volume"] * 1.25

    df["candle_body"] = abs(df["close"] - df["open"])
    df["range"] = df["high"] - df["low"]
    df["strong_candle"] = df["candle_body"] > df["range"] * 0.55

    return df


# =========================
# SIGNAL SCORING
# =========================

def score_trade(df, pair):
    if df.empty or len(df) < 60:
        return None, 0

    df = add_indicators(df)
    last = df.iloc[-1]
    prev = df.iloc[-2]

    score = 0
    direction = None

    bullish = (
        last["ema9"] > last["ema21"]
        and last["close"] > last["ema50"]
        and last["close"] > prev["close"]
    )

    bearish = (
        last["ema9"] < last["ema21"]
        and last["close"] < last["ema50"]
        and last["close"] < prev["close"]
    )

    if bullish:
        direction = "BUY"
        score += 35

    if bearish:
        direction = "SELL"
        score += 35

    if last["volume_spike"]:
        score += 25

    if last["strong_candle"]:
        score += 20

    if abs(last["ema9"] - last["ema21"]) > abs(prev["ema9"] - prev["ema21"]):
        score += 15

    # JPY pairs have been cleaner for your bot
    if "JPY" in pair:
        score += 5

    return direction, score


# =========================
# CORRELATION FILTER
# =========================

def currencies_in_pair(pair):
    return pair.split("_")


def can_open_trade(pair):
    if pair in active_trades:
        return False

    if len(active_trades) >= MAX_ACTIVE_TRADES:
        return False

    new_currencies = currencies_in_pair(pair)

    for active_pair in active_trades.keys():
        active_currencies = currencies_in_pair(active_pair)

        for currency in new_currencies:
            if currency in active_currencies:
                count = sum(
                    currency in currencies_in_pair(p)
                    for p in active_trades.keys()
                )

                if count >= MAX_SAME_CURRENCY_TRADES:
                    return False

    return True


# =========================
# PAPER TRADE OPEN
# =========================

def open_paper_trade(pair, direction, entry_price, score):
    active_trades[pair] = {
        "pair": pair,
        "direction": direction,
        "entry": entry_price,
        "score": score,
        "status": "OPEN",
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "profit_protection": False,
        "highest_progress": 0.0
    }

    message = f"""
🚀 PAPER TRADE OPENED

Pair: {pair}
Direction: {direction}
Entry: {entry_price}
Score: {score}

Stop Loss: -20%
Profit Protection: activates at +25%
"""
    send_telegram(message)


# =========================
# TP PROGRESS CALCULATION
# =========================

def calculate_tp_progress(trade, current_price):
    entry = trade["entry"]
    direction = trade["direction"]

    # Estimated TP distance for paper tracking
    if "JPY" in trade["pair"]:
        target_distance = 0.30
    else:
        target_distance = 0.0030

    if direction == "BUY":
        progress = ((current_price - entry) / target_distance) * 100
    else:
        progress = ((entry - current_price) / target_distance) * 100

    return round(progress, 2)


# =========================
# CLOSE TRADE
# =========================

def close_trade(pair, reason, current_price, progress):
    trade = active_trades.get(pair)

    if not trade:
        return

    message = f"""
✅ PAPER TRADE CLOSED

Pair: {pair}
Direction: {trade['direction']}
Entry: {trade['entry']}
Exit: {current_price}
Final TP Progress: {progress}%

Reason: {reason}
"""
    send_telegram(message)

    del active_trades[pair]


# =========================
# PAPER TRADE MANAGER
# =========================

def manage_open_trades():
    if not active_trades:
        return

    for pair in list(active_trades.keys()):
        trade = active_trades[pair]

        df = get_candles(pair)
        if df.empty:
            continue

        current_price = float(df.iloc[-1]["close"])
        progress = calculate_tp_progress(trade, current_price)

        if progress > trade["highest_progress"]:
            trade["highest_progress"] = progress

        # HARD STOP LOSS AT -20%
        if progress <= STOP_LOSS_PERCENT:
            close_trade(
                pair,
                "🛑 STOP LOSS HIT at -20%",
                current_price,
                progress
            )
            continue

        # ACTIVATE PROFIT PROTECTION AT +25%
        if progress >= PROFIT_PROTECTION_TRIGGER and not trade["profit_protection"]:
            trade["profit_protection"] = True

            send_telegram(f"""
🔒 PROFIT PROTECTION ACTIVATED

Pair: {pair}
Direction: {trade['direction']}
Current: {current_price}
TP Progress: {progress}%

Trade is now protected.
""")

        # PROTECTED EXIT
        if trade["profit_protection"] and progress <= PROFIT_PROTECTION_EXIT:
            close_trade(
                pair,
                "🔒 PROFIT PROTECTED EXIT",
                current_price,
                progress
            )
            continue

        # NORMAL UPDATE
        status = "Moving toward profit ✅" if progress > 0 else "Moving against entry ⚠️"

        send_telegram(f"""
⏳ PAPER TRADE UPDATE

Pair: {pair}
Direction: {trade['direction']}

Current: {current_price}
TP Progress: {progress}%

Profit Protection: {trade['profit_protection']}
Highest Progress: {trade['highest_progress']}%

{status}
""")


# =========================
# SCAN FOR NEW TRADES
# =========================

def scan_market():
    for pair in PAIRS:
        if not can_open_trade(pair):
            continue

        df = get_candles(pair)
        if df.empty:
            continue

        direction, score = score_trade(df, pair)

        if direction and score >= MIN_SCORE:
            entry_price = float(df.iloc[-1]["close"])
            open_paper_trade(pair, direction, entry_price, score)


# =========================
# MAIN LOOP
# =========================

def main():
    send_telegram("""
🤖 FOREX BOT STARTED

Mode: PAPER TRADING
Pairs: Forex majors + JPY pairs

Risk Rules:
🛑 Stop Loss: -20%
🔒 Profit Protection: +25%
🚪 Protected Exit: +10%

Bot is now scanning.
""")

    last_update = 0

    while True:
        try:
            print("Scanning market...")
            scan_market()

            now = time.time()

            if now - last_update >= UPDATE_SECONDS:
                manage_open_trades()
                last_update = now

            time.sleep(SCAN_SECONDS)

        except Exception as e:
            print("Main loop error:", e)
            send_telegram(f"⚠️ BOT ERROR: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()