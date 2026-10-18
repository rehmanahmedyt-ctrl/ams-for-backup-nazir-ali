"""Master-admin-only permanent client deletion ("delete cleanly").

Covers the invariant the feature exists for: when the built-in master Admin uses
the special power, the client and *every* transaction made for them are removed
with their effects cleanly reversed — cash/bank balances restored, stock
recomputed, FIFO/booked quantities released — leaving no voided rows, no
orphans and nothing for a ledger or report to pick up.  Any other user,
including a second admin, is refused and nothing is mutated.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.services.time_money import pk_today
from models import (
    db,
    Account,
    AccountingAuditLog,
    AccountTransaction,
    AuditLog,
    Booking,
    Client,
    DirectSale,
    DirectSaleDraft,
    Delivery,
    Entry,
    GRN,
    Invoice,
    Material,
    MaterialReturn,
    MaterialReturnItem,
    BookingItem,
    Payment,
    PendingBill,
    User,
    WaiveOff,
)
from tests.conftest import make_csrf_client

MASTER_USER = "Admin"
MASTER_PASSWORD = "Admin@fbm12345"
OTHER_PASSWORD = "Secret123"


def login(client, username=MASTER_USER, password=MASTER_PASSWORD):
    resp = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]


def grn_date():
    """GRNs are dated in the past so FIFO lots are consumable by sales today."""
    return (pk_today() - timedelta(days=5)).strftime("%Y-%m-%d")


def seed_material(client, app, name="CEMENT", grn_qty=1000, grn_rate=100):
    r = client.post("/add_material", data={
        "material_name": name, "material_unit": "Bags",
    }, follow_redirects=True)
    assert r.status_code == 200
    r = client.post("/grn", data={
        "action": "add", "supplier": "SUP1",
        "mat_name[]": name, "qty[]": str(grn_qty), "price[]": str(grn_rate),
        "paid_amount": "0", "date": grn_date(),
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        mat = Material.query.filter_by(name=name).first()
        assert mat is not None and float(mat.total or 0) >= grn_qty
        return mat.id


def seed_client(client, app, name="C1", code="CL-1"):
    r = client.post("/add_client", data={
        "name": name, "code": code, "category": "General", "opening_balance": "0",
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        row = Client.query.filter_by(code=code).first()
        assert row is not None
        return row.id


def seed_cash_account(client, app, name="CASH1", opening="100000"):
    r = client.post("/accounts/accounts/add", data={
        "name": name,
        "class_category": "Assets",
        "class_subcategory": "Cash",
        "class_account_type": "Main Cash",
        "account_status": "active",
        "opening_amount": opening,
        "opening_position": "debit",
        "opening_effective_date": "2026-01-01",
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        acc = Account.query.filter_by(name=name).first()
        assert acc is not None
        return acc.id


def add_credit_sale(client, app, client_name="C1", qty="10", rate="1000", paid="0"):
    r = client.post("/add_direct_sale", data={
        "client_name": client_name,
        "driver_name": "D",
        "category": "Credit Customer",
        "product_name[]": "CEMENT",
        "qty[]": qty,
        "unit_rate[]": rate,
        "payment_method": "Cash",
        "paid_amount": paid,
    }, follow_redirects=True)
    assert r.status_code == 200
    return r


def add_receipt(client, app, client_code="CL-1", amount="4000", account_id=None):
    r = client.post("/add_payment", data={
        "client_code": client_code,
        "amount": amount,
        "payment_type": "Receipt",
        "method": "Cash",
        "payment_account_id": str(account_id),
        "date": pk_today().strftime("%Y-%m-%d"),
    }, follow_redirects=True)
    assert r.status_code == 200
    return r


def make_second_admin(app, client, username="manager2"):
    """Create another account with role 'admin' but a different username.

    ``_user_can`` grants such an account every permission, so this is the sharp
    edge of the feature: full admin rights, yet *not* the master admin.
    """
    r = client.post("/add_user", data={
        "username": username, "password": OTHER_PASSWORD, "role": "user",
        "can_manage_clients": "on",
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        user = User.query.filter_by(username=username).one()
        user.role = "admin"
        db.session.commit()
        return user.id


def open_khata_id(app):
    with app.app_context():
        row = Client.query.filter(db.func.lower(Client.code) == "open-khata").first()
        assert row is not None, "bootstrap should seed the OPEN-KHATA system bucket"
        return row.id


# ---------------------------------------------------------------------------
# The happy path: master admin deletes a client cleanly
# ---------------------------------------------------------------------------
def test_master_admin_hard_delete_reverses_everything_cleanly(app, client):
    login(client)
    seed_material(client, app)
    seed_client(client, app, name="C1", code="CL-1")
    seed_client(client, app, name="C2", code="CL-2")
    account_id = seed_cash_account(client, app)

    # C1 buys on credit, then pays part of it into the cash account.
    add_credit_sale(client, app, client_name="C1", qty="10", rate="1000", paid="0")
    add_receipt(client, app, client_code="CL-1", amount="4000", account_id=account_id)
    # C2 keeps trading, so we can prove the purge is scoped to one client.
    add_credit_sale(client, app, client_name="C2", qty="5", rate="500", paid="0")

    with app.app_context():
        c1_id = Client.query.filter_by(code="CL-1").one().id
        assert DirectSale.query.filter_by(client_name="C1").count() == 1
        assert Payment.query.filter_by(client_name="C1").count() == 1
        assert PendingBill.query.filter(
            db.func.lower(PendingBill.client_name) == "c1").count() >= 1
        assert Entry.query.filter_by(client="C1").count() >= 1
        # The receipt landed in the cash account: 100000 opening + 4000.
        assert db.session.get(Account, account_id).balance == pytest.approx(104000.0)
        # 1000 received - 10 sold to C1 - 5 sold to C2.
        assert Material.query.filter_by(name="CEMENT").one().total == pytest.approx(985.0)
        tx_before = AccountTransaction.query.count()
        assert tx_before > 0

    resp = client.post(
        f"/hard_delete_client/{c1_id}",
        data={"confirm_name": "C1"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"cleanly reversed" in resp.data

    with app.app_context():
        # The client itself is gone.
        assert db.session.get(Client, c1_id) is None
        assert Client.query.filter_by(code="CL-1").count() == 0

        # No trace of C1 in any transactional table — including voided rows.
        assert DirectSale.query.filter(
            db.func.lower(DirectSale.client_name) == "c1").count() == 0
        assert Payment.query.filter(
            db.func.lower(Payment.client_name) == "c1").count() == 0
        assert PendingBill.query.filter(
            db.func.lower(PendingBill.client_name) == "c1").count() == 0
        assert WaiveOff.query.filter(
            db.func.lower(WaiveOff.client_name) == "c1").count() == 0
        assert Entry.query.filter(db.func.lower(Entry.client) == "c1").count() == 0
        assert MaterialReturn.query.filter(
            db.func.lower(MaterialReturn.client_name) == "c1").count() == 0
        assert Invoice.query.filter(db.func.lower(Invoice.client_name) == "c1").count() == 0
        assert Booking.query.filter(db.func.lower(Booking.client_name) == "c1").count() == 0
        assert Delivery.query.filter(db.func.lower(Delivery.client_name) == "c1").count() == 0
        assert DirectSaleDraft.query.filter(
            db.func.lower(DirectSaleDraft.client_name) == "c1").count() == 0

        # Money reversed: the cash account is back to its opening balance.
        assert db.session.get(Account, account_id).balance == pytest.approx(100000.0)

        # Stock reversed for C1's 10 bags, and only C1's: 1000 - 5 (C2) = 995.
        assert Material.query.filter_by(name="CEMENT").one().total == pytest.approx(995.0)

        # The account transactions behind C1's documents are deleted, not voided.
        assert AccountTransaction.query.filter(
            AccountTransaction.note.ilike("%[SRC:Payment:%")
        ).count() == 0
        assert AccountTransaction.query.filter(
            AccountTransaction.is_void == True  # noqa: E712 - no voided corpses left
        ).count() == 0
        assert AccountTransaction.query.count() < tx_before

        # C2 and the supply side are completely untouched.
        assert DirectSale.query.filter_by(client_name="C2").count() == 1
        assert Client.query.filter_by(code="CL-2").count() == 1
        assert GRN.query.count() == 1

    # The name and code are free again — the client can be re-added from scratch.
    r = client.post("/add_client", data={
        "name": "C1", "code": "CL-1", "category": "General", "opening_balance": "0",
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        fresh = Client.query.filter_by(code="CL-1").first()
        assert fresh is not None
        assert fresh.name == "C1"
        assert DirectSale.query.filter(
            db.func.lower(DirectSale.client_name) == "c1").count() == 0


def test_hard_delete_writes_audit_trail(app, client):
    """Business rows leave no trace, but the append-only audit log does."""
    login(client)
    seed_material(client, app)
    c1_id = seed_client(client, app, name="C1", code="CL-1")
    add_credit_sale(client, app, client_name="C1")

    resp = client.post(f"/hard_delete_client/{c1_id}",
                       data={"confirm_name": "C1"}, follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        rows = AuditLog.query.filter(AuditLog.action == "client.hard_delete").all()
        assert len(rows) == 1
        assert "C1" in (rows[0].details or "")
        assert rows[0].username == MASTER_USER

        # The structured accounting event commits atomically with the purge and
        # keeps the before-snapshot, so the deletion is reconstructable.
        acct_rows = AccountingAuditLog.query.filter(
            AccountingAuditLog.action == "HardDelete",
            AccountingAuditLog.entity_type == "Client",
            AccountingAuditLog.entity_id == c1_id,
        ).all()
        assert len(acct_rows) == 1
        assert acct_rows[0].module == "clients"
        assert acct_rows[0].username == MASTER_USER
        assert acct_rows[0].after_json is None
        assert '"code": "CL-1"' in (acct_rows[0].before_json or "").replace("\\", "") \
            or '"code":"CL-1"' in (acct_rows[0].before_json or "")
        assert acct_rows[0].party_before_id == c1_id



# ---------------------------------------------------------------------------
# The harder documents: bookings, material returns, waive-offs, legacy rows
# ---------------------------------------------------------------------------
def test_hard_delete_purges_bookings_returns_waiveoffs_and_legacy_rows(app, client):
    """Every document type a client can own is removed and cleanly reversed.

    Includes a legacy dispatch stock row that carries no ``source_table`` stamp
    and is therefore only reachable by matching the client's code/name — the
    class of row a naive purge silently leaves behind.
    """
    login(client)
    mat_id = seed_material(client, app)
    c1_id = seed_client(client, app, name="C1", code="CL-1")
    seed_client(client, app, name="C2", code="CL-2")
    account_id = seed_cash_account(client, app)
    today = pk_today().strftime("%Y-%m-%d")

    # Booking: 100 bags @ 1000, with 20000 advance paid into cash.
    r = client.post("/add_booking", data={
        "client_code": "CL-1",
        "material_name[]": "CEMENT",
        "material_id[]": str(mat_id),
        "qty[]": "100",
        "unit_rate[]": "1000",
        "amount": "100000",
        "paid_amount": "20000",
        "date": today,
        "payment_account_id": str(account_id),
        "payment_method": "Cash",
    }, follow_redirects=True)
    assert r.status_code == 200

    # Credit sale, then a receipt that carries a discount -> waive-off row.
    add_credit_sale(client, app, client_name="C1", qty="10", rate="1000", paid="0")
    r = client.post("/add_payment", data={
        "client_code": "CL-1",
        "amount": "3000",
        "discount": "500",
        "discount_reason": "goodwill",
        "payment_type": "Receipt",
        "method": "Cash",
        "payment_account_id": str(account_id),
        "date": today,
    }, follow_redirects=True)
    assert r.status_code == 200

    # Material return: 5 bags come back from C1.
    r = client.post("/add_material_return", data={
        "client_code": "CL-1",
        "material_name[]": "CEMENT",
        "material_id[]": str(mat_id),
        "qty[]": "5",
        "unit_rate[]": "1000",
        "date": today,
    }, follow_redirects=True)
    assert r.status_code == 200

    # Legacy dispatch row: attributed to the client only by name/code.
    r = client.post("/add_record", data={
        "date": today,
        "client": "C1",
        "type": "OUT",
        "material": "CEMENT",
        "material_id": str(mat_id),
        "qty": "7",
        "driver_name": "D",
    }, follow_redirects=True)
    assert r.status_code == 200

    # C2 keeps trading so the purge can be proven to stay scoped.
    add_credit_sale(client, app, client_name="C2", qty="5", rate="500", paid="0")

    with app.app_context():
        assert Booking.query.filter_by(client_name="C1").count() == 1
        assert MaterialReturn.query.filter_by(client_name="C1").count() == 1
        assert WaiveOff.query.filter(
            db.func.lower(WaiveOff.client_name) == "c1").count() >= 1
        assert Entry.query.filter_by(client="C1").count() >= 2
        cash_before = db.session.get(Account, account_id).balance
        stock_before = Material.query.filter_by(name="CEMENT").one().total

    resp = client.post(f"/hard_delete_client/{c1_id}",
                       data={"confirm_name": "C1"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"cleanly reversed" in resp.data

    with app.app_context():
        assert db.session.get(Client, c1_id) is None

        # Bookings and their lines are gone, not flagged.
        assert Booking.query.filter(
            db.func.lower(Booking.client_name) == "c1").count() == 0
        assert Booking.query.count() == 0
        assert db.session.query(BookingItem).count() == 0

        # Material returns and their lines are gone.
        assert MaterialReturn.query.filter(
            db.func.lower(MaterialReturn.client_name) == "c1").count() == 0
        assert db.session.query(MaterialReturnItem).count() == 0

        # Waive-offs, payments and pending bills are gone.
        assert WaiveOff.query.filter(
            db.func.lower(WaiveOff.client_name) == "c1").count() == 0
        assert Payment.query.filter(
            db.func.lower(Payment.client_name) == "c1").count() == 0
        assert PendingBill.query.filter(
            db.func.lower(PendingBill.client_name) == "c1").count() == 0

        # Every stock row attributed to C1 is gone, including the legacy one.
        assert Entry.query.filter(db.func.lower(Entry.client) == "c1").count() == 0
        assert Entry.query.filter(
            db.func.lower(Entry.client_code) == "cl-1").count() == 0

        # No voided corpses anywhere in the ledger tables.
        assert Entry.query.filter(Entry.is_void == True).count() == 0  # noqa: E712
        assert Payment.query.filter(Payment.is_void == True).count() == 0  # noqa: E712
        assert PendingBill.query.filter(PendingBill.is_void == True).count() == 0  # noqa: E712

        # Money fully reversed out of the cash account: the 20000 booking
        # advance and the 3000 receipt (its 500 discount was a waive-off, never
        # cash) both leave, returning the account to its 100000 opening.
        assert db.session.get(Account, account_id).balance == pytest.approx(100000.0)
        assert cash_before == pytest.approx(123000.0)

        # Stock recomputed from the surviving rows: 1000 received, less only the
        # 5 bags C2 still holds.  C1's 10 sold + 5 returned + 7 dispatched all
        # cancel out with the rows that created them.
        assert Material.query.filter_by(name="CEMENT").one().total == pytest.approx(995.0)
        assert stock_before == pytest.approx(983.0)

        # C2 and the supply side are untouched.
        assert DirectSale.query.filter_by(client_name="C2").count() == 1
        assert Entry.query.filter(db.func.lower(Entry.client) == "c2").count() >= 1
        assert Client.query.filter_by(code="CL-2").count() == 1
        assert GRN.query.count() == 1

# ---------------------------------------------------------------------------
# Permission: master admin only
# ---------------------------------------------------------------------------
def test_second_admin_cannot_hard_delete_client(app, client):
    """A different account with role 'admin' has every permission but not this one."""
    login(client)
    seed_material(client, app)
    c1_id = seed_client(client, app, name="C1", code="CL-1")
    account_id = seed_cash_account(client, app)
    add_credit_sale(client, app, client_name="C1")
    add_receipt(client, app, client_code="CL-1", amount="4000", account_id=account_id)
    make_second_admin(app, client, username="manager2")

    other = make_csrf_client(app)
    login(other, "manager2", OTHER_PASSWORD)

    resp = other.post(f"/hard_delete_client/{c1_id}",
                      data={"confirm_name": "C1"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Permission denied" in resp.data

    with app.app_context():
        # Nothing was mutated at all.
        assert db.session.get(Client, c1_id) is not None
        assert DirectSale.query.filter_by(client_name="C1").count() == 1
        assert Payment.query.filter_by(client_name="C1").count() == 1
        assert db.session.get(Account, account_id).balance == pytest.approx(104000.0)
        assert AuditLog.query.filter(
            AuditLog.action == "client.hard_delete.denied").count() == 1


def test_standard_user_cannot_hard_delete_client(app, client):
    login(client)
    seed_material(client, app)
    c1_id = seed_client(client, app, name="C1", code="CL-1")
    add_credit_sale(client, app, client_name="C1")

    r = client.post("/add_user", data={
        "username": "clerk", "password": OTHER_PASSWORD, "role": "user",
        "can_manage_clients": "on",
    }, follow_redirects=True)
    assert r.status_code == 200

    clerk = make_csrf_client(app)
    login(clerk, "clerk", OTHER_PASSWORD)
    resp = clerk.post(f"/hard_delete_client/{c1_id}",
                      data={"confirm_name": "C1"}, follow_redirects=True)
    assert resp.status_code in (200, 302)

    with app.app_context():
        assert db.session.get(Client, c1_id) is not None
        assert DirectSale.query.filter_by(client_name="C1").count() == 1


def test_anonymous_cannot_hard_delete_client(app, client):
    login(client)
    c1_id = seed_client(client, app, name="C1", code="CL-1")

    anon = make_csrf_client(app)
    resp = anon.post(f"/hard_delete_client/{c1_id}",
                     data={"confirm_name": "C1"}, follow_redirects=False)
    assert resp.status_code in (302, 401, 403)
    with app.app_context():
        assert db.session.get(Client, c1_id) is not None


def test_master_admin_button_only_rendered_for_master_admin(app, client):
    """The destructive control is invisible to everyone but the master admin."""
    login(client)
    seed_client(client, app, name="C1", code="CL-1")
    make_second_admin(app, client, username="manager2")

    master_html = client.get("/clients").get_data(as_text=True)
    assert 'id="hardDeleteClientModal' in master_html
    assert "Master admin power" in master_html
    assert "hard_delete_client/" in master_html

    other = make_csrf_client(app)
    login(other, "manager2", OTHER_PASSWORD)
    other_html = other.get("/clients").get_data(as_text=True)
    # Neither the dialog, the route, nor even its JavaScript is shipped to a
    # user who can never invoke it.
    assert 'id="hardDeleteClientModal' not in other_html
    assert "hard_delete_client/" not in other_html
    assert "hardDeletePreviewRows" not in other_html
    # A second admin still gets the non-destructive suspend action.
    assert "/delete_client/" in other_html


def test_master_admin_button_on_client_ledger_pages_only_for_master(app, client):
    """The same permanent-delete action must be reachable from a client's
    ledger (financial + material), not just the clients list — and must not
    be rendered at all for any other admin or user."""
    login(client)
    c1_id = seed_client(client, app, name="C1", code="CL-1")
    make_second_admin(app, client, username="manager2")

    for url in (f"/ledger/{c1_id}", f"/client_ledger/{c1_id}"):
        master_html = client.get(url).get_data(as_text=True)
        assert "Delete Client Permanently" in master_html, url
        assert 'id="hardDeleteClientModal' in master_html, url
        assert "hard_delete_client/" in master_html, url
        assert "hardDeletePreviewRows" in master_html, url

        other = make_csrf_client(app)
        login(other, "manager2", OTHER_PASSWORD)
        other_html = other.get(url).get_data(as_text=True)
        assert "Delete Client Permanently" not in other_html, url
        assert 'id="hardDeleteClientModal' not in other_html, url
        assert "hard_delete_client/" not in other_html, url
        assert "hardDeletePreviewRows" not in other_html, url


def test_master_admin_can_delete_client_from_ledger(app, client):
    """Submitting the ledger dialog (same route) purges the client."""
    login(client)
    c1_id = seed_client(client, app, name="C1", code="CL-1")
    assert "Delete Client Permanently" in client.get(f"/ledger/{c1_id}").get_data(as_text=True)

    resp = client.post(f"/hard_delete_client/{c1_id}",
                       data={"confirm_name": "C1"}, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(Client, c1_id) is None


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
def test_typed_confirmation_is_required(app, client):
    """A wrong or missing confirmation deletes nothing."""
    login(client)
    seed_material(client, app)
    c1_id = seed_client(client, app, name="C1", code="CL-1")
    add_credit_sale(client, app, client_name="C1")

    # Anything that is not the client's name must be refused — including the
    # code, an empty field, and a *different* client's name.
    for bad in ("", "c2", "delete", "CL-1", "C10", "C1 C1"):
        resp = client.post(f"/hard_delete_client/{c1_id}",
                           data={"confirm_name": bad}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Confirmation did not match" in resp.data
        with app.app_context():
            assert db.session.get(Client, c1_id) is not None, f"deleted with {bad!r}"
            assert DirectSale.query.filter_by(client_name="C1").count() == 1

    # Case and surrounding whitespace are forgiven; the name must still match.
    resp = client.post(f"/hard_delete_client/{c1_id}",
                       data={"confirm_name": "  c1 "}, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(Client, c1_id) is None


def test_open_khata_system_bucket_is_protected(app, client):
    """The shared walk-in bucket cannot be purged, even by the master admin."""
    login(client)
    khata_id = open_khata_id(app)

    resp = client.post(f"/hard_delete_client/{khata_id}",
                       data={"confirm_name": "OPEN KHATA"}, follow_redirects=True)
    assert resp.status_code == 200
    # The server-side refusal, not the dialog's own warning text.
    assert b"Cannot permanently delete" in resp.data
    assert b"walk-in sales" in resp.data
    assert b"Suspend it instead" in resp.data

    with app.app_context():
        assert db.session.get(Client, khata_id) is not None
        assert Client.query.filter(db.func.lower(Client.code) == "open-khata").count() == 1


def test_missing_client_is_handled(app, client):
    login(client)
    resp = client.post("/hard_delete_client/999999",
                       data={"confirm_name": "Nobody"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Client not found" in resp.data


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------
def test_preview_reports_blast_radius_and_is_master_only(app, client):
    login(client)
    seed_material(client, app)
    c1_id = seed_client(client, app, name="C1", code="CL-1")
    seed_client(client, app, name="C2", code="CL-2")
    account_id = seed_cash_account(client, app)
    add_credit_sale(client, app, client_name="C1", qty="10", rate="1000")
    add_receipt(client, app, client_code="CL-1", amount="4000", account_id=account_id)
    add_credit_sale(client, app, client_name="C2")
    make_second_admin(app, client, username="manager2")

    resp = client.get(f"/hard_delete_client/{c1_id}/preview")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["client_name"] == "C1"
    assert data["is_protected"] is False
    assert data["sales"] == 1
    assert data["payments"] == 1
    assert data["stock_entries"] >= 1
    assert data["pending_bills"] >= 1

    # Read-only: the preview itself must not change anything.
    with app.app_context():
        assert db.session.get(Client, c1_id) is not None
        assert DirectSale.query.filter_by(client_name="C1").count() == 1

    other = make_csrf_client(app)
    login(other, "manager2", OTHER_PASSWORD)
    assert other.get(f"/hard_delete_client/{c1_id}/preview").status_code == 403

    khata = open_khata_id(app)
    protected = client.get(f"/hard_delete_client/{khata}/preview").get_json()
    assert protected["is_protected"] is True


def test_preview_404_for_unknown_client(app, client):
    login(client)
    assert client.get("/hard_delete_client/999999/preview").status_code == 404
