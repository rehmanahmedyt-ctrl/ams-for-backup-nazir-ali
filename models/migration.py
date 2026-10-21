"""Audit-safe records for the controlled legacy-template migration pipeline."""
from .__base import *  # noqa
from .helpers import *  # noqa

class MigrationRun(db.Model):
    __tablename__ = 'migration_run'
    id = db.Column(db.Integer, primary_key=True)
    run_key = db.Column(db.String(36), unique=True, nullable=False, index=True)
    template_type = db.Column(db.String(40), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    source_hash = db.Column(db.String(64), nullable=False, index=True)
    status = db.Column(db.String(30), nullable=False, default='VALIDATED', index=True)
    mode = db.Column(db.String(20), nullable=False, default='VALIDATION_ONLY')
    summary_json = db.Column(db.Text, default='{}')
    uploaded_by = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=pk_model_now, nullable=False, index=True)
    imported_at = db.Column(db.DateTime)
    rows = db.relationship('MigrationRow', backref='run', cascade='all, delete-orphan', lazy=True)

class MigrationRow(db.Model):
    __tablename__ = 'migration_row'
    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey('migration_run.id'), nullable=False, index=True)
    source_sheet = db.Column(db.String(80), nullable=False)
    source_row = db.Column(db.Integer, nullable=False)
    legacy_reference = db.Column(db.String(150), index=True)
    row_hash = db.Column(db.String(64), nullable=False, index=True)
    status = db.Column(db.String(30), nullable=False, default='READY', index=True)
    entity_type = db.Column(db.String(50))
    new_entity_id = db.Column(db.Integer)
    data_json = db.Column(db.Text, nullable=False)
    error_json = db.Column(db.Text, default='[]')
    imported_at = db.Column(db.DateTime)

class MigrationMapping(db.Model):
    __tablename__ = 'migration_mapping'
    id = db.Column(db.Integer, primary_key=True)
    template_type = db.Column(db.String(40), nullable=False, index=True)
    legacy_reference = db.Column(db.String(150), nullable=False, index=True)
    entity_type = db.Column(db.String(50), nullable=False)
    entity_id = db.Column(db.Integer, nullable=False)
    run_id = db.Column(db.Integer, db.ForeignKey('migration_run.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, nullable=False)
    __table_args__ = (db.UniqueConstraint('template_type', 'legacy_reference', name='uq_migration_legacy_mapping'),)


# ---------------------------------------------------------------------------
# Old Data Migration provenance
# ---------------------------------------------------------------------------
# These tables never hold business facts. They make the database-file append
# idempotent while allowing the imported records themselves to live in the
# normal current tables and remain fully editable.

class OldDataSource(db.Model):
    __tablename__ = 'old_data_source'
    id = db.Column(db.Integer, primary_key=True)
    source_key = db.Column(db.String(64), nullable=False, unique=True, index=True)
    origin_label = db.Column(db.String(500), nullable=False)
    first_file_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=pk_model_now, nullable=False)


class OldDataImportRun(db.Model):
    __tablename__ = 'old_data_import_run'
    id = db.Column(db.String(36), primary_key=True)
    source_key = db.Column(db.String(64), nullable=False, index=True)
    filename = db.Column(db.String(255))
    file_hash = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(30), nullable=False, index=True)
    rows_inserted = db.Column(db.Integer, nullable=False, default=0)
    rows_skipped = db.Column(db.Integer, nullable=False, default=0)
    report_json = db.Column(db.Text)
    imported_by = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=pk_model_now, nullable=False)
    completed_at = db.Column(db.DateTime)


class OldDataRecordMap(db.Model):
    __tablename__ = 'old_data_record_map'
    id = db.Column(db.Integer, primary_key=True)
    source_key = db.Column(db.String(64), nullable=False, index=True)
    source_table = db.Column(db.String(100), nullable=False)
    source_pk = db.Column(db.String(255), nullable=False)
    target_table = db.Column(db.String(100), nullable=False)
    target_pk = db.Column(db.String(255), nullable=False)
    source_row_hash = db.Column(db.String(64), nullable=False)
    import_run_id = db.Column(db.String(36), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, nullable=False)
    __table_args__ = (
        db.UniqueConstraint(
            'source_key', 'source_table', 'source_pk',
            name='uq_old_data_record_source',
        ),
        db.Index('ix_old_data_record_target', 'target_table', 'target_pk'),
    )


class OldDataReferenceMap(db.Model):
    __tablename__ = 'old_data_reference_map'
    id = db.Column(db.Integer, primary_key=True)
    source_key = db.Column(db.String(64), nullable=False, index=True)
    reference_kind = db.Column(db.String(40), nullable=False)
    original_value = db.Column(db.String(255), nullable=False)
    current_value = db.Column(db.String(255), nullable=False)
    import_run_id = db.Column(db.String(36), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, nullable=False)
    __table_args__ = (
        db.UniqueConstraint(
            'source_key', 'reference_kind', 'original_value',
            name='uq_old_data_reference_source',
        ),
    )
