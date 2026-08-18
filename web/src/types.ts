// The wire shapes the console API serves. Mirrors of what the Python side
// declares — the catalog, the plan and the run store — kept in one file so a
// backend change shows up here as a type error rather than as a blank panel.

export type Outcome =
  | "observed"
  | "planned"
  | "applied"
  | "degraded"
  | "refused"
  | "failed";

export type Destructive = "none" | "reversible" | "irreversible";

export type ActionKind =
  | "config"
  | "secret"
  | "schema"
  | "data"
  | "lifecycle"
  | "code";

export type Confirmation = "none" | "acknowledge" | "typed-host-id";

export type FieldKind = "string" | "integer" | "boolean" | "path";

export interface PlanStep {
  id: string;
  description: string;
}

export interface Plan {
  operation: string;
  host_id: string;
  steps: PlanStep[];
  destructive: Destructive;
  requires_flags: string[];
  touches: ActionKind[];
}

export interface OperationField {
  name: string;
  kind: FieldKind;
  label: string;
  help: string;
  required: boolean;
  default: string | number | boolean | null;
  choices: string[];
  minimum: number | null;
  maximum: number | null;
  gate: boolean;
  suggestions: string[];
}

export interface OperationSpec {
  name: string;
  label: string;
  summary: string;
  group: "observe" | "lifecycle" | "release" | "authority" | "secret";
  capability: string;
  fields: OperationField[];
  sensitive_keys: string[];
}

export interface HostSummary {
  host_id: string;
  profile_path: string;
  error: string | null;
  capabilities: string[];
  adapter: {
    platform_profile: string;
    transport: string;
    supervisor: string;
    packages: string;
    capabilities: string[];
  } | null;
  platform?: string;
  driver?: string;
  paths?: Record<string, string>;
  app?: {
    hub_https_port: number;
    livekit_client_url: string;
    allow_insecure_livekit: boolean;
  } | null;
  source_overrides?: string[];
  active_run: string | null;
}

export interface HostDetail extends HostSummary {
  operations: OperationSpec[];
  readiness: Record<string, string>;
}

export interface Preview {
  operation: string;
  label: string;
  parameters: Record<string, unknown>;
  plan: Plan;
  applies: boolean;
  mutating: boolean;
  confirmation: Confirmation;
}

export type PhaseStatus = "running" | "done" | "failed";

export interface Phase {
  name: string;
  status: PhaseStatus;
  began_at: number;
  ended_at: number | null;
  detail: unknown;
}

export type RunStatus = "running" | "completed" | "failed";

export interface RunSummary {
  id: string;
  host_id: string;
  operation: string;
  label: string;
  parameters: Record<string, unknown>;
  confirmation: Confirmation;
  mutating: boolean;
  status: RunStatus;
  outcome: Outcome | null;
  started_at: number;
  finished_at: number | null;
  error: string | null;
  phases: Phase[];
}

export interface Run extends RunSummary {
  plan: Plan;
  evidence: Record<string, unknown> | null;
}

export type ParameterValues = Record<string, string | number | boolean | null>;
