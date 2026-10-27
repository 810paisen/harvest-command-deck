#!/usr/bin/env python3
"""
Harvest Command Deck Full-Stack Bidirectional API & Ledger Server (v2.9)
Role: 🔌フルスタックDB1号（双方向API・台帳直結専任）
- Eliminates static-only `http.server` delivery.
- Connects Web Dashboard actions (`POST /api/action`) & live reads (`GET /api/state`, `GET /api/ledger`)
  directly to `/workspace/.harvest_cfo.db` (SQLite WAL).
- Links one-tap approvals (`set_gate`), payout confirmations (`toggle_payout` / `mark_payout_landed`),
  task completions (`complete_task`), and bot dispatches (`dispatch_queue`) in real time to
  `approval_gates`, `deck_gates`, `realized_ledger`, `deck_payouts`, `harvest_targets`,
  `bot_execution_queue`, and `spawned_worker_bots`.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BASE_DIR = pathlib.Path("/workspace")
DB_PATH = pathlib.Path(os.environ.get("HARVEST_DB_PATH", str(BASE_DIR / ".harvest_cfo.db")))
SITE_DIR = pathlib.Path(os.environ.get("HARVEST_SITE_DIR", str(BASE_DIR / "board-site")))
HOST = os.environ.get("HARVEST_API_HOST", "0.0.0.0")
PORT = int(os.environ.get("HARVEST_API_PORT", "8799"))

DB_LOCK = threading.Lock()

POCKET_ALIAS_MAP = {
    "4138": ("BOUNTY_PLAZA_1904", 11250, "実提出済・審査待ち (PR #4138 & /claim #1904 comment #6431844463)", "4138", 75.0),
    "pr-4138": ("BOUNTY_PLAZA_1904", 11250, "実提出済・審査待ち (PR #4138 & /claim #1904 comment #6431844463)", "4138", 75.0),
    "BOUNTY_PLAZA_1904": ("BOUNTY_PLAZA_1904", 11250, "実提出済・審査待ち (PR #4138 & /claim #1904 comment #6431844463)", "4138", 75.0),
    "3922": ("MISAKANET_3901", 0, "実提出済・審査待ち (PR #3922, Intake #3921 & comment #6431829472)", "3922", 0.0),
    "pr-3922": ("MISAKANET_3901", 0, "実提出済・審査待ち (PR #3922, Intake #3921 & comment #6431829472)", "3922", 0.0),
    "MISAKANET_3901": ("MISAKANET_3901", 0, "実提出済・審査待ち (PR #3922, Intake #3921 & comment #6431829472)", "3922", 0.0),
}

DEFAULT_GATES = [
    (
        "gate-google-ai-pro",
        "Google AI Pro Trial (¥2,900/mo) 解約・課金停止ガード",
        "解約・決済境界",
        "hold",
        "# [承認後実行] Google Play / Google One 定期購入解約ページを開き、次回更新日前に解約手続きを実行",
        34800,
        "ZOMBIE-010-SUBSCRIPTION_LEAK_GUARD",
        5,
    ),
    (
        "gate-olive-welkatsu",
        "Olive Vポイント 40,000pt → WAON POINT 等価交換（ウエル活1.5倍準備）",
        "ポイント交換境界",
        "hold",
        "# [承認後実行] Vpass / VポイントPayアプリにて WAON POINT 40,000pt 即時等価交換ルートを実行（¥60,000相当化）",
        60000,
        "ZOMBIE-009-POINT_BOOST_ARBITRAGE",
        1,
    ),
    (
        "gate-l2-airdrop-sig",
        "Monad / MegaETH / Berachain Testnet (0円Faucet・公開アドレス登録)",
        "署名・規約境界",
        "hold",
        "# [承認後実行] 公開EVMアドレスのみを用い、秘密鍵非接触で0円Testnet Faucet取得ジョブをキュー投入",
        15000,
        "ZOMBIE-006-L2_BASE_ARBITRUM_CLAIM",
        4,
    ),
]


def now_jst() -> str:
    tz = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S JST")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_fullstack_schema() -> None:
    with DB_LOCK:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS approval_gates (
                gate_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                boundary_type TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'hold',
                command_text TEXT NOT NULL,
                impact_yen INTEGER NOT NULL DEFAULT 0,
                assigned_bot_code TEXT NOT NULL,
                target_id INTEGER,
                updated_by TEXT NOT NULL DEFAULT 'system',
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS deck_gates (
                gate_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                cmd_text TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS deck_payouts (
                pr_id TEXT PRIMARY KEY,
                landed INTEGER NOT NULL DEFAULT 0,
                yen INTEGER NOT NULL DEFAULT 0,
                usd REAL NOT NULL DEFAULT 0.0,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_execution_queue (
                queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                target_key TEXT NOT NULL,
                assigned_bot_code TEXT NOT NULL,
                queue_status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                db_effect_summary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                executed_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_action_audit (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT NOT NULL,
                action TEXT NOT NULL,
                target_key TEXT NOT NULL,
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL,
                queue_id INTEGER,
                client_source TEXT,
                recorded_at TEXT NOT NULL
            )
            """
        )
        ts = now_jst()
        for gid, title, btype, state, cmd, impact, bot_code, tid in DEFAULT_GATES:
            cur.execute(
                """
                INSERT OR IGNORE INTO approval_gates
                (gate_id, title, boundary_type, state, command_text, impact_yen, assigned_bot_code, target_id, updated_by, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'init_seed', ?)
                """,
                (gid, title, btype, state, cmd, impact, bot_code, tid, ts),
            )
            cur.execute(
                "UPDATE approval_gates SET assigned_bot_code = ? WHERE gate_id = ?",
                (bot_code, gid),
            )
            cur.execute(
                """
                INSERT OR IGNORE INTO deck_gates (gate_id, state, cmd_text, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (gid, state, cmd, ts),
            )
        cur.execute(
            """
            INSERT OR IGNORE INTO deck_payouts (pr_id, landed, yen, usd, updated_at)
            VALUES ('4138', 0, 11250, 75.0, ?)
            """,
            (ts,),
        )
        conn.commit()
        conn.close()


def fetch_full_state(conn: sqlite3.Connection) -> dict:
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            COALESCE(SUM(actual_landed_yen), 0) AS actual_landed_yen,
            COALESCE(SUM(submitted_pending_yen), 0) AS submitted_pending_yen,
            COALESCE(SUM(existing_balance_yen), 0) AS existing_balance_yen
        FROM realized_ledger
        """
    )
    kpi_row = dict(cur.fetchone())

    cur.execute("SELECT * FROM realized_ledger ORDER BY rowid ASC")
    ledger_rows = []
    for r in cur.fetchall():
        d = dict(r)
        iid = d.get("id") or d.get("item_id") or ""
        title = d.get("item_name") or d.get("title") or ""
        purl = d.get("proof_url") or d.get("evidence_url_or_path") or ""
        uat = d.get("updated_at") or d.get("recorded_at") or ""
        d["id"] = iid
        d["item_id"] = iid
        d["item_name"] = title
        d["title"] = title
        d["proof_url"] = purl
        d["evidence_url_or_path"] = purl
        d["updated_at"] = uat
        d["recorded_at"] = uat
        ledger_rows.append(d)

    cur.execute(
        """
        SELECT gate_id, title, boundary_type, state, command_text, impact_yen,
               assigned_bot_code, target_id, updated_by, updated_at
        FROM approval_gates
        ORDER BY rowid ASC
        """
    )
    approval_gates = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT gate_id, state, cmd_text, updated_at FROM deck_gates")
    deck_gates_map = {r["gate_id"]: dict(r) for r in cur.fetchall()}

    gates_dict = {}
    gate_states = {}
    for g in approval_gates:
        gid = g["gate_id"]
        dg = deck_gates_map.get(gid)
        st = g["state"]
        txt = (dg["cmd_text"] if dg and dg.get("cmd_text") else g["command_text"])
        gates_dict[gid] = {
            "state": st,
            "text": txt,
            "title": g["title"],
            "assigned_bot_code": g["assigned_bot_code"],
            "updated_at": g["updated_at"],
        }
        gate_states[gid] = {
            "state": st,
            "command_text": txt,
            "updated_at": g["updated_at"],
            "assigned_bot_code": g["assigned_bot_code"],
        }

    cur.execute("SELECT pr_id, landed, yen, usd, updated_at FROM deck_payouts")
    deck_payouts = {
        r["pr_id"]: {
            "landed": bool(r["landed"]),
            "yen": r["yen"],
            "usd": r["usd"],
            "updated_at": r["updated_at"],
        }
        for r in cur.fetchall()
    }

    payout_states = {}
    for row in ledger_rows:
        iid = row["item_id"]
        is_landed = (row["actual_landed_yen"] or 0) > 0
        payout_states[iid] = is_landed
        if iid == "BOUNTY_PLAZA_1904":
            payout_states["pr-4138"] = is_landed
            payout_states["4138"] = is_landed
            deck_payouts["4138"] = {
                "landed": is_landed,
                "yen": 11250,
                "usd": 75.0,
                "updated_at": row["recorded_at"],
            }
        elif iid == "MISAKANET_3901":
            payout_states["pr-3922"] = is_landed
            payout_states["3922"] = is_landed

    cur.execute("SELECT * FROM harvest_targets ORDER BY id ASC")
    targets = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM spawned_worker_bots ORDER BY bot_id ASC")
    bots = [dict(r) for r in cur.fetchall()]

    cur.execute(
        """
        SELECT queue_id, action_type, target_key, assigned_bot_code, queue_status,
               payload_json, db_effect_summary, created_at, executed_at
        FROM bot_execution_queue
        ORDER BY queue_id DESC
        LIMIT 25
        """
    )
    queue_items = [dict(r) for r in cur.fetchall()]

    cur.execute(
        """
        SELECT pr_url, repo_full_name, pr_number, state, mergeable, ci_summary,
               review_comments_count, action_needed, stylometry_profile, last_checked_at
        FROM pr_review_watcher_state
        ORDER BY pr_number DESC
        """
    )
    watched_prs = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM global_oss_engines")
    oss_engines = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM global_bounty_candidates ORDER BY ev_per_competitor DESC")
    bounty_candidates = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM repo_reputation_memory")
    repo_rep = [dict(r) for r in cur.fetchall()]

    campaign_sessions = []
    try:
        cur.execute("SELECT * FROM campaign_gui_sessions")
        campaign_sessions = [dict(r) for r in cur.fetchall()]
    except Exception:
        pass

    cur.execute("SELECT COUNT(*) AS cnt FROM sqlite_master WHERE type='table'")
    table_count = cur.fetchone()["cnt"]

    return {
        "ok": True,
        "server": "harvest_api_server_sqlite_v2.9",
        "db_path": str(DB_PATH),
        "table_count": table_count,
        "timestamp": now_jst(),
        "kpi": {
            "actual_landed_yen": int(kpi_row["actual_landed_yen"] or 0),
            "actual_landed_usd": round(float(kpi_row["actual_landed_yen"] or 0) / 150.0, 2),
            "submitted_pending_yen": int(kpi_row["submitted_pending_yen"] or 0),
            "submitted_pending_usd": round(float(kpi_row["submitted_pending_yen"] or 0) / 150.0, 2),
            "existing_balance_yen": int(kpi_row["existing_balance_yen"] or 0),
        },
        "gates": gates_dict,
        "gate_states": gate_states,
        "approval_gates": approval_gates,
        "payouts": deck_payouts,
        "payout_states": payout_states,
        "realized_ledger": ledger_rows,
        "harvest_targets": targets,
        "spawned_worker_bots": bots,
        "bot_execution_queue": queue_items,
        "pr_review_watcher_state": watched_prs,
        "global_oss_engines": oss_engines,
        "global_bounty_candidates": bounty_candidates,
        "repo_reputation_memory": repo_rep,
        "campaign_gui_sessions": campaign_sessions,
    }


