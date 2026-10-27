#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 /workspace/vm_algora_miner.py
 共有Linux VM 完結 / OpenRouter 非中国系 0円枠 Algora 確定バウンティ採掘機
=====================================================================
 ・Grok Bot を一切呼ばない（LLMは OpenRouter 0円枠 or /workspace/cheap_brain.py 直叩き）
 ・@algora-pbc エスクロー確定Issueのみ対象（支払い保証のない野良Issueは100%破棄）
 ・既存競合PR 2件以下 / 自動テスト存在 のみ対象
 ・VM内で clone -> patch -> テストPASS(exit 0) -> fork -> push -> PR
 ・結果は /workspace/.harvest_cfo.db の deck_payouts へ書き込み
=====================================================================
 依存 : Python3 標準ライブラリのみ + git + gh CLI
 実行 : python3 /workspace/vm_algora_miner.py --json
=====================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
WORKSPACE   = os.environ.get("HARVEST_WORKSPACE", "/workspace")
DB_PATH     = os.environ.get("HARVEST_DB", os.path.join(WORKSPACE, ".harvest_cfo.db"))
TMP_ROOT    = os.path.join(WORKSPACE, "tmp_bounty")
COUNT_FILE  = os.path.join(WORKSPACE, ".cheap_brain_count.json")
JOB_LOG     = os.path.join(WORKSPACE, "harvest_jobs.log")
DAILY_CAP   = int(os.environ.get("CHEAP_BRAIN_DAILY_CAP", "1000"))

# 非中国系 0円枠のみ（ローテーション）
FREE_MODELS = [
    "cohere/north-mini-code:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "poolside/laguna-s-2.1:free",
]

MIN_USD = float(os.environ.get("MIN_BOUNTY_USD", "50"))
MAX_USD = float(os.environ.get("MAX_BOUNTY_USD", "200"))
MAX_COMPETING_PRS = int(os.environ.get("MAX_COMPETING_PRS", "2"))
USD_JPY = float(os.environ.get("USD_JPY", "150"))


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def jlog(msg: str) -> None:
    line = f"[{now_iso()}] miner :: {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        with open(JOB_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


