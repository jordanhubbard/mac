---
schema: mac.docs.chapter.v1
chapter: 5
title: Evidence, Review, and Completion
audiences: [user, operator, contributor]
timeout_seconds: 90
---

# Evidence, Review, and Completion

The lifecycle separates execution from acceptance. A worker claims and starts a
task under a lease, records structured evidence, and submits the exact attempt
for independent review. A reviewer decides against that evidence. Completion
then records the accepted outcome.

This report task avoids repository publication so the entire lifecycle remains
local. Code tasks add test, push, review, and canonical-integration requirements.

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
mac --hub-url "$DOCS_HUB_URL" admin machine register lifecycle-host \
  --machine-id machine_lifecycle
writer_registration="$(mac --hub-url "$DOCS_HUB_URL" --json agent register \
  machine_lifecycle writer --agent-id agent_writer --capabilities docs)"
reviewer_registration="$(mac --hub-url "$DOCS_HUB_URL" --json agent register \
  machine_lifecycle reviewer --agent-id agent_reviewer --capabilities review)"
writer_key="$(printf '%s' "$writer_registration" | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["attestation_key"])')"
reviewer_key="$(printf '%s' "$reviewer_registration" | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["attestation_key"])')"
task_id="$(mac --hub-url "$DOCS_HUB_URL" --json task create \
  "Explain the lifecycle" --kind report --project '' | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
lease_id="$(mac --hub-url "$DOCS_HUB_URL" --json task claim \
  "$task_id" agent_writer | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["lease_id"])')"
mac --hub-url "$DOCS_HUB_URL" task start \
  "$task_id" agent_writer --lease-id "$lease_id"
executor_manifest="$(printf '%s' \
  '{"schema":"mac.worker_evidence.v1","status":"complete","evidence_type":"operator_result","summary":"Lifecycle explanation verified","checks":[{"name":"content","returncode":0}]}' | \
  env MAC_AGENT_ATTESTATION_KEY="$writer_key" MAC_AGENT_ID=agent_writer \
  mac-evidence sign --manifest-stdin --signed-by agent_writer)"
executor_metadata="$(printf '%s' "$executor_manifest" | python3 -c \
  'import json,sys; print(json.dumps({"returncode":0,"verification":json.load(sys.stdin)}))')"
evidence_id="$(mac --hub-url "$DOCS_HUB_URL" --json task evidence "$task_id" \
  --kind test --uri artifact://tutorial/lifecycle \
  --summary "Lifecycle explanation verified" --created-by agent_writer \
  --lease-id "$lease_id" --metadata "$executor_metadata" | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
mac --hub-url "$DOCS_HUB_URL" task submit-review \
  "$task_id" agent_writer --lease-id "$lease_id"
review_id="$(mac --hub-url "$DOCS_HUB_URL" --json admin review request \
  "$task_id" agent_reviewer | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
review_manifest="$(python3 -c \
  'import json,sys; print(json.dumps({"schema":"mac.worker_evidence.v1","status":"complete","evidence_type":"review_verdict","verdict":"approved","reviewed_evidence_id":sys.argv[1],"checks":[{"name":"independent verification","returncode":0}],"worktree_digest":"sha256:"+("0"*64),"llm_model":"docs-reviewer","llm":{"tool":"docs","agent":"review","model":"docs-reviewer"}}))' \
  "$evidence_id" | env MAC_AGENT_ATTESTATION_KEY="$reviewer_key" \
  MAC_AGENT_ID=agent_reviewer mac-evidence sign --manifest-stdin \
  --signed-by agent_reviewer)"
review_metadata="$(printf '%s' "$review_manifest" | python3 -c \
  'import json,sys; print(json.dumps({"returncode":0,"verification":json.load(sys.stdin)}))')"
review_evidence_id="$(mac --hub-url "$DOCS_HUB_URL" --json task evidence \
  "$task_id" --kind review --uri artifact://tutorial/lifecycle-review \
  --summary "Evidence independently approved" --created-by agent_reviewer \
  --metadata "$review_metadata" | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
mac --hub-url "$DOCS_HUB_URL" admin review decision \
  "$review_id" approved agent_reviewer --evidence-id "$review_evidence_id" \
  --reason "Evidence is complete"
mac --hub-url "$DOCS_HUB_URL" task close \
  "$task_id" --reason "Reviewed and accepted"
mac --hub-url "$DOCS_HUB_URL" task show "$task_id" >/dev/null
```

An executor's evidence cannot be self-approved by the same agent. Repository
completion additionally requires a durable canonical-integration receipt with a
remotely verified branch SHA. Operator recovery can bypass review, but not that
repository publication proof.
