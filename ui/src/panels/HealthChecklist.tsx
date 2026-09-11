import { type BotStatus, type SetupStatus } from "../api";

interface Check { label: string; ok: boolean | null; detail: string; tab?: string }

export default function HealthChecklist({ status, setup, nodeUp, onGo }: { status: BotStatus | null; setup: SetupStatus | null; nodeUp: boolean; onGo: (tab: string) => void }) {
  const s = status;
  const reachable = !!s?.reachable;
  const providers = s?.providers ? Object.values(s.providers) : [];
  const healthy = providers.filter((p) => p.healthy).length;
  const rxd = Number(s?.balance_rxd ?? NaN), ltc = Number(s?.balance_ltc ?? NaN);
  const checks: Check[] = [
    { label: "setup", ok: setup ? setup.complete : null, detail: setup?.complete ? "workspace, node, wallet, config" : "incomplete", tab: "setup" },
    { label: "KDF node", ok: nodeUp, detail: nodeUp ? "RPC answering" : "not reachable", tab: "kdf" },
    { label: "bot", ok: reachable ? true : s?.process.running ? null : false, detail: reachable ? `${s?.state}` : s?.process.running ? "starting" : "not running" },
    { label: "funds", ok: reachable ? rxd > 0 && ltc > 0 : null, detail: reachable ? `${isFinite(rxd) ? rxd.toFixed(0) : "?"} RXD · ${isFinite(ltc) ? ltc.toFixed(4) : "?"} LTC` : "unknown" },
    { label: "price sources", ok: reachable ? (s?.reference_sources ?? 0) >= 2 : null, detail: reachable ? `${s?.reference_sources ?? 0} valid · ${healthy}/${providers.length} healthy` : "unknown" },
    { label: "quoting", ok: reachable ? !s?.paused : null, detail: reachable ? (s?.paused ? `paused: ${s.paused_reason}` : s?.dry_run ? "dry run" : "live") : "unknown" },
    { label: "single instance", ok: reachable ? !(s?.paused_reason ?? "").includes("same seed") : null, detail: (s?.paused_reason ?? "").includes("same seed") ? "another wallet on this seed is running" : "ok" },
  ];
  return (
    <div className="row" style={{ gap: 6, marginBottom: 12 }}>
      {checks.map((c) => (
        <button key={c.label} className={`badge ${c.ok === true ? "green" : c.ok === false ? "red" : "grey"}`} style={{ border: 0, cursor: c.tab ? "pointer" : "default" }} title={c.detail} onClick={() => c.tab && onGo(c.tab)}>
          {c.ok === true ? "✓" : c.ok === false ? "✗" : "…"} {c.label}
        </button>
      ))}
    </div>
  );
}
