// Everything this interface knows about the console, in one place.
//
// The API refuses with a JSON body carrying `error`; surfacing that string is
// how the browser reports a refusal, because the reason a Host said no — a
// missing capability, an unconfirmed irreversible operation — is the useful
// part and rewriting it here would only make it vaguer.

import type {
  HostDetail,
  HostSummary,
  ParameterValues,
  Preview,
  Run,
  RunSummary,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: init?.body ? { "content-type": "application/json" } : undefined,
  });
  const text = await response.text();
  const body = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const detail =
      body && typeof body === "object" && "error" in body
        ? String((body as { error: unknown }).error)
        : `${response.status} ${response.statusText}`;
    throw new ApiError(response.status, detail);
  }
  return body as T;
}

export function fetchHosts(): Promise<{ hosts: HostSummary[] }> {
  return request("/api/hosts");
}

export function fetchHost(hostId: string): Promise<HostDetail> {
  return request(`/api/hosts/${encodeURIComponent(hostId)}`);
}

export function fetchPlan(
  hostId: string,
  operation: string,
  parameters: ParameterValues,
): Promise<Preview> {
  return request(`/api/hosts/${encodeURIComponent(hostId)}/plan`, {
    method: "POST",
    body: JSON.stringify({ operation, parameters }),
  });
}

export function startRun(
  hostId: string,
  operation: string,
  parameters: ParameterValues,
  confirm: { acknowledge: boolean; host_id?: string },
): Promise<Run> {
  return request(`/api/hosts/${encodeURIComponent(hostId)}/runs`, {
    method: "POST",
    body: JSON.stringify({ operation, parameters, confirm }),
  });
}

export function fetchRuns(hostId?: string): Promise<{ runs: RunSummary[] }> {
  const query = hostId ? `?host_id=${encodeURIComponent(hostId)}` : "";
  return request(`/api/runs${query}`);
}

export function fetchRun(runId: string): Promise<Run> {
  return request(`/api/runs/${encodeURIComponent(runId)}`);
}

// One run's event stream. The server replays what already happened before
// following live, so a browser that opens this late still sees every phase.
export function watchRun(
  runId: string,
  onEvent: (kind: string, payload: Record<string, unknown>) => void,
): () => void {
  const source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
  const kinds = ["run.started", "phase.began", "phase.recorded", "run.finished"];
  for (const kind of kinds) {
    source.addEventListener(kind, (event) => {
      onEvent(kind, JSON.parse((event as MessageEvent<string>).data));
    });
  }
  return () => source.close();
}
