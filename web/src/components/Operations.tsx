// Choose an operation, see what it would be, then say so out loud.
//
// The plan is re-asked on every change, so ticking `--wipe-authority-data`
// changes the badges and the confirmation in front of the operator rather than
// after them. What the confirmation block demands is whatever the server said it
// demands: this panel renders the requirement, it does not decide it.

import { useEffect, useMemo, useState } from "react";

import { fetchPlan, startRun } from "../api";
import type { HostDetail, OperationField, OperationSpec, ParameterValues, Preview } from "../types";
import { DestructiveChip, TouchChips } from "./Chips";

const GROUPS: { id: OperationSpec["group"]; label: string }[] = [
  { id: "observe", label: "只读观测" },
  { id: "lifecycle", label: "生命周期" },
  { id: "release", label: "发布与基础环境" },
  { id: "authority", label: "权威数据" },
  { id: "secret", label: "私密输入" },
];

export function Operations({
  host,
  onStarted,
}: {
  host: HostDetail;
  onStarted: (runId: string) => void;
}) {
  const [selected, setSelected] = useState<string>(host.operations[0]?.name ?? "");
  const spec = host.operations.find((item) => item.name === selected);
  const [values, setValues] = useState<ParameterValues>({});
  const [preview, setPreview] = useState<Preview | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [acknowledge, setAcknowledge] = useState(false);
  const [typed, setTyped] = useState("");
  const [startError, setStartError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    // Keyed to which operation is selected, not to the object the last poll
    // handed us. The host detail is re-fetched on a timer, so `spec` is a new
    // object every few seconds — keying the form's lifetime to that identity
    // emptied every input the operator had typed, on a timer, silently.
    setValues(defaults(host.operations.find((item) => item.name === selected)));
    setPreview(null);
    setPlanError(null);
    setAcknowledge(false);
    setTyped("");
    setStartError(null);
  }, [selected]);

  const serialized = useMemo(() => JSON.stringify(values), [values]);

  useEffect(() => {
    if (!spec) {
      return;
    }
    let live = true;
    const operation = spec.name;
    const timer = window.setTimeout(() => {
      fetchPlan(host.host_id, operation, JSON.parse(serialized) as ParameterValues)
        .then((value) => {
          if (live) {
            setPreview(value);
            setPlanError(null);
          }
        })
        .catch((error: Error) => {
          if (live) {
            setPreview(null);
            setPlanError(error.message);
          }
        });
    }, 180);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [host.host_id, selected, serialized]);

  if (host.operations.length === 0) {
    return <div className="notice">这台 Host 目前不能被要求任何操作：{host.error}</div>;
  }

  const blocked = preview?.mutating === true && host.active_run !== null;
  const needsAck = preview !== null && preview.confirmation !== "none";
  const needsTyped = preview?.confirmation === "typed-host-id";
  const ready =
    preview !== null &&
    !blocked &&
    (!needsAck || acknowledge) &&
    (!needsTyped || typed.trim() === host.host_id);

  async function submit() {
    if (!spec || !preview) {
      return;
    }
    setStarting(true);
    setStartError(null);
    try {
      const run = await startRun(host.host_id, spec.name, values, {
        acknowledge,
        host_id: typed.trim() || undefined,
      });
      onStarted(run.id);
      setAcknowledge(false);
      setTyped("");
    } catch (error) {
      setStartError((error as Error).message);
    } finally {
      setStarting(false);
    }
  }

  return (
    <div className="ops-layout">
      <div className="panel op-list">
        {GROUPS.map((group) => {
          const items = host.operations.filter((item) => item.group === group.id);
          if (items.length === 0) {
            return null;
          }
          return (
            <div className="op-group" key={group.id}>
              <div>{group.label}</div>
              {items.map((item) => (
                <button
                  key={item.name}
                  className={`op-item${item.name === selected ? " on" : ""}`}
                  onClick={() => setSelected(item.name)}
                >
                  {item.name}
                  <div className="desc" style={{ whiteSpace: "normal" }}>
                    {item.label}
                  </div>
                </button>
              ))}
            </div>
          );
        })}
      </div>

      {spec ? (
        <div className="stack">
          <div className="panel">
            <h2>{spec.name}</h2>
            <p style={{ margin: "0 0 12px", fontSize: "var(--fs-body)" }}>{spec.summary}</p>
            {spec.fields
              .filter((field) => !field.gate)
              .map((field) => (
                <FieldInput
                  key={field.name}
                  field={field}
                  value={values[field.name] ?? null}
                  onChange={(value) => setValues({ ...values, [field.name]: value })}
                />
              ))}
            {spec.fields
              .filter((field) => field.gate)
              .map((field) => (
                <div className="field gate" key={field.name}>
                  <FieldInput
                    field={field}
                    value={values[field.name] ?? null}
                    onChange={(value) => setValues({ ...values, [field.name]: value })}
                  />
                  <div className="help">不勾选时只出计划，这台 Host 不会被改动。</div>
                </div>
              ))}
          </div>

          {planError ? <div className="notice">{planError}</div> : null}

          {preview ? (
            <div className="panel">
              <h2>计划</h2>
              <div className="row" style={{ marginBottom: 10 }}>
                <DestructiveChip level={preview.plan.destructive} />
                <TouchChips touches={preview.plan.touches} />
                {preview.mutating ? (
                  <span className="chip warn">会真的执行</span>
                ) : preview.applies ? null : (
                  <span className="chip">只出计划</span>
                )}
                {preview.plan.requires_flags.map((flag) => (
                  <span key={flag} className="chip on">
                    {flag}
                  </span>
                ))}
              </div>
              <ul className="steps">
                {preview.plan.steps.map((step) => (
                  <li key={step.id}>
                    <span className="dot" />
                    <div>
                      {step.id}
                      <div className="desc">{step.description}</div>
                    </div>
                    <span />
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {blocked ? (
            <div className="notice">
              这台 Host 上已经有一个边界动作在跑（run {host.active_run}）。一次只允许一个。
            </div>
          ) : null}

          {needsAck ? (
            <div className="confirm">
              <h3>
                {needsTyped
                  ? "这是不可逆操作"
                  : "这个操作会改动这台 Host"}
              </h3>
              <label className="field check">
                <input
                  type="checkbox"
                  checked={acknowledge}
                  onChange={(event) => setAcknowledge(event.target.checked)}
                />
                <span>
                  我已读过上面的计划，确认它动的是
                  {preview?.plan.touches.length ? " " : "（只读）"}
                  <TouchChips touches={preview?.plan.touches ?? []} />
                </span>
              </label>
              {needsTyped ? (
                <div className="field">
                  <label>
                    请手输这台 Host 的 id：<code>{host.host_id}</code>
                  </label>
                  <input
                    value={typed}
                    placeholder={host.host_id}
                    onChange={(event) => setTyped(event.target.value)}
                  />
                </div>
              ) : null}
            </div>
          ) : null}

          {startError ? <div className="notice">{startError}</div> : null}

          <div className="row">
            <button
              className={needsTyped ? "danger" : "primary"}
              disabled={!ready || starting}
              onClick={submit}
            >
              {starting ? "正在提交…" : preview?.applies ? "执行" : "出计划并运行"}
            </button>
            <span className="hint">
              等价于{" "}
              <code>
                eidolon-ops --config {host.profile_path} {spec.name}
                {preview?.plan.requires_flags.length
                  ? ` ${preview.plan.requires_flags.join(" ")}`
                  : ""}
              </code>
            </span>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function defaults(spec: OperationSpec | undefined): ParameterValues {
  const values: ParameterValues = {};
  for (const field of spec?.fields ?? []) {
    values[field.name] = field.kind === "boolean" ? Boolean(field.default) : field.default;
  }
  return values;
}

function FieldInput({
  field,
  value,
  onChange,
}: {
  field: OperationField;
  value: string | number | boolean | null;
  onChange: (value: string | number | boolean | null) => void;
}) {
  if (field.kind === "boolean") {
    return (
      <label className="field check">
        <input
          type="checkbox"
          checked={value === true}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span>
          {field.label}
          {field.help ? <div className="help">{field.help}</div> : null}
        </span>
      </label>
    );
  }
  const listId = field.suggestions.length > 0 ? `suggest-${field.name}` : undefined;
  return (
    <div className="field">
      <label>
        {field.label}
        {field.required ? " *" : ""}
      </label>
      {field.choices.length > 0 ? (
        <select value={String(value ?? "")} onChange={(event) => onChange(event.target.value)}>
          {field.choices.map((choice) => (
            <option key={choice} value={choice}>
              {choice}
            </option>
          ))}
        </select>
      ) : (
        <>
          <input
            type={field.kind === "integer" ? "number" : "text"}
            value={value === null ? "" : String(value)}
            list={listId}
            min={field.minimum ?? undefined}
            max={field.maximum ?? undefined}
            onChange={(event) =>
              onChange(
                field.kind === "integer"
                  ? event.target.value === ""
                    ? null
                    : Number(event.target.value)
                  : event.target.value,
              )
            }
          />
          {listId ? (
            <datalist id={listId}>
              {field.suggestions.map((item) => (
                <option key={item} value={item} />
              ))}
            </datalist>
          ) : null}
        </>
      )}
      {field.help ? <div className="help">{field.help}</div> : null}
    </div>
  );
}
