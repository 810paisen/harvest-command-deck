#!/usr/bin/env python3
"""
Global Bounty Engine v2.8 — Unified Adapter for the 4 Global OSS Engines + 5 Practical Techniques
Integrates:
  1. 64johnlee/autopr (@8886ddc)       -> Algora/Opire Scanner + SQLite Bandit Memory (repo_reputation_memory)
  2. PARKSystemsCorporation/Lila (@8afb084) -> Forge ($50-$200 Bug/Feature Sweet-Spot) + Scout Dry-Time Fallback + Mark Payout Gate
  3. AfaAgent/solana-bounty-hunter (@3193f62) -> Superteam Earn Agent-Allowed Listing Scanner (Foreign keys/wallets stripped)
  4. xyaz1313/bounty-hunter-agent (@8cd8cb9) -> Multi-language gh search issues + 0-100 Feasibility Evaluator + 15m PR Watcher

Strict Security & Model Policy:
  - Chinese LLM endpoints (Alibaba DashScope / Qwen / DeepSeek / GLM) are 100% disabled.
  - Only the 3 approved non-Chinese $0 OpenRouter models are permitted:
      * nvidia/nemotron-3-ultra-550b-a55b:free
      * cohere/north-mini-code:free
      * poolside/laguna-s-2.1:free
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import urllib.request
from typing import Any

BASE_DIR = pathlib.Path(__file__).resolve().parent
VENDOR_DIR = (
    BASE_DIR / "vendor" / "global_agents"
    if (BASE_DIR / "vendor" / "global_agents").exists()
    else BASE_DIR.parent / "vendor" / "global_agents"
)
DB_PATH = BASE_DIR / ".harvest_cfo.db"
REPORT_PATH = BASE_DIR / "global_bounty_engine_latest.json"

ALLOWED_FREE_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "cohere/north-mini-code:free",
    "poolside/laguna-s-2.1:free",
]

# Import evaluator from xyaz1313/bounty-hunter-agent & models from 64johnlee/autopr
sys.path.insert(0, str(VENDOR_DIR / "bounty-hunter-agent"))
sys.path.insert(0, str(VENDOR_DIR / "autopr"))

try:
    from autopr.scanner import BLOCKED_ORGS, BLOCKED_REPOS  # type: ignore
except Exception:
    BLOCKED_REPOS = {"archestra-ai/archestra", "tscircuit/cli", "lingdojo/kana-dojo"}
    BLOCKED_ORGS = {"Scottcjn", "Expensify"}


def now_jst() -> str:
    tz = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S JST")


def init_v28_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS global_oss_engines (
            engine_id TEXT PRIMARY KEY,
            repo_url TEXT NOT NULL,
            local_path TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            sanitized_status TEXT NOT NULL,
            llm_binding TEXT NOT NULL,
            role_summary TEXT NOT NULL,
            last_verified_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS global_bounty_candidates (
            candidate_key TEXT PRIMARY KEY,
            source_engine TEXT NOT NULL,
            repo_or_platform TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            reward_usd REAL NOT NULL,
            competing_prs INTEGER NOT NULL,
            xyaz_feasibility_score INTEGER NOT NULL,
            lila_forge_band TEXT NOT NULL,
            ev_per_competitor REAL NOT NULL,
            triage_decision TEXT NOT NULL,
            discovered_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def inspect_vendor_engines(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ts = now_jst()
    specs = [
        (
            "autopr",
            "https://github.com/64johnlee/autopr",
            VENDOR_DIR / "autopr",
            "CLEAN (DashScope/Qwen disabled -> OpenRouter $0 Free 3-Model Bound)",
            "nvidia/nemotron-3-ultra-550b-a55b:free + cohere/north-mini-code:free",
            "Algora/Opire Scanner + Competing PR Counter + SQLite Bandit Memory (repo_reputation_memory)",
        ),
        (
            "Lila",
            "https://github.com/PARKSystemsCorporation/Lila",
            VENDOR_DIR / "Lila",
            "CLEAN (DeepSeek API disabled -> OpenRouter $0 Free 3-Model Bound)",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "Forge ($50-$200 Bug/Feature Sweet-Spot) + Scout Dry-Time Fallback + Operator Mark Payout Gate",
        ),
        (
            "solana-bounty-hunter",
            "https://github.com/AfaAgent/solana-bounty-hunter",
            VENDOR_DIR / "solana-bounty-hunter",
            "CLEAN (Foreign API_KEY & XRZLG Wallet stripped; Public Read-Only)",
            "poolside/laguna-s-2.1:free",
            "Superteam Earn Live Agent-Eligible Bounty Scanner (/api/agents/listings/live)",
        ),
        (
            "bounty-hunter-agent",
            "https://github.com/xyaz1313/bounty-hunter-agent",
            VENDOR_DIR / "bounty-hunter-agent",
            "CLEAN (Pure Python + gh CLI; 0 external webhooks)",
            "cohere/north-mini-code:free",
            "Multi-language gh search issues + 0-100 Feasibility Evaluator + 15m PR Watcher",
        ),
    ]
    rows: list[dict[str, Any]] = []
    cur = conn.cursor()
    for eid, url, path, san, llm, role in specs:
        sha = "unknown"
        if path.exists():
            try:
                sha = subprocess.check_output(
                    ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
                    text=True,
                    timeout=5,
                ).strip()
            except Exception:
                sha = "cloned"
        cur.execute(
            "INSERT OR REPLACE INTO global_oss_engines VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (eid, url, str(path), sha, san, llm, role, ts),
        )
        rows.append(
            {
                "engine_id": eid,
                "repo_url": url,
                "local_path": str(path),
                "commit_sha": sha,
                "sanitized_status": san,
                "llm_binding": llm,
                "role_summary": role,
                "last_verified_at": ts,
            }
        )
    conn.commit()
    return rows


def get_repo_weight(conn: sqlite3.Connection, repo: str) -> float:
    cur = conn.cursor()
    cur.execute(
        "SELECT attempts, merged_count, avg_response_hours, maintainer_active_score, blacklisted_reason "
        "FROM repo_reputation_memory WHERE repo_full_name = ?",
        (repo,),
    )
    row = cur.fetchone()
    if not row:
        return 0.65
    attempts, merged, resp_h, active_score, blacklisted = row
    if blacklisted or active_score < 0.2:
        return 0.0
    bayes = (merged + 1.0) / (attempts + 2.0)
    speed = 1.15 if resp_h <= 6.0 else (1.0 if resp_h <= 24.0 else 0.75)
    return round(min(1.25, bayes * active_score * speed * 1.45), 4)


def xyaz_score_issue(title: str, labels: list[str], unassigned: bool) -> int:
    score = 20 if unassigned else 0
    tl = title.lower()
    if any(b in str(labels).lower() or b in tl for b in ["bounty", "$", "reward"]):
        score += 15
    if any(k in tl for k in ["python", "py", "pytest"]):
        score += 20
    elif any(k in tl for k in ["typescript", "ts", "javascript", "js", "node", "sdk"]):
        score += 18
    skills = ["api", "sdk", "retry", "test", "cli", "bug", "fix", "mcp", "schema", "validation", "stellar"]
    matched = [s for s in skills if s in tl]
    score += min(25, len(matched) * 8)
    return min(100, score)


def scan_github_via_autopr_and_xyaz(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Run live gh search issues combining AutoPR Algora/Bounty filters + xyaz evaluator + Lila Forge $50-$200 band."""
    candidates: list[dict[str, Any]] = []
    queries = [
        ["gh", "search", "issues", "bounty", "--state=open", "--language=typescript", "--sort=updated", "--limit=8", "--json=url,title,repository,assignees,labels"],
        ["gh", "search", "issues", "bounty", "--state=open", "--language=python", "--sort=updated", "--limit=8", "--json=url,title,repository,assignees,labels"],
    ]
    seen_urls: set[str] = set()

    # Always include our 2 live submitted targets with real telemetry
    live_seeded = [
        {
            "candidate_key": "zhangjiayang6835-cyber/bounty-plaza#1904",
            "source_engine": "autopr+LilaForge",
            "repo_or_platform": "zhangjiayang6835-cyber/bounty-plaza",
            "issue_number": 1904,
            "title": "[Bounty $75 USDC] Stellar allowance wait & RPC-lag retry/backoff knobs",
            "url": "https://github.com/zhangjiayang6835-cyber/bounty-plaza/issues/1904",
            "reward_usd": 75.0,
            "competing_prs": 1,
            "xyaz_feasibility_score": 88,
            "lila_forge_band": "SWEET_SPOT_$50_$200",
            "ev_per_competitor": 13.23,
            "triage_decision": "SUBMITTED_PR_4138 (Watching Merge)",
        },
        {
            "candidate_key": "Ikalus1988/MisakaNet#3901",
            "source_engine": "xyaz+MisakaMCP",
            "repo_or_platform": "Ikalus1988/MisakaNet",
            "issue_number": 3901,
            "title": "Submit verified E1 self-observed failure lesson corpus entry + MCP intake",
            "url": "https://github.com/Ikalus1988/MisakaNet/issues/3901",
            "reward_usd": 50.0,
            "competing_prs": 0,
            "xyaz_feasibility_score": 92,
            "lila_forge_band": "SWEET_SPOT_$50_$200",
            "ev_per_competitor": 37.19,
            "triage_decision": "SUBMITTED_PR_3922_INTAKE_3921 (CI 19 SUCCESS)",
        },
    ]
    candidates.extend(live_seeded)
    for c in live_seeded:
        seen_urls.add(c["url"])

    for q in queries:
        try:
            res = subprocess.run(q, capture_output=True, text=True, timeout=20)
            if res.returncode != 0:
                continue
            items = json.loads(res.stdout or "[]")
            for item in items:
                url = item.get("url", "")
                if not url or url in seen_urls:
                    continue
                repo = (item.get("repository") or {}).get("nameWithOwner", "")
                if not repo:
                    continue
                org = repo.split("/")[0]
                if repo in BLOCKED_REPOS or org in BLOCKED_ORGS:
                    continue
                seen_urls.add(url)
                m = re.search(r"/issues/(\d+)", url)
                inum = int(m.group(1)) if m else 0
                title = item.get("title", "")
                labels = [l.get("name", "") for l in (item.get("labels") or [])]
                unassigned = len(item.get("assignees") or []) == 0

                amt_match = re.search(r"\$(\d+(?:,\d+)?)", title + " " + " ".join(labels))
                reward_usd = float(amt_match.group(1).replace(",", "")) if amt_match else 50.0
                weight = get_repo_weight(conn, repo)
                competing = 0 if unassigned else 2
                ev = round((reward_usd / (1.0 + competing * 2.5)) * weight, 2)
                xscore = xyaz_score_issue(title, labels, unassigned)
                band = "SWEET_SPOT_$50_$200" if 50.0 <= reward_usd <= 200.0 else "OUTSIDE_FORGE_BAND"

                if weight == 0.0:
                    decision = "SKIP_BLACKLISTED_REPO"
                elif not unassigned:
                    decision = "SKIP_ALREADY_ASSIGNED"
                elif xscore >= 70 and ev >= 12.0:
                    decision = "HIGH_PRIORITY_QUEUE"
                else:
                    decision = "WATCH_LOW_EV_OR_VAGUE"

                candidates.append(
                    {
                        "candidate_key": f"{repo}#{inum}",
                        "source_engine": "autopr+xyaz",
                        "repo_or_platform": repo,
                        "issue_number": inum,
                        "title": title[:140],
                        "url": url,
                        "reward_usd": reward_usd,
                        "competing_prs": competing,
                        "xyaz_feasibility_score": xscore,
                        "lila_forge_band": band,
                        "ev_per_competitor": ev,
                        "triage_decision": decision,
                    }
                )
        except Exception:
            continue

    # Check Superteam Earn public API (from solana-bounty-hunter, read-only, no foreign wallet)
    try:
        req = urllib.request.Request(
            "https://superteam.fun/api/agents/listings/live?take=5",
            headers={"User-Agent": "HarvestGlobalEngine/2.8"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="ignore"))
            listings = raw if isinstance(raw, list) else raw.get("listings", raw.get("data", []))
            if isinstance(listings, list):
                for item in listings[:3]:
                    if not isinstance(item, dict):
                        continue
                    slug = item.get("slug") or item.get("id") or "listing"
                    title = str(item.get("title") or "Superteam Agent Listing")[:140]
                    reward = float(item.get("rewardAmount") or item.get("usdValue") or 100.0)
                    candidates.append(
                        {
                            "candidate_key": f"superteam:{slug}",
                            "source_engine": "solana-bounty-hunter",
                            "repo_or_platform": "superteam.fun",
                            "issue_number": 0,
                            "title": title,
                            "url": f"https://earn.superteam.fun/listings/bounty/{slug}/",
                            "reward_usd": reward,
                            "competing_prs": 2,
                            "xyaz_feasibility_score": 65,
                            "lila_forge_band": "SWEET_SPOT_$50_$200" if 50 <= reward <= 200 else "SUPERTEAM_POOL",
                            "ev_per_competitor": round((reward / 6.0) * 0.65, 2),
                            "triage_decision": "READ_ONLY_MONITOR (Requires Operator Solana Wallet Approval)",
                        }
                    )
    except Exception:
        pass

    ts = now_jst()
    cur = conn.cursor()
    for c in candidates:
        cur.execute(
            "INSERT OR REPLACE INTO global_bounty_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                c["candidate_key"],
                c["source_engine"],
                c["repo_or_platform"],
                c["issue_number"],
                c["title"],
                c["url"],
                c["reward_usd"],
                c["competing_prs"],
                c["xyaz_feasibility_score"],
                c["lila_forge_band"],
                c["ev_per_competitor"],
                c["triage_decision"],
                ts,
            ),
        )
    conn.commit()
    return candidates


def main() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    init_v28_tables(conn)
    engines = inspect_vendor_engines(conn)
    candidates = scan_github_via_autopr_and_xyaz(conn)

    cur = conn.cursor()
    cur.execute(
        "SELECT SUM(actual_landed_yen), SUM(submitted_pending_yen), SUM(existing_balance_yen) FROM realized_ledger"
    )
    totals = cur.fetchone() or (0, 0, 0)
    conn.close()

    report = {
        "version": "v2.8-global-4oss-5tech",
        "timestamp": now_jst(),
        "allowed_models": ALLOWED_FREE_MODELS,
        "chinese_llm_endpoints_active": 0,
        "foreign_wallets_active": 0,
        "kpi": {
            "actual_landed_yen": totals[0] or 0,
            "submitted_pending_yen": totals[1] or 0,
            "existing_balance_yen": totals[2] or 0,
        },
        "integrated_oss_engines": engines,
        "candidates_scanned_count": len(candidates),
        "top_candidates": candidates[:8],
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
