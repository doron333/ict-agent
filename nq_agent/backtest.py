"""Bar-close backtester for the NQ detector + paper tracker.

Runs the exact live pipeline (sessions → detector → tracker) over historical
1-min bars. Nothing here touches the ledger: the ledger is a forward record.

Intrabar caveat: bars carry no sequence, so a bar that touches both stop and
target is scored as a STOP, and a bar that fills the limit and hits the stop is
a stop too. Results are conservative by construction.

Usage (from the repo root):
  python backtest.py --days 30                 # Databento, needs DATABENTO_API_KEY
  python backtest.py --csv data/nq_1m.csv      # any ts,open,high,low,close,volume CSV
  python backtest.py --days 30 --setups        # also print every setup
"""
import argparse
import os
import sys

import pandas as pd

from .config import NQConfig
from .sessions import SessionEngine, to_et
from .detector import detect, htf_bias
from .tracker import Tracker
from . import news as news_mod
from .data.databento_feed import DatabentoFeed, CsvFeed, normalize

GRADE_RANK = {"A": 3, "B": 2, "C": 1}
CAVEAT = ("Intrabar caveat: 1-min bars have no internal sequence — a bar touching both stop and target "
          "is scored as a STOP (and a fill bar that also hits the stop is a stop). Numbers are conservative.")


def run(df, cfg=None, lookback=240, sessions=None, min_grade=None, progress=None):
    """Replay `df` (UTC index, ohlcv) through the pipeline. Returns (stats, setups, positions)."""
    cfg = cfg or NQConfig()
    df = normalize(df)
    sessions = sessions or SessionEngine(cfg)
    if not sessions.news:
        news_mod.static_for_backtest(sessions)
    tracker = Tracker(cfg)
    min_grade = (min_grade or cfg.min_grade).upper()
    setups, last_key = [], None
    bias, bias_at = 0, 0
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    idx = df.index
    for i in range(len(df)):
        ts = idx[i]
        sessions.update(ts, o[i], h[i], l[i], c[i])
        tracker.on_bar(ts, o[i], h[i], l[i], c[i])
        if i - bias_at >= 15:
            bias = htf_bias(df.iloc[max(0, i - 2000):i + 1])
            bias_at = i
        s = detect(df.iloc[max(0, i + 1 - lookback):i + 1], sessions, cfg, bias=bias)
        if s is None:
            continue
        key = (s.sweep_time, s.side)
        if cfg.one_per_sweep and key == last_key:
            continue
        last_key = key
        if GRADE_RANK.get(s.grade, 0) < GRADE_RANK.get(min_grade, 2):
            continue
        if cfg.max_open > 0 and len(tracker.active()) >= cfg.max_open:
            continue
        setups.append(s)
        tracker.add(s)
        if progress:
            progress(s)
    return stats_of(tracker, setups, df), setups, tracker.closed + tracker.active()


def stats_of(tracker, setups, df):
    closed = [p for p in tracker.closed if p.r_net is not None]
    cancelled = [p for p in tracker.closed if p.r_net is None]
    rs = [p.r_net for p in closed]
    wins = [r for r in rs if r > 0.1]
    losses = [r for r in rs if r < -0.1]
    peak = cum = mdd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    by = {}
    for key, fn in (("grade", lambda p: p.setup.grade), ("window", lambda p: p.setup.window),
                    ("side", lambda p: p.setup.side), ("level", lambda p: p.setup.sweep_name.split("@")[0])):
        b = {}
        for p in closed:
            b.setdefault(fn(p), []).append(p.r_net)
        by[key] = {k: (len(v), sum(v), sum(v) / len(v)) for k, v in b.items()}
    days = max(1, len(set(ts.date() for ts in df.index)))
    return {"bars": len(df), "days": days, "from": df.index[0] if len(df) else None, "to": df.index[-1] if len(df) else None,
            "setups": len(setups), "closed": len(closed), "cancelled": len(cancelled),
            "wins": len(wins), "losses": len(losses), "be": len(rs) - len(wins) - len(losses),
            "win_rate": len(wins) / len(rs) if rs else 0.0, "total_r": sum(rs),
            "expectancy": sum(rs) / len(rs) if rs else 0.0,
            "avg_win": sum(wins) / len(wins) if wins else 0.0, "avg_loss": sum(losses) / len(losses) if losses else 0.0,
            "max_dd_r": mdd, "by": by}


def report_text(st, cfg):
    rows = [f"NQ backtest · {cfg.symbol} · {st['bars']} bars over {st['days']} days"
            + (f" ({st['from']:%Y-%m-%d} → {st['to']:%Y-%m-%d} UTC)" if st["from"] is not None else ""),
            f"config: {cfg.summary()}",
            f"setups {st['setups']} · closed {st['closed']} · cancelled/expired {st['cancelled']}",
            f"win rate {st['win_rate'] * 100:.0f}% ({st['wins']}W/{st['losses']}L/{st['be']}BE) · "
            f"expectancy {st['expectancy']:+.2f}R · total {st['total_r']:+.1f}R · max DD {st['max_dd_r']:+.1f}R",
            f"avg win {st['avg_win']:+.2f}R · avg loss {st['avg_loss']:+.2f}R"]
    for key, b in st["by"].items():
        if b:
            rows.append(f"by {key}: " + " · ".join(f"{k} n={n} {tot:+.1f}R (avg {avg:+.2f})"
                                                   for k, (n, tot, avg) in sorted(b.items())))
    rows.append(CAVEAT)
    return "\n".join(rows)


def load_frame(args, cfg):
    if args.csv:
        return CsvFeed(args.csv).backfill(days=args.days)
    feed = DatabentoFeed(cfg.symbol, cfg.dataset)
    if not feed.available():
        sys.exit("No data: set DATABENTO_API_KEY (Databento, GLBX.MDP3 ohlcv-1m) or pass --csv <file>.")
    return feed.backfill(days=args.days)


def main(argv=None):
    ap = argparse.ArgumentParser(description="NQ sweep→MSS→FVG backtest (bar-close, paper fills)")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--csv", default=os.environ.get("NQ_CSV", ""))
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--min-grade", default=None)
    ap.add_argument("--setups", action="store_true", help="print each setup")
    args = ap.parse_args(argv)
    cfg = NQConfig.from_env()
    if args.symbol:
        cfg.symbol = args.symbol
    df = load_frame(args, cfg)
    print(f"loaded {len(df)} bars", flush=True)
    printer = None
    if args.setups:
        def printer(s):
            print(f"  {to_et(s.bar_time):%m-%d %H:%M} ET {s.side:<5} {s.grade} {s.align:.0f}% {s.window} "
                  f"sweep {s.sweep_name}@{s.sweep_level} E{s.entry} S{s.stop} T{s.target} {s.rr}R  {s.layers}")
    st, setups, positions = run(df, cfg, min_grade=args.min_grade, progress=printer)
    print(report_text(st, cfg))


if __name__ == "__main__":
    main()
