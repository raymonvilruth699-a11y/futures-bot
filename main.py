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

# Focused cleaner pairs
PAIRS = [
    "USD_JPY",
    "GBP_JPY",
    "EUR_USD"
]

TIMEFRAME = "M5"
CANDLE_COUNT = 100

MIN_SCORE = 85

# =========================
# RISK MANAGEMENT
# =========================

# Close trade automatically at -20%
STOP_LOSS_PERCENT = -20.0

# Activate protection at +25%
PROFIT_PROTECTION_TRIGGER = 25.0

# Exit protected trades if profit falls back to +15%
PROFIT_PROTECTION_EXIT = 15.0

# Max active trades
MAX_ACTIVE_TRADES = 3

# Prevent too many correlated trades
MAX_SAME_CURRENCY_TRADES = 1

SCAN_SECONDS = 300
UPDATE_SECONDS = 300

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
# TELEGRAM ALERTS
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
        print("Telegram Error:", e)


# =========================
# GET OANDA CANDLES
# =========================

def get_candles(pair):

    try:

        url = f"{OANDA_URL}/instruments/{pair}/candles"

        params = {
            "granularity": TIMEFRAME,
            "count": CANDLE_COUNT,
            "price": "M"
        }

        response = requests.get(
            url,
            headers=HEADERS,
            params=params,
            timeout=15
        )

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
        print(f"Error loading {pair}:", e)
        return pd.DataFrame()


# =========================
# ADD INDICATORS
# =========================

def add_indicators(df):

    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()

    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()

    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    df["avg_volume"] = df["volume"].rolling(20).mean()

    df["volume_spike"] = (
        df["volume"] > df["avg_volume"] * 1.25
    )

    df["candle_body"] = abs(
        df["close"] - df["open"]
    )

    df["range"] = (
        df["high"] - df["low"]
    )

    df["strong_candle"] = (
        df["candle_body"] > df["range"] * 0.55
    )

    return df


# =========================
# SCORE TRADES
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

    # Extra boost for JPY trends
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
# OPEN PAPER TRADE
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

    send_telegram(f"""
🚀 PAPER TRADE OPENED

Pair: {pair}
Direction: {direction}
Entry: {entry_price}
Score: {score}

🛑 Stop Loss: -20%
🔒 Profit Protection: +25%
""")


# =========================
# TP PROGRESS
# =========================

def calculate_tp_progress(trade, current_price):

    entry = trade["entry"]
    direction = trade["direction"]

    # estimated TP movement
    if "JPY" in trade["pair"]:
        target_distance = 0.30
    else:
        target_distance = 0.0030

    if direction == "BUY":

        progress = (
            (current_price - entry)
            / target_distance
        ) * 100

    else:

        progress = (
            (entry - current_price)
            / target_distance
        ) * 100

    return round(progress, 2)


# =========================
# CLOSE TRADE
# =========================

def close_trade(pair, reason, current_price, progress):

    trade = active_trades.get(pair)

    if not trade:
        return

    send_telegram(f"""
✅ PAPER TRADE CLOSED

Pair: {pair}
Direction: {trade['direction']}

Entry: {trade['entry']}
Exit: {current_price}

Final Progress: {progress}%

Reason: {reason}
""")

    del active_trades[pair]


# =========================
# MANAGE OPEN TRADES
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

        progress = calculate_tp_progress(
            trade,
            current_price
        )

        # Track highest progress
        if progress > trade["highest_progress"]:
            trade["highest_progress"] = progress

        # =========================
        # STOP LOSS
        # =========================

        if progress <= STOP_LOSS_PERCENT:

            close_trade(
                pair,
                "🛑 STOP LOSS HIT",
                current_price,
                progress
            )

            continue

        # =========================
        # ACTIVATE PROFIT PROTECTION
        # =========================

        if (
            progress >= PROFIT_PROTECTION_TRIGGER
            and not trade["profit_protection"]
        ):

            trade["profit_protection"] = True

            send_telegram(f"""
🔒 PROFIT PROTECTION ACTIVATED

Pair: {pair}
Direction: {trade['direction']}

Current Progress: {progress}%
Highest Progress: {trade['highest_progress']}%
""")

        # =========================
        # PROTECTED EXIT
        # =========================

        if (
            trade["profit_protection"]
            and progress <= PROFIT_PROTECTION_EXIT
        ):

            close_trade(
                pair,
                "🔒 PROFIT PROTECTED EXIT",
                current_price,
                progress
            )

            continue

        # =========================
        # NORMAL UPDATE
        # =========================

        status = (
            "Moving toward profit ✅"
            if progress > 0
            else "Moving against entry ⚠️"
        )

        send_telegram(f"""
⏳ PAPER TRADE UPDATE

Pair: {pair}
Direction: {trade['direction']}

Current Price: {current_price}

TP Progress: {progress}%

Highest Progress: {trade['highest_progress']}%

Profit Protection: {trade['profit_protection']}

{status}
""")


# =========================
# MARKET SCANNER
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

            open_paper_trade(
                pair,
                direction,
                entry_price,
                score
            )


# =========================
# MAIN LOOP
# =========================

def main():

    send_telegram("""
🤖 FOREX BOT STARTED

Mode: PAPER TRADING

Pairs:
- USD_JPY
- GBP_JPY
- EUR_USD

Risk Rules:
🛑 Stop Loss: -20%
🔒 Profit Protection: +25%
🚪 Protected Exit: +15%

Max Active Trades: 3
Max Same Currency Trades: 1

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

            print("Main Loop Error:", e)

            send_telegram(f"⚠️ BOT ERROR: {e}")

            time.sleep(60)


# =========================
# START BOT
# =========================

if __name__ == "__main__":
    main()