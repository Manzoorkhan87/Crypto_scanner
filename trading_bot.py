"""
Binance USDT-Perpetual Scanner
--------------------------------
Strategy:
  1. RSI(7) oversold/overbought + bullish/bearish engulfing candle  (signal)
  2. Confirmed by a recent liquidity sweep in the opposite direction
     (a swing low/high got wicked through and price closed back beyond it)

Runs on 1H AND 4H candles (both, independently), scans every USDT-margined
perpetual on Binance Futures, and sends a Telegram message when a confirmed
signal appears on a freshly closed candle on either timeframe.

Setup:
  1. pip install -r requirements.txt
  2. Create a Telegram bot via @BotFather, get the token
  3. Message your bot once, then get your chat_id via:
     https://api.telegram.org/bot<TOKEN>/getUpdates
  4. Set environment variables (or edit CONFIG below):
       export TELEGRAM_BOT_TOKEN="123456:ABC-..."
       export TELEGRAM_CHAT_ID="123456789"
  5. python3 trading_bot.py

Run it on a VPS / always-on machine (PC sleeping = no alerts).
"""

import os
import time
import logging
import datetime as dt

import pandas as pd
import numpy as np
import requests

try:
    import ccxt
except ImportError:
    ccxt = None  # only required when actually connecting to the exchange

# ============================================================
# CONFIG
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")

TIMEFRAMES = ["1h", "4h"]  # scan both, independently
RSI_LENGTH = 7             # matches your Pine script
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30

PIVOT_LOOKBACK = 12        # bars each side to confirm a swing high/low

CANDLES_TO_FETCH = 150     # history pulled per symbol per scan (need enough
                            # for pivots + RSI to stabilize)

DEBUG_FUNNEL_STATS = True   # log why signals are/aren't firing across the
                             # whole scan -- turn off once tuned

SCAN_PAUSE_SEC = 0.1        # pause between requests (rate-limit safety)
QUOTE = "USDT"              # only scan USDT-margined perpetuals
POLL_INTERVAL_SEC = 120     # how often to check for newly-closed candles.
                             # We poll frequently rather than trying to sleep
                             # exactly until candle close, since 1h and 4h
                             # close at different times. Duplicate alerts on
                             # the same candle are prevented by alerted_state.

STATE_FILE = "alerted_state.txt"   # tracks which candles we've already alerted on

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("scanner")

# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message: str):
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN or "PUT_YOUR" in TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured, skipping send. Message was:\n%s", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        if not r.ok:
            log.error("Telegram send failed: %s", r.text)
    except Exception as e:
        log.error("Telegram send exception: %s", e)


# ============================================================
# ALREADY-ALERTED STATE (avoid duplicate pings for the same candle)
# ============================================================

def load_state() -> set:
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())


def save_state(key: str):
    with open(STATE_FILE, "a") as f:
        f.write(key + "\n")


# ============================================================
# EXCHANGE / SYMBOLS
# ============================================================

def get_exchange():
    if ccxt is None:
        raise SystemExit("Missing dependency 'ccxt'. Install it with: pip install ccxt")
    ex = ccxt.binanceusdm({
        "enableRateLimit": True,
    })
    ex.load_markets()
    return ex


def get_usdt_perpetual_symbols(ex) -> list:
    symbols = []
    for m in ex.markets.values():
        if (
            m.get("swap")
            and m.get("quote") == QUOTE
            and m.get("active", True)
            and m.get("linear", True)
        ):
            symbols.append(m["symbol"])
    return sorted(symbols)


def fetch_ohlcv_df(ex, symbol: str, timeframe: str, limit: int):
    raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


# ============================================================
# INDICATORS
# ============================================================

