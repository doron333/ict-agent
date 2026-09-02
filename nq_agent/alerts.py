"""NQ alert formatting. Delivery reuses the team's Telegram sender (main.send_telegram)."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
GRADE_MULT = {"A": 1.0, "B": 0.65, "C": 0.40}     # same risk multipliers as the crypto engine
ICON = {"setup": "🎯", "filled": "✅", "closed": "🏁", "tp1": "💰", "be": "🔒", "cancelled": "❌"}


def default_sender():
    """The repo's Telegram sender, imported lazily to avoid a circular import with main.py.
    Falls back to a dry-run printer when the root module isn't importable (e.g. tests)."""
    try:
        from main import send_telegram  # repo root
        return send_telegram
    except Exception:
        def _dry(text):
            print("[DRY-RUN telegram]\n" + text + "\n")
        return _dry


def _ny(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).astimezone(NY).strftime("%b %d %H:%M ET")


class Alerts:
    def __init__(self, send, cfg):
        self.send = send or default_sender()
        self.cfg = cfg

    # ---------------- sizing ----------------
    def contracts(self, setup):
        cfg = self.cfg
        risk_usd = cfg.account_size * cfg.risk_pct / 100 * GRADE_MULT.get(setup.grade, 0.4)
        per_ct = setup.risk_pts * cfg.point_value
        n = int(risk_usd // per_ct) if per_ct > 0 else 0
        return n, risk_usd, per_ct

    # ---------------- texts ----------------
    def online_text(self, bars, first_ts, last_ts, data_dir):
        cfg = self.cfg
        span = f"{bars} bars {_ny(first_ts)} → {_ny(last_ts)}" if bars else "no history yet"
        return ("🟩 <b>NQ agent online</b> — " + cfg.summary() +
                f"\nBackfill: {span}\nPolicy: alert-only (paper fills simulated, no orders) · ledger @ {data_dir}"
                "\nText <b>nq help</b> for commands")

    def setup_text(self, setup, rid=None):
        s = setup
        dot = "🟢" if s.side == "long" else "🔴"
        n, risk_usd, per_ct = self.contracts(s)
        lines = [f"{ICON['setup']} <b>SETUP</b> {dot} {s.side.upper()} · NQ · <b>1m</b> · {_ny(s.bar_time.timestamp())} · {s.window}",
                 f"Grade <b>{s.grade}</b> · {s.align:.0f}% aligned",
                 f"Sweep {s.sweep_name} {s.sweep_level:.2f} (ext {s.sweep_ext:.2f}) → MSS {s.mss_level:.2f} → FVG {s.fvg_bot:.2f}-{s.fvg_top:.2f}",
                 f"Entry <code>{s.entry:.2f}</code> · Stop <code>{s.stop:.2f}</code> ({s.risk_pts:.2f} pts) · "
                 f"Target <code>{s.target:.2f}</code> ({s.rr:.1f}R)",
                 f"Size @ {self.cfg.risk_pct:g}%×{GRADE_MULT.get(s.grade, 0.4):g}: <code>{n}</code> {self.cfg.symbol.split('.')[0]} "
                 f"(${per_ct:,.0f}/ct risk, ${risk_usd:,.0f} budget)",
                 f"<i>{s.layers}</i>"]
        if rid is not None:
            lines.append(f"🎫 <b>#{rid}</b> — reply <code>took {rid} &lt;price&gt;</code> if you take it")
        return "\n".join(lines)

    def event_text(self, ev):
        s = ev["setup"]
        dot = "🟢" if s.side == "long" else "🔴"
        head = f"{ICON.get(ev['event'], '🔔')} <b>{ev['event'].upper()}</b> {dot} {s.side.upper()} · NQ · <b>1m</b> · {_ny(ev['bar_time'])}"
        lines = [head]
        if ev.get("result"):
            lines.append(ev["result"])
        if ev.get("reason"):
            lines.append(f"reason: {ev['reason']}")
        lines.append(f"E {s.entry:.2f} · S {ev['stop']:.2f} · T {s.target:.2f}")
        return "\n".join(lines)

    def gated_text(self, setup, rid, reason):
        return (f"⛔ <b>#{rid} gated</b> — {reason}\n"
                f"(NQ 1m {setup.side} {setup.grade}, {setup.align:.0f}% · still tracked as a ghost)")
