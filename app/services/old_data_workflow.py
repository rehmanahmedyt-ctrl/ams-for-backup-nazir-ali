"""UI workflow around the append-only old-data merge engine."""
from __future__ import annotations

import fcntl
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from flask import current_app
from werkzeug.utils import secure_filename

from full_db_sync.engine import backup_database
from models import db
from .old_data_merge import (
    OldDataMergeError,
    append_old_data,
    clone_database,
    file_sha256,
    target_signature,
)

ALLOWED_SUFFIXES = {".amsdb", ".db", ".sqlite", ".sqlite3"}
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GiB hard safety boundary


def _base_dir() -> Path:
    configured = current_app.config.get("OLD_DATA_MIGRATION_DIR")
    path = (
        Path(configured)
        if configured
        else Path(current_app.instance_path) / "migration" / "old_data"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def _manifest_path(token: str) -> Path:
    try:
        safe = str(uuid.UUID(str(token)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise OldDataMergeError("Invalid old-data migration token.") from exc
    return _base_dir() / f"{safe}.json"


def _write_manifest(manifest: dict) -> None:
    path = _manifest_path(manifest["token"])
    temp = path.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(temp, path)


def load_manifest(token: str) -> dict:
    path = _manifest_path(token)
    if not path.exists():
        raise OldDataMergeError("Old-data migration analysis was not found.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OldDataMergeError("Old-data migration analysis is unreadable.") from exc


def list_manifests(limit: int = 30) -> list:
    rows = []
    for path in sorted(_base_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
        if len(rows) >= limit:
            break
    return rows


def save_upload(file_storage) -> tuple[str, Path, str]:
    name = secure_filename(file_storage.filename or "")
    suffix = Path(name).suffix.lower()
    if not name or suffix not in ALLOWED_SUFFIXES:
        raise OldDataMergeError("Choose an AMS SQLite file (.amsdb or .db).")
    token = str(uuid.uuid4())
    path = _base_dir() / f"{token}{suffix}"
    size = 0
    try:
        with path.open("wb") as destination:
            while True:
                chunk = file_storage.stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise OldDataMergeError(
                        "Uploaded database is larger than the 1 GiB safety limit."
                    )
                destination.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if size < 16:
        path.unlink(missing_ok=True)
        raise OldDataMergeError("Uploaded database file is empty or incomplete.")
    return token, path, name


def analyze_upload(token: str, upload_path: Path, original_name: str, actor: str) -> dict:
    db_path = Path(str(current_app.config.get("APP_DB_PATH") or ""))
    if not db_path.exists():
        raise OldDataMergeError("Current application database was not found.")
    source_hash = file_sha256(upload_path)
    signature = target_signature(db_path)
    stage_path = _base_dir() / f"{token}.stage.db"
    db.session.remove()
    try:
        clone_database(db_path, stage_path)
        report = append_old_data(
            upload_path,
            stage_path,
            filename=original_name,
            imported_by=actor,
            run_id=token,
            expected_target_signature=signature,
        )
    finally:
        stage_path.unlink(missing_ok=True)
        Path(str(stage_path) + "-wal").unlink(missing_ok=True)
        Path(str(stage_path) + "-shm").unlink(missing_ok=True)

    manifest = {
        "token": token,
        "status": "READY",
        "filename": original_name,
        "upload_path": str(upload_path),
        "source_hash": source_hash,
        "target_signature": signature,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "created_by": actor,
        "report": report,
    }
    _write_manifest(manifest)
    return manifest


def analyze_file_storage(file_storage, actor: str) -> dict:
    token, path, name = save_upload(file_storage)
    try:
        return analyze_upload(token, path, name, actor)
    except Exception:
        # Keep a rejected source only when a manifest/report exists. A malformed
        # upload has no audit value and may contain sensitive arbitrary bytes.
        path.unlink(missing_ok=True)
        raise


def apply_manifest(token: str, actor: str) -> dict:
    manifest = load_manifest(token)
    if manifest.get("status") == "COMPLETED":
        return manifest
    if manifest.get("status") != "READY":
        raise OldDataMergeError("This analysis is not ready to import.")
    upload_path = Path(manifest.get("upload_path") or "")
    if not upload_path.exists():
        raise OldDataMergeError("The analyzed upload is no longer available.")
    if file_sha256(upload_path) != manifest.get("source_hash"):
        raise OldDataMergeError("The uploaded file changed after analysis. Import refused.")

    db_path = Path(str(current_app.config.get("APP_DB_PATH") or ""))
    if not db_path.exists():
        raise OldDataMergeError("Current application database was not found.")

    lock_path = _base_dir() / ".apply.lock"
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OldDataMergeError("Another old-data migration is currently running.") from exc

        # Release ORM connections before the raw SQLite BEGIN IMMEDIATE lock.
        db.session.remove()
        backup = backup_database(
            db_path,
            backup_dir=Path(current_app.instance_path) / "migration",
            prefix="pre_old_data_import_",
        )
        try:
            report = append_old_data(
                upload_path,
                db_path,
                filename=manifest.get("filename"),
                imported_by=actor,
                run_id=token,
                expected_target_signature=manifest.get("target_signature"),
            )
        except Exception as exc:
            manifest["last_error"] = str(exc)
            manifest["last_attempt_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            manifest["backup_path"] = str(backup) if backup else None
            _write_manifest(manifest)
            raise

        report["backup_path"] = str(backup) if backup else None
        manifest.update(
            status="COMPLETED",
            completed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            completed_by=actor,
            backup_path=str(backup) if backup else None,
            report=report,
        )
        manifest.pop("last_error", None)
        _write_manifest(manifest)

    # A legitimate append changes row counts. Refresh the startup health guard
    # so it does not mistake the verified import for unexplained mutation.
    try:
        from app.services import health as health_service
        health_service.rebaseline_after_full_import(
            f"old_data_import source={manifest.get('filename')} run={token}"
        )
    except Exception:
        manifest["health_rebaseline_warning"] = True
        _write_manifest(manifest)
    return manifest
