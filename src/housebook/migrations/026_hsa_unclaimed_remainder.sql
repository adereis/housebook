-- Let an expense record the part of its card charge it does not claim.
--
-- The math proof requires the expenses linked to a card charge to add
-- up to that charge. A pharmacy checkout that also paid for something
-- with no itemized receipt (supplies bought alongside a prescription)
-- could therefore never reach a reimbursable level, even though its
-- receipt proves the prescription in full. The remainder is declared
-- explicitly, with a reason, so the proof still accounts for every
-- dollar of the charge and claims only what a document proves.
-- Both columns are nullable: existing expenses have no remainder.
ALTER TABLE hsa_expenses ADD COLUMN unclaimed_amount REAL DEFAULT NULL;
ALTER TABLE hsa_expenses ADD COLUMN unclaimed_reason TEXT DEFAULT NULL;
