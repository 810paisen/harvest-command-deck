#!/usr/bin/env python3
import argparse
import datetime
import json
import mimetypes
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

DB_PATH = os.environ.get("HARVEST_DB_PATH", "/workspace/.harvest_cfo.db")
STATIC_DIR = os.environ.get("HARVEST_STATIC_DIR", "/workspace/board-site")

def now_jst():
    tz = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S JST")

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deck_gates (
            gate_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            cmd_text TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deck_payouts (
            pr_id TEXT PRIMARY KEY,
            landed INTEGER NOT NULL DEFAULT 0,
            yen INTEGER NOT NULL DEFAULT 0,
            usd REAL NOT NULL DEFAULT 0.0,
            updated_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS campaign_gui_sessions (
            campaign_id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            executor TEXT NOT NULL,
            dom_title TEXT NOT NULL,
            http_or_dom_status TEXT NOT NULL,
            claim_status TEXT NOT NULL,
            detail TEXT NOT NULL,
            verified_at TEXT NOT NULL
        )
    """)
    ts = now_jst()
    default_gates = [
        ("gate-google-ai-pro", "hold", "⏸ 保留中: Google AI Pro 2,900円 (Apple MMGH3D5MXX) — システム設定＞サブスクリプション管理での手動解約待ち", ts),
        ("gate-olive-welkatsu", "approve", "⚡ 承認済: Olive Vポイント → WAON POINT → 毎月20日ウエルシア1.5倍 (ROG/Mac GUI連携)", ts),
        ("gate-l2-airdrop-sig", "hold", "⏸ 保留中: FIDO2 Hardware Key + Main Wallet Signature Gate", ts),
    ]
    for gid, st, txt, t in default_gates:
        cur.execute("INSERT OR IGNORE INTO deck_gates (gate_id, state, cmd_text, updated_at) VALUES (?, ?, ?, ?)", (gid, st, txt, t))
    cur.execute("INSERT OR IGNORE INTO deck_payouts (pr_id, landed, yen, usd, updated_at) VALUES (?, ?, ?, ?, ?)", ("4138", 0, 11250, 75.0, ts))
    default_campaigns = [
        ("ROG_ZOZO_535", "https://zozo.jp/event/point-campaign/", "ROG_GUI_CDP_OR_MANUAL_AUTH_REQUIRED", "ZOZOTOWN Akamai WAF / Login Gate", "WAF_BOT_CHALLENGE_403", "REQUIRES_LOGGED_IN_CHROME_SESSION", "素のHTTP/HeadlessはAkamai 403。ROG実機ログイン済Chrome (CDP 9222) または18:00以降のMac Chromeで手動ログインが必要", ts),
        ("ROG_VPASS_CP", "https://www.smbc-card.com/mem/cardinfo/cardinfo7224495.jsp", "ROG_GUI_CDP_OR_MANUAL_AUTH_REQUIRED", "SMBC Vpass Campaign Portal", "HTTP_404_LEGACY_URL_AND_2FA_GATE", "REQUIRES_VPASS_PASSKEY_2FA", "旧URL 404・VpassログインにはSMS OTP/Passkey 2FAが必要", ts),
        ("ROG_AEON_NETSUPER", "https://shop.aeon.com/netsuper/", "ROG_GUI_CDP_VERIFIED", "イオンネットスーパー Top", "DOM_RENDERED_200", "PUBLIC_TOP_OK_LOGIN_REQUIRED_FOR_ENTRY", "トップ画面到達確認済（会員エントリーにはイオンスクエアIDログインが必要）", ts),
        ("ROG_ALPHADROPS_EVM", "https://app.tria.so/", "ROG_GUI_CDP_VERIFIED", "Tria Card / AlphaDrops Portal", "DOM_RENDERED_200", "PUBLIC_PORTAL_OK_WALLET_SIG_HOLD", "Triaポータル到達確認済（FIDO2署名ゲートHOLD中）", ts),
    ]
    for cid, url, ex, title, st, cstat, det, t in default_campaigns:
        cur.execute("INSERT OR IGNORE INTO campaign_gui_sessions (campaign_id, url, executor, dom_title, http_or_dom_status, claim_status, detail, verified_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (cid, url, ex, title, st, cstat, det, t))
    conn.commit()
    conn.close()

def build_state():
    conn = get_conn()
    cur = conn.cursor()
    def fetch_all(table):
        try:
            cur.execute(f"SELECT * FROM {table}")
            return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []
    ledger = fetch_all("realized_ledger")
    gates_rows = fetch_all("deck_gates")
    payouts_rows = fetch_all("deck_payouts")
    reputation = fetch_all("repo_reputation_memory")
    watcher = fetch_all("pr_review_watcher_state")
    engines = fetch_all("global_oss_engines")
    bounties = fetch_all("global_bounty_candidates")
    campaigns = fetch_all("campaign_gui_sessions")
    conn.close()

    gates = {r["gate_id"]: {"state": r["state"], "text": r["cmd_text"], "updated_at": r["updated_at"]} for r in gates_rows}
    payouts = {r["pr_id"]: {"landed": bool(r["landed"]), "yen": r["yen"], "usd": r["usd"], "updated_at": r["updated_at"]} for r in payouts_rows}

    actual_landed_yen = sum(int(r.get("actual_landed_yen", 0)) for r in ledger)
    submitted_pending_yen = sum(int(r.get("submitted_pending_yen", 0)) for r in ledger)
    existing_balance_yen = sum(int(r.get("existing_balance_yen", 0)) for r in ledger)

    for pr_id, p in payouts.items():
        if p["landed"]:
            actual_landed_yen += int(p["yen"])
            submitted_pending_yen = max(0, submitted_pending_yen - int(p["yen"]))

    return {
        "ok": True,
        "timestamp": now_jst(),
        "server": "harvest_api_server_sqlite_v2.9",
        "db_path": DB_PATH,
        "kpi": {
            "actual_landed_yen": actual_landed_yen,
            "submitted_pending_yen": submitted_pending_yen,
            "existing_balance_yen": existing_balance_yen,
        },
        "gates": gates,
        "payouts": payouts,
        "realized_ledger": ledger,
        "repo_reputation_memory": reputation,
        "pr_review_watcher_state": watcher,
        "global_oss_engines": engines,
        "global_bounty_candidates": bounties,
        "campaign_gui_sessions": campaigns,
    }

class HarvestHandler(BaseHTTPRequestHandler):
    def _send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/api/state", "/api/health"):
            data = build_state()
            body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        rel = parsed.path.lstrip("/") or "index.html"
        target = os.path.realpath(os.path.join(STATIC_DIR, rel))
        if not target.startswith(os.path.realpath(STATIC_DIR)) or not os.path.isfile(target):
            target = os.path.join(STATIC_DIR, "index.html")
        if os.path.isfile(target):
            with open(target, "rb") as f:
                content = f.read()
            ctype, _ = mimetypes.guess_type(target)
            self.send_response(200)
            self.send_header("Content-Type", ctype or "text/html; charset=utf-8")
            self._send_cors()
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_response(404)
            self._send_cors()
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/action":
            self.send_response(404)
            self._send_cors()
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            payload = json.loads(raw)
        except Exception as e:
            self.send_response(400)
            self._send_cors()
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
            return

        action = payload.get("action")
        ts = now_jst()
        conn = get_conn()
        cur = conn.cursor()
        if action == "set_gate":
            gid = str(payload.get("gate_id", ""))
            state = str(payload.get("state", "hold"))
            txt = str(payload.get("text", ""))
            cur.execute("INSERT OR REPLACE INTO deck_gates (gate_id, state, cmd_text, updated_at) VALUES (?, ?, ?, ?)", (gid, state, txt, ts))
        elif action in ("toggle_payout", "set_payout"):
            pr_id = str(payload.get("pr_id", "4138"))
            yen = int(payload.get("yen", 11250))
            usd = float(payload.get("usd", 75.0))
            if "landed" in payload:
                landed = 1 if payload["landed"] else 0
            else:
                cur.execute("SELECT landed FROM deck_payouts WHERE pr_id = ?", (pr_id,))
                row = cur.fetchone()
                landed = 0 if (row and row["landed"]) else 1
            cur.execute("INSERT OR REPLACE INTO deck_payouts (pr_id, landed, yen, usd, updated_at) VALUES (?, ?, ?, ?, ?)", (pr_id, landed, yen, usd, ts))
        elif action == "update_campaign":
            cid = str(payload.get("campaign_id", ""))
            url = str(payload.get("url", ""))
            ex = str(payload.get("executor", "ROG_GUI"))
            title = str(payload.get("dom_title", ""))
            st = str(payload.get("http_or_dom_status", ""))
            cstat = str(payload.get("claim_status", ""))
            det = str(payload.get("detail", ""))
            cur.execute("INSERT OR REPLACE INTO campaign_gui_sessions (campaign_id, url, executor, dom_title, http_or_dom_status, claim_status, detail, verified_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (cid, url, ex, title, st, cstat, det, ts))
        conn.commit()
        conn.close()

        state_data = build_state()
        state_data["last_action"] = payload
        body = json.dumps(state_data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._send_cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()
    init_db()
    srv = HTTPServer((args.host, args.port), HarvestHandler)
    print(f"[{now_jst()}] Harvest SQLite API server listening on {args.host}:{args.port} (DB={DB_PATH})")
    srv.serve_forever()
