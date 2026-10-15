-- merlin schema v3: each chunk's heading chain ([h1, h2, h3], empty levels dropped), so ranking can tell a
-- section's overview (an ancestor) from its siblings. Existing documents re-chunk on the next ingest (blob_sha
-- cleared); chunk texts are unchanged, so their embeddings are reused.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS heading_path text[] NOT NULL DEFAULT '{}';
UPDATE documents SET blob_sha = '';

INSERT INTO meta (key, value) VALUES ('schema_version', '3') ON CONFLICT (key) DO UPDATE SET value = '3';
