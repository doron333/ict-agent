#!/usr/bin/env python3
"""
ICT Confluence Engine — multi-timeframe signal agent (no TradingView required)
==============================================================================
Runs the sweep -> reclaim -> MSS -> FVG confluence model independently on EVERY
timeframe Coinbase serves, each with an appropriate higher-timeframe bias:

    signal TF   bias TF     sessions gate   ADR guard   KZ/VWAP layers
    1m          15m         yes             yes         yes
    5m          1h          yes             yes         yes
    15m         4h (1h x4)  yes             yes         yes
    1h          1d          no              yes         yes
    6h          1w (1d x7)  no              no          no
    1d          1w (1d x7)  no              no          no

Data:    Coinbase Exchange public API (free, no key), incremental cache.
Runtime: worker wakes each minute (+~20s), recomputes only the TFs whose bar
         just closed, notifies new events via Telegram.
Deploy:  Railway worker. `python -u main.py`
Test:    python main.py --backtest            (recent signals per TF)
         python main.py --once                (single live cycle)

Env:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID    (without them: dry-run prints)
  PRODUCTS        default "ETH-USD"        e.g. "ETH-USD,BTC-USD"
  SIGNAL_TFS      default "1m,5m,15m,1h,6h,1d"
  MIN_GRADE       default "B"; per-TF override: MIN_GRADE_1M=A etc.
  ACCOUNT_SIZE    default "10000"
  RISK_PCT        default "1.0"
  USE_SESSIONS    default "1"   ("0" disables the kill-zone hard gate everywhere)
  BIAS_MODE       default "Strict"   Strict | Lenient | Off
"""

import json
import os
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from ledger import Ledger
from context_agent import ContextAgent
from macro_agent import MacroAgent
from risk_agent import RiskAgent
from review_agent import ReviewAgent
import commands as cmds
from team import Team

NY = ZoneInfo("America/New_York")
API = "https://api.exchange.coinbase.com"
UA = {"User-Agent": "ict-confluence-agent/2.0"}
WEEK_ANCHOR = 345600  # Mon 1970-01-05 00:00 UTC

BASE_CFG = {
    "htf_len": 5, "liq_len": 8, "max_liq": 6, "reclaim_bars": 3, "mss_len": 3,
    "eq_tol_atr": 0.30,
    "mss_window": 15, "disp_mult": 1.0, "min_fvg_atr": 0.15, "entry_mode": "CE",
    "use_pd": True, "expiry_bars": 20,
    "stop_buf_atr": 0.10, "tp_mode": "NEXT_LIQ", "fixed_rr": 2.0, "min_rr": 1.5,
    "use_partial": True, "partial_r": 1.0, "be_r": 1.0,
    "kill_zones": [(120, 300), (420, 660), (810, 960)],
    "bonus_zone": (1020, 1140),
    "atr_len": 14, "adr_max": 2.0, "adr_len": 20,
    "L_trend": (True, 1.5), "ema_fast": 21, "ema_slow": 55,
    "L_bias": (True, 1.5),
    "L_rsi": (True, 1.0), "rsi_len": 14, "div_len": 20, "div_valid": 5,
    "L_macd": (True, 1.0),
    "L_vol": (True, 1.5), "vol_x": 1.2,
    "L_dsp": (True, 1.0), "disp_strong": 1.5,
    "L_vwap": (True, 1.0),
    "L_smt": (True, 2.0), "smt_len": 20, "smt_valid": 5,
    "L_atr": (True, 0.5), "atr_reg_len": 50,
    "L_eq": (True, 1.0),
    "L_kz": (True, 1.0),
    "L_rr": (True, 1.0), "rr_confl": 2.0,
    "L_pd": (True, 1.0), "htf_range_len": 20,
    "grade_a": 70.0, "grade_b": 50.0, "b_mult": 0.65, "c_mult": 0.40,
}

# signal tf -> settings
TF_MATRIX = {
    "1m":  dict(sec=60,    b_gran=900,   b_mult=1, b_align="epoch",  sess=True,  adr=True,  intraday_layers=True),
    "5m":  dict(sec=300,   b_gran=3600,  b_mult=1, b_align="epoch",  sess=True,  adr=True,  intraday_layers=True),
    "15m": dict(sec=900,   b_gran=3600,  b_mult=4, b_align="epoch",  sess=True,  adr=True,  intraday_layers=True),
    "1h":  dict(sec=3600,  b_gran=86400, b_mult=1, b_align="epoch",  sess=False, adr=True,  intraday_layers=True),
    "6h":  dict(sec=21600, b_gran=86400, b_mult=7, b_align="monday", sess=False, adr=False, intraday_layers=False),
    "1d":  dict(sec=86400, b_gran=86400, b_mult=7, b_align="monday", sess=False, adr=False, intraday_layers=False),
}
SIGNAL_NEED = 700
BIAS_NEED = {900: 220, 3600: 480, 86400: 700}

