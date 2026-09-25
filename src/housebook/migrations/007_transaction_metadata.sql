-- Add metadata column (JSON) for multi-line transaction details
-- (e.g., flight routes, Uber trip codes, car rental locations)
ALTER TABLE transactions ADD COLUMN metadata TEXT;
