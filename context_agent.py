"""Context agent — derivatives + sentiment snapshot. All sources free, all fail-open."""
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (ict-team)"}


class ContextAgent:
    def __init__(self, refresh_s=1800):
        self.refresh_s = refresh_s
        self.ts = 0.0
        self.snap = {}
        self.prev = {}

    def maybe_refresh(self, force=False):
        if not force and time.time() - self.ts < self.refresh_s:
            return
        s = {}
        # funding (OKX: fraction per 8h — clean units)
        for inst, key in (("ETH-USD-SWAP", "eth"), ("BTC-USD-SWAP", "btc")):
            try:
                d = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={inst}",
                                 headers=UA, timeout=12).json()
                s[f"{key}_funding"] = float(d["data"][0]["fundingRate"])
            except Exception:
                pass
        # open interest (Kraken Futures perps, coin units)
        try:
            d = requests.get("https://futures.kraken.com/derivatives/api/v3/tickers",
                             headers=UA, timeout=12).json()
            tk = {x.get("symbol"): x for x in d.get("tickers", [])}
            for sym, key in (("PF_ETHUSD", "eth"), ("PF_XBTUSD", "btc")):
                t = tk.get(sym)
                if t and t.get("openInterest") is not None:
                    s[f"{key}_oi"] = float(t["openInterest"])
        except Exception:
            pass
        # sentiment
        try:
            d = requests.get("https://api.alternative.me/fng/?limit=2", headers=UA, timeout=12).json()["data"]
            s["fng"] = int(d[0]["value"])
            s["fng_label"] = d[0]["value_classification"]
            s["fng_prev"] = int(d[1]["value"])
        except Exception:
            pass
        # market structure
        try:
            g = requests.get("https://api.coingecko.com/api/v3/global", headers=UA, timeout=12).json()["data"]
            s["btc_dom"] = round(g["market_cap_percentage"]["btc"], 1)
            s["mcap_24h"] = round(g["market_cap_change_percentage_24h_usd"], 1)
        except Exception:
            pass
        if s:
            self.prev = self.snap or s
            self.snap = s
            self.ts = time.time()

    # ---------- interpretation ----------
    def lines(self, product=None, side=None):
        """1-2 compact lines for alerts; side-aware crowding warnings."""
        s = self.snap
        if not s:
            return []
        bits = []
        if "fng" in s:
            tag = " ⚠ extreme" if s["fng"] >= 75 or s["fng"] <= 25 else ""
            bits.append(f"F&G {s['fng']} {s['fng_label']}{tag}")
        if "btc_dom" in s:
            bits.append(f"BTC.D {s['btc_dom']}%")
        key = "btc" if (product or "").startswith("BTC") else "eth"
        f = s.get(f"{key}_funding")
        if f is not None:
            pct = f * 100
            crowd = ""
            if pct >= 0.05:
                crowd = " (longs crowded)"
            elif pct <= -0.05:
                crowd = " (shorts crowded)"
            bits.append(f"{key.upper()} funding {pct:+.3f}%/8h{crowd}")
        oi = s.get(f"{key}_oi")
        poi = self.prev.get(f"{key}_oi")
        if oi:
            d = f" ({(oi / poi - 1) * 100:+.1f}%)" if poi else ""
            bits.append(f"OI {oi:,.0f}{d}")
        out = ["📡 " + " · ".join(bits)] if bits else []
        w = self._conflict(side, s.get(f"{key}_funding"), s.get("fng"))
        if w:
            out.append(w)
        return out

    @staticmethod
    def _conflict(side, funding, fng):
        if side and funding is not None:
            pct = funding * 100
            if side == "long" and pct >= 0.05:
                return "⚠️ longs paying rich funding — crowded side, squeeze risk against you"
            if side == "short" and pct <= -0.05:
                return "⚠️ shorts paying rich funding — crowded side, squeeze risk against you"
        if side and fng is not None:
            if side == "long" and fng >= 80:
                return "⚠️ sentiment at extreme greed — late-cycle long risk"
            if side == "short" and fng <= 20:
                return "⚠️ sentiment at extreme fear — capitulation shorts get squeezed"
        return ""

    def full_text(self):
        s = self.snap
        if not s:
            return "No context data yet."
        age = int((time.time() - self.ts) / 60)
        rows = [f"Market context (as of {age}m ago):"]
        if "fng" in s:
            rows.append(f"• Fear & Greed: {s['fng']} ({s['fng_label']}), yday {s.get('fng_prev', '?')}")
        if "btc_dom" in s:
            rows.append(f"• BTC dominance: {s['btc_dom']}% · total mcap 24h {s.get('mcap_24h', '?')}%")
        for k in ("eth", "btc"):
            f, oi = s.get(f"{k}_funding"), s.get(f"{k}_oi")
            if f is not None or oi:
                fs = f"{f * 100:+.3f}%/8h" if f is not None else "?"
                os_ = f"{oi:,.0f}" if oi else "?"
                rows.append(f"• {k.upper()}: funding {fs} · OI {os_}")
        return "\n".join(rows)
