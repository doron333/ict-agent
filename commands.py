"""Telegram command thread — the team answers when spoken to. Locked to your chat ID."""
import threading
import time

import requests

import learning_agent as learning

HELP = ("🤖 <b>Team commands</b> (just text the word):\n"
        "status — engines, bias, open setups, day R\n"
        "test — fire a sample of every alert type\n"
        "scorecard — full performance report\n"
        "context — market snapshot (funding, OI, F&G)\n"
        "events — upcoming macro calendar\n"
        "risk — guardrail state\n"
        "weights — layer weights + any tuning proposal\n"
        "apply weights — activate the proposal\n"
        "took <id> [price] — mark that you traded setup #id\n"
        "skip <id> — mark that you passed\n"
        "note <id> <text> — attach a note\n"
        "pause / resume — mute new setup alerts (tracking continues)\n"
        "help — this list")


def start(team, send, bot, chat):
    if not bot or not chat:
        print("commands: no bot token/chat — command thread disabled")
        return
    threading.Thread(target=_loop, args=(team, send, bot, chat), daemon=True).start()


def _loop(team, send, bot, chat):
    offset = int(team.ledger.meta_get("tg_offset") or 0)
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{bot}/getUpdates",
                             params={"offset": offset + 1, "timeout": 45}, timeout=55).json()
            if not r.get("ok"):
                time.sleep(20)
                continue
            for u in r.get("result", []):
                offset = u["update_id"]
                team.ledger.meta_set("tg_offset", str(offset))
                m = u.get("message") or {}
                if str(m.get("chat", {}).get("id")) != str(chat):
                    continue
                txt = (m.get("text") or "").strip()
                if not txt:
                    continue
                try:
                    reply = dispatch(team, txt)
                except Exception as e:
                    reply = f"⚠️ command error: {e}"
                if reply:
                    send(reply)
        except Exception:
            time.sleep(10)


def dispatch(team, txt):
    t = txt.lower().lstrip("/").strip()
    L = team.ledger
    if t in ("help", "start", "commands"):
        return HELP
    if t == "status":
        return team.status_text()
    if t == "test":
        return _test_sequence(team)
    if t == "scorecard":
        return learning.scorecard(L)
    if t == "context":
        return team.context.full_text()
    if t == "events":
        return team.macro.upcoming_text()
    if t == "risk":
        return team.risk.status_text()
    if t == "weights":
        return learning.weights_text(L, team.base_cfg)
    if t in ("apply weights", "applyweights"):
        return team.apply_weights()
    if t == "pause":
        L.meta_set("paused", "1")
        return "⏸ Paused — new setup alerts muted. Engines and ledger keep tracking. 'resume' to unmute."
    if t == "resume":
        L.meta_set("paused", "0")
        return "▶️ Resumed — setup alerts back on."
    parts = t.split()
    if parts and parts[0] in ("took", "skip", "skipped", "note") and len(parts) >= 2:
        try:
            rid = int(parts[1].lstrip("#"))
        except ValueError:
            return "Give me the setup number, e.g. <code>took 12 2461.5</code>"
        row = L.find(rid)
        if not row:
            return f"No setup #{rid} in the ledger."
        _, product, tf, side, grade, status, entry, *_ = row
        tag = f"#{rid} {product} {tf} {side} ({grade})"
        if parts[0] == "took":
            price = None
            if len(parts) >= 3:
                try:
                    price = float(parts[2])
                except ValueError:
                    pass
            L.set_took(rid, price)
            slip = f" · fill {price} vs paper {entry} ({(price - entry):+.2f})" if price else ""
            return f"✍️ Logged: you took {tag}{slip}"
        if parts[0] in ("skip", "skipped"):
            L.set_skip(rid)
            return f"✍️ Logged: passed on {tag}"
        note = txt.split(None, 2)[2] if len(txt.split(None, 2)) > 2 else ""
        if not note:
            return "Add the note text: <code>note 12 chased it late</code>"
        L.set_note(rid, note)
        return f"✍️ Note saved on {tag}"
    return "Didn't catch that — text <b>help</b> for the command list."


def _test_sequence(team):
    """Send one sample of every message type through the live pipeline. No ledger writes."""
    now = int(time.time())
    p = {"event": "setup", "product": "ETH-USD", "tf": "15m", "side": "long", "grade": "A",
         "align": 87, "entry": 2412.50, "stop": 2404.30, "target": 2465.80, "rr": 6.5,
         "bar_time": now, "sid": "test",
         "layers": "EMA\u2713 HTF\u2713 RSI\u2717 MAC\u2713 VOL\u2713 DSP\u2713 VWP\u2713 "
                   "SMT\u2713 ATR\u2713 EQ\u2713 KZ\u2717 RR\u2713 PD\u2713"}
    team.context.maybe_refresh()
    team.macro.maybe_refresh()
    _, mline = team.macro.check()
    team.send("🧪 <b>TEST</b> — one sample of every alert type. Live market context, fake trade. "
              "Sample tickets say #0; real ones get real numbers.")
    time.sleep(1.1)
    lines = [team.fmt_event(p), "🎫 <b>#0</b> (sample) — you'd reply <code>took 0 2412.8</code>"]
    lines += team.context.lines(p["product"], p["side"])
    if mline:
        lines.append(mline)
    lines.append("🛡 day +0.0R · 0 open")
    team.send("\n".join(lines))
    for ev, extra in (("filled", {}), ("tp1", {"result": "partial +1.0R banked"}),
                      ("be", {"result": "stop moved to break-even"}),
                      ("closed", {"result": "target +6.5R"})):
        time.sleep(1.1)
        team.send(team.fmt_event({**p, "event": ev, **extra}))
    time.sleep(1.1)
    team.send("⛔ <b>#0 gated</b> — 2-loss streak — only A grades until a winner resets it\n"
              "(ETH-USD 15m short B, 58% · still tracked as a ghost)")
    time.sleep(1.1)
    team.send("👻 <i>(gated — ghost track)</i>\n" +
              team.fmt_event({**p, "event": "filled", "side": "short", "grade": "B", "align": 58}))
    time.sleep(1.1)
    return ("🧪 <b>TEST DONE</b> — that was: setup ticket, filled, TP1, break-even, closed, "
            "risk gate, ghost. On schedule you'll also get 🗓 heads-ups, the 🌙 nightly debrief, "
            "and the 🗞 Sunday scorecard.")
