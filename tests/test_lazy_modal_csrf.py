"""Lazily-loaded modal fragments must post with a valid CSRF token.

layout.html injects the session token into every mutating form at
DOMContentLoaded.  On-demand modals (Edit Client, Transfer Client, Edit Booking,
Edit Sale, pending-bill dialogs) are fetched *later* and swapped into a host
element, so that pass never sees them.  Those partials therefore have to carry
the token themselves, under the name the server actually reads.

The server reads ``_csrf_token``; the partials used to emit ``csrf_token``, so
every one of those dialogs answered 400 "Invalid or expired form token" and the
edit was silently impossible — most visibly as "a client's opening balance
cannot be edited".  A raw test client is used on purpose: the shared
``make_csrf_client`` fixture injects the correct field automatically and would
have hidden the bug.
"""
from __future__ import annotations

import re

import pytest

from models import db, Client, DirectSale, PendingBill
from tests.conftest import make_csrf_client

LOGIN = {"username": "Admin", "password": "Admin@fbm12345"}

MUTATING = ("POST", "PUT", "PATCH", "DELETE")


def _csrf_enforced_app(app_factory, tmp_path, name="csrf.db"):
    """An app that enforces CSRF even under TESTING, like production does."""
    return app_factory(tmp_path / name, AMS_CSRF_ALWAYS="1", AUTO_RECONCILE_ENABLED="0")


def _raw_login(app):
    """Return a plain test client with no automatic token injection."""
    raw = app.test_client()
    with raw.session_transaction() as sess:
        token = sess.get("_csrf_token")
    if not token:
        token = "session-token-under-test"
        with raw.session_transaction() as sess:
            sess["_csrf_token"] = token
    resp = raw.post("/login", data=dict(LOGIN, _csrf_token=token))
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]
    return raw, token


def _forms(html):
    """Yield (method, {fieldname: value}) for each form in a fragment."""
    out = []
    for chunk in re.findall(r"<form\b.*?</form>", html, re.S | re.I):
        method = (re.search(r'method=["\']?(\w+)', chunk, re.I) or [None, "GET"])[1].upper()
        fields = dict(re.findall(
            r'<input[^>]*\bname=["\']([^"\']+)["\'][^>]*\bvalue=["\']([^"\']*)["\']', chunk, re.I))
        # value-before-name ordering, as some partials are written
        for value, name in re.findall(
                r'<input[^>]*\bvalue=["\']([^"\']*)["\'][^>]*\bname=["\']([^"\']+)["\']', chunk, re.I):
            fields.setdefault(name, value)
        out.append((method, fields, chunk))
    return out


def _seed_client(app, code="CL-1", name="Editable Client"):
    with app.app_context():
        row = Client(code=code, name=name, category="General",
                     opening_balance=0, is_active=True)
        db.session.add(row)
        db.session.commit()
        return row.id


# ---------------------------------------------------------------------------
# Every lazy fragment carries the token under the name the server reads
# ---------------------------------------------------------------------------
def test_client_modal_fragment_uses_the_server_csrf_field_name(app_factory, tmp_path):
    app = _csrf_enforced_app(app_factory, tmp_path)
    raw, token = _raw_login(app)
    client_id = _seed_client(app)

    fragment = raw.get(f"/clients/{client_id}/modals").get_data(as_text=True)
    assert fragment.strip(), "modal fragment was empty"

    forms = _forms(fragment)
    assert forms, "fragment contained no forms"
    mutating = [f for f in forms if f[0] in MUTATING]
    assert mutating, "fragment contained no mutating forms"

    for method, fields, chunk in mutating:
        assert "csrf_token" not in fields, (
            f"fragment still posts the unread field name 'csrf_token'; "
            f"the server only accepts '_csrf_token'")
        assert "_csrf_token" in fields, f"{method} form in fragment has no CSRF token"
        assert fields["_csrf_token"] == token, "fragment token does not match the session token"

    # Both the edit dialog and the transfer dialog are covered.
    assert 'action="/edit_client/' in fragment
    assert 'action="/transfer_client/' in fragment


def test_layout_exposes_the_token_to_scripts(app_factory, tmp_path):
    """AMSLazyModal reads this meta tag to tokenise forms it injects later."""
    app = _csrf_enforced_app(app_factory, tmp_path)
    raw, token = _raw_login(app)
    _seed_client(app)

    page = raw.get("/clients").get_data(as_text=True)
    match = re.search(r'<meta name="csrf-token" content="([^"]*)"', page)
    assert match, "layout.html must expose <meta name=\"csrf-token\">"
    assert match.group(1) == token

    loader = raw.get("/static/lazy_modal_loader.js").get_data(as_text=True)
    assert "attachCsrfTokens" in loader
    assert 'meta[name="csrf-token"]' in loader


