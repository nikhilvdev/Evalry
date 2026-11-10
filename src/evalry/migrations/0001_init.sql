-- Evalry metadata schema, version 1 (plan §8). Applied by evalry.db.migrate; never edit an applied file, add a new one.
--
-- Isolation, two layers:
--   1. Every tenant-scoped table has tenant_id in its primary key, and foreign keys are composite (tenant_id, project_id),
--      so a row can never reference another tenant's project.
--   2. Row-level security: the application role sees only rows whose tenant_id equals the transaction's
--      evalry.tenant_id setting, and cannot write rows for another tenant. FORCE applies it to the table owner too.
--      Superusers bypass RLS in PostgreSQL, so the application must not connect as one.
--
-- Immutability: run records and gate decisions cannot be updated or deleted by anyone, owner included (retention and
-- legal-hold deletion will be a separate, audited, privileged procedure).

CREATE TABLE tenants (
    tenant_id  text PRIMARY KEY CHECK (tenant_id <> ''),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tenant_admins (
    tenant_id text NOT NULL REFERENCES tenants (tenant_id),
    subject   text NOT NULL CHECK (subject <> ''),
    PRIMARY KEY (tenant_id, subject)
);

CREATE TABLE projects (
    tenant_id  text NOT NULL REFERENCES tenants (tenant_id),
    project_id text NOT NULL CHECK (project_id ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id)
);

CREATE TABLE project_members (
    tenant_id  text NOT NULL,
    project_id text NOT NULL,
    subject    text NOT NULL CHECK (subject <> ''),
    role       smallint NOT NULL CHECK (role BETWEEN 1 AND 4),  -- 1 viewer, 2 author, 3 approver, 4 admin
    PRIMARY KEY (tenant_id, project_id, subject),
    FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, project_id)
);

CREATE TABLE runs (
    tenant_id     text NOT NULL,
    project_id    text NOT NULL,
    run_id        text NOT NULL CHECK (run_id <> ''),
    suite_version text NOT NULL,
    started_at    timestamptz NOT NULL,
    record        jsonb NOT NULL,           -- the version 1 run-record JSON, unchanged
    recorded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, run_id),
    FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, project_id)
);

CREATE TABLE gate_decisions (
    tenant_id   text NOT NULL,
    project_id  text NOT NULL,
    decision_id text NOT NULL CHECK (decision_id <> ''),
    run_id      text NOT NULL,
    outcome     text NOT NULL CHECK (outcome IN ('pass', 'block')),
    environment text NOT NULL CHECK (environment IN ('dev', 'staging', 'production')),
    decision    jsonb NOT NULL,             -- the signed decision, unchanged
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, decision_id),
    FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, project_id),
    FOREIGN KEY (tenant_id, project_id, run_id) REFERENCES runs (tenant_id, project_id, run_id)
);

CREATE INDEX runs_by_project_time ON runs (tenant_id, project_id, recorded_at);
CREATE INDEX decisions_by_run ON gate_decisions (tenant_id, project_id, run_id);

CREATE FUNCTION evalry_forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable (attempted %)', TG_TABLE_NAME, TG_OP USING ERRCODE = 'integrity_constraint_violation';
END $$;

CREATE TRIGGER runs_immutable BEFORE UPDATE OR DELETE ON runs
    FOR EACH ROW EXECUTE FUNCTION evalry_forbid_change();
CREATE TRIGGER gate_decisions_immutable BEFORE UPDATE OR DELETE ON gate_decisions
    FOR EACH ROW EXECUTE FUNCTION evalry_forbid_change();

-- Row-level security. current_setting(..., true) is NULL when unset, so with no tenant bound a session sees nothing.
ALTER TABLE tenant_admins    ENABLE ROW LEVEL SECURITY; ALTER TABLE tenant_admins    FORCE ROW LEVEL SECURITY;
ALTER TABLE projects         ENABLE ROW LEVEL SECURITY; ALTER TABLE projects         FORCE ROW LEVEL SECURITY;
ALTER TABLE project_members  ENABLE ROW LEVEL SECURITY; ALTER TABLE project_members  FORCE ROW LEVEL SECURITY;
ALTER TABLE runs             ENABLE ROW LEVEL SECURITY; ALTER TABLE runs             FORCE ROW LEVEL SECURITY;
ALTER TABLE gate_decisions   ENABLE ROW LEVEL SECURITY; ALTER TABLE gate_decisions   FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON tenant_admins   USING (tenant_id = current_setting('evalry.tenant_id', true)) WITH CHECK (tenant_id = current_setting('evalry.tenant_id', true));
CREATE POLICY tenant_isolation ON projects        USING (tenant_id = current_setting('evalry.tenant_id', true)) WITH CHECK (tenant_id = current_setting('evalry.tenant_id', true));
CREATE POLICY tenant_isolation ON project_members USING (tenant_id = current_setting('evalry.tenant_id', true)) WITH CHECK (tenant_id = current_setting('evalry.tenant_id', true));
CREATE POLICY tenant_isolation ON runs            USING (tenant_id = current_setting('evalry.tenant_id', true)) WITH CHECK (tenant_id = current_setting('evalry.tenant_id', true));
CREATE POLICY tenant_isolation ON gate_decisions  USING (tenant_id = current_setting('evalry.tenant_id', true)) WITH CHECK (tenant_id = current_setting('evalry.tenant_id', true));

-- Least privilege for the application role. It is created by whoever provisions the database (it is cluster-level, so a
-- migration cannot assume it); if it exists, grant exactly what the application needs. It gets nothing on `tenants`
-- (tenant creation is an operator action) and no UPDATE/DELETE on the immutable tables.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evalry_app') THEN
        GRANT SELECT, INSERT                 ON tenant_admins, runs, gate_decisions TO evalry_app;
        GRANT SELECT, INSERT                 ON projects TO evalry_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON project_members TO evalry_app;
    END IF;
END $$;
