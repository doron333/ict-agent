"""Macro agent — knows when CPI/FOMC/NFP land so setups aren't taken blind into news."""
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

NY = ZoneInfo("America/New_York")
URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
KEYWORDS = ("cpi", "fomc", "nonfarm", "non-farm", "interest rate", "gdp", "pce", "unemployment claims")


class MacroAgent:
    def __init__(self, mode="warn"):
        self.mode = mode if mode in ("off", "warn", "veto") else "warn"
        self.ts = 0.0
        self.events = []

    def maybe_refresh(self, force=False):
        if self.mode == "off":
            return
        if not force and time.time() - self.ts < 6 * 3600:
            return
        try:
            d = requests.get(URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=15).json()
            evs = []
            for e in d:
                if e.get("country") != "USD":
                    continue
                title = e.get("title", "")
                imp = e.get("impact", "")
                if imp != "High" and not any(k in title.lower() for k in KEYWORDS):
                    continue
                try:
                    ts = datetime.fromisoformat(e["date"]).timestamp()
                except Exception:
                    continue
                evs.append({"t": ts, "title": title, "impact": imp})
            self.events = sorted(evs, key=lambda x: x["t"])
            self.ts = time.time()
        except Exception:
            pass

    def check(self):
        """(level, line) for a setup being alerted right now.
        hot = inside 15m before / 30m after a High event; warn = within 60m before."""
        if self.mode == "off":
            return "clear", ""
        now = time.time()
        for e in self.events:
            if e["impact"] != "High":
                continue
            dm = (e["t"] - now) / 60
            if -30 <= dm <= 60:
                if dm < 0:
                    return "hot", f"🗓 {e['title']} hit {int(-dm)}m ago — post-news chop, spreads wide"
                lvl = "hot" if dm <= 15 else "warn"
                return lvl, f"🗓 {e['title']} in {int(dm)}m — event volatility risk"
        return "clear", ""

    def due_prealerts(self, already_sent):
        """~30m heads-up pings for High events (dedupe keys handled by caller)."""
        out = []
        if self.mode == "off":
            return out
        now = time.time()
        for e in self.events:
            if e["impact"] != "High":
                continue
            dm = (e["t"] - now) / 60
            key = f"pre|{int(e['t'])}|{e['title'][:24]}"
            if 25 <= dm <= 40 and key not in already_sent:
                when = datetime.fromtimestamp(e["t"], NY).strftime("%H:%M")
                out.append((key, f"🗓 Heads-up: <b>{e['title']}</b> at {when} NY (~{int(dm)}m) — volatility window"))
        return out

    def upcoming_text(self, n=6):
        now = time.time()
        evs = [e for e in self.events if e["t"] > now - 900][:n]
        if not evs:
            return "No tracked USD events on the calendar this week." + \
                   ("" if self.mode != "off" else " (macro agent is OFF)")
        rows = ["Upcoming USD events:"]
        for e in evs:
            d = datetime.fromtimestamp(e["t"], NY)
            star = "🔴" if e["impact"] == "High" else "🟠"
            rows.append(f"{star} {d:%a %b %d %H:%M} NY — {e['title']}")
        return "\n".join(rows)

    def digest(self, hours=30):
        now = datetime.now(NY)
        end = now.timestamp() + hours * 3600
        evs = [e for e in self.events if now.timestamp() < e["t"] <= end and e["impact"] == "High"]
        if not evs:
            return "No high-impact USD events in the next 30h."
        return "\n".join(f"• {datetime.fromtimestamp(e['t'], NY):%a %H:%M} NY — {e['title']}" for e in evs[:8])
