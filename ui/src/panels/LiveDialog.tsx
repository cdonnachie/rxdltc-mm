import { useEffect, useState } from "react";
import { api } from "../api";

export interface LiveChoice { phrase: string; startSmall: boolean }

export default function LiveDialog({ onCancel, onConfirm }: { onCancel: () => void; onConfirm: (choice: LiveChoice) => void }) {
  const [phrase, setPhrase] = useState("");
  const [typed, setTyped] = useState("");
  const [startSmall, setStartSmall] = useState(false); // opt-in: the config's own sizing is used unless ticked
  useEffect(() => { api.livePhrase().then(setPhrase); }, []);
  return (
    <div className="modal-backdrop">
      <div className="modal">
        <h3>Start LIVE trading?</h3>
        <p>The config has <code>dry_run: false</code>. The bot will place and cancel real maker orders on GLEEC DEX with the funds in the KDF wallet.</p>
        <label style={{ color: "var(--text)", display: "flex", gap: 8, alignItems: "flex-start" }}>
          <input type="checkbox" checked={startSmall} onChange={(e) => setStartSmall(e.target.checked)} style={{ width: "auto", marginTop: 3 }} />
          <span>Start small: override the config and quote <b>fixed 20,000 RXD / 0.01 LTC</b> per order. Useful for a first live run; leave unticked to use the sizing in Config.</span>
        </label>
        <p style={{ marginTop: 12 }}>Type the confirmation phrase exactly to continue:</p>
        <p className="mono small muted">{phrase}</p>
        <input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="confirmation phrase" autoFocus />
        {typed && typed !== phrase && (
          <p className="small" style={{ color: "var(--amber)", margin: "6px 0 0" }}>
            {phrase.startsWith(typed) ? `${phrase.length - typed.length} character(s) missing` : "does not match the phrase above (check spelling and underscores)"}
          </p>
        )}
        <div className="row" style={{ marginTop: 14, justifyContent: "flex-end" }}>
          <button onClick={onCancel}>Cancel</button>
          <button className="danger" disabled={!phrase || typed !== phrase} onClick={() => onConfirm({ phrase: typed, startSmall })}>Start live</button>
        </div>
      </div>
    </div>
  );
}
