"""Ledger — the team's shared memory. Every setup, outcome, gate, and human action."""
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

SCHEMA = """
CREATE TABLE IF NOT EXISTS setups(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sid TEXT UNIQUE, created REAL, product TEXT, tf TEXT, side TEXT,
  grade TEXT, align REAL, entry REAL, stop REAL, target REAL, rr REAL,
  layers TEXT, context TEXT, macro TEXT, review TEXT,
  gated INTEGER DEFAULT 0, gate_reason TEXT DEFAULT '',
  status TEXT DEFAULT 'PENDING', tp1 INTEGER DEFAULT 0, be INTEGER DEFAULT 0,
  r_net REAL, result TEXT, closed REAL,
  took INTEGER DEFAULT 0, took_price REAL, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def ny_day_start(ts=None):
    d = datetime.fromtimestamp(ts or time.time(), NY)
    return datetime(d.year, d.month, d.day, tzinfo=NY).timestamp()


class Ledger:
    def __init__(self, data_dir, partial_r=1.0):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "ledger.db")
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.lock = threading.Lock()
        self.partial_r = partial_r

    # ---------- primitives ----------
    def _exec(self, q, a=()):
        with self.lock:
            cur = self.db.execute(q, a)
            self.db.commit()
            return cur

    def q(self, q, a=()):
        with self.lock:
            return self.db.execute(q, a).fetchall()

    def meta_get(self, k, default=None):
        r = self.q("SELECT v FROM meta WHERE k=?", (k,))
        return r[0][0] if r else default

    def meta_set(self, k, v):
        self._exec("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    def meta_json(self, k):
        v = self.meta_get(k)
        try:
            return json.loads(v) if v else None
        except Exception:
            return None

    # ---------- writes ----------
    def record_setup(self, p, context=None, macro=None, review=None, gated=False, gate_reason=""):
        with self.lock:
            cur = self.db.execute("SELECT id FROM setups WHERE sid=?", (p["sid"],)).fetchone()
            if cur:
                return cur[0]
            c = self.db.execute(
                """INSERT INTO setups(sid,created,product,tf,side,grade,align,entry,stop,target,rr,
                   layers,context,macro,review,gated,gate_reason,status)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'PENDING')""",
                (p["sid"], time.time(), p["product"], p["tf"], p["side"], p["grade"],
                 p["align"], p["entry"], p["stop"], p["target"], p["rr"], p.get("layers", ""),
                 json.dumps(context or {}), json.dumps(macro or {}), json.dumps(review or {}),
                 1 if gated else 0, gate_reason or ""))
            self.db.commit()
            return c.lastrowid

    def on_event(self, p):
        """Apply a lifecycle event (filled/tp1/be/cancelled/closed) to its setup row.
        Returns (row_id, gated, r_net_or_None) or None if sid unknown."""
        sid = p.get("sid", "")
        if not sid:
            return None
        row = self.q("SELECT id,tp1,rr,gated FROM setups WHERE sid=?", (sid,))
        if not row:
            return None
        rid, tp1, rr, gated = row[0]
        ev = p["event"]
        if ev == "filled":
            self._exec("UPDATE setups SET status='FILLED' WHERE id=?", (rid,))
            return rid, gated, None
        if ev == "tp1":
            self._exec("UPDATE setups SET tp1=1,status='TP1' WHERE id=?", (rid,))
            return rid, gated, None
        if ev == "be":
            self._exec("UPDATE setups SET be=1,status='BE' WHERE id=?", (rid,))
            return rid, gated, None
        if ev == "cancelled":
            self._exec("UPDATE setups SET status='CANCELLED',result=?,closed=? WHERE id=?",
                       (p.get("reason", ""), time.time(), rid))
            return rid, gated, None
        if ev == "closed":
            res = p.get("result", "")
            r = self._net_r(res, bool(tp1), rr or 0)
            self._exec("UPDATE setups SET status='CLOSED',result=?,r_net=?,closed=? WHERE id=?",
                       (res, r, time.time(), rid))
            return rid, gated, r
        return rid, gated, None

    def _net_r(self, res, tp1, rr):
        """Net R with half-off-at-TP1 accounting (matches engine's partial model)."""
        pr = self.partial_r
        if res.startswith("target"):
            return round(0.5 * pr + 0.5 * rr, 2) if tp1 else round(rr, 2)
        if res.startswith("break-even"):
            return round(0.5 * pr, 2) if tp1 else 0.0
        if res.startswith("stop"):
            return -1.0
        if res.startswith("runner"):
            return round(0.5 * pr - 0.5, 2)
        return None  # ambiguous bar — excluded from stats

    def set_took(self, rid, price=None):
        self._exec("UPDATE setups SET took=1,took_price=? WHERE id=?", (price, rid))

    def set_skip(self, rid):
        self._exec("UPDATE setups SET took=0 WHERE id=?", (rid,))

    def set_note(self, rid, text):
        self._exec("UPDATE setups SET note=? WHERE id=?", (text[:400], rid))

    # ---------- reads ----------
    def find(self, rid):
        r = self.q("SELECT id,product,tf,side,grade,status,entry,stop,target,r_net FROM setups WHERE id=?", (rid,))
        return r[0] if r else None

    def today_r(self):
        r = self.q("SELECT COALESCE(SUM(r_net),0) FROM setups WHERE gated=0 AND r_net IS NOT NULL AND closed>=?",
                   (ny_day_start(),))
        return round(r[0][0], 2)

    def today_n(self):
        r = self.q("SELECT COUNT(*) FROM setups WHERE gated=0 AND status IN ('CLOSED','CANCELLED') AND closed>=?",
                   (ny_day_start(),))
        return r[0][0]

    def today_closed_rows(self):
        return self.q("""SELECT id,product,tf,side,grade,r_net,result FROM setups
                         WHERE gated=0 AND status='CLOSED' AND closed>=? ORDER BY closed""", (ny_day_start(),))

    def open_count(self):
        r = self.q("SELECT COUNT(*) FROM setups WHERE gated=0 AND status IN ('FILLED','TP1','BE')")
        return r[0][0]

    def open_rows(self):
        return self.q("""SELECT id,product,tf,side,grade,entry,stop,target,status FROM setups
                         WHERE gated=0 AND status IN ('PENDING','FILLED','TP1','BE') ORDER BY created""")

    def open_same_side(self, product, side):
        r = self.q("""SELECT product||' '||tf FROM setups WHERE gated=0 AND side=? AND product!=?
                      AND status IN ('FILLED','TP1','BE') LIMIT 1""", (side, product))
        return r[0][0] if r else None

    def last_closed_r(self, n=2):
        r = self.q("SELECT r_net FROM setups WHERE gated=0 AND status='CLOSED' AND r_net IS NOT NULL "
                   "ORDER BY closed DESC LIMIT ?", (n,))
        return [x[0] for x in r]

    def closed_rows(self, days=None, include_gated=True):
        cond = "" if include_gated else " AND gated=0"
        args = []
        if days:
            cond += " AND closed>=?"
            args.append(time.time() - days * 86400)
        return self.q("SELECT tf,grade,side,product,layers,r_net,gated,took FROM setups "
                      "WHERE status='CLOSED' AND r_net IS NOT NULL" + cond, tuple(args))

    def counts(self):
        r = self.q("SELECT COUNT(*), SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END) FROM setups")
        return r[0][0], (r[0][1] or 0)

    # ---------- weights ----------
    def applied_weights(self):
        return (self.meta_json("weights_applied") or {}).get("weights")

    def proposed_weights(self):
        return self.meta_json("weights_proposed")

    def set_proposed(self, obj):
        self.meta_set("weights_proposed", json.dumps(obj))

    def apply_proposed(self):
        prop = self.proposed_weights()
        if not prop:
            return None
        self.meta_set("weights_applied", json.dumps({"weights": prop["weights"], "ts": time.time(),
                                                     "basis_n": prop.get("basis_n")}))
        return prop["weights"]
