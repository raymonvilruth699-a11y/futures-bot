import os
import json
import time
import requests
import pandas as pd
import ta
from datetime import datetime, timezone

OANDA_API_KEY = os.getenv("OANDA_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# =========================
# FOREX PAIRS
# =========================

PAIRS = [
    "EUR_USD",
    "GBP_USD",
    "AUD_USD",
    "NZD_USD",
    "USD_CAD",
    "USD_JPY",
    "EUR_JPY",
    "GBP_JPY",
    "EUR_GBP",
    "GBP_CAD",
    "EUR_CAD"
]

# =========================
# SETTINGS
# =========================

TIMEFRAME = "M5"
MIN_SCORE = 75
MAX_ACTIVE_TRADES = 3
TRADE_EXPIRATION_HOURS = 4
SL_COOLDOWN_SECONDS = 60 * 60

STATE_FILE = "bot_state.json"

# =========================
# BOT STATE
# =========================

active_trades = []
cooldowns = {}

wins = 0
losses = 0
protected = 0
expired = 0

paper_balance = 1000.00

last_daily_summary_date = None

# =========================
# SAVE STATE
# =========================

def save_state():

    state = {
        "active_trades": active_trades,
        "cooldowns": cooldowns,
        "wins": wins,
        "losses": losses,
        "protected": protected,
        "expired": expired,
        "paper_balance": paper_balance,
        "last_daily_summary_date": last_daily_summary_date
    }

    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# =========================
# LOAD STATE
# =========================

def load_state():

    global active_trades
    global cooldowns
    global wins
    global losses
    global protected
    global expired
    global paper_balance
    global last_daily_summary_date

    try:

        with open(STATE_FILE, "r") as f:
            state = json.load(f)

        active_trades = state.get("active_trades", [])
        cooldowns = state.get("cooldowns", {})

        wins = state.get("wins", 0)
        losses = state.get("losses", 0)
        protected = state.get("protected", 0)
        expired = state.get("expired", 0)

        paper_balance = state.get("paper_balance", 1000.00)

        last_daily_summary_date = state.get(
            "last_daily_summary_date"
        )

    except:
        save_state()

# =========================
# TELEGRAM
# =========================

def send_message(message):

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    response = requests.post(url, data=payload)

    print(response.text)

# =========================
# MARKET DATA
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

    response = requests.get(
        url,
        headers=headers,
        params=params
    )

    data = response.json()

    candles = []

    for candle in data["candles"]:

        candles.append({
            "close": float(candle["mid"]["c"]),
            "high": float(candle["mid"]["h"]),
            "low": float(candle["mid"]["l"])
        })

    return pd.DataFrame(candles)

# =========================
# ANALYSIS
# =========================

def analyze_pair(pair):

    df = get_candles(pair)

    df["ema20"] = ta.trend.ema_indicator(
        df["close"],
        window=20
    )

    df["ema50"] = ta.trend.ema_indicator(
        df["close"],
        window=50
    )

    df["rsi"] = ta.momentum.rsi(
        df["close"],
        window=14
    )

    latest = df.iloc[-1]

    trend = "NONE"

    if latest["ema20"] > latest["ema50"]:
        trend = "BUY"

    elif latest["ema20"] < latest["ema50"]:
        trend = "SELL"

    volatility = (
        df["high"].iloc[-1]
        -
        df["low"].iloc[-1]
    )

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
        "price": float(latest["close"]),
        "rsi": round(float(latest["rsi"]), 2),
        "volatility": round(float(volatility), 5)
    }

# =========================
# JPY FILTER
# =========================

def has_active_jpy_trade():

    for trade in active_trades:

        if "JPY" in trade["pair"]:
            return True

    return False

# =========================
# PROGRESS
# =========================

def calculate_progress(trade, current_price):

    if trade["direction"] == "BUY":

        return (
            (
                current_price - trade["entry"]
            )
            /
            (
                trade["tp"] - trade["entry"]
            )
        ) * 100

    else:

        return (
            (
                trade["entry"] - current_price
            )
            /
            (
                trade["entry"] - trade["tp"]
            )
        ) * 100

# =========================
# PROFIT PROTECTION
# =========================

def protect_trade(trade, current_price, progress):

    # BREAK EVEN

    if progress >= 25 and not trade["break_even"]:

        trade["sl"] = trade["entry"]

        trade["break_even"] = True

        send_message(
            f"🛡️ BREAK-EVEN ACTIVE\n\n"
            f"{trade['pair']}\n"
            f"SL moved to entry."
        )

    # TRAILING STOP

    if progress >= 40:

        if trade["direction"] == "BUY":

            new_sl = (
                current_price
                -
                (
                    (trade["tp"] - trade["entry"])
                    * 0.25
                )
            )

            if new_sl > trade["sl"]:

                trade["sl"] = new_sl

        else:

            new_sl = (
                current_price
                +
                (
                    (trade["entry"] - trade["tp"])
                    * 0.25
                )
            )

            if new_sl < trade["sl"]:

                trade["sl"] = new_sl

# =========================
# TRADE UPDATES
# =========================

def send_trade_update(trade, current_price, progress):

    if progress > 0:
        status = "Moving toward profit ✅"
    else:
        status = "Moving against entry ⚠️"

    send_message(
        f"⏳ PAPER TRADE UPDATE\n\n"
        f"Pair: {trade['pair']}\n"
        f"Direction: {trade['direction']}\n\n"
        f"Current: {round(current_price,5)}\n"
        f"TP Progress: {round(progress,2)}%\n\n"
        f"{status}"
    )

