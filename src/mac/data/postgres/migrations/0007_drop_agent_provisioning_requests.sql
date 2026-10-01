-- Drop the agent provisioning request ledger. Its only consumer was the HGX
-- elastic-capacity autoscaler, which is deleted together with the k8s runner;
-- dispatch and service-role reconciliation no longer write demand rows, and
-- the /provisioning/requests API is gone. No code reads or writes the table
-- any longer.
--
-- No CASCADE, as in 0004 and 0005: an object nobody declared here makes the
-- migration fail and roll back instead of being dropped silently with the
-- table. Its indexes go with it. IF EXISTS keeps this a no-op for a table that
-- was never created.

DROP TABLE IF EXISTS agent_provisioning_requests;
