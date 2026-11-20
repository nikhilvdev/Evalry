-- Gate-failure disputes (plan §20). One dispute per decision and at most one resolution, both append-only: a contested decision
-- stays exactly as it was signed, and the outcome is a separate record. Keyed by decision so a second contest is a new decision.

CREATE TABLE disputes (
    tenant_id   text        NOT NULL,
    project_id  text        NOT NULL,
    decision_id text        NOT NULL,
    dispute     jsonb       NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, decision_id),
    FOREIGN KEY (tenant_id, project_id, decision_id) REFERENCES gate_decisions (tenant_id, project_id, decision_id)
);

CREATE TABLE dispute_resolutions (
    tenant_id   text        NOT NULL,
    project_id  text        NOT NULL,
    decision_id text        NOT NULL,
    resolution  jsonb       NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, decision_id),
    FOREIGN KEY (tenant_id, project_id, decision_id) REFERENCES disputes (tenant_id, project_id, decision_id)
);

CREATE TRIGGER disputes_immutable BEFORE UPDATE OR DELETE ON disputes
    FOR EACH ROW EXECUTE FUNCTION evalry_forbid_change();
CREATE TRIGGER dispute_resolutions_immutable BEFORE UPDATE OR DELETE ON dispute_resolutions
    FOR EACH ROW EXECUTE FUNCTION evalry_forbid_change();

ALTER TABLE disputes            ENABLE ROW LEVEL SECURITY; ALTER TABLE disputes            FORCE ROW LEVEL SECURITY;
ALTER TABLE dispute_resolutions ENABLE ROW LEVEL SECURITY; ALTER TABLE dispute_resolutions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON disputes USING (tenant_id = current_setting('evalry.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('evalry.tenant_id', true));
CREATE POLICY tenant_isolation ON dispute_resolutions USING (tenant_id = current_setting('evalry.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('evalry.tenant_id', true));

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evalry_app') THEN
        GRANT SELECT, INSERT ON disputes, dispute_resolutions TO evalry_app;
    END IF;
END $$;
