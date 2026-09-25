-- Sync metadata for optimistic concurrency control.
-- Stores the remote DB's SQLite change counter at last pull
-- so push can detect if someone else modified the remote.
CREATE TABLE IF NOT EXISTS sync_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
