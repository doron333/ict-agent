"""NQ view over the team's shared ledger (ledger.Ledger in the repo root).

No second database: NQ setups are rows in the same `setups` table with
product='NQ', tf='1m', so the portfolio risk gates (daily stop, max open,
loss streak) see crypto and NQ together, and the learning agent's scorecard
includes NQ automatically. This module only adds NQ-scoped reads
(`expectancy`, counts) and the NQ pause flag.
"""
import time

from .detector import PRODUCT, TF


class NQLedger:
    def __init__(self, root):
        self.root = root                      # ledger.Ledger

    # ---------------- passthrough ----------------
    @property
    def db(self):
        return self.root

    def record(self, setup, context=None, macro=None, review=None, gated=False, reason=""):
        return self.root.record_setup(setup.to_ledger(), context=context, macro=macro, review=review,
                                      gated=gated, gate_reason=reason)

    def on_event(self, ev):
        """ev: tracker event dict (has sid/event/result/reason)."""
        return self.root.on_event(ev)

    def find_sid(self, sid):
        r = self.root.q("SELECT id,status,gated FROM setups WHERE sid=?", (sid,))
        return r[0] if r else None

    # ---------------- pause flag ----------------
    def paused(self):
        return self.root.meta_get("nq_paused") == "1"

    def set_paused(self, on):
        self.root.meta_set("nq_paused", "1" if on else "0")

    # ---------------- NQ-scoped counts ----------------
    def today_count(self):
        from ledger import ny_day_start  # repo root module
        r = self.root.q("SELECT COUNT(*) FROM setups WHERE product=? AND gated=0 AND created>=?",
                        (PRODUCT, ny_day_start()))
        return r[0][0]

    def active_count(self):
        r = self.root.q("SELECT COUNT(*) FROM setups WHERE product=? AND gated=0 AND status IN "
                        "('PENDING','FILLED','TP1','BE')", (PRODUCT,))
        return r[0][0]

    def open_rows(self):
        return self.root.q("SELECT id,side,grade,entry,stop,target,status FROM setups WHERE product=? AND gated=0 "
                           "AND status IN ('PENDING','FILLED','TP1','BE') ORDER BY created", (PRODUCT,))

    def recent(self, n=8):
        return self.root.q("SELECT id,created,side,grade,status,r_net,result,gated FROM setups WHERE product=? "
                           "ORDER BY created DESC LIMIT ?", (PRODUCT, n))

    # ---------------- expectancy ----------------
    def expectancy(self, days=30, include_gated=False):
        """Forward paper record for NQ over the last `days` days.
        Returns a dict; expectancy = mean net R per closed setup."""
        cond = "" if include_gated else " AND gated=0"
        args = [PRODUCT, TF]
        if days:
            cond += " AND closed>=?"
            args.append(time.time() - days * 86400)
        rows = self.root.q("SELECT grade,side,r_net,result FROM setups WHERE product=? AND tf=? "
                           "AND status='CLOSED' AND r_net IS NOT NULL" + cond, tuple(args))
        rs = [r[2] for r in rows]
        n = len(rs)
        wins = [r for r in rs if r > 0.1]
        losses = [r for r in rs if r < -0.1]
        be = n - len(wins) - len(losses)
        out = {"days": days, "n": n, "wins": len(wins), "losses": len(losses), "be": be,
               "win_rate": (len(wins) / n) if n else 0.0,
               "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
               "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
               "expectancy": (sum(rs) / n) if n else 0.0, "total": sum(rs),
               "by_grade": {}, "by_side": {}}
        for key, idx in (("by_grade", 0), ("by_side", 1)):
            b = {}
            for r in rows:
                b.setdefault(r[idx], []).append(r[2])
            out[key] = {k: {"n": len(v), "total": sum(v), "avg": sum(v) / len(v)} for k, v in b.items()}
        pend = self.root.q("SELECT COUNT(*) FROM setups WHERE product=? AND status IN ('PENDING','FILLED','TP1','BE')",
                           (PRODUCT,))
        out["active"] = pend[0][0]
        return out

    def stats_text(self, days=30):
        e = self.expectancy(days)
        if not e["n"]:
            return (f"📊 <b>NQ stats</b> (last {days}d): no closed NQ setups yet · {e['active']} active.\n"
                    "Forward paper record only — backtests never enter the ledger.")
        rows = [f"📊 <b>NQ stats</b> — last {days}d, {e['n']} closed (forward paper record)",
                f"Expectancy <b>{e['expectancy']:+.2f}R</b> per setup · total {e['total']:+.1f}R",
                f"Win rate {e['win_rate'] * 100:.0f}% ({e['wins']}W / {e['losses']}L / {e['be']}BE)",
                f"Avg win {e['avg_win']:+.2f}R · avg loss {e['avg_loss']:+.2f}R"]
        if e["by_grade"]:
            rows.append("By grade: " + " · ".join(f"{g} n={d['n']} {d['avg']:+.2f}R"
                                                  for g, d in sorted(e["by_grade"].items())))
        if e["by_side"]:
            rows.append("By side: " + " · ".join(f"{s} n={d['n']} {d['avg']:+.2f}R"
                                                 for s, d in sorted(e["by_side"].items())))
        if e["active"]:
            rows.append(f"Active now: {e['active']}")
        return "\n".join(rows)
