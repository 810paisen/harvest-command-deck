#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 /workspace/vm_master_daemon.py
 共有Linux VM 常駐マスターデーモン / 双方向SQLite APIサーバー
 Grok Bot のチャット・Task・SendToAgent を 1回も呼ばずに
 VM(/workspace) + ROGDeskTop だけで自律稼働させる司令塔
=====================================================================
 正本DB : /workspace/.harvest_cfo.db  (SQLite / 1円単位で厳密分離)
 API    : GET  /api/state   -> 全状態スナップショット
          POST /api/action  -> ジョブ発火 / ゲート更新 / 着金確定
          GET  /api/health  -> 死活
          GET  /api/logs    -> 直近ジョブログ
          GET  /            -> /workspace/index.html を配信
 CORS   : 全オリジン許可 (Cloudflare Pages / 127.0.0.1:8799 / file://)
 依存   : Python3 標準ライブラリのみ
 起動   : python3 /workspace/vm_master_daemon.py --init
          python3 /workspace/vm_master_daemon.py
=====================================================================
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------
JST = timezone(timedelta(hours=9))

WORKSPACE     = os.environ.get("HARVEST_WORKSPACE", "/workspace")
DB_PATH       = os.environ.get("HARVEST_DB", os.path.join(WORKSPACE, ".harvest_cfo.db"))
BRAIN_REPO    = os.environ.get("HARVEST_BRAIN_REPO", os.path.join(WORKSPACE, "brain"))
INDEX_HTML    = os.path.join(WORKSPACE, "index.html")
MINER_SCRIPT  = os.path.join(WORKSPACE, "vm_algora_miner.py")
JOB_LOG_PATH  = os.path.join(WORKSPACE, "harvest_jobs.log")
ROG_QUEUE_REL = os.path.join("tasks", "rog_queue.json")
ROG_RESULT_REL = os.path.join("results", "rog_result.json")
PORT          = int(os.environ.get("HARVEST_PORT", "8799"))
BIND          = os.environ.get("HARVEST_BIND", "0.0.0.0")
VERSION       = "1.0.0-surgery"

# マネーフローのバケット（絶対に混ぜない）
BUCKET_EXISTING = "existing"   # 元から保有しているポイント残高
BUCKET_SUBS     = "subs"       # 解約による固定費削減

# 着金ステータス
ST_PENDING  = "pending_review"   # 提出済・審査待ち（未着金）
ST_LANDED   = "landed"           # 口座に着金済（実着金）
ST_REJECTED = "rejected"
ST_HOLD     = "gate_hold"

# ---------------------------------------------------------------------
# シードデータ（水増しゼロの初期値）
# ---------------------------------------------------------------------
SEED_STATE = [
    ("existing_olive_yen",     60000.0, "三井住友カード Olive 特典 40,000pt (ウエル活1.5倍換算) / 2026-11-30 付与予定", BUCKET_EXISTING),
    ("existing_rakuten_yen",   52089.0, "楽天ポイント保有残高 / アカウントロック解除 Case 81334359 / 本人確認書類添付待ち", BUCKET_EXISTING),
    ("existing_beams_yen",     23732.0, "BEAMS CLUB ポイント残高", BUCKET_EXISTING),
    ("existing_mynb_yen",       1405.0, "myNB ポイント残高", BUCKET_EXISTING),
    ("subs_google_ai_pro_yen",  2900.0, "Google AI Pro (5TB) 月額 / Apple ID経由 / 注文ID MMGH3D5MXX / 更新 2026-11-18", BUCKET_SUBS),
    ("subs_sofmap_yen",          758.0, "SOFMAP TEIKI EKIMU 月額長期保証プランM / 毎月1日決済 / 10-31までに解約", BUCKET_SUBS),
]

SEED_PAYOUTS = [
    # (source, title, url, amount_yen, status, note)
    ("algora", "bounty-plaza #1904: Stellar retry/backoff knobs ($75 USDC)",
     "https://github.com/zhangjiayang6835-cyber/bounty-plaza/pull/4138",
     11250.0, ST_PENDING, "Algoraエスクローなし野良repo / マージ&着金まで確定0円。Issue #1904 が正"),
    ("misakanet", "MisakaNet #3901: lesson_gate.py missing-PyYAML cascade",
     "https://github.com/Ikalus1988/MisakaNet/pull/3922",
     0.0, ST_PENDING, "直接金銭報酬なし / fork初回PRでworkflow承認待ち / needs-human-review"),
]

SEED_GATES = [
    ("gate_sofmap_cancel",        "SOFMAP TEIKI EKIMU 解約",           "open", "0077-78-9988 または会員ページ / 10-31までに解約で翌月以降0円", "2026-10-31"),
    ("gate_google_ai_pro_cancel", "Google AI Pro (5TB) 解約",          "open", "Mac システム設定 > Apple Account > メディアと購入 > サブスクリプション / 注文ID MMGH3D5MXX", "2026-11-18"),
    ("gate_rakuten_unlock",       "楽天アカウントロック解除",          "open", "Case 81334359 / 返信下書き draft_id r8345770108746311809 作成済 / 本人確認書類添付", ""),
    ("gate_olive_grant",          "Olive 特典 40,000pt 付与確認",      "open", "2026-11-30 付与予定 / 付与確認後に existing へ反映", "2026-11-30"),
    ("gate_rog_2fa",              "ROG Chrome 2FA 手動突破",           "open", "WAITING_2FA で停止したら人が1回だけ通す", ""),
]

# ---------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------
_DB_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS cfo_state (
    key        TEXT PRIMARY KEY,
    value      REAL NOT NULL DEFAULT 0,
    label      TEXT DEFAULT '',
    bucket     TEXT DEFAULT 'meta',
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS deck_payouts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT,
    title      TEXT,
    url        TEXT,
    amount_yen REAL NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'pending_review',
    note       TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS deck_gates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    gate_key   TEXT UNIQUE,
    label      TEXT,
    status     TEXT NOT NULL DEFAULT 'open',
    detail     TEXT DEFAULT '',
    deadline   TEXT DEFAULT '',
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS job_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT,
    status      TEXT,
    started_at  TEXT,
    finished_at TEXT,
    log         TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS action_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT,
    action  TEXT,
    payload TEXT,
    result  TEXT
);
CREATE TABLE IF NOT EXISTS rog_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT,
    task_id TEXT,
    site    TEXT,
    status  TEXT,
    detail  TEXT
);
"""


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init_db(force_seed: bool = True) -> None:
    os.makedirs(WORKSPACE, exist_ok=True)
    with _DB_LOCK, db() as con:
        con.executescript(SCHEMA)
        if force_seed:
            for key, val, label, bucket in SEED_STATE:
                con.execute(
                    "INSERT OR IGNORE INTO cfo_state(key,value,label,bucket,updated_at) VALUES(?,?,?,?,?)",
                    (key, val, label, bucket, now_iso()),
                )
            for src, title, url, amt, st, note in SEED_PAYOUTS:
                row = con.execute("SELECT id FROM deck_payouts WHERE url=?", (url,)).fetchone()
                if not row:
                    con.execute(
                        "INSERT INTO deck_payouts(source,title,url,amount_yen,status,note,created_at,updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (src, title, url, amt, st, note, now_iso(), now_iso()),
                    )
            for gk, label, st, detail, dl in SEED_GATES:
                con.execute(
                    "INSERT OR IGNORE INTO deck_gates(gate_key,label,status,detail,deadline,updated_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (gk, label, st, detail, dl, now_iso()),
                )
        con.commit()


def log_job(action: str, status: str, text: str = "") -> None:
    line = f"[{now_iso()}] {action} :: {status} :: {text}".rstrip()
    try:
        with open(JOB_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    try:
        with _DB_LOCK, db() as con:
            con.execute(
                "INSERT INTO job_runs(action,status,started_at,finished_at,log) VALUES(?,?,?,?,?)",
                (action, status, now_iso(), now_iso(), text[:4000]),
            )
            con.commit()
    except Exception:
        pass


def log_action(action: str, payload: dict, result: dict) -> None:
    try:
        with _DB_LOCK, db() as con:
            con.execute(
                "INSERT INTO action_log(ts,action,payload,result) VALUES(?,?,?,?)",
                (now_iso(), action, json.dumps(payload, ensure_ascii=False)[:2000],
                 json.dumps(result, ensure_ascii=False)[:4000]),
            )
            con.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------
# 集計（実着金 / 審査待ち / 既存残高 / 固定費削減 を厳密分離）
# ---------------------------------------------------------------------
def money_summary() -> dict:
    with _DB_LOCK, db() as con:
        landed = con.execute(
            "SELECT COALESCE(SUM(amount_yen),0) AS s FROM deck_payouts WHERE status=?",
            (ST_LANDED,)).fetchone()["s"]
        pending = con.execute(
            "SELECT COALESCE(SUM(amount_yen),0) AS s FROM deck_payouts WHERE status=?",
            (ST_PENDING,)).fetchone()["s"]
        hold = con.execute(
            "SELECT COALESCE(SUM(amount_yen),0) AS s FROM deck_payouts WHERE status=?",
            (ST_HOLD,)).fetchone()["s"]
        existing = con.execute(
            "SELECT COALESCE(SUM(value),0) AS s FROM cfo_state WHERE bucket=?",
            (BUCKET_EXISTING,)).fetchone()["s"]
        subs = con.execute(
            "SELECT COALESCE(SUM(value),0) AS s FROM cfo_state WHERE bucket=?",
            (BUCKET_SUBS,)).fetchone()["s"]
    return {
        "actual_landed_yen": float(landed),
        "submitted_pending_yen": float(pending),
        "gate_hold_yen": float(hold),
        "existing_balance_yen": float(existing),
        "monthly_cut_yen": float(subs),
        "annual_cut_yen": float(subs) * 12.0,
    }


def build_state() -> dict:
    with _DB_LOCK, db() as con:
        payouts = [dict(r) for r in con.execute(
            "SELECT * FROM deck_payouts ORDER BY (status=?) DESC, amount_yen DESC, id DESC",
            (ST_PENDING,)).fetchall()]
        gates = [dict(r) for r in con.execute(
            "SELECT * FROM deck_gates ORDER BY (status='open') DESC, deadline='' , deadline ASC, id ASC").fetchall()]
        jobs = [dict(r) for r in con.execute(
            "SELECT * FROM job_runs ORDER BY id DESC LIMIT 40").fetchall()]
        logs = [dict(r) for r in con.execute(
            "SELECT * FROM action_log ORDER BY id DESC LIMIT 60").fetchall()]
        state_rows = [dict(r) for r in con.execute(
            "SELECT * FROM cfo_state ORDER BY bucket, id" if False else
            "SELECT * FROM cfo_state ORDER BY bucket, key").fetchall()]
        rog = [dict(r) for r in con.execute(
            "SELECT * FROM rog_events ORDER BY id DESC LIMIT 40").fetchall()]

    return {
        "ok": True,
        "version": VERSION,
        "now": now_iso(),
        "money": money_summary(),
        "payouts": payouts,
        "gates": gates,
        "state_items": state_rows,
        "jobs": jobs,
        "logs": logs,
        "rog_events": rog,
        "quota": {
            "grok_bot_quota_pct": 0.0,
            "note": "チャット/Task/SendToAgent 未使用。司令塔は VM master daemon + Git + ROG agy。",
        },
        "wiring": {
            "db": DB_PATH,
            "brain_repo": BRAIN_REPO,
            "brain_repo_exists": os.path.isdir(BRAIN_REPO),
            "miner_exists": os.path.isfile(MINER_SCRIPT),
            "rog_queue": os.path.join(BRAIN_REPO, ROG_QUEUE_REL),
        },
    }


# ---------------------------------------------------------------------
# Git ブリッジ（MCP不要：VM -> brain repo -> ROG dot-poll）
# ---------------------------------------------------------------------
def git(args, cwd=BRAIN_REPO, timeout=180):
    return subprocess.run(["git", "-C", cwd] + args,
                          capture_output=True, text=True, timeout=timeout)


def brain_push(message: str) -> dict:
    if not os.path.isdir(os.path.join(BRAIN_REPO, ".git")):
        return {"ok": False, "reason": f"brain repo not found: {BRAIN_REPO}"}
    git(["add", "-A"])
    st = git(["status", "--porcelain"])
    if not st.stdout.strip():
        return {"ok": True, "pushed": False, "reason": "no changes"}
    c = git(["commit", "-m", message])
    if c.returncode != 0:
        return {"ok": False, "reason": c.stdout + c.stderr}
    p = git(["push", "origin", "HEAD"])
    return {"ok": p.returncode == 0, "pushed": p.returncode == 0,
            "detail": (p.stdout + p.stderr).strip()[:800]}


ROG_SITE_PRESETS = {
    "vpass": {
        "label": "Vpass (三井住友カード) ログイン/特典確認",
        "url": "https://vpass.ne.jp/",
        "steps": [
            {"type": "navigate", "url": "https://vpass.ne.jp/"},
            {"type": "wait", "ms": 3500},
            {"type": "screenshot", "name": "vpass_top"},
            {"type": "detect2fa", "selectors": ["input[type=password]", "input[name*=otp]"]},
            {"type": "eval", "js": "document.title"},
            {"type": "screenshot", "name": "vpass_after"},
        ],
    },
    "rakuten": {
        "label": "楽天 ログイン/ポイント残高確認 (Case 81334359)",
        "url": "https://www.rakuten.co.jp/",
        "steps": [
            {"type": "navigate", "url": "https://www.rakuten.co.jp/"},
            {"type": "wait", "ms": 4000},
            {"type": "screenshot", "name": "rakuten_top"},
            {"type": "click_text", "text": "ログイン"},
            {"type": "wait", "ms": 3000},
            {"type": "detect2fa", "selectors": ["input[type=password]", "input[name*=code]"]},
            {"type": "screenshot", "name": "rakuten_after"},
        ],
    },
    "ion": {
        "label": "イオン ログイン/WAON確認",
        "url": "https://www.aeon.com/",
        "steps": [
            {"type": "navigate", "url": "https://www.aeon.com/"},
            {"type": "wait", "ms": 4000},
            {"type": "screenshot", "name": "aeon_top"},
            {"type": "click_text", "text": "ログイン"},
            {"type": "wait", "ms": 3000},
            {"type": "detect2fa", "selectors": ["input[type=password]"]},
            {"type": "screenshot", "name": "aeon_after"},
        ],
    },
    "zozo": {
        "label": "ZOZO (Akamai 403 突破確認 / 実Chrome必須)",
        "url": "https://zozo.jp/",
        "steps": [
            {"type": "navigate", "url": "https://zozo.jp/"},
            {"type": "wait", "ms": 5000},
            {"type": "eval", "js": "document.title"},
            {"type": "screenshot", "name": "zozo_top"},
            {"type": "detect2fa", "selectors": ["input[type=password]"]},
        ],
    },
}


def dispatch_rog_gui(payload: dict) -> dict:
    sites = payload.get("sites") or list(ROG_SITE_PRESETS.keys())
    sites = [s for s in sites if s in ROG_SITE_PRESETS]
    if not sites:
        return {"ok": False, "reason": "no valid sites", "valid": list(ROG_SITE_PRESETS.keys())}

    queue_path = os.path.join(BRAIN_REPO, ROG_QUEUE_REL)
    os.makedirs(os.path.dirname(queue_path), exist_ok=True)
    if os.path.isfile(queue_path):
        try:
            with open(queue_path, encoding="utf-8") as fh:
                queue = json.load(fh)
        except Exception:
            queue = {"version": 1, "tasks": []}
    else:
        queue = {"version": 1, "tasks": []}
    queue.setdefault("tasks", [])

    created = []
    for site in sites:
        preset = ROG_SITE_PRESETS[site]
        task = {
            "id": "rog-" + uuid.uuid4().hex[:12],
            "site": site,
            "label": preset["label"],
            "url": preset["url"],
            "steps": preset["steps"],
            "status": "queued",
            "created_at": now_iso(),
            "note": payload.get("note", ""),
            "engine": {"chrome_cdp_port": 9222, "agy_model": "gemini-3.8-flash"},
        }
        queue["tasks"].append(task)
        created.append(task["id"])

    queue["tasks"] = queue["tasks"][-200:]
    with open(queue_path, "w", encoding="utf-8") as fh:
        json.dump(queue, fh, ensure_ascii=False, indent=2)

    push = brain_push(f"chore(rog-queue): dispatch {len(created)} GUI task(s) [{','.join(sites)}]")
    return {"ok": True, "queued": created, "sites": sites, "git": push}


def ack_rog_results() -> dict:
    """ROGがgit pushで戻した results/rog_result.json をSQLiteへ取り込む。"""
    path = os.path.join(BRAIN_REPO, ROG_RESULT_REL)
    if not os.path.isfile(path):
        return {"ok": False, "reason": f"not found: {path}"}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    items = data if isinstance(data, list) else data.get("results", [])
    n = 0
    with _DB_LOCK, db() as con:
        for it in items:
            con.execute(
                "INSERT INTO rog_events(ts,task_id,site,status,detail) VALUES(?,?,?,?,?)",
                (now_iso(), str(it.get("id", "")), str(it.get("site", "")),
                 str(it.get("status", "unknown")), json.dumps(it, ensure_ascii=False)[:2000]))
            n += 1
        con.commit()
    # キュー側を消費済みに更新
    qpath = os.path.join(BRAIN_REPO, ROG_QUEUE_REL)
    done_ids = {str(it.get("id")) for it in items}
    if os.path.isfile(qpath):
        try:
            with open(qpath, encoding="utf-8") as fh:
                q = json.load(fh)
            for t in q.get("tasks", []):
                if t.get("id") in done_ids:
                    t["status"] = "acked"
            with open(qpath, "w", encoding="utf-8") as fh:
                json.dump(q, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass
    log_job("ack_rog_results", "ok", f"{n} events ingested")
    return {"ok": True, "ingested": n}


# ---------------------------------------------------------------------
# ジョブ実行（Grok Botを介さずVM内スレッドで即実行）
# ---------------------------------------------------------------------
_RUNNING = {}
_RUN_LOCK = threading.Lock()


def _spawn(name: str, fn) -> dict:
    with _RUN_LOCK:
        if _RUNNING.get(name):
            return {"ok": True, "accepted": False, "reason": f"{name} already running"}
        _RUNNING[name] = True

    def _wrap():
        try:
            log_job(name, "start")
            out = fn()
            log_job(name, "ok", json.dumps(out, ensure_ascii=False)[:3000] if out else "")
        except Exception:
            log_job(name, "error", traceback.format_exc()[-3000:])
        finally:
            with _RUN_LOCK:
                _RUNNING[name] = False

    threading.Thread(target=_wrap, name=name, daemon=True).start()
    return {"ok": True, "accepted": True, "job": name}


def job_run_algora_scan(payload: dict) -> dict:
    def _run():
        args = [sys.executable, MINER_SCRIPT, "--json"]
        if payload.get("limit"):
            args += ["--limit", str(int(payload["limit"]))]
        if payload.get("max_prs"):
            args += ["--max-prs", str(int(payload["max_prs"]))]
        if payload.get("repo_filter"):
            args += ["--repo-filter", str(payload["repo_filter"])]
        if payload.get("dry_run"):
            args.append("--dry-run")
        p = subprocess.run(args, capture_output=True, text=True, timeout=7200, cwd=WORKSPACE)
        log_job("algora_scan.stdout", "ok", p.stdout[-3000:])
        if p.returncode != 0:
            log_job("algora_scan.stderr", "error", p.stderr[-3000:])
        return {"returncode": p.returncode, "stdout_tail": p.stdout[-1500:]}

    return _spawn("run_algora_scan", _run)


# ---------------------------------------------------------------------
# アクションルーティング
# ---------------------------------------------------------------------
def act_update_gate(payload: dict) -> dict:
    gk = payload.get("gate_key")
    status = payload.get("status", "open")
    if not gk:
        return {"ok": False, "reason": "gate_key required"}
    with _DB_LOCK, db() as con:
        con.execute("UPDATE deck_gates SET status=?, detail=COALESCE(?,detail), updated_at=? WHERE gate_key=?",
                    (status, payload.get("detail"), now_iso(), gk))
        con.commit()
    return {"ok": True, "gate_key": gk, "status": status}


def act_confirm_payout(payload: dict) -> dict:
    pid = payload.get("payout_id")
    if pid is None:
        return {"ok": False, "reason": "payout_id required"}
    with _DB_LOCK, db() as con:
        row = con.execute("SELECT * FROM deck_payouts WHERE id=?", (int(pid),)).fetchone()
        if not row:
            return {"ok": False, "reason": "payout not found"}
        amount = float(payload.get("landed_yen", row["amount_yen"]) or 0.0)
        con.execute("UPDATE deck_payouts SET status=?, amount_yen=?, note=?, updated_at=? WHERE id=?",
                    (ST_LANDED, amount,
                     (row["note"] or "") + f" | 着金確定 {now_iso()} {amount}円",
                     now_iso(), int(pid)))
        con.commit()
    return {"ok": True, "payout_id": int(pid), "landed_yen": amount,
            "money": money_summary()}


def act_reject_payout(payload: dict) -> dict:
    pid = payload.get("payout_id")
    if pid is None:
        return {"ok": False, "reason": "payout_id required"}
    with _DB_LOCK, db() as con:
        con.execute("UPDATE deck_payouts SET status=?, note=COALESCE(note,'')||?, updated_at=? WHERE id=?",
                    (ST_REJECTED, f" | 却下 {now_iso()} {payload.get('reason','')}", now_iso(), int(pid)))
        con.commit()
    return {"ok": True, "money": money_summary()}


def act_hold_payout(payload: dict) -> dict:
    pid = payload.get("payout_id")
    if pid is None:
        return {"ok": False, "reason": "payout_id required"}
    with _DB_LOCK, db() as con:
        con.execute("UPDATE deck_payouts SET status=?, updated_at=? WHERE id=?",
                    (ST_HOLD, now_iso(), int(pid)))
        con.commit()
    return {"ok": True, "money": money_summary()}


def act_add_payout(payload: dict) -> dict:
    with _DB_LOCK, db() as con:
        cur = con.execute(
            "INSERT INTO deck_payouts(source,title,url,amount_yen,status,note,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (payload.get("source", "manual"), payload.get("title", ""), payload.get("url", ""),
             float(payload.get("amount_yen", 0) or 0), ST_PENDING,
             payload.get("note", ""), now_iso(), now_iso()))
        con.commit()
        return {"ok": True, "payout_id": cur.lastrowid}


def act_heartbeat(payload: dict) -> dict:
    """ROGからの死活。Grok Bot経由ではなくHTTP直結。"""
    with _DB_LOCK, db() as con:
        con.execute("INSERT INTO rog_events(ts,task_id,site,status,detail) VALUES(?,?,?,?,?)",
                    (now_iso(), "heartbeat", str(payload.get("host", "rog")),
                     "alive", json.dumps(payload, ensure_ascii=False)[:1000]))
        con.commit()
    return {"ok": True, "server_time": now_iso()}


ACTIONS = {
    "run_algora_scan": job_run_algora_scan,
    "dispatch_rog_gui": lambda p: dispatch_rog_gui(p),
    "ack_rog_results": lambda p: ack_rog_results(),
    "update_gate": act_update_gate,
    "confirm_payout": act_confirm_payout,
    "reject_payout": act_reject_payout,
    "hold_payout": act_hold_payout,
    "add_payout": act_add_payout,
    "heartbeat": act_heartbeat,
    "ping": lambda p: {"ok": True, "pong": now_iso()},
}


# ---------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "HarvestMasterDaemon/" + VERSION
    protocol_version = "HTTP/1.1"

    # ---- helpers ----
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Harvest-Token")
        self.send_header("Access-Control-Max-Age", "86400")

    def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {"_raw": raw.decode("utf-8", "replace")}

    # ---- verbs ----
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            if os.path.isfile(INDEX_HTML):
                with open(INDEX_HTML, "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            return self._send(200, b"<h1>harvest master daemon</h1><p>index.html not found</p>",
                              "text/html; charset=utf-8")
        if path == "/api/health":
            return self._json(200, {"ok": True, "version": VERSION, "now": now_iso(),
                                    "db": DB_PATH, "grok_quota_pct": 0.0})
        if path == "/api/state":
            try:
                return self._json(200, build_state())
            except Exception:
                return self._json(500, {"ok": False, "error": traceback.format_exc()})
        if path == "/api/logs":
            try:
                with open(JOB_LOG_PATH, encoding="utf-8") as fh:
                    lines = fh.readlines()[-200:]
                return self._json(200, {"ok": True, "lines": lines})
            except FileNotFoundError:
                return self._json(200, {"ok": True, "lines": []})
        if path == "/api/rog_queue":
            qp = os.path.join(BRAIN_REPO, ROG_QUEUE_REL)
            if os.path.isfile(qp):
                with open(qp, encoding="utf-8") as fh:
                    return self._send(200, fh.read().encode("utf-8"))
            return self._json(200, {"version": 1, "tasks": []})
        return self._json(404, {"ok": False, "reason": "not found", "path": path})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/action":
            return self._json(404, {"ok": False, "reason": "not found", "path": path})
        payload = self._read_json()
        action = payload.get("action") or payload.get("cmd")
        if not action:
            return self._json(400, {"ok": False, "reason": "action required",
                                    "available": sorted(ACTIONS.keys())})
        fn = ACTIONS.get(action)
        if not fn:
            return self._json(400, {"ok": False, "reason": f"unknown action: {action}",
                                    "available": sorted(ACTIONS.keys())})
        try:
            result = fn(payload) or {}
            if "ok" not in result:
                result["ok"] = True
            result["action"] = action
            result["state"] = build_state()
            log_action(action, payload, result)
            return self._json(200, result)
        except Exception:
            err = {"ok": False, "action": action, "error": traceback.format_exc()}
            log_action(action, payload, err)
            return self._json(500, err)

    def log_message(self, fmt, *args):
        if os.environ.get("HARVEST_HTTP_VERBOSE") == "1":
            sys.stderr.write("[http] " + (fmt % args) + "\n")


# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------
def main():
    argv = sys.argv[1:]
    init_db(force_seed=True)

    if "--init" in argv:
        s = money_summary()
        print(json.dumps({"ok": True, "db": DB_PATH, "seeded": True, "money": s},
                         ensure_ascii=False, indent=2))
        if "--serve" not in argv:
            return
    if "--selftest" in argv:
        print(json.dumps(build_state(), ensure_ascii=False, indent=2)[:3000])
        return

    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    banner = {
        "service": "harvest-master-daemon", "version": VERSION, "bind": f"{BIND}:{PORT}",
        "db": DB_PATH, "brain_repo": BRAIN_REPO, "grok_bot_quota_pct": 0.0,
        "endpoints": ["GET /api/state", "POST /api/action", "GET /api/health", "GET /"],
        "actions": sorted(ACTIONS.keys()),
    }
    print(json.dumps(banner, ensure_ascii=False, indent=2), flush=True)
    log_job("daemon", "listen", f"{BIND}:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log_job("daemon", "stop", "KeyboardInterrupt")


if __name__ == "__main__":
    main()