MIN_GRADE = os.environ.get("MIN_GRADE", "B").upper()
ACCOUNT_SIZE = float(os.environ.get("ACCOUNT_SIZE", "10000"))
RISK_PCT = float(os.environ.get("RISK_PCT", "1.0"))
USE_SESSIONS = os.environ.get("USE_SESSIONS", "1") != "0"
BIAS_MODE = os.environ.get("BIAS_MODE", "Strict")
PRODUCTS = [p.strip() for p in os.environ.get("PRODUCTS", "ETH-USD").split(",") if p.strip()]
SIGNAL_TFS = [t.strip() for t in os.environ.get("SIGNAL_TFS", "1m,5m,15m,1h,6h,1d").split(",")
              if t.strip() in TF_MATRIX]
BOT = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR") or ("/data" if os.path.isdir("/data") else ".")
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(DATA_DIR, "state.json"))
GRADE_RANK = {"A": 3, "B": 2, "C": 1}
TEAM = None


def min_grade_for(tf: str) -> str:
    return os.environ.get(f"MIN_GRADE_{tf.upper()}", MIN_GRADE).upper()


def align_start(t: int, sec: int, mode: str) -> int:
    if mode == "monday":
        return (t - WEEK_ANCHOR) // (7 * 86400) * (7 * 86400) + WEEK_ANCHOR
    return t - (t % sec)


# ------------------------------ DATA ------------------------------

@dataclass
class Bar:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float


class CandleCache:
    """Incremental per-(product, granularity) candle store."""

    def __init__(self):
        self.store = {}

    def _fetch(self, product, gran, start, end):
        r = requests.get(
            f"{API}/products/{product}/candles",
            params={"granularity": gran,
                    "start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                    "end": datetime.fromtimestamp(end, timezone.utc).isoformat()},
            headers=UA, timeout=20)
        r.raise_for_status()
        time.sleep(0.12)
        return r.json()

    def get(self, product, gran, need):
        key = (product, gran)
        d = self.store.setdefault(key, {})
        now = int(time.time())
        cutoff = now - (now % gran)                     # start of in-progress bar
        end = min(d) if d else cutoff
        floor = cutoff - (need + 400) * gran
        while len([t for t in d if t + gran <= cutoff]) < need and end > floor:
            start = end - 300 * gran
            for t, lo, hi, op, cl, vol in self._fetch(product, gran, start, end):
                d[int(t)] = Bar(int(t), float(op), float(hi), float(lo), float(cl), float(vol))
            end = start
        # refresh the tail (bars cached while incomplete)
        for t, lo, hi, op, cl, vol in self._fetch(product, gran, cutoff - 6 * gran, cutoff + gran):
            d[int(t)] = Bar(int(t), float(op), float(hi), float(lo), float(cl), float(vol))
        bars = [d[t] for t in sorted(d) if t + gran <= cutoff]
        for t in [t for t in d if t < cutoff - (need + 500) * gran]:
            del d[t]
        return bars[-need:]


def resample(bars, sec, mode):
    groups = {}
    for b in bars:
        groups.setdefault(align_start(b.t, sec, mode), []).append(b)
    out = []
    for t in sorted(groups):
        g = sorted(groups[t], key=lambda b: b.t)
        out.append(Bar(t, g[0].o, max(x.h for x in g), min(x.l for x in g), g[-1].c,
                       sum(x.v for x in g)))
    return out


# ------------------------------ INDICATORS ------------------------------

def ema(vals, n):
    out, k, e = [], 2 / (n + 1), None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def rma(vals, n):
    out, e = [], None
    for i, v in enumerate(vals):
        if e is None:
            if i + 1 >= n:
                e = sum(vals[i + 1 - n:i + 1]) / n
            out.append(e)
        else:
            e = (e * (n - 1) + v) / n
            out.append(e)
    return out


def sma(vals, n):
    out = []
    for i in range(len(vals)):
        if i + 1 >= n and all(x is not None for x in vals[i + 1 - n:i + 1]):
            out.append(sum(vals[i + 1 - n:i + 1]) / n)
        else:
            out.append(None)
    return out


def atr_series(bars, n):
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b.h - b.l)
        else:
            pc = bars[i - 1].c
            trs.append(max(b.h - b.l, abs(b.h - pc), abs(b.l - pc)))
    return rma(trs, n)


def rsi_series(closes, n):
    ups, dns = [0.0], [0.0]
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ups.append(max(ch, 0.0))
        dns.append(max(-ch, 0.0))
    au, ad = rma(ups, n), rma(dns, n)
    out = []
    for u, d in zip(au, ad):
        if u is None or d is None:
            out.append(None)
        elif d == 0:
            out.append(100.0)
        else:
            out.append(100 - 100 / (1 + u / d))
    return out


def pivots(bars, n, kind):
    out = [None] * len(bars)
    for i in range(2 * n, len(bars)):
        c = i - n
        if kind == "H":
            v = bars[c].h
            if all(bars[j].h < v for j in range(i - 2 * n, i + 1) if j != c):
                out[i] = v
        else:
            v = bars[c].l
            if all(bars[j].l > v for j in range(i - 2 * n, i + 1) if j != c):
                out[i] = v
    return out


def rolling_extreme_prev(vals, n, fn):
    out = [None] * len(vals)
    for i in range(n, len(vals)):
        out[i] = fn(vals[i - n:i])
    return out


