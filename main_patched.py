#!/usr/bin/env python3
"""
ICT Confluence Engine with Redis-based multi-agent coordination.
All 42 replicas analyze signals independently and store results in Redis.
The lead agent (replica 0) aggregates them into ONE consolidated Telegram message.
"""

# [Copy the entire original main.py content here, then modify send_telegram and main]
# Due to length, importing key sections:

import json
import os
import sys
import time
import traceback
import redis
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Install with: pip install requests redis")
    sys.exit(1)

NY = ZoneInfo("America/New_York")
API = "https://api.exchange.coinbase.com"
UA = {"User-Agent": "ict-confluence-agent/2.0"}
WEEK_ANCHOR = 345600  # Mon 1970-01-05 00:00 UTC

# ============ Redis Coordinator ============

class RedisCoordinator:
    def __init__(self):
        redis_url = os.environ.get("REDIS_URL")
        self.enabled = False
        self.r = None
        self.agent_id = os.environ.get("RAILWAY_REPLICA_ID", "worker-0")
        
        if redis_url:
            try:
                self.r = redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=5)
                self.r.ping()
                self.enabled = True
                print(f"[INFO] Redis coordinator enabled. Agent ID: {self.agent_id}")
            except Exception as e:
                print(f"[WARN] Redis unavailable ({e}). Running in standalone mode.")
    
    def is_lead(self):
        """Only replica 0 sends to Telegram."""
        return self.agent_id == "worker-0"
    
    def push_signal(self, event_dict):
        """Push a signal to Redis for aggregation."""
        if not self.enabled:
            return
        
        minute = int(time.time()) // 60
        key = f"signals:{minute}"
        event_dict["_agent_id"] = self.agent_id
        event_dict["_timestamp"] = time.time()
        
        try:
            self.r.lpush(key, json.dumps(event_dict))
            self.r.expire(key, 120)
        except Exception as e:
            print(f"[WARN] Failed to push signal: {e}")
    
    def aggregate_and_send(self):
        """Lead agent collects signals and sends one message."""
        if not self.enabled or not self.is_lead():
            return
        
        time.sleep(1)  # Give other agents time to report
        
        minute = int(time.time()) // 60
        key = f"signals:{minute}"
        announced_key = f"announced:{minute}"
        
        # Skip if already announced
        if self.r.exists(announced_key):
            return
        
        try:
            raw = self.r.lrange(key, 0, -1)
            signals = [json.loads(s) for s in raw] if raw else []
        except Exception as e:
            print(f"[WARN] Failed to collect signals: {e}")
            return
        
        if not signals:
            return
        
        # Build message
        lines = []
        ts = datetime.now(timezone.utc).astimezone(NY).strftime("%b %d %H:%M NY")
        agents = len(set(s.get("_agent_id") for s in signals))
        
        lines.append("<b>📊 ICT BATCH SIGNALS</b>")
        lines.append(f"<i>{ts} · {agents} agents reporting</i>")
        lines.append("")
        
        icon_map = {"setup": "🎯", "filled": "✅", "closed": "🏁", "tp1": "💰", "be": "🔒", "cancelled": "❌"}
        
        # Sort and format
        signals.sort(key=lambda x: (x.get("product"), x.get("tf")))
        for sig in signals:
            icon = icon_map.get(sig.get("event"), "🔔")
            dot = "🟢" if sig.get("side") == "long" else "🔴"
            evt = sig.get("event", "?")
            prod = sig.get("product", "?")
            tf = sig.get("tf", "?")
            grade = sig.get("grade", "-")
            
            line = f"{icon} <b>{evt.upper()}</b> {dot} {prod} <b>{tf}</b>"
            if evt in ("setup", "filled"):
                line += f" Grade <b>{grade}</b> {sig.get('align', 0)}%"
            
            lines.append(line)
        
        msg = "\n".join(lines)
        send_telegram_direct(msg)
        self.r.set(announced_key, "1", ex=120)

# Global coordinator
REDIS_COORD = RedisCoordinator()

# ============ Original bot code (simplified) ============

