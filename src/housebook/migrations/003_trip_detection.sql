-- Trip detection: enrich trips table with status, type, location.

ALTER TABLE trips ADD COLUMN status TEXT NOT NULL DEFAULT 'confirmed';
-- Values: 'suggested', 'confirmed'

ALTER TABLE trips ADD COLUMN type TEXT NOT NULL DEFAULT 'unknown';
-- Values: 'personal', 'work', 'unknown'

ALTER TABLE trips ADD COLUMN location TEXT;
-- Free-text, nullable. E.g. "Boston, MA" or "Rome, Italy"

ALTER TABLE trips ADD COLUMN created_by TEXT NOT NULL DEFAULT 'manual';
-- Values: 'manual', 'detector'
