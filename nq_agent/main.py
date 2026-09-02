#!/usr/bin/env python3
"""NQ agent runtime — second instrument worker for the ICT team.

Two ways to run:
  in-process   NQ_ENABLED=1 python main.py            (recommended: one worker, one
               Telegram poller, shared ledger/risk/macro — the crypto team's
               Team object gets a `.nq` attribute and `nq ...` commands work)
  standalone   python main.py --instrument nq   or   python -m nq_agent.main
               (NQ only; starts its own command thread — don't run it next to the
               crypto worker on the SAME bot token, Telegram getUpdates allows one
               consumer per token)

Lifecycle per 1-min bar close: sessions.update → tracker (paper fills) →
detector → risk gate → ledger → Telegram. Alert-only: see execution.py.
"""
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

import pandas as pd

from .config import NQConfig
from .sessions import SessionEngine, to_et, to_utc
from .detector import detect, htf_bias
from .tracker import Tracker
from .ledger import NQLedger
from .risk import NQRisk
from .alerts import Alerts, default_sender
from . import news as news_mod
from .data.databento_feed import make_feed, normalize, COLS

GRADE_RANK = {"A": 3, "B": 2, "C": 1}
LOOKBACK_BARS = int(os.environ.get("NQ_LOOKBACK_BARS", "240"))   # bars handed to detect()


def _log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] nq: {msg}", flush=True)


