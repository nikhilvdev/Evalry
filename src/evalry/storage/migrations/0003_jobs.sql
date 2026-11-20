-- Durable job queue (plan §7). One row per task x sample x stage, keyed by its inputs, so resubmitting never duplicates it.
-- Two roles use this table. evalry_app (the API) is bound to one tenant by row-level security and may only enqueue and read
-- its own jobs. evalry_worker claims across tenants, which is why it has its own policy; it gets this table and nothing else.
-- A DONE job never changes again (trigger), so a retry or a duplicate completion cannot double-count or double-charge.
-- A claim holds a lease; a worker that dies leaves an expired lease, and the next claim requeues the job.

CREATE TABLE jobs (
    tenant_id    text             NOT NULL REFERENCES tenants (tenant_id),
    key          text             NOT NULL CHECK (length(key) = 64),
    seq          bigint GENERATED ALWAYS AS IDENTITY,
    project_id   text             NOT NULL,
    task_id      text             NOT NULL,
    sample       integer          NOT NULL CHECK (sample >= 0),
    stage        text             NOT NULL CHECK (stage IN ('generate', 'grade', 'judge')),
    priority     smallint         NOT NULL,
    state        text             NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'done', 'failed')),
    attempts     integer          NOT NULL DEFAULT 0,
    max_attempts integer          NOT NULL CHECK (max_attempts >= 1),
    error        text             NOT NULL DEFAULT '',
    result       jsonb,
    cost_usd     double precision NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    lease_until  timestamptz,
    PRIMARY KEY (tenant_id, key),
    FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, project_id)
);

CREATE INDEX jobs_by_key ON jobs (key);
CREATE INDEX jobs_claimable ON jobs (priority, seq) WHERE state = 'queued';

CREATE FUNCTION evalry_job_terminal() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state = 'done' THEN
        RAISE EXCEPTION 'job % is done; a finished job never changes', OLD.key;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER jobs_done_is_final BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION evalry_job_terminal();

ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON jobs
    USING (tenant_id = current_setting('evalry.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('evalry.tenant_id', true));
CREATE POLICY worker_all ON jobs USING (current_user = 'evalry_worker') WITH CHECK (current_user = 'evalry_worker');

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evalry_app') THEN
        GRANT SELECT, INSERT ON jobs TO evalry_app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evalry_worker') THEN
        GRANT SELECT, UPDATE ON jobs TO evalry_worker;
    END IF;
END $$;
