"""Sweep → MSS → FVG detector for NQ 1-min bars (bar-close evaluation).

`detect(df, sessions, cfg, bias)` looks only at the LAST closed bar of `df` and
answers "did a complete setup just print?". It is stateless: call it once per
bar close. The pattern, evaluated at bar i (the third candle of the FVG):

  1. time gate     — bar close must sit inside an allowed window (Silver Bullet
                     windows by default) and outside any news blackout.
  2. sweep         — within the last `mss_window` bars, a bar traded through a
                     liquidity level (PDH/PDL, Asia/London/OR high-low, or a 1-min
                     swing pivot) by >= `sweep_min_ticks` and a close came back
                     inside within `reclaim_bars`. The sweep extreme must hold
                     (no trade beyond it) until now.
  3. MSS           — after the reclaim, a close beyond the short-term swing
                     (`mss_len` pivot) that stood before the sweep.
  4. FVG           — bars i-2/i-1/i leave a gap >= `min_fvg_pts`, with bar i-1 a
                     displacement candle (body >= `disp_mult` x ATR) in the setup
                     direction.

Entry = consequent encroachment (midpoint) of the FVG, stop = sweep extreme
±`stop_buf_pts`, target = nearest untouched liquidity giving >= `min_rr`
(else `fixed_rr`). Ambiguous cases return None; a None is the normal answer.

Engineered example (see tests/fixtures/engineered_sweep_mss_fvg.csv, built by
tests/test_nq_detector.py::build_engineered_frame): prior-day low at 20,000.
Bars 10:02–10:04 ET pierce 19,997.5 and close back above 20,000 (sweep+reclaim),
10:06 closes above the 20,012 swing high (MSS) with a 14-point body leaving a
gap between the 10:05 high (20,006) and 10:07 low (20,011) -> long, entry
20,008.5, stop 19,995.5, target at PDH 20,060 -> rr ≈ 3.9.
"""
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from .config import NQConfig

PRODUCT = "NQ"
TF = "1m"


@dataclass
class Setup:
    side: str
    entry: float
    stop: float
    target: float
    rr: float
    sweep_name: str
    sweep_level: float
    sweep_ext: float
    sweep_time: pd.Timestamp
    reclaim_time: pd.Timestamp
    mss_level: float
    mss_time: pd.Timestamp
    fvg_top: float
    fvg_bot: float
    bar_time: pd.Timestamp        # close time (UTC) of the signal bar
    window: str
    grade: str
    align: float
    layers: str
    sid: str
    atr: float = 0.0

    @property
    def risk_pts(self):
        return abs(self.entry - self.stop)

    @property
    def dir(self):
        return 1 if self.side == "long" else -1

    def to_ledger(self):
        """Dict shaped like the crypto engine's setup payload (ledger.record_setup)."""
        return {"sid": self.sid, "product": PRODUCT, "tf": TF, "side": self.side, "grade": self.grade,
                "align": round(self.align), "entry": round(self.entry, 2), "stop": round(self.stop, 2),
                "target": round(self.target, 2), "rr": round(self.rr, 2), "layers": self.layers,
                "bar_time": int(self.bar_time.timestamp()), "event": "setup",
                "sweep": f"{self.sweep_name}@{self.sweep_level:.2f}", "window": self.window}

    def as_dict(self):
        d = asdict(self)
        for k in ("sweep_time", "reclaim_time", "mss_time", "bar_time"):
            d[k] = d[k].isoformat()
        return d


# ------------------------------------------------------------------ helpers

def atr_series(df, n):
    h, l, c = df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float)
    pc = np.concatenate(([c[0]], c[:-1]))
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr, index=df.index).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().to_numpy()


def pivot_points(vals, n, kind):
    """Confirmed swing points: list of (center_index, value). Center i needs n bars each side
    and must be the unique extreme of its 2n+1 window."""
    v = np.asarray(vals, float)
    if len(v) < 2 * n + 1:
        return []
    win = np.lib.stride_tricks.sliding_window_view(v, 2 * n + 1)
    center = win[:, n]
    ext = win.max(axis=1) if kind == "H" else win.min(axis=1)
    uniq = (win == center[:, None]).sum(axis=1) == 1
    idx = np.nonzero((center == ext) & uniq)[0]
    return [(int(c + n), float(v[c + n])) for c in idx]


