"""News blackout feed for the NQ session engine.

Primary source: the team's MacroAgent (macro_agent.py) — its ForexFactory
this-week calendar (USD, High impact + CPI/FOMC/NFP/GDP/PCE keywords).
Fallback: a small static list for the current month, used only on trade dates
where the macro feed has nothing (fetch failed, MACRO_MODE=off, or a date
outside "this week").

TODO: the static list is hand-entered and approximate. Verify against the
BLS/BEA/Fed release calendars at the start of each month, or better, extend
MacroAgent to a monthly calendar so this list can be dropped.
"""
import os
import re
from datetime import datetime

import pandas as pd

from .sessions import to_utc

KEYWORDS = ("cpi", "nonfarm", "non-farm", "nfp", "fomc", "interest rate", "ppi", "pce", "retail sales")

# ("YYYY-MM-DD HH:MM" ET, label) — TODO verify each month (see module docstring)
STATIC_EVENTS = [
    ("2026-09-04 08:30", "NFP (Aug)"),
    ("2026-09-10 08:30", "CPI (Aug)"),
    ("2026-09-11 08:30", "PPI (Aug)"),
    ("2026-09-16 08:30", "Retail Sales (Aug)"),
    ("2026-09-16 14:00", "FOMC decision"),
    ("2026-09-25 08:30", "PCE (Aug)"),
]


def _et(s):
    return pd.Timestamp(s, tz="America/New_York").tz_convert("UTC")


def env_events():
    """NQ_NEWS_EXTRA='2026-09-04 08:30 NFP;2026-09-16 14:00 FOMC'"""
    raw = os.environ.get("NQ_NEWS_EXTRA", "")
    out = []
    for part in raw.split(";"):
        part = part.strip()
        m = re.match(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})\s+(.+)", part)
        if m:
            out.append((_et(m.group(1).replace("T", " ")), m.group(2).strip()))
    return out


def macro_events(macro):
    """[(utc_ts, label)] from MacroAgent.events that matter for NQ."""
    out = []
    if macro is None or not getattr(macro, "events", None):
        return out
    for e in macro.events:
        title = e.get("title", "")
        if e.get("impact") == "High" or any(k in title.lower() for k in KEYWORDS):
            out.append((to_utc(e["t"]), title))
    return out


def sync(sessions, macro=None, now=None):
    """Rebuild the session engine's blackout list. Returns the number of events loaded."""
    now = to_utc(now) if now is not None else pd.Timestamp.now(tz="UTC")
    sessions.clear_news()
    evs = macro_events(macro)
    covered = {sessions.trade_date(t) for t, _ in evs}
    for s, label in STATIC_EVENTS:
        t = _et(s)
        if t < now - pd.Timedelta(days=1):
            continue
        if sessions.trade_date(t) in covered:
            continue                       # the live calendar owns that date
        evs.append((t, label + " (static)"))
    evs += env_events()
    n = 0
    for t, label in evs:
        if t < now - pd.Timedelta(days=2):
            continue
        sessions.add_news(t, label)
        n += 1
    return n


def static_for_backtest(sessions):
    """Backtests have no live calendar: load the static list + env extras only."""
    sessions.clear_news()
    for s, label in STATIC_EVENTS:
        sessions.add_news(_et(s), label)
    for t, label in env_events():
        sessions.add_news(t, label)
