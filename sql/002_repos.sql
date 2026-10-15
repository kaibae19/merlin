-- merlin schema v2: multi-repo corpora. documents.path is the virtual path (`<repo>/<path>` when [[repos]] are
-- configured), so it stays unique; `repo` is that prefix ('' for a single-repo corpus), kept for the repos filter.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS repo text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS documents_repo ON documents (repo);

INSERT INTO meta (key, value) VALUES ('schema_version', '2') ON CONFLICT (key) DO UPDATE SET value = '2';
