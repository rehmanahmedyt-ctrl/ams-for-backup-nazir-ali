"""Regression test: editing a client's opening balance must be audited.

Historically ``/edit_client`` wrote a structured ``AccountingAuditLog`` row but
the dedicated ``/client_opening_balance`` endpoint (the ledger's "Set Opening
Balance" dialog) saved the new balance with **no audit trail at all** — a silent
gap for a financial field.  These tests pin the fixed behaviour: every
opening-balance save, through either route, leaves a before/after audit event.
"""
from __future__ import annotations

import json

from models import db, AccountingAuditLog, Client
from tests.conftest import make_csrf_client


MASTER_USER = "Admin"
MASTER_PASSWORD = "Admin@fbm12345"


def login(client, username=MASTER_USER, password=MASTER_PASSWORD):
    resp = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]


def _create_client(client, app, name="test12345", code="TEST12345"):
    resp = client.post(
        "/add_client",
        data={
            "name": name,
            "code": code,
            "category": "General",
            "phone": "0300-0000000",
            "address": "Rawalpindi",
            "opening_balance": "0",
            "opening_balance_date": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]
    with app.app_context():
        row = Client.query.filter_by(name=name).first()
        assert row is not None
        return row.id


def _audit_rows(app, client_id):
    with app.app_context():
        rows = (
            AccountingAuditLog.query.filter_by(entity_type="Client", entity_id=client_id)
            .order_by(AccountingAuditLog.created_at)
            .all()
        )
        return [
            {
                "reason": r.reason,
                "action": r.action,
                "amount_before_minor": r.amount_before_minor,
                "amount_after_minor": r.amount_after_minor,
                "after": json.loads(r.after_json or "{}"),
                "before": json.loads(r.before_json or "{}"),
            }
            for r in rows
        ]


def test_client_opening_balance_endpoint_writes_audit(app):
    client = make_csrf_client(app)
    login(client)
    cid = _create_client(client, app)

    # No audit rows yet — the create path does not audit the opening balance.
    assert _audit_rows(app, cid) == []

    resp = client.post(
        f"/client_opening_balance/{cid}",
        data={"opening_balance": "25000", "opening_balance_date": "2026-10-01"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]

    # The balance is persisted...
    with app.app_context():
        c = db.session.get(Client, cid)
        assert abs(float(c.opening_balance) - 25000.0) < 0.005

    # ...and the change is now audited with exact before/after minor amounts.
    rows = _audit_rows(app, cid)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["action"] == "Edit"
    assert row["reason"] == "Client opening balance updated"
    assert row["amount_before_minor"] == 0
    assert row["amount_after_minor"] == 2_500_000  # Rs 25,000.00 in paisa
    assert row["before"]["opening_balance"] == 0
    assert abs(float(row["after"]["opening_balance"]) - 25000.0) < 0.005


def test_opening_balance_second_edit_audits_before_and_after(app):
    client = make_csrf_client(app)
    login(client)
    cid = _create_client(client, app)

    client.post(
        f"/client_opening_balance/{cid}",
        data={"opening_balance": "25000", "opening_balance_date": "2026-10-01"},
        follow_redirects=False,
    )
    # Edit it again — this time to a negative (advance/credit) value.
    resp = client.post(
        f"/client_opening_balance/{cid}",
        data={"opening_balance": "-5000.50", "opening_balance_date": "2026-10-05"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]

    rows = _audit_rows(app, cid)
    assert len(rows) == 2, rows
    second = rows[1]
    assert second["amount_before_minor"] == 2_500_000
    assert second["amount_after_minor"] == -500_050  # Rs -5,000.50 in paisa

    with app.app_context():
        c = db.session.get(Client, cid)
        assert abs(float(c.opening_balance) + 5000.5) < 0.005
        assert str(c.opening_balance_date).startswith("2026-10-05")


def test_edit_client_route_still_audits_opening_balance(app):
    """The pre-existing /edit_client audit path must keep working unchanged."""
    client = make_csrf_client(app)
    login(client)
    cid = _create_client(client, app)

    resp = client.post(
        f"/edit_client/{cid}",
        data={
            "name": "test12345",
            "code": "TEST12345",
            "category": "General",
            "phone": "0300-0000000",
            "address": "Rawalpindi",
            "opening_balance": "47500.75",
            "opening_balance_date": "2026-10-01",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]

    rows = _audit_rows(app, cid)
    assert len(rows) == 1, rows
    assert rows[0]["reason"] == "Client master updated"
    assert abs(float(rows[0]["after"]["opening_balance"]) - 47500.75) < 0.005
