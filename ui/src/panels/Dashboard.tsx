import { useState } from "react";
import { api, type BotStatus, copyText, fmtDuration, fmtM, fmtNum, fmtTime, fmtUsd, ltcPerRxd, rebalanceHint } from "../api";

function Address({ label, value, holding }: { label: string; value: string | null | undefined; holding?: string }) {
  const [copied, setCopied] = useState(false);
  if (!value) return <div className="row small muted">{label}: not available</div>;
  return (
    <div className="row" style={{ gap: 6 }}>
      <span className="muted small" style={{ width: 90 }}>{label}</span>
      <span className="mono small" style={{ userSelect: "all", wordBreak: "break-all" }}>{value}</span>
      <button className="small" style={{ padding: "2px 8px" }} onClick={async () => { await copyText(value); setCopied(true); setTimeout(() => setCopied(false), 1500); }}>
        {copied ? "copied" : "copy"}
      </button>
      {holding && <span className="small muted">{holding}</span>}
    </div>
  );
}

function RebalanceCard({ status }: { status: BotStatus }) {
  const base = status.base ?? "RXD", quote = status.quote ?? "LTC";
  const hint = rebalanceHint(status);
  const total = status.balance_rxd_usd && status.balance_ltc_usd ? Number(status.balance_rxd_usd) + Number(status.balance_ltc_usd) : null;
  return (
    <div className="card">
      <h2>Wallet addresses (dedicated liquidity wallet){total !== null && <span className="muted"> · total {fmtUsd(total)}</span>}</h2>
      <Address label={base} value={status.address_rxd}
               holding={status.balance_rxd_usd ? `${fmtNum(status.balance_rxd, 2)} ${base} ≈ ${fmtUsd(status.balance_rxd_usd)} @ ${fmtUsd(status.rxd_usd, 8)}` : undefined} />
      <Address label={quote} value={status.address_ltc}
               holding={status.balance_ltc_usd ? `${fmtNum(status.balance_ltc, 8)} ${quote} ≈ ${fmtUsd(status.balance_ltc_usd)} @ ${fmtUsd(status.ltc_usd)}` : undefined} />
      <h2 style={{ marginTop: 14 }}>Rebalance</h2>
      {hint ? (
        <p className="small" style={{ margin: 0 }}>
          {base} share is <b>{(hint.share * 100).toFixed(1)}%</b> of value, target {(hint.target * 100).toFixed(0)}%
          {" · "}
          {Math.abs(hint.share - hint.target) < 0.005
            ? <>balanced, nothing to add</>
            : hint.addLtc !== undefined
              ? <>to reach the target add about <b>{hint.addLtc.toFixed(5)} {quote}</b> (the wallet is {base}-heavy; adding {base} would skew it further)</>
              : <>to reach the target add about <b>{fmtNum(hint.addRxd, 0)} {base}</b> (the wallet is {quote}-heavy)</>}
          . Bounds: no bid above {status.inventory_max_pct}% {base}, no ask below {status.inventory_min_pct}%.
        </p>
      ) : <span className="muted small">needs balances and a fair price</span>}
    </div>
  );
}

