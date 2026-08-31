# Binance USDT-Perp Scanner (RSI + Engulfing + Liquidity Sweep)

Alert-only bot. It does **not** place trades — it scans every USDT-margined
perpetual on Binance Futures on **both 1H and 4H** candles and sends you a
Telegram message when a signal fires on either timeframe.

## Strategy

**BUY alert** — all of the following on the same closed 4H candle:
1. RSI(7) was ≤30 on this candle or the previous one
2. A bullish engulfing candle formed
3. In the last 7 candles, price wicked below a recent swing low and closed
   back above it (a sell-side liquidity sweep — a stop-hunt before reversing up)

**SELL alert** — the mirror image:
1. RSI(7) was ≥70 on this candle or the previous one
2. A bearish engulfing candle formed
3. In the last 7 candles, price wicked above a recent swing high and closed
   back below it (a buy-side liquidity sweep)

All thresholds (RSI length/levels, pivot lookback, sweep lookback window,
timeframes) are adjustable constants at the top of `trading_bot.py`. To scan
only one timeframe, edit `TIMEFRAMES = ["1h", "4h"]` down to just one.

The bot polls every 2 minutes (`POLL_INTERVAL_SEC`) checking both timeframes
for newly closed candles, rather than sleeping until an exact candle close —
this keeps things simple since 1H and 4H candles close at different times.
Duplicate alerts on the same candle are prevented automatically.

## Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Create a Telegram bot:**
   - Message [@BotFather](https://t.me/BotFather) on Telegram
   - Send `/newbot` and follow the prompts
   - Copy the bot token it gives you

3. **Get your chat ID:**
   - Send any message to your new bot
   - Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
   - Find `"chat":{"id": ...}` in the response — that number is your chat ID

4. **Set environment variables:**
   ```bash
   export TELEGRAM_BOT_TOKEN="123456789:AAExampleTokenHere"
   export TELEGRAM_CHAT_ID="987654321"
   ```
   (Or just edit the two constants directly at the top of `trading_bot.py`.)

5. **Run it:**
   ```bash
   python3 trading_bot.py
   ```

## Running it 24/7

Your machine needs to stay on and connected for alerts to keep firing.
Options, roughly cheapest/simplest to more robust:
- A free-tier VPS (Oracle Cloud Free Tier, Railway, etc.)
- A Raspberry Pi or old PC left running at home
- `screen`/`tmux` + `nohup python3 trading_bot.py &` on any always-on Linux box
- A systemd service (ask me and I'll write the unit file) for auto-restart on crash/reboot

## Notes & limitations

- This is a **scanner + notifier only**. No orders are ever placed.
- It only evaluates **fully closed** candles, never the currently-forming one.
- `alerted_state.txt` tracks which candle+symbol+signal combos you've
  already been notified about, so you won't get duplicate pings for the
  same candle across scan cycles. Delete this file to reset.
- Binance Futures has ~250+ USDT perpetuals; a full scan takes roughly
  1–2 minutes depending on rate limits — well within the 4H window.
- RSI is computed with Wilder's smoothing to match TradingView's `ta.rsi()`.
- Pivot/swing detection and liquidity sweep logic here are a standard,
  independently-implemented version of the "smart money concepts" idea
  (swing structure + stop hunts) — not a copy of any specific published
  indicator's code.

## Disclaimer

This is a technical-analysis alerting tool, not financial advice. Signals
are based on lagging indicators and can produce false positives, especially
in choppy/low-volume markets. Always do your own risk management.
