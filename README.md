# ICT Trading Team — multi-agent signal system

One Railway worker, six specialist agents, one shared ledger. Watches ETH/BTC on six
timeframes with the sweep → reclaim → MSS → FVG confluence model and coordinates
everything through Telegram. **Notify-only by design** — it never touches an exchange.

## The team

| Agent | Job | Feeds on |
|---|---|---|
| **Signal** ×6 | ICT confluence engine per TF (1m→1d), 13 weighted layers, A/B/C grades | Coinbase candles |
| **Context** | Market regime stamped onto every alert; crowding warnings | OKX funding, Kraken OI, Fear&Greed, BTC dominance |
| **Macro** | Event awareness: 30-min heads-ups, warn/veto near red news | ForexFactory calendar (USD, High impact) |
| **Risk** | Portfolio guardrails across all TFs — gates are logged, never silent | The ledger |
| **Review** | Optional LLM second opinion (TAKE/CAUTION/SKIP) on each B+ setup | Claude API (needs your key) |
| **Learning** | Scorecards, per-layer lift, weight tuning proposals | The ledger |

## Risk guardrails (env-tunable)
- `DAILY_STOP_R` (default 3): day ≤ −3R → all further setups gated
- `MAX_OPEN` (default 3): concurrent open setups cap
- 2 consecutive losses → only A grades until a winner resets it
- ETH+BTC same-direction → correlation warning (half-size suggestion)

Gated setups are still tracked as **ghosts** (👻) so the learning agent can audit
whether the gates save or cost money.

## Telegram commands
`status` `scorecard` `context` `events` `risk` `weights` `apply weights`
`took <id> [price]` `skip <id>` `note <id> <text>` `pause` `resume` `help`

## R accounting (honest math)
Half off at TP1 (+1R), stop to BE: full win = 0.5·1R + 0.5·target-R.
BE after TP1 = +0.5R. Straight stop = −1R. No backtest data is ever mixed
into the ledger — forward record only.

## Weight adaptation
After 40 closed setups the learning agent computes per-layer lift (avg R with
layer ✓ vs ✗) and **proposes** new weights. Nothing changes until you text
`apply weights`. Clamped to 0.25–3.0, ±40% per revision.

## Env
Required: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
Core: `PRODUCTS` (ETH-USD) · `SIGNAL_TFS` (1m,5m,15m,1h,6h,1d) · `MIN_GRADE` (B, per-TF `MIN_GRADE_1M=A`) · `ACCOUNT_SIZE` (10000) · `RISK_PCT` (1.0) · `USE_SESSIONS` (1) · `BIAS_MODE` (Strict)
Team: `DATA_DIR` (auto: /data if a volume is mounted) · `DAILY_STOP_R` (3) · `MAX_OPEN` (3) · `MACRO_MODE` (warn|veto|off) · `ADAPT_MIN` (40)
Review: `ANTHROPIC_API_KEY` (unset = agent off) · `ANTHROPIC_MODEL` (claude-haiku-4-5-20251001) · `REVIEW_MIN_GRADE` (B)

## Persistence
Attach a Railway volume at `/data` — the ledger and dedupe state live there and
survive redeploys. Without it the code still runs, but history resets on deploy.

## Run
`python main.py` (worker) · `python main.py --once` (single cycle) · `python main.py --backtest` (recent signals per TF, no team)
