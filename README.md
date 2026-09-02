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
`took <id> [price]` `skip <id>` `note <id> <text>` `pause` `resume` `help` · `nq …` (see NQ agent)

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

## NQ agent

A second instrument alongside ETH/BTC: **NQ futures, 1-min bars**, same sweep → MSS → FVG
model, driven by an ET session engine (`nq_agent/`). It reuses the team's Telegram bot,
ledger DB, risk gates and macro calendar — no second database, no second bot.

**Alert-only policy.** The NQ agent never places orders. `NQ_MODE=alert` posts setups;
`NQ_MODE=paper` (default) additionally simulates fills / stops / targets against completed
bars (`nq_agent/tracker.py`) so the ledger gets outcomes. `NQ_MODE=live` is refused.
`nq_agent/execution.py` is a clearly-marked stub for any future routing work.

### How it runs
- **In-process (recommended):** set `NQ_ENABLED=1` on the existing worker. One process, one
  Telegram poller, shared ledger. Startup posts `🟩 NQ agent online` after a 6-day backfill.
- **Standalone:** `python main.py --instrument nq` (or `python -m nq_agent.main`). The
  `Procfile` has an `nq:` process type, but Railway runs one process type per service — to run
  NQ separately, create a second Railway service on this repo with start command
  `python -u main.py --instrument nq`. Telegram `getUpdates` allows one consumer per bot
  token, so a standalone NQ service needs the crypto worker stopped or its own bot token.
- Data: Databento `GLBX.MDP3`, continuous symbol `NQ.c.0` (or `MNQ.c.0`), `stype_in=continuous`.
  Historical backfill uses schema `ohlcv-1m`; live uses `trades` aggregated to 1-min bars.
  `NQ_FEED=poll` re-fetches delayed `ohlcv-1m` each minute (no live subscription needed);
  `NQ_FEED=csv` + `NQ_CSV=` replays a file.

### Session engine (all wall-clock times ET, stored UTC)
Trade date rolls at 18:00 (Globex open). Levels for the current trade date: **PDH/PDL**
(prior RTH 09:30–16:00 by default, `NQ_PD_SOURCE=globex` for the full session), **Asia**
20:00–00:00, **London** 02:00–05:00, **OR** 09:30–09:45, plus RTH/Globex ranges and the
00:00 / 09:30 opens. Setups fire only inside the trade windows — the **Silver Bullet**
windows 03:00–04:00, 10:00–11:00, 14:00–15:00 (`NQ_WINDOWS`) — and never inside a
**news blackout**: ±15 min (`NQ_NEWS_BEFORE_MIN` / `NQ_NEWS_AFTER_MIN`) around High-impact
USD events from the macro agent's ForexFactory feed (CPI, NFP, FOMC, PPI, PCE, Retail
Sales at 08:30 / 14:00), with a small static monthly list in `nq_agent/news.py` as a
fallback when the feed has nothing (TODO there: verify it monthly) and `NQ_NEWS_EXTRA`
for ad-hoc additions.

### Detector thresholds (`nq_agent/config.py`, env `NQ_*`)
| Threshold | Default | Meaning |
|---|---|---|
| `sweep_min_ticks` | 2 | wick must trade this far through a level |
| `reclaim_bars` | 3 | close back inside within N bars |
| `mss_len` / `mss_window` | 3 / 20 | swing used for the MSS; sweep→FVG must finish in N bars |
| `disp_mult` | 1.2×ATR(14) | displacement candle body |
| `min_fvg_pts` | 2.0 | gap size |
| `stop_buf_pts` / `max_stop_pts` | 2 / 60 | stop past the sweep extreme; skip absurd risk |
| `min_rr` / `fixed_rr` | 2.0 / 2.0 | target = next liquidity paying ≥ min_rr, else fixed |
| `expiry_bars` | 30 | unfilled limit expires |

Tune these from `nq stats` and `backtest.py` evidence, not by hand to make it fire more.
Grades A/B/C come from weighted layers (LVL key level, BIAS 15m structure, SB window, DSP,
RR, PD, EQ, VOL); `NQ_MIN_GRADE` (B) gates alerts. Risk: the team's daily stop, max open
and loss-streak gates apply across crypto+NQ, plus `NQ_MAX_PER_DAY` (3) and `NQ_MAX_OPEN` (1).
Gated NQ setups are ghost-tracked like everything else.

### Telegram
`nq status` · `nq levels` (PDH/PDL/Asia/London/OR for the current trade date) · `nq bias` ·
`nq pause` / `nq resume` · `nq stats [days]` (ledger expectancy, default 30d) · `nq help`.

### Backtest
`python backtest.py --days 30` (Databento, needs `DATABENTO_API_KEY`) or
`python backtest.py --csv tests/fixtures/engineered_sweep_mss_fvg.csv`. Runs the live
pipeline bar by bar with paper fills; results never enter the ledger. **Intrabar caveat:**
1-min bars have no internal sequence, so a bar that touches both stop and target is scored
as a stop (and a fill bar that also hits the stop is a stop) — numbers are conservative.

### Tests
`pytest` — `tests/test_nq_detector.py` checks that a seeded random walk never fires and that an
engineered sweep→MSS→FVG (`tests/fixtures/engineered_sweep_mss_fvg.csv`) returns a Setup with
rr ≥ 2, plus tracker, session, ledger, news and command coverage.

### Env (see `.env.example`)
`NQ_ENABLED` · `DATABENTO_API_KEY` · `NQ_SYMBOL` · `NQ_DATASET` · `NQ_MODE` · `NQ_FEED` · `NQ_CSV` ·
`NQ_BACKFILL_DAYS` · `NQ_WINDOWS` · `NQ_PD_SOURCE` · `NQ_NEWS_BEFORE_MIN` · `NQ_NEWS_AFTER_MIN` ·
`NQ_NEWS_EXTRA` · `NQ_MIN_GRADE` · `NQ_MAX_PER_DAY` · `NQ_MAX_OPEN` · `NQ_ACCOUNT_SIZE` · `NQ_RISK_PCT` ·
and one `NQ_<THRESHOLD>` per row of the table above. On Railway add them to the worker's
Variables (the same Variables tab where `TELEGRAM_*` live); `DATA_DIR` follows the volume.
