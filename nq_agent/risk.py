"""NQ risk — the team's RiskAgent (portfolio gates) plus NQ-specific caps.

Portfolio gates come from risk_agent.RiskAgent and are shared with ETH/BTC:
daily stop (DAILY_STOP_R), max concurrent open (MAX_OPEN), 2-loss throttle.
On top of that, NQ adds `NQ_MAX_PER_DAY` and `NQ_MAX_OPEN` (default 1 — one
NQ setup at a time). Gated setups are still recorded and ghost-tracked, so the
learning agent can score the gates.
"""


class NQRisk:
    def __init__(self, root_risk, nq_ledger, cfg):
        self.root = root_risk        # risk_agent.RiskAgent
        self.L = nq_ledger
        self.cfg = cfg

    def evaluate(self, setup):
        """-> (gated, notes, reason)"""
        p = setup.to_ledger()
        gated, notes, reason = self.root.evaluate(p)
        if gated:
            return True, [], reason
        if self.cfg.max_per_day > 0 and self.L.today_count() >= self.cfg.max_per_day:
            return True, [], f"NQ daily cap ({self.cfg.max_per_day} setups) reached"
        if self.cfg.max_open > 0 and self.L.active_count() >= self.cfg.max_open:
            return True, [], f"NQ already has {self.L.active_count()} active setup(s) (cap {self.cfg.max_open})"
        # the root's correlation note is worded for ETH/BTC — keep the fact, drop the crypto phrasing
        notes = [n.replace(" — ETH/BTC move together, consider half size", " — consider half size") for n in notes]
        return False, notes, ""

    def status_text(self):
        base = self.root.status_text()
        return base + (f"\n• NQ today {self.L.today_count()}/{self.cfg.max_per_day} · "
                       f"active {self.L.active_count()}/{self.cfg.max_open}")
