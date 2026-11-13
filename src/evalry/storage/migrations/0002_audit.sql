-- Audit log in the database (plan §8: append-only system of record). One hash chain per tenant: chain_id is the tenant id,
-- seq counts from 0 within it, and (chain_id, seq) is the primary key so a chain cannot fork even if the application-level
-- lock failed. `event` holds the exact sealed event JSON, so hashes re-verify bit for bit; the other columns are for queries.
-- Events cannot be updated or deleted by anyone (trigger), and the application role sees and writes only its own tenant.

CREATE TABLE audit_events (
    chain_id   text        NOT NULL CHECK (chain_id <> ''),
    seq        bigint      NOT NULL CHECK (seq >= 0),
    ts         timestamptz NOT NULL,
    actor      text        NOT NULL,
    action     text        NOT NULL,
    target     text        NOT NULL,
    project_id text,
    hash       text        NOT NULL CHECK (length(hash) = 64),
    event      jsonb       NOT NULL,
    PRIMARY KEY (chain_id, seq)
);

CREATE INDEX audit_by_time ON audit_events (chain_id, ts);
CREATE INDEX audit_by_project ON audit_events (chain_id, project_id, ts);

CREATE TRIGGER audit_events_immutable BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION evalry_forbid_change();

ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON audit_events
    USING (chain_id = current_setting('evalry.tenant_id', true))
    WITH CHECK (chain_id = current_setting('evalry.tenant_id', true));

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evalry_app') THEN
        GRANT SELECT, INSERT ON audit_events TO evalry_app;
    END IF;
END $$;
