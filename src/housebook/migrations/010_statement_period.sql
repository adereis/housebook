-- Add statement billing period columns to processed_files.
-- These store the billing cycle start/end dates extracted from
-- statement PDFs, enabling accurate gap detection.

ALTER TABLE processed_files ADD COLUMN statement_start DATE;
ALTER TABLE processed_files ADD COLUMN statement_end DATE;
