-- Re-run migration 014's catch-all remap. The hsa_expenses
-- evidence_level column DEFAULT is still the retired level
-- 'unverified' (013), and the CC-stub scanner omitted the column
-- on INSERT, so every stub created after 014 ran carries an
-- invalid level again. The scanner now sets 'stub' explicitly;
-- this cleans up the rows created in between.

UPDATE hsa_expenses SET evidence_level = 'stub'
WHERE evidence_level NOT IN ('stub', 'weak', 'ready', 'strong');
