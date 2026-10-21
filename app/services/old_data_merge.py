"""Fail-closed, append-only importer for old AMS SQLite databases.

The public UI intentionally stays small; this module carries the safety policy:

* the uploaded database is opened read-only and is never modified;
* current business rows are INSERT-only (never UPDATE/DELETE);
* source primary/foreign keys are remapped into a separate ID namespace;
* colliding display identities and bill references are namespaced, not guessed;
* every imported row receives durable provenance so a repeated upload is a no-op;
* the current schema is authoritative and incompatible changes stop the run;
* foreign keys and UNIQUE constraints stay enabled throughout the transaction;
* every current business row is hashed before/after to prove it was unchanged.

This is deliberately not a permissive generic SQLite copier.  A schema it cannot
prove safe is refused before any production transaction can commit.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


META_TABLE = "__ams_full_db_meta__"
PROBE_TABLES = {"user", "client", "entry"}

# These are application/runtime state, not business history.  They are reported
# but never copied.  In particular, an old file can never replace users,
# credentials, settings, migration state, locks or backup configuration.
TECHNICAL_TABLES = {
    META_TABLE,
    "user",
    "user_login_session",
    "settings",
    "schema_version",
    "migration_history",
    "migration_run",
    "migration_row",
    "migration_mapping",
    "import_upload",
    "import_job",
    "import_history_entry",
    "old_data_source",
    "old_data_import_run",
    "old_data_record_map",
    "old_data_reference_map",
    "system_lock",
    "root_backup_settings",
    "root_backup_email_history",
    "root_recovery_code",
    "tenant_wipe_backup_history",
    "staff_email",
    "bill_counter",
    "direct_sale_draft",
    "recon_basket",
}

# A source FK that deliberately points at protected runtime state is cleared.
PROTECTED_PARENT_TABLES = {"user", "user_login_session"}

# Login/request auditing can append between the analysis POST and its preview
# GET without changing business state. Real financial mutations also change a
# non-audit business table, so excluding these append-only logs avoids false
# stale-analysis refusals without weakening the write-race guard.
SIGNATURE_IGNORED_TABLES = {
    "audit_log",
    "accounting_audit_log",
    "future_account_audit_log",
}

# Root transactions used to detect a first-import overlap.  Same bill + same
# business facts is not auto-duplicated; the run stops for human review.
ROOT_TRANSACTION_SIGNATURES = {
    "direct_sale": ("client_name", "date_posted", "amount", "paid_amount"),
    "booking": ("client_name", "date_posted", "amount", "paid_amount"),
    "grn": ("supplier", "date_posted", "paid_amount", "supplier_invoice_no"),
    "payment": ("client_name", "date_posted", "amount", "payment_type"),
    "supplier_payment": ("date_posted", "amount", "payment_type"),
    "material_return": ("client_name", "date_posted", "amount", "return_type"),
    "delivery": ("client_name", "date_posted"),
}

# Values in these master fields are namespaced only when they collide with a
# current value.  Text snapshots in transaction tables are changed through the
# semantic column maps below so the imported graph stays internally coherent.
MASTER_FIELDS = {
    "client": (("code", "client_code", "code"), ("name", "client_name", "name")),
    "supplier": (("name", "supplier_name", "name"),),
    "material": (("code", "material_code", "code"), ("name", "material_name", "name")),
    "account": (("name", "account_name", "name"),),
    "delivery_person": (("name", "delivery_person_name", "name"),),
    "fbm_client": (("name", "fbm_client_name", "name"),),
    "fbm_rental_item": (("name", "fbm_item_name", "name"),),
}

CLIENT_NAME_COLS = {"client_name", "client"}
CLIENT_CODE_COLS = {"client_code"}
SUPPLIER_NAME_COLS = {"supplier", "supplier_name"}
MATERIAL_NAME_COLS = {"material", "material_name", "mat_name", "product_name", "product", "booked_material"}
ACCOUNT_NAME_COLS = {"account_name"}
DELIVERY_PERSON_NAME_COLS = {"delivery_person_name"}

AUTO_REF_COLS = {"auto_bill_no"}
MANUAL_REF_COLS = {"manual_bill_no"}
RELATED_REF_COLS = {"bill_no", "invoice_no", "source_bill_no"}

SOURCE_TYPE_TABLES = {
    "directsale": "direct_sale",
    "direct_sale": "direct_sale",
    "sales": "direct_sale",
    "payment": "payment",
    "supplierpayment": "supplier_payment",
    "supplier_payment": "supplier_payment",
    "materialreturn": "material_return",
    "material_return": "material_return",
    "booking": "booking",
    "grn": "grn",
    "cashflowentry": "cash_flow_entry",
    "cash_flow_entry": "cash_flow_entry",
    "accounttransaction": "account_transaction",
    "account_transaction": "account_transaction",
}

CONTROL_DDL = """
CREATE TABLE IF NOT EXISTS old_data_source (
    id INTEGER PRIMARY KEY,
    source_key VARCHAR(64) NOT NULL UNIQUE,
    origin_label VARCHAR(500) NOT NULL,
    first_file_hash VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL
);
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
    UNIQUE(source_key, source_table, source_pk)
);
CREATE INDEX IF NOT EXISTS ix_old_data_record_map_target
    ON old_data_record_map(target_table, target_pk);