def htf_bias_series(h, length):
    ph, pl = pivots(h, length, "H"), pivots(h, length, "L")
    last_ph = last_pl = None
    last_up = last_dn = None
    out = []
    for i, b in enumerate(h):
        if ph[i] is not None:
            last_ph = ph[i]
        if pl[i] is not None:
            last_pl = pl[i]
        if last_ph is not None and b.c > last_ph:
            last_up = i
        if last_pl is not None and b.c < last_pl:
            last_dn = i
        if last_up is None and last_dn is None:
            out.append(0)
        elif last_dn is None or (last_up is not None and last_up > last_dn):
            out.append(1)
        elif last_up is None or last_dn > last_up:
            out.append(-1)
        else:
            out.append(0)
    return out


# ------------------------------ ENGINE ------------------------------

@dataclass
class Pool:
    level: float
    raid: int = -1
    ext: float = 0.0
    eq: bool = False


@dataclass
class Setup:
    dir: int = 0
    state: int = 0
    sweep_bar: int = -1
    sweep_ext: float = 0.0
    mss_lvl: float = 0.0
    mss_done: bool = False
    leg_ext: float = 0.0
    deal_ref: float = 0.0
    cand_top: float = 0.0
    cand_bot: float = 0.0
    cand_ok: bool = False
    cand_q: float = 0.0
    vol_c: bool = False
    smt: bool = False
    eq_sweep: bool = False
    rsi_div: bool = False
    ep: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    tp1: float = 0.0
    tp1_done: bool = True
    ru: float = 0.0
    grade: str = ""
    pct: float = 0.0
    layers: str = ""
    order_bar: int = -1
    fill_bar: int = -1
    sid: str = ""


