-- Drop the rollout and deploy tables. The rollout service, the deploy
-- service's environment deployments, and the sandbox-image rollout were never
-- used: the live hub holds no rows in any of these tables, and no code reads
-- or writes them any longer. managed_task_publication_rollout was already
-- unreferenced by any code.
--
-- Kept on purpose: environments (fleet_desired_source_states references it and
-- the source-release service reads it), artifacts (AgentBus artifact
-- publication records into it), and the runtime_environment* and runtime_runs
-- tables (the runtime environment registry is live).
--
-- The unified events view read rollout_events and environment_events, so it is
-- replaced first with the same columns minus those two sources; otherwise
-- dropping them would fail on the dependent view.
--
-- No CASCADE, as in 0004: an object nobody declared here makes the migration
-- fail and roll back instead of being dropped silently with the table. The
-- indexes go with their table. IF EXISTS keeps this a no-op for a table that
-- was never created.

CREATE OR REPLACE VIEW events AS
    SELECT
        id,
        'task' AS subject_type,
        task_id AS subject_id,
        event_type,
        actor,
        (
            -- jsonb_set is STRICT — a NULL replacement collapses the
            -- whole expression to NULL. SQLite json_set encodes NULL as
            -- JSON null instead, which is the behavior the rest of the
            -- code expects. Wrap to_jsonb() in COALESCE so a SQL NULL
            -- from_state/to_state lands as `null` inside the object.
            jsonb_set(
                jsonb_set(
                    COALESCE(NULLIF(detail, '')::jsonb, '{}'::jsonb),
                    '{from_state}',
                    COALESCE(to_jsonb(from_state), 'null'::jsonb),
                    true
                ),
                '{to_state}',
                COALESCE(to_jsonb(to_state), 'null'::jsonb),
                true
            )
        )::text AS detail,
        created_at
    FROM task_history
    UNION ALL
    SELECT id, 'eval_set' AS subject_type, eval_set_id AS subject_id,
           event_type, actor, detail, created_at
    FROM eval_set_events
    UNION ALL
    SELECT
        id,
        'secret' AS subject_type,
        secret_id AS subject_id,
        'secret.' || result AS event_type,
        accessor_agent_id AS actor,
        jsonb_build_object(
            'purpose', purpose,
            'expires_at', expires_at,
            'revealed_at', revealed_at
        )::text AS detail,
        created_at
    FROM secret_access_audit
    UNION ALL
    SELECT id, 'project' AS subject_type, project_id AS subject_id,
           event_type, actor, detail, created_at
    FROM project_events
    UNION ALL
    SELECT id, 'fleet' AS subject_type, fleet_id AS subject_id,
           event_type, actor, detail, created_at
    FROM fleet_events
    UNION ALL
    SELECT id, 'agent' AS subject_type, agent_id AS subject_id,
           event_type, actor, detail, created_at
    FROM agent_lifecycle_events
    UNION ALL
    SELECT id, 'agent' AS subject_type, agent_id AS subject_id,
           event_type, actor, detail, created_at
    FROM agent_events
    UNION ALL
    SELECT
        id,
        CASE WHEN task_id IS NOT NULL THEN 'task' ELSE 'agent' END AS subject_type,
        COALESCE(task_id, agent_id) AS subject_id,
        'command.' || phase AS event_type,
        agent_id AS actor,
        jsonb_build_object(
            'command_id', command_id,
            'agent_id', agent_id,
            'argv0', json_extract(argv, '$[0]'),
            'argv_redacted', true,
            'cwd', cwd,
            'task_id', task_id,
            'lease_id', lease_id,
            'started_at', started_at,
            'completed_at', completed_at,
            'duration_ms', duration_ms,
            'returncode', returncode,
            'stdout_sha256', stdout_sha256,
            'stderr_sha256', stderr_sha256,
            'stdout_bytes', stdout_bytes,
            'stderr_bytes', stderr_bytes,
            'metadata',
                CASE WHEN metadata IS NULL OR metadata = ''
                     THEN NULL
                     ELSE metadata::jsonb END
        )::text AS detail,
        created_at
    FROM command_audit
    UNION ALL
    SELECT
        event_id AS id,
        COALESCE(NULLIF(subject_type, ''), 'action_event') AS subject_type,
        COALESCE(subject_id, event_id) AS subject_id,
        'action.' || action_type || '.' || action_name AS event_type,
        actor,
        jsonb_build_object(
            'schema', 'mac.action_event.v1',
            'agent_id', agent_id,
            'hermes_instance_id', hermes_instance_id,
            'task_id', task_id,
            'session_id', session_id,
            'sandbox_id', sandbox_id,
            'action_type', action_type,
            'action_name', action_name,
            'outcome', outcome,
            'severity', severity,
            'policy_id', policy_id,
            'policy_version', policy_version,
            'command_id', command_id,
            'parent_event_id', parent_event_id,
            'redaction_state', redaction_state,
            'attributes',
                CASE WHEN attributes IS NULL OR attributes = ''
                     THEN '{}'::jsonb
                     ELSE attributes::jsonb END
        )::text AS detail,
        timestamp AS created_at
    FROM action_events
    UNION ALL
    SELECT
        id,
        'conversation_thread' AS subject_type,
        id AS subject_id,
        'gateway.thread_tracked' AS event_type,
        'gateway' AS actor,
        jsonb_build_object(
            'platform_binding_id', platform_binding_id,
            'external_thread_id', external_thread_id,
            'latest_task_id', latest_task_id,
            'summary', summary
        )::text AS detail,
        last_seen_at AS created_at
    FROM conversation_threads
    UNION ALL
    SELECT
        id,
        'vector_ref' AS subject_type,
        memory_id AS subject_id,
        'vector.indexed' AS event_type,
        created_by AS actor,
        jsonb_build_object(
            'vector_db', vector_db,
            'collection', collection,
            'point_id', point_id,
            'embedding_model', embedding_model
        )::text AS detail,
        created_at
    FROM vector_refs;

DROP TABLE IF EXISTS rollout_events;
DROP TABLE IF EXISTS rollouts;
DROP TABLE IF EXISTS deployments;
DROP TABLE IF EXISTS environment_events;
DROP TABLE IF EXISTS managed_task_publication_rollout;
