"""Smoke tests for the NQ sweep → MSS → FVG pipeline.

(a) a seeded random walk over a full trade date never produces a setup;
(b) an engineered sweep → MSS → FVG sequence (tests/fixtures/engineered_sweep_mss_fvg.csv,
    regenerate with `python -m tests.test_nq_detector`) returns a long Setup with rr >= 2.
"""
import os

import numpy as np
import pandas as pd
import pytest

from nq_agent.config import NQConfig
from nq_agent.sessions import SessionEngine
from nq_agent.detector import detect, Setup
from nq_agent.tracker import Tracker
from nq_agent.data.databento_feed import normalize

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "engineered_sweep_mss_fvg.csv")
LOOKBACK = 240


def _et(s):
    return pd.Timestamp(s, tz="America/New_York").tz_convert("UTC")


# ----------------------------------------------------------------- builders

def build_engineered_frame():
    """Prior RTH day (Tue 2026-09-01) prints PDH 20060 / PDL 20000. On Wed 2026-09-02 price
    drifts down into the 10:00 Silver Bullet window, sweeps PDL at 10:02-10:03, reclaims at
    10:04, displaces through the pre-sweep swing high at 10:06 and leaves an FVG confirmed by
    the 10:07 bar (low 20011 > 10:05 high 20004)."""
    rows = []

    def bar(ts, o, h, l, c, v=500):
        ts = _et(ts) if isinstance(ts, str) else ts.tz_convert("UTC")
        rows.append((ts, o, h, l, c, v))

    t = pd.Timestamp("2026-09-01 09:30", tz="America/New_York")
    end = pd.Timestamp("2026-09-01 16:00", tz="America/New_York")
    i = 0
    while t < end:
        px = 20030 + 3 * np.sin(i / 7.0)
        h, l = px + 1.0, px - 1.0
        if i == 60:
            h = 20060.0
        if i == 200:
            l = 20000.0
        bar(t, px, h, l, px + 0.25)
        t += pd.Timedelta(minutes=1)
        i += 1
    t = pd.Timestamp("2026-09-02 09:00", tz="America/New_York")
    i = 0
    while t < pd.Timestamp("2026-09-02 10:02", tz="America/New_York"):
        px = 20020 - i * 0.2 + 1.0 * np.sin(i / 3.0)
        bar(t, px, px + 0.75, px - 0.75, px - 0.1)
        t += pd.Timedelta(minutes=1)
        i += 1
    bar("2026-09-02 10:02", 20005.0, 20005.5, 19998.5, 19999.0)          # pierce PDL
    bar("2026-09-02 10:03", 19999.0, 20001.0, 19997.5, 19999.5)          # sweep extreme 19997.5
    bar("2026-09-02 10:04", 19999.5, 20002.0, 19998.0, 20001.5)          # reclaim (close > PDL)
    bar("2026-09-02 10:05", 20001.5, 20004.0, 20000.5, 20003.0)
    bar("2026-09-02 10:06", 20003.0, 20019.0, 20002.5, 20018.5, 2000)    # displacement + MSS
    bar("2026-09-02 10:07", 20018.5, 20020.0, 20011.0, 20014.0)          # FVG 20004-20011 confirmed
    return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).set_index("ts")


