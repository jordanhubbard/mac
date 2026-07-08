export type WorkbenchView =
  | "cockpit"
  | "work"
  | "task"
  | "workflows"
  | "agents"
  | "runtime"
  | "observability"
  | "connections";

const ITEMS: Array<{ id: WorkbenchView; icon: string; label: string }> = [
  { id: "cockpit", icon: "dashboard", label: "Cockpit" },
  { id: "work", icon: "tools", label: "Work" },
  { id: "workflows", icon: "type-hierarchy-super", label: "Workflows" },
  { id: "agents", icon: "organization", label: "Agents" },
  { id: "runtime", icon: "server-process", label: "Runtime" },
  { id: "observability", icon: "pulse", label: "Observability" },
  { id: "connections", icon: "plug", label: "Connections" },
];

export function ActivityRail({
  active,
  onChange,
}: {
  active: WorkbenchView;
  onChange: (view: WorkbenchView) => void;
}) {
  return (
    <nav className="activity-rail" aria-label="Workbench views">
      {ITEMS.map((item) => (
        <button
          aria-current={active === item.id ? "page" : undefined}
          className={active === item.id ? "active" : ""}
          key={item.id}
          onClick={() => onChange(item.id)}
          title={item.label}
          type="button"
        >
          <i aria-hidden="true" className={`codicon codicon-${item.icon}`} />
          <span>{item.label}</span>
        </button>
      ))}
      <div className="rail-spacer" />
      <button onClick={() => onChange("connections")} title="Settings" type="button">
        <i aria-hidden="true" className="codicon codicon-settings-gear" />
        <span>Settings</span>
      </button>
    </nav>
  );
}
