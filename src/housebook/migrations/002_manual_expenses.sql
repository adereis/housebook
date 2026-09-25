-- Manual expenses: templates expanded into virtual transactions
-- at query time, keeping the source-based ledger immutable.

CREATE TABLE IF NOT EXISTS manual_expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    amount REAL NOT NULL,
    category TEXT NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE,
    frequency TEXT NOT NULL DEFAULT 'one-time',
    is_active BOOLEAN DEFAULT 1
);
