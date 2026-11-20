-- A queued job must say which run it belongs to and which suite version holds its task: the worker has only the job row, and
-- the API assembles the run record from the jobs sharing a run_id. Existing rows keep the empty default (local CLI runs).

ALTER TABLE jobs ADD COLUMN run_id text NOT NULL DEFAULT '';
ALTER TABLE jobs ADD COLUMN suite_version text NOT NULL DEFAULT '';

CREATE INDEX jobs_by_run ON jobs (tenant_id, run_id) WHERE run_id <> '';