CREATE TABLE IF NOT EXISTS old_data_reference_map (
    id INTEGER PRIMARY KEY,
    source_key VARCHAR(64) NOT NULL,
    reference_kind VARCHAR(40) NOT NULL,
    original_value VARCHAR(255) NOT NULL,
    current_value VARCHAR(255) NOT NULL,
    import_run_id VARCHAR(36) NOT NULL,
    created_at DATETIME NOT NULL,
    UNIQUE(source_key, reference_kind, original_value)
);
"""


class OldDataMergeError(Exception):
    """A safety check refused an old-data operation."""


class OldDataCompatibilityError(OldDataMergeError):
    """The source cannot be represented by the current schema without guessing."""


class OldDataConflictError(OldDataMergeError):
    """The source overlaps/conflicts with current business data."""


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _open(path: Path, mode: str = "ro") -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode={mode}"
    con = sqlite3.connect(uri, uri=True, timeout=120, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=120000")
    return con


def file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _tables(con: sqlite3.Connection) -> List[str]:
    return [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _columns(con: sqlite3.Connection, table: str) -> List[sqlite3.Row]:
    return list(con.execute(f"PRAGMA table_info({_q(table)})"))


def _column_names(con: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in _columns(con, table)]


def _primary_key(con: sqlite3.Connection, table: str) -> List[sqlite3.Row]:
    return sorted((r for r in _columns(con, table) if r[5]), key=lambda r: r[5])


def _count(con: sqlite3.Connection, table: str) -> int:
    return int(con.execute(f"SELECT COUNT(*) FROM {_q(table)}").fetchone()[0])


def _read_meta(con: sqlite3.Connection) -> dict:
    if META_TABLE not in set(_tables(con)):
        return {}
    result = {}
    try:
        rows = con.execute(f"SELECT key, value FROM {_q(META_TABLE)}")
        for key, value in rows:
            try:
                result[key] = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                result[key] = value
    except sqlite3.DatabaseError:
        return {}
    return result


def _canonical(value):
    if isinstance(value, bytes):
        return {"__bytes_sha256__": hashlib.sha256(value).hexdigest()}
    return value


def _row_hash(row: Mapping) -> str:
    payload = {str(k): _canonical(row[k]) for k in sorted(row.keys())}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _norm(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _affinity(declared: str) -> str:
    text = (declared or "").upper()
    if "INT" in text:
        return "INTEGER"
    if any(x in text for x in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in text or not text:
        return "BLOB"
    if any(x in text for x in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _compatible_type(source_type: str, target_type: str) -> bool:
    a, b = _affinity(source_type), _affinity(target_type)
    if a == b:
        return True
    return {a, b} <= {"INTEGER", "REAL", "NUMERIC"}


def _declared_size(declared: str, fallback: int = 255) -> int:
    match = re.search(r"\((\d+)\)", declared or "")
    return int(match.group(1)) if match else fallback


def _bounded(value: str, suffix: str, size: int) -> str:
    value = str(value or "").strip()
    if len(value) + len(suffix) <= size:
        return value + suffix
    return value[: max(1, size - len(suffix))].rstrip() + suffix


def _source_identity(con: sqlite3.Connection, file_hash: str) -> Tuple[str, str]:
    meta = _read_meta(con)
    explicit = meta.get("database_id") or meta.get("instance_id")
    if explicit:
        label = f"database_id={explicit}"
    else:
        host = str(meta.get("host") or "").strip()
        source = str(meta.get("source") or "").strip()
        if host or source:
            label = f"host={host or 'unknown'};source={source or 'unknown'}"
        else:
            # Plain DB files have no sealed installation UUID. Build a stable
            # conservative anchor from schema plus the earliest master
            # identities, so a later snapshot from the same DB is incremental
            # rather than a duplicate. If those anchor identities themselves
            # change, overlap detection still fails closed.
            anchor = hashlib.sha256()
            anchored_rows = 0
            for table, identity_columns in (
                ("user", ("id", "username")),
                ("client", ("id", "code", "name")),
                ("material", ("id", "code", "name")),
                ("account", ("id", "name")),
            ):
                if table not in set(_tables(con)):
                    continue
                available = [c for c in identity_columns if c in set(_column_names(con, table))]
                if not available:
                    continue
                pk = _primary_key(con, table)
                order = _q(pk[0][1]) if pk else "rowid"
                sql = (
                    f"SELECT {', '.join(_q(c) for c in available)} "
                    f"FROM {_q(table)} ORDER BY {order} LIMIT 5"
                )
                for row in con.execute(sql):
                    anchor.update(table.encode())
                    anchor.update(json.dumps(list(row), default=str).encode())
                    anchored_rows += 1
            label = (
                f"plain-anchor={anchor.hexdigest()}"
                if anchored_rows
                else f"plain-file={file_hash}"
            )
    key = hashlib.sha256(label.encode("utf-8")).hexdigest()
    return key, label


def _verify_database(con: sqlite3.Connection, *, source: bool) -> dict:
    integrity = [r[0] for r in con.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        raise OldDataMergeError(f"{'Source' if source else 'Current'} database integrity failed: {integrity[:5]}")
    tables = set(_tables(con))
    missing = sorted(PROBE_TABLES - tables)
    if missing:
        raise OldDataMergeError(
            f"{'Source' if source else 'Current'} file is not a supported AMS database; "
            f"missing tables: {missing}"
        )
    fk = list(con.execute("PRAGMA foreign_key_check"))
    if fk:
        raise OldDataMergeError(
            f"{'Source' if source else 'Current'} database has {len(fk)} foreign-key "
            "violations. Repair it before old-data migration."
        )
    counts = {t: _count(con, t) for t in sorted(tables) if t != META_TABLE}
    if source:
        meta = _read_meta(con)
        expected = meta.get("tables")
        if isinstance(expected, dict):
            mismatches = {
                t: {"expected": expected.get(t), "actual": counts.get(t)}
                for t in expected
                if counts.get(t) != expected.get(t)
            }
            if mismatches:
                raise OldDataMergeError(
                    "Snapshot row counts do not match its sealed metadata; the "
                    f"file may be incomplete or changed: {dict(list(mismatches.items())[:5])}"
                )
    return {"integrity": "ok", "fk_violations": 0, "tables": counts}


def _business_tables(con: sqlite3.Connection) -> List[str]:
    return [t for t in _tables(con) if t not in TECHNICAL_TABLES]


def target_signature(target_or_connection) -> str:
    """Strong digest of current business schema + rows (technical state ignored)."""
    owns = not isinstance(target_or_connection, sqlite3.Connection)
    con = _open(Path(target_or_connection), "ro") if owns else target_or_connection
    try:
        digest = hashlib.sha256()
        for table in _business_tables(con):
            if table in SIGNATURE_IGNORED_TABLES:
                continue
            schema = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            digest.update(table.encode())
            digest.update(str(schema[0] if schema else "").encode())
            pk = [r[1] for r in _primary_key(con, table)]
            order = ", ".join(_q(c) for c in pk) if pk else "rowid"
            for row in con.execute(f"SELECT * FROM {_q(table)} ORDER BY {order}"):
                digest.update(_row_hash(dict(row)).encode())
        return digest.hexdigest()
    finally:
        if owns:
            con.close()


def clone_database(source_path, destination_path) -> Path:
    """Create a transactionally consistent SQLite backup copy."""
    src = _open(Path(source_path), "ro")
    destination = Path(destination_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    dst = sqlite3.connect(str(destination), timeout=120, isolation_level=None)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return destination


def _ensure_compatible(source: sqlite3.Connection, target: sqlite3.Connection) -> List[str]:
    source_tables = set(_tables(source))
    target_tables = set(_tables(target))
    unknown = sorted(t for t in source_tables - target_tables if t not in TECHNICAL_TABLES)
    if unknown:
        raise OldDataCompatibilityError(
            "The old file contains business tables that the current app does not "
            f"understand: {unknown}. No rows were imported."
        )

    shared = sorted((source_tables & target_tables) - TECHNICAL_TABLES)
    issues = []
    for table in shared:
        source_info = {r[1]: r for r in _columns(source, table)}
        target_info = {r[1]: r for r in _columns(target, table)}
        source_only = sorted(set(source_info) - set(target_info))
        if source_only:
            issues.append(f"{table}: current schema has no columns {source_only}")
        for name in sorted(set(source_info) & set(target_info)):
            if not _compatible_type(source_info[name][2], target_info[name][2]):
                issues.append(
                    f"{table}.{name}: source type {source_info[name][2]!r} is not "
                    f"compatible with current type {target_info[name][2]!r}"
                )
        missing = sorted(set(target_info) - set(source_info))
        for name in missing:
            row = target_info[name]
            if row[5]:  # primary key
                issues.append(f"{table}: source lacks current primary key column {name}")
            elif row[3] and row[4] is None:
                # Known current derivations are populated explicitly later.
                if not (table == "direct_sale_item" and name in {"material_id", "booked_material_id"}):
                    issues.append(
                        f"{table}.{name}: new required column has no safe default"
                    )
        if len(_primary_key(source, table)) != 1 or len(_primary_key(target, table)) != 1:
            issues.append(f"{table}: exactly one primary-key column is required")
    if issues:
        raise OldDataCompatibilityError(
            "Schema compatibility check failed; an explicit version adapter is "
            "required. " + " | ".join(issues[:20])
        )
    return shared


def _source_rows(con: sqlite3.Connection, table: str) -> List[dict]:
    pk = [r[1] for r in _primary_key(con, table)]
    order = ", ".join(_q(c) for c in pk) if pk else "rowid"
    return [dict(r) for r in con.execute(f"SELECT * FROM {_q(table)} ORDER BY {order}")]


def _existing_row_snapshot(con: sqlite3.Connection, tables: Sequence[str]) -> dict:
    result = {}
    for table in tables:
        pk = [r[1] for r in _primary_key(con, table)]
        if len(pk) != 1:
            continue
        name = pk[0]
        result[table] = {
            str(row[name]): _row_hash(dict(row))
            for row in con.execute(f"SELECT * FROM {_q(table)}")
        }
    return result


def _assert_existing_unchanged(con: sqlite3.Connection, before: dict) -> int:
    checked = 0
    for table, expected in before.items():
        pk = _primary_key(con, table)[0][1]
        remaining = dict(expected)
        for row in con.execute(f"SELECT * FROM {_q(table)}"):
            key = str(row[pk])
            if key not in remaining:
                continue
            if _row_hash(dict(row)) != remaining.pop(key):
                raise OldDataMergeError(
                    f"Safety invariant failed: existing {table} record {key} changed."
                )
            checked += 1
        if remaining:
            raise OldDataMergeError(
                f"Safety invariant failed: {len(remaining)} existing {table} rows disappeared."
            )
    return checked


def _foreign_keys(con: sqlite3.Connection, table: str) -> Dict[str, Tuple[str, str]]:
    # PRAGMA columns: id, seq, table, from, to, on_update, on_delete, match
    return {r[3]: (r[2], r[4]) for r in con.execute(f"PRAGMA foreign_key_list({_q(table)})")}


def _parse_pk(value, declared_type: str):
    if value is None:
        return None
    if _affinity(declared_type) == "INTEGER":
        return int(value)
    return str(value)


def _make_unique_text(original: str, used: set, token: str, size: int, *, code=False) -> str:
    original = str(original or "").strip()
    if _norm(original) not in used:
        used.add(_norm(original))
        return original
    if code:
        prefix = f"OD{token[:6]}-"
        candidate = (prefix + original)[:size]
    else:
        candidate = _bounded(original, f" [OLD {token[:6]}]", size)
    base = candidate
    counter = 2
    while _norm(candidate) in used:
        suffix = f"-{counter}"
        candidate = _bounded(base, suffix, size)
        counter += 1
    used.add(_norm(candidate))
    return candidate


def _master_value_maps(source, target, shared: Sequence[str], token: str) -> Tuple[dict, int]:
    maps = {
        "client_code": {}, "client_name": {}, "supplier_name": {},
        "material_code": {}, "material_name": {}, "account_name": {},
        "delivery_person_name": {}, "fbm_client_name": {}, "fbm_item_name": {},
    }
    collisions = 0
    for table, specs in MASTER_FIELDS.items():
        if table not in shared:
            continue
        target_cols = {r[1]: r for r in _columns(target, table)}
        source_cols = set(_column_names(source, table))
        for field, map_name, style in specs:
            if field not in target_cols or field not in source_cols:
                continue
            used = {
                _norm(r[0])
                for r in target.execute(
                    f"SELECT {_q(field)} FROM {_q(table)} WHERE {_q(field)} IS NOT NULL"
                )
                if _norm(r[0])
            }
            size = _declared_size(target_cols[field][2])
            for row in source.execute(
                f"SELECT DISTINCT {_q(field)} FROM {_q(table)} WHERE {_q(field)} IS NOT NULL"
            ):
                old = str(row[0]).strip()
                if not old or old in maps[map_name]:
                    continue
                new = _make_unique_text(old, used, token, size, code=(style == "code"))
                maps[map_name][old] = new
                if new != old:
                    collisions += 1
    return maps, collisions


def _safe_namespace(raw: str) -> str:
    text = re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())
    return text[:4] or "GEN"


def _reference_maps(source, target, shared: Sequence[str], token: str) -> Tuple[dict, int]:
    target_values = {"auto": set(), "manual": set()}
    source_values = {"auto": set(), "manual": set()}
    for con, bucket in ((target, target_values), (source, source_values)):
        for table in shared:
            cols = set(_column_names(con, table))
            for col in AUTO_REF_COLS & cols:
                bucket["auto"].update(
                    str(r[0]).strip() for r in con.execute(
                        f"SELECT DISTINCT {_q(col)} FROM {_q(table)} "
                        f"WHERE {_q(col)} IS NOT NULL AND TRIM({_q(col)})<>''"
                    )
                )
            for col in MANUAL_REF_COLS & cols:
                bucket["manual"].update(
                    str(r[0]).strip() for r in con.execute(
                        f"SELECT DISTINCT {_q(col)} FROM {_q(table)} "
                        f"WHERE {_q(col)} IS NOT NULL AND TRIM({_q(col)})<>''"
                    )
                )
    auto_map, manual_map = {}, {}
    used_auto = {_norm(x) for x in target_values["auto"]}
    used_manual = {_norm(x) for x in target_values["manual"]}
    for raw in sorted(source_values["auto"]):
        match = re.search(r"(?:SB\s*-\s*)?([A-Z][A-Z0-9]{0,7})\s*-\s*(\d+)\s*$", raw.upper())
        if match:
            old_ns, seq = match.group(1), int(match.group(2))
        else:
            old_ns = "GEN"
            seq = int(hashlib.sha256(raw.encode()).hexdigest()[:10], 16) % 900000000 + 100000000
        namespace = ("O" + token[:3].upper() + _safe_namespace(old_ns))[:8]
        candidate = f"SB-{namespace}-{seq}"
        bump = 0
        while _norm(candidate) in used_auto:
            bump += 1
            candidate = f"SB-{namespace}-{seq + bump}"
        used_auto.add(_norm(candidate))
        auto_map[raw] = candidate

    # Manual/external references are preserved unless they already exist.  A
    # collision receives a visible, deterministic source namespace.
    for raw in sorted(source_values["manual"]):
        if _norm(raw) not in used_manual:
            manual_map[raw] = raw
            used_manual.add(_norm(raw))
            continue
        body = re.sub(r"^MB\s*NO\.\s*", "", raw, flags=re.I).strip()
        candidate = f"MB NO.OD{token[:6]}-{body}"[:50]
        n = 2
        base = candidate
        while _norm(candidate) in used_manual:
            candidate = _bounded(base, f"-{n}", 50)
            n += 1
        used_manual.add(_norm(candidate))
        manual_map[raw] = candidate
    changed = sum(1 for k, v in auto_map.items() if k != v) + sum(
        1 for k, v in manual_map.items() if k != v
    )
    return {"auto": auto_map, "manual": manual_map}, changed


def _simple_value(value):
    if value is None:
        return None
    if isinstance(value, float):
        return round(value, 6)
    text = str(value).strip()
    # Compare date/time facts across harmless formatting differences.
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:19]
    return _norm(text)


def _detect_exact_transaction_overlap(source, target, shared: Sequence[str]) -> List[str]:
    conflicts = []
    for table, facts in ROOT_TRANSACTION_SIGNATURES.items():
        if table not in shared:
            continue
        cols = set(_column_names(source, table)) & set(_column_names(target, table))
        refs = [c for c in ("auto_bill_no", "manual_bill_no") if c in cols]
        compare = [c for c in facts if c in cols]
        if not refs or not compare:
            continue
        target_by_ref = {}
        for row in target.execute(f"SELECT * FROM {_q(table)}"):
            item = dict(row)
            for ref in refs:
                value = _norm(item.get(ref))
                if value:
                    target_by_ref.setdefault((ref, value), []).append(item)
        for row in source.execute(f"SELECT * FROM {_q(table)}"):
            item = dict(row)
            for ref in refs:
                raw = item.get(ref)
                if not _norm(raw):
                    continue
                for existing in target_by_ref.get((ref, _norm(raw)), []):
                    if all(_simple_value(item.get(c)) == _simple_value(existing.get(c)) for c in compare):
                        conflicts.append(f"{table}.{ref}={raw}")
                        break
                if conflicts and conflicts[-1].startswith(f"{table}.{ref}={raw}"):
                    break
    return conflicts


def _source_table_name(value) -> Optional[str]:
    if value is None:
        return None
    raw = str(value).strip()
    key = re.sub(r"[^a-z0-9_]", "", raw.casefold())
    compact = key.replace("_", "")
    return SOURCE_TYPE_TABLES.get(key) or SOURCE_TYPE_TABLES.get(compact)


def _semantic_transform(table: str, column: str, value, name_maps: dict, ref_maps: dict):
    if value is None:
        return None
    if column in CLIENT_CODE_COLS:
        return name_maps["client_code"].get(str(value).strip(), value)
    if column in CLIENT_NAME_COLS:
        return name_maps["client_name"].get(str(value).strip(), value)
    if column in SUPPLIER_NAME_COLS:
        return name_maps["supplier_name"].get(str(value).strip(), value)
    if column in MATERIAL_NAME_COLS:
        return name_maps["material_name"].get(str(value).strip(), value)
    if column in ACCOUNT_NAME_COLS:
        return name_maps["account_name"].get(str(value).strip(), value)
    if column in DELIVERY_PERSON_NAME_COLS:
        return name_maps["delivery_person_name"].get(str(value).strip(), value)
    if table == "client" and column == "code":
        return name_maps["client_code"].get(str(value).strip(), value)
    if table == "client" and column == "name":
        return name_maps["client_name"].get(str(value).strip(), value)
    if table == "supplier" and column == "name":
        return name_maps["supplier_name"].get(str(value).strip(), value)
    if table == "material" and column == "code":
        return name_maps["material_code"].get(str(value).strip(), value)
    if table == "material" and column == "name":
        return name_maps["material_name"].get(str(value).strip(), value)
    if table == "account" and column == "name":
        return name_maps["account_name"].get(str(value).strip(), value)
    if table == "delivery_person" and column == "name":
        return name_maps["delivery_person_name"].get(str(value).strip(), value)
    if table == "fbm_client" and column == "name":
        return name_maps["fbm_client_name"].get(str(value).strip(), value)
    if table == "fbm_rental_item" and column == "name":
        return name_maps["fbm_item_name"].get(str(value).strip(), value)
    if column in AUTO_REF_COLS:
        return ref_maps["auto"].get(str(value).strip(), value)
    if column in MANUAL_REF_COLS:
        return ref_maps["manual"].get(str(value).strip(), value)
    if column in RELATED_REF_COLS:
        raw = str(value).strip()
        return ref_maps["auto"].get(raw, ref_maps["manual"].get(raw, value))
    return value


def _lookup_map(pk_map: dict, table: str, old_value):
    if old_value is None:
        return None
    key = (table, str(old_value))
    if key not in pk_map:
        raise OldDataCompatibilityError(
            f"Cannot safely remap reference to {table} id {old_value}; source graph is incomplete."
        )
    return pk_map[key]


def _apply_undeclared_links(table: str, row: dict, values: dict, pk_map: dict) -> int:
    """Remap best-effort historical links that SQLite does not enforce.

    Legacy databases can contain dangling values in these optional provenance
    columns even while PRAGMA foreign_key_check passes (there is no declared
    FK). Keeping such an old integer would be dangerous because it might point
    at an unrelated current row. We therefore clear only the dangling optional
    link, retain the transaction itself, and count it in the audit report.
    """
    cleared = 0

    def remap(column, parent, *, required=False):
        nonlocal cleared
        if column not in row or row.get(column) is None:
            return
        key = (parent, str(row[column]))
        if key in pk_map:
            values[column] = pk_map[key]
        elif required:
            raise OldDataCompatibilityError(
                f"Cannot safely remap required reference {table}.{column} to "
                f"{parent} id {row[column]}."
            )
        else:
            values[column] = None
            cleared += 1

    if table == "cash_day_account_position":
        remap("account_id", "account", required=True)
    elif table == "account_reconciliation":
        remap("adjustment_transaction_id", "account_transaction")
        if "created_by_id" in values:
            values["created_by_id"] = None
    elif table == "booking_allocation_repair_archive":
        # Archive snapshot identifiers intentionally have no FKs; remap where
        # the live source row survives, otherwise preserve the archive fact.
        for column, parent in (
            ("sale_id", "direct_sale"),
            ("sale_item_id", "direct_sale_item"),
            ("booking_item_id", "booking_item"),
            ("original_allocation_id", "booking_allocation"),
        ):
            old = row.get(column)
            if old is not None and (parent, str(old)) in pk_map:
                values[column] = pk_map[(parent, str(old))]
    elif table == "audit_log":
        if "user_id" in values:
            values["user_id"] = None
    elif table == "accounting_audit_log":
        if "user_id" in values:
            values["user_id"] = None
        entity_table = _source_table_name(row.get("entity_type"))
        if entity_table and row.get("entity_id") is not None:
            key = (entity_table, str(row["entity_id"]))
            if key in pk_map:
                values["entity_id"] = pk_map[key]
            else:
                values["entity_id"] = None
                cleared += 1
        for column in ("account_before_id", "account_after_id"):
            remap(column, "account")

    # Polymorphic source links used throughout ledgers/stock.
    if row.get("source_id") is not None:
        discriminator = row.get("source_table") or row.get("source_type")
        parent = _source_table_name(discriminator)
        key = (parent, str(row["source_id"])) if parent else None
        if key and key in pk_map:
            values["source_id"] = pk_map[key]
        elif "source_id" in values:
            values["source_id"] = None
            cleared += 1
    return cleared


def _material_id_for_name(source_name, source_material_ids: dict, pk_map: dict):
    ids = source_material_ids.get(_norm(source_name), [])
    if len(ids) != 1:
        return None
    return pk_map.get(("material", str(ids[0])))


def _control_row(con, source_key: str, table: str, old_pk: str):
    return con.execute(
        "SELECT target_pk, source_row_hash FROM old_data_record_map "
        "WHERE source_key=? AND source_table=? AND source_pk=?",
        (source_key, table, old_pk),
    ).fetchone()


def append_old_data(
    source_path,
    target_path,
    *,
    filename: Optional[str] = None,
    imported_by: Optional[str] = None,
    run_id: Optional[str] = None,
    expected_target_signature: Optional[str] = None,
) -> dict:
    """Append a complete old AMS business graph into the current database.

    The caller should run this once against a disposable clone (analysis), then
    against production with ``expected_target_signature`` from the analysis.
    This function itself is atomic: any issue rolls back every imported row.
    """
    src_path = Path(source_path).expanduser().resolve()
    tgt_path = Path(target_path).expanduser().resolve()
    if not src_path.exists() or not tgt_path.exists():
        raise OldDataMergeError("Source and current database files must both exist.")
    if src_path == tgt_path:
        raise OldDataMergeError("Old-data source and current database are the same file.")
    with src_path.open("rb") as header_file:
        header = header_file.read(16)
    if header != b"SQLite format 3\x00":
        raise OldDataMergeError("Uploaded file is not a SQLite database.")

    source_hash = file_sha256(src_path)
    source = _open(src_path, "ro")
    target = _open(tgt_path, "rw")
    run_id = run_id or str(uuid.uuid4())
    started = _now()
    in_tx = False
    try:
        source_check = _verify_database(source, source=True)
        _verify_database(target, source=False)
        source_key, origin_label = _source_identity(source, source_hash)
        token = source_key[:6].upper()

        # Lock all writers before checking the analyzed target fingerprint.
        target.execute("PRAGMA foreign_keys=ON")
        target.execute("BEGIN IMMEDIATE")
        in_tx = True
        target.execute("PRAGMA defer_foreign_keys=ON")

        # Crash recovery must run before the stale-analysis signature check: a
        # successful committed import necessarily changed that signature even
        # if the filesystem manifest write was interrupted afterwards.
        has_run_table = target.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='old_data_import_run'"
        ).fetchone()
        if has_run_table:
            committed_run = target.execute(
                "SELECT file_hash,status,report_json FROM old_data_import_run WHERE id=?",
                (run_id,),
            ).fetchone()
            if committed_run:
                if committed_run[0] != source_hash or committed_run[1] != "COMPLETED":
                    raise OldDataConflictError(
                        "Import run identifier is already used by a different/incomplete run."
                    )
                try:
                    committed_report = json.loads(committed_run[2] or "{}")
                except json.JSONDecodeError as exc:
                    raise OldDataMergeError("Committed import report is unreadable.") from exc
                target.execute("ROLLBACK")
                in_tx = False
                return committed_report

        current_signature = target_signature(target)
        if expected_target_signature and current_signature != expected_target_signature:
            raise OldDataConflictError(
                "The current database changed after analysis. No data was imported; "
                "upload/analyze again against the latest data."
            )

        # Execute each control-table DDL statement inside the same transaction;
        # sqlite3.executescript() would implicitly commit first in autocommit
        # mode, weakening the all-or-nothing promise.
        for statement in CONTROL_DDL.split(";"):
            if statement.strip():
                target.execute(statement)

        shared = _ensure_compatible(source, target)
        excluded = {
            table: source_check["tables"].get(table, 0)
            for table in sorted(set(_tables(source)) & TECHNICAL_TABLES)
            if source_check["tables"].get(table, 0)
        }

        # The running app has a hard no-void-at-boot policy. Silently importing
        # rows that the next restart would delete is not acceptable.
        voided = {}
        for table in shared:
            if "is_void" in set(_column_names(source, table)):
                n = int(source.execute(
                    f"SELECT COUNT(*) FROM {_q(table)} WHERE COALESCE(is_void,0)<>0"
                ).fetchone()[0])
                if n:
                    voided[table] = n
        if voided:
            raise OldDataCompatibilityError(
                "The old file contains voided rows, but this AMS installation "
                f"uses a no-void storage policy: {voided}. Purge/resolve them in "
                "the source first; no rows were imported."
            )

        known_source = target.execute(
            "SELECT 1 FROM old_data_record_map WHERE source_key=? LIMIT 1",
            (source_key,),
        ).fetchone()
        if not known_source:
            overlaps = _detect_exact_transaction_overlap(source, target, shared)
            if overlaps:
                raise OldDataConflictError(
                    "Probable transactions already exist in the current database: "
                    + ", ".join(overlaps[:20])
                    + ". The importer refuses to double-post them. No rows were imported."
                )

        existing_before = _existing_row_snapshot(target, shared)
        source_rows = {table: _source_rows(source, table) for table in shared}

        # Existing provenance makes repeat imports (including after a user edits
        # the imported current record) a no-op and protects those edits.
        prior = {}
        for table, rows in source_rows.items():
            pk_name = _primary_key(source, table)[0][1]
            for row in rows:
                old_pk = str(row[pk_name])
                mapped = _control_row(target, source_key, table, old_pk)
                if mapped:
                    if mapped[1] != _row_hash(row):
                        raise OldDataConflictError(
                            f"Previously imported source row changed: {table} {old_pk}. "
                            "Current edits were protected and no rows were imported."
                        )
                    prior[(table, old_pk)] = mapped[0]

        # Allocate the entire key graph before inserting any row. New integer
        # keys are always above the current maximum; text keys are deterministic
        # UUIDs scoped by source/table/old key.
        pk_map = {}
        for table in shared:
            src_pk = _primary_key(source, table)[0]
            tgt_pk = _primary_key(target, table)[0]
            src_name, tgt_name = src_pk[1], tgt_pk[1]
            if src_name != tgt_name:
                raise OldDataCompatibilityError(
                    f"{table}: primary key was renamed ({src_name} -> {tgt_name}); adapter required."
                )
            declared = tgt_pk[2]
            maximum = 0
            if _affinity(declared) == "INTEGER":
                maximum = int(target.execute(
                    f"SELECT COALESCE(MAX({_q(tgt_name)}),0) FROM {_q(table)}"
                ).fetchone()[0])
            for row in source_rows[table]:
                old_key = str(row[src_name])
                if (table, old_key) in prior:
                    pk_map[(table, old_key)] = _parse_pk(prior[(table, old_key)], declared)
                    continue
                if _affinity(declared) == "INTEGER":
                    maximum += 1
                    new_key = maximum
                else:
                    new_key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ams-old:{source_key}:{table}:{old_key}"))
                    bump = 0
                    while target.execute(
                        f"SELECT 1 FROM {_q(table)} WHERE {_q(tgt_name)}=?", (new_key,)
                    ).fetchone():
                        bump += 1
                        new_key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ams-old:{source_key}:{table}:{old_key}:{bump}"))
                pk_map[(table, old_key)] = new_key

        name_maps, master_collisions = _master_value_maps(source, target, shared, token)
        ref_maps, references_rewritten = _reference_maps(source, target, shared, token)
        # A newer snapshot from the same source must reuse every transformation
        # chosen by its first import; otherwise newly added child transactions
        # could display a different client/material/reference namespace.
        for mapped in target.execute(
            "SELECT reference_kind, original_value, current_value "
            "FROM old_data_reference_map WHERE source_key=?",
            (source_key,),
        ):
            kind, original, current = mapped
            if kind == "auto_bill":
                ref_maps["auto"][original] = current
            elif kind == "manual_bill":
                ref_maps["manual"][original] = current
            elif kind in name_maps:
                name_maps[kind][original] = current

        source_material_ids = {}
        if "material" in source_rows:
            for row in source_rows["material"]:
                source_material_ids.setdefault(_norm(row.get("name")), []).append(row.get("id"))

        inserted_by_table, skipped_by_table = {}, {}
        optional_links_cleared = 0
        imported_at = _now()
        # Deferred FK checking permits self references and the reconciliation /
        # adjustment cycle while constraints remain enabled and are checked
        # before commit.
        for table in shared:
            target_info = {r[1]: r for r in _columns(target, table)}
            source_cols = set(_column_names(source, table))
            common_cols = [name for name in target_info if name in source_cols]
            pk_name = _primary_key(source, table)[0][1]
            fks = _foreign_keys(target, table)
            inserted = skipped = 0
            for row in source_rows[table]:
                old_pk = str(row[pk_name])
                if (table, old_pk) in prior:
                    skipped += 1
                    continue
                values = {}
                for column in common_cols:
                    value = row.get(column)
                    if column == pk_name:
                        value = pk_map[(table, old_pk)]
                    value = _semantic_transform(table, column, value, name_maps, ref_maps)
                    values[column] = value

                # Declared relational links always use the remapped parent ID.
                for column, (parent_table, _parent_column) in fks.items():
                    if column not in row or row.get(column) is None:
                        continue
                    if parent_table in PROTECTED_PARENT_TABLES:
                        values[column] = None
                    else:
                        values[column] = _lookup_map(pk_map, parent_table, row[column])

                optional_links_cleared += _apply_undeclared_links(
                    table, row, values, pk_map
                )

                # New current-schema material links can be derived without
                # guessing because the source material name is unambiguous.
                if table == "direct_sale_item":
                    if "material_id" in target_info and "material_id" not in source_cols:
                        values["material_id"] = _material_id_for_name(
                            row.get("product_name"), source_material_ids, pk_map
                        )
                    if "booked_material_id" in target_info and "booked_material_id" not in source_cols:
                        values["booked_material_id"] = _material_id_for_name(
                            row.get("booked_material"), source_material_ids, pk_map
                        )

                if "idempotency_key" in values and values["idempotency_key"]:
                    values["idempotency_key"] = hashlib.sha256(
                        f"old-data:{source_key}:{table}:{old_pk}:{values['idempotency_key']}".encode()
                    ).hexdigest()

                columns = list(values)
                sql = (
                    f"INSERT INTO {_q(table)} ({', '.join(_q(c) for c in columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})"
                )
                try:
                    target.execute(sql, [values[c] for c in columns])
                except sqlite3.IntegrityError as exc:
                    raise OldDataConflictError(
                        f"Current constraints refused {table} source row {old_pk}: {exc}. "
                        "Nothing was imported."
                    ) from exc
                target.execute(
                    "INSERT INTO old_data_record_map "
                    "(source_key,source_table,source_pk,target_table,target_pk," 
                    "source_row_hash,import_run_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        source_key, table, old_pk, table,
                        str(pk_map[(table, old_pk)]), _row_hash(row), run_id, imported_at,
                    ),
                )
                inserted += 1
            inserted_by_table[table] = inserted
            skipped_by_table[table] = skipped

        for kind, mapping in (("auto_bill", ref_maps["auto"]), ("manual_bill", ref_maps["manual"])):
            for old, new in mapping.items():
                # Persist unchanged choices too. A later snapshot from the same
                # source must not reinterpret an originally non-colliding value
                # as a new collision against its own previously imported row.
                target.execute(
                    "INSERT OR IGNORE INTO old_data_reference_map "
                    "(source_key,reference_kind,original_value,current_value,import_run_id,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (source_key, kind, old, new, run_id, imported_at),
                )
        for kind, mapping in name_maps.items():
            for old, new in mapping.items():
                target.execute(
                    "INSERT OR IGNORE INTO old_data_reference_map "
                    "(source_key,reference_kind,original_value,current_value,import_run_id,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (source_key, kind, old, new, run_id, imported_at),
                )

        existing_checked = _assert_existing_unchanged(target, existing_before)
        fk_rows = list(target.execute("PRAGMA foreign_key_check"))
        if fk_rows:
            raise OldDataMergeError(
                f"Merged database would have {len(fk_rows)} foreign-key violations: "
                f"{[tuple(r) for r in fk_rows[:5]]}"
            )
        integrity = [r[0] for r in target.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise OldDataMergeError(f"Merged database integrity failed: {integrity[:5]}")

        rows_inserted = sum(inserted_by_table.values())
        rows_skipped = sum(skipped_by_table.values())
        business_rows = sum(len(rows) for rows in source_rows.values())
        if rows_inserted + rows_skipped != business_rows:
            raise OldDataMergeError(
                "Source coverage invariant failed; not every business row was handled."
            )

        report = {
            "ok": True,
            "verification": "PASS",
            "policy": "append_only_fail_closed_v1",
            "run_id": run_id,
            "source_key": source_key,
            "source_origin": origin_label,
            "source_file": filename or src_path.name,
            "source_hash": source_hash,
            "source_schema_version": (_read_meta(source).get("spec_version") or "unversioned"),
            "analyzed_target_signature": current_signature,
            "business_tables": len(shared),
            "source_business_rows": business_rows,
            "rows_inserted_total": rows_inserted,
            "rows_already_imported": rows_skipped,
            "inserted_by_table": inserted_by_table,
            "skipped_by_table": skipped_by_table,
            "technical_rows_excluded": excluded,
            "master_collisions_namespaced": master_collisions,
            "references_rewritten": references_rewritten,
            "dangling_optional_links_cleared": optional_links_cleared,
            "existing_business_rows_verified_unchanged": existing_checked,
            "existing_business_rows_modified": 0,
            "existing_business_rows_deleted": 0,
            "foreign_key_violations": 0,
            "integrity": "ok",
            "started_at": started,
            "completed_at": _now(),
            "imported_by": imported_by,
        }
        target.execute(
            "INSERT INTO old_data_source "
            "(source_key,origin_label,first_file_hash,created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(source_key) DO NOTHING",
            (source_key, origin_label, source_hash, imported_at),
        )
        target.execute(
            "INSERT INTO old_data_import_run "
            "(id,source_key,filename,file_hash,status,rows_inserted,rows_skipped," 
            "report_json,imported_by,created_at,completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, source_key, filename or src_path.name, source_hash, "COMPLETED",
                rows_inserted, rows_skipped, json.dumps(report, ensure_ascii=False, default=str),
                imported_by, started, report["completed_at"],
            ),
        )
        target.execute("COMMIT")
        in_tx = False
        return report
    except Exception:
        if in_tx and target.in_transaction:
            target.execute("ROLLBACK")
        raise
    finally:
        source.close()
        target.close()
