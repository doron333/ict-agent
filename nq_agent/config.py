"""NQ agent configuration.

Every detector threshold lives here (with an env override) so it can be tuned from
ledger / backtest evidence. Do not loosen thresholds by hand to make the agent fire
more — change them only when `/nq stats` or `backtest.py` says the change earns.

All times below are America/New_York wall-clock strings ("HH:MM"). Timestamps in
the data path are always UTC; sessions.py does the conversion.
"""
import os
from dataclasses import dataclass, field, fields


def _env(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    if isinstance(default, bool):
        return v.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(float(v))
    if isinstance(default, float):
        return float(v)
    return v


def _window_env(name, default):
    """'03:00-04:00,10:00-11:00' -> [("03:00","04:00"), ...]"""
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    out = []
    for part in raw.split(","):
        a, b = part.strip().split("-")
        out.append((a.strip(), b.strip()))
    return out


@dataclass
class NQConfig:
    # ---- instrument / data ----
    symbol: str = "NQ.c.0"            # continuous front month (or MNQ.c.0)
    dataset: str = "GLBX.MDP3"
    tick: float = 0.25
    point_value: float = 20.0         # $ per point per contract (MNQ = 2.0)
    mode: str = "paper"               # alert | paper  (never "live" — see execution.py)
    feed: str = "live"                # live | poll | csv
    csv_path: str = ""
    backfill_days: int = 6
    keep_bars: int = 4000             # rolling in-memory 1m history
    bar_grace_s: int = 5              # flush a 1m bar this long after minute end even with no trade

    # ---- ET session engine ----
    trade_date_roll: str = "18:00"    # CME Globex open — trade date rolls here
    asia: tuple = ("20:00", "00:00")
    london: tuple = ("02:00", "05:00")
    ny: tuple = ("09:30", "16:00")    # RTH cash session
    opening_range: tuple = ("09:30", "09:45")
    pd_source: str = "rth"            # PDH/PDL from prior "rth" (09:30-16:00) or "globex" session
    windows: list = field(default_factory=lambda: [("03:00", "04:00"), ("10:00", "11:00"), ("14:00", "15:00")])
    window_names: list = field(default_factory=lambda: ["SB_LDN", "SB_AM", "SB_PM"])
    news_before_min: int = 15
    news_after_min: int = 15

    # ---- detector thresholds (tune from evidence) ----
    atr_len: int = 14
    pivot_len: int = 5                # swing pools (1m pivots) used as secondary liquidity
    sweep_min_ticks: int = 2          # low must trade this far through the level
    reclaim_bars: int = 3             # close back inside within N bars of the pierce
    mss_len: int = 3                  # short-term swing used for the structure shift
    mss_window: int = 20              # sweep → FVG must complete within N bars
    disp_mult: float = 1.2            # displacement candle body >= x ATR
    disp_strong: float = 2.0          # "strong displacement" layer
    min_fvg_pts: float = 2.0          # FVG gap size in points
    stop_buf_pts: float = 2.0         # stop = sweep extreme -/+ buffer
    max_stop_pts: float = 60.0        # skip setups with absurd risk (data spikes, gaps)
    min_rr: float = 2.0
    fixed_rr: float = 2.0             # used when no liquidity target qualifies
    rr_confl: float = 3.0             # "RR" layer
    tp_mode: str = "NEXT_LIQ"         # NEXT_LIQ | FIXED
    target_buf_pts: float = 1.0       # take profit just in front of the liquidity
    entry_mode: str = "CE"            # CE | NEAR | FAR
    expiry_bars: int = 30             # unfilled limit expires
    one_per_sweep: bool = True
    use_partial: bool = True
    partial_r: float = 1.0
    be_r: float = 1.0
    vol_x: float = 1.2

    # ---- grading ----
    grade_a: float = 70.0
    grade_b: float = 50.0
    min_grade: str = "B"
    weights: dict = field(default_factory=lambda: {
        "LVL": 2.0, "BIAS": 1.5, "SB": 1.0, "DSP": 1.0, "RR": 1.0, "PD": 1.0, "EQ": 1.0, "VOL": 1.0})

    # ---- NQ-specific risk (portfolio gates come from risk_agent.py) ----
    max_per_day: int = 3
    max_open: int = 1
    account_size: float = 10000.0
    risk_pct: float = 1.0

    @classmethod
    def from_env(cls):
        c = cls()
        c.symbol = _env("NQ_SYMBOL", c.symbol)
        c.dataset = _env("NQ_DATASET", c.dataset)
        c.tick = _env("NQ_TICK", c.tick)
        c.point_value = _env("NQ_POINT_VALUE", 2.0 if c.symbol.upper().startswith("MNQ") else c.point_value)
        c.mode = _env("NQ_MODE", c.mode).lower()
        c.feed = _env("NQ_FEED", c.feed).lower()
        c.csv_path = _env("NQ_CSV", c.csv_path)
        c.backfill_days = _env("NQ_BACKFILL_DAYS", c.backfill_days)
        c.pd_source = _env("NQ_PD_SOURCE", c.pd_source).lower()
        c.windows = _window_env("NQ_WINDOWS", c.windows)
        if len(c.window_names) != len(c.windows):
            c.window_names = [f"W{i + 1}" for i in range(len(c.windows))]
        c.news_before_min = _env("NQ_NEWS_BEFORE_MIN", c.news_before_min)
        c.news_after_min = _env("NQ_NEWS_AFTER_MIN", c.news_after_min)
        for f in fields(cls):
            key = "NQ_" + f.name.upper()
            if f.name in ("symbol", "dataset", "tick", "point_value", "mode", "feed", "csv_path",
                          "backfill_days", "pd_source", "windows", "window_names", "weights",
                          "asia", "london", "ny", "opening_range", "news_before_min", "news_after_min"):
                continue
            if isinstance(getattr(c, f.name), (int, float, str, bool)):
                setattr(c, f.name, _env(key, getattr(c, f.name)))
        c.min_grade = c.min_grade.upper()
        # share the crypto team's sizing env when the NQ ones aren't set
        c.account_size = _env("NQ_ACCOUNT_SIZE", float(os.environ.get("ACCOUNT_SIZE", c.account_size)))
        c.risk_pct = _env("NQ_RISK_PCT", float(os.environ.get("RISK_PCT", c.risk_pct)))
        if c.mode not in ("alert", "paper"):
            # "live" (or anything else) is refused by policy — see execution.py
            print(f"nq: NQ_MODE={c.mode!r} is not supported (alert-only policy) — using 'paper'")
            c.mode = "paper"
        return c

    def summary(self):
        w = ", ".join(f"{n} {a}-{b}" for n, (a, b) in zip(self.window_names, self.windows))
        return (f"{self.symbol} · mode {self.mode} · feed {self.feed} · windows {w} ET · "
                f"sweep≥{self.sweep_min_ticks}t reclaim≤{self.reclaim_bars} mss≤{self.mss_window} "
                f"disp≥{self.disp_mult}xATR fvg≥{self.min_fvg_pts}pt rr≥{self.min_rr} · min grade {self.min_grade}")
