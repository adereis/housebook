-- Baseline schema: captures all tables as of initial release.
-- This migration is idempotent (CREATE IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS processed_files (
    file_path TEXT PRIMARY KEY,
    file_hash TEXT,
    last_processed TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date DATE,
    description TEXT,
    amount REAL,
    category TEXT,
    source TEXT,
    original_file TEXT,
    status TEXT DEFAULT 'UNVERIFIED',
    trip_id INTEGER REFERENCES trips(id),
    needs_review BOOLEAN DEFAULT 1,
    FOREIGN KEY (original_file) REFERENCES processed_files(file_path)
);

CREATE TABLE IF NOT EXISTS categorization_rules (
    category TEXT,
    keyword TEXT,
    UNIQUE(category, keyword)
);

CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    start_date DATE,
    end_date DATE
);

CREATE TABLE IF NOT EXISTS tax_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tax_year INTEGER,
    document_type TEXT,
    issuer TEXT,
    category TEXT,
    amount REAL,
    currency TEXT DEFAULT 'USD',
    original_file TEXT,
    status TEXT DEFAULT 'UNVERIFIED',
    needs_review BOOLEAN DEFAULT 1,
    raw_data TEXT,
    FOREIGN KEY (original_file) REFERENCES processed_files(file_path)
);

CREATE TABLE IF NOT EXISTS ingestion_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT,
    line_number INTEGER,
    raw_text TEXT,
    error TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);
