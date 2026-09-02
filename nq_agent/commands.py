"""`nq ...` Telegram commands. Dispatched from the team's commands.py.

Usage (text, with or without a leading slash):
  nq status · nq levels · nq bias · nq pause · nq resume · nq stats [days] · nq help
"""

HELP = ("📟 <b>NQ commands</b>:\n"
        "nq status — feed, last bar, window, tracker state\n"
        "nq levels — PDH/PDL, Asia, London, OR for the current trade date\n"
        "nq bias — higher-timeframe structure read\n"
        "nq pause / nq resume — mute NQ setup alerts (tracking continues)\n"
        "nq stats [days] — expectancy from the ledger (default 30d)\n"
        "nq help — this list")


def is_nq(txt):
    t = txt.lower().lstrip("/").strip()
    return t == "nq" or t.startswith("nq ") or t.startswith("nq_")


def dispatch(agent, txt):
    t = txt.lower().lstrip("/").strip().replace("nq_", "nq ", 1)
    parts = t.split()
    sub = parts[1] if len(parts) > 1 else "status"
    arg = parts[2] if len(parts) > 2 else None
    if agent is None:
        return "NQ agent is not running in this worker (set NQ_ENABLED=1 or start `python main.py --instrument nq`)."
    if sub in ("help", "?"):
        return HELP
    if sub == "status":
        return agent.status_text()
    if sub == "levels":
        return agent.levels_text()
    if sub == "bias":
        return agent.bias_text()
    if sub == "pause":
        agent.L.set_paused(True)
        return "⏸ NQ paused — setup alerts muted. Tracker and ledger keep running. 'nq resume' to unmute."
    if sub == "resume":
        agent.L.set_paused(False)
        return "▶️ NQ resumed — setup alerts back on."
    if sub == "stats":
        days = 30
        if arg:
            try:
                days = max(1, int(arg))
            except ValueError:
                return "Give me a number of days, e.g. <code>nq stats 14</code>"
        return agent.stats_text(days)
    return "Unknown NQ command — text <b>nq help</b>."
