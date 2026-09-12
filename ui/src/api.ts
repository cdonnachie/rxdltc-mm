import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

export interface Settings {
  bot_command: string;
  config_path: string;
  data_dir: string;
  status_url: string;
  kdf_rpc_url: string;
  kdf_dir: string;
  kdf_exe: string;
}

export interface ProcessInfo {
  running: boolean;
  pid: number | null;
  uptime_seconds: number | null;
  exit_code: number | null;
}

export interface OrderInfo {
  uuid: string;
  price_rxd_per_ltc: string;
  amount: string;
  created_at: number;
}

export interface BookLevel {
  price_rxd_per_ltc: string;
  rxd: string;
  mine: boolean;
}

export interface BotStatus {
  reachable: boolean;
  error?: string;
  process: ProcessInfo;
  state?: string;
  state_reason?: string;
  dry_run?: number;
  pair?: string;
  base?: string;
  quote?: string;
  poll_interval_seconds?: number;
  paused?: number;
  paused_reason?: string | null;
  operator_paused?: number;
  resume_in_seconds?: number | null;
  fair_price_rxd_per_ltc?: string | null;
  target_bid_rxd_per_ltc?: string | null;
  target_ask_rxd_per_ltc?: string | null;
  targets_detail?: { skew_pct: string; clamped: boolean; bid_ltc_per_rxd: string | null; ask_ltc_per_rxd: string | null } | null;
  reference_sources?: number;
  reference_disagreement_pct?: string | null;
  reference_reason?: string | null;
  balance_rxd?: string | null;
  balance_ltc?: string | null;
  address_rxd?: string | null;
  address_ltc?: string | null;
  rxd_usd?: string | null;
  ltc_usd?: string | null;
  balance_rxd_usd?: string | null;
  balance_ltc_usd?: string | null;
  inventory_target_pct?: string;
  inventory_min_pct?: string;
  inventory_max_pct?: string;
  inventory_rxd_value_pct?: string | null;
  orders?: { bid: OrderInfo | null; ask: OrderInfo | null };
  orderbook?: {
    foreign_asks: number;
    foreign_bids: number;
    best_foreign_ask_rxd_per_ltc: string | null;
    best_foreign_bid_rxd_per_ltc: string | null;
    asks: BookLevel[];
    bids: BookLevel[];
  } | null;
  last_plan?: { kind: string; side: string; reason: string; uuid: string | null; price_rxd_per_ltc: string | null; amount: string | null }[];
  recent_events?: { ts: number; level: string; kind: string; message: string }[];
  uptime_seconds?: number;
  cycles_total?: number;
  swaps_total?: string;
  swaps_failed_total?: string;
  rxd_bought_total?: string;
  rxd_sold_total?: string;
  ltc_spent_total?: string;
  ltc_received_total?: string;
  two_sided_seconds_total?: string;
  rpc_failures_total?: number;
  order_errors_total?: number;
  providers?: Record<string, { healthy: boolean; consecutive_failures: number; last_error: string | null; price_rxd_per_ltc: string | null }>;
}

export interface SetupStatus {
  workspace: string;
  config_exists: boolean;
  bot_command: string;
  bot_command_exists: boolean;
  bundled_bot: boolean;
  kdf_dir: string;
  kdf_exe_exists: boolean;
  kdf_download_available: boolean;
  kdf_release: string;
  coins_exists: boolean;
  mm2_exists: boolean;
  mm2_wallet_mode: "encrypted" | "plaintext" | null;
  mm2_has_plaintext_passphrase: boolean;
  rpc_password_stored: boolean;
  wallet_password_stored: boolean;
  complete: boolean;
}

export interface Mm2Summary {
  path: string;
  netid: number;
  seednodes: string[];
  wallet_mode: string;
  rpc_password_generated: boolean;
}

