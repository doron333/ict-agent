# ICT Confluence Agent — multi-timeframe (free, no TradingView)

Runs the sweep → reclaim → MSS → FVG confluence model independently on every
timeframe Coinbase serves, each with its own higher-timeframe bias:

| signal TF | bias TF      | session gate | ADR guard | KZ/VWAP layers |
|-----------|--------------|--------------|-----------|----------------|
| 1m        | 15m          | yes          | yes       | yes            |
| 5m        | 1h           | yes          | yes       | yes            |
| 15m       | 4h (1h×4)    | yes          | yes       | yes            |
| 1h        | 1d           | no           | yes       | yes            |
| 6h        | 1w (1d×7)    | no           | no        | no             |
| 1d        | 1w (1d×7)    | no           | no        | no             |

Every alert is tagged with its timeframe. One setup tracked at a time per
(product, TF); all grades are tracked on paper, only grades ≥ MIN_GRADE ping.

## Deploy on Railway
1. Repo needs: `main.py`, `requirements.txt`, `Procfile`.
2. Railway → Deploy from GitHub repo. Worker, no port needed.
3. Variables: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and optionally
   `PRODUCTS=ETH-USD,BTC-USD` · `SIGNAL_TFS=1m,5m,15m,1h,6h,1d`
   `MIN_GRADE=B` · per-TF overrides like `MIN_GRADE_1M=A` `MIN_GRADE_5M=A`
   `ACCOUNT_SIZE=10000` · `RISK_PCT=1.0` · `BIAS_MODE=Strict` · `USE_SESSIONS=1`
4. Optional Volume + `STATE_FILE=/data/state.json` to survive redeploys cleanly.

## Test locally
    python main.py --backtest    # recent signals per timeframe, live data
    python main.py --once        # one live cycle (dry-run prints without token)

## Behavior notes
- Worker wakes every minute (+20s) and recomputes only TFs whose bar just closed.
- 1m is the noisiest and sees only ~12h of history per pass — consider
  `MIN_GRADE_1M=A` or dropping it from SIGNAL_TFS.
- 1d/6h with Strict weekly bias fire rarely by design. `BIAS_MODE=Lenient`
  loosens all TFs at once.
- Signals only — no orders are ever placed.