# =========================
# CLOSE TRADE
# =========================

def close_trade_result(
    trade,
    current_price,
    result_type
):

    global wins
    global losses
    global protected
    global expired
    global paper_balance

    if result_type == "WIN":

        wins += 1

        paper_balance += trade["profit"]

        title = "✅ PAPER TP HIT"

    elif result_type == "LOSS":

        losses += 1

        paper_balance -= trade["risk"]

        cooldowns[trade["pair"]] = time.time()

        title = "❌ PAPER SL HIT"

    elif result_type == "PROTECTED":

        protected += 1

        title = "🟨 PROTECTED EXIT"

    else:

        expired += 1

        title = "⏰ TRADE EXPIRED"

    send_message(
        f"{title}\n\n"
        f"{trade['pair']}\n"
        f"{trade['direction']}\n\n"
        f"Balance: ${round(paper_balance,2)}"
    )

    save_state()

# =========================
# ACTIVE TRADE CHECKER
# =========================

def check_active_trades():

    completed = []

    now = time.time()

    for trade in active_trades:

        df = get_candles(trade["pair"])

        current_price = float(
            df.iloc[-1]["close"]
        )

        progress = calculate_progress(
            trade,
            current_price
        )

        protect_trade(
            trade,
            current_price,
            progress
        )

        trade_age = (
            now - trade["opened_at"]
        ) / 3600

        if trade_age >= TRADE_EXPIRATION_HOURS:

            close_trade_result(
                trade,
                current_price,
                "EXPIRED"
            )

            completed.append(trade)

            continue

        if trade["direction"] == "BUY":

            if current_price >= trade["tp"]:

                close_trade_result(
                    trade,
                    current_price,
                    "WIN"
                )

                completed.append(trade)

            elif current_price <= trade["sl"]:

                if trade["break_even"]:

                    close_trade_result(
                        trade,
                        current_price,
                        "PROTECTED"
                    )

                else:

                    close_trade_result(
                        trade,
                        current_price,
                        "LOSS"
                    )

                completed.append(trade)

            else:

                send_trade_update(
                    trade,
                    current_price,
                    progress
                )

        else:

            if current_price <= trade["tp"]:

                close_trade_result(
                    trade,
                    current_price,
                    "WIN"
                )

                completed.append(trade)

            elif current_price >= trade["sl"]:

                if trade["break_even"]:

                    close_trade_result(
                        trade,
                        current_price,
                        "PROTECTED"
                    )

                else:

                    close_trade_result(
                        trade,
                        current_price,
                        "LOSS"
                    )

                completed.append(trade)

            else:

                send_trade_update(
                    trade,
                    current_price,
                    progress
                )

    for trade in completed:
        active_trades.remove(trade)

    if completed:
        save_state()

# =========================
# DAILY SUMMARY
# =========================

def send_daily_summary():

    global last_daily_summary_date

    today = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")

    hour = datetime.utcnow().hour

    if last_daily_summary_date == today:
        return

    if hour != 21:
        return

    send_message(
        f"📊 DAILY SUMMARY\n\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Protected: {protected}\n"
        f"Expired: {expired}\n\n"
        f"Balance: ${round(paper_balance,2)}"
    )

    last_daily_summary_date = today

    save_state()

# =========================
# OPEN TRADE
# =========================

def open_trade(signal):

    pair = signal["pair"]

    # ONLY ONE JPY TRADE AT A TIME
    if "JPY" in pair and has_active_jpy_trade():

        print(f"Blocked extra JPY trade: {pair}")

        return

    if len(active_trades) >= MAX_ACTIVE_TRADES:
        return

    if (
        pair in cooldowns
        and
        time.time() - cooldowns[pair]
        < SL_COOLDOWN_SECONDS
    ):
        return

    already_open = any(
        trade["pair"] == pair
        for trade in active_trades
    )

    if already_open:
        return

    price = signal["price"]

    risk = round(
        paper_balance * 0.01,
        2
    )

    profit = round(
        risk * 2,
        2
    )

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
        "profit": profit,
        "break_even": False,
        "opened_at": time.time()
    }

    active_trades.append(trade)

    save_state()

    send_message(
        f"🚨 A+ PAPER TRADE OPENED\n\n"
        f"Pair: {pair}\n"
        f"Direction: {signal['trend']}\n"
        f"Score: {signal['score']}\n\n"
        f"Entry: {round(price,5)}\n"
        f"TP: {round(tp,5)}\n"
        f"SL: {round(sl,5)}\n\n"
        f"Risk: ${risk}\n"
        f"Target Profit: ${profit}\n\n"
        f"Mode: PAPER TRADING ONLY"
    )

# =========================
# START BOT
# =========================

load_state()

send_message(
    "🤖 Forex AI Paper Bot Started — JPY Protection Mode"
)

while True:

    try:

        print("Scanning forex market...")

        check_active_trades()

        send_daily_summary()

        for pair in PAIRS:

            signal = analyze_pair(pair)

            print(signal)

            if (
                signal["score"] >= MIN_SCORE
                and
                signal["trend"] != "NONE"
            ):

                open_trade(signal)

        time.sleep(300)

    except Exception as e:

        print("ERROR:", e)

        send_message(
            f"⚠️ BOT ERROR:\n{e}"
        )

        time.sleep(30)