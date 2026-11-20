-- Human labeling (plan §20). A batch is one immutable row (its items are blind: prompt and code only); labels are append-only
-- rows, one per reviewer and item, so a reviewer cannot score the same item twice and nothing is ever edited. Which item is
-- open, who disagrees and what the final score is are all derived from these rows (evalry.labeling).

CREATE TABLE label_batches (
    tenant_id  text        NOT NULL,
    project_id text        NOT NULL,
    batch_id   text        NOT NULL CHECK (batch_id <> ''),
    batch      jsonb       NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, batch_id),
    FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, project_id)
);

CREATE TABLE labels (
    tenant_id  text        NOT NULL,
    project_id text        NOT NULL,
    batch_id   text        NOT NULL,
    item_id    integer     NOT NULL CHECK (item_id >= 0),
    reviewer   text        NOT NULL CHECK (reviewer <> ''),
    kind       text        NOT NULL CHECK (kind IN ('review', 'adjudication')),
    label      jsonb       NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, batch_id, item_id, reviewer, kind),
    FOREIGN KEY (tenant_id, project_id, batch_id) REFERENCES label_batches (tenant_id, project_id, batch_id)
);

CREATE TRIGGER label_batches_immutable BEFORE UPDATE OR DELETE ON label_batches
    FOR EACH ROW EXECUTE FUNCTION evalry_forbid_change();
CREATE TRIGGER labels_immutable BEFORE UPDATE OR DELETE ON labels
    FOR EACH ROW EXECUTE FUNCTION evalry_forbid_change();

ALTER TABLE label_batches ENABLE ROW LEVEL SECURITY; ALTER TABLE label_batches FORCE ROW LEVEL SECURITY;
ALTER TABLE labels        ENABLE ROW LEVEL SECURITY; ALTER TABLE labels        FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON label_batches USING (tenant_id = current_setting('evalry.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('evalry.tenant_id', true));
CREATE POLICY tenant_isolation ON labels USING (tenant_id = current_setting('evalry.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('evalry.tenant_id', true));

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evalry_app') THEN
        GRANT SELECT, INSERT ON label_batches, labels TO evalry_app;
    END IF;
END $$;
