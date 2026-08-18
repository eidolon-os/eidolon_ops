// What the plan said, against what has actually happened so far.
//
// The skeleton is the plan's declared steps, drawn before anything runs — that
// is what makes a twenty-minute release legible from the first second. Phases
// the plan never declared are still shown, marked as such: they are real work,
// and the asymmetry is the honest one, since the plan is what was approved.

import { useState } from "react";

import type { Phase, Plan, RunStatus } from "../types";
import { Json } from "./Json";

// `unreported` is a declared step the operation never produced evidence for.
// Not every plan step reports a phase — `doctor` names three and reports none —
// and leaving those as pending after the run finished would read as "never ran".
type RowStatus = "pending" | "running" | "done" | "failed" | "unreported";

interface Row {
  name: string;
  description: string;
  declared: boolean;
  status: RowStatus;
  seconds: number | null;
  detail: unknown;
}

export function Timeline({
  plan,
  phases,
  runStatus,
}: {
  plan: Plan;
  phases: Phase[];
  runStatus: RunStatus;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const rows = merge(plan, phases, runStatus);
  return (
    <ul className="steps">
      {rows.map((row) => (
        <li key={row.name}>
          <span className={`dot ${row.status}${row.declared ? "" : " extra"}`} />
          <div>
            <button
              className="op-item"
              style={{ padding: 0, fontSize: "var(--fs-mono)" }}
              onClick={() => setOpen(open === row.name ? null : row.name)}
              disabled={row.detail === null || row.detail === undefined}
            >
              {row.name}
              {row.declared ? "" : " ·未在计划内"}
            </button>
            <div className="desc">{row.description}</div>
            {open === row.name && row.detail ? <Json value={row.detail} /> : null}
          </div>
          <span className="duration">
            {row.status === "unreported"
              ? "未单独报告"
              : row.seconds === null
                ? ""
                : `${row.seconds.toFixed(1)}s`}
          </span>
        </li>
      ))}
    </ul>
  );
}

function merge(plan: Plan, phases: Phase[], runStatus: RunStatus): Row[] {
  const described = new Map(plan.steps.map((step) => [step.id, step.description]));
  const seen = new Set<string>();
  const rows: Row[] = [];
  for (const phase of phases) {
    seen.add(phase.name);
    rows.push({
      name: phase.name,
      description: described.get(phase.name) ?? "该操作报告的阶段",
      declared: described.has(phase.name),
      status: phase.status,
      seconds:
        phase.ended_at === null ? null : Math.max(0, phase.ended_at - phase.began_at),
      detail: phase.detail,
    });
  }
  for (const step of plan.steps) {
    if (!seen.has(step.id)) {
      rows.push({
        name: step.id,
        description: step.description,
        declared: true,
        status: runStatus === "running" ? "pending" : "unreported",
        seconds: null,
        detail: null,
      });
    }
  }
  return rows;
}
