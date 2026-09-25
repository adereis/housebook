-- Add evidence_level to track IRS audit-readiness per expense.
-- Only 'audit_ready' expenses qualify for HSA reimbursement.

ALTER TABLE hsa_expenses
    ADD COLUMN evidence_level TEXT DEFAULT 'unverified';
