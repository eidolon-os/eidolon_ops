// What this Host is, read off its profile and its composed adapter.
//
// The capability list is the interesting part: it is why this page offers
// `install` on a board and `debug` on a workstation without knowing what either
// is. Nothing here is inferred from the platform name.

import { useState } from "react";

import { startRun } from "../api";
import type { HostDetail } from "../types";

const QUICK = ["status", "doctor", "app-ready"];

export function Overview({
  host,
  onStarted,
}: {
  host: HostDetail;
  onStarted: (runId: string) => void;
}) {
  const [error, setError] = useState<string | null>(null);

  async function run(operation: string) {
    setError(null);
    try {
      const started = await startRun(host.host_id, operation, {}, { acknowledge: false });
      onStarted(started.id);
    } catch (caught) {
      setError((caught as Error).message);
    }
  }

  return (
    <div className="stack">
      <div className="row">
        {QUICK.filter((name) => host.operations.some((item) => item.name === name)).map(
          (name) => (
            <button key={name} className="primary" onClick={() => run(name)}>
              运行 {name}
            </button>
          ),
        )}
        {host.active_run ? (
          <span className="chip warn">该 Host 有一个边界动作正在跑</span>
        ) : null}
      </div>

      {error ? <div className="notice">{error}</div> : null}
      {host.error ? <div className="notice">{host.error}</div> : null}

      <div className="grid">
        <div className="panel">
          <h2>身份与组合</h2>
          <dl className="rows">
            <dt>host_id</dt>
            <dd>{host.host_id}</dd>
            <dt>platform</dt>
            <dd>{host.platform ?? "—"}</dd>
            <dt>driver</dt>
            <dd>{host.driver ?? "—"}</dd>
            <dt>transport</dt>
            <dd>{host.adapter?.transport ?? "—"}</dd>
            <dt>supervisor</dt>
            <dd>{host.adapter?.supervisor ?? "—"}</dd>
            <dt>packages</dt>
            <dd>{host.adapter?.packages ?? "—"}</dd>
            <dt>profile</dt>
            <dd>{host.profile_path}</dd>
          </dl>
        </div>

        <div className="panel">
          <h2>capability（{host.capabilities.length}）</h2>
          <div className="chips">
            {host.capabilities.map((item) => (
              <span key={item} className="chip on">
                {item}
              </span>
            ))}
          </div>
        </div>

        {host.app ? (
          <div className="panel">
            <h2>App 契约</h2>
            <dl className="rows">
              <dt>hub_https_port</dt>
              <dd>{host.app.hub_https_port}</dd>
              <dt>livekit_client_url</dt>
              <dd>{host.app.livekit_client_url}</dd>
              <dt>allow_insecure_livekit</dt>
              <dd>{String(host.app.allow_insecure_livekit)}</dd>
            </dl>
          </div>
        ) : null}

        <div className="panel">
          <h2>路径契约</h2>
          <dl className="rows">
            {Object.entries(host.paths ?? {}).map(([role, path]) => (
              <div key={role} style={{ display: "contents" }}>
                <dt>{role}</dt>
                <dd>{path}</dd>
              </div>
            ))}
          </dl>
        </div>

        {host.source_overrides && host.source_overrides.length > 0 ? (
          <div className="panel">
            <h2>source override</h2>
            <div className="chips">
              {host.source_overrides.map((item) => (
                <span key={item} className="chip warn">
                  {item}
                </span>
              ))}
            </div>
            <div className="help" style={{ marginTop: 8 }}>
              只改变这台 Host 的 source-run，不改变共用的 Pi release matrix。
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