def htf_bias(df, cfg: NQConfig = None, tf="15min", piv=3):
    """Structure bias from resampled bars: +1 bull, -1 bear, 0 neutral.
    Last event wins: close above the last swing high -> bull, below last swing low -> bear."""
    if df is None or len(df) < 60:
        return 0
    h = df.resample(tf, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    if len(h) < 2 * piv + 5:
        return 0
    ph = dict(pivot_points(h["high"], piv, "H"))
    pl = dict(pivot_points(h["low"], piv, "L"))
    last_ph = last_pl = None
    last_up = last_dn = None
    closes = h["close"].to_numpy(float)
    for i in range(len(h)):
        if i - piv in ph:
            last_ph = ph[i - piv]
        if i - piv in pl:
            last_pl = pl[i - piv]
        if last_ph is not None and closes[i] > last_ph:
            last_up = i
        if last_pl is not None and closes[i] < last_pl:
            last_dn = i
    if last_up is None and last_dn is None:
        return 0
    if last_dn is None or (last_up is not None and last_up > last_dn):
        return 1
    return -1


# ------------------------------------------------------------------ detector

def _find_sweep(lo, hi, cl, i0, i1, levels, direction, cfg):
    """Most recent sweep+reclaim in bars [i0, i1] (inclusive).
    direction +1: lows swept (long setup); -1: highs swept.
    Returns dict or None."""
    thr = cfg.sweep_min_ticks * cfg.tick
    best = None
    for j in range(i1, i0 - 1, -1):
        for name, lvl, is_key in levels:
            if direction == 1:
                if not (lo[j] < lvl - thr + 1e-9):
                    continue
                # must not already be inside a reclaim of the same level (first pierce bar)
                if j > 0 and lo[j - 1] < lvl and j - 1 >= i0:
                    continue
                ext = lo[j]
                k_re = None
                for k in range(j, min(j + cfg.reclaim_bars, i1 + 1)):
                    ext = min(ext, lo[k])
                    if cl[k] > lvl:
                        k_re = k
                        break
            else:
                if not (hi[j] > lvl + thr - 1e-9):
                    continue
                if j > 0 and hi[j - 1] > lvl and j - 1 >= i0:
                    continue
                ext = hi[j]
                k_re = None
                for k in range(j, min(j + cfg.reclaim_bars, i1 + 1)):
                    ext = max(ext, hi[k])
                    if cl[k] < lvl:
                        k_re = k
                        break
            if k_re is None:
                continue
            cand = {"j": j, "k": k_re, "name": name, "level": lvl, "ext": ext, "key": is_key}
            if best is None or (cand["key"] and not best["key"]):
                best = cand
        if best is not None:
            return best
    return None


def detect(df, sessions, cfg: NQConfig = None, bias=None, now=None):
    """Evaluate the last closed 1-min bar of `df`. Returns Setup or None.

    df: UTC-indexed frame (index = bar OPEN time) with open/high/low/close[/volume].
    sessions: SessionEngine already fed with these bars (levels + windows + news).
    bias: optional +1/-1/0 higher-timeframe bias (scored, not gated).
    """
    cfg = cfg or NQConfig()
    need = cfg.atr_len + cfg.mss_window + 2 * cfg.pivot_len + 5
    if df is None or len(df) < need:
        return None
    i = len(df) - 1
    open_ts = df.index[i]
    close_ts = open_ts + pd.Timedelta(minutes=1)

    # 1. time gate
    window = sessions.window(close_ts)
    if window is None:
        return None
    if sessions.in_blackout(close_ts):
        return None

    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    v = df["volume"].to_numpy(float) if "volume" in df.columns else np.zeros(len(df))
    atr = atr_series(df, cfg.atr_len)
    a_prev = atr[i - 1]
    if not np.isfinite(a_prev) or a_prev <= 0:
        return None

    # 4. FVG on bars i-2, i-1, i with displacement candle at i-1
    body = abs(c[i - 1] - o[i - 1])
    disp_q = body / a_prev
    if disp_q < cfg.disp_mult:
        return None
    if c[i - 1] > o[i - 1] and l[i] - h[i - 2] >= cfg.min_fvg_pts:
        direction, fvg_top, fvg_bot = 1, l[i], h[i - 2]
    elif c[i - 1] < o[i - 1] and l[i - 2] - h[i] >= cfg.min_fvg_pts:
        direction, fvg_top, fvg_bot = -1, l[i - 2], h[i]
    else:
        return None

    # 2. sweep of a level within the lookback
    i0 = max(0, i - cfg.mss_window)
    lv = sessions.levels(close_ts)
    key_levels = [(n, p, True) for n, p in lv.pools()]
    piv_n = cfg.pivot_len
    if direction == 1:
        piv = [(f"SWL@{p:.2f}", p, False) for cidx, p in pivot_points(l[:i0 + piv_n + 1], piv_n, "L")
               if cidx + piv_n <= i0 + piv_n]  # confirmed before/at start of lookback
        levels = [(n, p, k) for n, p, k in key_levels + piv if p < fvg_bot]
    else:
        piv = [(f"SWH@{p:.2f}", p, False) for cidx, p in pivot_points(h[:i0 + piv_n + 1], piv_n, "H")
               if cidx + piv_n <= i0 + piv_n]
        levels = [(n, p, k) for n, p, k in key_levels + piv if p > fvg_top]
    if not levels:
        return None
    sw = _find_sweep(l, h, c, i0, i - 1, levels, direction, cfg)
    if sw is None:
        return None
    j, k = sw["j"], sw["k"]
    ext = sw["ext"]
    # sweep extreme must hold from reclaim to now
    if direction == 1 and l[k + 1:i + 1].size and l[k + 1:i + 1].min() < ext:
        return None
    if direction == -1 and h[k + 1:i + 1].size and h[k + 1:i + 1].max() > ext:
        return None

    # 3. MSS: close beyond the short-term swing that stood before the sweep
    m = cfg.mss_len
    if direction == 1:
        piv_h = [p for cidx, p in pivot_points(h[:j + 1], m, "H")]
        mss_lvl = piv_h[-1] if piv_h else float(h[max(0, j - 10):j + 1].max())
        mss_idx = next((x for x in range(k, i + 1) if c[x] > mss_lvl), None)
    else:
        piv_l = [p for cidx, p in pivot_points(l[:j + 1], m, "L")]
        mss_lvl = piv_l[-1] if piv_l else float(l[max(0, j - 10):j + 1].min())
        mss_idx = next((x for x in range(k, i + 1) if c[x] < mss_lvl), None)
    if mss_idx is None:
        return None
    if direction == 1 and mss_lvl <= ext:
        return None
    if direction == -1 and mss_lvl >= ext:
        return None

    # entry / stop / target
    em = cfg.entry_mode
    if direction == 1:
        entry = fvg_top if em == "NEAR" else fvg_bot if em == "FAR" else (fvg_top + fvg_bot) / 2
        stop = ext - cfg.stop_buf_pts
        ru = entry - stop
    else:
        entry = fvg_bot if em == "NEAR" else fvg_top if em == "FAR" else (fvg_top + fvg_bot) / 2
        stop = ext + cfg.stop_buf_pts
        ru = stop - entry
    if ru <= 0 or ru > cfg.max_stop_pts:
        return None

    target = entry + direction * cfg.fixed_rr * ru
    if cfg.tp_mode == "NEXT_LIQ":
        # opposing liquidity: key levels + confirmed 1m pivots beyond entry, nearest that pays min_rr
        if direction == 1:
            opp = [p for _, p in lv.pools() if p > entry]
            opp += [p for _, p in pivot_points(h[:i + 1], piv_n, "H") if p > entry]
            opp = sorted(set(opp))
            for p in opp:
                t = p - cfg.target_buf_pts
                if (t - entry) / ru >= cfg.min_rr:
                    target = t
                    break
        else:
            opp = [p for _, p in lv.pools() if p < entry]
            opp += [p for _, p in pivot_points(l[:i + 1], piv_n, "L") if p < entry]
            opp = sorted(set(opp), reverse=True)
            for p in opp:
                t = p + cfg.target_buf_pts
                if (entry - t) / ru >= cfg.min_rr:
                    target = t
                    break
    rr = (target - entry) / ru if direction == 1 else (entry - target) / ru
    if rr < cfg.min_rr:
        return None

    # scoring
    leg_hi = float(h[j:i + 1].max())
    leg_lo = float(l[j:i + 1].min())
    eq = (leg_hi + leg_lo) / 2
    n_swept = sum(1 for n, p, _ in levels if (l[j:k + 1].min() < p if direction == 1 else h[j:k + 1].max() > p))
    vol_ma = float(np.mean(v[max(0, i - 21):i - 1])) if i > 22 else 0.0
    is_sb = window.startswith("SB")
    layers = [
        ("LVL", sw["key"]),
        ("BIAS", bias is not None and bias == direction),
        ("SB", is_sb),
        ("DSP", disp_q >= cfg.disp_strong),
        ("RR", rr >= cfg.rr_confl),
        ("PD", (entry <= eq) if direction == 1 else (entry >= eq)),
        ("EQ", n_swept >= 2),
        ("VOL", vol_ma > 0 and v[i - 1] >= cfg.vol_x * vol_ma),
    ]
    ws = wh = 0.0
    parts = []
    for tag, ok in layers:
        w = cfg.weights.get(tag, 0.0)
        if w <= 0:
            continue
        ws += w
        wh += w if ok else 0.0
        parts.append(tag + ("✓" if ok else "✗"))
    pct = wh / ws * 100 if ws else 100.0
    grade = "A" if pct >= cfg.grade_a else "B" if pct >= cfg.grade_b else "C"
    side = "long" if direction == 1 else "short"
    return Setup(
        side=side, entry=round(float(entry), 2), stop=round(float(stop), 2), target=round(float(target), 2),
        rr=round(float(rr), 2),
        sweep_name=sw["name"], sweep_level=round(float(sw["level"]), 2), sweep_ext=round(float(ext), 2),
        sweep_time=df.index[j], reclaim_time=df.index[k],
        mss_level=round(mss_lvl, 2), mss_time=df.index[mss_idx],
        fvg_top=round(float(fvg_top), 2), fvg_bot=round(float(fvg_bot), 2),
        bar_time=close_ts, window=window, grade=grade, align=float(pct), layers=" ".join(parts),
        sid=f"{PRODUCT}:{TF}:{int(close_ts.timestamp())}:{side}", atr=round(float(a_prev), 2))
