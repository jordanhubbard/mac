import { useCallback, useEffect, useMemo, useState } from "react";
import { ConsoleClient, type Snapshot } from "./lib/api";
import { useLive } from "./lib/useLive";
import { duration } from "./lib/format";
import { LiveView } from "./views/Live";
import { StuckView } from "./views/Stuck";
import { AgentsView, ProjectsView } from "./views/Fleet";
import { CyclesView, PipelinesView, TelemetryView } from "./views/Systems";
import { TaskView } from "./views/Task";
import { MergeQueueView } from "./views/MergeQueue";

/**
 * Same key the legacy dashboard uses, so an operator who already has a session
 * on /ui does not have to paste the token again to open the console.
 */
const TOKEN_KEY = "mac.dashboard.token";

const VIEWS = [
  { id: "live", label: "Live", group: "Movement" },
  { id: "stuck", label: "Stuck work", group: "Movement" },
  { id: "agents", label: "Agents", group: "Fleet" },
  { id: "projects", label: "Projects", group: "Fleet" },
  { id: "pipelines", label: "Pipelines", group: "Delivery" },
  { id: "merge-queue", label: "Merge queue", group: "Delivery" },
  { id: "cycles", label: "Dream & nap", group: "Delivery" },
  { id: "telemetry", label: "Telemetry", group: "Health" },
] as const;

type RailViewId = (typeof VIEWS)[number]["id"];
/**
 * "task" is reachable by clicking a task, not from the rail — it is a detail
 * view of one row, so putting it in the nav would offer a link with nothing
 * behind it.
 */
type ViewId = RailViewId | "task";

const WINDOWS = [1, 6, 24, 72] as const;

function readToken(): string {
  try {
    const params = new URLSearchParams(window.location.search);
    const fromUrl = params.get("t");
    if (fromUrl) {
      sessionStorage.setItem(TOKEN_KEY, fromUrl);
      params.delete("t");
      const search = params.toString();
      history.replaceState(
        null,
        "",
        window.location.pathname + (search ? `?${search}` : "") + window.location.hash,
      );
      return fromUrl;
    }
    return sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

function readView(): ViewId {
  const raw = new URLSearchParams(window.location.search).get("view");
  if (raw === "task") return "task";
  return (VIEWS.find((v) => v.id === raw)?.id ?? "live") as ViewId;
}

function readTaskId(): string | null {
  return new URLSearchParams(window.location.search).get("task");
}

export function App() {
  const [token, setToken] = useState(readToken);
  const [view, setView] = useState<ViewId>(readView);
  const [taskId, setTaskId] = useState<string | null>(readTaskId);
  const [windowHours, setWindowHours] = useState<number>(6);
  const [draftToken, setDraftToken] = useState("");

  const client = useMemo(() => new ConsoleClient(() => token), [token]);
  const live = useLive(client, windowHours, 60);

  // Keep ?view= (and ?task=) in the URL so a view is linkable and survives a
  // reload — the same query-param contract the legacy dashboard publishes.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    params.set("view", view);
    if (view === "task" && taskId) params.set("task", taskId);
    else params.delete("task");
    history.replaceState(null, "", `${window.location.pathname}?${params}`);
  }, [view, taskId]);

  const openTask = useCallback((id: string) => {
    setTaskId(id);
    setView("task");
  }, []);

  const needsToken = live.errorKind === "auth" && !live.snapshot;

  if (needsToken) {
    return (
      <div className="gate">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            const next = draftToken.trim();
            try {
              if (next) sessionStorage.setItem(TOKEN_KEY, next);
            } catch {
              /* private browsing; the token still works for this page load */
            }
            setToken(next);
          }}
        >
          <h1>MAC · observability</h1>
          <p>
            The hub refused this session: <strong>{live.error}</strong>. Paste a hub
            token with the same scope you use for the dashboard.
          </p>
          <input
            type="password"
            value={draftToken}
            autoFocus
            placeholder="hub token"
            aria-label="hub token"
            onChange={(event) => setDraftToken(event.target.value)}
          />
          <button className="primary" type="submit">
            Connect
          </button>
        </form>
      </div>
    );
  }

  const snap = live.snapshot;
  const grouped = VIEWS.reduce<Record<string, typeof VIEWS[number][]>>((acc, item) => {
    (acc[item.group] ??= []).push(item);
    return acc;
  }, {});

  return (
    <div className="shell">
      <div className="topbar">
        <span className="brand">
          mac<span>/</span>observe
        </span>
        <span
          className="pulse"
          data-live={live.liveness === "connecting" ? "stale" : live.liveness}
          title={
            live.ageSeconds === null
              ? "no successful read yet"
              : `last successful read ${duration(live.ageSeconds)} ago`
          }
        >
          <span className="dot" />
          {live.liveness === "connecting"
            ? "connecting"
            : live.liveness === "live"
              ? `live · ${live.stream === "streaming" ? "stream" : "poll"}`
              : `${live.liveness} · ${duration(live.ageSeconds)} old`}
        </span>

        <span style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
          <span className="micro" style={{ alignSelf: "center", marginRight: 4 }}>
            window
          </span>
          {WINDOWS.map((hours) => (
            <button
              key={hours}
              type="button"
              className="ghost"
              aria-pressed={windowHours === hours}
              onClick={() => setWindowHours(hours)}
            >
              {hours}h
            </button>
          ))}
        </span>
        <span className="micro num" title="hub-side assembly time">
          {snap ? `${snap.build_ms.toFixed(0)}ms` : "—"}
        </span>
      </div>

      <nav className="rail">
        {Object.entries(grouped).map(([group, items]) => (
          <div className="rail-group" key={group}>
            <span className="micro">{group}</span>
            {items.map((item) => (
              <a
                key={item.id}
                href={`?view=${item.id}`}
                aria-current={view === item.id ? "page" : undefined}
                onClick={(event) => {
                  event.preventDefault();
                  setView(item.id);
                }}
              >
                {item.label}
              </a>
            ))}
          </div>
        ))}
      </nav>

      <main>
        <Honesty live={live} />
        {snap ? (
          <Router
            view={view}
            snap={snap}
            client={client}
            taskId={taskId}
            onOpenTask={openTask}
            onBack={() => setView("live")}
          />
        ) : live.error ? null : (
          <p className="empty">Reading the hub…</p>
        )}
      </main>
    </div>
  );
}