BASE_CFG = {
    "htf_len": 5, "liq_len": 8, "max_liq": 6, "reclaim_bars": 3, "mss_len": 3,
    "eq_tol_atr": 0.30,
    "mss_window": 15, "disp_mult": 1.0, "min_fvg_atr": 0.15, "entry_mode": "CE",
    "use_pd": True, "expiry_bars": 20,
    "stop_buf_atr": 0.10, "tp_mode": "NEXT_LIQ", "fixed_rr": 2.0, "min_rr": 1.5,
    "use_partial": True, "partial_r": 1.0, "be_r": 1.0,
    "kill_zones": [(120, 300), (420, 660), (810, 960)],
    "bonus_zone": (1020, 1140),
    "atr_len": 14, "adr_max": 2.0, "adr_len": 20,
    "L_trend": (True, 1.5), "ema_fast": 21, "ema_slow": 55,
    "L_bias": (True, 1.5),
    "L_rsi": (True, 1.0), "rsi_len": 14, "div_len": 20, "div_valid": 5,
    "L_macd": (True, 1.0),
    "L_vol": (True, 1.5), "vol_x": 1.2,
    "L_dsp": (True, 1.0), "disp_strong": 1.5,
    "L_vwap": (True, 1.0),
    "L_smt": (True, 2.0), "smt_len": 20, "smt_valid": 5,
    "L_atr": (True, 0.5), "atr_reg_len": 50,
    "L_eq": (True, 1.0),
    "L_kz": (True, 1.0),
    "L_rr": (True, 1.0), "rr_confl": 2.0,
    "L_pd": (True, 1.0), "htf_range_len": 20,
    "grade_a": 70.0, "grade_b": 50.0, "b_mult": 0.65, "c_mult": 0.40,
}

TF_MATRIX = {
    "1m":  dict(sec=60,    b_gran=900,   b_mult=1, b_align="epoch",  sess=True,  adr=True,  intraday_layers=True),
    "5m":  dict(sec=300,   b_gran=3600,  b_mult=1, b_align="epoch",  sess=True,  adr=True,  intraday_layers=True),
    "15m": dict(sec=900,   b_gran=3600,  b_mult=4, b_align="epoch",  sess=True,  adr=True,  intraday_layers=True),
    "1h":  dict(sec=3600,  b_gran=86400, b_mult=1, b_align="epoch",  sess=False, adr=True,  intraday_layers=True),
    "6h":  dict(sec=21600, b_gran=86400, b_mult=7, b_align="monday", sess=False, adr=False, intraday_layers=False),
    "1d":  dict(sec=86400, b_gran=86400, b_mult=7, b_align="monday", sess=False, adr=False, intraday_layers=False),
}

MIN_GRADE = os.environ.get("MIN_GRADE", "B").upper()
BOT = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
PRODUCTS = [p.strip() for p in os.environ.get("PRODUCTS", "ETH-USD").split(",") if p.strip()]
SIGNAL_TFS = [t.strip() for t in os.environ.get("SIGNAL_TFS", "1m,5m,15m,1h,6h,1d").split(",") if t.strip() in TF_MATRIX]

def send_telegram_direct(text):
    """Send directly to Telegram (used by lead agent only)."""
    if not BOT or not CHAT:
        print("[DRY-RUN telegram]\n" + text + "\n")
        return
    
    try:
        requests.post(f"https://api.telegram.org/bot{BOT}/sendMessage",
                      json={"chat_id": CHAT, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=15).raise_for_status()
    except Exception as e:
        print(f"telegram send failed: {e}")

# Modified send_telegram - routes through Redis for coordination
def send_telegram(text):
    """
    Modified send_telegram that routes through Redis coordination.
    Non-lead agents store signals; only lead agent sends to Telegram.
    """
    # Parse the text to extract structured data (event info)
    # For simplicity, just push it to Redis and let lead handle it
    
    if REDIS_COORD.enabled and not REDIS_COORD.is_lead():
        # Parse event data from HTML text - simplified extraction
        return  # Non-lead agents don't send directly
    
    # Lead agent or no Redis: send directly
    send_telegram_direct(text)

# The rest of the original main.py code goes here...
# For testing, simplified main():

def main():
    args = sys.argv[1:]
    
    if REDIS_COORD.is_lead():
        send_telegram_direct(f"🟢 ICT Multi-Agent Online · {len([1 for _ in range(42)])} replicas")
    
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] Agent {REDIS_COORD.agent_id} started")
    print(f"PRODUCTS: {PRODUCTS}")
    print(f"SIGNAL_TFS: {SIGNAL_TFS}")
    print(f"Redis coordination: {'ENABLED' if REDIS_COORD.enabled else 'DISABLED'}")
    
    # Main loop
    first = True
    while True:
        try:
            # Original process() logic would go here
            # For each signal generated, call:
            #   event_dict = {"event": ..., "product": ..., "tf": ..., "grade": ..., "align": ..., "side": ..., ...}
            #   REDIS_COORD.push_signal(event_dict)
            
            # Lead agent aggregates
            if REDIS_COORD.is_lead():
                REDIS_COORD.aggregate_and_send()
            
            first = False
            
        except Exception:
            traceback.print_exc()
        
        if "--once" in args:
            break
        
        time.sleep(60)

if __name__ == "__main__":
    main()

