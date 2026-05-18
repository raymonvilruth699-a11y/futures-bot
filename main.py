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
TRADE_UNITS = 1000

PAIRS = [
    "EUR_USD",
    "GBP_USD",
    "USD_JPY",
    "AUD_USD",
]

TIMEFRAME = "M5"
CANDLE_COUNT = 100
MIN_SCORE = 65

STOP_LOSS_PERCENT = -40.0
PROFIT_PROTECTION_TRIGGER = 12.0
TRAILING_PROFIT_GIVEBACK = 6.0
MIN_PROTECTED_EXIT = 6.0

MAX_ACTIVE_TRADES = 3
SCAN_SECONDS = 10

STOP_LOSS_WINDOW_MINUTES = 15
STOP_LOSS_LIMIT_IN_WINDOW = 2
COOLDOWN_MINUTES = 20
FAILED_ENTRY_BLOCK_MINUTES = 10

TRADING_TIMEZONE = ZoneInfo("America/New_York")
OANDA_URL = "https://api-fxtrade.oanda.com/v3"

token = OANDA_API_KEY or ""

HEADERS = {
    "Authorization": token if token.startswith("Bearer ") else f"Bearer {token}",
    "Content-Type": "application/json"
}

active_trades = {}
recent_stop_losses = []
cooldown_until = None
blocked_entries = {}


def send_telegram(message):
    try:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            print("Telegram not configured")
            return

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10
        )

    except Exception as e:
        print("Telegram Error:", e)


def market_is_closed():
    now_et = datetime.now(TRADING_TIMEZONE)

    if now_et.weekday() == 5:
        return True

    if now_et.weekday() == 6 and now_et.hour < 17:
        return True

    if now_et.weekday() == 4 and now_et.hour >= 17:
        return True

    return False


def within_trading_hours():
    now_et = datetime.now(TRADING_TIMEZONE)

    if 17 <= now_et.hour < 20:
        return False

    if now_et.weekday() == 5:
        return False

    if now_et.weekday() == 6:
        return now_et.hour >= 20

    if now_et.weekday() == 4:
        return now_et.hour < 12

    if now_et.weekday() in [0, 1, 2, 3]:
        return True

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

        candles = []

        for candle in response.json().get("candles", []):
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
        print(f"{pair} candle error:", e)
        return pd.DataFrame()


def get_target_distance(pair):
    if "JPY" in pair:
        return 0.30

    return 0.0030


def get_open_positions_map():
    positions = {}

    try:
        url = f"{OANDA_URL}/accounts/{OANDA_ACCOUNT_ID}/openPositions"

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=15
        )

        if response.status_code != 200:
            print("OPEN POSITIONS ERROR:", response.text)
            return None

        data = response.json()

        for position in data.get("positions", []):
            pair = position["instrument"]

            long_units = int(float(position["long"]["units"]))
            short_units = int(float(position["short"]["units"]))

            if long_units > 0:
                positions[pair] = {
                    "direction": "BUY",
                    "units": long_units,
                    "entry": float(position["long"]["averagePrice"])
                }

            elif short_units < 0:
                positions[pair] = {
                    "direction": "SELL",
                    "units": short_units,
                    "entry": float(position["short"]["averagePrice"])
                }

        return positions

    except Exception as e:
        print("Open positions error:", e)
        return None


def sync_existing_positions():
    open_positions = get_open_positions_map()

    if open_positions is None:
        return

    for pair, position in open_positions.items():
        if pair in PAIRS and pair not in active_trades:
            active_trades[pair] = {
                "pair": pair,
                "direction": position["direction"],
                "entry": position["entry"],
                "score": 0,
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "profit_protection": False,
                "highest_progress": 0.0,
                "protected_exit": None,
                "units": position["units"]
            }

    for pair in list(active_trades.keys()):
        if pair not in open_positions:
            del active_trades[pair]


def block_failed_entry(pair):
    blocked_entries[pair] = datetime.now(timezone.utc) + timedelta(
        minutes=FAILED_ENTRY_BLOCK_MINUTES
    )


def is_entry_blocked(pair):
    if pair not in blocked_entries:
        return False

    now = datetime.now(timezone.utc)

    if now >= blocked_entries[pair]:
        del blocked_entries[pair]
        return False

    return True


