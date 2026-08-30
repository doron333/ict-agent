#!/usr/bin/env python3
"""
ICT Confluence Engine — standalone signal agent (no TradingView required)
=========================================================================
Faithful Python port of the Pine v3 strategy:
  HTF bias -> liquidity sweep + reclaim -> MSS with displacement -> FVG limit
  entry -> stop beyond sweep wick -> target at next untouched pool (min RR),
  scored across 13 confluence layers -> grade A/B/C -> Telegram notification.

Data:    Coinbase Exchange public API (free, no key). 15m signal TF, 1h->4h
         resample for HTF bias, daily for ADR guard, BTC-USD (or ETH-USD)
         pair for SMT divergence.
Runtime: long-running worker. Wakes ~25s after every 15m bar close, recomputes
         the whole state machine from raw candles (deterministic), diffs
         against what it already announced, notifies only new events.
Deploy:  Railway worker service. `python -u main.py`
Test:    python main.py --backtest --days 5     (prints recent signals)
         python main.py --once                  (single live cycle, then exit)

Env vars (all optional except Telegram for real notifications):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   -> without them, events print to stdout
  PRODUCTS       default "ETH-USD"        e.g. "ETH-USD,BTC-USD"
  MIN_GRADE      default "B"              only notify setups this grade+ (A|B|C)
  ACCOUNT_SIZE   default "10000"          used for suggested position size
  RISK_PCT       default "1.0"            % of account risked on an A setup
  USE_SESSIONS   default "1"              "0" disables the kill-zone hard gate
  BIAS_MODE      default "Strict"         Strict | Lenient | Off
"""

import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

# ────────────────────────────── CONFIG ──────────────────────────────
NY = ZoneInfo("America/New_York")
API = "https://api.exchange.coinbase.com"
UA = {"User-Agent": "ict-confluence-agent/1.0"}

CFG = {
    # structure & liquidity
    "htf_len": 5,            # HTF swing length (4h pivots)
    "liq_len": 8,            # external swing length (liquidity pools)
    "max_liq": 6,            # max tracked pools per side
    "reclaim_bars": 3,       # sweep must reclaim within N bars
    "mss_len": 3,            # internal swing length (MSS)
    "eq_tol_atr": 0.30,      # equal high/low tolerance (ATR x)
    # entry model
    "mss_window": 15,        # bars allowed from sweep to MSS + FVG
    "disp_mult": 1.0,        # FVG candle body >= ATR x
    "min_fvg_atr": 0.15,     # min FVG height (ATR x)
    "entry_mode": "CE",      # NEAR | CE | FAR
    "use_pd": True,          # hard discount/premium gate (leg-based)
    "expiry_bars": 20,       # cancel unfilled entry after N bars
    # risk / management
    "stop_buf_atr": 0.10,
    "tp_mode": "NEXT_LIQ",   # NEXT_LIQ | FIXED
    "fixed_rr": 2.0,
    "min_rr": 1.5,
    "use_partial": True,
    "partial_r": 1.0,
    "be_r": 1.0,
    # sessions & guards
    "kill_zones": [(120, 300), (420, 660), (810, 960)],   # NY minutes: 0200-0500,0700-1100,1330-1600
    "bonus_zone": (1020, 1140),                            # 1700-1900 NY (post-close edge)
    "atr_len": 14,
    "adr_max": 2.0,
    "adr_len": 20,
    # confluence layers: (enabled, weight)
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
    # grading
    "grade_a": 70.0,
    "grade_b": 50.0,
    "b_mult": 0.65,
    "c_mult": 0.40,
}

MIN_GRADE = os.environ.get("MIN_GRADE", "B").upper()
ACCOUNT_SIZE = float(os.environ.get("ACCOUNT_SIZE", "10000"))
RISK_PCT = float(os.environ.get("RISK_PCT", "1.0"))
USE_SESSIONS = os.environ.get("USE_SESSIONS", "1") != "0"
BIAS_MODE = os.environ.get("BIAS_MODE", "Strict")
PRODUCTS = [p.strip() for p in os.environ.get("PRODUCTS", "ETH-USD").split(",") if p.strip()]
BOT = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
GRADE_RANK = {"A": 3, "B": 2, "C": 1}

