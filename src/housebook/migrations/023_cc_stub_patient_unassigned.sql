-- The CC-stub scanner wrote patient 'self' on every stub. 'self' is a
-- relationship, not a patient id, and it read as the account holder
-- although a card charge names no patient. The scanner now leaves
-- patient NULL until a document or cardholder data names one; this
-- clears the placeholder from stubs created before that change.
--
-- Only scanner stubs are touched: merges never copy patient, so no
-- receipt, EOB or invoice row can have inherited 'self'. Each change
-- is logged first, because hsa_audit_log is the IRS-defense trail.

INSERT INTO hsa_audit_log
    (table_name, record_id, field_name, old_value, new_value,
     changed_by, reason)
SELECT 'hsa_expenses', id, 'patient', 'self', NULL, 'migration',
       'Scanner placeholder cleared: a card charge names no patient'
FROM hsa_expenses
WHERE source = 'cc_stub' AND patient = 'self';

UPDATE hsa_expenses SET patient = NULL
WHERE source = 'cc_stub' AND patient = 'self';
