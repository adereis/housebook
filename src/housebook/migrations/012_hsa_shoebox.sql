-- HSA Shoebox: medical expense ledger with document integrity tracking.

CREATE TABLE IF NOT EXISTS hsa_expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_date DATE,
    provider TEXT,
    patient TEXT,
    description TEXT,
    amount_billed REAL,
    insurance_paid REAL DEFAULT 0,
    patient_responsibility REAL,
    category TEXT,
    payment_method TEXT,
    payment_date DATE,
    transaction_id INTEGER REFERENCES transactions(id),
    source TEXT DEFAULT 'manual',
    status TEXT DEFAULT 'UNREIMBURSED',
    needs_review BOOLEAN DEFAULT 1,
    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS hsa_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_id INTEGER REFERENCES hsa_expenses(id),
    document_type TEXT,
    file_path TEXT,
    file_hash TEXT NOT NULL,
    original_filename TEXT,
    raw_data TEXT,
    ingested_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS hsa_providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT UNIQUE NOT NULL,
    category TEXT,
    aliases TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS hsa_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    field_name TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_by TEXT DEFAULT 'system',
    changed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS hsa_reimbursements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reimbursement_date DATE,
    total_amount REAL,
    method TEXT,
    packet_file TEXT,
    status TEXT DEFAULT 'PLANNED',
    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS hsa_reimbursement_items (
    reimbursement_id INTEGER REFERENCES hsa_reimbursements(id),
    expense_id INTEGER REFERENCES hsa_expenses(id),
    PRIMARY KEY (reimbursement_id, expense_id)
);

CREATE INDEX IF NOT EXISTS idx_hsa_expenses_status
    ON hsa_expenses(status);
CREATE INDEX IF NOT EXISTS idx_hsa_expenses_service_date
    ON hsa_expenses(service_date);
CREATE INDEX IF NOT EXISTS idx_hsa_expenses_transaction_id
    ON hsa_expenses(transaction_id);
CREATE INDEX IF NOT EXISTS idx_hsa_documents_expense_id
    ON hsa_documents(expense_id);
CREATE INDEX IF NOT EXISTS idx_hsa_audit_log_record
    ON hsa_audit_log(table_name, record_id);
