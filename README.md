# ICT Confluence Agent (free — no TradingView required)

Standalone Python port of the v3 Pine strategy. Pulls free Coinbase candles,
runs the sweep → MSS → FVG state machine with 13-layer confluence grading,
and pushes Telegram alerts for setup / filled / TP1 / break-even / closed /
cancelled events. Runs as an always-on Railway worker.

## Deploy on Railway (~5 min)
1. Push these 3 files (`main.py`, `requirements.txt`, `Procfile`) to a GitHub repo.
2. Railway → New Project → Deploy from GitHub repo.
3. Service → Settings → Start Command: `python -u main.py` (Procfile also covers it).
   No public networking / port needed — it's a worker, not a web app.
4. Variables:
   - `TELEGRAM_BOT_TOKEN` — from @BotFather
   - `TELEGRAM_CHAT_ID` — from api.telegram.org/bot<TOKEN>/getUpdates after messaging the bot
   - optional: `PRODUCTS=ETH-USD,BTC-USD` · `MIN_GRADE=B` · `ACCOUNT_SIZE=10000`
     `RISK_PCT=1.0` · `BIAS_MODE=Strict` · `USE_SESSIONS=1`
5. (Optional) attach a small Volume mounted anywhere and set `STATE_FILE=/data/state.json`
   so dedupe survives redeploys. Without it, restarts self-heal within one cycle.

## Test locally
    python main.py --backtest --days 5   # print recent signals from live data
    python main.py --once                # one live cycle (dry-run prints if no token)

## Behavior
- Wakes ~25s after every 15m bar close, recomputes everything from raw candles
  (deterministic), notifies only new events. Latency ≈ under a minute after close.
- `MIN_GRADE` filters setup alerts; lifecycle alerts follow only setups it announced.
- Sizing line in each alert uses ACCOUNT_SIZE × RISK_PCT × grade multiplier (A 1.0 / B 0.65 / C 0.40).
- Signals only — it never places orders. Trade them manually on Kraken/Avantis/wherever.