class Engine:
    def __init__(self, product, tf, bars, htf, daily, pair):
        m = TF_MATRIX[tf]
        cfg = dict(BASE_CFG)
        if not m["intraday_layers"]:
            cfg["L_kz"] = (False, 0.0)
            cfg["L_vwap"] = (False, 0.0)
        if not m["adr"]:
            cfg["adr_max"] = 0.0
        self.cfg = cfg
        self.product, self.tf = product, tf
        self.sec = m["sec"]
        self.sess_gate = USE_SESSIONS and m["sess"]
        self.b_sec = m["b_gran"] * m["b_mult"]
        self.b_align = m["b_align"]
        self.bars = bars
        self.events = []
        n = len(bars)
        highs = [b.h for b in bars]
        lows = [b.l for b in bars]
        closes = [b.c for b in bars]

        self.atr = atr_series(bars, cfg["atr_len"])
        self.ema_f = ema(closes, cfg["ema_fast"])
        self.ema_s = ema(closes, cfg["ema_slow"])
        macd = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
        self.macd, self.sig = macd, ema(macd, 9)
        self.rsi = rsi_series(closes, cfg["rsi_len"])
        self.vol_ma = sma([b.v for b in bars], 20)
        self.atr_ma = sma([a if a is not None else 0.0 for a in self.atr], cfg["atr_reg_len"])
        self.vwap = []
        pv = vv = 0.0
        day = None
        for b in bars:
            d = b.t // 86400
            if d != day:
                day, pv, vv = d, 0.0, 0.0
            tp_ = (b.h + b.l + b.c) / 3
            pv += tp_ * b.v
            vv += b.v
            self.vwap.append(pv / vv if vv > 0 else b.c)
        self.hh10 = rolling_extreme_prev(highs, 10, max)
        self.ll10 = rolling_extreme_prev(lows, 10, min)
        self.our_ll = rolling_extreme_prev(lows, cfg["smt_len"], min)
        self.our_hh = rolling_extreme_prev(highs, cfg["smt_len"], max)
        pmap = {b.t: b for b in pair}
        pl_, ph_ = [], []
        lastp = pair[0] if pair else Bar(0, 0, 0, 0, 0, 0)
        for b in bars:
            lastp = pmap.get(b.t, lastp)
            pl_.append(lastp.l)
            ph_.append(lastp.h)
        self.p_low, self.p_high = pl_, ph_
        self.p_ll = rolling_extreme_prev(pl_, cfg["smt_len"], min)
        self.p_hh = rolling_extreme_prev(ph_, cfg["smt_len"], max)
        self.phL = pivots(bars, cfg["liq_len"], "H")
        self.plL = pivots(bars, cfg["liq_len"], "L")
        self.phI = pivots(bars, cfg["mss_len"], "H")
        self.plI = pivots(bars, cfg["mss_len"], "L")

        bias_h = htf_bias_series(htf, cfg["htf_len"])
        h_t = [b.t for b in htf]
        self.htf_bias = [0] * n
        self.htf_eq = [None] * n
        rl = cfg["htf_range_len"]
        for i, b in enumerate(bars):
            cur = align_start(b.t, self.b_sec, self.b_align)
            k = -1
            for kk in range(len(h_t) - 1, -1, -1):
                if h_t[kk] < cur:
                    k = kk
                    break
            if k >= 0:
                self.htf_bias[i] = bias_h[k]
                if k + 1 >= rl:
                    seg = htf[k + 1 - rl:k + 1]
                    self.htf_eq[i] = (max(x.h for x in seg) + min(x.l for x in seg)) / 2
        self.adr = [None] * n
        self.day_range = [0.0] * n
        d_hl = [(b.t // 86400, b.h - b.l) for b in sorted(daily, key=lambda x: x.t)]
        day = None
        dh = dl = None
        for i, b in enumerate(bars):
            d = b.t // 86400
            if d != day:
                day, dh, dl = d, b.h, b.l
            else:
                dh, dl = max(dh, b.h), min(dl, b.l)
            self.day_range[i] = dh - dl
            if cfg["adr_max"] > 0:
                prev = [r for dd, r in d_hl if dd < d][-cfg["adr_len"]:]
                if len(prev) >= cfg["adr_len"]:
                    self.adr[i] = sum(prev) / len(prev)

        self.pools_hi = []
        self.pools_lo = []
        self.last_int_h = self.last_int_l = None
        self.last_ext_h = self.last_ext_l = None
        self.last_div_bull = self.last_div_bear = -10**9
        self.last_smt_bull = self.last_smt_bear = -10**9
        self.S = Setup()
        self.setup_seq = 0

    def in_kz(self, i):
        d = datetime.fromtimestamp(self.bars[i].t, timezone.utc).astimezone(NY)
        mnt = d.hour * 60 + d.minute
        return any(a <= mnt < b for a, b in self.cfg["kill_zones"])

    def in_bonus(self, i):
        d = datetime.fromtimestamp(self.bars[i].t, timezone.utc).astimezone(NY)
        mnt = d.hour * 60 + d.minute
        a, b = self.cfg["bonus_zone"]
        return a <= mnt < b

    def emit(self, i, event, extra=None):
        S = self.S
        p = {"event": event, "product": self.product, "tf": self.tf,
             "bar_time": self.bars[i].t + self.sec,
             "side": "long" if S.dir == 1 else "short",
             "entry": round(S.ep, 2), "stop": round(S.sl, 2), "target": round(S.tp, 2),
             "rr": round(abs(S.tp - S.ep) / S.ru, 2) if S.ru else 0,
             "grade": S.grade or "-", "align": round(S.pct), "layers": S.layers,
             "htf_bias": self.htf_bias[i], "sid": S.sid}
        if extra:
            p.update(extra)
        self.events.append(p)

    def reset(self):
        self.S = Setup()

    def score(self, i, d, ep, ru, tp):
        cfg, S = self.cfg, self.S
        ws = wh = 0.0
        parts = []

        def layer(key, ok, tag):
            nonlocal ws, wh
            en, w = cfg[key]
            if not en:
                return
            ws += w
            if ok:
                wh += w
            parts.append(tag + ("\u2713" if ok else "\u2717"))

        layer("L_trend", (self.ema_f[i] > self.ema_s[i]) if d == 1 else (self.ema_f[i] < self.ema_s[i]), "EMA")
        layer("L_bias", self.htf_bias[i] == d, "HTF")
        layer("L_rsi", S.rsi_div, "RSI")
        layer("L_macd", (self.macd[i] > self.sig[i]) if d == 1 else (self.macd[i] < self.sig[i]), "MAC")
        layer("L_vol", S.vol_c, "VOL")
        layer("L_dsp", S.cand_q >= cfg["disp_strong"], "DSP")
        layer("L_vwap", (ep <= self.vwap[i]) if d == 1 else (ep >= self.vwap[i]), "VWP")
        layer("L_smt", S.smt, "SMT")
        layer("L_atr", self.atr_ma[i] is not None and self.atr[i] is not None and self.atr[i] > self.atr_ma[i], "ATR")
        layer("L_eq", S.eq_sweep, "EQ")
        layer("L_kz", self.in_kz(i) or self.in_bonus(i), "KZ")
        layer("L_rr", ((tp - ep) if d == 1 else (ep - tp)) / ru >= cfg["rr_confl"], "RR")
        layer("L_pd", self.htf_eq[i] is not None and ((ep <= self.htf_eq[i]) if d == 1 else (ep >= self.htf_eq[i])), "PD")
        pct = wh / ws * 100 if ws > 0 else 100.0
        return pct, " ".join(parts)

    def next_liq(self, ep, ru, is_long):
        best = None
        for p in (self.pools_hi if is_long else self.pools_lo):
            if p.raid != -1:
                continue
            rr = (p.level - ep) / ru if is_long else (ep - p.level) / ru
            if rr >= self.cfg["min_rr"] and (best is None or (p.level < best if is_long else p.level > best)):
                best = p.level
        return best

    def step(self, i):
        cfg = self.cfg
        b = self.bars[i]
        atr = self.atr[i] or 0.0
        S = self.S

        if self.phI[i] is not None:
            self.last_int_h = self.phI[i]
        if self.plI[i] is not None:
            self.last_int_l = self.plI[i]

        if self.phL[i] is not None:
            v = self.phL[i]
            eq = False
            for p in self.pools_hi:
                if abs(v - p.level) <= cfg["eq_tol_atr"] * atr:
                    eq = p.eq = True
            self.pools_hi.append(Pool(v, -1, v, eq))
            if len(self.pools_hi) > cfg["max_liq"]:
                self.pools_hi.pop(0)
            self.last_ext_h = v
        if self.plL[i] is not None:
            v = self.plL[i]
            eq = False
            for p in self.pools_lo:
                if abs(v - p.level) <= cfg["eq_tol_atr"] * atr:
                    eq = p.eq = True
            self.pools_lo.append(Pool(v, -1, v, eq))
            if len(self.pools_lo) > cfg["max_liq"]:
                self.pools_lo.pop(0)
            self.last_ext_l = v

        swept_lo = swept_hi = False
        lo_ext = hi_ext = None
        lo_eq = hi_eq = False
        lo_n = hi_n = 0
        for p in list(reversed(self.pools_lo)):
            if p.raid == -1 and b.l < p.level:
                p.raid, p.ext = i, b.l
            elif p.raid != -1 and b.l < p.ext:
                p.ext = b.l
            if p.raid != -1:
                if b.c > p.level:
                    swept_lo, lo_n = True, lo_n + 1
                    lo_ext = p.ext if lo_ext is None else min(lo_ext, p.ext)
                    lo_eq = lo_eq or p.eq
                    self.pools_lo.remove(p)
                elif i - p.raid >= cfg["reclaim_bars"]:
                    self.pools_lo.remove(p)
        if lo_n >= 2:
            lo_eq = True
        for p in list(reversed(self.pools_hi)):
            if p.raid == -1 and b.h > p.level:
                p.raid, p.ext = i, b.h
            elif p.raid != -1 and b.h > p.ext:
                p.ext = b.h
            if p.raid != -1:
                if b.c < p.level:
                    swept_hi, hi_n = True, hi_n + 1
                    hi_ext = p.ext if hi_ext is None else max(hi_ext, p.ext)
                    hi_eq = hi_eq or p.eq
                    self.pools_hi.remove(p)
                elif i - p.raid >= cfg["reclaim_bars"]:
                    self.pools_hi.remove(p)
        if hi_n >= 2:
            hi_eq = True

        if self.our_ll[i] is not None and self.rsi[i] is not None:
            prev_rsi = [r for r in self.rsi[max(0, i - cfg["div_len"]):i] if r is not None]
            if prev_rsi and b.l < self.our_ll[i] and self.rsi[i] > min(prev_rsi):
                self.last_div_bull = i
            if prev_rsi and b.h > self.our_hh[i] and self.rsi[i] < max(prev_rsi):
                self.last_div_bear = i
        if self.our_ll[i] is not None and self.p_ll[i] is not None:
            if b.l < self.our_ll[i] and self.p_low[i] > self.p_ll[i]:
                self.last_smt_bull = i
            if b.h > self.our_hh[i] and self.p_high[i] < self.p_hh[i]:
                self.last_smt_bear = i

        fvg_bull = fvg_bear = False
        cand_q = 0.0
        if i >= 2 and self.atr[i - 1]:
            body = abs(self.bars[i - 1].c - self.bars[i - 1].o)
            disp_ok = body >= cfg["disp_mult"] * self.atr[i - 1]
            cand_q = body / self.atr[i - 1]
            if b.l > self.bars[i - 2].h and (b.l - self.bars[i - 2].h) >= cfg["min_fvg_atr"] * atr \
                    and disp_ok and self.bars[i - 1].c > self.bars[i - 1].o:
                fvg_bull = True
            if b.h < self.bars[i - 2].l and (self.bars[i - 2].l - b.h) >= cfg["min_fvg_atr"] * atr \
                    and disp_ok and self.bars[i - 1].c < self.bars[i - 1].o:
                fvg_bear = True

        if S.state == 2:
            filled = (b.l <= S.ep) if S.dir == 1 else (b.h >= S.ep)
            if filled:
                S.state, S.fill_bar = 3, i
                self.emit(i, "filled")
            else:
                expired = i - S.order_bar > cfg["expiry_bars"]
                violated = (b.c < S.cand_bot or b.h >= S.tp) if S.dir == 1 else (b.c > S.cand_top or b.l <= S.tp)
                if expired or violated:
                    self.emit(i, "cancelled", {"reason": "expired" if expired else "invalidated"})
                    self.reset()
        if S.state == 3:
            hit_sl = (b.l <= S.sl) if S.dir == 1 else (b.h >= S.sl)
            hit_tp = (b.h >= S.tp) if S.dir == 1 else (b.l <= S.tp)
            hit_tp1 = (not S.tp1_done) and ((b.h >= S.tp1) if S.dir == 1 else (b.l <= S.tp1))
            if hit_sl and hit_tp:
                self.emit(i, "closed", {"result": "ambiguous - SL and TP both inside this bar, check fills"})
                self.reset()
            elif hit_sl:
                res = "break-even" if abs(S.sl - S.ep) < 1e-9 else ("stop -1R" if not S.tp1_done else "runner stopped")
                self.emit(i, "closed", {"result": res})
                self.reset()
            else:
                if hit_tp1:
                    S.tp1_done = True
                    self.emit(i, "tp1", {"result": f"partial +{cfg['partial_r']}R banked"})
                if cfg["be_r"] > 0 and S.sl != S.ep:
                    trig = S.ep + cfg["be_r"] * S.ru if S.dir == 1 else S.ep - cfg["be_r"] * S.ru
                    if (b.h >= trig) if S.dir == 1 else (b.l <= trig):
                        S.sl = S.ep
                        self.emit(i, "be", {"result": "stop moved to break-even"})
                if hit_tp:
                    self.emit(i, "closed", {"result": f"target +{round(abs(S.tp-S.ep)/S.ru,1)}R"})
                    self.reset()
        S = self.S

        long_ok = BIAS_MODE == "Off" or self.htf_bias[i] == 1 or (BIAS_MODE == "Lenient" and self.htf_bias[i] == 0)
        short_ok = BIAS_MODE == "Off" or self.htf_bias[i] == -1 or (BIAS_MODE == "Lenient" and self.htf_bias[i] == 0)
        if S.state in (0, 1):
            if swept_hi and short_ok:
                self.setup_seq += 1
                self.S = S = Setup(dir=-1, state=1, sweep_bar=i, sweep_ext=hi_ext,
                                   mss_lvl=self.last_int_l if self.last_int_l is not None else (self.ll10[i] or b.l),
                                   leg_ext=b.l,
                                   deal_ref=self.last_ext_l if self.last_ext_l is not None else (self.ll10[i] or b.l),
                                   smt=(i - self.last_smt_bear) <= cfg["smt_valid"],
                                   eq_sweep=hi_eq,
                                   rsi_div=(i - self.last_div_bear) <= cfg["div_valid"],
                                   sid=f"{self.product}:{self.tf}:{self.bars[i].t}:{self.setup_seq}")
            if swept_lo and long_ok:
                self.setup_seq += 1
                self.S = S = Setup(dir=1, state=1, sweep_bar=i, sweep_ext=lo_ext,
                                   mss_lvl=self.last_int_h if self.last_int_h is not None else (self.hh10[i] or b.h),
                                   leg_ext=b.h,
                                   deal_ref=self.last_ext_h if self.last_ext_h is not None else (self.hh10[i] or b.h),
                                   smt=(i - self.last_smt_bull) <= cfg["smt_valid"],
                                   eq_sweep=lo_eq,
                                   rsi_div=(i - self.last_div_bull) <= cfg["div_valid"],
                                   sid=f"{self.product}:{self.tf}:{self.bars[i].t}:{self.setup_seq}")

        if S.state == 1:
            d = S.dir
            if d == 1:
                S.leg_ext = max(S.leg_ext, b.h)
                dead = b.l < S.sweep_ext
            else:
                S.leg_ext = min(S.leg_ext, b.l)
                dead = b.h > S.sweep_ext
            if dead or i - S.sweep_bar > cfg["mss_window"]:
                self.reset()
                return
            if not S.mss_done and ((b.c > S.mss_lvl) if d == 1 else (b.c < S.mss_lvl)):
                S.mss_done = True
            got_fvg = fvg_bull if d == 1 else fvg_bear
            if got_fvg:
                if d == 1 and self.bars[i - 2].h > S.sweep_ext:
                    S.cand_top, S.cand_bot, S.cand_ok = b.l, self.bars[i - 2].h, True
                    S.cand_q = cand_q
                    S.vol_c = self.vol_ma[i - 1] is not None and self.bars[i - 1].v >= cfg["vol_x"] * self.vol_ma[i - 1]
                if d == -1 and self.bars[i - 2].l < S.sweep_ext:
                    S.cand_top, S.cand_bot, S.cand_ok = self.bars[i - 2].l, b.h, True
                    S.cand_q = cand_q
                    S.vol_c = self.vol_ma[i - 1] is not None and self.bars[i - 1].v >= cfg["vol_x"] * self.vol_ma[i - 1]
            elif S.cand_ok and ((b.l < S.cand_bot) if d == 1 else (b.h > S.cand_top)):
                S.cand_ok = False
            if S.mss_done and S.cand_ok:
                em = cfg["entry_mode"]
                if d == 1:
                    ep = S.cand_top if em == "NEAR" else (S.cand_top + S.cand_bot) / 2 if em == "CE" else S.cand_bot
                    sl = S.sweep_ext - cfg["stop_buf_atr"] * atr
                    ru = ep - sl
                else:
                    ep = S.cand_bot if em == "NEAR" else (S.cand_top + S.cand_bot) / 2 if em == "CE" else S.cand_top
                    sl = S.sweep_ext + cfg["stop_buf_atr"] * atr
                    ru = sl - ep
                if ru <= 0:
                    self.reset()
                    return
                tp = ep + cfg["fixed_rr"] * ru if d == 1 else ep - cfg["fixed_rr"] * ru
                if cfg["tp_mode"] == "NEXT_LIQ":
                    nl = self.next_liq(ep, ru, d == 1)
                    if nl is not None:
                        tp = nl - cfg["stop_buf_atr"] * atr if d == 1 else nl + cfg["stop_buf_atr"] * atr
                leg_eq = (max(S.deal_ref, S.leg_ext) + S.sweep_ext) / 2 if d == 1 else (min(S.deal_ref, S.leg_ext) + S.sweep_ext) / 2
                pd_ok = (not cfg["use_pd"]) or ((ep <= leg_eq) if d == 1 else (ep >= leg_eq))
                sess_ok = (not self.sess_gate) or self.in_kz(i) or self.in_bonus(i)
                adr_ok = cfg["adr_max"] <= 0 or self.adr[i] is None or self.day_range[i] < cfg["adr_max"] * self.adr[i]
                pct, layers = self.score(i, d, ep, ru, tp)
                grade = "A" if pct >= cfg["grade_a"] else "B" if pct >= cfg["grade_b"] else "C"
                if pd_ok and sess_ok and adr_ok:
                    S.ep, S.sl, S.tp, S.ru = ep, sl, tp, ru
                    S.grade, S.pct, S.layers = grade, pct, layers
                    S.tp1 = ep + cfg["partial_r"] * ru if d == 1 else ep - cfg["partial_r"] * ru
                    S.tp1_done = (not cfg["use_partial"]) or ((S.tp1 >= tp) if d == 1 else (S.tp1 <= tp))
                    S.order_bar, S.state = i, 2
                    self.emit(i, "setup")
                else:
                    self.emit(i, "skipped", {"reason": "PD" if not pd_ok else "session" if not sess_ok else "ADR",
                                             "grade": grade, "align": round(pct), "layers": layers,
                                             "entry": round(ep, 2), "stop": round(sl, 2), "target": round(tp, 2)})
                    self.reset()

    def run(self):
        for i in range(len(self.bars)):
            self.step(i)
        return self.events


# ------------------------------ NOTIFY ------------------------------

def send_telegram(text):
    if not BOT or not CHAT:
        print("[DRY-RUN telegram]\n" + text + "\n")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT}/sendMessage",
                      json={"chat_id": CHAT, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=15).raise_for_status()
    except Exception as e:
        print(f"telegram send failed: {e}")


def fmt_event(p):
    icon = {"setup": "\U0001F3AF", "filled": "\u2705", "closed": "\U0001F3C1",
            "tp1": "\U0001F4B0", "be": "\U0001F512", "cancelled": "\u274C"}.get(p["event"], "\U0001F514")
    dot = "\U0001F7E2" if p["side"] == "long" else "\U0001F534"
    ts = datetime.fromtimestamp(p["bar_time"], timezone.utc).astimezone(NY).strftime("%b %d %H:%M NY")
    head = f"{icon} <b>{p['event'].upper()}</b> {dot} {p['side'].upper()} · {p['product']} · <b>{p['tf']}</b> · {ts}"
    lines = [head]
    if p["event"] in ("setup", "filled"):
        lines.append(f"Grade <b>{p['grade']}</b> · {p['align']}% aligned")
        risk_mult = 1.0 if p["grade"] == "A" else BASE_CFG["b_mult"] if p["grade"] == "B" else BASE_CFG["c_mult"]
        ru = abs(p["entry"] - p["stop"])
        qty = ACCOUNT_SIZE * RISK_PCT / 100 * risk_mult / ru if ru else 0
        lines.append(f"Entry <code>{p['entry']}</code> · Stop <code>{p['stop']}</code> · Target <code>{p['target']}</code> ({p['rr']}R)")
        lines.append(f"Size @ {RISK_PCT}%x{risk_mult:g}: <code>{qty:.4f}</code> (acct ${ACCOUNT_SIZE:g})")
        if p.get("layers"):
            lines.append(f"<i>{p['layers']}</i>")
    else:
        if p.get("result"):
            lines.append(p["result"])
        if p.get("reason"):
            lines.append(f"reason: {p['reason']}")
        lines.append(f"E {p['entry']} · S {p['stop']} · T {p['target']}")
    return "\n".join(lines)


def notifiable(p, announced):
    if p["event"] == "skipped":
        return False
    if p["event"] == "setup":
        return GRADE_RANK.get(p["grade"], 0) >= GRADE_RANK.get(min_grade_for(p["tf"]), 2)
    return p.get("sid", "") in announced


# ------------------------------ RUNTIME ------------------------------

CACHE = CandleCache()


def build_engine(product, tf):
    m = TF_MATRIX[tf]
    pair = "BTC-USD" if product.startswith("ETH") else "ETH-USD"
    bars = CACHE.get(product, m["sec"], SIGNAL_NEED)
    braw = CACHE.get(product, m["b_gran"], BIAS_NEED[m["b_gran"]])
    htf = braw if m["b_mult"] == 1 else resample(braw, m["b_gran"] * m["b_mult"], m["b_align"])
    daily = CACHE.get(product, 86400, BASE_CFG["adr_len"] + 10) if m["adr"] else []
    pbars = CACHE.get(pair, m["sec"], SIGNAL_NEED)
    return Engine(product, tf, bars, htf, daily, pbars)


def load_state():
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
            return set(d.get("sent", [])), set(d.get("announced", []))
    except Exception:
        return set(), set()


def save_state(sent, announced):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"sent": list(sent)[-8000:], "announced": list(announced)[-1200:]}, f)
    except Exception as e:
        print(f"state save failed (ok without a volume): {e}")


