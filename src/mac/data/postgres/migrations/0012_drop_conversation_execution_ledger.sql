-- Drop the OpenClaw direct-execution ledger. OpenClaw is removed from MAC:
-- Hermes is the only human interface, and the direct Slack code-execution
-- service and its /persona-instances/{id}/openclaw-executions API that wrote
-- this table are deleted. No code reads or writes the table any longer.
--
-- No CASCADE, as in 0004 to 0008: an object nobody declared here makes the
-- migration fail and roll back instead of being dropped silently with the
-- table. Its indexes go with it. IF EXISTS keeps this a no-op for a table
-- that was never created.

DROP TABLE IF EXISTS openclaw_conversation_executions;
