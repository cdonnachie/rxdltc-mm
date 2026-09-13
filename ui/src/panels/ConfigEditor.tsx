import { useCallback, useEffect, useState } from "react";
import { parseDocument, type Document } from "yaml";
import { api } from "../api";

type FieldType = "number" | "bool" | "select" | "text";

/** `def` is the value the bot itself uses when the key is absent from config.yaml. It is shown
 *  so an untouched field reflects what the bot will actually do, rather than a blank or false. */
interface Field {
  path: string;
  label: string;
  type: FieldType;
  options?: string[];
  def?: string | number | boolean;
  hint?: string;
  unit?: string;
}
interface Section { title: string; desc?: string; fields: Field[] }

const SECTIONS: Section[] = [
  {
    title: "Mode",
    desc: "Dry run is the safe default: the bot reads everything and logs the orders it would place, but never touches the order book.",
    fields: [
      { path: "dry_run", label: "Dry run", type: "bool", def: true, hint: "Log intended actions only; never place or cancel." },
      { path: "pair.quote", label: "LTC ticker", type: "select", options: ["LTC-segwit", "LTC"], def: "LTC-segwit",
        hint: "LTC-segwit is the ltc1… address, LTC the legacy L… one. Both trade in the same order book." },
      { path: "poll_interval_seconds", label: "Poll interval", type: "number", def: 30, unit: "s",
        hint: "How often prices, balances and orders are re-checked." },
    ],
  },
  {
    title: "Pricing",
    desc: "Prices are RXD per LTC, where a larger number means cheaper RXD. The bid is quoted above fair value and the ask below it, which in LTC-per-RXD terms is the familiar buy low, sell high.",
    fields: [
      { path: "pricing.bid_offset_pct", label: "Bid offset", type: "number", def: 1.0, unit: "%", hint: "How far under fair value the bot buys RXD." },
      { path: "pricing.ask_offset_pct", label: "Ask offset", type: "number", def: 1.0, unit: "%", hint: "How far over fair value the bot sells RXD." },
      { path: "pricing.min_edge_pct", label: "Minimum edge", type: "number", def: 0.1, unit: "%", hint: "Quotes never come closer to fair value than this, whatever the skew says." },
      { path: "pricing.min_valid_providers", label: "Minimum sources", type: "number", def: 2, hint: "Quoting stops with fewer healthy price sources than this." },
      { path: "pricing.max_provider_disagreement_pct", label: "Max source disagreement", type: "number", def: 5.0, unit: "%", hint: "Spread between sources after outliers are dropped." },
      { path: "pricing.max_source_age_seconds", label: "Max source age", type: "number", def: 600, unit: "s", hint: "Ignore a quote the source itself reports as older than this." },
    ],
  },
  {
    title: "Repricing",
    desc: "Controls order churn. An order is only replaced when it has drifted meaningfully and has lived long enough.",
    fields: [
      { path: "reprice_threshold_pct", label: "Reprice threshold", type: "number", def: 0.5, unit: "%", hint: "Replace once the price drifts this far from target." },
      { path: "resize_threshold_pct", label: "Resize threshold", type: "number", def: 25, unit: "%", hint: "Replace once the remaining size drifts this far, typically after a partial fill." },
      { path: "minimum_order_lifetime_seconds", label: "Minimum lifetime", type: "number", def: 60, unit: "s", hint: "Never cancel an order younger than this, unless it is unsafe." },
    ],
  },
  {
    title: "Inventory",
    desc: "Share of the wallet's value held as RXD, valued at fair price. The skew nudges quotes toward the target; the bounds stop one-way drift entirely.",
    fields: [
      { path: "inventory.target_rxd_value_pct", label: "Target RXD share", type: "number", def: 50, unit: "%" },
      { path: "inventory.min_rxd_value_pct", label: "Minimum RXD share", type: "number", def: 25, unit: "%", hint: "Below this the ask is withdrawn, so no more RXD is sold." },
      { path: "inventory.max_rxd_value_pct", label: "Maximum RXD share", type: "number", def: 75, unit: "%", hint: "Above this the bid is withdrawn, so no more RXD is bought." },
      { path: "inventory_skew.enabled", label: "Inventory skew", type: "bool", def: true, hint: "Shift quotes toward rebalancing as the share moves off target." },
      { path: "inventory_skew.max_adjustment_pct", label: "Max skew", type: "number", def: 1.0, unit: "%", hint: "Full shift is reached at the min or max bound." },
    ],
  },
  {
    title: "Order sizing and reserve",
    desc: "The ask is sized in RXD and the bid in LTC. The reserve is never committed, leaving room for fees and settlement.",
    fields: [
      { path: "order_sizing.mode", label: "Sizing mode", type: "select", options: ["balance_percent", "fixed"], def: "balance_percent" },
      { path: "order_sizing.rxd_balance_percent", label: "RXD per ask", type: "number", def: 50, unit: "%", hint: "Share of the RXD left after reserve." },
      { path: "order_sizing.ltc_balance_percent", label: "LTC per bid", type: "number", def: 50, unit: "%", hint: "Share of the LTC left after reserve." },
      { path: "order_sizing.rxd_order_amount", label: "Fixed ask size", type: "number", def: 0, unit: "RXD", hint: "Used only in fixed mode." },
      { path: "order_sizing.ltc_order_amount", label: "Fixed bid size", type: "number", def: 0, unit: "LTC", hint: "Used only in fixed mode." },
      { path: "order_sizing.min_rxd_order_amount", label: "Minimum ask", type: "number", def: 0, unit: "RXD", hint: "Smaller asks are skipped." },
      { path: "order_sizing.min_ltc_order_amount", label: "Minimum bid", type: "number", def: 0, unit: "LTC", hint: "Smaller bids are skipped." },
      { path: "reserve.rxd_percent", label: "RXD reserve", type: "number", def: 10, unit: "%" },
      { path: "reserve.ltc_percent", label: "LTC reserve", type: "number", def: 10, unit: "%" },
    ],
  },
  {
    title: "Circuit breakers",
    desc: "Each of these cancels every order and pauses. The bot resumes on its own once conditions have been healthy for the recovery period.",
    fields: [
      { path: "safety.max_price_move_pct", label: "Max price move", type: "number", def: 5.0, unit: "%", hint: "Pull quotes if fair value jumps this much inside the window." },
      { path: "safety.max_price_move_window_seconds", label: "Price move window", type: "number", def: 300, unit: "s" },
      { path: "safety.anchor_enabled", label: "Slow price anchor", type: "bool", def: true, hint: "Stop quoting while fair value sits far from its recent median. Guards against a thin market being pushed." },
      { path: "safety.anchor_window_seconds", label: "Anchor window", type: "number", def: 3600, unit: "s", hint: "The anchor is the median over this period." },
      { path: "safety.max_anchor_deviation_pct", label: "Max anchor deviation", type: "number", def: 10, unit: "%", hint: "Also sets the fastest trend tolerated: roughly this much per half window." },
      { path: "safety.anchor_min_span_seconds", label: "Anchor warm-up", type: "number", def: 900, unit: "s", hint: "No anchor check until this much history exists. Seeded from the database on restart." },
      { path: "safety.rpc_failure_limit", label: "RPC failure limit", type: "number", def: 3, hint: "Consecutive KDF failures before pausing." },
      { path: "safety.order_error_limit", label: "Order error limit", type: "number", def: 3, hint: "Consecutive place or cancel failures before pausing." },
      { path: "safety.cooldown_seconds", label: "Pause cooldown", type: "number", def: 300, unit: "s", hint: "Minimum time paused before resuming." },
      { path: "safety.recovery_seconds", label: "Recovery period", type: "number", def: 120, unit: "s", hint: "Conditions must stay healthy this long before resuming." },
    ],
  },
  {
    title: "Order safety and shutdown",
    fields: [
      { path: "safety.max_quote_deviation_pct", label: "Max quote deviation", type: "number", def: 3.0, unit: "%", hint: "Hard ceiling on how far any quote may sit from fair value." },
      { path: "trading.prevent_crossing_book", label: "Prevent crossing", type: "bool", def: true, hint: "Never quote through another maker's order." },
      { path: "trading.on_cross", label: "When a quote would cross", type: "select", options: ["skip", "adjust"], def: "skip", hint: "Skip the order, or move it just inside the other quote." },
      { path: "shutdown.cancel_orders_on_exit", label: "Cancel orders on exit", type: "bool", def: true, hint: "Leave nothing resting when the bot stops." },
    ],
  },
];

