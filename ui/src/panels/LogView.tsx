import { useEffect, useRef, useState } from "react";
import { api, onLog } from "../api";

function classify(line: string): string {
  if (line.includes(" ERROR ") || line.includes("[stderr]") || line.includes("] ERROR")) return "ERROR";
  if (line.includes(" WARNING ") || line.includes("SAFETY TRIGGER")) return "WARNING";
  if (line.includes("STATE ")) return "STATE";
  return "";
}

export default function LogView() {
  const [which, setWhich] = useState<"bot" | "kdf">("bot");
  const [lines, setLines] = useState<string[]>([]);
  const [filter, setFilter] = useState("");
  const [follow, setFollow] = useState(true);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    api.logsGet(which).then((l) => alive && setLines(l));
    const un = onLog(which, (line) => setLines((prev) => (prev.length > 3000 ? [...prev.slice(-2500), line] : [...prev, line])));
    return () => { alive = false; un.then((f) => f()); };
  }, [which]);

  useEffect(() => {
    if (follow && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [lines, follow]);

  const shown = filter ? lines.filter((l) => l.toLowerCase().includes(filter.toLowerCase())) : lines;
  return (
    <div>
      <div className="row" style={{ marginBottom: 8 }}>
        <select value={which} onChange={(e) => setWhich(e.target.value as "bot" | "kdf")} style={{ width: 140 }}>
          <option value="bot">bot</option>
          <option value="kdf">KDF node</option>
        </select>
        <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="filter…" style={{ width: 260 }} />
        <label style={{ margin: 0 }}><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} style={{ width: "auto", marginRight: 6 }} />follow</label>
        <button onClick={() => { api.logsClear(which); setLines([]); }}>Clear</button>
        <span className="muted small">{shown.length} lines</span>
      </div>
      <div className="log" ref={ref}>
        {shown.map((l, i) => <div key={i} className={classify(l)}>{l}</div>)}
      </div>
    </div>
  );
}
