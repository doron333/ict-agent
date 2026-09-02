"""ET session engine for NQ.

Tracks, per *trade date* (rolls at the CME Globex open, 18:00 ET):
  Asia / London / NY (RTH) ranges, the opening range, prior-day high/low,
  the Silver Bullet windows, and a news blackout list.

All inputs are UTC timestamps (pandas Timestamp, tz-aware datetime, or epoch
seconds). Conversion to America/New_York happens here and nowhere else.
"""
from dataclasses import dataclass, asdict
from datetime import datetime, date, timedelta, timezone

import pandas as pd

from .config import NQConfig

NY = pd.Timestamp.now(tz="America/New_York").tz  # zoneinfo via pandas — no extra dep


def to_utc(ts):
    """Anything -> tz-aware UTC pandas Timestamp."""
    if isinstance(ts, pd.Timestamp):
        return ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC")
    if isinstance(ts, datetime):
        return pd.Timestamp(ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).tz_convert("UTC")
    if isinstance(ts, (int, float)):
        return pd.Timestamp(float(ts), unit="s", tz="UTC")
    return pd.Timestamp(ts).tz_localize("UTC") if pd.Timestamp(ts).tzinfo is None else pd.Timestamp(ts).tz_convert("UTC")


def to_et(ts):
    return to_utc(ts).tz_convert("America/New_York")


def hm(s):
    """'09:30' -> minutes since midnight."""
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def in_window(minute, start, end):
    """Half-open [start, end); wraps midnight when start > end."""
    a, b = hm(start), hm(end)
    if a == b:
        return False
    if a < b:
        return a <= minute < b
    return minute >= a or minute < b


@dataclass
class DayLevels:
    trade_date: date
    pdh: float = None
    pdl: float = None
    asia_h: float = None
    asia_l: float = None
    lon_h: float = None
    lon_l: float = None
    or_h: float = None
    or_l: float = None
    ny_h: float = None
    ny_l: float = None
    globex_h: float = None
    globex_l: float = None
    open_0000: float = None      # "true day open" (00:00 ET)
    open_0930: float = None
    bars: int = 0

    def as_dict(self):
        return asdict(self)

    def pools(self):
        """Named liquidity levels for the detector: (name, price)."""
        out = []
        for name in ("pdh", "pdl", "asia_h", "asia_l", "lon_h", "lon_l", "or_h", "or_l"):
            v = getattr(self, name)
            if v is not None:
                out.append((name.upper(), float(v)))
        return out