# ---------------------------------------------------------------------------
# The reported symptom: a client's opening balance can actually be saved
# ---------------------------------------------------------------------------
def test_edit_client_opening_balance_succeeds(app_factory, tmp_path):
    app = _csrf_enforced_app(app_factory, tmp_path)
    raw, token = _raw_login(app)
    client_id = _seed_client(app, name="Opening Balance Client")

    # Post exactly what the Edit Client dialog submits.
    resp = raw.post(f"/edit_client/{client_id}", data={
        "name": "Opening Balance Client",
        "code": "CL-1",
        "category": "General",
        "phone": "03001234567",
        "address": "Jalal Pur",
        "opening_balance": "77000",
        "opening_balance_date": "2026-05-05",
        "_csrf_token": token,
    })
    assert resp.status_code in (302, 303), (
        f"edit was rejected: {resp.status_code} {resp.get_data(as_text=True)[:200]}")

    with app.app_context():
        row = db.session.get(Client, client_id)
        assert row.opening_balance == pytest.approx(77000.0)
        assert row.opening_balance_date.strftime("%Y-%m-%d") == "2026-05-05"
        assert row.phone == "03001234567"


def test_stale_csrf_field_name_is_still_rejected(app_factory, tmp_path):
    """Guard the guard: the wrong field name must keep failing.

    If this ever passes, the server started accepting 'csrf_token' too and the
    regression above would no longer prove anything.
    """
    app = _csrf_enforced_app(app_factory, tmp_path)
    raw, token = _raw_login(app)
    client_id = _seed_client(app)

    resp = raw.post(f"/edit_client/{client_id}", data={
        "name": "Editable Client", "code": "CL-1", "category": "General",
        "opening_balance": "12345",
        "csrf_token": token,          # the old, unread field name
    })
    assert resp.status_code == 400
    assert b"Invalid or expired form token" in resp.data

    with app.app_context():
        assert db.session.get(Client, client_id).opening_balance != 12345


def test_dedicated_opening_balance_route_succeeds(app_factory, tmp_path):
    app = _csrf_enforced_app(app_factory, tmp_path)
    raw, token = _raw_login(app)
    client_id = _seed_client(app)

    resp = raw.post(f"/client_opening_balance/{client_id}", data={
        "opening_balance": "-4500",
        "opening_balance_date": "2026-06-01",
        "_csrf_token": token,
    })
    assert resp.status_code in (302, 303)

    with app.app_context():
        row = db.session.get(Client, client_id)
        assert row.opening_balance == pytest.approx(-4500.0)
        assert row.opening_balance_date.strftime("%Y-%m-%d") == "2026-06-01"


def test_client_ledger_opening_balance_form_embeds_csrf_token(app_factory, tmp_path):
    """The ledger's own modal must work even if page JavaScript is unavailable.

    The global layout injector is a useful fallback, but the opening-balance
    form is a financial mutation and must carry its token in the HTML it posts.
    Previously this form relied only on DOMContentLoaded JavaScript, so a
    blocked or stale script made the visible "Update Opening Balance" button
    fail with the server's 400 CSRF response.
    """
    app = _csrf_enforced_app(app_factory, tmp_path)
    raw, token = _raw_login(app)
    client_id = _seed_client(app, name="Ledger Opening Balance Client")

    page = raw.get(f"/ledger/{client_id}")
    assert page.status_code == 200
    forms = _forms(page.get_data(as_text=True))
    opening_forms = [
        fields for method, fields, chunk in forms
        if method == "POST" and f"/client_opening_balance/{client_id}" in chunk
    ]
    assert len(opening_forms) == 1, "client ledger did not render its opening-balance form"
    assert opening_forms[0].get("_csrf_token") == token

    resp = raw.post(f"/client_opening_balance/{client_id}", data={
        "opening_balance": "88000",
        "opening_balance_date": "2026-07-07",
        "_csrf_token": opening_forms[0]["_csrf_token"],
    })
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]

    with app.app_context():
        row = db.session.get(Client, client_id)
        assert row.opening_balance == pytest.approx(88000.0)
        assert row.opening_balance_date.strftime("%Y-%m-%d") == "2026-07-07"


# ---------------------------------------------------------------------------
# The other lazy partials share the same defect class
# ---------------------------------------------------------------------------
def test_other_lazy_fragments_also_carry_the_token(app_factory, tmp_path):
    app = _csrf_enforced_app(app_factory, tmp_path)
    raw, token = _raw_login(app)
    client_id = _seed_client(app)

    with app.app_context():
        sale = DirectSale(client_name="Editable Client", client_code="CL-1",
                          category="Credit Customer", amount=1000, paid_amount=0)
        db.session.add(sale)
        db.session.flush()
        sale_id = sale.id
        bill = PendingBill(client_code="CL-1", client_name="Editable Client",
                           bill_no="PB-TEST-1", amount=500, reason="test")
        db.session.add(bill)
        db.session.commit()
        bill_id = bill.id

    checked = 0
    for url in (f"/clients/{client_id}/modals",
                f"/direct_sales/{sale_id}/edit-modal",
                f"/pending_bills/{bill_id}/modals"):
        resp = raw.get(url)
        if resp.status_code != 200:
            continue
        fragment = resp.get_data(as_text=True)
        for method, fields, _chunk in _forms(fragment):
            if method not in MUTATING:
                continue
            checked += 1
            assert "csrf_token" not in fields, f"{url} still posts 'csrf_token'"
            assert fields.get("_csrf_token") == token, f"{url} form lacks the session token"
    assert checked >= 2, f"expected several mutating forms across fragments, found {checked}"
