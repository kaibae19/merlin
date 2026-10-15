-- merlin schema v1. {{EMBED_DIM}} is substituted from EMBED_DIM when the schema is created.
-- Always in public: under a pinned search_path (<schema>, public) it would otherwise land in the first pinned schema,
-- and every other schema's `vector` columns would fail with "type vector does not exist".
CREATE EXTENSION IF NOT EXISTS vector SCHEMA public;

CREATE TABLE IF NOT EXISTS meta (
    key   text PRIMARY KEY,
    value text NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    path       text PRIMARY KEY,
    type       text NOT NULL,
    title      text,
    doc_date   date,
    blob_sha   text NOT NULL,
    indexed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
    id         bigserial PRIMARY KEY,
    path       text NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
    ord        int  NOT NULL,
    heading    text NOT NULL DEFAULT '',
    body       text NOT NULL,
    hosts      text[] NOT NULL DEFAULT '{}',
    chunk_date date,                      -- section date (dated heading), else the document's date
    text_sha   text NOT NULL,             -- sha256 of the exact embedded text; the embedding-reuse key
    embedding  vector({{EMBED_DIM}}),
    tsv        tsvector GENERATED ALWAYS AS (
                   setweight(to_tsvector('english', coalesce(heading, '')), 'A') ||
                   setweight(to_tsvector('english', body), 'B')) STORED,
    UNIQUE (path, ord)
);

CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_tsv_gin   ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_hosts_gin ON chunks USING gin (hosts);
CREATE INDEX IF NOT EXISTS chunks_text_sha  ON chunks (text_sha);
CREATE INDEX IF NOT EXISTS documents_type   ON documents (type);

INSERT INTO meta (key, value) VALUES ('schema_version', '1') ON CONFLICT (key) DO UPDATE SET value = '1';