class SessionEngine:
    def __init__(self, cfg: NQConfig = None):
        self.cfg = cfg or NQConfig()
        self.days = {}            # date -> DayLevels
        self.news = []            # (start_utc_ts, end_utc_ts, label)
        self.last_ts = None

    # ---------------- calendar ----------------
    def trade_date(self, ts) -> date:
        et = to_et(ts)
        minute = et.hour * 60 + et.minute
        d = et.date()
        if minute >= hm(self.cfg.trade_date_roll):
            d = d + timedelta(days=1)
        return d

    def window(self, ts):
        """Name of the active trade window at ts (bar close time), else None."""
        et = to_et(ts)
        minute = et.hour * 60 + et.minute
        for name, (a, b) in zip(self.cfg.window_names, self.cfg.windows):
            if in_window(minute, a, b):
                return name
        return None

    def in_rth(self, ts):
        et = to_et(ts)
        return in_window(et.hour * 60 + et.minute, *self.cfg.ny)

    # ---------------- news blackout ----------------
    def add_news(self, ts, label, before_min=None, after_min=None):
        """Register an event (UTC ts). Setups are suppressed inside [t-before, t+after]."""
        t = to_utc(ts)
        b = self.cfg.news_before_min if before_min is None else before_min
        a = self.cfg.news_after_min if after_min is None else after_min
        key = (t - pd.Timedelta(minutes=b), t + pd.Timedelta(minutes=a), str(label))
        if key not in self.news:
            self.news.append(key)
            self.news.sort()

    def clear_news(self):
        self.news = []

    def in_blackout(self, ts):
        """Label of the blackout covering ts, else None."""
        t = to_utc(ts)
        for a, b, label in self.news:
            if a <= t <= b:
                return label
        return None

    def upcoming_news(self, ts, hours=24):
        t = to_utc(ts)
        end = t + pd.Timedelta(hours=hours)
        return [(a + (b - a) / 2, label) for a, b, label in self.news if t <= b and a <= end]

    # ---------------- bar ingestion ----------------
    def _new_day(self, td):
        day = DayLevels(td)
        prev = [d for d in sorted(self.days) if d < td]
        src = self.cfg.pd_source
        for d in reversed(prev):
            p = self.days[d]
            if src == "globex" and p.globex_h is not None:
                day.pdh, day.pdl = p.globex_h, p.globex_l
                break
            if src != "globex" and p.ny_h is not None:
                day.pdh, day.pdl = p.ny_h, p.ny_l
                break
        self.days[td] = day
        # keep memory bounded
        for d in prev[:-10]:
            self.days.pop(d, None)
        return day

    def update(self, ts, o, h, l, c):
        """Feed one completed 1-min bar. `ts` is the bar OPEN time (UTC)."""
        et = to_et(ts)
        td = self.trade_date(ts)
        day = self.days.get(td) or self._new_day(td)
        minute = et.hour * 60 + et.minute
        cfg = self.cfg

        def rng(hname, lname):
            hv, lv = getattr(day, hname), getattr(day, lname)
            setattr(day, hname, h if hv is None else max(hv, h))
            setattr(day, lname, l if lv is None else min(lv, l))

        rng("globex_h", "globex_l")
        if in_window(minute, *cfg.asia):
            rng("asia_h", "asia_l")
        if in_window(minute, *cfg.london):
            rng("lon_h", "lon_l")
        if in_window(minute, *cfg.ny):
            rng("ny_h", "ny_l")
            if day.open_0930 is None:
                day.open_0930 = o
        if in_window(minute, *cfg.opening_range):
            rng("or_h", "or_l")
        if minute == 0 and day.open_0000 is None:
            day.open_0000 = o
        day.bars += 1
        self.last_ts = to_utc(ts)
        return day

    def ingest(self, df):
        """Bulk-feed a bar frame (UTC DatetimeIndex, open/high/low/close)."""
        for ts, row in zip(df.index, df[["open", "high", "low", "close"]].itertuples(index=False)):
            self.update(ts, *row)

    # ---------------- reads ----------------
    def levels(self, ts=None) -> DayLevels:
        if ts is None:
            ts = self.last_ts
        if ts is None:
            return DayLevels(date.today())
        td = self.trade_date(ts)
        return self.days.get(td) or self._new_day(td)

    def levels_text(self, ts=None):
        lv = self.levels(ts)
        ts = ts or self.last_ts
        et = to_et(ts) if ts is not None else None

        def f(v):
            return f"{v:,.2f}" if v is not None else "—"

        rows = [f"📐 <b>NQ levels</b> — trade date {lv.trade_date:%a %b %d}"
                + (f" · as of {et:%H:%M} ET" if et is not None else "")]
        rows.append(f"PDH {f(lv.pdh)} · PDL {f(lv.pdl)}  ({self.cfg.pd_source})")
        rows.append(f"Asia {f(lv.asia_h)} / {f(lv.asia_l)}  ({self.cfg.asia[0]}-{self.cfg.asia[1]})")
        rows.append(f"London {f(lv.lon_h)} / {f(lv.lon_l)}  ({self.cfg.london[0]}-{self.cfg.london[1]})")
        rows.append(f"OR {f(lv.or_h)} / {f(lv.or_l)}  ({self.cfg.opening_range[0]}-{self.cfg.opening_range[1]})")
        rows.append(f"RTH {f(lv.ny_h)} / {f(lv.ny_l)} · Globex {f(lv.globex_h)} / {f(lv.globex_l)}")
        rows.append(f"00:00 open {f(lv.open_0000)} · 09:30 open {f(lv.open_0930)}")
        w = self.window(ts) if ts is not None else None
        rows.append("Window: " + (f"<b>{w}</b> (active)" if w else "none — " + ", ".join(
            f"{n} {a}-{b}" for n, (a, b) in zip(self.cfg.window_names, self.cfg.windows)) + " ET"))
        bo = self.in_blackout(ts) if ts is not None else None
        if bo:
            rows.append(f"🚫 news blackout: {bo}")
        up = self.upcoming_news(ts, 12) if ts is not None else []
        if up:
            rows.append("News (12h): " + " · ".join(f"{to_et(t):%H:%M} {lbl}" for t, lbl in up[:4]))
        return "\n".join(rows)
