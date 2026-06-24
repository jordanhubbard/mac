# MAC — Fleet IDE (greenfield)

A VS Code / Cursor-style shell over the MAC control plane. Replaces the legacy
`src/mac/ui` dashboard SPA with a React + TypeScript + Monaco app.

**Layout** (maps the Cursor template onto fleet data):
- **Activity bar + Sidebar** — projects/repos and the task ledger as a tree.
- **Editor (Monaco)** — task detail + the per-task activity narrative (and, next,
  the diff a task produced).
- **Bottom panel** — Activity / Evidence / History for the selected task.
- **Agents panel (right)** — live fleet agents + a composer to **dispatch a task**
  (the analogue of Cursor's "New Agent"). This is wired to `POST /tasks`.

## Run (dev)

```bash
cd ide
npm install
MAC_API_URL=http://100.72.16.110:8789 npm run dev   # hub to talk to
# open http://localhost:5273, paste a hub bearer token when prompted
```

`/api/*` is proxied to the hub (see `vite.config.ts`). The token is stored in
`localStorage["mac.token"]` (or set `VITE_MAC_TOKEN`).

## Status — first increment

Runnable IDE shell with live data: task tree, task detail + activity in Monaco,
activity/evidence/history panel, and the Agents panel with dispatch.

## Next

- Diff view (Monaco diff) of a task's branch vs main, from the evidence repo.
- Open real repo files in the editor (file tree → editor), not just task docs.
- Live activity streaming (SSE) instead of 5s polling.
- Per-agent live terminal (the AgentBus debug-terminal streams) in the bottom panel.
- Wrap in the existing `desktop/` Electron app; retire `src/mac/ui`.
