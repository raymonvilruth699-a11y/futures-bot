import os
import time
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

LIVE_TRADING = True
PAPER_TRADING = not LIVE_TRADING

TRADE_UNITS = 250

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
MAX_SAME_CURRENCY_TRADES = 2

SCAN_SECONDS = 60

# Trade entries only between 3 AM and 12 PM Eastern
TRADING_TIMEZONE = ZoneInfo("America/New_York")
TRADING_START_HOUR = 3
TRADING_END_HOUR = 12

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


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured.")
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10
        )
    except Exception as e:
        print("Telegram Error:", e)


def market_is_closed():
    now = datetime.now(timezone.utc)

    if now.weekday() == 4 and now.hour >= 22:
        return True

    if now.weekday() == 5:
        return True

    if now.weekday() == 6 and now.hour < 22:
        return True

    return False


def within_trading_hours():
    now_et = datetime.now(TRADING_TIMEZONE)

    # Sunday entries allowed after 5 PM Eastern
    if now_et.weekday() == 6:
        return now_et.hour >= 17

    # Monday-Friday entries allowed 3 AM to 12 PM Eastern
    if now_et.weekday() in [0, 1, 2, 3, 4]:
        return TRADING_START_HOUR <= now_et.hour < TRADING_END_HOUR

    return False


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


def place_live_order(pair, direction):
    units = TRADE_UNITS if direction == "BUY" else -TRADE_UNITS

    url = f"{OANDA_URL}/accounts/{OANDA_ACCOUNT_ID}/orders"

    payload = {
        "order": {
            "type": "MARKET",
            "instrument": pair,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT"
        }
    }

    response = requests.post(
        url,
        headers=HEADERS,
        json=payload,
        timeout=15
    )

    if response.status_code not in [200, 201]:
        send_telegram(
            f"⚠️ LIVE ORDER FAILED\n"
            f"Pair: {pair}\n"
            f"Direction: {direction}\n"
            f"Error: {response.text}"
        )
        return None

    return {
        "units": units,
        "response": response.json()
    }


def close_live_order(trade):
    opposite_units = -trade["units"]

    url = f"{OANDA_URL}/accounts/{OANDA_ACCOUNT_ID}/orders"

    payload = {
        "order": {
            "type": "MARKET",
            "instrument": trade["pair"],
            "units": str(opposite_units),
            "timeInForce": "FOK",
            "positionFill": "REDUCE_FIRST"
        }
    }

    response = requests.post(
        url,
        headers=HEADERS,
        json=payload,
        timeout=15
    )

    if response.status_code not in [200, 201]:
        send_telegram(
            f"⚠️ LIVE CLOSE FAILED\n"
            f"Pair: {trade['pair']}\n"
            f"Error: {response.text}"
        )
        return False

    return True


def add_indicators(df):
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    df["avg_volume"] = df["volume"].rolling(20).mean()
    df["volume_spike"] = df["volume"] > df["avg_volume"] * 1.10

    df["candle_body"] = abs(df["close"] - df["open"])
    df["range"] = df["high"] - df["low"]
    df["strong_candle"] = df["candle_body"] > df["range"] * 0.45

    return df


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
        score += 20

    if last["strong_candle"]:
        score += 20

    if abs(last["ema9"] - last["ema21"]) > abs(prev["ema9"] - prev["ema21"]):
        score += 15

    if "JPY" in pair:
        score += 5

    return direction, score


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


def is_in_cooldown():
    global cooldown_until

    if cooldown_until is None:
        return False

    now = datetime.now(timezone.utc)

    if now >= cooldown_until:
        send_telegram("✅ Cooldown finished. Bot can open new trades again.")
        cooldown_until = None
        return False

    return True


def register_stop_loss():
    global cooldown_until, recent_stop_losses

    now = datetime.now(timezone.utc)
    recent_stop_losses.append(now)

    cutoff = now - timedelta(minutes=STOP_LOSS_WINDOW_MINUTES)

    recent_stop_losses = [
        t for t in recent_stop_losses
        if t >= cutoff
    ]

    if len(recent_stop_losses) >= STOP_LOSS_LIMIT_IN_WINDOW:
        cooldown_until = now + timedelta(minutes=COOLDOWN_MINUTES)
        recent_stop_losses = []

        send_telegram(f"""
🧊 COOLDOWN ACTIVATED

Reason: {STOP_LOSS_LIMIT_IN_WINDOW} stop losses within {STOP_LOSS_WINDOW_MINUTES} minutes.

New entries paused for {COOLDOWN_MINUTES} minutes.

Existing trades will still be managed.
""")


def open_trade(pair, direction, entry_price, score):
    live_result = None
    units = 0

    if LIVE_TRADING:
        live_result = place_live_order(pair, direction)

        if live_result is None:
            return

        units = live_result["units"]

    active_trades[pair] = {
        "pair": pair,
        "direction": direction,
        "entry": entry_price,
        "score": score,
        "status": "OPEN",
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "profit_protection": False,
        "highest_progress": 0.0,
        "protected_exit": None,
        "units": units
    }

    mode = "LIVE TRADE OPENED" if LIVE_TRADING else "PAPER TRADE OPENED"

    send_telegram(f"""
🚀 {mode}

Pair: {pair}
Direction: {direction}
Units: {TRADE_UNITS if LIVE_TRADING else "Paper"}

Entry: {entry_price}
Score: {score}

🛑 Stop Loss: {STOP_LOSS_PERCENT}%
🔒 Profit Protection Starts: +{PROFIT_PROTECTION_TRIGGER}%
📈 Dynamic Lock: Highest Profit - {TRAILING_PROFIT_GIVEBACK}%
""")


