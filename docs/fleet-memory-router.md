# Fleet memory query router

Fleet memory is partitioned by question type. The stores are not stages in a
general fallback cascade: route a question to the store that owns its category,
and use only the fallback named for that route.

| Question type | Primary layer | Probe method | Miss criterion | Fallback |
|---|---|---|---|---|
| What do I know or believe about X? | `soul_graph` identity and reasoning graph | `soul_fisheye(X)` for ranked focal hits plus their DAG neighbors | No focal node has a score above the configured retrieval threshold | `soul_query(X)` for lexical recall without neighbor expansion |
| What did we collectively decide about X? | Fleet KV in PostgreSQL `memory_records` | List memory keys matching `architecture.<domain>` | No matching `architecture.*` key | `soul_related(X)` for settled knowledge and its graph lineage |
| What are peers working toward? | Qdrant `reasoning_atoms` (2048-dimensional cosine vectors) | Filter to peer atoms on the same thread, then compute `cosine(X, peer.next_focus)` | All relevant scores are `< 0.4`: no peer is on the same trajectory | AgentBus broadcast to ask peers directly |
| What happened in past tasks involving X? | Qdrant `mac_memory_medium` | Semantic search of task summaries | Best result is below the search service's configured similarity threshold | Repeat semantic recall against `mac_memory_long` |

`architecture.*` is the namespace for reviewed, settled fleet conventions. Do
not infer a collective decision from an unnamespaced KV record or publish a new
architectural decision as a reasoning atom. The Qdrant medium and long stores
hold historical task summaries; they are not authorities for current identity,
decisions, or peer intent.

## Similar-looking cosine probes

Read-before-`soul_add` and read-before-publishing a reasoning atom both use
similarity, but they make different decisions and therefore use different
threshold semantics:

| Probe | Compared values | Threshold | Meaning and action |
|---|---|---|---|
| Soul deduplication | Candidate concept against existing soul nodes returned by `soul_related` or `soul_query` | `cosine >= 0.7` (or an unambiguously identical concept) | The knowledge already exists. Do not add a duplicate root; link a refinement, instance, convergence, or contradiction to the existing node. Below the threshold, add and link the novel node. |
| Trajectory coordination | Candidate insight against each other author's `next_focus` on the same reasoning thread | `cosine >= 0.4` means overlap; `< 0.4` for every peer means divergence | At or above the threshold, a peer is working in the same territory, so extend, confirm, or cite instead of duplicating. When every peer is below it, publish the divergent trajectory. Empirical insights remain independently publishable. |

The first probe asks whether knowledge is already represented. The second asks
whether agents are heading toward the same work. A soul miss must not trigger a
reasoning-atom lookup, and a divergent trajectory must not be interpreted as a
knowledge miss.
