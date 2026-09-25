-- Remove soft-delete column from manual_expenses.
-- Rows with is_active = 0 are purged; the column is then dropped.

DELETE FROM manual_expenses WHERE is_active = 0;
ALTER TABLE manual_expenses DROP COLUMN is_active;
