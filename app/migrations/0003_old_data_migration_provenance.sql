-- Durable, non-business provenance for the append-only Old Data Migration.
-- Imported business records continue to live in their normal current tables.
-- These maps only prevent repeat uploads from duplicating or overwriting them.

CREATE TABLE IF NOT EXISTS old_data_source (
    id INTEGER PRIMARY KEY,
    source_key VARCHAR(64) NOT NULL UNIQUE,
    origin_label VARCHAR(500) NOT NULL,
    first_file_hash VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_old_data_source_source_key
    ON old_data_source(source_key);

CREATE TABLE IF NOT EXISTS old_data_import_run (
    id VARCHAR(36) PRIMARY KEY,
    source_key VARCHAR(64) NOT NULL,
    filename VARCHAR(255),
    file_hash VARCHAR(64) NOT NULL,
    status VARCHAR(30) NOT NULL,
    rows_inserted INTEGER NOT NULL DEFAULT 0,
    rows_skipped INTEGER NOT NULL DEFAULT 0,
    report_json TEXT,
    imported_by VARCHAR(100),
    created_at DATETIME NOT NULL,
    completed_at DATETIME
);

CREATE INDEX IF NOT EXISTS ix_old_data_import_run_source_key
    ON old_data_import_run(source_key);
CREATE INDEX IF NOT EXISTS ix_old_data_import_run_status
    ON old_data_import_run(status);

CREATE TABLE IF NOT EXISTS old_data_record_map (
    id INTEGER PRIMARY KEY,
    source_key VARCHAR(64) NOT NULL,
    source_table VARCHAR(100) NOT NULL,
    source_pk VARCHAR(255) NOT NULL,
    target_table VARCHAR(100) NOT NULL,
    target_pk VARCHAR(255) NOT NULL,
    source_row_hash VARCHAR(64) NOT NULL,
    import_run_id VARCHAR(36) NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_old_data_record_source
        UNIQUE(source_key, source_table, source_pk)
);

CREATE INDEX IF NOT EXISTS ix_old_data_record_map_source_key
    ON old_data_record_map(source_key);
CREATE INDEX IF NOT EXISTS ix_old_data_record_map_import_run_id
    ON old_data_record_map(import_run_id);
CREATE INDEX IF NOT EXISTS ix_old_data_record_target
    ON old_data_record_map(target_table, target_pk);

CREATE TABLE IF NOT EXISTS old_data_reference_map (
    id INTEGER PRIMARY KEY,
    source_key VARCHAR(64) NOT NULL,
    reference_kind VARCHAR(40) NOT NULL,
    original_value VARCHAR(255) NOT NULL,
    current_value VARCHAR(255) NOT NULL,
    import_run_id VARCHAR(36) NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_old_data_reference_source
        UNIQUE(source_key, reference_kind, original_value)
);

CREATE INDEX IF NOT EXISTS ix_old_data_reference_map_source_key
    ON old_data_reference_map(source_key);
CREATE INDEX IF NOT EXISTS ix_old_data_reference_map_import_run_id
    ON old_data_reference_map(import_run_id);
