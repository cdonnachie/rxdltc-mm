import { useCallback, useEffect, useState } from "react";
import { parseDocument, type Document } from "yaml";
import { api } from "../api";

type FieldType = "number" | "bool" | "select" | "text";
interface Field { path: string; label: string; type: FieldType; options?: string[]; hint?: string }
interface Section { title: string; fields: Field[] }

const SECTIONS: Section[] = [
  { title: "Mode", fields: [
    { path: "dry_run", label: "Dry run (log only, never place/cancel)", type: "bool" },
    { path: "pair.quote", label: "LTC ticker (LTC-segwit = ltc1… address, LTC = legacy)", type: "select", options: ["LTC-segwit", "LTC"] },
    { path: "poll_interval_seconds", label: "Poll interval (s)", type: "number" },
  ]},
  { title: "Pricing (RXD per LTC; bid = fair × (1+offset), ask = fair × (1−offset))", fields: [
    { path: "pricing.bid_offset_pct", label: "Bid offset % (bot buys RXD)", type: "number" },
    { path: "pricing.ask_offset_pct", label: "Ask offset % (bot sells RXD)", type: "number" },
    { path: "pricing.min_edge_pct", label: "Minimum edge from fair %", type: "number" },
    { path: "pricing.min_valid_providers", label: "Min valid price providers", type: "number" },
    { path: "pricing.max_provider_disagreement_pct", label: "Max provider disagreement %", type: "number" },
    { path: "pricing.max_source_age_seconds", label: "Max source-reported age (s)", type: "number" },
  ]},
  { title: "Repricing", fields: [
    { path: "reprice_threshold_pct", label: "Reprice threshold %", type: "number" },
    { path: "resize_threshold_pct", label: "Resize threshold %", type: "number" },
    { path: "minimum_order_lifetime_seconds", label: "Minimum order lifetime (s)", type: "number" },
  ]},
  { title: "Inventory", fields: [
    { path: "inventory.target_rxd_value_pct", label: "Target RXD share %", type: "number" },
    { path: "inventory.min_rxd_value_pct", label: "Min RXD share % (below: no ask)", type: "number" },
    { path: "inventory.max_rxd_value_pct", label: "Max RXD share % (above: no bid)", type: "number" },
    { path: "inventory_skew.enabled", label: "Inventory skew enabled", type: "bool" },
    { path: "inventory_skew.max_adjustment_pct", label: "Max skew adjustment %", type: "number" },
  ]},
  { title: "Sizing & reserve", fields: [
    { path: "order_sizing.mode", label: "Sizing mode", type: "select", options: ["balance_percent", "fixed"] },
    { path: "order_sizing.rxd_balance_percent", label: "RXD balance % per ask", type: "number" },
    { path: "order_sizing.ltc_balance_percent", label: "LTC balance % per bid", type: "number" },
    { path: "order_sizing.rxd_order_amount", label: "Fixed ask size (RXD)", type: "number" },
    { path: "order_sizing.ltc_order_amount", label: "Fixed bid size (LTC)", type: "number" },
    { path: "order_sizing.min_rxd_order_amount", label: "Min ask size (RXD)", type: "number" },
    { path: "order_sizing.min_ltc_order_amount", label: "Min bid size (LTC)", type: "number" },
    { path: "reserve.rxd_percent", label: "RXD reserve %", type: "number" },
    { path: "reserve.ltc_percent", label: "LTC reserve %", type: "number" },
  ]},
  { title: "Safety", fields: [
    { path: "safety.max_price_move_pct", label: "Max price move %", type: "number" },
    { path: "safety.max_price_move_window_seconds", label: "Price move window (s)", type: "number" },
    { path: "safety.max_quote_deviation_pct", label: "Max quote deviation from fair %", type: "number" },
    { path: "safety.rpc_failure_limit", label: "RPC failure limit", type: "number" },
    { path: "safety.order_error_limit", label: "Order error limit", type: "number" },
    { path: "safety.cooldown_seconds", label: "Pause cooldown (s)", type: "number" },
    { path: "safety.recovery_seconds", label: "Recovery period (s)", type: "number" },
    { path: "trading.prevent_crossing_book", label: "Prevent crossing the book", type: "bool" },
    { path: "trading.on_cross", label: "On cross", type: "select", options: ["skip", "adjust"] },
    { path: "shutdown.cancel_orders_on_exit", label: "Cancel orders on exit", type: "bool" },
  ]},
];

