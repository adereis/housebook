-- Tax provenance: per-document audit boundary back to the source file.

ALTER TABLE tax_documents ADD COLUMN source_file_path TEXT;
ALTER TABLE tax_documents ADD COLUMN source_file_sha256 TEXT;
ALTER TABLE tax_documents ADD COLUMN source_page INTEGER;
ALTER TABLE tax_documents ADD COLUMN sidecar_path TEXT;
