// The shell: which Host, which view, and which run is being watched.

import { useCallback, useEffect, useState } from "react";

import { fetchHost, fetchHosts, fetchRuns } from "./api";
import type { HostDetail, HostSummary, RunSummary } from "./types";
import { OutcomeChip } from "./components/Chips";
import { Logs } from "./components/Logs";
import { Operations } from "./components/Operations";
import { Overview } from "./components/Overview";
import { RunView } from "./components/RunView";
import { useRun } from "./useRun";

type Tab = "overview" | "operations" | "logs" | "runs";

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "概览" },
  { id: "operations", label: "操作" },
  { id: "logs", label: "日志" },
  { id: "runs", label: "运行记录" },
];

// A profile is a file on this machine and a run finishes on its own schedule, so
// the lists refresh on a timer. Everything expensive is on the operator's
// initiative; this only keeps the sidebar and the history honest.
const REFRESH_MS = 5000;

export function App() {
  const [hosts, setHosts] = useState<HostSummary[] | null>(null);
  const [hostId, setHostId] = useState<string | null>(null);
  const [host, setHost] = useState<HostDetail | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const run = useRun(runId);

  const reload = useCallback(() => {
    fetchHosts()
      .then((value) => {
        setHosts(value.hosts);
        setError(null);
        setHostId((current) => current ?? value.hosts[0]?.host_id ?? null);
      })
      .catch((caught: Error) => setError(caught.message));
  }, []);

  useEffect(() => {
    reload();
    const timer = window.setInterval(reload, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [reload]);

  useEffect(() => {
    if (hostId === null) {
      return;
    }
    let live = true;
    const load = () => {
      fetchHost(hostId)
        .then((value) => live && setHost(value))
        .catch((caught: Error) => live && setError(caught.message));
      fetchRuns(hostId)
        .then((value) => live && setRuns(value.runs))
        .catch(() => undefined);
    };
    load();
    const timer = window.setInterval(load, REFRESH_MS);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [hostId]);

  const watch = useCallback((started: string) => {
    setRunId(started);
    setTab("runs");
  }, []);

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">eidolon-ops 控制台</div>
        <div>
          {(hosts ?? []).map((item) => (
            <button
              key={item.host_id}
              className={`host-card${item.host_id === hostId ? " selected" : ""}`}
              onClick={() => {
                setHostId(item.host_id);
                setHost(null);
                setRunId(null);
                setTab("overview");
              }}
            >
              <div className="id">{item.host_id}</div>
              <div className="meta">
                {item.error
                  ? "profile 无法加载"
                  : `${item.platform} · ${item.adapter?.supervisor ?? "—"} · ${item.capabilities.length} caps`}
              </div>
              {item.active_run ? <div className="meta">● 有操作在跑</div> : null}
            </button>
          ))}
        </div>
        <div className="brand" style={{ marginTop: "auto", textTransform: "none" }}>
          只监听 127.0.0.1。这里的每个操作都是真的会落到 Host 上的。
        </div>
      </aside>

      <main className="main">
        <div className="topbar">
          <h1>{hostId ?? "没有可管理的 Host"}</h1>
          {host?.driver ? <span className="chip on">{host.driver}</span> : null}
          {host?.adapter ? (
            <span className="chip">{host.adapter.platform_profile}</span>
          ) : null}
        </div>

        <div className="tabs">
          {TABS.map((item) => (
            <button
              key={item.id}
              className={item.id === tab ? "on" : ""}
              onClick={() => setTab(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>

        <div className="content">
          {error ? <div className="notice">{error}</div> : null}
          {host === null ? (
            <div className="muted">正在读取 Host profile…</div>
          ) : tab === "overview" ? (
            <Overview host={host} onStarted={watch} />
          ) : tab === "operations" ? (
            <Operations host={host} onStarted={watch} />
          ) : tab === "logs" ? (
            <Logs host={host} />
          ) : (
            <div className="ops-layout">
              <div className="panel">
                <h2>最近的运行</h2>
                <div className="runlist">
                  {runs.length === 0 ? (
                    <span className="muted">这个 Host 还没有运行记录。</span>
                  ) : null}
                  {runs.map((item) => (
                    <button
                      key={item.id}
                      className={item.id === runId ? "on" : ""}
                      onClick={() => setRunId(item.id)}
                    >
                      <OutcomeChip outcome={item.outcome} status={item.status} />
                      <span>{item.operation}</span>
                      <span className="duration">
                        {new Date(item.started_at * 1000).toLocaleTimeString()}
                      </span>
                    </button>
                  ))}
                </div>
                <div className="help" style={{ marginTop: 10 }}>
                  运行记录只在这个控制台进程里，重启即清空。Host 上的收据才是权威。
                </div>
              </div>
              {run ? (
                <RunView run={run} host={host} />
              ) : (
                <div className="muted">左边选一个运行，或者去「操作」发起一个。</div>
              )}
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
