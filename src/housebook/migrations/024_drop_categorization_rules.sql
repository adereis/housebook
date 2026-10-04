-- Retire the DB copy of the categorization rules.
-- The workspace rules.json is the only rule store. The table was seeded
-- from it once and then edited separately (web UI corrections, the
-- optimize tool), so the two drifted: ingest categorized with one set
-- of rules and `apply-rules` with another.

DROP TABLE IF EXISTS categorization_rules;
