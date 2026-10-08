# Soul graph (experimental)

A soul graph is an agent's long-term memory kept as a graph rather than a
flat file. It lives in `src/mac/soul_graph.py`, is served to agents over MCP
by `src/mac/soul_mcp.py`, and can prime a session through
`src/mac/soul_seed.py`. Every host's Hermes gets it through
`mac.soul_install` (see [Where it is installed](#where-it-is-installed)). This
page says what it is, how it is installed and rolled out, and what replaying
real history through it showed.

## The model

Every memory, decision or belief is a node with text content. Four structures
sit over the nodes:

| Structure | What it records | Used by |
|---|---|---|
| DAG edges | causal or semantic lineage: this node was built on that one | `related`, `discover`, `ancestors`, `path` |
| Splay order | how recently and how often a node was used | `hot`, and as a boost in search |
| Tags | cross-cutting dimensions (`axiom`, `lesson`, `project:<name>`) | `by_tag`, `tagged`, search candidates |
| Pins | axioms that never decay | always at the top of `hot` |

A node's recency score is `(access_count + 1) * exp(-age / 1 day)`. The splay
tree is keyed on `log(access_count + 1) + last_accessed / 1 day`, which orders
nodes exactly as the score does at every moment (the `now` term is common to
all nodes), so the tree never goes stale and `hot()` is a read of it.

Search uses an inverted index. Each query word a node shares counts by its
inverse document frequency, then the total is boosted by recency
(`recency_weight` scales the boost; 0 turns it off).

An exploration branch is a copy of the graph for trying out hypotheses. A
hypothesis enters the main graph only when it is promoted.

## Running it

```console
python -m mac.soul_mcp [--soul-file PATH]
```

The server speaks MCP (JSON-RPC 2.0) over stdio. The soul file defaults to
`soul.json` in the Hermes home: `$HERMES_HOME`, else `~/.hermes` (or
`$MAC_HOME/hermes` when `MAC_HOME` relocates MAC), never an OpenClaw path. It is
created on the first write and saved atomically after every tool call that
changes the graph.

| Tool | Use |
|---|---|
| `soul_prime` | Once at session start: the hottest nodes plus a short walk from the node that best matches `context`. |
| `soul_related` | "What earlier work is this built on?" Search hits followed by their DAG parents and children. |
| `soul_query` | Plain search, optionally merged with a second `hint` query. |
| `soul_hot` | The splay root: what the agent has been doing. |
| `soul_discover` | Walk the DAG's children from a seed node. |
| `soul_by_tag` | Every node with a tag. |
| `soul_add`, `soul_link`, `soul_pin`, `soul_splay` | Write: add a node, add an edge (cycles are refused), pin an axiom, mark a node used. |
| `soul_explore`, `soul_promote` | Try a hypothesis in the exploration branch, then move that one node into the graph. |
| `soul_summary` | Counts and the hottest nodes. |

A hand-written DAG in the portable `{"nodes": [...], "edges": [...]}` form
loads with `SoulGraph.from_dag()`. Each node's `label` becomes its content, its
`type` a tag, and `axiom` nodes are pinned.

## Where it is installed

Hermes, the agent's human interface on each host, is the only MAC component
that loads MCP servers from a per-host config. `python -m mac.soul_install
[--hermes-home DIR]` makes three idempotent changes in that Hermes home:

1. **MCP server.** An `mcp_servers.soul` entry in `config.yaml` that runs
   `mac.soul_mcp` with the MAC venv's Python and an explicit `--soul-file
   $HERMES_HOME/soul.json`. Other `mcp_servers` entries are left as they are,
   and `config.yaml` is backed up before any change.
2. **Seed.** If `soul.json` does not exist, it is built from `SOUL.md`,
   `USER.md`, `MEMORY.md` and Hermes' `memories/` copies: one node per entry,
   each section heading the parent of the entries under it, SOUL.md entries
   pinned as axioms. An existing `soul.json` is never touched.
3. **Skill.** `skills/soul-graph/SKILL.md`, which tells the agent the tools
   exist and how to use them. It is installed only next to the MCP entry, and
   a hand-written skill of the same name is left alone.

A host without a Hermes `config.yaml` is skipped. `scripts/fleet-update` runs
it on every host it updates, after the import check and before services
restart, and `deploy/hermes/install-hermes-gateway.sh` runs it on a new host.
Neither treats a failure as fatal.

Coding agents (Claude Code, opencode) do not get it. They run in an OpenShell
sandbox that cannot see `$HERMES_HOME`, and what they learn about a task
belongs to the task's board and evidence, not to one agent's memory.

### Rolling it out

```console
scripts/fleet-update --hermes --yes all <sha>
```

`--hermes` is what makes the tools live: the install step edits Hermes'
config, and Hermes reads MCP servers only when it starts. Without `--hermes`
the config is in place but the tools appear at the next Hermes restart.
There is no other per-host step.

To check a host, use Hermes' own MCP client, which connects to the server and
lists its tools:

```console
# Linux worker: the gateway's runtime interpreter
HPY=$(systemctl --user cat hermes-gateway | sed -n 's/^ExecStart=\([^ ]*python\).*/\1/p')
HERMES_HOME=~/.hermes "$HPY" -m hermes_cli.main mcp test soul
#   ✓ Connected   ✓ Tools discovered: 13
HERMES_HOME=~/.hermes "$HPY" -m hermes_cli.main mcp list    # soul ... ✓ enabled
ls -l ~/.hermes/soul.json ~/.hermes/skills/soul-graph/SKILL.md
```

On the macOS hub the interpreter is
`~/.mac/hermes-runtimes/<release>/runtime/.venv/bin/python`. Re-running
`~/.mac/venv/bin/python -m mac.soul_install` is safe and reports
`"mcp_server": "unchanged", "seed": "exists"` on a host that already has it.

Then ask the agent, in Slack, to run `soul_summary`.

## Does it recall the right things?

`scripts/soul-graph-eval.py` replays a task ledger through a soul graph at the
ledger's own timestamps. Each task becomes a node, tagged with its project.
Its dependencies become DAG edges. A task touches the dependencies it uses,
and is touched itself when it is updated. As each task is created, before it
is added, every retrieval method is asked two questions using only the task's
title:

- **built on**: which existing tasks does this one depend on?
- **waiting on**: which existing tasks declared a dependency on this one before
  it existed?

The real dependency list is the answer key. Most dependencies were created
seconds earlier by the same plan decomposition, and pure recency finds those.
The split that tests memory is **long range**: answers created at least six
hours before the question.

Results on the full MAC task ledger: 11,443 tasks, 9,091 edges and 7,046
questions, replayed in 25 seconds. Each retrieval method took under half a
millisecond per question.

| Long-range question | Questions | recent | keyword | BM25 | soul `semantic_search` | soul `related` | soul `related`, no recency |
|---|---|---|---|---|---|---|---|
| built on, recall@5 | 78 | 0.03 | 0.20 | 0.19 | 0.22 | **0.60** | 0.56 |
| waiting on, recall@5 | 1,422 | 0.00 | 0.59 | 0.62 | 0.62 | **0.67** | 0.63 |
| waiting on, recall@10 | 1,422 | 0.01 | 0.64 | 0.67 | 0.68 | **0.72** | 0.68 |

What this shows:

- **The DAG is what earns its keep.** On "built on", search alone finds about
  a fifth of the old dependencies. Following each hit to its parents finds
  three fifths, because the hit is usually a recent sibling and the old
  foundation is its parent.
- **Recency helps a little, alongside the DAG.** With the DAG, the recency
  boost adds about four points on the long-range questions and six to eight
  on all questions. Before search weighted words by rarity, the boost made
  old answers harder to find.
- **`hot()` is not retrieval.** It recalls almost none of these answers. It
  describes what the agent has been doing, which is what `soul_prime` uses it
  for.

The test `tests/test_soul_graph_eval.py` runs the harness on
`tests/fixtures/soul_graph/ledger-sample.json`. The sample is a scrubbed
slice of the same ledger: 1,091 tasks, with renumbered ids, shifted times and
role names in place of hosts. The test pins the result that `related` beats
keyword search and BM25 on long-range recall.

To rerun the full evaluation against a live hub:

```console
mac task list --all --all-states --full-ids > /tmp/tasks.json
scripts/soul-graph-eval.py /tmp/tasks.json          # or --json, --project P
```

## Limits and open questions

- Search is lexical. An embedding layer would replace the word index behind
  `semantic_search`; the harness is how to tell whether it helps.
- The evaluation's answer key comes from task dependencies, which are a proxy
  for an agent's memory. Agents' own soul files are still a few kilobytes of
  markdown with no edges, so they cannot answer the question yet. Ingesting
  memories with lineage, from conversations or journals, is the next corpus to
  test.
- Only Hermes has it. Whether coding agents should get a read-only view of
  an agent's soul is open; it would need a path through the sandbox boundary.