function Router({
  view,
  snap,
  client,
  taskId,
  onOpenTask,
  onBack,
}: {
  view: ViewId;
  snap: Snapshot;
  client: ConsoleClient;
  taskId: string | null;
  onOpenTask: (id: string) => void;
  onBack: () => void;
}) {
  switch (view) {
    case "task":
      return (
        <TaskView client={client} taskId={taskId} snap={snap} onBack={onBack} />
      );
    case "stuck":
      return <StuckView snap={snap} onOpenTask={onOpenTask} />;
    case "agents":
      return <AgentsView snap={snap} />;
    case "projects":
      return <ProjectsView snap={snap} />;
    case "pipelines":
      return <PipelinesView snap={snap} />;
    case "merge-queue":
      return <MergeQueueView snap={snap} />;
    case "cycles":
      return <CyclesView snap={snap} />;
    case "telemetry":
      return <TelemetryView snap={snap} />;
    case "live":
    default:
      return <LiveView snap={snap} onOpenTask={onOpenTask} />;
  }
}

/**
 * Everything the console knows to be wrong with what it is showing, stated
 * before the data rather than after it. This block is the reason the app is
 * trustworthy: it is never possible to look at a number here without also
 * seeing whether it could be read, and how old it is.
 */
function Honesty({ live }: { live: ReturnType<typeof useLive> }) {
  const snap = live.snapshot;
  return (
    <>
      {live.error && !snap ? (
        <div className="banner critical">
          <span className="icon" aria-hidden="true">
            ▲
          </span>
          <span>
            <strong>Cannot reach the hub.</strong> {live.error}. Nothing below is
            being shown because there is nothing true to show — this is not a fleet
            with zero tasks.
          </span>
        </div>
      ) : null}

      {live.error && snap ? (
        <div className="banner">
          <span className="icon" aria-hidden="true">
            !
          </span>
          <span>
            <strong>
              Showing data from {duration(live.ageSeconds)} ago — the latest read
              failed.
            </strong>{" "}
            {live.error}
          </span>
        </div>
      ) : null}

      {live.schemaMismatch ? (
        <div className="banner critical">
          <span className="icon" aria-hidden="true">
            ▲
          </span>
          <span>
            <strong>Schema mismatch.</strong> The hub returned{" "}
            <code>{live.schemaMismatch}</code>, which this console build does not
            understand. Fields may be missing or misread; upgrade one side.
          </span>
        </div>
      ) : null}

      {live.streamNote ? (
        <div className="banner">
          <span className="icon" aria-hidden="true">
            !
          </span>
          <span>{live.streamNote}</span>
        </div>
      ) : null}

      {snap?.degraded.length ? (
        <div className="banner serious">
          <span className="icon" aria-hidden="true">
            !
          </span>
          <span>
            <strong>
              {snap.degraded.length} section
              {snap.degraded.length === 1 ? "" : "s"} could not be read.
            </strong>{" "}
            Those panels say "unavailable" rather than showing zero.
            <ul style={{ margin: "5px 0 0", paddingLeft: 16 }}>
              {snap.degraded.map((entry) => (
                <li key={entry.section} className="unknown-text">
                  <code>{entry.section}</code> — {entry.reason}
                </li>
              ))}
            </ul>
          </span>
        </div>
      ) : null}
    </>
  );
}
