import { type Task } from "../api/mac";

export function Sidebar({
  tasks,
  selected,
  onSelect,
}: {
  tasks: Task[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const byProject: Record<string, Task[]> = {};
  for (const t of tasks) {
    const key = t.project || "(unassigned)";
    (byProject[key] ||= []).push(t);
  }
  const label = (t: Task) =>
    t.title || (t.description || "").split("\n")[0] || t.id;

  return (
    <div className="panel">
      <div className="panel-title">Fleet · Tasks</div>
      {Object.keys(byProject)
        .sort()
        .map((proj) => (
          <div className="tree-group" key={proj}>
            <div className="row">
              ▾ {proj} <span className="muted">({byProject[proj].length})</span>
            </div>
            {byProject[proj].map((t) => (
              <div
                key={t.id}
                className={"row" + (t.id === selected ? " selected" : "")}
                style={{ paddingLeft: 26 }}
                title={label(t)}
                onClick={() => onSelect(t.id)}
              >
                <span className={"dot " + (t.state || "open")} />
                <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                  {label(t)}
                </span>
              </div>
            ))}
          </div>
        ))}
    </div>
  );
}