class NQAgent:
    def __init__(self, cfg: NQConfig, root_ledger, root_risk, macro=None, send=None, data_dir=".", feed=None):
        self.cfg = cfg
        self.L = NQLedger(root_ledger)
        self.risk = NQRisk(root_risk, self.L, cfg)
        self.macro = macro
        self.alerts = Alerts(send or default_sender(), cfg)
        self.send = self.alerts.send
        self.sessions = SessionEngine(cfg)
        self.tracker = Tracker(cfg)
        self.feed = feed or make_feed(cfg)
        self.data_dir = data_dir
        self.df = pd.DataFrame(columns=COLS, index=pd.DatetimeIndex([], tz="UTC", name="ts"))
        self.bias = 0
        self.last_sweep_key = None
        self.bars_seen = 0
        self.started = time.time()
        self.last_news_sync = 0.0
        self.last_bias_calc = 0
        self.live = {"close": None, "bar_time": None, "window": None, "state": "warming up", "bias": "NEUT"}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.team = None                    # set in standalone mode (for team.tick())

    # ------------------------------------------------------------ data in
    def backfill(self):
        df = normalize(self.feed.backfill(self.cfg.backfill_days))
        if len(df) == 0:
            _log("backfill returned no bars")
            return 0
        self.sessions.ingest(df)
        self.sync_news(force=True)
        # warm replay: rebuild tracker state for setups the ledger still has open; no alerts, no new rows
        warm = df.tail(min(len(df), 2 * 1440))
        self.df = df.iloc[:max(0, len(df) - len(warm))].tail(self.cfg.keep_bars)
        for ts, r in zip(warm.index, warm.itertuples(index=False)):
            self.on_bar(ts, r.open, r.high, r.low, r.close, r.volume, live=False, update_sessions=False)
        self.bias = htf_bias(self.df)
        self.live["bias"] = {1: "BULL", -1: "BEAR"}.get(self.bias, "NEUT")
        _log(f"backfill: {len(df)} bars {df.index[0]:%m-%d %H:%M} → {df.index[-1]:%m-%d %H:%M} UTC · "
             f"levels for {self.sessions.levels().trade_date}")
        return len(df)

    def sync_news(self, force=False):
        if not force and time.time() - self.last_news_sync < 1800:
            return
        try:
            if self.macro is not None:
                self.macro.maybe_refresh()
            n = news_mod.sync(self.sessions, self.macro)
            self.last_news_sync = time.time()
            if force:
                _log(f"news blackout: {n} events loaded")
        except Exception as e:
            _log(f"news sync failed: {e}")

    def on_bar(self, ts, o, h, l, c, v=0.0, live=True, update_sessions=True):
        """One completed 1-min bar (ts = bar OPEN time, UTC)."""
        ts = to_utc(ts)
        with self.lock:
            if len(self.df) and ts <= self.df.index[-1]:
                return                                             # duplicate / out of order
            row = pd.DataFrame([[o, h, l, c, v]], columns=COLS, index=pd.DatetimeIndex([ts], name="ts"))
            self.df = pd.concat([self.df, row]) if len(self.df) else row
            if len(self.df) > self.cfg.keep_bars:
                self.df = self.df.iloc[-self.cfg.keep_bars:]
            if update_sessions:
                self.sessions.update(ts, o, h, l, c)
            self.bars_seen += 1
            for ev in self.tracker.on_bar(ts, o, h, l, c):
                self._on_event(ev, live)
            if self.bars_seen - self.last_bias_calc >= 15:
                self.bias = htf_bias(self.df)
                self.last_bias_calc = self.bars_seen
            setup = detect(self.df.tail(LOOKBACK_BARS), self.sessions, self.cfg, bias=self.bias)
            if setup is not None:
                self.handle_setup(setup, live)
            close_ts = ts + pd.Timedelta(minutes=1)
            w = self.sessions.window(close_ts)
            st = self.tracker.status_rows()
            self.live.update({"close": c, "bar_time": int(close_ts.timestamp()), "window": w,
                              "state": (st[0] if st else "idle"),
                              "bias": {1: "BULL", -1: "BEAR"}.get(self.bias, "NEUT")})
            if live:
                _log(f"bar {to_et(close_ts):%H:%M} ET close={c:.2f} window={w or '-'} bias={self.live['bias']} "
                     f"pend/open={len(self.tracker.pending)}/{len(self.tracker.open)}"
                     + (f" SETUP {setup.side} {setup.grade}" if setup else ""))

    # ------------------------------------------------------------ pipeline
    def handle_setup(self, setup, live=True):
        cfg = self.cfg
        key = (setup.sweep_time, setup.side)
        if cfg.one_per_sweep and self.last_sweep_key == key:
            return
        self.last_sweep_key = key
        if not live:
            # warm replay after a restart: only resume setups the ledger still has open
            row = self.L.find_sid(setup.sid)
            if row and row[1] in ("PENDING", "FILLED", "TP1", "BE") and cfg.mode == "paper":
                self.tracker.add(setup)
            return
        if GRADE_RANK.get(setup.grade, 0) < GRADE_RANK.get(cfg.min_grade, 2):
            _log(f"setup {setup.side} grade {setup.grade} below min {cfg.min_grade} — not alerted")
            return
        mlevel, mline = ("clear", "")
        if self.macro is not None:
            try:
                mlevel, mline = self.macro.check()
            except Exception:
                pass
        gated, notes, reason = self.risk.evaluate(setup)
        if not gated and self.macro is not None and getattr(self.macro, "mode", "") == "veto" and mlevel == "hot":
            gated, reason = True, f"macro veto — {mline.lstrip('🗓 ')}"
        lv = self.sessions.levels(setup.bar_time).as_dict()
        lv["trade_date"] = str(lv["trade_date"])
        rid = self.L.record(setup, context={"window": setup.window, "bias": self.bias, "levels": lv},
                            macro={"level": mlevel, "line": mline}, gated=gated, reason=reason)
        if cfg.mode == "paper":
            self.tracker.add(setup)                    # ghosts are tracked too
        if gated:
            self.send(self.alerts.gated_text(setup, rid, reason))
            return
        if self.L.paused():
            return
        lines = [self.alerts.setup_text(setup, rid)]
        if mline:
            lines.append(mline)
        lines += notes
        self.send("\n".join(lines))

    def _on_event(self, ev, live):
        res = self.L.on_event(ev)
        if not live:
            return
        gated = bool(res and res[1])
        text = self.alerts.event_text(ev)
        if gated:
            text = "👻 <i>(gated — ghost track)</i>\n" + text
        self.send(text)

    # ------------------------------------------------------------ texts
    @property
    def last_ts(self):
        return self.df.index[-1] + pd.Timedelta(minutes=1) if len(self.df) else None

    def status_text(self):
        lv = self.live
        up = int((time.time() - self.started) / 60)
        rows = ["📟 <b>NQ status</b>" + (" ⏸ PAUSED" if self.L.paused() else "")]
        rows.append(f"• {self.cfg.symbol} · mode {self.cfg.mode} · feed {self.cfg.feed} · up {up}m · {self.bars_seen} bars")
        if lv["close"] is not None:
            age = int((time.time() - lv["bar_time"]) / 60)
            rows.append(f"• last bar {to_et(lv['bar_time']):%H:%M} ET ({age}m ago) close {lv['close']:.2f} · "
                        f"bias {lv['bias']} · window {lv['window'] or 'none'}")
        else:
            rows.append("• no bars yet")
        rows.append(f"• tracker: {lv['state']}")
        for r in self.tracker.status_rows()[1:]:
            rows.append("  " + r)
        bo = self.sessions.in_blackout(self.last_ts) if self.last_ts is not None else None
        if bo:
            rows.append(f"• 🚫 news blackout: {bo}")
        rows.append("")
        rows.append(self.risk.status_text())
        open_rows = self.L.open_rows()
        if open_rows:
            rows.append("\nNQ open/pending:")
            for r in open_rows[:6]:
                rows.append(f"• #{r[0]} {r[1]} {r[2]} [{r[6]}] E{r[3]} S{r[4]} T{r[5]}")
        return "\n".join(rows)

    def levels_text(self):
        return self.sessions.levels_text(self.last_ts)

    def bias_text(self):
        if len(self.df) < 60:
            return "NQ bias: not enough bars yet."
        b = htf_bias(self.df)
        lv = self.sessions.levels(self.last_ts)
        px = float(self.df["close"].iloc[-1])
        rows = [f"🧭 <b>NQ bias</b>: <b>{ {1: 'BULL', -1: 'BEAR'}.get(b, 'NEUTRAL') }</b> (15m structure)"]
        rows.append(f"Price {px:,.2f} @ {to_et(self.last_ts):%H:%M} ET")

        def rel(name, v):
            if v is None:
                return None
            return f"{name} {v:,.2f} → {'above' if px > v else 'below'}"

        for name, v in (("00:00 open", lv.open_0000), ("09:30 open", lv.open_0930)):
            r = rel(name, v)
            if r:
                rows.append("• " + r)
        if lv.pdh is not None and lv.pdl is not None:
            mid = (lv.pdh + lv.pdl) / 2
            rows.append(f"• PD range {lv.pdl:,.2f}–{lv.pdh:,.2f}: {'premium' if px > mid else 'discount'}"
                        + (" (above PDH)" if px > lv.pdh else " (below PDL)" if px < lv.pdl else ""))
        for nm, hi, lo in (("Asia", lv.asia_h, lv.asia_l), ("London", lv.lon_h, lv.lon_l), ("OR", lv.or_h, lv.or_l)):
            if hi is not None:
                pos = "above" if px > hi else "below" if px < lo else "inside"
                rows.append(f"• {nm} {lo:,.2f}–{hi:,.2f}: {pos}")
        rows.append("Bias is scored (BIAS layer), not gated — setups fire both ways.")
        return "\n".join(rows)

    def stats_text(self, days=30):
        return self.L.stats_text(days)

    # ------------------------------------------------------------ runtime
    def run(self, once=False):
        """Backfill, announce, then consume bars until stopped."""
        fails = 0
        while not self.stop_event.is_set():
            try:
                n = self.backfill()
                break
            except Exception as e:
                fails += 1
                _log(f"backfill failed ({fails}): {e}")
                if fails in (3, 10):
                    self.send(f"⚠️ NQ agent: backfill failing ({e})")
                if once:
                    return
                time.sleep(min(300, 15 * fails))
        first = int(self.df.index[0].timestamp()) if len(self.df) else 0
        last = int(self.df.index[-1].timestamp()) if len(self.df) else 0
        self.send(self.alerts.online_text(len(self.df), first, last, self.data_dir))
        if once:
            return
        backoff = 5
        while not self.stop_event.is_set():
            try:
                if self.cfg.feed == "poll":
                    self._poll_loop()
                else:
                    # Databento live replay reaches back ~24h; older gaps (weekend, long outage) are skipped
                    start = self.last_ts
                    floor = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=23)
                    if start is not None and start < floor:
                        _log(f"replay start {start:%m-%d %H:%M} older than 23h — resuming from {floor:%m-%d %H:%M} UTC")
                        start = floor
                    for ts, o, h, l, c, v in self.feed.iter_live_bars(start=start, stop_event=self.stop_event):
                        self.on_bar(ts, o, h, l, c, v, live=True)
                        self._housekeep()
                    _log("feed ended — reconnecting")
                backoff = 5
            except Exception as e:
                traceback.print_exc()
                _log(f"feed error: {e} — retry in {backoff}s")
                time.sleep(backoff)
                backoff = min(300, backoff * 2)

    def _poll_loop(self):
        """NQ_FEED=poll: re-fetch the recent historical tail each minute (delayed data)."""
        while not self.stop_event.is_set():
            df = normalize(self.feed.poll_recent(90))
            last = self.df.index[-1] if len(self.df) else None
            new = df if last is None else df[df.index > last]
            for ts, r in zip(new.index, new.itertuples(index=False)):
                self.on_bar(ts, r.open, r.high, r.low, r.close, r.volume, live=True)
            self._housekeep()
            now = time.time()
            time.sleep(max(5, (int(now) // 60 + 1) * 60 + self.cfg.bar_grace_s - now))

    def _housekeep(self):
        self.sync_news()
        if self.team is not None:
            try:
                self.team.tick()
            except Exception as e:
                _log(f"team.tick failed: {e}")

    def run_forever(self):
        while not self.stop_event.is_set():
            try:
                self.run()
            except Exception as e:
                traceback.print_exc()
                _log(f"agent crashed: {e} — restarting in 30s")
                time.sleep(30)


# ---------------------------------------------------------------- entrypoints

def start_in_thread(team, send, data_dir="."):
    """Called by main.py when NQ_ENABLED=1: run the NQ agent inside the crypto worker."""
    cfg = NQConfig.from_env()
    agent = NQAgent(cfg, team.ledger, team.risk, team.macro, send, data_dir)
    team.nq = agent
    threading.Thread(target=agent.run_forever, daemon=True, name="nq-agent").start()
    _log("started in-process · " + cfg.summary())
    return agent


def run_standalone(once=False):
    """`python main.py --instrument nq` / `python -m nq_agent.main`."""
    sys.path.insert(0, os.getcwd())
    import main as root                                   # repo root: send_telegram, DATA_DIR, BASE_CFG
    from ledger import Ledger
    from risk_agent import RiskAgent
    from macro_agent import MacroAgent
    from context_agent import ContextAgent
    from review_agent import ReviewAgent
    from team import Team
    import commands as cmds

    cfg = NQConfig.from_env()
    ledger = Ledger(root.DATA_DIR, partial_r=cfg.partial_r if cfg.use_partial else 0.0)
    team = Team(ledger, ContextAgent(), MacroAgent(os.environ.get("MACRO_MODE", "warn")),
                RiskAgent(ledger), ReviewAgent(), root.BASE_CFG, root.send_telegram, root.fmt_event)
    team.live = {}
    agent = NQAgent(cfg, ledger, team.risk, team.macro, root.send_telegram, root.DATA_DIR)
    agent.team = team
    team.nq = agent
    team.macro.maybe_refresh(force=True)
    if not once:
        cmds.start(team, root.send_telegram, root.BOT, root.CHAT)
    _log("standalone · " + cfg.summary())
    agent.run(once=once)


if __name__ == "__main__":
    run_standalone(once="--once" in sys.argv[1:])