export default function Dashboard({ status }: { status: BotStatus | null }) {
  if (!status) return <div className="muted">Loading…</div>;
  if (!status.process.running && !status.reachable) {
    return (
      <div className="card">
        <h2>Bot</h2>
        <p>The bot is not running. Check the <b>Settings</b> tab (bot command, config path, RPC password), make sure the KDF node is up (<b>KDF node</b> tab), then press <b>Start bot</b>.</p>
        {status.process.exit_code !== null && status.process.exit_code !== undefined && (
          <p className="muted small">Last exit code: {status.process.exit_code}</p>
        )}
      </div>
    );
  }
  if (!status.reachable) {
    return (
      <div className="card">
        <h2>Bot</h2>
        <p>Process running (pid {status.process.pid}) but the status endpoint is not answering yet: {status.error}</p>
      </div>
    );
  }
  const base = status.base ?? "RXD", quote = status.quote ?? "LTC";
  const bid = status.orders?.bid, ask = status.orders?.ask;
  return (
    <div className="grid" style={{ gap: 12 }}>
      {status.paused ? (
        <div className={`notice ${status.operator_paused ? "warn" : "error"}`}>
          <b>{status.operator_paused ? "Paused by operator" : "Safety pause"}:</b> {status.paused_reason}
          {!status.operator_paused && status.resume_in_seconds != null && <span className="muted"> · resumes in ~{fmtDuration(status.resume_in_seconds)} if conditions stay healthy</span>}
        </div>
      ) : null}
      {status.reference_reason && status.reference_reason !== "ok" && (
        <div className="notice warn"><b>Reference price:</b> {status.reference_reason}</div>
      )}

      <div className="grid cols-4">
        <div className="card stat">
          <h2>Fair price</h2>
          <span className="value">{fmtM(status.fair_price_rxd_per_ltc)}</span>
          <span className="sub">{base} per {quote} · {ltcPerRxd(status.fair_price_rxd_per_ltc)} LTC/RXD</span>
          <span className="sub">{status.reference_sources} sources · disagreement {fmtNum(status.reference_disagreement_pct, 2)}%</span>
        </div>
        <div className="card stat">
          <h2>Bid · bot buys {base}</h2>
          <span className="value">{fmtM(status.target_bid_rxd_per_ltc)}</span>
          <span className="sub">{status.targets_detail?.bid_ltc_per_rxd ? Number(status.targets_detail.bid_ltc_per_rxd).toExponential(4) + " LTC/RXD" : "not quoting"}</span>
          <span className="sub">{bid ? `open @ ${fmtM(bid.price_rxd_per_ltc)} · ${fmtNum(bid.amount, 8)} ${quote}` : "no open bid"}</span>
        </div>
        <div className="card stat">
          <h2>Ask · bot sells {base}</h2>
          <span className="value">{fmtM(status.target_ask_rxd_per_ltc)}</span>
          <span className="sub">{status.targets_detail?.ask_ltc_per_rxd ? Number(status.targets_detail.ask_ltc_per_rxd).toExponential(4) + " LTC/RXD" : "not quoting"}</span>
          <span className="sub">{ask ? `open @ ${fmtM(ask.price_rxd_per_ltc)} · ${fmtNum(ask.amount, 2)} ${base}` : "no open ask"}</span>
        </div>
        <div className="card stat">
          <h2>Inventory</h2>
          <span className="value">{fmtNum(status.inventory_rxd_value_pct, 1)}% {base}</span>
          <span className="sub">{fmtNum(status.balance_rxd, 2)} {base} · {fmtNum(status.balance_ltc, 8)} {quote}</span>
          {status.balance_rxd_usd && status.balance_ltc_usd && (
            <span className="sub">{fmtUsd(status.balance_rxd_usd)} + {fmtUsd(status.balance_ltc_usd)} = {fmtUsd(Number(status.balance_rxd_usd) + Number(status.balance_ltc_usd))}</span>
          )}
          <span className="sub">skew {fmtNum(status.targets_detail?.skew_pct, 3)}%{status.targets_detail?.clamped ? " · clamped" : ""}</span>
        </div>
      </div>

      <RebalanceCard status={status} />

      <div className="grid cols-3">
        <div className="card">
          <h2>Order book (foreign quotes, {base} per {quote})</h2>
          {status.orderbook ? (
            <table>
              <thead><tr><th>side</th><th className="num">price</th><th className="num">{base}</th></tr></thead>
              <tbody>
                {/* asks: worst (fewest RXD per LTC) at the top, best touching the fair line */}
                {[...status.orderbook.asks].sort((a, b) => Number(a.price_rxd_per_ltc) - Number(b.price_rxd_per_ltc)).slice(-8).map((l, i) => (
                  <tr key={"a" + i} className={l.mine ? "mine" : ""}><td>{l.mine ? "mine" : "sell"}</td><td className="num">{fmtM(l.price_rxd_per_ltc)}</td><td className="num">{fmtNum(l.rxd, 0)}</td></tr>
                ))}
                <tr><td colSpan={3} className="muted small">fair {fmtM(status.fair_price_rxd_per_ltc)}</td></tr>
                {/* bids: best (fewest RXD per LTC, i.e. paying the most) touching the fair line */}
                {[...status.orderbook.bids].sort((a, b) => Number(a.price_rxd_per_ltc) - Number(b.price_rxd_per_ltc)).slice(0, 8).map((l, i) => (
                  <tr key={"b" + i} className={l.mine ? "mine" : ""}><td>{l.mine ? "mine" : "buy"}</td><td className="num">{fmtM(l.price_rxd_per_ltc)}</td><td className="num">{fmtNum(l.rxd, 0)}</td></tr>
                ))}
              </tbody>
            </table>
          ) : <span className="muted">no data</span>}
          <p className="muted small">Larger number = cheaper {base}. Sells above the fair line are foreign asks, buys below are foreign bids.</p>
        </div>
        <div className="card">
          <h2>Last plan</h2>
          {status.last_plan && status.last_plan.length ? (
            <table>
              <thead><tr><th>action</th><th>side</th><th>reason</th></tr></thead>
              <tbody>
                {status.last_plan.map((a, i) => (
                  <tr key={i}><td><span className={`badge ${a.kind === "place" ? "green" : a.kind === "cancel" ? "amber" : "grey"}`}>{a.kind}</span></td><td>{a.side}</td><td className="small">{a.reason}</td></tr>
                ))}
              </tbody>
            </table>
          ) : <span className="muted">nothing planned yet</span>}
          <h2 style={{ marginTop: 14 }}>Providers</h2>
          <table>
            <tbody>
              {Object.entries(status.providers ?? {}).map(([name, p]) => (
                <tr key={name}>
                  <td>{name}</td>
                  <td><span className={`badge ${p.healthy ? "green" : "red"}`}>{p.healthy ? "ok" : "fail"}</span></td>
                  <td className="num">{fmtM(p.price_rxd_per_ltc)}</td>
                  <td className="small muted">{p.last_error ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="card">
          <h2>Statistics</h2>
          <table>
            <tbody>
              <tr><td>state</td><td className="num">{status.state} <span className="muted small">{status.state_reason}</span></td></tr>
              <tr><td>uptime</td><td className="num">{fmtDuration(status.uptime_seconds)}</td></tr>
              <tr><td>cycles</td><td className="num">{status.cycles_total}</td></tr>
              <tr><td>swaps (failed)</td><td className="num">{status.swaps_total} ({status.swaps_failed_total})</td></tr>
              <tr><td>{base} bought / sold</td><td className="num">{fmtNum(status.rxd_bought_total, 2)} / {fmtNum(status.rxd_sold_total, 2)}</td></tr>
              <tr><td>{quote} spent / received</td><td className="num">{fmtNum(status.ltc_spent_total, 8)} / {fmtNum(status.ltc_received_total, 8)}</td></tr>
              <tr><td>two-sided time (all runs)</td><td className="num">{fmtDuration(Number(status.two_sided_seconds_total ?? 0))}</td></tr>
              <tr><td>RPC failures / order errors</td><td className="num">{status.rpc_failures_total} / {status.order_errors_total}</td></tr>
            </tbody>
          </table>
          <div className="row" style={{ marginTop: 14, justifyContent: "space-between" }}>
            <h2 style={{ margin: 0 }}>Recent events</h2>
            <button className="small" style={{ padding: "2px 8px" }} title="Delete the persisted event log"
                    onClick={() => api.botControl("clear_events").catch((e) => alert(String(e)))}>clear</button>
          </div>
          <table>
            <tbody>
              {(status.recent_events ?? []).slice(0, 8).map((e, i) => (
                <tr key={i}><td className="muted small">{fmtTime(e.ts)}</td><td><span className={`badge ${e.level === "ERROR" ? "red" : e.level === "WARNING" ? "amber" : "grey"}`}>{e.kind}</span></td><td className="small">{e.message}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
