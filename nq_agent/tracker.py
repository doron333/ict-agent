"""Paper tracker — simulates limit fills / stops / targets against completed bars.

This is the whole of "paper mode": no orders, no broker. It mirrors the crypto
engine's lifecycle so the shared ledger's R math applies unchanged:

  setup -> (limit) -> filled -> [tp1 (half off at +partial_r R)] -> [be (stop to entry)] -> closed
                   -> cancelled (expired after `expiry_bars`, or invalidated)

Intrabar caveat: 1-min bars have no sequence inside them. When a single bar
touches both the stop and the target the trade is scored as a STOP ("ambiguous"
is noted in the result). Same for the fill bar: if the bar that fills the limit
also runs through the stop, it is a stop. Backtest numbers are therefore
conservative; live fills may do better, never worse, than the record.
"""
from dataclasses import dataclass, field

import pandas as pd

from .config import NQConfig
from .detector import Setup


def net_r(result, tp1, rr, partial_r):
    """Mirror of ledger.Ledger._net_r so backtests can score without touching the DB."""
    if result.startswith("target"):
        return round(0.5 * partial_r + 0.5 * rr, 2) if tp1 else round(rr, 2)
    if result.startswith("break-even"):
        return round(0.5 * partial_r, 2) if tp1 else 0.0
    if result.startswith("stop"):
        return -1.0
    if result.startswith("runner"):
        return round(0.5 * partial_r - 0.5, 2)
    return None


@dataclass
class Position:
    setup: Setup
    state: str = "pending"          # pending | open
    bars_pending: int = 0
    stop: float = 0.0
    tp1: float = 0.0
    tp1_done: bool = False
    fill_time: object = None
    close_time: object = None
    result: str = ""
    r_net: float = None
    events: list = field(default_factory=list)


class Tracker:
    def __init__(self, cfg: NQConfig = None):
        self.cfg = cfg or NQConfig()
        self.pending = []
        self.open = []
        self.closed = []

    # ---------------- api ----------------
    def add(self, setup: Setup):
        cfg = self.cfg
        d = setup.dir
        ru = setup.risk_pts
        pos = Position(setup=setup, stop=setup.stop)
        pos.tp1 = setup.entry + d * cfg.partial_r * ru
        # partial disabled, or TP1 sits beyond the target -> no partial leg
        pos.tp1_done = (not cfg.use_partial) or ((pos.tp1 >= setup.target) if d == 1 else (pos.tp1 <= setup.target))
        self.pending.append(pos)
        return pos

    def has_open(self):
        return bool(self.open)

    def active(self):
        return self.pending + self.open

    def on_bar(self, ts, o, h, l, c):
        """Feed one completed bar (ts = bar OPEN time, UTC). Returns list of event dicts."""
        events = []
        cfg = self.cfg
        close_ts = pd.Timestamp(ts) + pd.Timedelta(minutes=1)
        for pos in list(self.pending):
            s = pos.setup
            d = s.dir
            pos.bars_pending += 1
            filled = (l <= s.entry) if d == 1 else (h >= s.entry)
            if filled:
                pos.state, pos.fill_time = "open", close_ts
                self.pending.remove(pos)
                self.open.append(pos)
                events.append(self._ev(pos, "filled", close_ts))
                events += self._manage(pos, h, l, c, close_ts)
                continue
            expired = pos.bars_pending > cfg.expiry_bars
            invalid = (c < s.fvg_bot or h >= s.target) if d == 1 else (c > s.fvg_top or l <= s.target)
            if expired or invalid:
                pos.result = "expired" if expired else "invalidated"
                pos.close_time = close_ts
                self.pending.remove(pos)
                self.closed.append(pos)
                events.append(self._ev(pos, "cancelled", close_ts, reason=pos.result))
        for pos in list(self.open):
            if pos.close_time is None:
                events += self._manage(pos, h, l, c, close_ts)
        return events

    # ---------------- internals ----------------
    def _manage(self, pos, h, l, c, close_ts):
        cfg = self.cfg
        s = pos.setup
        d = s.dir
        ru = s.risk_pts
        out = []
        hit_sl = (l <= pos.stop) if d == 1 else (h >= pos.stop)
        hit_tp = (h >= s.target) if d == 1 else (l <= s.target)
        hit_tp1 = (not pos.tp1_done) and ((h >= pos.tp1) if d == 1 else (l <= pos.tp1))
        if hit_sl and hit_tp:
            res = "stop -1R (ambiguous bar: stop and target both inside, scored as stop)"
            if abs(pos.stop - s.entry) < 1e-9:
                res = "break-even (ambiguous bar: BE stop and target both inside)"
            out.append(self._close(pos, res, close_ts))
            return out
        if hit_sl:
            if abs(pos.stop - s.entry) < 1e-9:
                res = "break-even"
            elif pos.tp1_done and cfg.use_partial and pos.tp1 != s.target:
                res = "runner stopped" if pos.tp1_done and self._tp1_banked(pos) else "stop -1R"
            else:
                res = "stop -1R"
            out.append(self._close(pos, res, close_ts))
            return out
        if hit_tp1:
            pos.tp1_done = True
            pos.events.append("tp1")
            out.append(self._ev(pos, "tp1", close_ts, result=f"partial +{cfg.partial_r:g}R banked"))
        if cfg.be_r > 0 and abs(pos.stop - s.entry) > 1e-9:
            trig = s.entry + d * cfg.be_r * ru
            if (h >= trig) if d == 1 else (l <= trig):
                pos.stop = s.entry
                out.append(self._ev(pos, "be", close_ts, result="stop moved to break-even"))
        if hit_tp:
            out.append(self._close(pos, f"target +{s.rr:.1f}R", close_ts))
        return out

    @staticmethod
    def _tp1_banked(pos):
        return "tp1" in pos.events

    def _close(self, pos, result, close_ts):
        pos.result = result
        pos.close_time = close_ts
        pos.r_net = net_r(result, self._tp1_banked(pos), pos.setup.rr, self.cfg.partial_r)
        if pos in self.open:
            self.open.remove(pos)
        self.closed.append(pos)
        return self._ev(pos, "closed", close_ts, result=result, r_net=pos.r_net)

    @staticmethod
    def _ev(pos, event, close_ts, **extra):
        s = pos.setup
        p = {"event": event, "sid": s.sid, "product": "NQ", "tf": "1m", "side": s.side,
             "entry": s.entry, "stop": round(pos.stop, 2), "target": s.target, "rr": s.rr,
             "grade": s.grade, "align": round(s.align), "layers": s.layers,
             "bar_time": int(close_ts.timestamp()), "setup": s}
        p.update(extra)
        return p

    # ---------------- status ----------------
    def status_rows(self):
        rows = []
        for pos in self.pending:
            s = pos.setup
            rows.append(f"⏳ {s.side} limit {s.entry} (S {s.stop} / T {s.target}, {s.rr}R) · {pos.bars_pending}/{self.cfg.expiry_bars} bars")
        for pos in self.open:
            s = pos.setup
            rows.append(f"📈 {s.side} open @ {s.entry} · stop {pos.stop:.2f} · target {s.target}"
                        + (" · TP1 banked" if self._tp1_banked(pos) else ""))
        return rows
