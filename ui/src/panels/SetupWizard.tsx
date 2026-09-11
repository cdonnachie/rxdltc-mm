import { useCallback, useEffect, useState } from "react";
import QRCode from "qrcode";
import { parseDocument } from "yaml";
import { api, copyText, enabledTickers, fmtNum, onSetupProgress, type SetupStatus } from "../api";

type Msg = { kind: "ok" | "error" | "warn"; text: string } | null;

const STEPS = ["Workspace", "KDF node", "Wallet", "Backup", "Fund", "Finish"] as const;

function Step({ n, title, done, active, children }: { n: number; title: string; done: boolean; active: boolean; children: React.ReactNode }) {
  return (
    <div className="card" style={{ opacity: active || done ? 1 : 0.55 }}>
      <h2><span className={`badge ${done ? "green" : active ? "blue" : "grey"}`}>{done ? "✓" : n}</span>&nbsp; {title}</h2>
      {children}
    </div>
  );
}

export default function SetupWizard({ onDone, onStartBot }: { onDone: () => void; onStartBot: () => void }) {
  const [st, setSt] = useState<SetupStatus | null>(null);
  const [msg, setMsg] = useState<Msg>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [progress, setProgress] = useState<string>("");
  const [walletMode, setWalletMode] = useState<"create" | "import">("create");
  const [seedInput, setSeedInput] = useState("");
  const [nodeVersion, setNodeVersion] = useState<string | null>(null);
  const [mnemonic, setMnemonic] = useState<string | null>(null);
  const [backedUp, setBackedUp] = useState(false);
  const [checkWords, setCheckWords] = useState<{ idx: number; value: string }[]>([]);
  const [walletDone, setWalletDone] = useState(false);
  const [addresses, setAddresses] = useState<Record<string, { address: string; balance: string; qr: string }>>({});

  const refresh = useCallback(async () => {
    try { setSt(await api.setupStatus()); } catch (e) { setMsg({ kind: "error", text: String(e) }); }
    try {
      const v = (await api.kdfRpc("version")) as { result?: string };
      setNodeVersion(v.result ?? "?");
    } catch { setNodeVersion(null); }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 4000);
    const un = onSetupProgress((p) => setProgress(`${p.step}: ${p.message}`));
    return () => { clearInterval(id); un.then((f) => f()); };
  }, [refresh]);

  const run = async (label: string, fn: () => Promise<unknown>, ok?: string) => {
    setBusy(label); setMsg(null); setProgress("");
    try {
      const r = await fn();
      setMsg({ kind: "ok", text: ok ?? (typeof r === "string" ? r : "done") });
      await refresh();
    } catch (e) {
      setMsg({ kind: "error", text: String(e) });
    } finally { setBusy(null); setProgress(""); }
  };

  const reveal = async () => {
    await run("reveal", async () => {
      const m = await api.walletRevealMnemonic();
      setMnemonic(m);
      const words = m.split(" ");
      const idx = [...new Set([0, 0, 0].map(() => Math.floor(Math.random() * words.length)))].slice(0, 3);
      setCheckWords(idx.map((i) => ({ idx: i, value: "" })));
      return "seed revealed: write it down now";
    });
  };

  const wordsOk = mnemonic !== null && checkWords.length > 0 && checkWords.every((c) => c.value.trim().toLowerCase() === mnemonic.split(" ")[c.idx]);

  const finishWallet = async () => {
    await run("finalize", async () => {
      const removed = await api.setupFinalizeWallet();
      setWalletDone(true);
      setMnemonic(null);
      return removed ? "plaintext seed removed from MM2.json; the node keeps it encrypted" : "wallet setup complete";
    });
  };

  const activateAndShowAddresses = async () => {
    await run("activate", async () => {
      const text = await import("../api").then(() => api.configRead());
      const doc = parseDocument(text);
      const coins = (doc.getIn(["kdf", "coins"]) as { toJSON?: () => Record<string, { electrum?: { url: string; protocol?: string }[] }> } | undefined)?.toJSON?.() ?? {};
      const quote = String(doc.getIn(["pair", "quote"]) ?? "LTC-segwit");
      const enabled = await enabledTickers();
      const out: Record<string, { address: string; balance: string; qr: string }> = {};
      for (const ticker of ["RXD", quote]) {
        let bal: { address: string; balance: string };
        if (!enabled.includes(ticker)) {
          const servers = coins[ticker]?.electrum ?? [];
          if (!servers.length) throw new Error(`no electrum servers for ${ticker} in config.yaml`);
          bal = (await api.kdfRpc("electrum", { coin: ticker, servers, tx_history: false })) as { address: string; balance: string };
        } else {
          bal = (await api.kdfRpc("my_balance", { coin: ticker })) as { address: string; balance: string };
        }
        out[ticker] = { address: bal.address, balance: bal.balance, qr: await QRCode.toDataURL(bal.address, { width: 140, margin: 1 }) };
      }
      setAddresses(out);
      return "coins activated; send funds to the addresses below";
    });
  };

  if (!st) return <div className="muted">Loading…</div>;
  const nodeUp = nodeVersion !== null;
  const done = [
    st.config_exists && st.bot_command_exists,
    st.kdf_exe_exists && st.coins_exists,
    st.mm2_exists && st.rpc_password_stored,
    walletDone || (st.mm2_exists && !st.mm2_has_plaintext_passphrase && st.wallet_password_stored && nodeUp && backedUp),
    Object.keys(addresses).length > 0,
    false,
  ];
  const active = done.findIndex((d) => !d);

  return (
    <div className="grid" style={{ gap: 12, maxWidth: 1000 }}>
      <div className="notice">
        <b>Setup</b> — one-time preparation of a dedicated liquidity wallet and node. Everything is installed under
        <span className="mono"> {st.workspace}</span>. Nothing trades until you press Start bot, and the first start is a dry run.
      </div>
      {msg && <div className={`notice ${msg.kind}`}>{msg.text}</div>}
      {progress && <div className="notice">{progress}</div>}

      <Step n={1} title={STEPS[0]} done={done[0]} active={active === 0}>
        <p className="small">Creates <span className="mono">config.yaml</span> from the shipped defaults (dry run, 1% offsets, NonKYC + CoinGecko + CoinPaprika + Gleec CEX price sources) plus the <span className="mono">kdf</span> and <span className="mono">data</span> folders.</p>
        <p className="small muted">Bot executable: {st.bot_command} {st.bot_command_exists ? "✓" : "✗ (not found)"} {st.bundled_bot ? "· bundled with this app" : "· from the Python venv (Settings tab)"}</p>
        <div className="row"><button className="primary" disabled={busy !== null} onClick={() => run("ws", () => api.setupInitWorkspace(), "workspace ready")}>{st.config_exists ? "Re-check workspace" : "Create workspace"}</button></div>
      </Step>

      <Step n={2} title={STEPS[1]} done={done[1]} active={active === 1}>
        <p className="small">Downloads the pinned GLEEC KDF release <b>{st.kdf_release}</b> from GitHub and verifies its SHA-256 before extracting <span className="mono">{st.kdf_dir}</span>. Then fetches the GLEEC coins file (pinned commit) and checks it contains RXD and LTC-segwit.</p>
        <div className="row">
          <button className="primary" disabled={busy !== null || !st.kdf_download_available} onClick={() => run("kdf", () => api.setupDownloadKdf())}>{st.kdf_exe_exists ? "Re-download & verify KDF" : "Download & verify KDF"}</button>
          <button disabled={busy !== null} onClick={() => run("coins", () => api.setupDownloadCoins())}>{st.coins_exists ? "Re-download coins" : "Download coins file"}</button>
          <span className="small muted">kdf {st.kdf_exe_exists ? "✓" : "✗"} · coins {st.coins_exists ? "✓" : "✗"}{!st.kdf_download_available && " · no pinned build for this platform: copy the kdf binary into the folder yourself"}</span>
        </div>
      </Step>

      <Step n={3} title={STEPS[2]} done={done[2]} active={active === 2}>
        {st.mm2_exists ? (
          <p className="small">MM2.json exists ({st.mm2_wallet_mode} wallet{st.mm2_has_plaintext_passphrase ? ", plaintext passphrase still present" : ""}). RPC password {st.rpc_password_stored ? "stored" : "NOT stored"} in the keychain.</p>
        ) : (
          <>
            <p className="small">Writes MM2.json for GLEEC DEX (netid 6133, GLEEC seed nodes, RPC on 127.0.0.1) with a generated RPC password and a generated wallet password, both stored in the OS keychain.</p>
            <div className="row">
              <label style={{ margin: 0 }}><input type="radio" checked={walletMode === "create"} onChange={() => setWalletMode("create")} style={{ width: "auto", marginRight: 6 }} />Create a new wallet (KDF generates the seed and stores it encrypted)</label>
              <label style={{ margin: 0 }}><input type="radio" checked={walletMode === "import"} onChange={() => setWalletMode("import")} style={{ width: "auto", marginRight: 6 }} />Import an existing seed</label>
            </div>
            {walletMode === "import" && (
              <div style={{ marginTop: 8 }}>
                <label>12 or 24 words. Use a DEDICATED wallet, never your main one. The words are written to MM2.json for the first start only and removed after the node has encrypted them.</label>
                <textarea className="code" style={{ minHeight: 70 }} value={seedInput} onChange={(e) => setSeedInput(e.target.value)} />
              </div>
            )}
            <div className="row" style={{ marginTop: 8 }}>
              <button className="primary" disabled={busy !== null || (walletMode === "import" && !seedInput.trim())} onClick={() => run("mm2", async () => {
                const s = await api.setupWriteMm2(walletMode, walletMode === "import" ? seedInput : undefined);
                setSeedInput("");
                return `MM2.json written (${s.path}); netid ${s.netid}; seed nodes ${s.seednodes.join(", ")}`;
              })}>Generate MM2.json</button>
            </div>
          </>
        )}
      </Step>

      <Step n={4} title={STEPS[3]} done={done[3]} active={active === 3}>
        <p className="small">Start the node, then reveal the seed once and write it down on paper. Anyone with these words controls the funds; the app never stores them.</p>
        <div className="row">
          {nodeUp ? <span className="badge green">node up · {nodeVersion}</span> : <button className="primary" disabled={busy !== null || !st.kdf_exe_exists || !st.mm2_exists} onClick={() => run("kdf-start", () => api.kdfStart(), "node starting; wait for the badge to turn green")}>Start node</button>}
          {nodeUp && !mnemonic && !walletDone && <button disabled={busy !== null || !st.wallet_password_stored} onClick={reveal}>Reveal seed for backup</button>}
        </div>
        {mnemonic && (
          <div style={{ marginTop: 10 }}>
            <div className="notice warn mono" style={{ fontSize: 15, lineHeight: 1.8 }}>
              {mnemonic.split(" ").map((w, i) => <span key={i} style={{ display: "inline-block", width: "25%" }}>{i + 1}. {w}</span>)}
            </div>
            <label style={{ marginTop: 8, color: "var(--text)" }}><input type="checkbox" checked={backedUp} onChange={(e) => setBackedUp(e.target.checked)} style={{ width: "auto", marginRight: 6 }} />I have written all the words down, in order, on paper.</label>
            {backedUp && (
              <div className="row" style={{ marginTop: 8 }}>
                {checkWords.map((c, i) => (
                  <div key={i}><label>word #{c.idx + 1}</label><input value={c.value} onChange={(e) => setCheckWords(checkWords.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))} style={{ width: 140 }} /></div>
                ))}
                <button className="primary" disabled={!wordsOk || busy !== null} onClick={finishWallet}>Confirm backup</button>
              </div>
            )}
          </div>
        )}
        {walletDone && <p className="small">Backup confirmed. {st.mm2_has_plaintext_passphrase ? "" : "MM2.json holds only the encrypted wallet reference."}</p>}
      </Step>

      <Step n={5} title={STEPS[4]} done={done[4]} active={active === 4}>
        <p className="small">Activates RXD and LTC on the node and shows the addresses of this wallet. Send the RXD and LTC you want to quote, in roughly equal value (1 LTC ≈ 2.7M RXD at the time of writing), plus a little extra for fees.</p>
        <div className="row"><button className="primary" disabled={busy !== null || !nodeUp} onClick={activateAndShowAddresses}>{Object.keys(addresses).length ? "Refresh balances" : "Show deposit addresses"}</button></div>
        {Object.entries(addresses).length > 0 && (
          <div className="grid cols-2" style={{ marginTop: 10 }}>
            {Object.entries(addresses).map(([coin, a]) => (
              <div key={coin} className="row" style={{ alignItems: "flex-start" }}>
                <img src={a.qr} alt={`${coin} address`} width={140} height={140} style={{ borderRadius: 6, background: "#fff" }} />
                <div>
                  <div className="muted small">{coin} · balance {fmtNum(a.balance, 8)}</div>
                  <div className="mono small" style={{ wordBreak: "break-all", userSelect: "all" }}>{a.address}</div>
                  <button className="small" style={{ marginTop: 6 }} onClick={() => copyText(a.address)}>copy</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Step>

      <Step n={6} title={STEPS[5]} done={false} active={active === 5 || active === -1}>
        <p className="small">Start the bot in dry run: it computes quotes and logs what it would do without placing anything. Watch a few cycles on the dashboard; switch to live from the Config tab (dry_run off) when you are comfortable.</p>
        <div className="row">
          <button className="primary" disabled={busy !== null || !st.complete} onClick={() => { onDone(); onStartBot(); }}>Start bot (dry run) and open the dashboard</button>
          <button disabled={busy !== null} onClick={onDone}>Go to the dashboard without starting</button>
          {!st.complete && <span className="small muted">complete the steps above first</span>}
        </div>
      </Step>
    </div>
  );
}
