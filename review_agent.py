"""Review agent — an LLM second opinion on each qualifying setup. Optional; fails open."""
import os

import requests

RANK = {"A": 3, "B": 2, "C": 1}


class ReviewAgent:
    def __init__(self):
        self.key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
        self.min_grade = os.environ.get("REVIEW_MIN_GRADE", "B").upper()

    def enabled(self):
        return bool(self.key)

    def judge(self, p, ctx_lines, macro_line, lane_line):
        if not self.key or RANK.get(p["grade"], 0) < RANK.get(self.min_grade, 2):
            return None
        prompt = (
            f"Setup: {p['product']} {p['tf']} {p['side'].upper()} · grade {p['grade']} ({p['align']}% aligned)\n"
            f"Entry {p['entry']}  Stop {p['stop']}  Target {p['target']}  ({p['rr']}R)\n"
            f"Layers: {p.get('layers', '')}\n"
            f"Market: {' | '.join(ctx_lines) if ctx_lines else 'n/a'}\n"
            f"Macro: {macro_line or 'clear'}\n"
            f"Lane history: {lane_line}\n\n"
            "You are a strict reviewer for an ICT liquidity-sweep system (sweep→MSS→FVG limit entries). "
            "Judge internal coherence of the layers, conflicts between direction and funding/sentiment "
            "crowding, event risk, and whether this lane has been earning. Be skeptical; a mediocre "
            "setup deserves SKIP. Reply in EXACTLY this format:\n"
            "VERDICT: TAKE|CAUTION|SKIP\nWHY: <max 22 words>")
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": self.key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": self.model, "max_tokens": 120,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=25)
            txt = "".join(b.get("text", "") for b in r.json().get("content", []))
            up = txt.upper()
            v = ("TAKE" if "VERDICT: TAKE" in up else
                 "SKIP" if "VERDICT: SKIP" in up else
                 "CAUTION" if "CAUTION" in up else None)
            why = txt.split("WHY:", 1)[1].strip().splitlines()[0] if "WHY:" in txt else ""
            if not v:
                return None
            return {"verdict": v, "why": why[:160]}
        except Exception:
            return None

    @staticmethod
    def line(review):
        if not review:
            return ""
        icon = {"TAKE": "🧠✅", "CAUTION": "🧠⚠️", "SKIP": "🧠⛔"}.get(review["verdict"], "🧠")
        return f"{icon} Review: <b>{review['verdict']}</b> — {review['why']}"