def process(product, tf, sent, announced, live_window_s):
    eng = build_engine(product, tf)
    now = int(time.time())
    for p in eng.run():
        key = f"{p['product']}|{p['tf']}|{p['event']}|{p['bar_time']}|{p.get('sid','')}"
        fresh = now - p["bar_time"] <= live_window_s
        if p["event"] == "setup" and notifiable(p, announced):
            if fresh and key not in sent:
                TEAM.handle_setup(p)
                announced.add(p["sid"])
            elif not fresh:
                announced.add(p["sid"])
            sent.add(key)
        elif p["event"] != "setup" and notifiable(p, announced):
            if key not in sent:
                TEAM.on_outcome(p, fresh)
            sent.add(key)
        else:
            sent.add(key)
    S = eng.S
    st = {0: "idle", 1: "swept-waiting", 2: f"{'L' if S.dir==1 else 'S'} limit@{S.ep:.2f}({S.grade})",
          3: f"{'L' if S.dir==1 else 'S'} open@{S.ep:.2f}({S.grade})"}[S.state]
    bias = {1: "BULL", -1: "BEAR", 0: "NEUT"}[eng.htf_bias[-1]]
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {product} {tf}: c={eng.bars[-1].c:.2f} "
          f"bias={bias} {st} pools {len(eng.pools_hi)}/{len(eng.pools_lo)}")
    TEAM.live[(product, tf)] = {"close": eng.bars[-1].c, "bias": bias, "state": st, "ts": now}


