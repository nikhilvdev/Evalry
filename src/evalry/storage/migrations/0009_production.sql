-- Production monitoring (plan §11). Signals are masked by schema (ids and an outcome, never code or user content), append-only
-- and idempotent by caller-chosen id, so a producer can resend a batch safely. Eval incidents are one row to open and one to
-- close; nothing is edited. The application role may only insert and read.

CREATE TABLE production_signals (
    tenant_id   text        NOT NULL,
    project_id  text        NOT NULL,
    signal_id   text        NOT NULL CHECK (signal_id <> ''),
    task_id     text        NOT NULL,
    kind        text        NOT NULL CHECK (kind IN ('accepted', 'edited', 'rejected', 'build_failed', 'reverted')),
    observed_at timestamptz NOT NULL,
    signal      jsonb       NOT NULL,
    PRIMARY KEY (tenant_id, project_id, signal_id),
    FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, project_id)
);
CREATE INDEX signals_by_time ON production_signals (tenant_id, project_id, observed_at);

CREATE TABLE eval_incidents (
    tenant_id   text        NOT NULL,
    project_id  text        NOT NULL,
    incident_id text        NOT NULL CHECK (incident_id <> ''),
    task_id     text        NOT NULL,
    incident    jsonb       NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, incident_id),
    FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, project_id)
);

CREATE TABLE incident_closures (
    tenant_id   text        NOT NULL,
    project_id  text        NOT NULL,
    incident_id text        NOT NULL,
    closure     jsonb       NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, incident_id),
    FOREIGN KEY (tenant_id, project_id, incident_id) REFERENCES eval_incidents (tenant_id, project_id, incident_id)
);

CREATE TRIGGER production_signals_immutable BEFORE UPDATE OR DELETE ON production_signals
    FOR EACH ROW EXECUTE FUNCTION evalry_forbid_change();
CREATE TRIGGER eval_incidents_immutable BEFORE UPDATE OR DELETE ON eval_incidents
    FOR EACH ROW EXECUTE FUNCTION evalry_forbid_change();
CREATE TRIGGER incident_closures_immutable BEFORE UPDATE OR DELETE ON incident_closures
    FOR EACH ROW EXECUTE FUNCTION evalry_forbid_change();

ALTER TABLE production_signals ENABLE ROW LEVEL SECURITY; ALTER TABLE production_signals FORCE ROW LEVEL SECURITY;
ALTER TABLE eval_incidents     ENABLE ROW LEVEL SECURITY; ALTER TABLE eval_incidents     FORCE ROW LEVEL SECURITY;
ALTER TABLE incident_closures  ENABLE ROW LEVEL SECURITY; ALTER TABLE incident_closures  FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON production_signals USING (tenant_id = current_setting('evalry.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('evalry.tenant_id', true));
CREATE POLICY tenant_isolation ON eval_incidents USING (tenant_id = current_setting('evalry.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('evalry.tenant_id', true));
CREATE POLICY tenant_isolation ON incident_closures USING (tenant_id = current_setting('evalry.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('evalry.tenant_id', true));

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evalry_app') THEN
        GRANT SELECT, INSERT ON production_signals, eval_incidents, incident_closures TO evalry_app;
    END IF;
END $$;
