"""Databento adapter for NQ.

Historical: dataset GLBX.MDP3, schema ohlcv-1m, stype_in="continuous", symbol
NQ.c.0 (front month by open interest) or MNQ.c.0.
Live:       schema trades, aggregated into 1-min bars client-side; the bar is
            emitted when the first trade of the next minute arrives, or
            `bar_grace_s` seconds after the minute ends if the tape is quiet.

The `databento` package is imported lazily so backtests on CSV and the unit
tests run without it. Every frame this module returns has a UTC DatetimeIndex
named `ts` (bar OPEN time) and float columns open/high/low/close/volume.

Env: DATABENTO_API_KEY.
"""
import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

COLS = ["open", "high", "low", "close", "volume"]


def normalize(df):
    """Any ohlcv-ish frame -> standard shape (UTC index 'ts', sorted, de-duplicated)."""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=COLS, index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    d = df.copy()
    if not isinstance(d.index, pd.DatetimeIndex):
        for cand in ("ts", "ts_event", "time", "timestamp", "datetime", "date"):
            if cand in d.columns:
                d[cand] = pd.to_datetime(d[cand], utc=True)
                d = d.set_index(cand)
                break
        else:
            raise ValueError("no timestamp column found")
    if d.index.tz is None:
        d.index = d.index.tz_localize("UTC")
    else:
        d.index = d.index.tz_convert("UTC")
    d.index.name = "ts"
    d = d[[c for c in COLS if c in d.columns]].astype(float)
    for c in COLS:
        if c not in d.columns:
            d[c] = 0.0
    d = d[~d.index.duplicated(keep="last")].sort_index()
    return d[COLS]


class LiveBarBuilder:
    """Trades -> completed 1-min bars."""

    def __init__(self, grace_s=5):
        self.grace_s = grace_s
        self.cur_min = None     # epoch minute of the bar being built
        self.o = self.h = self.l = self.c = None
        self.v = 0.0

    def _emit(self):
        bar = (pd.Timestamp(self.cur_min * 60, unit="s", tz="UTC"), self.o, self.h, self.l, self.c, self.v)
        self.cur_min = None
        self.o = self.h = self.l = self.c = None
        self.v = 0.0
        return bar

    def push(self, ts_ns, price, size):
        """Returns a completed bar tuple if this trade closed one, else None."""
        m = int(ts_ns // 60_000_000_000)
        out = None
        if self.cur_min is not None and m > self.cur_min:
            out = self._emit()
        if self.cur_min is None:
            self.cur_min = m
            self.o = self.h = self.l = self.c = float(price)
            self.v = 0.0
        self.h = max(self.h, float(price))
        self.l = min(self.l, float(price))
        self.c = float(price)
        self.v += float(size)
        return out

    def flush(self, now_s=None):
        """Emit the open bar if its minute has ended (plus grace)."""
        if self.cur_min is None:
            return None
        now_s = time.time() if now_s is None else now_s
        if now_s >= (self.cur_min + 1) * 60 + self.grace_s:
            return self._emit()
        return None


class DatabentoFeed:
    def __init__(self, symbol="NQ.c.0", dataset="GLBX.MDP3", api_key=None, grace_s=5):
        self.symbol = symbol
        self.dataset = dataset
        self.key = api_key or os.environ.get("DATABENTO_API_KEY", "")
        self.grace_s = grace_s
        self._db = None

    # ---------------- helpers ----------------
    def _lib(self):
        if self._db is None:
            import databento as db  # lazy: optional at import time
            self._db = db
        return self._db

    def available(self):
        return bool(self.key)

    # ---------------- historical ----------------
    def backfill(self, days=6, end=None):
        """Last `days` days of ohlcv-1m bars (continuous contract)."""
        db = self._lib()
        end = end or datetime.now(timezone.utc)
        # Historical availability lags real time; ask up to "now" and let the API clamp.
        start = end - timedelta(days=days)
        client = db.Historical(self.key)
        data = client.timeseries.get_range(
            dataset=self.dataset, symbols=[self.symbol], schema="ohlcv-1m", stype_in="continuous",
            start=start.strftime("%Y-%m-%dT%H:%M:%S"), end=end.strftime("%Y-%m-%dT%H:%M:%S"))
        df = data.to_df()
        return normalize(df)

    def poll_recent(self, minutes=90):
        """Polling fallback (NQ_FEED=poll): re-fetch the recent tail every minute.
        Note GLBX historical data is published with a delay, so bars arrive late."""
        end = datetime.now(timezone.utc)
        return self.backfill(days=minutes / 1440.0, end=end)

    # ---------------- live ----------------
    def iter_live_bars(self, start=None, stop_event=None):
        """Yield completed 1-min bars from the trades schema. `start` (UTC ts) replays
        the tape from that time (Databento supports intraday replay up to ~24h back)."""
        db = self._lib()
        q = queue.Queue()
        builder = LiveBarBuilder(self.grace_s)
        stop_event = stop_event or threading.Event()

        def reader():
            try:
                client = db.Live(key=self.key)
                kw = {}
                if start is not None:
                    kw["start"] = pd.Timestamp(start).tz_convert("UTC").to_pydatetime()
                client.subscribe(dataset=self.dataset, schema="trades", stype_in="continuous",
                                 symbols=[self.symbol], **kw)
                for rec in client:
                    if stop_event.is_set():
                        break
                    if isinstance(rec, db.TradeMsg):
                        q.put(("t", int(rec.ts_event), rec.price / 1e9, int(rec.size)))
                    elif isinstance(rec, db.ErrorMsg):
                        q.put(("e", str(getattr(rec, "err", rec))))
                q.put(("end", None))
            except Exception as e:  # surfaced to the consumer, which reconnects
                q.put(("e", repr(e)))
                q.put(("end", None))

        threading.Thread(target=reader, daemon=True, name="databento-live").start()
        while not stop_event.is_set():
            try:
                item = q.get(timeout=1.0)
            except queue.Empty:
                bar = builder.flush()
                if bar:
                    yield bar
                continue
            kind = item[0]
            if kind == "t":
                bar = builder.push(item[1], item[2], item[3])
                if bar:
                    yield bar
            elif kind == "e":
                raise RuntimeError(f"databento live: {item[1]}")
            elif kind == "end":
                bar = builder.flush(now_s=float("inf"))
                if bar:
                    yield bar
                return


class CsvFeed:
    """Replay a CSV (ts,open,high,low,close,volume) — for backtests and tests."""

    def __init__(self, path):
        self.path = path

    def available(self):
        return bool(self.path) and os.path.exists(self.path)

    def backfill(self, days=None, end=None):
        df = normalize(pd.read_csv(self.path))
        if end is not None:
            df = df[df.index <= pd.Timestamp(end).tz_convert("UTC")]
        if days:
            df = df[df.index >= df.index[-1] - pd.Timedelta(days=days)]
        return df

    def iter_live_bars(self, start=None, stop_event=None):
        df = self.backfill()
        if start is not None:
            df = df[df.index > pd.Timestamp(start).tz_convert("UTC")]
        for ts, r in zip(df.index, df.itertuples(index=False)):
            yield (ts, r.open, r.high, r.low, r.close, r.volume)


def make_feed(cfg):
    if cfg.feed == "csv" or (cfg.csv_path and not os.environ.get("DATABENTO_API_KEY")):
        return CsvFeed(cfg.csv_path)
    return DatabentoFeed(cfg.symbol, cfg.dataset, grace_s=cfg.bar_grace_s)