def calculate_tp_progress(trade, current_price):
    entry = trade["entry"]
    direction = trade["direction"]

    if "JPY" in trade["pair"]:
        target_distance = 0.30
    else:
        target_distance = 0.0030

    if direction == "BUY":
        progress = ((current_price - entry) / target_distance) * 100
    else:
        progress = ((entry - current_price) / target_distance) * 100

    return round(progress, 2)


def calculate_protected_exit(highest_progress):
    protected_exit = highest_progress - TRAILING_PROFIT_GIVEBACK

    if protected_exit < MIN_PROTECTED_EXIT:
        protected_exit = MIN_PROTECTED_EXIT

    return round(protected_exit, 2)


def close_trade(pair, reason, current_price, progress):
    trade = active_trades.get(pair)

    if not trade:
        return

    if LIVE_TRADING:
        closed = close_live_order(trade)

        if not closed:
            return

    mode = "LIVE TRADE CLOSED" if LIVE_TRADING else "PAPER TRADE CLOSED"

    send_telegram(f"""
✅ {mode}

Pair: {pair}
Direction: {trade['direction']}

Entry: {trade['entry']}
Exit: {current_price}

Final Progress: {progress}%
Highest Progress: {trade['highest_progress']}
Protected Exit: {trade.get('protected_exit')}

Reason: {reason}
""")

    del active_trades[pair]

    if "STOP LOSS" in reason:
        register_stop_loss()


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

        if progress <= STOP_LOSS_PERCENT:
            close_trade(pair, "🛑 STOP LOSS HIT", current_price, progress)
            continue

        if progress >= PROFIT_PROTECTION_TRIGGER and not trade["profit_protection"]:
            trade["profit_protection"] = True
            trade["protected_exit"] = calculate_protected_exit(trade["highest_progress"])

            send_telegram(f"""
🔒 PROFIT PROTECTION ACTIVATED

Pair: {pair}
Direction: {trade['direction']}

Current Progress: {progress}%
Highest Progress: {trade['highest_progress']}%
Protected Exit: {trade['protected_exit']}%
""")

        if trade["profit_protection"]:
            new_protected_exit = calculate_protected_exit(trade["highest_progress"])

            if trade["protected_exit"] is None or new_protected_exit > trade["protected_exit"]:
                trade["protected_exit"] = new_protected_exit

                send_telegram(f"""
📈 TRAILING PROFIT LOCK MOVED UP

Pair: {pair}
Direction: {trade['direction']}

Highest Progress: {trade['highest_progress']}%
New Protected Exit: {trade['protected_exit']}%
""")

            if progress <= trade["protected_exit"]:
                close_trade(
                    pair,
                    "🔒 DYNAMIC PROFIT PROTECTED EXIT",
                    current_price,
                    progress
                )
                continue

        status = "Moving toward profit ✅" if progress > 0 else "Moving against entry ⚠️"

        send_telegram(f"""
⏳ TRADE UPDATE

Pair: {pair}
Direction: {trade['direction']}

Current Price: {current_price}

TP Progress: {progress}%
Highest Progress: {trade['highest_progress']}%

Profit Protection: {trade['profit_protection']}
Protected Exit: {trade.get('protected_exit')}

{status}
""")


def scan_market():
    if not within_trading_hours():
        print("Outside trading hours. Managing existing trades only.")
        return

    if is_in_cooldown():
        print("Cooldown active. Skipping new entries.")
        return

    found_signal = False

    for pair in PAIRS:
        if not can_open_trade(pair):
            print(f"Skipping {pair}: max trades or correlation")
            continue

        df = get_candles(pair)

        if df.empty:
            print(f"No candle data for {pair}")
            continue

        direction, score = score_trade(df, pair)

        print(f"{pair} | Direction: {direction} | Score: {score}")

        if direction and score >= MIN_SCORE:
            entry_price = float(df.iloc[-1]["close"])
            open_trade(pair, direction, entry_price, score)
            found_signal = True

        else:
            print(f"No trade for {pair}. Score too low.")

    if not found_signal:
        print("No clean setup yet.")


def main():
    send_telegram(f"""
🤖 FOREX BOT STARTED

Mode: {"LIVE TRADING" if LIVE_TRADING else "PAPER TRADING"}
Data Source: LIVE OANDA

Pairs:
- USD_JPY
- EUR_USD
- GBP_USD
- GBP_CAD

Live Units: {TRADE_UNITS}

Risk Rules:
🛑 Stop Loss: {STOP_LOSS_PERCENT}%
🔒 Profit Protection Starts: +{PROFIT_PROTECTION_TRIGGER}%
📈 Dynamic Profit Lock: Highest Profit - {TRAILING_PROFIT_GIVEBACK}%
🚪 Minimum Protected Exit: +{MIN_PROTECTED_EXIT}%

Cooldown:
🧊 {STOP_LOSS_LIMIT_IN_WINDOW} stop losses in {STOP_LOSS_WINDOW_MINUTES} minutes
→ pause new entries for {COOLDOWN_MINUTES} minutes

Max Active Trades: {MAX_ACTIVE_TRADES}
Max Same Currency Trades: {MAX_SAME_CURRENCY_TRADES}

Entry Hours:
Sunday from 5 PM Eastern
Monday-Friday 3 AM–12 PM Eastern

Weekend Filter: ON
Scan Speed: Every {SCAN_SECONDS} seconds

Bot is now scanning.
""")

    while True:
        try:
            if market_is_closed():
                print("Forex market closed. Waiting...")
                manage_open_trades()
                time.sleep(300)
                continue

            print("Scanning market...")

            scan_market()
            manage_open_trades()

            time.sleep(SCAN_SECONDS)

        except Exception as e:
            print("Main Loop Error:", e)
            send_telegram(f"⚠️ BOT ERROR: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()