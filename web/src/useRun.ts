// One run, followed live.
//
// The run's own state machine stays on the server: every phase event carries the
// whole phase list, so this hook only ever replaces what it was handed. What is
// left here is the ordinary browser problem — open the stream, close it when the
// operator looks at something else.

import { useEffect, useState } from "react";

import { fetchRun, watchRun } from "./api";
import type { Phase, Outcome, Run, RunStatus } from "./types";

export function useRun(runId: string | null): Run | null {
  const [run, setRun] = useState<Run | null>(null);

  useEffect(() => {
    if (runId === null) {
      setRun(null);
      return;
    }
    let live = true;
    setRun(null);
    fetchRun(runId)
      .then((value) => {
        if (live) {
          setRun((current) => current ?? value);
        }
      })
      .catch(() => undefined);
    const stop = watchRun(runId, (kind, payload) => {
      if (!live) {
        return;
      }
      setRun((current) => apply(current, kind, payload));
    });
    return () => {
      live = false;
      stop();
    };
  }, [runId]);

  return run;
}

function apply(
  current: Run | null,
  kind: string,
  payload: Record<string, unknown>,
): Run | null {
  if (kind === "run.started") {
    const started = payload["run"] as Run | undefined;
    return current ?? (started ? { ...started, plan: started.plan, evidence: null } : null);
  }
  if (current === null) {
    return null;
  }
  if (kind === "phase.began" || kind === "phase.recorded") {
    return { ...current, phases: (payload["phases"] as Phase[]) ?? current.phases };
  }
  if (kind === "run.finished") {
    return {
      ...current,
      status: payload["status"] as RunStatus,
      outcome: (payload["outcome"] as Outcome | null) ?? null,
      error: (payload["error"] as string | null) ?? null,
      evidence: (payload["evidence"] as Record<string, unknown> | null) ?? null,
      phases: (payload["phases"] as Phase[]) ?? current.phases,
      finished_at: Date.now() / 1000,
    };
  }
  return current;
}
