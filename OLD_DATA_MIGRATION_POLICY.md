# Old Data Migration — safety policy

`/old-data-migration/` is the only supported way to **append** a populated old
AMS `.amsdb` / `.db` into a populated current AMS database. Full Database
Import remains a clean replacement tool; its old raw-PK append UI is disabled.

## Non-negotiable guarantees

1. **Upload is read-only.** The source file is never opened writable.
2. **Analyze before apply.** The full merge runs against a consistent clone of
   the current DB. Production apply is not offered unless that run passes.
3. **No current business UPDATE/DELETE.** Business tables receive INSERTs only.
   Every pre-existing business row is hashed before and after the staged/final
   merge. A changed or missing row aborts the transaction.
4. **No old identity trust.** Every source PK is mapped to a current PK and all
   declared FKs are remapped. Polymorphic links are remapped by type; a dangling
   optional legacy provenance ID is cleared rather than pointed at an unrelated
   current row.
5. **Current constraints stay active.** Foreign-key and UNIQUE indexes are not
   dropped or relaxed. `PRAGMA foreign_key_check` and `integrity_check` must pass
   before commit.
6. **All or nothing.** Final apply uses `BEGIN IMMEDIATE`; any exception rolls
   back every business and provenance insert.
7. **Backup before apply.** A verified SQLite backup is created before the final
   transaction. Its path is retained in the run manifest/report.
8. **Idempotent.** `(source identity, table, source PK)` provenance protects
   imported records from duplicate upload. A later source snapshot can add new
   rows but cannot overwrite an imported record that a user edited in AMS.
9. **Protected runtime state.** Users/passwords, login sessions, settings,
   schema history, locks, backup settings, counters, drafts and import-runtime
   tables are never copied from the old database.
10. **Fail closed for future schema changes.** Nullable/defaulted current fields
    are safe. Removed source facts, incompatible types, new required fields with
    no default, unknown tables, changed previously imported source rows, exact
    transaction overlap or stale analysis all refuse the complete run.

## Collision policy

* Integer/UUID primary keys are remapped.
* Master code/name collisions receive a visible `[OLD <source>]` or `OD...`
  namespace; current masters are never silently merged or overwritten.
* Imported auto bill references receive a valid source-specific `SB-...`
  namespace. Colliding manual references receive a source namespace.
* The original-to-current values are retained in `old_data_reference_map`.
* A same-reference transaction with matching business facts is treated as a
  probable duplicate and blocks first import instead of double-posting it.

## Imported data behavior

Business rows are inserted into the normal current tables—not an archive—so
they use current views, ledgers, edit forms, permissions and audit behavior.
Technical provenance lives separately in:

* `old_data_source`
* `old_data_import_run`
* `old_data_record_map`
* `old_data_reference_map`

Deleting or editing an imported current record is respected. Re-upload never
restores/overwrites it behind the user's back.

## Deliberate refusal cases

A refusal is a safety result, not a partial failure. In particular, the run
stops if:

* the file is corrupt, tampered, not AMS, or has FK violations;
* the source contains an unknown business table/column;
* a required current field cannot be defaulted;
* current business data changes between analysis and apply;
* an exact transaction overlap suggests the history is already present;
* a previously imported source row changed;
* a current UNIQUE/FK/business constraint rejects any transformed row;
* the source contains voided rows while this installation's boot policy is
  configured to purge voided data.

Every future breaking schema change must ship an explicit old-data adapter and
fixture test. The importer must refuse an unknown semantic change; it must never
guess.