def main():
    args = sys.argv[1:]
    if "--backtest" in args:
        for product in PRODUCTS:
            for tf in SIGNAL_TFS:
                eng = build_engine(product, tf)
                events = eng.run()
                days = {"1m": 0.5, "5m": 2, "15m": 4, "1h": 20, "6h": 120, "1d": 400}[tf]
                cutoff = int(time.time()) - int(days * 86400)
                recent = [p for p in events if p["bar_time"] >= cutoff and p["event"] != "skipped"]
                print(f"\n===== {product} {tf}: {len(recent)} events (last {days}d) =====")
                for p in recent[-14:]:
                    ts = datetime.fromtimestamp(p["bar_time"], timezone.utc).astimezone(NY).strftime("%m-%d %H:%M")
                    extra = p.get("result") or p.get("reason") or ""
                    print(f"{ts} NY  {p['event']:<9} {p['side']:<5} {p.get('grade','-')} "
                          f"{p.get('align','')}%  E{p['entry']} S{p['stop']} T{p['target']} {p['rr']}R  {extra}")
                    if p["event"] == "setup":
                        print(f"                {p['layers']}")
        return
    global TEAM
    ledger = Ledger(DATA_DIR, partial_r=BASE_CFG["partial_r"] if BASE_CFG["use_partial"] else 0.0)
    tuned = ledger.applied_weights() or {}
    for lk, lw in tuned.items():
        if lk in BASE_CFG and isinstance(BASE_CFG[lk], tuple):
            BASE_CFG[lk] = (BASE_CFG[lk][0], float(lw))
    TEAM = Team(ledger, ContextAgent(), MacroAgent(os.environ.get("MACRO_MODE", "warn")),
                RiskAgent(ledger), ReviewAgent(), BASE_CFG, send_telegram, fmt_event)
    TEAM.context.maybe_refresh(force=True)
    TEAM.macro.maybe_refresh(force=True)
    cmds.start(TEAM, send_telegram, BOT, CHAT)
    sent, announced = load_state()
    last_done = {}
    first = True
    fails = 0
    base_sec = min(TF_MATRIX[t]["sec"] for t in SIGNAL_TFS)
    while True:
        try:
            now = int(time.time())
            for product in PRODUCTS:
                for tf in SIGNAL_TFS:
                    sec = TF_MATRIX[tf]["sec"]
                    completed = now - (now % sec) - sec
                    if last_done.get((product, tf), -1) >= completed and not first:
                        continue
                    process(product, tf, sent, announced,
                            live_window_s=0 if first else 2 * sec + 120)
                    last_done[(product, tf)] = completed
            if first:
                roster = ["signal x" + str(len(SIGNAL_TFS)), "risk", "context", "macro", "learning"]
                if TEAM.review.enabled():
                    roster.append("review(" + TEAM.review.model + ")")
                send_telegram("\U0001F7E2 ICT <b>team</b> online — " + ", ".join(PRODUCTS)
                              + " · TFs: " + ", ".join(SIGNAL_TFS) + f" · min grade {MIN_GRADE}"
                              + "\nAgents: " + ", ".join(roster)
                              + "\nText <b>help</b> for commands · ledger @ " + DATA_DIR)
                first = False
            save_state(sent, announced)
            TEAM.tick()
            fails = 0
        except Exception:
            fails += 1
            traceback.print_exc()
            if fails in (5, 20):
                send_telegram(f"\u26A0\uFE0F ICT agent: {fails} consecutive cycle failures — check Railway logs")
        if "--once" in args:
            break
        now = time.time()
        nxt = (int(now) // base_sec + 1) * base_sec + 20
        time.sleep(max(5, nxt - now))


if __name__ == "__main__":
    main()
