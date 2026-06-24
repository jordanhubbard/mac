import Editor from "@monaco-editor/react";
import { type TaskDetail } from "../api/mac";

function renderDoc(detail: TaskDetail | null): string {
  if (!detail) {
    return [
      "# MAC — Fleet IDE",
      "",
      "Select a task on the left to view its detail, evidence, and the",
      "per-task activity narrative. Dispatch new work from the Agents panel →.",
    ].join("\n");
  }
  const t = detail.task;
  const act = (t.metadata?.activity as any[]) || [];
  const lines: string[] = [
    `# ${t.title || t.id}`,
    "",
    `state: ${t.state}    project: ${t.project}    id: ${t.id}`,
    "",
    "## Description",
    t.description || "(none)",
    "",
    "## Activity",
  ];
  if (act.length) {
    for (const e of act) {
      lines.push(`• ${e.phase} / ${e.actor} @ ${String(e.at || "").slice(0, 19)}`);
      for (const l of String(e.summary || "").split("\n")) lines.push("    " + l);
    }
  } else {
    lines.push("(none yet)");
  }
  return lines.join("\n");
}

export function EditorArea({ detail }: { detail: TaskDetail | null }) {
  const tabName = detail ? detail.task.title || detail.task.id : "Welcome";
  return (
    <div className="editor-wrap">
      <div className="tabs">
        <div className="tab active">{tabName.slice(0, 48)}</div>
      </div>
      <div className="editor-mono">
        <Editor
          theme="vs-dark"
          language="markdown"
          path={detail?.task.id || "welcome.md"}
          value={renderDoc(detail)}
          options={{
            readOnly: true,
            minimap: { enabled: false },
            wordWrap: "on",
            fontSize: 13,
            scrollBeyondLastLine: false,
          }}
        />
      </div>
    </div>
  );
}
