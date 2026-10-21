"""Safety and UI tests for append-only Old Data Migration."""
from __future__ import annotations

import io
import sqlite3
from pathlib import Path

import pytest

from app.services.old_data_merge import (
    OldDataCompatibilityError,
    OldDataConflictError,
    append_old_data,
    target_signature,
)
from tests.conftest import make_csrf_client


DDL = """
CREATE TABLE user(id INTEGER PRIMARY KEY, username TEXT);
CREATE TABLE client(
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    opening_balance REAL DEFAULT 0
);
CREATE TABLE invoice(id INTEGER PRIMARY KEY, invoice_no TEXT, total_amount REAL DEFAULT 0);
CREATE TABLE direct_sale(
    id INTEGER PRIMARY KEY,
    client_name TEXT,
    client_code TEXT,
    amount REAL DEFAULT 0,
    paid_amount REAL DEFAULT 0,
    auto_bill_no TEXT UNIQUE,
    manual_bill_no TEXT,
    invoice_id INTEGER REFERENCES invoice(id),
    date_posted TEXT
);
CREATE TABLE direct_sale_item(
    id INTEGER PRIMARY KEY,
    sale_id INTEGER NOT NULL REFERENCES direct_sale(id),
    product_name TEXT,
    qty REAL DEFAULT 0
);
CREATE TABLE entry(
    id INTEGER PRIMARY KEY,
    client TEXT,
    client_code TEXT,
    material TEXT,
    qty REAL DEFAULT 0,
    auto_bill_no TEXT UNIQUE,
    bill_no TEXT,
    invoice_id INTEGER REFERENCES invoice(id),
    source_table TEXT,
    source_id INTEGER
);
"""


def _make_db(path: Path, *, old=False, required_extra=False):
    con = sqlite3.connect(str(path))
    con.executescript(DDL)
    if required_extra:
        con.execute("ALTER TABLE entry ADD COLUMN current_required TEXT NOT NULL DEFAULT 'CURRENT'")
    con.execute("INSERT INTO user(id,username) VALUES (1,'Admin')")
    if old:
        con.execute("INSERT INTO client(id,code,name,opening_balance) VALUES (1,'C-1','Old Client',25)")
        con.execute("INSERT INTO invoice(id,invoice_no,total_amount) VALUES (1,'SB-SL-1000',100)")
        con.execute(
            "INSERT INTO direct_sale(id,client_name,client_code,amount,paid_amount,auto_bill_no,manual_bill_no,invoice_id,date_posted) "
            "VALUES (1,'Old Client','C-1',100,20,'SB-SL-1000','MB NO.7',1,'2020-01-01 10:00:00')"
        )
        con.execute("INSERT INTO direct_sale_item(id,sale_id,product_name,qty) VALUES (1,1,'CEMENT',2)")
        con.execute(
            "INSERT INTO entry(id,client,client_code,material,qty,auto_bill_no,bill_no,invoice_id,source_table,source_id) "
            "VALUES (1,'Old Client','C-1','CEMENT',2,'SB-ST-1000','SB-SL-1000',1,'direct_sale',1)"
        )
    else:
        con.execute("INSERT INTO client(id,code,name,opening_balance) VALUES (1,'C-1','Current Client',50)")
    con.commit()
    con.close()


def _row(path, sql, args=()):
    con = sqlite3.connect(str(path))
    try:
        return con.execute(sql, args).fetchone()
    finally:
        con.close()


def test_append_remaps_complete_graph_and_never_changes_current_row(tmp_path):
    source, target = tmp_path / "old.db", tmp_path / "current.db"
    _make_db(source, old=True)
    _make_db(target, old=False)
    current_before = _row(target, "SELECT * FROM client WHERE id=1")

    report = append_old_data(source, target, imported_by="Admin")

    assert report["verification"] == "PASS"
    assert report["rows_inserted_total"] == 5
    assert report["existing_business_rows_modified"] == 0
    assert report["existing_business_rows_deleted"] == 0
    assert _row(target, "SELECT * FROM client WHERE id=1") == current_before

    imported_client = _row(target, "SELECT id,code,name FROM client WHERE id<>1")
    assert imported_client[0] != 1
    assert imported_client[1] != "C-1"  # collision namespaced
    sale = _row(target, "SELECT id,client_code,invoice_id,auto_bill_no FROM direct_sale")
    invoice = _row(target, "SELECT id,invoice_no FROM invoice")
    item = _row(target, "SELECT sale_id FROM direct_sale_item")
    entry = _row(target, "SELECT client_code,invoice_id,source_id,bill_no FROM entry")
    assert sale[1] == imported_client[1]
    assert sale[2] == invoice[0]
    assert sale[3].startswith("SB-O")
    assert item[0] == sale[0]
    assert entry[:3] == (imported_client[1], invoice[0], sale[0])
    assert entry[3] == sale[3]
    assert _row(target, "PRAGMA foreign_key_check") is None


def test_repeat_upload_is_noop_and_preserves_user_edits(tmp_path):
    source, target = tmp_path / "old.db", tmp_path / "current.db"
    _make_db(source, old=True)
    _make_db(target, old=False)
    first = append_old_data(source, target)
    imported_id = _row(target, "SELECT id FROM client WHERE id<>1")[0]
    con = sqlite3.connect(str(target))
    con.execute("UPDATE client SET name='User edited name' WHERE id=?", (imported_id,))
    con.commit()
    con.close()

    second = append_old_data(source, target)
    assert first["rows_inserted_total"] == 5
    assert second["rows_inserted_total"] == 0
    assert second["rows_already_imported"] == 5
    assert _row(target, "SELECT name FROM client WHERE id=?", (imported_id,))[0] == "User edited name"