def place_live_order(pair, direction, entry_price):
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
        block_failed_entry(pair)

        send_telegram(f"""
⚠️ LIVE ORDER FAILED

Pair: {pair}
Direction: {direction}
Units: {units}

Pair blocked for {FAILED_ENTRY_BLOCK_MINUTES} minutes.

Error:
{response.text}
""")
        return None

    data = response.json()

    if "orderFillTransaction" not in data:
        block_failed_entry(pair)

        send_telegram(f"""
⚠️ ORDER NOT FILLED

Pair: {pair}
Direction: {direction}

OANDA did not confirm a filled trade.
Bot will NOT mark this as open.

Pair blocked for {FAILED_ENTRY_BLOCK_MINUTES} minutes.
""")
        return None

    time.sleep(2)

    open_positions = get_open_positions_map()

    if open_positions is None or pair not in open_positions:
        block_failed_entry(pair)

        send_telegram(f"""
⚠️ ORDER NOT CONFIRMED AT BROKER

Pair: {pair}

OANDA did not show this position open.
Bot will NOT mark this as a live trade.

Pair blocked for {FAILED_ENTRY_BLOCK_MINUTES} minutes.
""")
        return None

    return {
        "units": open_positions[pair]["units"],
        "entry": open_positions[pair]["entry"],
        "response": data
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
        send_telegram(f"""
⚠️ LIVE CLOSE FAILED

Pair: {trade['pair']}

Error:
{response.text}
""")
        return False

    time.sleep(2)

    open_positions = get_open_positions_map()

    if open_positions is None:
        send_telegram(f"""
⚠️ CLOSE CHECK FAILED

Pair: {trade['pair']}

Could not verify with OANDA.
Bot will keep managing this trade.
""")
        return False

    if trade["pair"] in open_positions:
        send_telegram(f"""
⚠️ CLOSE NOT CONFIRMED

Pair: {trade['pair']}

OANDA still shows this trade open.
Bot will keep managing it.
""")
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


def can_open_trade(pair):
    if pair in active_trades:
        return False

    if is_entry_blocked(pair):
        return False

    if len(active_trades) >= MAX_ACTIVE_TRADES:
        return False

    return True


def is_in_cooldown():
    global cooldown_until

    if cooldown_until is None:
        return False

    now = datetime.now(timezone.utc)

    if now >= cooldown_until:
        cooldown_until = None
        send_telegram("✅ Cooldown finished. Bot can open new trades again.")
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

Reason: {STOP_LOSS_LIMIT_IN_WINDOW} losses within {STOP_LOSS_WINDOW_MINUTES} minutes.

New entries paused for {COOLDOWN_MINUTES} minutes.
Existing trades still managed.
""")


def open_trade(pair, direction, entry_price, score):
    if pair in active_trades:
        return

    live_result = place_live_order(pair, direction, entry_price)

    if live_result is None:
        return

    active_trades[pair] = {
        "pair": pair,
        "direction": direction,
        "entry": live_result["entry"],
        "score": score,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "profit_protection": False,
        "highest_progress": 0.0,
        "protected_exit": None,
        "units": live_result["units"]
    }

    send_telegram(f"""
🚀 LIVE TRADE OPENED

Pair: {pair}
Direction: {direction}
Units: {live_result["units"]}

Entry: {live_result["entry"]}
Score: {score}

Max Trades: {MAX_ACTIVE_TRADES}
Bot Stop Loss: {STOP_LOSS_PERCENT}%
Profit Protection Starts: +{PROFIT_PROTECTION_TRIGGER}%
""")


def calculate_tp_progress(trade, current_price):
    entry = trade["entry"]
    direction = trade["direction"]
    target_distance = get_target_distance(trade["pair"])

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

    closed = close_live_order(trade)

    if not closed:
        return

    result = "PROFIT" if progress > 0 else "LOSS"

    send_telegram(f"""
✅ LIVE TRADE CLOSED

Result: {result}

Pair: {pair}
Direction: {trade['direction']}

Entry: {trade['entry']}
Exit: {current_price}

Final Progress: {progress}%
Highest Progress: {trade['highest_progress']}%
Protected Exit: {trade.get('protected_exit')}

Reason:
{reason}
""")

    del active_trades[pair]

    if progress <= 0 or "STOP LOSS" in reason:
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
            close_trade(pair, "🛑 BOT STOP LOSS HIT", current_price, progress)
            continue

        if progress >= PROFIT_PROTECTION_TRIGGER and not trade["profit_protection"]:
            trade["profit_protection"] = True
            trade["protected_exit"] = calculate_protected_exit(trade["highest_progress"])

        if trade["profit_protection"]:
            new_protected_exit = calculate_protected_exit(trade["highest_progress"])

            if trade["protected_exit"] is None or new_protected_exit > trade["protected_exit"]:
                trade["protected_exit"] = new_protected_exit

            if progress <= trade["protected_exit"]:
                close_trade(pair, "🔒 DYNAMIC PROFIT PROTECTED EXIT", current_price, progress)
                continue


def scan_market():
    if not within_trading_hours():
        print("Outside trading hours. Managing trades only.")
        return

    if is_in_cooldown():
        print("Cooldown active. Skipping new entries.")
        return

    for pair in PAIRS:
        if not can_open_trade(pair):
            continue

        df = get_candles(pair)

        if df.empty:
            continue

        direction, score = score_trade(df, pair)

        print(f"{pair} | Direction: {direction} | Score: {score}")

        if direction and score >= MIN_SCORE:
            entry_price = float(df.iloc[-1]["close"])
            open_trade(pair, direction, entry_price, score)


def main():
    send_telegram(f"""
🤖 FOREX BOT STARTED

LIVE MODE ACTIVE

Pairs:
{", ".join(PAIRS)}

Units: {TRADE_UNITS}

Telegram Alerts:
Entry only after OANDA confirms position
Exit only after OANDA confirms close
Cooldown/errors only

Trading:
Runs most of the day
Avoids 5PM–8PM ET rollover/choppy hours

Max Active Trades: {MAX_ACTIVE_TRADES}

Bot-managed Stop Loss: {STOP_LOSS_PERCENT}%
Profit Protection Starts: +{PROFIT_PROTECTION_TRIGGER}%
Trailing Lock: Highest Profit - {TRAILING_PROFIT_GIVEBACK}%

Scan Speed: Every {SCAN_SECONDS} seconds

Bot now scanning.
""")

    sync_existing_positions()

    while True:
        try:
            sync_existing_positions()

            if market_is_closed():
                print("Forex market closed. Waiting...")
                time.sleep(300)
                continue

            scan_market()
            manage_open_trades()

            time.sleep(SCAN_SECONDS)

        except Exception as e:
            print("Main Loop Error:", e)
            send_telegram(f"⚠️ BOT ERROR: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()