-- Projects: user-initiated expense groupings (renovations, events, ...).
--
-- The supervised-retrieval counterpart to trips: the user declares a
-- project and stores its matching criteria (keywords/categories) on the
-- row; the matcher then scores existing transactions against that target.
-- Projects GROUP spending for reporting and budget tracking; they are
-- NOT a spending-view exclusion filter (a renovation is real spending).

CREATE TABLE IF NOT EXISTS projects (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    description   TEXT,                            -- user's framing, verbatim
    type          TEXT NOT NULL DEFAULT 'unknown', -- renovation|event|purchase|...
    location      TEXT,                            -- "Master Bathroom" (free text)
    start_date    DATE,
    end_date      DATE,                            -- NULL = open / ongoing
    status        TEXT NOT NULL DEFAULT 'open',    -- open | closed
    match_keywords    TEXT,                        -- JSON array of strings
    match_categories  TEXT,                        -- JSON array of category names
    budget        REAL,                            -- optional planned budget
    created_by    TEXT NOT NULL DEFAULT 'agent',   -- agent | manual
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at     TIMESTAMP
);

-- Nullable FK with no default: existing rows become NULL (unassigned),
-- which is exactly the matcher's candidate pool.
ALTER TABLE transactions ADD COLUMN project_id INTEGER REFERENCES projects(id);

CREATE INDEX IF NOT EXISTS idx_transactions_project_id
    ON transactions(project_id);
