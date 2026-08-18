// A report, readable without leaving the page.
//
// Ops reports are deep and the interesting leaf is usually a boolean, so
// booleans are coloured and objects past the first level start closed. This is
// the fallback view: an operation whose report has a shape worth showing
// properly gets that in Evidence.tsx, and everything else lands here rather
// than being summarized into something that is no longer the evidence.

import type { ReactNode } from "react";

const OPEN_DEPTH = 1;

export function Json({ value, depth = 0 }: { value: unknown; depth?: number }) {
  return <div className="json">{render(value, depth)}</div>;
}

function render(value: unknown, depth: number): ReactNode {
  if (value === null) {
    return <span className="null">null</span>;
  }
  if (typeof value === "boolean") {
    return <span className={value ? "bool-true" : "bool-false"}>{String(value)}</span>;
  }
  if (typeof value === "number") {
    return <span className="num">{value}</span>;
  }
  if (typeof value === "string") {
    return <span className="str">{value.includes("\n") ? <pre>{value}</pre> : value}</span>;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return <span className="null">[]</span>;
    }
    return (
      <details open={depth < OPEN_DEPTH}>
        <summary>[{value.length}]</summary>
        <ul>
          {value.map((item, index) => (
            <li key={index}>{render(item, depth + 1)}</li>
          ))}
        </ul>
      </details>
    );
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) {
      return <span className="null">{"{}"}</span>;
    }
    return (
      <details open={depth < OPEN_DEPTH}>
        <summary>{summarize(entries)}</summary>
        <ul>
          {entries.map(([key, item]) => (
            <li key={key}>
              <span className="key">{key}</span>
              {": "}
              {render(item, depth + 1)}
            </li>
          ))}
        </ul>
      </details>
    );
  }
  return <span className="null">{String(value)}</span>;
}

// Whatever a verdict-ish key says, so a closed node still reports its status.
function summarize(entries: [string, unknown][]): string {
  for (const key of ["status", "healthy", "ok", "phase"]) {
    const found = entries.find(([name]) => name === key);
    if (found) {
      return `{${key}: ${String(found[1])}, …${entries.length - 1}}`;
    }
  }
  return `{${entries.length}}`;
}