def build_random_walk(seed=7, step=0.5, wick=0.5):
    """One full trade date (Tue 18:00 ET -> Wed 17:00 ET) of seeded 1-min noise around 20,000."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(_et("2026-09-01 18:00"), _et("2026-09-02 17:00"), freq="1min", inclusive="left")
    closes = 20000 + np.cumsum(rng.normal(0, step, len(idx)))
    opens = np.concatenate(([20000.0], closes[:-1]))
    highs = np.maximum(opens, closes) + rng.uniform(0, wick, len(idx))
    lows = np.minimum(opens, closes) - rng.uniform(0, wick, len(idx))
    vol = rng.integers(100, 900, len(idx)).astype(float)
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": vol},
                        index=pd.DatetimeIndex(idx, name="ts"))


def _scan(df, cfg):
    """Feed bars one at a time, exactly like the live loop. Returns list of (i, Setup)."""
    s = SessionEngine(cfg)
    hits = []
    for i in range(len(df)):
        r = df.iloc[i]
        s.update(df.index[i], r.open, r.high, r.low, r.close)
        st = detect(df.iloc[max(0, i + 1 - LOOKBACK):i + 1], s, cfg)
        if st is not None:
            hits.append((i, st))
    return hits


# ----------------------------------------------------------------- tests

@pytest.mark.parametrize("seed", [7, 11, 23])
def test_random_walk_never_fires(seed):
    cfg = NQConfig()
    df = build_random_walk(seed)
    hits = _scan(df, cfg)
    assert hits == [], f"random walk produced setups: {[(i, h.side, h.sweep_name) for i, h in hits]}"


def test_engineered_sweep_mss_fvg_fires_long():
    cfg = NQConfig()
    df = normalize(pd.read_csv(FIXTURE)) if os.path.exists(FIXTURE) else build_engineered_frame()
    s = SessionEngine(cfg)
    s.ingest(df)
    lv = s.levels(df.index[-1])
    assert (lv.pdh, lv.pdl) == (20060.0, 20000.0)
    st = detect(df.tail(LOOKBACK), s, cfg)
    assert isinstance(st, Setup)
    assert st.side == "long"
    assert st.sweep_name == "PDL" and st.sweep_ext == 19997.5
    assert st.fvg_bot == 20004.0 and st.fvg_top == 20011.0
    assert st.fvg_bot <= st.entry <= st.fvg_top
    assert st.stop < st.sweep_ext
    assert st.rr >= 2
    assert st.window == "SB_AM"
    assert st.grade in ("A", "B")
    # the setup exists only on the FVG confirmation bar — nothing earlier in the window
    hits = _scan(df, cfg)
    assert [i for i, _ in hits] == [len(df) - 1]


def test_engineered_fixture_matches_builder():
    if not os.path.exists(FIXTURE):
        pytest.skip("fixture not generated")
    a = normalize(pd.read_csv(FIXTURE))
    b = normalize(build_engineered_frame())
    pd.testing.assert_frame_equal(a, b, check_exact=False, atol=1e-6)


def test_detector_respects_window_and_blackout():
    cfg = NQConfig()
    df = build_engineered_frame()
    s = SessionEngine(cfg)
    s.ingest(df)
    assert detect(df.tail(LOOKBACK), s, cfg) is not None
    s.add_news(df.index[-1] + pd.Timedelta(minutes=1), "CPI")          # blackout covers the signal bar
    assert detect(df.tail(LOOKBACK), s, cfg) is None
    s.clear_news()
    cfg2 = NQConfig()
    cfg2.windows, cfg2.window_names = [("14:00", "15:00")], ["SB_PM"]  # 10:07 ET is outside
    s2 = SessionEngine(cfg2)
    s2.ingest(df)
    assert detect(df.tail(LOOKBACK), s2, cfg2) is None


def test_tracker_lifecycle_and_intrabar_caveat():
    cfg = NQConfig()
    df = build_engineered_frame()
    s = SessionEngine(cfg)
    s.ingest(df)
    st = detect(df.tail(LOOKBACK), s, cfg)
    t0 = df.index[-1]
    m = pd.Timedelta(minutes=1)
    tr = Tracker(cfg)
    tr.add(st)
    ev = tr.on_bar(t0 + m, 20014, 20015, 20007, 20010)                # dips into the FVG -> fill
    assert [e["event"] for e in ev] == ["filled"]
    ev = tr.on_bar(t0 + 2 * m, 20010, 20062, 20009, 20055)             # runs to target
    assert [e["event"] for e in ev] == ["tp1", "be", "closed"]
    assert ev[-1]["result"].startswith("target") and tr.closed[0].r_net == pytest.approx(0.5 * 1 + 0.5 * st.rr, abs=0.01)
    # ambiguous bar: stop and target inside one bar -> scored as a stop
    tr = Tracker(cfg)
    tr.add(st)
    tr.on_bar(t0 + m, 20014, 20015, 20007, 20010)
    ev = tr.on_bar(t0 + 2 * m, 20010, 20062, 19990, 20030)
    assert ev[-1]["event"] == "closed" and ev[-1]["result"].startswith("stop") and tr.closed[0].r_net == -1.0
    # expiry
    tr = Tracker(cfg)
    tr.add(st)
    for k in range(1, cfg.expiry_bars + 2):
        ev = tr.on_bar(t0 + k * m, 20030, 20031, 20029, 20030)
    assert ev and ev[-1]["event"] == "cancelled" and ev[-1]["reason"] == "expired"


def test_sessions_levels_windows_trade_date():
    cfg = NQConfig()
    s = SessionEngine(cfg)
    assert str(s.trade_date(_et("2026-09-01 17:59"))) == "2026-09-01"
    assert str(s.trade_date(_et("2026-09-01 18:00"))) == "2026-09-02"
    assert s.window(_et("2026-09-02 10:30")) == "SB_AM"
    assert s.window(_et("2026-09-02 03:15")) == "SB_LDN"
    assert s.window(_et("2026-09-02 12:00")) is None
    idx = pd.date_range(_et("2026-09-01 18:00"), _et("2026-09-02 12:00"), freq="1min", inclusive="left")
    for i, ts in enumerate(idx):
        et = ts.tz_convert("America/New_York")
        px = 20000.0
        if et.strftime("%H:%M") == "21:00":
            px = 20050.0                                    # Asia high
        if et.strftime("%H:%M") == "03:30":
            px = 19950.0                                    # London low
        s.update(ts, px, px + 1, px - 1, px)
    lv = s.levels(idx[-1])
    assert lv.asia_h == 20051.0 and lv.lon_l == 19949.0
    assert lv.or_h == 20001.0 and lv.or_l == 19999.0
    assert lv.open_0000 == 20000.0
    assert "PDH" in s.levels_text(idx[-1])


def test_ledger_expectancy_uses_shared_ledger(tmp_path):
    from ledger import Ledger
    from nq_agent.ledger import NQLedger
    root = Ledger(str(tmp_path), partial_r=1.0)
    L = NQLedger(root)
    df = build_engineered_frame()
    s = SessionEngine(NQConfig())
    s.ingest(df)
    st = detect(df.tail(LOOKBACK), s, NQConfig())
    rid = L.record(st)
    assert rid == L.record(st)                                             # idempotent by sid
    assert L.active_count() == 1 and L.today_count() == 1
    L.on_event({"sid": st.sid, "event": "filled"})
    L.on_event({"sid": st.sid, "event": "tp1"})
    L.on_event({"sid": st.sid, "event": "closed", "result": f"target +{st.rr}R"})
    e = L.expectancy(30)
    assert e["n"] == 1 and e["wins"] == 1
    assert e["expectancy"] == pytest.approx(0.5 + 0.5 * st.rr, abs=0.01)
    assert "Expectancy" in L.stats_text(30)
    assert not L.paused()
    L.set_paused(True)
    assert L.paused()


def test_news_sync_from_macro_and_static():
    from nq_agent import news
    cfg = NQConfig()
    s = SessionEngine(cfg)

    class FakeMacro:
        events = [{"t": _et("2026-09-02 08:30").timestamp(), "title": "CPI m/m", "impact": "High"},
                  {"t": _et("2026-09-02 10:00").timestamp(), "title": "Consumer Confidence", "impact": "Medium"}]

    n = news.sync(s, FakeMacro(), now=_et("2026-09-01 12:00"))
    assert n >= 1
    assert s.in_blackout(_et("2026-09-02 08:20")) == "CPI m/m"
    assert s.in_blackout(_et("2026-09-02 08:46")) is None
    assert s.in_blackout(_et("2026-09-02 10:00")) is None                  # medium impact, no keyword
    # static fallback fills dates the live calendar doesn't cover
    assert any("static" in lbl for _, _, lbl in s.news)


def test_nq_commands_dispatch():
    from nq_agent import commands as nq_cmds
    import commands as root_cmds

    class Stub:
        class L:
            paused_flag = False

            @classmethod
            def set_paused(cls, v):
                cls.paused_flag = v

        def status_text(self):
            return "STATUS"

        def levels_text(self):
            return "LEVELS"

        def bias_text(self):
            return "BIAS"

        def stats_text(self, days=30):
            return f"STATS {days}"

    a = Stub()
    assert nq_cmds.dispatch(a, "/nq status") == "STATUS"
    assert nq_cmds.dispatch(a, "nq levels") == "LEVELS"
    assert nq_cmds.dispatch(a, "nq bias") == "BIAS"
    assert nq_cmds.dispatch(a, "nq stats 14") == "STATS 14"
    assert nq_cmds.dispatch(a, "nq stats") == "STATS 30"
    assert "paused" in nq_cmds.dispatch(a, "nq pause").lower() and Stub.L.paused_flag is True
    assert "resumed" in nq_cmds.dispatch(a, "nq resume").lower() and Stub.L.paused_flag is False
    assert "not running" in nq_cmds.dispatch(None, "nq status")

    class Team:
        nq = a
        ledger = None

    assert root_cmds.dispatch(Team(), "/nq levels") == "LEVELS"          # routed through the team dispatcher


if __name__ == "__main__":
    os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
    build_engineered_frame().to_csv(FIXTURE)
    print("wrote", FIXTURE)
