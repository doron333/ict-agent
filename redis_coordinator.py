"""
Redis-based multi-agent coordinator.
Intercepts Telegram sends from all replicas and aggregates them.
"""
import json
import os
import time
import redis
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

class SignalAggregator:
    def __init__(self):
        self.redis_url = os.environ.get("REDIS_URL")
        self.agent_id = os.environ.get("RAILWAY_REPLICA_ID", "worker-0")
        self.r = None
        
        if self.redis_url:
            try:
                self.r = redis.from_url(self.redis_url, decode_responses=True, socket_connect_timeout=3)
                self.r.ping()
                print(f"[COORD] Agent {self.agent_id} connected to Redis")
            except Exception as e:
                print(f"[COORD] Redis connection failed: {e}")
    
    def is_lead(self):
        return self.agent_id == "worker-0"
    
    def queue_signal(self, signal_json):
        """Store a signal from this agent."""
        if not self.r:
            return
        
        try:
            minute_key = int(time.time()) // 60
            self.r.lpush(f"batch:{minute_key}", signal_json)
            self.r.expire(f"batch:{minute_key}", 180)
        except:
            pass
    
    def collect_batch(self):
        """Lead agent collects all signals."""
        if not self.r or not self.is_lead():
            return None
        
        time.sleep(1)
        minute_key = int(time.time()) // 60
        announced_key = f"announced:{minute_key}"
        
        if self.r.exists(announced_key):
            return None
        
        try:
            raw = self.r.lrange(f"batch:{minute_key}", 0, -1)
            if not raw:
                return None
            
            signals = []
            for item in raw:
                try:
                    signals.append(json.loads(item))
                except:
                    pass
            
            self.r.set(announced_key, "1", ex=180)
            return self._build_message(signals)
        except:
            return None
    
    def _build_message(self, signals):
        if not signals:
            return None
        
        lines = []
        ts = datetime.now(timezone.utc).astimezone(NY).strftime("%b %d %H:%M NY")
        agents_reporting = len(set(s.get("_agent_id", "?") for s in signals))
        
        lines.append("<b>📊 ICT SIGNAL BATCH</b>")
        lines.append(f"<i>{ts}</i>")
        lines.append(f"<i>🤖 {agents_reporting} agents reporting</i>")
        lines.append("")
        
        icon_map = {"setup": "🎯", "filled": "✅", "closed": "🏁", "tp1": "💰", "be": "🔒", "cancelled": "❌"}
        
        signals.sort(key=lambda x: (x.get("product", ""), x.get("tf", "")))
        
        for sig in signals:
            evt = sig.get("event", "?")
            icon = icon_map.get(evt, "🔔")
            dot = "🟢" if sig.get("side") == "long" else "🔴"
            prod = sig.get("product", "?")
            tf = sig.get("tf", "?")
            grade = sig.get("grade", "-")
            align = sig.get("align", "?")
            
            line = f"{icon} <b>{evt}</b> {dot} {prod} {tf}"
            if evt in ("setup", "filled"):
                line += f" · Grade <b>{grade}</b> · {align}%"
            
            lines.append(line)
        
        return "\n".join(lines)

aggregator = SignalAggregator()
