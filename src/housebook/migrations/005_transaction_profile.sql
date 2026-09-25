-- Add profile column to transactions table
ALTER TABLE transactions ADD COLUMN profile TEXT;

-- Data migration: Extract profile from description for existing Amazon transactions
-- Pattern: "Amazon (John): ..." -> Profile: "john"
-- Pattern: "Amazon REFUND (Jane): ..." -> Profile: "jane"

UPDATE transactions
SET profile = LOWER(SUBSTR(description, INSTR(description, '(') + 1, INSTR(description, ')') - INSTR(description, '(') - 1)),
    description = SUBSTR(description, 1, INSTR(description, ' (') - 1) || ': ' || SUBSTR(description, INSTR(description, '): ') + 3)
WHERE source = 'Amazon'
  AND description LIKE 'Amazon % (%): %';
