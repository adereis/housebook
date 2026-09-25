-- Allow manual expenses to be grouped under a project.
--
-- Projects group transactions, but real projects also incur off-ledger
-- costs (cash/check craftsmanship, lump material totals) that live as
-- one-time manual_expenses rather than in the immutable source ledger
-- (see 002_manual_expenses.sql). This FK lets project-summary roll those
-- costs into the project total. Nullable: existing manual expenses stay
-- unassigned.
ALTER TABLE manual_expenses ADD COLUMN project_id INTEGER REFERENCES projects(id);

CREATE INDEX IF NOT EXISTS idx_manual_expenses_project_id
    ON manual_expenses(project_id);
