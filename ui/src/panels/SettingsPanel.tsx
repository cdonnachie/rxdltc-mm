import { useEffect, useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import { api, type Settings } from "../api";

export default function SettingsPanel({ settings, onChange }: { settings: Settings; onChange: (s: Settings) => void }) {
  const [draft, setDraft] = useState<Settings>(settings);
  const [pw, setPw] = useState("");
  const [pwStored, setPwStored] = useState<boolean | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  useEffect(() => { setDraft(settings); }, [settings]);
  useEffect(() => { api.secretStatus("KDF_RPC_PASSWORD").then(setPwStored).catch(() => setPwStored(null)); }, []);

  const set = (k: keyof Settings) => (e: React.ChangeEvent<HTMLInputElement>) => setDraft({ ...draft, [k]: e.target.value });
  const pick = async (k: keyof Settings, directory: boolean, filters?: { name: string; extensions: string[] }[]) => {
    const chosen = await open({ directory, multiple: false, filters });
    if (typeof chosen === "string") setDraft({ ...draft, [k]: chosen });
  };
  const save = async () => {
    try {
      await api.settingsSet(draft);
      onChange(draft);
      setMsg({ kind: "ok", text: "settings saved" });
    } catch (e) { setMsg({ kind: "error", text: String(e) }); }
  };
  const savePw = async () => {
    try {
      await api.secretSet("KDF_RPC_PASSWORD", pw);
      setPwStored(pw !== "");
      setPw("");
      setMsg({ kind: "ok", text: pw ? "RPC password stored in the OS keychain" : "RPC password removed" });
    } catch (e) { setMsg({ kind: "error", text: String(e) }); }
  };

  return (
    <div className="grid" style={{ gap: 12, maxWidth: 980 }}>
      {msg && <div className={`notice ${msg.kind}`}>{msg.text}</div>}
      <div className="card">
        <h2>Bot</h2>
        <div className="form">
          <div className="full">
            <label>Bot command (the rxdltc-mm executable from the Python venv)</label>
            <div className="row"><input value={draft.bot_command} onChange={set("bot_command")} /><button onClick={() => pick("bot_command", false)}>…</button></div>
          </div>
          <div className="full">
            <label>Config file (config.yaml)</label>
            <div className="row"><input value={draft.config_path} onChange={set("config_path")} /><button onClick={() => pick("config_path", false, [{ name: "YAML", extensions: ["yaml", "yml"] }])}>…</button></div>
          </div>
          <div>
            <label>Data directory (SQLite state; empty = next to the config)</label>
            <div className="row"><input value={draft.data_dir} onChange={set("data_dir")} /><button onClick={() => pick("data_dir", true)}>…</button></div>
          </div>
          <div>
            <label>Bot status URL (metrics.bind/port in the config)</label>
            <input value={draft.status_url} onChange={set("status_url")} />
          </div>
        </div>
      </div>
      <div className="card">
        <h2>KDF node</h2>
        <div className="form">
          <div className="full">
            <label>KDF folder (contains kdf.exe, MM2.json and the coins file)</label>
            <div className="row"><input value={draft.kdf_dir} onChange={set("kdf_dir")} /><button onClick={() => pick("kdf_dir", true)}>…</button></div>
          </div>
          <div>
            <label>KDF executable name</label>
            <input value={draft.kdf_exe} onChange={set("kdf_exe")} />
          </div>
          <div>
            <label>KDF RPC URL</label>
            <input value={draft.kdf_rpc_url} onChange={set("kdf_rpc_url")} />
          </div>
          <div className="full">
            <label>KDF RPC password (rpc_password in MM2.json) — stored in the OS keychain, never in a file {pwStored === true ? "· currently stored" : pwStored === false ? "· not stored yet" : ""}</label>
            <div className="row"><input type="password" value={pw} onChange={(e) => setPw(e.target.value)} placeholder={pwStored ? "•••••••• (enter a new value to replace)" : "enter the rpc_password"} /><button onClick={savePw}>Store</button></div>
          </div>
        </div>
      </div>
      <div className="row"><button className="primary" onClick={save}>Save settings</button></div>
    </div>
  );
}