# ────────────────────────────── DATA ──────────────────────────────

@dataclass
class Bar:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float


def fetch_candles(product: str, granularity: int, need: int) -> list[Bar]:
    """Fetch `need` completed candles, ascending by time."""
    out: dict[int, Bar] = {}
    now = int(time.time())
    cutoff = now - (now % granularity)          # start of in-progress candle
    end = cutoff
    while len(out) < need:
        start = end - 300 * granularity
        r = requests.get(
            f"{API}/products/{product}/candles",
            params={
                "granularity": granularity,
                "start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                "end": datetime.fromtimestamp(end, timezone.utc).isoformat(),
            },
            headers=UA, timeout=20,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for t, lo, hi, op, cl, vol in rows:
            if t + granularity <= cutoff:       # completed candles only
                out[int(t)] = Bar(int(t), float(op), float(hi), float(lo), float(cl), float(vol))
        end = start
        time.sleep(0.15)                        # stay friendly to rate limits
    bars = [out[t] for t in sorted(out)]
    return bars[-need:]


def resample_4h(h1: list[Bar]) -> list[Bar]:
    """1h -> 4h anchored to 00:00 UTC (matches TradingView 4h on crypto)."""
    groups: dict[int, list[Bar]] = {}
    for b in h1:
        groups.setdefault(b.t - (b.t % 14400), []).append(b)
    out = []
    for t in sorted(groups):
        g = sorted(groups[t], key=lambda b: b.t)
        if len(g) < 4 and t != max(groups):     # drop partial groups except we exclude last anyway
            if len(g) < 4:
                continue
        out.append(Bar(t, g[0].o, max(b.h for b in g), min(b.l for b in g), g[-1].c, sum(b.v for b in g)))
    return out


# ────────────────────────────── INDICATORS ──────────────────────────────

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
    out, s = [], 0.0
    for i, v in enumerate(vals):
        s += v if v is not None else 0.0
        if i >= n:
            s -= vals[i - n] if vals[i - n] is not None else 0.0
        out.append(s / n if i + 1 >= n and all(x is not None for x in vals[i + 1 - n:i + 1]) else None)
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
    """Pine-style pivot: value confirmed at index i for center i-n. Returns list[Optional[float]]."""
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
    """fn(min|max) over the previous n bars, i.e. ta.lowest/highest(x, n)[1]."""
    out = [None] * len(vals)
    for i in range(n, len(vals)):
        out[i] = fn(vals[i - n:i])
    return out


# ────────────────────────────── HTF BIAS ──────────────────────────────

def htf_bias_series(h4: list[Bar], length: int) -> list[int]:
    """Per-4h-bar bias: +1 last event was close>last pivot high, -1 close<last pivot low."""
    ph, pl = pivots(h4, length, "H"), pivots(h4, length, "L")
    last_ph = last_pl = None
    last_up = last_dn = None
    out = []
    for i, b in enumerate(h4):
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


# ────────────────────────────── ENGINE ──────────────────────────────

@dataclass
class Pool:
    level: float
    raid: int = -1
    ext: float = 0.0
    eq: bool = False


@dataclass
class Setup:
    dir: int = 0
    state: int = 0            # 0 idle, 1 swept, 2 pending, 3 filled
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
    def __init__(self, product: str, bars: list[Bar], h4: list[Bar],
                 daily: list[Bar], pair: list[Bar]):
        self.product = product
        self.bars = bars
        self.events: list[dict] = []
        n = len(bars)
        highs = [b.h for b in bars]
        lows = [b.l for b in bars]
        closes = [b.c for b in bars]

        # indicators
        self.atr = atr_series(bars, CFG["atr_len"])
        self.ema_f = ema(closes, CFG["ema_fast"])
        self.ema_s = ema(closes, CFG["ema_slow"])
        m_f, m_s = ema(closes, 12), ema(closes, 26)
        macd = [a - b for a, b in zip(m_f, m_s)]
        self.macd = macd
        self.sig = ema(macd, 9)
        self.rsi = rsi_series(closes, CFG["rsi_len"])
        vols = [b.v for b in bars]
        self.vol_ma = sma(vols, 20)
        self.atr_ma = sma([a if a is not None else 0.0 for a in self.atr], CFG["atr_reg_len"])
        # session VWAP (UTC day)
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
        # rolling refs
        self.hh10 = rolling_extreme_prev(highs, 10, max)
        self.ll10 = rolling_extreme_prev(lows, 10, min)
        self.our_ll = rolling_extreme_prev(lows, CFG["smt_len"], min)
        self.our_hh = rolling_extreme_prev(highs, CFG["smt_len"], max)
        # pair series aligned by timestamp (carry last known)
        pmap = {b.t: b for b in pair}
        pl_, ph_ = [], []
        lastp = pair[0] if pair else Bar(0, 0, 0, 0, 0, 0)
        for b in bars:
            lastp = pmap.get(b.t, lastp)
            pl_.append(lastp.l)
            ph_.append(lastp.h)
        self.p_low, self.p_high = pl_, ph_
        self.p_ll = rolling_extreme_prev(pl_, CFG["smt_len"], min)
        self.p_hh = rolling_extreme_prev(ph_, CFG["smt_len"], max)
        # pivots
        self.phL = pivots(bars, CFG["liq_len"], "H")
        self.plL = pivots(bars, CFG["liq_len"], "L")
        self.phI = pivots(bars, CFG["mss_len"], "H")
        self.plI = pivots(bars, CFG["mss_len"], "L")
        # HTF bias + range mapped per 15m bar (previous completed 4h bar)
        bias4 = htf_bias_series(h4, CFG["htf_len"])
        h4_t = [b.t for b in h4]
        self.htf_bias = [0] * n
        self.htf_eq = [None] * n
        rl = CFG["htf_range_len"]
        for i, b in enumerate(bars):
            cur = b.t - (b.t % 14400)
            # index of last 4h bar strictly before the containing one
            j = -1
            for k in range(len(h4_t) - 1, -1, -1):
                if h4_t[k] < cur:
                    j = k
                    break
            if j >= 0:
                self.htf_bias[i] = bias4[j]
                if j + 1 >= rl:
                    seg = h4[j + 1 - rl:j + 1]
                    self.htf_eq[i] = (max(x.h for x in seg) + min(x.l for x in seg)) / 2
        # ADR (previous days) + running day range
        self.adr = [None] * n
        self.day_range = [0.0] * n
        dmap = sorted(daily, key=lambda b: b.t)
        d_hl = [(b.t // 86400, b.h - b.l) for b in dmap]
        day = None
        dh = dl = None
        for i, b in enumerate(bars):
            d = b.t // 86400
            if d != day:
                day, dh, dl = d, b.h, b.l
            else:
                dh, dl = max(dh, b.h), min(dl, b.l)
            self.day_range[i] = dh - dl
            prev = [r for dd, r in d_hl if dd < d][-CFG["adr_len"]:]
            if len(prev) >= CFG["adr_len"]:
                self.adr[i] = sum(prev) / len(prev)

        # runtime state
        self.pools_hi: list[Pool] = []
        self.pools_lo: list[Pool] = []
        self.last_int_h = self.last_int_l = None
        self.last_ext_h = self.last_ext_l = None
        self.last_div_bull = self.last_div_bear = -10**9
        self.last_smt_bull = self.last_smt_bear = -10**9
        self.S = Setup()
        self.setup_seq = 0

    # ── helpers ──
    def in_kz(self, i):
        d = datetime.fromtimestamp(self.bars[i].t, timezone.utc).astimezone(NY)
        m = d.hour * 60 + d.minute
        return any(a <= m < b for a, b in CFG["kill_zones"])

    def in_bonus(self, i):
        d = datetime.fromtimestamp(self.bars[i].t, timezone.utc).astimezone(NY)
        m = d.hour * 60 + d.minute
        a, b = CFG["bonus_zone"]
        return a <= m < b

    def emit(self, i, event, extra=None):
        S = self.S
        p = {
            "event": event, "product": self.product, "tf": "15m",
            "bar_time": self.bars[i].t + 900,  # close time of the bar
            "side": "long" if S.dir == 1 else "short",
            "entry": round(S.ep, 2), "stop": round(S.sl, 2), "target": round(S.tp, 2),
            "rr": round(abs(S.tp - S.ep) / S.ru, 2) if S.ru else 0,
            "grade": S.grade or "-", "align": round(S.pct), "layers": S.layers,
            "htf_bias": self.htf_bias[i], "sid": S.sid,
        }
        if extra:
            p.update(extra)
        self.events.append(p)

    def reset(self):
        self.S = Setup()

    # ── confluence scorer ──
    def score(self, i, d, ep, ru, tp):
        S = self.S
        ws = wh = 0.0
        parts = []

        def layer(key, ok, tag):
            nonlocal ws, wh
            en, w = CFG[key]
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
        layer("L_dsp", S.cand_q >= CFG["disp_strong"], "DSP")
        layer("L_vwap", (ep <= self.vwap[i]) if d == 1 else (ep >= self.vwap[i]), "VWP")
        layer("L_smt", S.smt, "SMT")
        layer("L_atr", self.atr_ma[i] is not None and self.atr[i] is not None and self.atr[i] > self.atr_ma[i], "ATR")
        layer("L_eq", S.eq_sweep, "EQ")
        layer("L_kz", self.in_kz(i) or self.in_bonus(i), "KZ")
        layer("L_rr", ((tp - ep) if d == 1 else (ep - tp)) / ru >= CFG["rr_confl"], "RR")
        layer("L_pd", self.htf_eq[i] is not None and ((ep <= self.htf_eq[i]) if d == 1 else (ep >= self.htf_eq[i])), "PD")
        pct = wh / ws * 100 if ws > 0 else 100.0
        return pct, " ".join(parts)

    def next_liq(self, ep, ru, is_long):
        best = None
        pools = self.pools_hi if is_long else self.pools_lo
        for p in pools:
            if p.raid != -1:
                continue
            rr = (p.level - ep) / ru if is_long else (ep - p.level) / ru
            if rr >= CFG["min_rr"] and (best is None or (p.level < best if is_long else p.level > best)):
                best = p.level
        return best

    # ── per-bar step ──
    def step(self, i):
        b = self.bars[i]
        atr = self.atr[i] or 0.0
        S = self.S

        # internal / external swing trackers
        if self.phI[i] is not None:
            self.last_int_h = self.phI[i]
        if self.plI[i] is not None:
            self.last_int_l = self.plI[i]

        # register new pools (with EQ tagging)
        if self.phL[i] is not None:
            v = self.phL[i]
            eq = False
            for p in self.pools_hi:
                if abs(v - p.level) <= CFG["eq_tol_atr"] * atr:
                    eq = p.eq = True
            self.pools_hi.append(Pool(v, -1, v, eq))
            if len(self.pools_hi) > CFG["max_liq"]:
                self.pools_hi.pop(0)
            self.last_ext_h = v
        if self.plL[i] is not None:
            v = self.plL[i]
            eq = False
            for p in self.pools_lo:
                if abs(v - p.level) <= CFG["eq_tol_atr"] * atr:
                    eq = p.eq = True
            self.pools_lo.append(Pool(v, -1, v, eq))
            if len(self.pools_lo) > CFG["max_liq"]:
                self.pools_lo.pop(0)
            self.last_ext_l = v

        # sweep detection
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
                elif i - p.raid >= CFG["reclaim_bars"]:
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
                elif i - p.raid >= CFG["reclaim_bars"]:
                    self.pools_hi.remove(p)
        if hi_n >= 2:
            hi_eq = True

        # divergence / SMT recency
        if self.our_ll[i] is not None and self.rsi[i] is not None:
            prev_rsi = [r for r in self.rsi[max(0, i - CFG["div_len"]):i] if r is not None]
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
            disp_ok = body >= CFG["disp_mult"] * self.atr[i - 1]
            cand_q = body / self.atr[i - 1]
            if b.l > self.bars[i - 2].h and (b.l - self.bars[i - 2].h) >= CFG["min_fvg_atr"] * atr \
                    and disp_ok and self.bars[i - 1].c > self.bars[i - 1].o:
                fvg_bull = True
            if b.h < self.bars[i - 2].l and (self.bars[i - 2].l - b.h) >= CFG["min_fvg_atr"] * atr \
                    and disp_ok and self.bars[i - 1].c < self.bars[i - 1].o:
                fvg_bear = True

        # ── (b) pending / filled lifecycle ──
        if S.state == 2:
            filled = (b.l <= S.ep) if S.dir == 1 else (b.h >= S.ep)
            if filled:
                S.state, S.fill_bar = 3, i
                self.emit(i, "filled")
            else:
                expired = i - S.order_bar > CFG["expiry_bars"]
                violated = (b.c < S.cand_bot or b.h >= S.tp) if S.dir == 1 else (b.c > S.cand_top or b.l <= S.tp)
                if expired or violated:
                    self.emit(i, "cancelled", {"reason": "expired" if expired else "invalidated"})
                    self.reset()
        if S.state == 3:
            hit_sl = (b.l <= S.sl) if S.dir == 1 else (b.h >= S.sl)
            hit_tp = (b.h >= S.tp) if S.dir == 1 else (b.l <= S.tp)
            hit_tp1 = (not S.tp1_done) and ((b.h >= S.tp1) if S.dir == 1 else (b.l <= S.tp1))
            if hit_sl and hit_tp:
                self.emit(i, "closed", {"result": "ambiguous — SL and TP both inside this bar, check fills"})
                self.reset()
            elif hit_sl:
                res = "break-even" if abs(S.sl - S.ep) < 1e-9 else ("stop -1R" if not S.tp1_done else "runner stopped")
                self.emit(i, "closed", {"result": res})
                self.reset()
            else:
                if hit_tp1:
                    S.tp1_done = True
                    self.emit(i, "tp1", {"result": f"partial +{CFG['partial_r']}R banked"})
                if CFG["be_r"] > 0 and S.sl != S.ep:
                    trig = S.ep + CFG["be_r"] * S.ru if S.dir == 1 else S.ep - CFG["be_r"] * S.ru
                    if (b.h >= trig) if S.dir == 1 else (b.l <= trig):
                        S.sl = S.ep
                        self.emit(i, "be", {"result": "stop moved to break-even"})
                if hit_tp:
                    self.emit(i, "closed", {"result": f"target +{round(abs(S.tp-S.ep)/S.ru,1)}R"})
                    self.reset()
        S = self.S

        # ── (c) arm on fresh sweep ──
        long_ok = BIAS_MODE == "Off" or self.htf_bias[i] == 1 or (BIAS_MODE == "Lenient" and self.htf_bias[i] == 0)
        short_ok = BIAS_MODE == "Off" or self.htf_bias[i] == -1 or (BIAS_MODE == "Lenient" and self.htf_bias[i] == 0)
        if S.state in (0, 1):
            if swept_hi and short_ok:
                self.setup_seq += 1
                self.S = S = Setup(dir=-1, state=1, sweep_bar=i, sweep_ext=hi_ext,
                                   mss_lvl=self.last_int_l if self.last_int_l is not None else (self.ll10[i] or b.l),
                                   leg_ext=b.l,
                                   deal_ref=self.last_ext_l if self.last_ext_l is not None else (self.ll10[i] or b.l),
                                   smt=(i - self.last_smt_bear) <= CFG["smt_valid"],
                                   eq_sweep=hi_eq,
                                   rsi_div=(i - self.last_div_bear) <= CFG["div_valid"],
                                   sid=f"{self.product}:{self.bars[i].t}:{self.setup_seq}")
            if swept_lo and long_ok:
                self.setup_seq += 1
                self.S = S = Setup(dir=1, state=1, sweep_bar=i, sweep_ext=lo_ext,
                                   mss_lvl=self.last_int_h if self.last_int_h is not None else (self.hh10[i] or b.h),
                                   leg_ext=b.h,
                                   deal_ref=self.last_ext_h if self.last_ext_h is not None else (self.hh10[i] or b.h),
                                   smt=(i - self.last_smt_bull) <= CFG["smt_valid"],
                                   eq_sweep=lo_eq,
                                   rsi_div=(i - self.last_div_bull) <= CFG["div_valid"],
                                   sid=f"{self.product}:{self.bars[i].t}:{self.setup_seq}")

        # ── (d) state 1: wait for MSS + FVG, score, arm order ──
        if S.state == 1:
            d = S.dir
            if d == 1:
                S.leg_ext = max(S.leg_ext, b.h)
                dead = b.l < S.sweep_ext
            else:
                S.leg_ext = min(S.leg_ext, b.l)
                dead = b.h > S.sweep_ext
            if dead or i - S.sweep_bar > CFG["mss_window"]:
                self.reset()
                return
            if not S.mss_done and ((b.c > S.mss_lvl) if d == 1 else (b.c < S.mss_lvl)):
                S.mss_done = True
            got_fvg = fvg_bull if d == 1 else fvg_bear
            if got_fvg:
                if d == 1 and self.bars[i - 2].h > S.sweep_ext:
                    S.cand_top, S.cand_bot, S.cand_ok = b.l, self.bars[i - 2].h, True
                    S.cand_q = cand_q
                    S.vol_c = self.vol_ma[i - 1] is not None and self.bars[i - 1].v >= CFG["vol_x"] * self.vol_ma[i - 1]
                if d == -1 and self.bars[i - 2].l < S.sweep_ext:
                    S.cand_top, S.cand_bot, S.cand_ok = self.bars[i - 2].l, b.h, True
                    S.cand_q = cand_q
                    S.vol_c = self.vol_ma[i - 1] is not None and self.bars[i - 1].v >= CFG["vol_x"] * self.vol_ma[i - 1]
            elif S.cand_ok and ((b.l < S.cand_bot) if d == 1 else (b.h > S.cand_top)):
                S.cand_ok = False
            if S.mss_done and S.cand_ok:
                em = CFG["entry_mode"]
                if d == 1:
                    ep = S.cand_top if em == "NEAR" else (S.cand_top + S.cand_bot) / 2 if em == "CE" else S.cand_bot
                    sl = S.sweep_ext - CFG["stop_buf_atr"] * atr
                    ru = ep - sl
                else:
                    ep = S.cand_bot if em == "NEAR" else (S.cand_top + S.cand_bot) / 2 if em == "CE" else S.cand_top
                    sl = S.sweep_ext + CFG["stop_buf_atr"] * atr
                    ru = sl - ep
                if ru <= 0:
                    self.reset()
                    return
                tp = ep + CFG["fixed_rr"] * ru if d == 1 else ep - CFG["fixed_rr"] * ru
                if CFG["tp_mode"] == "NEXT_LIQ":
                    nl = self.next_liq(ep, ru, d == 1)
                    if nl is not None:
                        tp = nl - CFG["stop_buf_atr"] * atr if d == 1 else nl + CFG["stop_buf_atr"] * atr
                leg_eq = (max(S.deal_ref, S.leg_ext) + S.sweep_ext) / 2 if d == 1 else (min(S.deal_ref, S.leg_ext) + S.sweep_ext) / 2
                pd_ok = (not CFG["use_pd"]) or ((ep <= leg_eq) if d == 1 else (ep >= leg_eq))
                sess_ok = (not USE_SESSIONS) or self.in_kz(i) or self.in_bonus(i)
                adr_ok = CFG["adr_max"] <= 0 or self.adr[i] is None or self.day_range[i] < CFG["adr_max"] * self.adr[i]
                pct, layers = self.score(i, d, ep, ru, tp)
                grade = "A" if pct >= CFG["grade_a"] else "B" if pct >= CFG["grade_b"] else "C"
                if pd_ok and sess_ok and adr_ok:
                    S.ep, S.sl, S.tp, S.ru = ep, sl, tp, ru
                    S.grade, S.pct, S.layers = grade, pct, layers
                    S.tp1 = ep + CFG["partial_r"] * ru if d == 1 else ep - CFG["partial_r"] * ru
                    S.tp1_done = (not CFG["use_partial"]) or ((S.tp1 >= tp) if d == 1 else (S.tp1 <= tp))
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


# ────────────────────────────── NOTIFY ──────────────────────────────

def send_telegram(text: str):
    if not BOT or not CHAT:
        print("[DRY-RUN telegram]\n" + text + "\n")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT}/sendMessage",
                      json={"chat_id": CHAT, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True},
                      timeout=15).raise_for_status()
    except Exception as e:
        print(f"telegram send failed: {e}")


def fmt_event(p: dict) -> str:
    icon = {"setup": "\U0001F3AF", "filled": "\u2705", "closed": "\U0001F3C1",
            "tp1": "\U0001F4B0", "be": "\U0001F512", "cancelled": "\u274C"}.get(p["event"], "\U0001F514")
    dot = "\U0001F7E2" if p["side"] == "long" else "\U0001F534"
    ts = datetime.fromtimestamp(p["bar_time"], timezone.utc).astimezone(NY).strftime("%b %d %H:%M NY")
    head = f"{icon} <b>{p['event'].upper()}</b> {dot} {p['side'].upper()} · {p['product']} · {p['tf']} · {ts}"
    lines = [head]
    if p["event"] in ("setup", "filled"):
        lines.append(f"Grade <b>{p['grade']}</b> · {p['align']}% aligned")
        risk_mult = 1.0 if p["grade"] == "A" else CFG["b_mult"] if p["grade"] == "B" else CFG["c_mult"]
        ru = abs(p["entry"] - p["stop"])
        qty = ACCOUNT_SIZE * RISK_PCT / 100 * risk_mult / ru if ru else 0
        lines.append(f"Entry <code>{p['entry']}</code> · Stop <code>{p['stop']}</code> · Target <code>{p['target']}</code> ({p['rr']}R)")
        lines.append(f"Size @ {RISK_PCT}%×{risk_mult:g}: <code>{qty:.4f}</code> (acct ${ACCOUNT_SIZE:g})")
        if p.get("layers"):
            lines.append(f"<i>{p['layers']}</i>")
    else:
        if p.get("result"):
            lines.append(p["result"])
        if p.get("reason"):
            lines.append(f"reason: {p['reason']}")
        lines.append(f"E {p['entry']} · S {p['stop']} · T {p['target']}")
    return "\n".join(lines)


def notifiable(p: dict, announced: set) -> bool:
    if p["event"] == "skipped":
        return False
    if p["event"] == "setup":
        return GRADE_RANK.get(p["grade"], 0) >= GRADE_RANK.get(MIN_GRADE, 2)
    return p.get("sid", "") in announced  # lifecycle only for setups we announced


# ────────────────────────────── RUNTIME ──────────────────────────────

def build_engine(product: str) -> Engine:
    pair = "BTC-USD" if product.startswith("ETH") else "ETH-USD"
    bars = fetch_candles(product, 900, 700)
    h1 = fetch_candles(product, 3600, 460)
    daily = fetch_candles(product, 86400, CFG["adr_len"] + 8)
    pbars = fetch_candles(pair, 900, 700)
    return Engine(product, bars, resample_4h(h1), daily, pbars)


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
            json.dump({"sent": list(sent)[-3000:], "announced": list(announced)[-500:]}, f)
    except Exception as e:
        print(f"state save failed (ok without a volume): {e}")


def cycle(sent: set, announced: set, live_window_s: int):
    now = int(time.time())
    for product in PRODUCTS:
        eng = build_engine(product)
        events = eng.run()
        for p in events:
            key = f"{p['product']}|{p['event']}|{p['bar_time']}|{p.get('sid','')}"
            fresh = now - p["bar_time"] <= live_window_s
            if p["event"] == "setup" and notifiable(p, announced):
                if fresh and key not in sent:
                    send_telegram(fmt_event(p))
                    announced.add(p["sid"])
                elif not fresh:
                    announced.add(p["sid"])  # historical; don't re-announce lifecycle noise
                sent.add(key)
            elif p["event"] != "setup" and notifiable(p, announced):
                if fresh and key not in sent:
                    send_telegram(fmt_event(p))
                sent.add(key)
            else:
                sent.add(key)
        S = eng.S
        st = {0: "idle — waiting for a sweep", 1: "swept — waiting for MSS+FVG",
              2: f"{'LONG' if S.dir==1 else 'SHORT'} limit resting @ {S.ep:.2f} ({S.grade})",
              3: f"{'LONG' if S.dir==1 else 'SHORT'} open @ {S.ep:.2f} ({S.grade})"}[S.state]
        bias = {1: "BULLISH", -1: "BEARISH", 0: "NEUTRAL"}[eng.htf_bias[-1]]
        print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {product}: close={eng.bars[-1].c:.2f} "
              f"bias={bias} state={st} pools hi/lo={len(eng.pools_hi)}/{len(eng.pools_lo)}")
    save_state(sent, announced)


