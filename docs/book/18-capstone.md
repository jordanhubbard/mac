---
schema: mac.docs.chapter.v1
chapter: 18
title: From Request to Production
audiences: [user, operator, integrator, contributor]
timeout_seconds: 120
---

# From Request to Production

The complete system is an evidence pipeline. A human or Hermes expresses intent
as a task. Planning chooses an atomic fast lane or a versioned DAG. The scheduler
assigns bounded work to qualified agents. Executors produce evidence; reviewers
judge exact attempts; assembly and certification evaluate combined work;
publication proves the canonical result; deployment qualifies a cohort; and
operations preserve the audit trail and rollback path.

This compact local exercise revisits the durable core: active project, qualified
identities, leased execution, evidence, independent review, completion, and
cross-resource audit.

```bash
mac --db "$DOCS_DB" admin init
MAC_DB="$DOCS_DB" MAC_API_ALLOW_OPEN=1 \
  uvicorn mac.api:create_app --factory --host 127.0.0.1 \
  --port "$DOCS_PORT" >"$TMPDIR/mac-docs-hub.log" 2>&1 &
hub_pid=$!
trap 'kill "$hub_pid" 2>/dev/null || true; wait "$hub_pid" 2>/dev/null || true' EXIT
for attempt in $(seq 1 30); do
  curl --fail --silent "$DOCS_HUB_URL/health" >/dev/null && break
  sleep 1
done
curl --fail --silent "$DOCS_HUB_URL/health" >/dev/null
mac --hub-url "$DOCS_HUB_URL" project create production-demo --active
mac --hub-url "$DOCS_HUB_URL" admin machine register production-host \
  --machine-id machine_production
executor_registration="$(mac --hub-url "$DOCS_HUB_URL" --json agent register \
  machine_production executor --agent-id agent_executor --capabilities docs)"
reviewer_registration="$(mac --hub-url "$DOCS_HUB_URL" --json agent register \
  machine_production reviewer --agent-id agent_certifier --capabilities review)"
executor_key="$(printf '%s' "$executor_registration" | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["attestation_key"])')"
reviewer_key="$(printf '%s' "$reviewer_registration" | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["attestation_key"])')"
task_id="$(mac --hub-url "$DOCS_HUB_URL" --json task create \
  "Produce the release summary" --project production-demo --kind report | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
lease_id="$(mac --hub-url "$DOCS_HUB_URL" --json task claim \
  "$task_id" agent_executor | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["lease_id"])')"
mac --hub-url "$DOCS_HUB_URL" task start \
  "$task_id" agent_executor --lease-id "$lease_id"
executor_manifest="$(printf '%s' \
  '{"schema":"mac.worker_evidence.v1","status":"complete","evidence_type":"operator_result","summary":"Release summary verified","checks":[{"name":"acceptance","returncode":0}]}' | \
  env MAC_AGENT_ATTESTATION_KEY="$executor_key" MAC_AGENT_ID=agent_executor \
  mac-evidence sign --manifest-stdin --signed-by agent_executor)"
executor_metadata="$(printf '%s' "$executor_manifest" | python3 -c \
  'import json,sys; print(json.dumps({"returncode":0,"verification":json.load(sys.stdin)}))')"
evidence_id="$(mac --hub-url "$DOCS_HUB_URL" --json task evidence "$task_id" \
  --kind test --uri artifact://production/release-summary \
  --summary "Release summary verified" --created-by agent_executor \
  --lease-id "$lease_id" --metadata "$executor_metadata" | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
mac --hub-url "$DOCS_HUB_URL" task submit-review \
  "$task_id" agent_executor --lease-id "$lease_id"
review_id="$(mac --hub-url "$DOCS_HUB_URL" --json admin review request \
  "$task_id" agent_certifier | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
review_manifest="$(python3 -c \
  'import json,sys; print(json.dumps({"schema":"mac.worker_evidence.v1","status":"complete","evidence_type":"review_verdict","verdict":"approved","reviewed_evidence_id":sys.argv[1],"checks":[{"name":"independent acceptance","returncode":0}],"worktree_digest":"sha256:"+("0"*64),"llm_model":"docs-certifier","llm":{"tool":"docs","agent":"review","model":"docs-certifier"}}))' \
  "$evidence_id" | env MAC_AGENT_ATTESTATION_KEY="$reviewer_key" \
  MAC_AGENT_ID=agent_certifier mac-evidence sign --manifest-stdin \
  --signed-by agent_certifier)"
review_metadata="$(printf '%s' "$review_manifest" | python3 -c \
  'import json,sys; print(json.dumps({"returncode":0,"verification":json.load(sys.stdin)}))')"
review_evidence_id="$(mac --hub-url "$DOCS_HUB_URL" --json task evidence \
  "$task_id" --kind review --uri artifact://production/release-review \
  --summary "Independent acceptance passed" --created-by agent_certifier \
  --metadata "$review_metadata" | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
mac --hub-url "$DOCS_HUB_URL" admin review decision \
  "$review_id" approved agent_certifier --evidence-id "$review_evidence_id" \
  --reason "Independent acceptance passed"
mac --hub-url "$DOCS_HUB_URL" task close \
  "$task_id" --reason "Accepted outcome recorded"
mac --hub-url "$DOCS_HUB_URL" task stats --all
mac --hub-url "$DOCS_HUB_URL" admin events list --subject-type task \
  --subject-id "$task_id" --limit 50 >/dev/null
```

In production, replace the standalone database with a scoped hub profile, bind
the project to its repository contract, and let the managed package pipeline
provide assembly, external certification, and guarded publication. Before fleet
activation, require the qualified immutable images and synchronized cutover
receipt described in Chapters 14 and 15.

The system's unifying idea is not that every worker runs at the same speed. It
is that intent, authority, identity, evidence, and publication advance through
explicit contracts. That is how a diverse collection of agents behaves like
one trustworthy production system without pretending the underlying machines
are homogeneous.