# =====================================================================
# 0円ブレイン（cheap_brain 直呼び -> サブプロセス -> OpenRouter直叩き）
# =====================================================================
def _bump_count() -> dict:
    today = datetime.now(JST).strftime("%Y-%m-%d")
    data = {"date": today, "count": 0}
    try:
        with open(COUNT_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        pass
    if data.get("date") != today:
        data = {"date": today, "count": 0}
    data["count"] = int(data.get("count", 0)) + 1
    try:
        with open(COUNT_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception:
        pass
    return data


def _count_ok() -> bool:
    try:
        with open(COUNT_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        if d.get("date") != datetime.now(JST).strftime("%Y-%m-%d"):
            return True
        return int(d.get("count", 0)) < DAILY_CAP
    except Exception:
        return True


def _or_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        return key
    for p in (os.path.join(WORKSPACE, ".openrouter_key"),
              os.path.expanduser("~/.openrouter_key")):
        try:
            with open(p, encoding="utf-8") as fh:
                v = fh.read().strip()
            if v:
                return v
        except Exception:
            pass
    return ""


def _openrouter(prompt: str, system: str, max_tokens: int) -> str:
    key = _or_key()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY missing (env or /workspace/.openrouter_key)")
    model = FREE_MODELS[_bump_count()["count"] % len(FREE_MODELS)]
    body = {
        "model": model,
        "messages": ([{"role": "system", "content": system}] if system else [])
                    + [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://harvest-command-deck.pages.dev",
            "X-Title": "harvest-vm-miner",
        },
        method="POST")
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""


def _via_cheap_brain_module(prompt: str, system: str, max_tokens: int):
    if WORKSPACE not in sys.path:
        sys.path.insert(0, WORKSPACE)
    try:
        import cheap_brain  # type: ignore
    except Exception:
        return None
    for name in ("ask", "chat", "complete", "call", "cheap_ask", "query", "generate"):
        fn = getattr(cheap_brain, name, None)
        if not callable(fn):
            continue
        for call in (
            lambda: fn(prompt, system=system, max_tokens=max_tokens),
            lambda: fn(prompt, system=system),
            lambda: fn(prompt),
        ):
            try:
                out = call()
            except TypeError:
                continue
            except Exception:
                continue
            if isinstance(out, str) and out.strip():
                return out
            if isinstance(out, tuple) and out and isinstance(out[0], str):
                return out[0]
            if isinstance(out, dict):
                for k in ("text", "content", "answer", "message"):
                    if isinstance(out.get(k), str) and out[k].strip():
                        return out[k]
    return None


def _via_cheap_brain_subprocess(prompt: str, system: str, max_tokens: int):
    script = os.path.join(WORKSPACE, "cheap_brain.py")
    if not os.path.isfile(script):
        return None
    blob = (system + "\n\n" + prompt) if system else prompt
    for args in (
        [sys.executable, script, "--prompt", blob, "--max-tokens", str(max_tokens)],
        [sys.executable, script, blob],
        [sys.executable, script],
    ):
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=600,
                               input=(None if len(args) > 2 else blob), cwd=WORKSPACE)
        except Exception:
            continue
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout
    return None


def llm(prompt: str, system: str = "", max_tokens: int = 4000) -> str:
    if not _count_ok():
        jlog("cheap brain daily cap reached -> OpenRouter直叩きにフォールバック")
    for tier, fn in (("module", _via_cheap_brain_module),
                     ("subprocess", _via_cheap_brain_subprocess),
                     ("openrouter", _openrouter)):
        try:
            out = fn(prompt, system, max_tokens)
            if out and out.strip():
                jlog(f"llm via {tier}: {len(out)} chars")
                return out
        except Exception as e:
            jlog(f"llm tier {tier} failed: {e}")
    raise RuntimeError("all LLM tiers failed")


def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(text[a:b + 1])
        except Exception:
            pass
    raise ValueError("no JSON object found in model output")


# =====================================================================
# gh / git
# =====================================================================
def gh(args, cwd=None, timeout=300, check=False):
    p = subprocess.run(["gh"] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {p.stderr.strip()[:500]}")
    return p


def gh_json(args, cwd=None):
    p = gh(args, cwd=cwd)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    try:
        return json.loads(p.stdout)
    except Exception:
        return None


def git(args, cwd, timeout=600):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def preflight() -> None:
    if shutil.which("gh") is None:
        raise RuntimeError("gh CLI not found")
    if shutil.which("git") is None:
        raise RuntimeError("git not found")
    st = gh(["auth", "status"])
    if st.returncode != 0:
        raise RuntimeError("gh not authenticated: " + (st.stderr or st.stdout)[:400])
    os.makedirs(TMP_ROOT, exist_ok=True)


# =====================================================================
# バウンティ探索
# =====================================================================
BOT_LOGINS = {"algora-pbc", "algora", "algora-pbc[bot]"}
AMOUNT_RE = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)")


def search_issues(limit: int):
    data = gh_json([
        "search", "issues", "--mentions", "algora-pbc", "--state", "open",
        "--limit", str(limit),
        "--json", "number,title,url,repository,labels,commentsCount,updatedAt",
    ])
    return data or []


def extract_bounty(owner: str, repo: str, number: int):
    data = gh_json(["api", f"repos/{owner}/{repo}/issues/{number}/comments",
                    "--paginate", "-q", ".[] | {login: .user.login, body: .body}"])
    if not data:
        raw = gh(["api", f"repos/{owner}/{repo}/issues/{number}/comments", "--paginate"])
        if raw.returncode != 0:
            return None
        try:
            data = [{"login": c.get("user", {}).get("login", ""), "body": c.get("body", "")}
                    for c in json.loads(raw.stdout)]
        except Exception:
            return None
    if isinstance(data, dict):
        data = [data]
    for c in data:
        if (c.get("login") or "").lower() in BOT_LOGINS:
            body = c.get("body") or ""
            if "$" in body and ("bounty" in body.lower() or "💰" in body):
                m = AMOUNT_RE.search(body)
                if m:
                    return float(m.group(1).replace(",", "")), body[:600]
    return None


def count_competing_prs(owner: str, repo: str, number: int) -> int:
    seen = set()
    for match_field in ("body", "title"):
        data = gh_json(["search", "prs", "--repo", f"{owner}/{repo}", "--state", "open",
                        "--match", match_field, f"#{number}", "--limit", "30", "--json", "url"])
        for item in (data or []):
            seen.add(item.get("url"))
    return len(seen)


def repo_has_tests(owner: str, repo: str) -> bool:
    for path in (".github/workflows", "pytest.ini", "tox.ini", "package.json",
                 "Cargo.toml", "go.mod", "pyproject.toml", "Makefile"):
        p = gh(["api", f"repos/{owner}/{repo}/contents/{path}", "-q", ".[].name"])
        if p.returncode == 0 and p.stdout.strip():
            if path == ".github/workflows":
                return True
            if path in ("pytest.ini", "tox.ini", "Cargo.toml", "go.mod"):
                return True
            if path in ("package.json", "pyproject.toml", "Makefile"):
                return True
    return False


# =====================================================================
# パッチ生成
# =====================================================================
SKIP_DIRS = ("node_modules/", "dist/", "build/", ".git/", "vendor/", "__pycache__/")
SKIP_EXT = (".lock", ".min.js", ".png", ".jpg", ".svg", ".ico", ".snap")


def candidate_files(repo_dir: str, issue_text: str, limit: int = 18):
    p = git(["ls-files"], repo_dir)
    files = [f for f in p.stdout.splitlines()
             if f and not any(d in f for d in SKIP_DIRS) and not f.endswith(SKIP_EXT)]
    mentioned = [f for f in files if os.path.basename(f) in issue_text]
    scored = []
    for f in files:
        s = 0
        if f in mentioned:
            s += 100
        if f.startswith(("src/", "lib/", "pkg/", "app/")):
            s += 20
        if f.endswith((".ts", ".tsx", ".js", ".py", ".rs", ".go")):
            s += 10
        if f.startswith(("tests/", "test/")) or "/test" in f:
            s += 5
        if "readme" in f.lower():
            s += 2
        scored.append((s, f))
    scored.sort(reverse=True)
    return [f for _, f in scored[:limit]]


def read_file(repo_dir: str, rel: str, limit: int = 9000) -> str:
    try:
        with open(os.path.join(repo_dir, rel), encoding="utf-8", errors="replace") as fh:
            return fh.read()[:limit]
    except Exception:
        return ""


def build_prompt(owner, repo, number, title, body, comments_text, files_ctx) -> str:
    return f"""You are fixing a real GitHub issue in the repository {owner}/{repo}.

ISSUE #{number}: {title}

ISSUE BODY:
{body[:6000]}

RELEVANT COMMENTS:
{comments_text[:2500]}

FILES (current content):
{files_ctx}

TASK:
Produce a minimal, correct patch that resolves the issue.

Return STRICT JSON ONLY, no prose, in exactly this shape:
{{
  "files": {{ "relative/path.py": "FULL new content of the file", "...": "..." }},
  "test_cmd": "shell command that must exit 0 (e.g. python3 -m pytest -q)",
  "summary": "one paragraph summary of the change"
}}

RULES:
- Only include files you actually modify.
- Content must be the COMPLETE file after your change, not a diff.
- Do not reformat unrelated code.
- Keep the change minimal and idiomatic for the repo.
- test_cmd must be runnable from the repo root.
"""


def detect_test_cmd(repo_dir: str):
    pkg = os.path.join(repo_dir, "package.json")
    if os.path.isfile(pkg):
        try:
            with open(pkg, encoding="utf-8") as fh:
                scripts = (json.load(fh) or {}).get("scripts", {})
            if scripts.get("test"):
                return ("npm ci --no-audit --no-fund && npm test"
                        if os.path.isfile(os.path.join(repo_dir, "package-lock.json"))
                        else "npm install --no-audit --no-fund && npm test")
        except Exception:
            pass
    if os.path.isfile(os.path.join(repo_dir, "Cargo.toml")):
        return "cargo test --quiet"
    if os.path.isfile(os.path.join(repo_dir, "go.mod")):
        return "go test ./..."
    if (os.path.isfile(os.path.join(repo_dir, "pytest.ini"))
            or os.path.isfile(os.path.join(repo_dir, "tox.ini"))
            or os.path.isdir(os.path.join(repo_dir, "tests"))):
        return "python3 -m pytest -q"
    return "python3 -m pytest -q"


def apply_files(repo_dir: str, files_map: dict) -> list:
    written = []
    for rel, content in files_map.items():
        rel = rel.lstrip("/").replace("\\", "/")
        if not rel or ".." in rel:
            continue
        full = os.path.join(repo_dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content if content.endswith("\n") else content + "\n")
        written.append(rel)
    return written


def run_tests(repo_dir: str, cmd: str, timeout: int = 1800):
    env = dict(os.environ)
    env["CI"] = "1"
    try:
        p = subprocess.run(cmd, cwd=repo_dir, shell=True, capture_output=True,
                           text=True, timeout=timeout, env=env)
        return p.returncode, (p.stdout or "")[-6000:] + (p.stderr or "")[-3000:]
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"


# =====================================================================
# DB書き込み
# =====================================================================
def db_write_payout(url: str, title: str, amount_yen: float, note: str, source="algora"):
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        row = con.execute("SELECT id FROM deck_payouts WHERE url=?", (url,)).fetchone()
        if row:
            con.execute("UPDATE deck_payouts SET status='pending_review', note=?, updated_at=? WHERE id=?",
                        (note, now_iso(), row[0]))
            pid = row[0]
        else:
            cur = con.execute(
                "INSERT INTO deck_payouts(source,title,url,amount_yen,status,note,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (source, title, url, amount_yen, "pending_review", note, now_iso(), now_iso()))
            pid = cur.lastrowid
        con.commit()
        return pid
    finally:
        con.close()


# =====================================================================
# メイン処理
# =====================================================================
def process_issue(issue: dict, dry_run: bool) -> dict:
    repo_full = issue["repository"]["nameWithOwner"]
    owner, repo = repo_full.split("/", 1)
    number = issue["number"]
    title = issue["title"]
    url = issue["url"]

    jlog(f"--- candidate {repo_full}#{number}: {title[:80]}")

    bounty = extract_bounty(owner, repo, number)
    if not bounty:
        return {"repo": repo_full, "number": number, "skip": "no @algora-pbc escrow amount"}
    usd, btext = bounty
    if not (MIN_USD <= usd <= MAX_USD):
        return {"repo": repo_full, "number": number, "skip": f"amount ${usd} out of range"}

    comp = count_competing_prs(owner, repo, number)
    if comp > MAX_COMPETING_PRS:
        return {"repo": repo_full, "number": number, "skip": f"{comp} competing PRs"}

    if not repo_has_tests(owner, repo):
        return {"repo": repo_full, "number": number, "skip": "no automated tests"}

    slug = f"{owner}__{repo}__{number}"
    repo_dir = os.path.join(TMP_ROOT, slug)
    if os.path.isdir(repo_dir):
        shutil.rmtree(repo_dir, ignore_errors=True)

    clone = subprocess.run(
        ["git", "clone", "--depth", "50", f"https://github.com/{repo_full}.git", repo_dir],
        capture_output=True, text=True, timeout=900)
    if clone.returncode != 0:
        return {"repo": repo_full, "number": number, "skip": "clone failed",
                "err": clone.stderr[-400:]}

    branch = f"venice-fix-{number}"
    git(["checkout", "-b", branch], repo_dir)

    body_p = gh(["api", f"repos/{repo_full}/issues/{number}", "-q", ".body"])
    body = body_p.stdout or ""
    issue_text = title + "\n" + body

    files = candidate_files(repo_dir, issue_text)
    files_ctx = "\n\n".join(f"===== {f} =====\n{read_file(repo_dir, f)}" for f in files)

    test_cmd = None
    written = []
    last_out = ""
    for attempt in range(1, 4):
        prompt = build_prompt(owner, repo, number, title, body,
                              last_out[-2500:] if last_out else "", files_ctx)
        raw = llm(prompt, system="You are a precise senior engineer. Output JSON only.", max_tokens=8000)
        try:
            plan = extract_json(raw)
        except Exception as e:
            jlog(f"attempt {attempt}: JSON parse failed ({e})")
            continue
        written = apply_files(repo_dir, plan.get("files") or {})
        if not written:
            jlog(f"attempt {attempt}: no files written")
            continue
        test_cmd = plan.get("test_cmd") or detect_test_cmd(repo_dir)
        rc, last_out = run_tests(repo_dir, test_cmd)
        jlog(f"attempt {attempt}: test '{test_cmd}' -> exit {rc} / files {written}")
        if rc == 0:
            break

    if not written or test_cmd is None:
        return {"repo": repo_full, "number": number, "skip": "no patch produced"}

    rc, last_out = run_tests(repo_dir, test_cmd)
    if rc != 0:
        return {"repo": repo_full, "number": number, "skip": f"tests failed exit {rc}",
                "test_cmd": test_cmd, "out": last_out[-800:]}

    diff = git(["diff", "--stat"], repo_dir).stdout[-500:]
    if dry_run:
        return {"repo": repo_full, "number": number, "dry_run": True,
                "test_cmd": test_cmd, "files": written, "diffstat": diff, "usd": usd}

    # commit
    git(["config", "user.email", os.environ.get("GIT_EMAIL", "810paisen@users.noreply.github.com")], repo_dir)
    git(["config", "user.name", os.environ.get("GIT_NAME", "810paisen")], repo_dir)
    git(["add", "-A"], repo_dir)
    git(["commit", "-s", "-m",
         f"fix({repo}): resolve #{number} ({title[:60]})\n\nSigned-off-by: 810paisen <810paisen@users.noreply.github.com>"],
        repo_dir)

    fork = gh(["repo", "fork", repo_full, "--clone=false", "--remote=false"])
    if fork.returncode != 0:
        return {"repo": repo_full, "number": number, "skip": "fork failed", "err": fork.stderr[-400:]}
    time.sleep(3)

    me = gh(["api", "user", "-q", ".login"]).stdout.strip() or "810paisen"
    git(["remote", "add", "fork", f"https://github.com/{me}/{repo}.git"], repo_dir)
    push = git(["push", "-u", "fork", branch], repo_dir, timeout=900)
    if push.returncode != 0:
        return {"repo": repo_full, "number": number, "skip": "push failed", "err": push.stderr[-400:]}

    pr_body = (f"Fixes #{number}\n\n"
               f"Auto-generated on a shared Linux VM (0円枠 LLM, no chat quota used).\n\n"
               f"**Test**: `{test_cmd}` -> exit 0\n\n"
               f"```\n{diff}\n```\n")
    pr = gh(["pr", "create", "--repo", repo_full, "--head", f"{me}:{branch}",
             "--title", f"fix({repo}): resolve #{number}", "--body", pr_body])
    if pr.returncode != 0:
        return {"repo": repo_full, "number": number, "skip": "pr create failed", "err": pr.stderr[-500:]}
    pr_url = (pr.stdout or "").strip().splitlines()[-1]

    amount_yen = round(usd * USD_JPY, 0)
    pid = db_write_payout(pr_url, f"{repo_full} #{number}: {title[:90]}", amount_yen,
                          f"algora escrow ${usd} / test {test_cmd} PASS / competing<={comp}")
    jlog(f"PR created: {pr_url} (${usd} -> {amount_yen}円) payout_id={pid}")
    return {"repo": repo_full, "number": number, "pr": pr_url, "usd": usd,
            "amount_yen": amount_yen, "payout_id": pid, "test_cmd": test_cmd, "files": written}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--max-prs", type=int, default=3)
    ap.add_argument("--repo-filter", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = {"ok": True, "started": now_iso(), "results": [], "prs": []}
    try:
        preflight()
    except Exception as e:
        report["ok"] = False
        report["error"] = str(e)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    issues = search_issues(args.limit)
    jlog(f"search returned {len(issues)} issues mentioning @algora-pbc")
    made = 0
    for issue in issues:
        if made >= args.max_prs:
            break
        rf = args.repo_filter
        if rf and rf.lower() not in issue["repository"]["nameWithOwner"].lower():
            continue
        try:
            r = process_issue(issue, args.dry_run)
        except Exception:
            r = {"repo": issue["repository"]["nameWithOwner"], "number": issue["number"],
                 "error": traceback.format_exc()[-900:]}
        report["results"].append(r)
        if r.get("pr"):
            report["prs"].append(r["pr"])
            made += 1

    report["finished"] = now_iso()
    report["prs_created"] = made
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