def sleep_to_next_quarter(offset_s: int = 25):
    now = time.time()
    nxt = (int(now) // 900 + 1) * 900 + offset_s
    time.sleep(max(5, nxt - now))


def main():
    args = sys.argv[1:]
    if "--backtest" in args:
        days = 5
        if "--days" in args:
            days = int(args[args.index("--days") + 1])
        for product in PRODUCTS:
            eng = build_engine(product)
            events = eng.run()
            cutoff = int(time.time()) - days * 86400
            recent = [p for p in events if p["bar_time"] >= cutoff]
            print(f"\n===== {product}: {len(recent)} events in last {days}d "
                  f"(of {len(events)} total in window) =====")
            for p in recent:
                ts = datetime.fromtimestamp(p["bar_time"], timezone.utc).astimezone(NY).strftime("%m-%d %H:%M")
                extra = p.get("result") or p.get("reason") or ""
                print(f"{ts} NY  {p['event']:<9} {p['side']:<5} {p.get('grade','-')} "
                      f"{p.get('align','')}%  E{p['entry']} S{p['stop']} T{p['target']} {p['rr']}R  {extra}")
                if p["event"] == "setup":
                    print(f"                {p['layers']}")
            S, bias = eng.S, eng.htf_bias[-1]
            print(f"now: bias={bias} state={S.state} close={eng.bars[-1].c}")
        return
    sent, announced = load_state()
    first = True
    fails = 0
    while True:
        try:
            if first:
                cycle(sent, announced, live_window_s=0)   # warm start: announce nothing old
                send_telegram("\U0001F7E2 ICT agent online — watching " + ", ".join(PRODUCTS)
                              + f" · 15m · min grade {MIN_GRADE}")
                first = False
            else:
                cycle(sent, announced, live_window_s=2 * 900 + 120)
            fails = 0
        except Exception:
            fails += 1
            traceback.print_exc()
            if fails in (5, 20):
                send_telegram(f"\u26A0\uFE0F ICT agent: {fails} consecutive cycle failures — check Railway logs")
        if "--once" in args:
            break
        sleep_to_next_quarter()


if __name__ == "__main__":
    main()
