-- Allow a one-time manual expense to belong to a trip.
--
-- A trip's total used to come from statement transactions alone, so a
-- cost paid with no statement behind it (cash, a Brazilian PiX
-- transfer, an installment billed on a statement that will not be
-- imported) could never count toward the trip. This mirrors
-- 021_manual_expense_project: the manual row keeps the source ledger
-- immutable, and the FK lets trip totals roll it in. Nullable:
-- existing manual expenses stay unassigned.
ALTER TABLE manual_expenses ADD COLUMN trip_id INTEGER REFERENCES trips(id);

CREATE INDEX IF NOT EXISTS idx_manual_expenses_trip_id
    ON manual_expenses(trip_id);