function getPath(doc: Document, path: string): unknown {
  return doc.getIn(path.split("."), false);
}

export default function ConfigEditor({ onSaved, botRunning }: { onSaved: () => void; botRunning: boolean }) {
  const [text, setText] = useState("");
  const [doc, setDoc] = useState<Document | null>(null);
  const [raw, setRaw] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "error" | "warn"; text: string } | null>(null);
  const [check, setCheck] = useState<{ ok: boolean; output: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const t = await api.configRead();
      setText(t);
      setDoc(parseDocument(t));
      setDirty(false);
      setMsg(null);
    } catch (e) {
      setMsg({ kind: "error", text: String(e) });
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  const setValue = (path: string, value: unknown) => {
    if (!doc) return;
    doc.setIn(path.split("."), value);
    const t = doc.toString();
    setText(t);
    setDoc(parseDocument(t));
    setDirty(true);
  };

  const save = async () => {
    try {
      // syntactic validation first, then the bot's own validator
      const d = parseDocument(text);
      if (d.errors.length) throw new Error(d.errors.map((e) => e.message).join("; "));
      await api.configWrite(text);
      const result = await api.botCheckConfig();
      setCheck(result);
      setDirty(false);
      setMsg(result.ok
        ? { kind: botRunning ? "warn" : "ok", text: botRunning ? "saved and valid. The running bot keeps its old settings until you stop and start it." : "saved and valid (backup: config.yaml.bak)" }
        : { kind: "error", text: "saved, but the bot rejects this config — fix it before starting" });
      onSaved();
    } catch (e) {
      setMsg({ kind: "error", text: String(e) });
    }
  };

  const render = (f: Field) => {
    const v = doc ? getPath(doc, f.path) : undefined;
    if (f.type === "bool") {
      return <label style={{ color: "var(--text)", display: "flex", alignItems: "center", gap: 8 }}><input type="checkbox" style={{ width: "auto" }} checked={Boolean(v)} onChange={(e) => setValue(f.path, e.target.checked)} />{f.label}</label>;
    }
    if (f.type === "select") {
      return <><label>{f.label}</label><select value={String(v ?? "")} onChange={(e) => setValue(f.path, e.target.value)}>{f.options!.map((o) => <option key={o} value={o}>{o}</option>)}</select></>;
    }
    return <><label>{f.label}</label><input type={f.type === "number" ? "number" : "text"} step="any" value={v === undefined || v === null ? "" : String(v)} onChange={(e) => setValue(f.path, f.type === "number" ? Number(e.target.value) : e.target.value)} /></>;
  };

  return (
    <div className="grid" style={{ gap: 12 }}>
      <div className="row">
        <button className="primary" disabled={!dirty} onClick={save}>Save & validate</button>
        <button disabled={!dirty} onClick={load}>Discard</button>
        <button onClick={() => setRaw(!raw)}>{raw ? "Form view" : "Raw YAML"}</button>
        {dirty && <span className="badge amber">unsaved</span>}
        {botRunning && <span className="muted small">bot is running: changes apply after a restart</span>}
      </div>
      {msg && <div className={`notice ${msg.kind}`}>{msg.text}</div>}
      {check && !check.ok && <pre className="notice error mono small" style={{ whiteSpace: "pre-wrap" }}>{check.output}</pre>}
      {raw ? (
        <textarea className="code" style={{ minHeight: "60vh" }} value={text} onChange={(e) => { setText(e.target.value); setDoc(parseDocument(e.target.value)); setDirty(true); }} spellCheck={false} />
      ) : (
        SECTIONS.map((s) => (
          <div className="card" key={s.title}>
            <h2>{s.title}</h2>
            <div className="form">{s.fields.map((f) => <div key={f.path}>{render(f)}</div>)}</div>
          </div>
        ))
      )}
      <p className="muted small">Comments in config.yaml are preserved. Price providers, Electrum servers and other advanced keys are edited in the raw YAML view.</p>
    </div>
  );
}
