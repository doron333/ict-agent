"""Team orchestrator — one process, six specialists, shared ledger."""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import learning_agent as learning

NY = ZoneInfo("America/New_York")


class Team:
    def __init__(self, ledger, context, macro, risk, review, base_cfg, send, fmt_event):
        self.ledger = ledger
        self.context = context
        self.macro = macro
        self.risk = risk
        self.review = review
        self.base_cfg = base_cfg
        self.send = send
        self.fmt_event = fmt_event
        self.live = {}          # (product, tf) -> engine snapshot for `status`
        self._pre_sent = set()
        self.nq = None          # nq_agent.main.NQAgent when NQ_ENABLED=1 (or standalone)

    def paused(self):
        return self.ledger.meta_get("paused") == "1"

    # ---------------- setup pipeline ----------------
    def handle_setup(self, p):
        """Called for each fresh, notifiable setup. Enrich → gate → record → alert."""
        self.context.maybe_refresh()
        ctx_lines = self.context.lines(p["product"], p["side"])
        mlevel, mline = self.macro.check()

        gated, notes, reason = self.risk.evaluate(p)
        if not gated and self.macro.mode == "veto" and mlevel == "hot":
            gated, reason = True, f"macro veto — {mline.lstrip('🗓 ')}"

        review = None
        if not gated:
            review = self.review.judge(p, ctx_lines, mline, self._lane_line(p))

        rid = self.ledger.record_setup(p, context=self.context.snap, macro={"level": mlevel, "line": mline},
                                       review=review, gated=gated, gate_reason=reason)

        if gated:
            self.send(f"⛔ <b>#{rid} gated</b> — {reason}\n"
                      f"({p['product']} {p['tf']} {p['side']} {p['grade']}, {p['align']}% · still tracked as a ghost)")
            return
        if self.paused():
            return

        lines = [self.fmt_event(p), f"🎫 <b>#{rid}</b> — reply <code>took {rid} &lt;price&gt;</code> if you take it"]
        lines += ctx_lines
        if mline:
            lines.append(mline)
        lines += notes
        rl = self.review.line(review)
        if rl:
            lines.append(rl)
        self.send("\n".join(lines))

    def _lane_line(self, p):
        rows = self.ledger.q("SELECT r_net FROM setups WHERE tf=? AND grade=? AND status='CLOSED' "
                             "AND r_net IS NOT NULL ORDER BY closed DESC LIMIT 12", (p["tf"], p["grade"]))
        rs = [r[0] for r in rows]
        if not rs:
            return "no closed history for this TF+grade yet"
        wins = sum(1 for r in rs if r > 0.1)
        return f"{p['tf']} {p['grade']}-grades: last {len(rs)} closed → {wins} wins, {sum(rs):+.1f}R"

    # ---------------- outcome pipeline ----------------
    def on_outcome(self, p, fresh):
        res = self.ledger.on_event(p)
        if not fresh:
            return
        gated = bool(res and res[1])
        msg = self.fmt_event(p)
        if gated:
            msg = "👻 <i>(gated — ghost track)</i>\n" + msg
        self.send(msg)

    # ---------------- scheduled rhythms ----------------
    def tick(self):
        self.context.maybe_refresh()
        self.macro.maybe_refresh()
        for key, text in self.macro.due_prealerts(self._pre_sent):
            self._pre_sent.add(key)
            self.send(text)
        now = datetime.now(NY)
        today = now.strftime("%Y-%m-%d")
        if (now.hour > 20 or (now.hour == 20 and now.minute >= 30)) and self.ledger.meta_get("digest_day") != today:
            self.ledger.meta_set("digest_day", today)
            self.send(self._nightly())
        if now.weekday() == 6 and now.hour >= 18 and self.ledger.meta_get("week_card") != today:
            self.ledger.meta_set("week_card", today)
            if self.ledger.counts()[1]:
                learning.propose(self.ledger, self.base_cfg)
                self.send("🗞 <b>Weekly review</b>\n\n" + learning.scorecard(self.ledger) +
                          "\n\n" + learning.weights_text(self.ledger, self.base_cfg))

    def _nightly(self):
        rows = self.ledger.today_closed_rows()
        if rows:
            body = "\n".join(f"• #{r[0]} {r[1]} {r[2]} {r[3]} {r[4]}: {r[5]:+.1f}R" if r[5] is not None
                             else f"• #{r[0]} {r[1]} {r[2]} {r[3]} {r[4]}: {r[6]}" for r in rows)
        else:
            body = "• no setups resolved today"
        return (f"🌙 <b>Nightly debrief</b> — {self.ledger.today_r():+.1f}R today\n{body}\n\n"
                f"<b>Tomorrow:</b>\n{self.macro.digest()}")

    # ---------------- status / weights ----------------
    def status_text(self):
        rows = ["🤖 <b>Team status</b>" + (" ⏸ PAUSED" if self.paused() else "")]
        if self.live:
            for (product, tf), s in sorted(self.live.items()):
                rows.append(f"• {product} {tf}: {s['bias']} · {s['state']} · c={s['close']:.2f}")
        else:
            rows.append("• engines warming up…")
        if self.nq is not None:
            lv = self.nq.live
            rows.append(f"• NQ 1m: {lv['bias']} · {lv['state']} · c={lv['close']:.2f}" if lv["close"] is not None
                        else "• NQ 1m: warming up…")
        rows.append("")
        rows.append(self.risk.status_text())
        open_rows = self.ledger.open_rows()
        if open_rows:
            rows.append("\nOpen/pending:")
            for r in open_rows[:8]:
                rows.append(f"• #{r[0]} {r[1]} {r[2]} {r[3]} {r[4]} [{r[8]}] E{r[5]}")
        rows.append(f"\n🧠 review agent: {'ON (' + self.review.model + ')' if self.review.enabled() else 'off — set ANTHROPIC_API_KEY to enable'}")
        return "\n".join(rows)

    def apply_weights(self):
        w = self.ledger.apply_proposed()
        if not w:
            return "No proposal on file yet — the learning agent builds one after enough closed setups (see 'weights')."
        changed = 0
        for key, val in w.items():
            if key in self.base_cfg and isinstance(self.base_cfg[key], tuple):
                en, _ = self.base_cfg[key]
                self.base_cfg[key] = (en, float(val))
                changed += 1
        return f"⚖️ Applied tuned weights to {changed} layers — engines pick them up next cycle. 'weights' to view."
