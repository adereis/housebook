-- HSA provenance: per-document audit boundary back to the source file.
--
-- file_path and file_hash already exist (sha256 of the source PDF/JPG).
-- Add two more columns so every hsa_documents row can answer:
--   "which page of which file" and "which sidecar produced this row"
--
-- These unlock the future /hsa-style modal pattern on /spending and
-- /tax: a UI just needs (file_path, source_page) to deep-link into
-- the original document.

ALTER TABLE hsa_documents ADD COLUMN source_page INTEGER;
ALTER TABLE hsa_documents ADD COLUMN sidecar_path TEXT;