def compute_rsi(close: pd.Series, length: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Wilder's smoothing (RMA), matching Pine Script's ta.rsi
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(avg_loss != 0, 100.0)
    return rsi


def compute_engulfing(df: pd.DataFrame):
    close, open_ = df["close"], df["open"]
    prev_close, prev_open = close.shift(1), open_.shift(1)
    bull_engulf = (close > prev_open) & (prev_close < prev_open)
    bear_engulf = (close < prev_open) & (prev_close > prev_open)
    return bull_engulf, bear_engulf


def compute_pivots(df: pd.DataFrame, lookback: int):
    """
    A pivot high at index i is a bar whose high is the max within
    [i-lookback, i+lookback]. Same idea for pivot low.
    Because it needs 'lookback' bars of future data to confirm, a pivot
    at index i is only known once we reach index i + lookback.
    """
    high, low = df["high"], df["low"]
    n = len(df)
    piv_high = pd.Series(False, index=df.index)
    piv_low = pd.Series(False, index=df.index)

    for i in range(lookback, n - lookback):
        window_h = high.iloc[i - lookback: i + lookback + 1]
        window_l = low.iloc[i - lookback: i + lookback + 1]
        if high.iloc[i] == window_h.max() and (window_h == window_h.max()).sum() == 1:
            piv_high.iloc[i] = True
        if low.iloc[i] == window_l.min() and (window_l == window_l.min()).sum() == 1:
            piv_low.iloc[i] = True

    return piv_high, piv_low


def compute_sweep_and_bos_same_candle(df: pd.DataFrame, piv_high: pd.Series, piv_low: pd.Series):
    """
    Stricter version matching the user's chart: the liquidity sweep AND the
    break-of-structure (BOS) confirmation must happen on the SAME candle.

    Bullish sweep+BOS candle:
      - low[i]  wicks BELOW the most recent confirmed swing low  (sweeps sell-side liquidity)
      - close[i] closes ABOVE the most recent confirmed swing high (breaks structure / BOS)
      Both true on the same bar i.

    Bearish sweep+BOS candle (mirror):
      - high[i] wicks ABOVE the most recent confirmed swing high (sweeps buy-side liquidity)
      - close[i] closes BELOW the most recent confirmed swing low  (breaks structure / BOS)

    Returns two boolean Series aligned to df.index.
    """
    n = len(df)
    bull_sweep_bos = pd.Series(False, index=df.index)
    bear_sweep_bos = pd.Series(False, index=df.index)

    last_piv_high_val = None
    last_piv_low_val = None

    for i in range(n):
        # Check same-candle sweep+BOS BEFORE updating with this candle's own pivot
        # (a pivot can only be "known" using bars before/around it, and since our
        # pivot detection already looks lookback bars into the future to confirm,
        # the pivot value itself is safe to compare against future candles).
        if last_piv_low_val is not None and last_piv_high_val is not None:
            if df["low"].iloc[i] < last_piv_low_val and df["close"].iloc[i] > last_piv_high_val:
                bull_sweep_bos.iloc[i] = True
            if df["high"].iloc[i] > last_piv_high_val and df["close"].iloc[i] < last_piv_low_val:
                bear_sweep_bos.iloc[i] = True

        if piv_high.iloc[i]:
            last_piv_high_val = df["high"].iloc[i]
        if piv_low.iloc[i]:
            last_piv_low_val = df["low"].iloc[i]

    return bull_sweep_bos, bear_sweep_bos


# ============================================================
# STRATEGY EVALUATION FOR ONE SYMBOL
# ============================================================

def evaluate_symbol(df: pd.DataFrame):
    """
    Returns (signal_type, info_dict) for the LAST FULLY CLOSED candle,
    or (None, None) if no signal.
    signal_type is 'BUY', 'SELL', or None.
    """
    if len(df) < (2 * PIVOT_LOOKBACK + RSI_LENGTH + 5):
        return None, None

    df = df.copy()
    df["rsi"] = compute_rsi(df["close"], RSI_LENGTH)
    df["rsi_os"] = df["rsi"] <= RSI_OVERSOLD
    df["rsi_ob"] = df["rsi"] >= RSI_OVERBOUGHT

    bull_engulf, bear_engulf = compute_engulfing(df)
    df["bull_engulf"] = bull_engulf
    df["bear_engulf"] = bear_engulf

    piv_high, piv_low = compute_pivots(df, PIVOT_LOOKBACK)
    bull_sweep_bos, bear_sweep_bos = compute_sweep_and_bos_same_candle(df, piv_high, piv_low)
    df["bull_sweep_bos"] = bull_sweep_bos   # Candle A condition for a future BUY
    df["bear_sweep_bos"] = bear_sweep_bos   # Candle A condition for a future SELL

    # Use the last CLOSED candle: if the exchange returns the still-forming
    # candle as the last row, use iloc[-2]. We always drop the last row to
    # be safe, since fetch_ohlcv's final bar is usually still forming.
    closed = df.iloc[:-1]
    if len(closed) < 2:
        return None, None

    i = len(closed) - 1
    row = closed.iloc[i]          # Candle B — the just-closed candle, checked for engulfing
    prev = closed.iloc[i - 1]     # Candle A — the sweep + BOS candle
    prev2 = closed.iloc[i - 2] if i - 2 >= 0 else prev  # candle just before the sweep

    # RSI is checked on the candle(s) BEFORE the sweep, not the sweep candle
    # itself -- the sweep+BOS candle's own close is already the reversal move,
    # so its RSI reflects the recovery, not the oversold/overbought extreme
    # that preceded it.
    rsi_signal_bull = prev2["rsi_os"] or prev["rsi_os"]
    rsi_signal_bear = prev2["rsi_ob"] or prev["rsi_ob"]

    # Candle A must be the sweep+BOS candle; Candle B (current) must engulf it
    buy_trigger = prev["bull_sweep_bos"] and row["bull_engulf"] and rsi_signal_bull
    sell_trigger = prev["bear_sweep_bos"] and row["bear_engulf"] and rsi_signal_bear

    info = {
        "close": row["close"],
        "rsi": round(row["rsi"], 2) if not np.isnan(row["rsi"]) else None,
        "candle_time": df["ts"].iloc[df.index.get_loc(row.name)],
    }

    if buy_trigger:
        return "BUY", info
    if sell_trigger:
        return "SELL", info

    return None, None


def debug_funnel_counts(df: pd.DataFrame) -> dict:
    """
    Diagnostic only: counts how many times each stage of the funnel
    occurred across the ENTIRE fetched history for one symbol (not just
    the last candle). Used to figure out which condition is the bottleneck
    when real-world alert volume looks too low.
    """
    if len(df) < (2 * PIVOT_LOOKBACK + RSI_LENGTH + 5):
        return {}

    df = df.copy()
    df["rsi"] = compute_rsi(df["close"], RSI_LENGTH)
    df["rsi_os"] = df["rsi"] <= RSI_OVERSOLD
    df["rsi_ob"] = df["rsi"] >= RSI_OVERBOUGHT
    bull_engulf, bear_engulf = compute_engulfing(df)
    piv_high, piv_low = compute_pivots(df, PIVOT_LOOKBACK)
    bull_sweep_bos, bear_sweep_bos = compute_sweep_and_bos_same_candle(df, piv_high, piv_low)

    n = len(df)
    bull_full = 0
    bear_full = 0
    bull_full_with_rsi = 0
    bear_full_with_rsi = 0

    for i in range(2, n):
        if bull_sweep_bos.iloc[i - 1] and bull_engulf.iloc[i]:
            bull_full += 1
            if df["rsi_os"].iloc[i - 2] or df["rsi_os"].iloc[i - 1]:
                bull_full_with_rsi += 1
        if bear_sweep_bos.iloc[i - 1] and bear_engulf.iloc[i]:
            bear_full += 1
            if df["rsi_ob"].iloc[i - 2] or df["rsi_ob"].iloc[i - 1]:
                bear_full_with_rsi += 1

    return {
        "pivot_highs": int(piv_high.sum()),
        "pivot_lows": int(piv_low.sum()),
        "bull_sweep_bos": int(bull_sweep_bos.sum()),
        "bear_sweep_bos": int(bear_sweep_bos.sum()),
        "bull_engulf": int(bull_engulf.sum()),
        "bear_engulf": int(bear_engulf.sum()),
        "bull_full_pattern": bull_full,
        "bear_full_pattern": bear_full,
        "bull_full_with_rsi": bull_full_with_rsi,
        "bear_full_with_rsi": bear_full_with_rsi,
    }


# ============================================================
# MAIN SCAN LOOP
# ============================================================

def run_scan(ex, symbols, alerted):
    log.info("Starting scan of %d symbols across %s...", len(symbols), TIMEFRAMES)
    hits = 0
    funnel_totals = {}
    symbols_scanned = 0

    for symbol in symbols:
        for timeframe in TIMEFRAMES:
            try:
                df = fetch_ohlcv_df(ex, symbol, timeframe, CANDLES_TO_FETCH)
                signal, info = evaluate_symbol(df)

                if DEBUG_FUNNEL_STATS:
                    stats = debug_funnel_counts(df)
                    if stats:
                        symbols_scanned += 1
                        for k, v in stats.items():
                            funnel_totals[k] = funnel_totals.get(k, 0) + v

                if signal:
                    candle_key = f"{symbol}|{timeframe}|{info['candle_time']}|{signal}"
                    if candle_key not in alerted:
                        msg = (
                            f"*{signal} SIGNAL* — `{symbol}`\n"
                            f"Timeframe: {timeframe}\n"
                            f"Candle close: {info['candle_time']}\n"
                            f"Price: {info['close']}\n"
                            f"RSI({RSI_LENGTH}): {info['rsi']}\n"
                            f"Sweep+BOS confirmed, followed by engulfing reversal"
                        )
                        send_telegram(msg)
                        log.info("ALERT: %s %s [%s] @ %s", signal, symbol, timeframe, info["close"])
                        alerted.add(candle_key)
                        save_state(candle_key)
                        hits += 1
            except ccxt.BaseError as e:
                log.warning("Exchange error for %s [%s]: %s", symbol, timeframe, e)
            except Exception as e:
                log.warning("Error processing %s [%s]: %s", symbol, timeframe, e)
            time.sleep(SCAN_PAUSE_SEC)

    if DEBUG_FUNNEL_STATS and symbols_scanned:
        log.info(
            "FUNNEL STATS (across %d symbol-timeframe checks, %d candles each): "
            "pivotH=%d pivotL=%d | bull_sweep_bos=%d bear_sweep_bos=%d | "
            "bull_engulf=%d bear_engulf=%d | bull_full_pattern=%d bear_full_pattern=%d | "
            "bull_full_with_rsi=%d bear_full_with_rsi=%d",
            symbols_scanned, CANDLES_TO_FETCH,
            funnel_totals.get("pivot_highs", 0), funnel_totals.get("pivot_lows", 0),
            funnel_totals.get("bull_sweep_bos", 0), funnel_totals.get("bear_sweep_bos", 0),
            funnel_totals.get("bull_engulf", 0), funnel_totals.get("bear_engulf", 0),
            funnel_totals.get("bull_full_pattern", 0), funnel_totals.get("bear_full_pattern", 0),
            funnel_totals.get("bull_full_with_rsi", 0), funnel_totals.get("bear_full_with_rsi", 0),
        )

    log.info("Scan complete. %d signal(s) found.", hits)


def main():
    log.info("Initializing exchange connection...")
    ex = get_exchange()
    symbols = get_usdt_perpetual_symbols(ex)
    log.info("Found %d USDT-margined perpetual symbols.", len(symbols))

    alerted = load_state()

    send_telegram(
        f"✅ Scanner started. Watching {len(symbols)} USDT perpetuals on Binance "
        f"across {', '.join(TIMEFRAMES)} candles "
        f"(RSI({RSI_LENGTH}) + Engulfing + Liquidity Sweep)."
    )

    while True:
        try:
            symbols = get_usdt_perpetual_symbols(ex)  # refresh in case new listings
            run_scan(ex, symbols, alerted)
        except Exception as e:
            log.error("Scan loop error: %s", e)
        log.info("Sleeping %d seconds before next poll...", POLL_INTERVAL_SEC)
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
