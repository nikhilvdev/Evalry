-- Suite registry (plan §8): a suite version is an immutable list of tasks, stored as one row, so "the approved version" can
-- never change underneath a gate. Tasks include their hidden tests. The API role may create and read its own tenant's suites;
-- workers read across tenants (they must, to grade) but never write. content_hash lets a gate pin the exact content.

CREATE TABLE suites (
    tenant_id     text        NOT NULL,
    project_id    text        NOT NULL,
    suite_version text        NOT NULL CHECK (suite_version <> ''),
    content_hash  text        NOT NULL CHECK (length(content_hash) = 64),
    tasks         jsonb       NOT NULL,
    created_by    text        NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, suite_version),
    FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, project_id)
);

CREATE TRIGGER suites_immutable BEFORE UPDATE OR DELETE ON suites
    FOR EACH ROW EXECUTE FUNCTION evalry_forbid_change();

ALTER TABLE suites ENABLE ROW LEVEL SECURITY;
ALTER TABLE suites FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON suites
    USING (tenant_id = current_setting('evalry.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('evalry.tenant_id', true));
CREATE POLICY worker_read ON suites FOR SELECT USING (current_user = 'evalry_worker');

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evalry_app') THEN
        GRANT SELECT, INSERT ON suites TO evalry_app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evalry_worker') THEN
        GRANT SELECT ON suites TO evalry_worker;
    END IF;
END $$;
