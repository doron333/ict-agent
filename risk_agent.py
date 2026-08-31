"""Risk agent — portfolio guardrails across all six engines. Gates are logged, never silent."""
import os


class RiskAgent:
    def __init__(self, ledger):
        self.L = ledger
        self.daily_stop = float(os.environ.get("DAILY_STOP_R", "3"))
        self.max_open = int(os.environ.get("MAX_OPEN", "3"))

    def evaluate(self, p):
        """-> (gated: bool, notes: list[str], gate_reason: str). Gated setups are still
        tracked in the ledger so the learning agent can score the gate itself."""
        day = self.L.today_r()
        n_open = self.L.open_count()

        if self.daily_stop > 0 and day <= -self.daily_stop:
            return True, [], f"daily stop hit ({day:+.1f}R ≤ −{self.daily_stop:g}R) — done for today"
        if n_open >= self.max_open:
            return True, [], f"max concurrent setups ({n_open}/{self.max_open}) already open"

        last2 = self.L.last_closed_r(2)
        streak = len(last2) == 2 and all((r or 0) < -0.2 for r in last2)
        if streak and p["grade"] != "A":
            return True, [], "2-loss streak — only A grades until a winner resets it"

        notes = []
        other = self.L.open_same_side(p["product"], p["side"])
        if other:
            notes.append(f"🛡 correlated: {other} already open {p['side']} — ETH/BTC move together, consider half size")
        if streak:
            notes.append("🛡 loss-streak throttle active (A-only) — this one qualifies")
        if day or n_open:
            notes.append(f"🛡 day {day:+.1f}R · {n_open} open")
        return False, notes, ""

    def status_text(self):
        day = self.L.today_r()
        n_open = self.L.open_count()
        last2 = self.L.last_closed_r(2)
        streak = len(last2) == 2 and all((r or 0) < -0.2 for r in last2)
        rows = [f"Day {day:+.1f}R (stop at −{self.daily_stop:g}R)",
                f"Open setups {n_open}/{self.max_open}",
                f"Loss-streak throttle: {'ACTIVE — A grades only' if streak else 'off'}"]
        g = self.L.q("SELECT COUNT(*), COALESCE(SUM(r_net),0) FROM setups WHERE gated=1 AND r_net IS NOT NULL")
        if g and g[0][0]:
            n, r = g[0]
            saved = f"avoided {-r:+.1f}R of damage" if r < 0 else f"cost {r:+.1f}R of missed wins"
            rows.append(f"Gates so far: {n} setups blocked → {saved}")
        return "🛡 Risk state:\n" + "\n".join("• " + x for x in rows)
