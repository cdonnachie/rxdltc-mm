import { useCallback, useEffect, useState } from "react";
import { api, enabledTickers, fmtM, fmtNum, type ProcessInfo, type Settings } from "../api";

interface MakerOrder { uuid: string; base: string; rel: string; price: string; max_base_vol: string; available_amount: string; created_at: number; matches: Record<string, unknown> }

export default function KdfPanel({ settings }: { settings: Settings | null }) {
  const [proc, setProc] = useState<ProcessInfo | null>(null);
  const [files, setFiles] = useState<{ configured: boolean; dir?: string; exe?: boolean; mm2_json?: boolean; coins?: boolean } | null>(null);
  const [version, setVersion] = useState<string | null>(null);
  const [coins, setCoins] = useState<string[]>([]);
  const [balances, setBalances] = useState<Record<string, { balance: string; address: string }>>({});
  const [orders, setOrders] = useState<MakerOrder[]>([]);
  const [mm2, setMm2] = useState<string>("");
  const [mm2Dirty, setMm2Dirty] = useState(false);
  const [showSecrets, setShowSecrets] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setProc(await api.kdfProcess());
    setFiles(await api.kdfFiles());
    try {
      const v = (await api.kdfRpc("version")) as { result?: string };
      setVersion(v.result ?? "?");
      const tickers = await enabledTickers();
      setCoins(tickers);
      const bals: Record<string, { balance: string; address: string }> = {};
      for (const t of tickers) {
        try {
          const b = (await api.kdfRpc("my_balance", { coin: t })) as { balance: string; address: string };
          bals[t] = { balance: b.balance, address: b.address };
        } catch { /* ignore */ }
      }
      setBalances(bals);
      const mo = (await api.kdfRpc("my_orders")) as { result?: { maker_orders?: Record<string, MakerOrder> } };
      setOrders(Object.values(mo.result?.maker_orders ?? {}));
    } catch (e) {
      setVersion(null);
      setCoins([]); setBalances({}); setOrders([]);
      void e;
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 5000);
    return () => clearInterval(id);
  }, [refresh]);

  useEffect(() => {
    if (!mm2Dirty) api.mm2Read().then(setMm2).catch(() => setMm2(""));
  }, [files, mm2Dirty]);

  const act = async (fn: () => Promise<unknown>, ok?: string) => {
    setBusy(true); setMsg(null);
    try { const r = await fn(); setMsg({ kind: "ok", text: ok ?? (typeof r === "string" ? r : "done") }); await refresh(); }
    catch (e) { setMsg({ kind: "error", text: String(e) }); }
    finally { setBusy(false); }
  };

  const maskedMm2 = showSecrets ? mm2 : mm2.replace(/("(?:passphrase|rpc_password|wallet_password)"\s*:\s*")([^"]*)(")/g, (_m, a, _v, c) => a + "••••••••" + c);

  return (
    <div className="grid" style={{ gap: 12 }}>
      {msg && <div className={`notice ${msg.kind}`}>{msg.text}</div>}
      <div className="card">
        <h2>Node</h2>
        <div className="row">
          {proc?.running ? <span className="badge green">managed process running (pid {proc.pid})</span> : version ? <span className="badge blue">external node reachable</span> : <span className="badge grey">not reachable</span>}
          {version && <span className="mono small">version {version}</span>}
          <span className="muted small">{settings?.kdf_rpc_url}</span>
          <div className="spacer" style={{ flex: 1 }} />
          {!proc?.running && <button className="primary" disabled={busy || !files?.exe} onClick={() => act(() => api.kdfStart(), "KDF started")}>Start kdf.exe</button>}
          {(proc?.running || version) && <button className="danger" disabled={busy} onClick={() => act(() => api.kdfStop())}>Stop node</button>}
        </div>
        <div className="row" style={{ marginTop: 8 }}>
          <button disabled={busy} onClick={() => act(async () => {
            const r = await api.setupLatestRelease();
            return r.newer
              ? `A newer KDF release exists (${r.latest}, ${r.published_at.slice(0, 10)}); this app is verified against ${r.pinned}. It is not installed automatically: ${r.url}`
              : `KDF is up to date (${r.pinned})`;
          })}>Check for KDF updates</button>
        </div>
        {files && (
          <p className="small muted">
            {files.configured
              ? <>folder {files.dir}: executable {files.exe ? "✓" : "✗"} · MM2.json {files.mm2_json ? "✓" : "✗"} · coins {files.coins ? "✓" : "✗ (download from GLEECBTC/coins)"}</>
              : "KDF folder not set (Settings tab). You can still use an already running node through the RPC URL."}
          </p>
        )}
      </div>
      <div className="grid cols-2">
        <div className="card">
          <h2>Enabled coins</h2>
          {coins.length ? (
            <table>
              <thead><tr><th>coin</th><th className="num">balance</th><th>address</th></tr></thead>
              <tbody>{coins.map((c) => <tr key={c}><td>{c}</td><td className="num">{fmtNum(balances[c]?.balance, 8)}</td><td className="mono small">{balances[c]?.address}</td></tr>)}</tbody>
            </table>
          ) : <span className="muted">none (the bot activates RXD and LTC when it starts)</span>}
        </div>
        <div className="card">
          <h2>Maker orders on this node</h2>
          {orders.length ? (
            <table>
              <thead><tr><th>sells</th><th>for</th><th className="num">price (rel/base)</th><th className="num">RXD/LTC</th><th className="num">left</th><th></th></tr></thead>
              <tbody>{orders.map((o) => {
                const rxdPerLtc = o.base === "RXD" ? 1 / Number(o.price) : Number(o.price);
                return (
                  <tr key={o.uuid}>
                    <td>{o.base}</td><td>{o.rel}</td>
                    <td className="num">{Number(o.price).toPrecision(6)}</td>
                    <td className="num">{fmtM(String(rxdPerLtc))}</td>
                    <td className="num">{fmtNum(o.available_amount, 8)}</td>
                    <td><button className="small" disabled={busy} onClick={() => act(() => api.kdfRpc("cancel_order", { uuid: o.uuid }), "order cancelled")}>cancel</button></td>
                  </tr>
                );
              })}</tbody>
            </table>
          ) : <span className="muted">no maker orders</span>}
          {orders.length > 0 && <div className="row" style={{ marginTop: 8 }}><button className="danger" disabled={busy} onClick={() => act(() => api.kdfRpc("cancel_all_orders", { cancel_by: { type: "All" } }), "all orders cancelled")}>Cancel all orders on node</button><span className="muted small">If the bot is running it will re-quote on its next cycle.</span></div>}
        </div>
      </div>
      <div className="card">
        <h2>MM2.json</h2>
        <p className="small muted">Node configuration: netid + seednodes (GLEEC DEX uses 6133), rpcip/rpcport, rpc_password (must also be stored in Settings), passphrase of the dedicated liquidity wallet. Changes need a node restart.</p>
        <textarea className="code" value={maskedMm2} readOnly={!showSecrets} onChange={(e) => { setMm2(e.target.value); setMm2Dirty(true); }} />
        <div className="row" style={{ marginTop: 8 }}>
          <label style={{ margin: 0 }}><input type="checkbox" checked={showSecrets} onChange={(e) => setShowSecrets(e.target.checked)} style={{ width: "auto", marginRight: 6 }} />show secrets (required to edit)</label>
          <button className="primary" disabled={!mm2Dirty || busy} onClick={() => act(async () => { await api.mm2Write(mm2); setMm2Dirty(false); }, "MM2.json saved (backup: MM2.json.bak)")}>Save MM2.json</button>
          <button disabled={!mm2Dirty} onClick={() => setMm2Dirty(false)}>Discard</button>
        </div>
      </div>
    </div>
  );
}
