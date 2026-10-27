/**
 * Cloudflare Pages Function: GET /api/state
 * Returns live surgical state compatible with Venice index.html & vm_master_daemon.py
 */
const CORS_HEADERS = {
  "Content-Type": "application/json; charset=utf-8",
  "Cache-Control": "no-store, no-cache, must-revalidate",
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With",
};

function nowIsoJst() {
  const d = new Date(Date.now() + 9 * 3600 * 1000);
  return d.toISOString().slice(0, 19) + "+09:00";
}

const DEFAULT_PAYOUTS = [
  {
    id: 1,
    source: "algora",
    title: "bounty-plaza #1904: Stellar retry/backoff knobs ($75 USDC)",
    url: "https://github.com/zhangjiayang6835-cyber/bounty-plaza/pull/4138",
    amount_yen: 11250.0,
    status: "pending_review",
    note: "Algoraエスクローなし野良repo / マージ&着金まで確定0円。Issue #1904 が正",
    created_at: "2026-10-27T12:34:07+09:00",
    updated_at: "2026-10-27T12:34:07+09:00",
  },
  {
    id: 2,
    source: "misakanet",
    title: "MisakaNet #3901: lesson_gate.py missing-PyYAML cascade",
    url: "https://github.com/Ikalus1988/MisakaNet/pull/3922",
    amount_yen: 0.0,
    status: "pending_review",
    note: "直接金銭報酬なし / fork初回PRでworkflow承認待ち / needs-human-review",
    created_at: "2026-10-27T12:34:07+09:00",
    updated_at: "2026-10-27T12:34:07+09:00",
  },
];

const DEFAULT_GATES = [
  {
    id: 1,
    gate_key: "gate_sofmap_cancel",
    label: "SOFMAP TEIKI EKIMU 解約",
    status: "open",
    detail: "0077-78-9988 または会員ページ / 10-31までに解約で翌月以降0円",
    deadline: "2026-10-31",
    updated_at: "2026-10-27T12:34:07+09:00",
  },
  {
    id: 2,
    gate_key: "gate_google_ai_pro_cancel",
    label: "Google AI Pro (5TB) 解約",
    status: "open",
    detail: "Mac システム設定 > Apple Account > メディアと購入 > サブスクリプション / 注文ID MMGH3D5MXX",
    deadline: "2026-11-18",
    updated_at: "2026-10-27T12:34:07+09:00",
  },
  {
    id: 3,
    gate_key: "gate_olive_grant",
    label: "Olive 特典 40,000pt 付与確認",
    status: "open",
    detail: "2026-11-30 付与予定 / 付与確認後に existing へ反映",
    deadline: "2026-11-30",
    updated_at: "2026-10-27T12:34:07+09:00",
  },
  {
    id: 4,
    gate_key: "gate_rakuten_unlock",
    label: "楽天アカウントロック解除",
    status: "open",
    detail: "Case 81334359 / 返信下書き draft_id r8345770108746311809 作成済 / 本人確認書類添付",
    deadline: "",
    updated_at: "2026-10-27T12:34:07+09:00",
  },
  {
    id: 5,
    gate_key: "gate_rog_2fa",
    label: "ROG Chrome 2FA 手動突破",
    status: "open",
    detail: "WAITING_2FA で停止したら人が1回だけ通す",
    deadline: "",
    updated_at: "2026-10-27T12:34:07+09:00",
  },
];

const DEFAULT_STATE_ITEMS = [
  { key: "existing_beams_miles", value: 23300.0, label: "BEAMS マイル（既存・円非等価）", bucket: "existing" },
  { key: "existing_beams_pt", value: 432.0, label: "BEAMS CLUB pt（既存残高）", bucket: "existing" },
  { key: "existing_mynb_pt", value: 1405.0, label: "myNB ポイント（既存残高）", bucket: "existing" },
  { key: "existing_rakuten_cash", value: 31797.0, label: "楽天キャッシュ（既存残高）", bucket: "existing" },
  { key: "existing_rakuten_pt", value: 23764.0, label: "楽天ポイント（既存残高）", bucket: "existing" },
  { key: "existing_tria_sol", value: 56528.0, label: "Tria / SOL 相当（既存残高）", bucket: "existing" },
  { key: "sub_google_ai_pro_monthly", value: 2900.0, label: "Google AI Pro 5TB 解約ターゲット（月額）", bucket: "subs" },
  { key: "sub_sofmap_monthly", value: 758.0, label: "SOFMAP TEIKI EKIMU 解約ターゲット（月額）", bucket: "subs" },
];

export async function onRequestOptions() {
  return new Response(null, { status: 204, headers: CORS_HEADERS });
}

export async function onRequestGet() {
  return new Response(
    JSON.stringify({
      ok: true,
      version: "2026.10.27-surgery-v1",
      now: nowIsoJst(),
      money: {
        actual_landed_yen: 0.0,
        submitted_pending_yen: 11250.0,
        gate_hold_yen: 0.0,
        existing_balance_yen: 137226.0,
        monthly_cut_yen: 3658.0,
        annual_cut_yen: 43896.0,
      },
      payouts: DEFAULT_PAYOUTS,
      gates: DEFAULT_GATES,
      state_items: DEFAULT_STATE_ITEMS,
      jobs: [],
      logs: [],
      rog_events: [],
      quota: {
        grok_bot_quota_pct: 0.0,
        note: "チャット/Task/SendToAgent 未使用。司令塔は VM master daemon + Git + ROG agy。",
      },
    }),
    { status: 200, headers: CORS_HEADERS }
  );
}