def execute_action(payload: dict, client_source: str = "web-dashboard") -> dict:
    action = str(payload.get("action") or "").strip()
    ts = now_jst()

    with DB_LOCK:
        conn = get_conn()
        cur = conn.cursor()
        try:
            if action in ("set_gate", "approve_gate", "update_gate"):
                gate_id = str(payload.get("gate_id") or payload.get("id") or "").strip()
                new_state = str(payload.get("state") or payload.get("status") or "hold").strip().lower()
                if new_state not in ("approve", "hold", "cancel"):
                    raise ValueError(f"Invalid gate state: {new_state}")

                cur.execute("SELECT * FROM approval_gates WHERE gate_id = ?", (gate_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Unknown gate_id: {gate_id}")
                before_obj = dict(row)

                cmd_text = str(payload.get("cmd_text") or payload.get("text") or "").strip()
                if not cmd_text:
                    if new_state == "approve":
                        cmd_text = f"⚡ 承認済 (SQLite永続化 & Queue連動 @ {ts}): {before_obj['command_text']}"
                    elif new_state == "cancel":
                        cmd_text = f"🛑 停止・却下済 (SQLite永続化 @ {ts}): {before_obj['title']}"
                    else:
                        cmd_text = f"⏸ 保留中 (SQLite永続化 @ {ts}): {before_obj['title']}"

                cur.execute(
                    """
                    UPDATE approval_gates
                    SET state = ?, command_text = ?, updated_by = ?, updated_at = ?
                    WHERE gate_id = ?
                    """,
                    (new_state, cmd_text, client_source, ts, gate_id),
                )
                cur.execute(
                    """
                    INSERT INTO deck_gates (gate_id, state, cmd_text, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(gate_id) DO UPDATE SET
                        state = excluded.state,
                        cmd_text = excluded.cmd_text,
                        updated_at = excluded.updated_at
                    """,
                    (gate_id, new_state, cmd_text, ts),
                )

                target_id = before_obj.get("target_id")
                if target_id:
                    target_status = (
                        "APPROVED_QUEUED"
                        if new_state == "approve"
                        else ("CANCELLED_BY_GATE" if new_state == "cancel" else "READY_MANUAL_DISPATCH")
                    )
                    cur.execute(
                        "UPDATE harvest_targets SET status = ? WHERE id = ?",
                        (target_status, target_id),
                    )

                bot_code = before_obj["assigned_bot_code"]
                q_status = "QUEUED" if new_state == "approve" else ("CANCELLED" if new_state == "cancel" else "HELD")
                effect_summary = (
                    f"Gate {gate_id} -> {new_state.upper()} | Bot {bot_code} queue={q_status}"
                )
                cur.execute(
                    """
                    INSERT INTO bot_execution_queue
                    (action_type, target_key, assigned_bot_code, queue_status, payload_json, db_effect_summary, created_at, executed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"GATE_{new_state.upper()}",
                        gate_id,
                        bot_code,
                        q_status,
                        json.dumps(payload, ensure_ascii=False),
                        effect_summary,
                        ts,
                        ts if new_state != "approve" else None,
                    ),
                )
                queue_id = cur.lastrowid

                cur.execute(
                    """
                    UPDATE spawned_worker_bots
                    SET runs_completed = runs_completed + 1,
                        last_status = ?,
                        last_result_summary = ?,
                        last_run_at = ?
                    WHERE bot_code = ?
                    """,
                    (
                        f"GATE_{new_state.upper()}_SYNCED",
                        f"[POST /api/action] {gate_id} set to {new_state.upper()} (queue #{queue_id})",
                        ts,
                        bot_code,
                    ),
                )

                cur.execute("SELECT * FROM approval_gates WHERE gate_id = ?", (gate_id,))
                after_obj = dict(cur.fetchone())
                cur.execute(
                    """
                    INSERT INTO dashboard_action_audit
                    (endpoint, action, target_key, before_json, after_json, queue_id, client_source, recorded_at)
                    VALUES ('/api/action', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action,
                        gate_id,
                        json.dumps(before_obj, ensure_ascii=False),
                        json.dumps(after_obj, ensure_ascii=False),
                        queue_id,
                        client_source,
                        ts,
                    ),
                )
                conn.commit()
                state_snapshot = fetch_full_state(conn)
                return {
                    "ok": True,
                    "action": "set_gate",
                    "gate_id": gate_id,
                    "state": new_state,
                    "cmd_text": cmd_text,
                    "assigned_bot_code": bot_code,
                    "queue_id": queue_id,
                    "queue_status": q_status,
                    "db_effect_summary": effect_summary,
                    "timestamp": ts,
                    "kpi": state_snapshot["kpi"],
                    "gates": state_snapshot["gates"],
                    "gate_states": state_snapshot["gate_states"],
                    "bot_execution_queue": state_snapshot["bot_execution_queue"][:10],
                }

            elif action in ("toggle_payout", "mark_payout_landed", "set_payout"):
                raw_id = str(
                    payload.get("pr_id")
                    or payload.get("item_id")
                    or payload.get("pocket_id")
                    or "4138"
                ).strip()
                item_id, default_yen, default_pending_status, pr_short_id, default_usd = POCKET_ALIAS_MAP.get(
                    raw_id, (raw_id, 11250, "実提出済・審査待ち", "4138", 75.0)
                )

                cur.execute("PRAGMA table_info(realized_ledger)")
                rl_cols = {c[1] for c in cur.fetchall()}
                pk_col = "id" if "id" in rl_cols else "item_id"
                ts_col = "updated_at" if "updated_at" in rl_cols else "recorded_at"
                cur.execute(f"SELECT * FROM realized_ledger WHERE {pk_col} = ?", (item_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Unknown ledger item_id: {item_id}")
                before_obj = dict(row)

                if "landed" in payload and payload["landed"] is not None:
                    landed = bool(payload["landed"])
                else:
                    landed = (before_obj.get("actual_landed_yen") or 0) == 0

                amount_yen = int(
                    payload.get("yen")
                    or payload.get("amount_yen")
                    or before_obj.get("actual_landed_yen")
                    or before_obj.get("submitted_pending_yen")
                    or default_yen
                )

                if landed:
                    new_actual = amount_yen
                    new_pending = 0
                    new_status = f"実着金確定済 (Mark Payout Landed @ {ts} / ¥{amount_yen:,})"
                else:
                    new_actual = 0
                    new_pending = amount_yen
                    new_status = default_pending_status

                cur.execute(
                    f"""
                    UPDATE realized_ledger
                    SET actual_landed_yen = ?,
                        submitted_pending_yen = ?,
                        status = ?,
                        {ts_col} = ?
                    WHERE {pk_col} = ?
                    """,
                    (new_actual, new_pending, new_status, ts, item_id),
                )
                cur.execute(
                    """
                    INSERT INTO deck_payouts (pr_id, landed, yen, usd, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(pr_id) DO UPDATE SET
                        landed = excluded.landed,
                        yen = excluded.yen,
                        usd = excluded.usd,
                        updated_at = excluded.updated_at
                    """,
                    (pr_short_id, 1 if landed else 0, amount_yen, default_usd, ts),
                )

                bot_code = "ZOMBIE-001-BOUNTY_GH_60M_RUST"
                q_status = "COMPLETED"
                effect_summary = (
                    f"Ledger {item_id} (PR #{pr_short_id}) -> landed={landed} "
                    f"(actual_landed_yen=¥{new_actual:,}, submitted_pending_yen=¥{new_pending:,})"
                )
                cur.execute(
                    """
                    INSERT INTO bot_execution_queue
                    (action_type, target_key, assigned_bot_code, queue_status, payload_json, db_effect_summary, created_at, executed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "PAYOUT_LANDED_CONFIRM" if landed else "PAYOUT_REVERT_PENDING",
                        item_id,
                        bot_code,
                        q_status,
                        json.dumps(payload, ensure_ascii=False),
                        effect_summary,
                        ts,
                        ts,
                    ),
                )
                queue_id = cur.lastrowid

                cur.execute(
                    """
                    UPDATE spawned_worker_bots
                    SET runs_completed = runs_completed + 1,
                        last_status = ?,
                        last_result_summary = ?,
                        last_run_at = ?
                    WHERE bot_code = ?
                    """,
                    (
                        "PAYOUT_LANDED_SYNC" if landed else "PAYOUT_PENDING_SYNC",
                        effect_summary,
                        ts,
                        bot_code,
                    ),
                )

                cur.execute(f"SELECT * FROM realized_ledger WHERE {pk_col} = ?", (item_id,))
                after_obj = dict(cur.fetchone())
                cur.execute(
                    """
                    INSERT INTO dashboard_action_audit
                    (endpoint, action, target_key, before_json, after_json, queue_id, client_source, recorded_at)
                    VALUES ('/api/action', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action,
                        item_id,
                        json.dumps(before_obj, ensure_ascii=False),
                        json.dumps(after_obj, ensure_ascii=False),
                        queue_id,
                        client_source,
                        ts,
                    ),
                )
                conn.commit()
                state_snapshot = fetch_full_state(conn)
                return {
                    "ok": True,
                    "action": "toggle_payout",
                    "item_id": item_id,
                    "pr_id": pr_short_id,
                    "landed": landed,
                    "actual_landed_yen": new_actual,
                    "submitted_pending_yen": new_pending,
                    "queue_id": queue_id,
                    "db_effect_summary": effect_summary,
                    "timestamp": ts,
                    "kpi": state_snapshot["kpi"],
                    "payouts": state_snapshot["payouts"],
                    "payout_states": state_snapshot["payout_states"],
                    "bot_execution_queue": state_snapshot["bot_execution_queue"][:10],
                }

            elif action in ("complete_task", "update_target"):
                raw_tid = str(payload.get("target_id") or payload.get("id") or "").strip()
                new_status = str(payload.get("status") or "COMPLETED_CONFIRMED").strip()
                cur.execute("SELECT * FROM harvest_targets WHERE id = ? OR CAST(rowid AS TEXT) = ?", (raw_tid, raw_tid))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Unknown harvest_targets.id: {raw_tid}")
                before_obj = dict(row)
                target_id = before_obj["id"]

                cur.execute(
                    "UPDATE harvest_targets SET status = ? WHERE id = ?",
                    (new_status, target_id),
                )
                bot_code = str(payload.get("bot_code") or "ZOMBIE-009-POINT_BOOST_ARBITRAGE")
                tname = before_obj.get('name') or before_obj.get('target_name') or before_obj.get('title') or f'Target-{target_id}'
                effect_summary = f"Target #{target_id} ({tname}) -> {new_status}"
                cur.execute(
                    """
                    INSERT INTO bot_execution_queue
                    (action_type, target_key, assigned_bot_code, queue_status, payload_json, db_effect_summary, created_at, executed_at)
                    VALUES ('TASK_COMPLETE', ?, ?, 'COMPLETED', ?, ?, ?, ?)
                    """,
                    (
                        f"TARGET-{target_id}",
                        bot_code,
                        json.dumps(payload, ensure_ascii=False),
                        effect_summary,
                        ts,
                        ts,
                    ),
                )
                queue_id = cur.lastrowid

                cur.execute(
                    """
                    UPDATE spawned_worker_bots
                    SET runs_completed = runs_completed + 1,
                        last_status = 'TASK_COMPLETED_SYNC',
                        last_result_summary = ?,
                        last_run_at = ?
                    WHERE bot_code = ?
                    """,
                    (effect_summary, ts, bot_code),
                )

                cur.execute("SELECT * FROM harvest_targets WHERE id = ?", (target_id,))
                after_obj = dict(cur.fetchone())
                cur.execute(
                    """
                    INSERT INTO dashboard_action_audit
                    (endpoint, action, target_key, before_json, after_json, queue_id, client_source, recorded_at)
                    VALUES ('/api/action', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action,
                        f"TARGET-{target_id}",
                        json.dumps(before_obj, ensure_ascii=False),
                        json.dumps(after_obj, ensure_ascii=False),
                        queue_id,
                        client_source,
                        ts,
                    ),
                )
                conn.commit()
                state_snapshot = fetch_full_state(conn)
                return {
                    "ok": True,
                    "action": "complete_task",
                    "target_id": target_id,
                    "status": new_status,
                    "queue_id": queue_id,
                    "db_effect_summary": effect_summary,
                    "timestamp": ts,
                    "kpi": state_snapshot["kpi"],
                    "harvest_targets": state_snapshot["harvest_targets"],
                    "bot_execution_queue": state_snapshot["bot_execution_queue"][:10],
                }

            elif action in ("dispatch_queue", "trigger_queue", "run_bot"):
                target_bot = str(payload.get("bot_code") or "ALL_10_BOTS").strip()
                cur.execute(
                    "UPDATE bot_execution_queue SET queue_status = 'COMPLETED', executed_at = ? WHERE queue_status = 'QUEUED'",
                    (ts,),
                )
                flushed = cur.rowcount
                if target_bot == "ALL_10_BOTS":
                    cur.execute(
                        """
                        UPDATE spawned_worker_bots
                        SET runs_completed = runs_completed + 1,
                            last_status = 'ACTIVE_QUEUE_SYNCED',
                            last_result_summary = ?,
                            last_run_at = ?
                        """,
                        (f"[Queue Dispatch] 全ボット同期スイープ完了 (flushed={flushed} @ {ts})", ts),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE spawned_worker_bots
                        SET runs_completed = runs_completed + 1,
                            last_status = 'ACTIVE_QUEUE_SYNCED',
                            last_result_summary = ?,
                            last_run_at = ?
                        WHERE bot_code = ?
                        """,
                        (f"[Queue Dispatch] 単体ボット実行完了 (flushed={flushed} @ {ts})", ts, target_bot),
                    )
                effect_summary = f"Dispatched execution queue to {target_bot} (flushed_queued={flushed})"
                cur.execute(
                    """
                    INSERT INTO bot_execution_queue
                    (action_type, target_key, assigned_bot_code, queue_status, payload_json, db_effect_summary, created_at, executed_at)
                    VALUES ('DISPATCH_BOT_SWEEP', ?, ?, 'COMPLETED', ?, ?, ?, ?)
                    """,
                    (
                        target_bot,
                        target_bot,
                        json.dumps(payload, ensure_ascii=False),
                        effect_summary,
                        ts,
                        ts,
                    ),
                )
                queue_id = cur.lastrowid
                conn.commit()
                state_snapshot = fetch_full_state(conn)
                return {
                    "ok": True,
                    "action": "dispatch_queue",
                    "target_bot": target_bot,
                    "flushed_queued": flushed,
                    "queue_id": queue_id,
                    "db_effect_summary": effect_summary,
                    "timestamp": ts,
                    "spawned_worker_bots": state_snapshot["spawned_worker_bots"],
                    "bot_execution_queue": state_snapshot["bot_execution_queue"][:10],
                }
            else:
                raise ValueError(f"Unsupported action: {action}")
        finally:
            conn.close()


class HarvestAPIHandler(BaseHTTPRequestHandler):
    server_version = "HarvestFullStackDB/2.9"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"[{now_jst()}] {self.client_address[0]} {fmt % args}\n")

    def _send_json(self, status: int, data: dict) -> None:
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()
        self.wfile.write(raw)

    def _send_file(self, file_path: pathlib.Path, content_type: str) -> None:
        raw = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/api/state", "/api/ledger", "/api/status"):
            with DB_LOCK:
                conn = get_conn()
                try:
                    data = fetch_full_state(conn)
                finally:
                    conn.close()
            self._send_json(200, data)
            return

        if path == "/api/action":
            with DB_LOCK:
                conn = get_conn()
                try:
                    state = fetch_full_state(conn)
                finally:
                    conn.close()
            self._send_json(
                200,
                {
                    "ok": True,
                    "endpoint": "POST /api/action",
                    "supported_actions": ["set_gate", "toggle_payout", "mark_payout_landed", "complete_task", "dispatch_queue"],
                    "db_path": str(DB_PATH),
                    "kpi": state["kpi"],
                    "recent_queue": state["bot_execution_queue"][:5],
                },
            )
            return

        if path in ("/", "/index.html"):
            idx = SITE_DIR / "index.html"
            if idx.is_file():
                self._send_file(idx, "text/html; charset=utf-8")
                return

        if path == "/board.json":
            bj = SITE_DIR / "board.json"
            if bj.is_file():
                self._send_file(bj, "application/json; charset=utf-8")
                return

        self._send_json(404, {"ok": False, "error": f"Path not found: {path}"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/action":
            self._send_json(404, {"ok": False, "error": f"POST only supported on /api/action (got {parsed.path})"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            payload = json.loads(raw_body) if raw_body.strip() else {}
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": f"Invalid JSON body: {exc}"})
            return

        client_source = self.headers.get("Origin") or self.client_address[0] or "local-api"
        try:
            res = execute_action(payload, client_source=client_source)
            self._send_json(200, res)
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})


def main() -> int:
    parser = argparse.ArgumentParser(description="Harvest Full-Stack DB Server")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--host", type=str, default=HOST)
    args, _ = parser.parse_known_args()

    init_fullstack_schema()
    server = ThreadingHTTPServer((args.host, args.port), HarvestAPIHandler)
    server.daemon_threads = True
    print(f"[HarvestFullStackDB] Serving full-stack dashboard & POST /api/action on http://{args.host}:{args.port}/ -> {DB_PATH}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
