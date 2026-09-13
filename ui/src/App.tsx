import { useCallback, useEffect, useState } from "react";
import { parseDocument } from "yaml";
import { api, type BotStatus, type Settings, type SetupStatus } from "./api";
import Dashboard from "./panels/Dashboard";
import ConfigEditor from "./panels/ConfigEditor";
import KdfPanel from "./panels/KdfPanel";
import LogView from "./panels/LogView";
import SettingsPanel from "./panels/SettingsPanel";
import SetupWizard from "./panels/SetupWizard";
import HealthChecklist from "./panels/HealthChecklist";
import LiveDialog, { type LiveChoice } from "./panels/LiveDialog";

type Tab = "setup" | "dashboard" | "config" | "kdf" | "logs" | "settings";
const TABS: { id: Tab; label: string }[] = [
  { id: "setup", label: "Setup" },
  { id: "dashboard", label: "Dashboard" },
  { id: "config", label: "Config" },
  { id: "kdf", label: "KDF node" },
  { id: "logs", label: "Logs" },
  { id: "settings", label: "Settings" },
];

export default function App() {
  const [tab, setTab] = useState<Tab | null>(null);
  const [status, setStatus] = useState<BotStatus | null>(null);
  const [setup, setSetup] = useState<SetupStatus | null>(null);
  const [nodeUp, setNodeUp] = useState(false);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "error" | "warn"; text: string } | null>(null);
  const [liveDialog, setLiveDialog] = useState(false);
  const [configDryRun, setConfigDryRun] = useState<boolean | null>(null);
  const [appVersion, setAppVersion] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.botStatus());
    } catch (e) {
      setStatus({ reachable: false, error: String(e), process: { running: false, pid: null, uptime_seconds: null, exit_code: null } });
    }
    try { setSetup(await api.setupStatus()); } catch { /* ignore */ }
    try { await api.kdfRpc("version"); setNodeUp(true); } catch { setNodeUp(false); }
  }, []);

  useEffect(() => {
    api.settingsGet().then(setSettings).catch((e) => setNotice({ kind: "error", text: String(e) }));
    api.appVersion().then(setAppVersion).catch(() => setAppVersion(null));
    (async () => {
      await refresh();
      try {
        const s = await api.setupStatus();
        setTab(s.complete ? "dashboard" : "setup");
      } catch {
        setTab("dashboard");
      }
    })();
    const id = setInterval(refresh, 3000);
    return () => clearInterval(id);
  }, [refresh]);

  // read the config's dry_run flag so the Start button knows whether to ask for the live confirmation
  const readDryRun = useCallback(async () => {
    try {
      const text = await api.configRead();
      const m = text.match(/^\s*dry_run:\s*(true|false)/m);
      setConfigDryRun(m ? m[1] === "true" : true);
    } catch {
      setConfigDryRun(null);
    }
  }, []);
  useEffect(() => { readDryRun(); }, [readDryRun, tab]);

  const run = async (label: string, fn: () => Promise<unknown>, okText?: string) => {
    setBusy(label);
    setNotice(null);
    try {
      const r = await fn();
      if (okText) setNotice({ kind: "ok", text: okText });
      else if (typeof r === "string") setNotice({ kind: "ok", text: r });
      await refresh();
    } catch (e) {
      setNotice({ kind: "error", text: String(e) });
    } finally {
      setBusy(null);
    }
  };

  const startBot = async () => {
    await readDryRun();
    const text = await api.configRead().catch(() => "");
    const dry = !/^\s*dry_run:\s*false/m.test(text);
    if (!dry) {
      setLiveDialog(true);
      return;
    }
    await run("start", () => api.botStart(), "bot started in DRY RUN");
  };

  const startLive = async (choice: LiveChoice) => {
    setLiveDialog(false);
    await run("start", async () => {
      if (choice.startSmall) {
        const doc = parseDocument(await api.configRead());
        doc.setIn(["order_sizing", "mode"], "fixed");
        doc.setIn(["order_sizing", "rxd_order_amount"], 20000);
        doc.setIn(["order_sizing", "ltc_order_amount"], 0.01);
        await api.configWrite(doc.toString());
        const check = await api.botCheckConfig();
        if (!check.ok) throw new Error("config invalid after applying the start-small preset:\n" + check.output);
      }
      return api.botStart(choice.phrase);
    }, choice.startSmall ? "bot started LIVE with small fixed sizes" : "bot started LIVE");
  };

  const running = status?.process.running ?? false;
  const stateBadge = () => {
    if (!running) return <span className="badge grey">NOT RUNNING</span>;
    if (!status?.reachable) return <span className="badge amber">STARTING…</span>;
    const s = status.state ?? "?";
    const cls = s === "ACTIVE" || s === "REPRICING" ? "green" : s === "PAUSED" || s === "WAITING_FOR_REFERENCE" ? "amber" : s === "RPC_ERROR" ? "red" : "grey";
    return <span className={`badge ${cls}`}>{s}</span>;
  };

  if (tab === null) return <div className="content muted">Loading…</div>;

  return (
    <div className="app">
      <div className="topbar">
        <h1>RXD/LTC Liquidity Bot</h1>
        {appVersion && (
          <span className="muted small mono"
                title={status?.bot_version && status.bot_version !== appVersion ? `bot ${status.bot_version} (restart the bot to match the app)` : "app and bot version"}>
            v{appVersion}
            {status?.bot_version && status.bot_version !== appVersion && <span style={{ color: "var(--amber)" }}> · bot v{status.bot_version}</span>}
          </span>
        )}
        {stateBadge()}
        {status?.reachable && (status.dry_run ? <span className="badge blue">DRY RUN</span> : <span className="badge red">LIVE</span>)}
        {status?.reachable && status.paused ? <span className="badge amber small" title={status.paused_reason ?? ""}>{status.operator_paused ? "operator pause" : "auto pause"}</span> : null}
        <div className="spacer" />
        {!running ? (
          <button className="primary" disabled={busy !== null || !settings || (setup !== null && !setup.complete)} title={setup && !setup.complete ? "finish Setup first" : ""} onClick={startBot}>
            Start bot{configDryRun === false ? " (LIVE)" : ""}
          </button>
        ) : (
          <>
            {status?.reachable && !status.paused && (
              <button className="warn" disabled={busy !== null} onClick={() => run("pause", () => api.botControl("pause", "desktop UI"), "pause requested: orders are being cancelled")}>Pause & cancel</button>
            )}
            {status?.reachable && status.paused ? (
              <button disabled={busy !== null} onClick={() => run("resume", () => api.botControl("resume"), "resume requested")}>Resume</button>
            ) : null}
            <button className="danger" disabled={busy !== null} onClick={() => run("stop", () => api.botStop())}>Stop bot</button>
          </>
        )}
      </div>
      <div className="tabs">
        {TABS.map((t) => (
          <button key={t.id} className={tab === t.id ? "active" : ""} onClick={() => setTab(t.id)}>
            {t.label}{t.id === "setup" && setup && !setup.complete ? " •" : ""}
          </button>
        ))}
      </div>
      <div className="content">
        {notice && <div className={`notice ${notice.kind}`} style={{ marginBottom: 12, whiteSpace: "pre-wrap" }}>{notice.text}</div>}
        {tab !== "setup" && <HealthChecklist status={status} setup={setup} nodeUp={nodeUp} onGo={(t) => setTab(t as Tab)} />}
        {tab === "setup" && <SetupWizard onDone={() => { setTab("dashboard"); refresh(); }} onStartBot={startBot} />}
        {tab === "dashboard" && <Dashboard status={status} />}
        {tab === "config" && <ConfigEditor onSaved={readDryRun} botRunning={running} lastReload={status?.last_reload} />}
        {tab === "kdf" && <KdfPanel settings={settings} />}
        {tab === "logs" && <LogView />}
        {tab === "settings" && settings && <SettingsPanel settings={settings} onChange={setSettings} />}
      </div>
      {liveDialog && <LiveDialog onCancel={() => setLiveDialog(false)} onConfirm={startLive} />}
    </div>
  );
}
