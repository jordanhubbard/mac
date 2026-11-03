# Hierarchical memory access tiers

Status: design proposal; enforcement is not implemented. Task:
`task_ce50ecda2f1140f6a5dde1f7e74af87f`.

## Finding

MAC memory is currently a shared, unfiltered collection. `memory_records` stores
content and provenance but no visibility, owner, or group audience. The
`MemoryService.get_memory()` and `search_memory()` read rows without a caller
identity or mandatory authorization predicate. Their filters (`subject_*`,
`record_type`, and `created_by`, among others) select content; they do not grant
access. A caller can omit those filters, and `created_by` is provenance rather
than an access-control owner.

Consequently, a tagging convention alone cannot provide confidentiality. A tag
such as `personal` is data controlled by the writer, while the current read path
still returns matching and non-matching rows to any client with service or
database access. Direct database credentials also bypass any MCP convention.

## Proposed model

Every memory has exactly one visibility tier and the corresponding audience:

| Tier | Required audience | Intended content | Read rule |
| --- | --- | --- | --- |
| `owner` | `owner_agent_id` | Soul/persona state, personal preferences, private reflection | Only that agent identity (and an explicit audited administrator capability) |
| `group` | `group_id` | Team context such as devops practices or a project working set | An identity with active membership in that group |
| `fleet` | none | Shared operational knowledge safe for every agent in the fleet | Any authenticated identity in the same fleet |

All three tiers are also bounded by `tenant_id` (the hub/user security domain)
and `fleet_id`. Neither a group nor a fleet name is globally meaningful. Reads
must first match the caller's tenant and fleet, then apply the tier rule. The
owner tier does not mean "created by": a service may write on an owner's behalf,
and ownership must remain stable and explicit.

Suggested relational fields on `memory_records` are:

```text
tenant_id       NOT NULL
fleet_id        NOT NULL
visibility      NOT NULL CHECK IN ('owner', 'group', 'fleet')
owner_agent_id  NULL
group_id        NULL
```

Add constraints so `owner_agent_id` is present only for `owner`, `group_id` is
present only for `group`, and neither is present for `fleet`. Keep `created_by`
as provenance. Vector payloads must duplicate the immutable authorization
attributes for candidate filtering, but a vector hit is only a candidate: load
the authoritative memory row and authorize it before returning content.

Group membership belongs in an authoritative hub table keyed by
`(tenant_id, fleet_id, group_id, agent_id)`, with lifecycle and audit data. It
must not be inferred from a prompt, client-supplied list, subject tag, or stale
vector payload.

## Enforcement boundary

The mandatory decision belongs in the MAC memory application service, before
all reads and writes. Today the supported paths converge on `MemoryService` via
the hub API/CLI; an MCP memory tool must call the same authorized service rather
than query storage itself. The boundary must receive a server-authenticated
principal containing tenant, fleet, agent identity, and explicit capabilities.
It must not trust an `owner_agent_id`, group membership list, or administrator
flag supplied as ordinary request data.

For every operation:

1. Authenticate the transport and construct the principal server-side.
2. On create, validate that the principal may write to the requested audience.
   Normal agents may create owner memory only for themselves, group memory only
   for a current group, and fleet memory only with a dedicated publish
   capability.
3. On get, search, list, export, vector recall, update, and delete, add the
   mandatory tenant/fleet/tier predicate independently of optional search
   filters. Fetch-by-ID must return the same not-found response for absent and
   unauthorized rows.
4. Authorize the authoritative row again before returning content or mutating
   it. Audit denied access and privileged override without logging memory
   content.

Authorization must fail closed when identity or group resolution is unavailable.
Background jobs (decay, consolidation, re-embedding, backup, and migration)
need named service principals with only the audiences required by their job;
being an internal process is not implicit fleet-wide read authority.

### Database row-level security versus application checks

Application-layer enforcement is required because MAC supports SQLite, which
has no row-level security, and because semantic/vector retrieval spans stores.
It also provides one policy for API, CLI, MCP, and background consumers.

Where production uses PostgreSQL, enable row-level security as defense in depth:
set trusted transaction-local principal attributes, force RLS for the service
role and table owner, and express the same tenant/fleet/tier policy in `USING`
and `WITH CHECK` clauses. The runtime role must not have `BYPASSRLS`; migration
and break-glass roles must be separate and audited. RLS does not replace the
application check, and an application check is ineffective if agents retain
direct database or Qdrant credentials. Normal agents and MCP processes must
receive only the memory-service credential; storage networks and grants must
reject direct access.

## API contract

- Create requests require an explicit `visibility`; `owner_agent_id` and
  `group_id` are accepted only where the tier requires them. There is no silent
  default after migration.
- Search does not expose an unrestricted mode. It may accept a narrower tier or
  audience filter, but cannot broaden the principal's authorized set.
- Responses include visibility and audience metadata when the caller can read
  the row, allowing downstream caches to retain the security label.
- Batch, count, and semantic-search endpoints apply the same policy. Counts and
  existence checks can leak private records and are not exceptions.
- Caches are keyed by principal and membership version, or cache only record IDs
  and reauthorize each result. Membership removal invalidates group access.

## Migration and rollout

1. Add the fields and constraints while reads remain on the old path. Inventory
   all writers, readers, direct database users, vector consumers, exports, and
   backups before claiming enforcement coverage.
2. Backfill deterministically. Explicit soul/personal record classes become
   `owner` only when an unambiguous owner identity can be resolved. Known team
   records become `group` only from authoritative membership/configuration.
   Existing curated shared operational records may become `fleet`. Quarantine
   ambiguous rows for an administrator decision; do not guess them public.
3. Dual-write relational rows and vector payload labels. Re-index existing
   vectors and reject unlabeled vector candidates.
4. Deploy mandatory application predicates in report-only comparison mode,
   then enforce them. Remove direct storage credentials from agents before
   describing owner memory as private.
5. If PostgreSQL is used, enable and force matching RLS after application
   enforcement is verified. SQLite deployments remain supported with exclusive
   storage ownership by the memory service.
6. Remove compatibility paths for unlabeled rows. Make `visibility` and the
   relevant audience non-null under the tier constraints.

Rollback may disable new writes and revert service binaries, but must not make
new owner/group rows readable through the legacy unfiltered endpoint. Keep the
legacy path disabled or take memory reads offline until enforcement is restored.

## Required implementation proof

- A matrix test covers each operation for owner, same-group, other-group,
  fleet peer, cross-fleet, cross-tenant, administrator, and unauthenticated
  principals, including denied counts and fetch-by-ID non-disclosure.
- A hostile client cannot broaden access with forged owner/group fields,
  optional filters, batch IDs, stale memberships, cache reuse, or vector
  payload manipulation.
- API, CLI, MCP, semantic recall, and every background job use the same policy;
  an inventory test prevents a newly added read route from omitting a principal.
- PostgreSQL integration tests prove RLS for direct SQL when that backend is
  configured. SQLite tests prove agents have no direct file or service
  credential and that application checks fail closed.
- Migration tests cover each classified record family and quarantine ambiguous
  rows. Deployment verifies no unlabeled relational rows or vector points
  remain before enforcement is declared complete.
- Audit events identify principal, operation, record ID, audience, decision,
  and policy version without including private content.

## Ownership and follow-up

Implementation is a separate task. Ownership depends on the deployed boundary:
the team controlling MAC's memory/MCP service owns authenticated-principal
propagation and application enforcement; the team controlling production
database grants owns storage isolation and optional PostgreSQL RLS. These are
joint completion requirements, not alternative implementations.
