"""Learning agent — turns the ledger into judgment: scorecards, layer lift, weight proposals."""
import os
import time

TAG2KEY = {"EMA": "L_trend", "HTF": "L_bias", "RSI": "L_rsi", "MAC": "L_macd",
           "VOL": "L_vol", "DSP": "L_dsp", "VWP": "L_vwap", "SMT": "L_smt",
           "ATR": "L_atr", "EQ": "L_eq", "KZ": "L_kz", "RR": "L_rr", "PD": "L_pd"}
ADAPT_MIN = int(os.environ.get("ADAPT_MIN", "40"))


def _parse_layers(s):
    """'EMA✓ HTF✗ ...' -> {tag: bool}"""
    out = {}
    for tok in (s or "").split():
        if len(tok) >= 2 and tok[:-1] in TAG2KEY:
            out[tok[:-1]] = tok.endswith("\u2713")
    return out


def _agg(rows):
    n = len(rows)
    if not n:
        return "n=0"
    rs = [r for r in rows if r is not None]
    wins = sum(1 for r in rs if r > 0.1)
    tot = sum(rs)
    return f"n={n} · {wins}/{len(rs)} wins ({wins / len(rs) * 100:.0f}%) · {tot:+.1f}R · avg {tot / len(rs):+.2f}R" \
        if rs else f"n={n}"


def layer_lift(closed):
    """closed rows: (tf,grade,side,product,layers,r_net,gated,took) -> {tag:(lift,n_p,n_a)}"""
    out = {}
    for tag in TAG2KEY:
        p, a = [], []
        for row in closed:
            layers, r = row[4], row[5]
            if r is None:
                continue
            d = _parse_layers(layers)
            if tag not in d:
                continue
            (p if d[tag] else a).append(r)
        if len(p) >= 5 and len(a) >= 5:
            out[tag] = (sum(p) / len(p) - sum(a) / len(a), len(p), len(a))
    return out


def scorecard(L):
    total, closed_n = L.counts()
    closed = L.closed_rows(include_gated=False)
    week = L.closed_rows(days=7, include_gated=False)
    rows = ["📊 <b>Scorecard</b> (forward paper record — no backtest mixed in)",
            f"All-time: {_agg([r[5] for r in closed])}",
            f"Last 7d:  {_agg([r[5] for r in week])}",
            f"Tracked setups: {total} · resolved: {closed_n}"]
    for label, idx in (("By grade", 1), ("By TF", 0)):
        buckets = {}
        for r in closed:
            buckets.setdefault(r[idx], []).append(r[5])
        if buckets:
            rows.append(f"\n{label}:")
            for k in sorted(buckets, key=lambda x: -(sum(v for v in buckets[x] if v) or 0)):
                rows.append(f"  {k}: {_agg(buckets[k])}")
    lifts = layer_lift(L.closed_rows(include_gated=True))
    if lifts:
        rows.append("\nLayer lift (avg R when present vs absent):")
        for tag, (lift, np_, na) in sorted(lifts.items(), key=lambda x: -x[1][0]):
            arrow = "▲" if lift > 0 else "▼"
            rows.append(f"  {arrow} {tag}: {lift:+.2f}R  ({np_}✓/{na}✗)")
    took = [r[5] for r in closed if r[7]]
    passed = [r[5] for r in closed if not r[7]]
    if took:
        rows.append(f"\nDiscipline: taken {_agg(took)}")
        rows.append(f"           passed {_agg(passed)}")
    gated = [r[5] for r in L.closed_rows(include_gated=True) if r[6]]
    if gated:
        s = sum(r for r in gated if r is not None)
        verdict = f"saved {-s:+.1f}R" if s < 0 else f"cost {s:+.1f}R"
        rows.append(f"Risk gates: {len(gated)} blocked → {verdict}")
    if closed_n < ADAPT_MIN:
        rows.append(f"\nWeight adaptation unlocks at {ADAPT_MIN} closed ({closed_n}/{ADAPT_MIN}).")
    return "\n".join(rows)


def propose(L, base_cfg):
    """Build a weight proposal from layer lift. Returns proposal dict or None."""
    closed = L.closed_rows(include_gated=True)
    n = len([r for r in closed if r[5] is not None])
    if n < ADAPT_MIN:
        return None
    lifts = layer_lift(closed)
    if not lifts:
        return None
    weights = {}
    for tag, key in TAG2KEY.items():
        en, w = base_cfg.get(key, (False, 0.0))
        if not en or key in ("L_kz", "L_vwap") and not w:
            continue
        if tag in lifts:
            lift = lifts[tag][0]
            factor = 1 + max(-0.4, min(0.4, lift / 2.0))
            weights[key] = round(max(0.25, min(3.0, w * factor)), 2)
        else:
            weights[key] = w
    prop = {"weights": weights, "basis_n": n, "ts": time.time()}
    L.set_proposed(prop)
    return prop


def weights_text(L, base_cfg):
    rows = ["⚖️ Layer weights (current):"]
    applied = L.applied_weights() or {}
    for tag, key in TAG2KEY.items():
        en, w = base_cfg.get(key, (False, 0.0))
        if not en:
            continue
        mark = " ←tuned" if key in applied else ""
        rows.append(f"  {tag}: {w:g}{mark}")
    prop = L.proposed_weights()
    if prop:
        rows.append(f"\nProposal on file (from {prop['basis_n']} closed):")
        for key, w in prop["weights"].items():
            cur = base_cfg.get(key, (False, 0))[1]
            if abs(w - cur) > 0.01:
                tag = next((t for t, k in TAG2KEY.items() if k == key), key)
                rows.append(f"  {tag}: {cur:g} → {w:g}")
        rows.append("Reply <b>apply weights</b> to activate.")
    else:
        _, closed_n = L.counts()
        rows.append(f"\nNo proposal yet — adaptation unlocks at {ADAPT_MIN} closed ({closed_n} so far).")
    return "\n".join(rows)
