// One run: what it was approved as, how far it has got, and what it produced.

import type { HostDetail, Run } from "../types";
import { DestructiveChip, OutcomeChip, TouchChips } from "./Chips";
import { Evidence } from "./Evidence";
import { Timeline } from "./Timeline";

export function RunView({ run, host }: { run: Run; host: HostDetail }) {
  const spec = host.operations.find((item) => item.name === run.operation);
  const elapsed =
    (run.finished_at ?? Date.now() / 1000) - run.started_at;
  return (
    <div className="stack">
      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div className="row">
            <strong className="mono">{run.operation}</strong>
            <span className="muted">{run.label}</span>
            <OutcomeChip outcome={run.outcome} status={run.status} />
            <DestructiveChip level={run.plan.destructive} />
            <TouchChips touches={run.plan.touches} />
          </div>
          <span className="duration">
            {elapsed.toFixed(1)}s · {new Date(run.started_at * 1000).toLocaleTimeString()}
          </span>
        </div>
        {Object.keys(run.parameters).length > 0 ? (
          <dl className="rows" style={{ marginTop: 10 }}>
            {Object.entries(run.parameters).map(([key, value]) => (
              <div key={key} style={{ display: "contents" }}>
                <dt>{key}</dt>
                <dd>{String(value)}</dd>
              </div>
            ))}
          </dl>
        ) : null}
        {run.plan.requires_flags.length > 0 ? (
          <div className="row" style={{ marginTop: 8 }}>
            <span className="muted" style={{ fontSize: "var(--fs-small)" }}>
              等价命令行开关
            </span>
            {run.plan.requires_flags.map((flag) => (
              <span key={flag} className="chip on">
                {flag}
              </span>
            ))}
          </div>
        ) : null}
      </div>

      <div className="panel">
        <h2>进度</h2>
        <Timeline plan={run.plan} phases={run.phases} runStatus={run.status} />
      </div>

      <div className="panel">
        <h2>证据</h2>
        <Evidence
          run={run}
          readiness={host.readiness}
          sensitiveKeys={spec?.sensitive_keys ?? []}
        />
      </div>
    </div>
  );
}
