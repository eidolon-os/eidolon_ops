// The report a run produced, shown as what it is.
//
// Three reports have a shape worth rendering properly rather than as a tree: the
// readiness contract's fact list, a log read, and a Setup code. Everything else
// falls through to the JSON view — deliberately, because the alternative is a
// summary that quietly stops being the evidence the operator is looking at.

import { useState } from "react";

import type { Run } from "../types";
import { HealthChip } from "./Chips";
import { Json } from "./Json";

export function Evidence({
  run,
  readiness,
  sensitiveKeys,
}: {
  run: Run;
  readiness: Record<string, string>;
  sensitiveKeys: string[];
}) {
  const report = run.evidence;
  if (run.error) {
    return <div className="notice">{run.error}</div>;
  }
  if (!report) {
    return <div className="muted">还没有报告。</div>;
  }
  const checks = report["checks"];
  const logs = report["logs"];
  return (
    <div className="stack">
      {sensitiveKeys.map((key) =>
        typeof report[key] === "string" ? (
          <Secret key={key} label={key} value={report[key] as string} />
        ) : null,
      )}
      {isFacts(checks) ? <Facts checks={checks} readiness={readiness} /> : null}
      {logs !== undefined && logs !== null ? <Logs logs={logs} /> : null}
      <details open={!isFacts(checks) && logs === undefined}>
        <summary className="muted" style={{ cursor: "pointer", fontSize: "var(--fs-mono)" }}>
          完整报告
        </summary>
        <Json value={report} />
      </details>
    </div>
  );
}

function isFacts(value: unknown): value is Record<string, boolean> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.values(value).every((item) => typeof item === "boolean")
  );
}

function Facts({
  checks,
  readiness,
}: {
  checks: Record<string, boolean>;
  readiness: Record<string, string>;
}) {
  const entries = Object.entries(checks);
  const failed = entries.filter(([, value]) => !value);
  return (
    <div>
      <div className="row" style={{ marginBottom: 8 }}>
        <span className="chip on">
          {entries.length - failed.length}/{entries.length} 通过
        </span>
        {failed.length > 0 ? (
          <span className="chip bad">{failed.length} 条未通过</span>
        ) : (
          <span className="chip good">全部通过</span>
        )}
      </div>
      <table className="facts">
        <thead>
          <tr>
            <th>事实</th>
            <th>读的是什么</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {entries
            .slice()
            .sort((left, right) => Number(left[1]) - Number(right[1]))
            .map(([fact, value]) => (
              <tr key={fact}>
                <td className="fact">{fact}</td>
                <td className="why">{readiness[fact] ?? ""}</td>
                <td>
                  <HealthChip value={value} />
                </td>
              </tr>
            ))}
        </tbody>
      </table>
    </div>
  );
}

// Mac reads log files and answers a name to lines; a product Host reads the
// journal and answers a unit to one blob. Both are "a name and its output".
function Logs({ logs }: { logs: unknown }) {
  if (typeof logs === "string") {
    return <pre className="logs">{logs || "（无输出）"}</pre>;
  }
  if (typeof logs !== "object" || logs === null) {
    return <Json value={logs} />;
  }
  const entries = Object.entries(logs as Record<string, unknown>);
  if (entries.length === 0) {
    return <div className="muted">这个选择下没有日志。</div>;
  }
  return (
    <div className="stack">
      {entries.map(([name, content]) => (
        <div key={name}>
          <div className="mono muted" style={{ fontSize: "var(--fs-small)", marginBottom: 4 }}>
            {name}
          </div>
          <pre className="logs">
            {Array.isArray(content)
              ? content.join("\n")
              : typeof content === "string"
                ? content
                : JSON.stringify(content, null, 2)}
          </pre>
        </div>
      ))}
    </div>
  );
}

// Shown because issuing it is the point, masked because a console tab is not
// where a Setup code should sit while the operator walks to the phone.
function Secret({ label, value }: { label: string; value: string }) {
  const [shown, setShown] = useState(false);
  return (
    <div className="panel" style={{ background: "#0f1a14", borderColor: "#23503f" }}>
      <h2>{label}</h2>
      <div className="secret">
        <code>{shown ? value : "•".repeat(Math.min(value.length, 12))}</code>
        <button onClick={() => setShown(!shown)}>{shown ? "隐藏" : "显示"}</button>
        <span className="muted" style={{ fontSize: "var(--fs-small)" }}>
          一次性，有寿命上限；本页不保存，刷新即消失
        </span>
      </div>
    </div>
  );
}
