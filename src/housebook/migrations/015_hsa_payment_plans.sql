-- Payment plan support: link master liability records to installment CC stubs.
-- Master records (EOB/INV) document total liability; installments are the actual cash payments.

CREATE TABLE IF NOT EXISTS hsa_payment_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    total_liability REAL NOT NULL,
    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE hsa_expenses ADD COLUMN payment_plan_id INTEGER
    REFERENCES hsa_payment_plans(id);

-- 'master' = total liability record (not directly reimbursable)
-- 'installment' = individual cash payment (reimbursable when evidence is ready)
ALTER TABLE hsa_expenses ADD COLUMN plan_role TEXT;

CREATE INDEX IF NOT EXISTS idx_hsa_expenses_payment_plan_id
    ON hsa_expenses(payment_plan_id);
