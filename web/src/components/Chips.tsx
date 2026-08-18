// The plan's vocabulary, rendered. Every badge here is a field of the Plan the
// server built — nothing about danger is decided in the browser.

import type { ActionKind, Destructive, Outcome, RunStatus } from "../types";

const OUTCOME_TONE: Record<Outcome, string> = {
  observed: "good",
  planned: "on",
  applied: "good",
  degraded: "warn",
  refused: "bad",
  failed: "bad",
};

const OUTCOME_LABEL: Record<Outcome, string> = {
  observed: "已观测",
  planned: "已出计划",
  applied: "已执行",
  degraded: "降级",
  refused: "已拒绝",
  failed: "失败",
};

const TOUCH_LABEL: Record<ActionKind, string> = {
  config: "配置",
  secret: "秘密",
  schema: "schema",
  data: "权威数据",
  lifecycle: "生命周期",
  code: "代码",
};

const DESTRUCTIVE_LABEL: Record<Destructive, string> = {
  none: "无破坏性",
  reversible: "可走回来",
  irreversible: "不可逆",
};

export function OutcomeChip({
  outcome,
  status,
}: {
  outcome: Outcome | null;
  status: RunStatus;
}) {
  if (status === "running") {
    return <span className="chip on">运行中</span>;
  }
  if (outcome === null) {
    return <span className="chip bad">未完成</span>;
  }
  return (
    <span className={`chip ${OUTCOME_TONE[outcome]}`}>{OUTCOME_LABEL[outcome]}</span>
  );
}

export function DestructiveChip({ level }: { level: Destructive }) {
  const tone = level === "irreversible" ? "danger" : level === "reversible" ? "warn" : "";
  return <span className={`chip ${tone}`}>{DESTRUCTIVE_LABEL[level]}</span>;
}

export function TouchChips({ touches }: { touches: ActionKind[] }) {
  if (touches.length === 0) {
    return <span className="chip good">只读</span>;
  }
  return (
    <>
      {touches.map((kind) => (
        <span key={kind} className={`chip ${kind === "data" ? "bad" : "on"}`}>
          {TOUCH_LABEL[kind] ?? kind}
        </span>
      ))}
    </>
  );
}

export function HealthChip({ value }: { value: unknown }) {
  const ok = value === true || value === "healthy" || value === "app_ready" || value === "ok";
  return <span className={`chip ${ok ? "good" : "bad"}`}>{ok ? "通过" : "未通过"}</span>;
}