function getPath(doc: Document, path: string): unknown {
  return doc.getIn(path.split("."), false);
}

export default function ConfigEditor({ onSaved, botRunning }: { onSaved: () => void; botRunning: boolean }) {
  const [text, setText] = useState("");
  const [doc, setDoc] = useState<Document | null>(null);
  const [saved, setSaved] = useState<Document | null>(null); // last saved state, for change highlighting
  const [raw, setRaw] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "error" | "warn"; text: string } | null>(null);
  const [check, setCheck] = useState<{ ok: boolean; output: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const t = await api.configRead();
      setText(t);
      setDoc(parseDocument(t));
      setSaved(parseDocument(t));
      setDirty(false);
      setMsg(null);
      setCheck(null);
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
      const d = parseDocument(text);
      if (d.errors.length) throw new Error(d.errors.map((e) => e.message).join("; "));
      await api.configWrite(text);
      const result = await api.botCheckConfig();
      setCheck(result);
      setDirty(false);
      setSaved(parseDocument(text));
      setMsg(result.ok
        ? { kind: botRunning ? "warn" : "ok", text: botRunning ? "Saved and valid. The running bot keeps its old settings until you stop and start it." : "Saved and valid. Previous version kept as config.yaml.bak." }
        : { kind: "error", text: "Saved, but the bot rejects this config. Fix it before starting." });
      onSaved();
    } catch (e) {
      setMsg({ kind: "error", text: String(e) });
    }
  };

  const changedCount = doc && saved
    ? SECTIONS.flatMap((s) => s.fields).filter((f) => String(getPath(doc, f.path)) !== String(getPath(saved, f.path))).length
    : 0;

  const renderField = (f: Field) => {
    const raw = doc ? getPath(doc, f.path) : undefined;
    const missing = raw === undefined || raw === null;
    const v = missing ? f.def : raw; // an absent key means the bot's built-in default applies
    const changed = Boolean(doc && saved && String(getPath(doc, f.path)) !== String(getPath(saved, f.path)));
    const label = (
      <label>
        {f.label}
        {f.unit && <span className="muted small">{f.unit}</span>}
        {changed ? <span className="tag">changed</span> : missing && f.def !== undefined ? <span className="tag" title="not set in config.yaml, so the bot's default applies">default</span> : null}
      </label>
    );
    let control: JSX.Element;
    if (f.type === "bool") {
      control = (
        <div className="switch" onClick={() => setValue(f.path, !v)}>
          <input type="checkbox" checked={Boolean(v)} readOnly />
          <span>{v ? "on" : "off"}</span>
        </div>
      );
    } else if (f.type === "select") {
      control = (
        <select value={String(v ?? "")} onChange={(e) => setValue(f.path, e.target.value)}>
          {f.options!.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      );
    } else {
      control = (
        <input type={f.type === "number" ? "number" : "text"} step="any"
               value={v === undefined || v === null ? "" : String(v)}
               onChange={(e) => setValue(f.path, f.type === "number" ? Number(e.target.value) : e.target.value)} />
      );
    }
    return (
      <div className={`field${changed ? " changed" : ""}`} key={f.path}>
        {label}
        {control}
        {f.hint && <div className="field-hint">{f.hint}</div>}
      </div>
    );
  };

  return (
    <div className="grid panel" style={{ gap: 12 }}>
      <div className="row">
        <button className="primary" disabled={!dirty} onClick={save}>Save &amp; validate</button>
        <button disabled={!dirty} onClick={load}>Discard</button>
        <button onClick={() => setRaw(!raw)}>{raw ? "Form view" : "Raw YAML"}</button>
        {dirty && <span className="badge amber">{changedCount || ""} unsaved</span>}
        {botRunning && <span className="muted small">Bot is running: changes apply after a restart.</span>}
      </div>
      {msg && <div className={`notice ${msg.kind}`}>{msg.text}</div>}
      {check && !check.ok && <pre className="notice error mono small" style={{ whiteSpace: "pre-wrap" }}>{check.output}</pre>}
      {raw ? (
        <textarea className="code" style={{ minHeight: "68vh" }} value={text} spellCheck={false}
                  onChange={(e) => { setText(e.target.value); setDoc(parseDocument(e.target.value)); setDirty(true); }} />
      ) : (
        SECTIONS.map((s) => (
          <div className="card" key={s.title}>
            <h2>{s.title}</h2>
            {s.desc && <p className="section-desc">{s.desc}</p>}
            <div className="form">{s.fields.map(renderField)}</div>
          </div>
        ))
      )}
      <p className="muted small">
        Comments in config.yaml are preserved. Price providers, Electrum servers and other advanced keys are edited in the raw YAML view.
      </p>
    </div>
  );
}
