// The one read an operator does over and over, given its own tab.
//
// It is the same `logs` operation as everywhere else — same capability check,
// same run, same bounded line count. What is different is only that the form is
// three inputs and a button instead of a plan review, because a log read has
// nothing to approve.

import { useState } from "react";

import { startRun } from "../api";
import type { HostDetail, Run } from "../types";
import { Evidence } from "./Evidence";
import { useRun } from "../useRun";

export function Logs({ host }: { host: HostDetail }) {
  const spec = host.operations.find((item) => item.name === "logs");
  const services = spec?.fields.find((item) => item.name === "service")?.suggestions ?? [];
  const historic = spec?.fields.some((item) => item.name === "since") ?? false;
  const [service, setService] = useState("");
  const [lines, setLines] = useState(200);
  const [since, setSince] = useState("");
  const [runId, setRunId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const run = useRun(runId);

  if (!spec) {
    return <div className="notice">这台 Host 不提供日志读取。</div>;
  }

  async function read() {
    setError(null);
    try {
      const started = await startRun(
        host.host_id,
        "logs",
        {
          service: service || null,
          lines,
          ...(historic ? { since: since || null } : {}),
        },
        { acknowledge: false },
      );
      setRunId(started.id);
    } catch (caught) {
      setError((caught as Error).message);
    }
  }

  return (
    <div className="stack">
      <div className="panel">
        <div
          className="row"
          style={{ display: "grid", gridTemplateColumns: "2fr 1fr 2fr max-content", gap: 10 }}
        >
          <div className="field" style={{ margin: 0 }}>
            <label>服务</label>
            <input
              value={service}
              list="log-services"
              placeholder="留空读取全部"
              onChange={(event) => setService(event.target.value)}
            />
            <datalist id="log-services">
              {services.map((item) => (
                <option key={item} value={item} />
              ))}
            </datalist>
          </div>
          <div className="field" style={{ margin: 0 }}>
            <label>行数</label>
            <input
              type="number"
              min={1}
              max={5000}
              value={lines}
              onChange={(event) => setLines(Number(event.target.value))}
            />
          </div>
          <div className="field" style={{ margin: 0 }}>
            <label>起始时间{historic ? "" : "（本 Host 不支持）"}</label>
            <input
              value={since}
              disabled={!historic}
              placeholder="-30min"
              onChange={(event) => setSince(event.target.value)}
            />
          </div>
          <div style={{ alignSelf: "end" }}>
            <button className="primary" onClick={read}>
              读取
            </button>
          </div>
        </div>
      </div>

      {error ? <div className="notice">{error}</div> : null}
      {run ? <LogResult run={run} host={host} /> : null}
    </div>
  );
}

function LogResult({ run, host }: { run: Run; host: HostDetail }) {
  if (run.status === "running") {
    return <div className="muted">正在读取…</div>;
  }
  return <Evidence run={run} readiness={host.readiness} sensitiveKeys={[]} />;
}
