-- Judge calibration records (plan §6): the evidence that a judge version may gate. Append-only like runs and decisions, so a
-- later failing recalibration supersedes a pass without erasing it. Recorded by approvers (the access layer checks the role).

CREATE TABLE judge_calibrations (
    tenant_id      text        NOT NULL,
    project_id     text        NOT NULL,
    calibration_id text        NOT NULL CHECK (calibration_id <> ''),
    judge_version  text        NOT NULL CHECK (judge_version <> ''),
    passed         boolean     NOT NULL,
    record         jsonb       NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, calibration_id),
    FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, project_id)
);

CREATE INDEX calibrations_by_judge ON judge_calibrations (tenant_id, project_id, judge_version, created_at);

CREATE TRIGGER judge_calibrations_immutable BEFORE UPDATE OR DELETE ON judge_calibrations
    FOR EACH ROW EXECUTE FUNCTION evalry_forbid_change();

ALTER TABLE judge_calibrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE judge_calibrations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON judge_calibrations
    USING (tenant_id = current_setting('evalry.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('evalry.tenant_id', true));

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evalry_app') THEN
        GRANT SELECT, INSERT ON judge_calibrations TO evalry_app;
    END IF;
END $$;