def test_committed_run_replay_recovers_manifest_crash(tmp_path):
    source, target = tmp_path / "old.db", tmp_path / "current.db"
    _make_db(source, old=True)
    _make_db(target, old=False)
    analyzed = target_signature(target)
    run_id = "11111111-2222-3333-4444-555555555555"
    first = append_old_data(
        source, target, run_id=run_id, expected_target_signature=analyzed
    )
    # Simulates DB commit succeeding while the UI manifest write crashed. The
    # retry still carries the old analyzed signature and must return the
    # durable report rather than reject or duplicate.
    replay = append_old_data(
        source, target, run_id=run_id, expected_target_signature=analyzed
    )
    assert replay["run_id"] == first["run_id"]
    assert _row(target, "SELECT COUNT(*) FROM direct_sale")[0] == 1


def test_changed_source_row_is_refused_atomically(tmp_path):
    source, target = tmp_path / "old.db", tmp_path / "current.db"
    _make_db(source, old=True)
    _make_db(target, old=False)
    append_old_data(source, target)
    before = target_signature(target)
    con = sqlite3.connect(str(source))
    con.execute("UPDATE direct_sale SET amount=999 WHERE id=1")
    con.commit()
    con.close()

    with pytest.raises(OldDataConflictError, match="Previously imported source row changed"):
        append_old_data(source, target)
    assert target_signature(target) == before


def test_unknown_source_column_fails_closed(tmp_path):
    source, target = tmp_path / "old.db", tmp_path / "current.db"
    _make_db(source, old=True)
    _make_db(target, old=False)
    con = sqlite3.connect(str(source))
    con.execute("ALTER TABLE entry ADD COLUMN future_business_fact TEXT")
    con.commit()
    con.close()
    before = target_signature(target)

    with pytest.raises(OldDataCompatibilityError, match="current schema has no columns"):
        append_old_data(source, target)
    assert target_signature(target) == before


def test_stale_analysis_signature_refuses_without_writes(tmp_path):
    source, target = tmp_path / "old.db", tmp_path / "current.db"
    _make_db(source, old=True)
    _make_db(target, old=False)
    analyzed = target_signature(target)
    con = sqlite3.connect(str(target))
    con.execute("INSERT INTO client(id,code,name) VALUES (2,'NEW','Written after analysis')")
    con.commit()
    con.close()
    before = target_signature(target)

    with pytest.raises(OldDataConflictError, match="changed after analysis"):
        append_old_data(source, target, expected_target_signature=analyzed)
    assert target_signature(target) == before
    assert _row(target, "SELECT COUNT(*) FROM direct_sale")[0] == 0


def _minimal_app_source(path: Path):
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE user(id INTEGER PRIMARY KEY, username TEXT);
        CREATE TABLE client(id INTEGER PRIMARY KEY, code TEXT NOT NULL, name TEXT NOT NULL);
        CREATE TABLE entry(id INTEGER PRIMARY KEY, client TEXT, client_code TEXT, material TEXT, qty REAL);
        INSERT INTO user VALUES (1,'Old Admin');
        INSERT INTO client VALUES (1,'OLD-1','Imported UI Client');
        INSERT INTO entry VALUES (1,'Imported UI Client','OLD-1','CEMENT',3);
        """
    )
    con.commit()
    con.close()


def test_old_data_ui_analyzes_then_applies(app_factory, tmp_path):
    app = app_factory()
    app.config["OLD_DATA_MIGRATION_DIR"] = str(tmp_path / "old_data_runs")
    client = make_csrf_client(app)
    login = client.post(
        "/login",
        data={"username": "Admin", "password": "Admin@fbm12345"},
        follow_redirects=False,
    )
    assert login.status_code in (302, 303)
    source = tmp_path / "upload.db"
    _minimal_app_source(source)

    with source.open("rb") as handle:
        response = client.post(
            "/old-data-migration/analyze",
            data={"file": (io.BytesIO(handle.read()), "upload.db")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "Safety proof passed" in text
    assert "Ready to append" in text

    manifests = list((tmp_path / "old_data_runs").glob("*.json"))
    assert len(manifests) == 1
    token = manifests[0].stem
    response = client.post(
        f"/old-data-migration/run/{token}/apply",
        data={"confirm_append": "yes"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Import completed" in response.get_data(as_text=True)
    assert _row(Path(app.config["APP_DB_PATH"]), "SELECT COUNT(*) FROM client WHERE code='OLD-1'")[0] == 1


def test_legacy_url_redirects_to_new_name(app, client):
    login = client.post(
        "/login",
        data={"username": "Admin", "password": "Admin@fbm12345"},
        follow_redirects=False,
    )
    assert login.status_code in (302, 303)
    response = client.get("/legacy-migration/", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["Location"].endswith("/old-data-migration/")


def test_unsafe_full_db_append_ui_redirects_to_old_data_migration(app, client, tmp_path):
    login = client.post(
        "/login",
        data={"username": "Admin", "password": "Admin@fbm12345"},
        follow_redirects=False,
    )
    assert login.status_code in (302, 303)
    source = tmp_path / "old.db"
    _minimal_app_source(source)
    with source.open("rb") as handle:
        response = client.post(
            "/import_export/full_db_import",
            data={"file": (io.BytesIO(handle.read()), "old.db"), "mode": "append"},
            content_type="multipart/form-data",
            follow_redirects=False,
        )
    assert response.status_code in (302, 303)
    assert response.headers["Location"].endswith("/old-data-migration/")