export const api = {
  setupStatus: () => invoke<SetupStatus>("setup_status"),
  setupInitWorkspace: () => invoke<Settings>("setup_init_workspace"),
  setupDownloadKdf: () => invoke<string>("setup_download_kdf"),
  setupDownloadCoins: () => invoke<string>("setup_download_coins"),
  setupWriteMm2: (mode: "create" | "import", passphrase?: string) => invoke<Mm2Summary>("setup_write_mm2", { mode, passphrase: passphrase ?? null }),
  setupFinalizeWallet: () => invoke<boolean>("setup_finalize_wallet"),
  setupLatestRelease: () => invoke<{ latest: string; pinned: string; newer: boolean; url: string; published_at: string }>("setup_latest_release"),
  walletRevealMnemonic: () => invoke<string>("wallet_reveal_mnemonic"),
  kdfRpcV2: (method: string, params?: Record<string, unknown>) => invoke<unknown>("kdf_rpc_v2", { method, params: params ?? {} }),
  settingsGet: () => invoke<Settings>("settings_get"),
  settingsSet: (settings: Settings) => invoke<void>("settings_set", { settings }),
  secretStatus: (key: string) => invoke<boolean>("secret_status", { key }),
  secretSet: (key: string, value: string) => invoke<void>("secret_set", { key, value }),
  botStart: (liveConfirmation?: string) => invoke<ProcessInfo>("bot_start", { liveConfirmation: liveConfirmation ?? null }),
  botStop: () => invoke<string>("bot_stop"),
  botProcess: () => invoke<ProcessInfo>("bot_process"),
  botStatus: () => invoke<BotStatus>("bot_status"),
  botControl: (action: "pause" | "resume" | "cancel_all" | "stop" | "clear_events", reason?: string) =>
    invoke<{ ok: boolean; message: string }>("bot_control", { action, reason: reason ?? null }),
  botCheckConfig: () => invoke<{ ok: boolean; output: string }>("bot_check_config"),
  configRead: () => invoke<string>("config_read"),
  configWrite: (text: string) => invoke<void>("config_write", { text }),
  mm2Read: () => invoke<string>("mm2_read"),
  mm2Write: (text: string) => invoke<void>("mm2_write", { text }),
  kdfFiles: () => invoke<{ configured: boolean; dir?: string; exe?: boolean; mm2_json?: boolean; coins?: boolean }>("kdf_files"),
  kdfStart: () => invoke<ProcessInfo>("kdf_start"),
  kdfStop: () => invoke<string>("kdf_stop"),
  kdfProcess: () => invoke<ProcessInfo>("kdf_process"),
  kdfRpc: (method: string, params?: Record<string, unknown>) => invoke<unknown>("kdf_rpc", { method, params: params ?? {} }),
  logsGet: (which: "bot" | "kdf") => invoke<string[]>("logs_get", { which }),
  logsClear: (which: "bot" | "kdf") => invoke<void>("logs_clear", { which }),
  livePhrase: () => invoke<string>("live_phrase"),
};

export function onLog(which: "bot" | "kdf", cb: (line: string) => void): Promise<UnlistenFn> {
  return listen<string>(`${which}-log`, (e) => cb(e.payload));
}

export function onSetupProgress(cb: (p: { step: string; message: string }) => void): Promise<UnlistenFn> {
  return listen<{ step: string; message: string }>("setup-progress", (e) => cb(e.payload));
}

export function onExit(which: "bot" | "kdf", cb: (info: { pid: number; code: number | null }) => void): Promise<UnlistenFn> {
  return listen<{ pid: number; code: number | null }>(`${which}-exit`, (e) => cb(e.payload));
}

// ---- formatting helpers -------------------------------------------------

export function fmtM(v: string | null | undefined): string {
  if (!v) return "–";
  const n = Number(v);
  if (!Number.isFinite(n)) return v;
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(3) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
  return n.toPrecision(4);
}

export function fmtNum(v: string | number | null | undefined, digits = 4): string {
  if (v === null || v === undefined || v === "") return "–";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return n.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export function fmtUsd(v: string | number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || v === "") return "–";
  const n = Number(v);
  if (!Number.isFinite(n)) return "–";
  return "$" + n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function ltcPerRxd(v: string | null | undefined): string {
  if (!v) return "–";
  const n = Number(v);
  return n > 0 ? (1 / n).toExponential(4) : "–";
}

export function fmtDuration(s: number | null | undefined): string {
  if (s === null || s === undefined) return "–";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
  return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${sec}s` : `${sec}s`;
}

export function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString();
}

/** What to add to reach the target RXD share, valued at the fair price (RXD per LTC). */
export function rebalanceHint(s: BotStatus): { addLtc?: number; addRxd?: number; share: number; target: number } | null {
  const rxd = Number(s.balance_rxd), ltc = Number(s.balance_ltc), fair = Number(s.fair_price_rxd_per_ltc);
  const target = Number(s.inventory_target_pct ?? 50) / 100;
  if (!(rxd >= 0) || !(ltc >= 0) || !(fair > 0) || !(target > 0 && target < 1)) return null;
  const rxdValue = rxd / fair;
  const total = rxdValue + ltc;
  if (total <= 0) return null;
  const share = rxdValue / total;
  if (share > target) {
    // add LTC until rxdValue / (rxdValue + ltc') == target
    return { addLtc: rxdValue * (1 - target) / target - ltc, share, target };
  }
  return { addRxd: (ltc * target / (1 - target)) * fair - rxd, share, target };
}

/** Enabled coin tickers. Handles both response shapes KDF uses:
 *  legacy `{"result": [{"ticker": "RXD", "address": "..."}]}` and v2 `{"coins": [{"ticker": "RXD"}]}`. */
export async function enabledTickers(): Promise<string[]> {
  const raw = (await api.kdfRpc("get_enabled_coins")) as { result?: unknown; coins?: unknown };
  const list = Array.isArray(raw.result) ? raw.result
    : Array.isArray((raw.result as { coins?: unknown } | undefined)?.coins) ? (raw.result as { coins: unknown[] }).coins
    : Array.isArray(raw.coins) ? raw.coins : [];
  return (list as { ticker?: string }[]).map((c) => String(c.ticker ?? "")).filter(Boolean);
}

export async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
}
