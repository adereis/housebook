-- Track purchase↔refund / charge↔cancellation pairs.
-- Both sides point to each other (bidirectional).
-- Transactions with linked_transaction_id != NULL are hidden
-- from spending views while remaining in the DB for audit.
ALTER TABLE transactions ADD COLUMN linked_transaction_id INTEGER
    REFERENCES transactions(id);
