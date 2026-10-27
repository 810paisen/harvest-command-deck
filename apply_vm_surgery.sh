#!/usr/bin/env bash
# =====================================================================
# /workspace/apply_vm_surgery.sh
# 共有Linux VM 外科手術：旧ハリボテ全停止 -> 新基盤配置確認 -> 起動
# 実行: bash /workspace/apply_vm_surgery.sh
# =====================================================================
set -uo pipefail

WS="${HARVEST_WORKSPACE:-/workspace}"
BRAIN="${HARVEST_BRAIN_REPO:-$WS/brain}"
PORT="${HARVEST_PORT:-8799}"
DB="$WS/.harvest_cfo.db"
STAMP="$(date +%Y%m%d_%H%M%S)"

echo "== [1/8] 旧ハリボテプロセスの全停止 =="
for pat in "spawn_harvest_bots.py" "rog_resident_patrol.py" "harvest_api_server.py" \
           "vm_master_daemon.py" "http.server 8799" "SimpleHTTPServer 8799" "serve -l 8799"; do
  pkill -f "$pat" 2>/dev/null && echo "  killed: $pat" || true
done
if command -v fuser >/dev/null 2>&1; then fuser -k "${PORT}/tcp" 2>/dev/null || true; fi
sleep 1

echo "== [2/8] 必須ファイルの存在確認 =="
MISSING=0
for f in "$WS/vm_master_daemon.py" "$WS/vm_algora_miner.py" "$WS/index.html"; do
  if [ -f "$f" ]; then echo "  OK  $f"; else echo "  MISSING  $f"; MISSING=1; fi
done
[ "$MISSING" = "1" ] && echo "  -> 上の3ファイルを配置してから再実行してください。" && exit 2

echo "== [3/8] Python / gh / git 確認 =="
python3 -V || { echo "python3 がありません"; exit 3; }
command -v gh  >/dev/null 2>&1 && echo "  gh: $(gh --version | head -1)" || echo "  WARN: gh 未導入（Algora採掘が動きません）"
command -v git >/dev/null 2>&1 && echo "  git: $(git --version)" || { echo "git がありません"; exit 3; }
gh auth status >/dev/null 2>&1 && echo "  gh auth: OK" || echo "  WARN: gh 未ログイン -> gh auth login"

echo "== [4/8] DB初期化（既存があればバックアップ） =="
[ -f "$DB" ] && cp -a "$DB" "$DB.bak.$STAMP" && echo "  backup: $DB.bak.$STAMP"
python3 "$WS/vm_master_daemon.py" --init | tee "$WS/.surgery_init.json"
chmod +x "$WS/vm_master_daemon.py" "$WS/vm_algora_miner.py"

echo "== [5/8] brain リポジトリ同期 =="
if [ -d "$BRAIN/.git" ]; then
  git -C "$BRAIN" fetch --all --prune || true
  git -C "$BRAIN" pull --rebase --autostash || true
  echo "  brain: $(git -C "$BRAIN" rev-parse --short HEAD 2>/dev/null)"
else
  echo "  WARN: $BRAIN が git リポジトリではありません。ROG側と共有する場合は:"
  echo "        git clone <your-private-remote> $BRAIN"
fi
mkdir -p "$BRAIN/tasks" "$BRAIN/results" "$BRAIN/evidence" 2>/dev/null || true

echo "== [6/8] master daemon 起動 =="
cd "$WS"
nohup setsid python3 "$WS/vm_master_daemon.py" >> "$WS/harvest_daemon.out" 2>&1 &
echo "  pid: $!"
sleep 2

echo "== [7/8] 死活確認 =="
for i in $(seq 1 15); do
  if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    echo "  /api/health OK"
    curl -fsS "http://127.0.0.1:${PORT}/api/state" | python3 -c "
import json,sys
s=json.load(sys.stdin); m=s['money']
print('  actual_landed_yen      :', m['actual_landed_yen'])
print('  submitted_pending_yen  :', m['submitted_pending_yen'])
print('  existing_balance_yen   :', m['existing_balance_yen'])
print('  monthly_cut_yen        :', m['monthly_cut_yen'])
print('  grok_bot_quota_pct     :', s['quota']['grok_bot_quota_pct'])
"
    break
  fi
  sleep 1
done

echo "== [8/8] systemd user unit（任意・再起動後も自動起動） =="
UNIT_DIR="$HOME/.config/systemd/user"; mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/harvest-master.service" <<EOF
[Unit]
Description=Harvest Master Daemon (VM direct, Grok Bot bypass)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$WS
Environment=HARVEST_WORKSPACE=$WS
Environment=HARVEST_PORT=$PORT
ExecStart=/usr/bin/env python3 $WS/vm_master_daemon.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
echo "  unit: $UNIT_DIR/harvest-master.service"
command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload 2>/dev/null && \
  systemctl --user enable --now harvest-master 2>/dev/null && echo "  systemd: enabled" || \
  echo "  systemd 無効環境 -> nohup起動のみで運用中"

echo
echo "外科手術 完了。Grok Bot を一度も開かずに以下で状態が見えます:"
echo "  curl -s http://127.0.0.1:${PORT}/api/state | head -c 800"
echo "  ブラウザ: http://127.0.0.1:${PORT}/   （index.html を直接配信）"
echo "  Cloudflare Tunnel: cloudflared tunnel --url http://127.0.0.1:${PORT}"
