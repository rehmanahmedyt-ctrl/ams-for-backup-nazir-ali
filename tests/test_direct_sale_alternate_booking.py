"""Booked item + alternate material + qty above the booking.

Business rule (from the field, bill "Capital Steel booked 756 / ISM delivered
797.8"):

* the client booked 756 kg of the ORIGINAL material,
* 797.8 kg of the ALTERNATE material physically left stock,
* the booking is consumed up to 756,
* the remaining 41.8 is a due (chargeable) sale **of the alternate material**,
* the original booked material must never go negative because of this.

These tests drive the real HTTP routes and recompute the expected state from
the ORM, so a regression fails here instead of corrupting live stock.
"""
from __future__ import annotations

import re

import pytest

from models import (
    Booking,
    BookingAllocation,
    BookingItem,
    Client,
    DirectSale,
    Entry,
    Material,
    db,
)


ORIGINAL = "Capital Steel"
ALTERNATE = "ISM"
BOOKED_QTY = 756.0
DELIVERED_QTY = 797.8
DUE_QTY = DELIVERED_QTY - BOOKED_QTY
RATE = 100.0


def login(client):
    resp = client.post(
        "/login",
        data={"username": "Admin", "password": "Admin@fbm12345"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)


@pytest.fixture()
def seeded(app, client):
    login(client)
    with app.app_context():
        db.session.add_all([
            Material(code="CAP-ST", name=ORIGINAL, unit="Kg", total=10_000, unit_price=RATE),
            Material(code="ISM-01", name=ALTERNATE, unit="Kg", total=10_000, unit_price=RATE),
            Client(code="STEEL-1", name="Steel Client", category="General"),
        ])
        # Stock totals are derived from active entry rows, so seed opening IN
        # movement for both materials.
        for mat_name in (ORIGINAL, ALTERNATE):
            db.session.add(Entry(
                date="2026-01-01", time="00:00:00", type="IN", material=mat_name,
                qty=10_000, note="Opening stock", nimbus_no="Opening Stock",
                client_category="Opening",
            ))
        db.session.commit()
        booking = Booking(client_name="Steel Client", amount=BOOKED_QTY * RATE)
        db.session.add(booking)
        db.session.flush()
        db.session.add(BookingItem(
            booking_id=booking.id, material_name=ORIGINAL, qty=BOOKED_QTY, price_at_time=RATE
        ))
        db.session.commit()
    return client


def _post_sale(client, **overrides):
    data = {
        "client_code": "STEEL-1",
        "category": "Mixed Transaction",
        "driver_name": "Driver One",
        "manual_bill_no": "ST-BILL-1",
        "paid_amount": "0",
        "discount": "0",
        "product_name[]": ORIGINAL,
        "qty[]": str(DELIVERED_QTY),
        "unit_rate[]": str(RATE),
        "alternate_material[]": ALTERNATE,
        "ignore_booking_item[]": "0",
    }
    data.update(overrides)
    return client.post("/add_direct_sale", data=data, follow_redirects=True)


def _flash(body: str) -> str:
    import re
    msgs = re.findall(r"alert[^>]*>(.{0,300}?)</div>", body, re.S)
    return " | ".join(re.sub(r"<[^>]+>|\s+", " ", m).strip() for m in msgs)[:400]


def _sale_state(app):
    with app.app_context():
        sale = DirectSale.query.order_by(DirectSale.id.desc()).first()
        entries = Entry.query.filter(
            Entry.is_void == False, Entry.type == "OUT"
        ).order_by(Entry.id).all()
        materials = {m.name: float(m.total or 0) for m in Material.query.all()}
        allocations = BookingAllocation.query.filter_by(is_void=False).all()
        return {
            "sale": {
                "id": sale.id,
                "category": sale.category,
                "amount": float(sale.amount or 0),
                "items": [
                    (i.product_name, float(i.qty or 0), float(i.price_at_time or 0))
                    for i in sale.items
                ],
            },
            "entries": [
                (e.type, e.material, e.booked_material, float(e.qty or 0), e.client_category)
                for e in entries
            ],
            "materials": materials,
            "allocations": [(a.sale_item_id, a.booking_item_id, float(a.qty or 0)) for a in allocations],
        }


def test_alternate_booking_with_excess_is_due_on_the_alternate(seeded, app):
    resp = _post_sale(seeded)
    body = resp.get_data(as_text=True)
    assert "Direct sale added successfully" in body, _flash(body)

    state = _sale_state(app)
    sale = state["sale"]

    # Amount is charged on the excess only.
    assert sale["amount"] == pytest.approx(DUE_QTY * RATE)

    # Two lines, BOTH on the delivered material: 756 booked (rate 0) + 41.8 due.
    assert [(name, round(qty, 3), round(rate, 2)) for name, qty, rate in sale["items"]] == [
        (ALTERNATE, BOOKED_QTY, 0.0),
        (ALTERNATE, round(DUE_QTY, 3), RATE),
    ]

    # Material movement: the alternate is fully deducted, the booked original
    # material is untouched (this is what used to go -41.8).
    assert state["materials"][ALTERNATE] == pytest.approx(10_000 - DELIVERED_QTY)
    assert state["materials"][ORIGINAL] == pytest.approx(10_000)

    assert [(t, mat, booked, round(qty, 3), cat) for t, mat, booked, qty, cat in state["entries"]] == [
        ("OUT", ALTERNATE, ORIGINAL, BOOKED_QTY, "Booking Delivery"),
        ("OUT", ALTERNATE, None, round(DUE_QTY, 3), "Credit Customer"),
    ]
    # No material row was ever posted against the original booked material.
    assert all(e[1] != ORIGINAL for e in state["entries"])

    # The booking itself is consumed exactly once, up to the booked quantity.
    assert len(state["allocations"]) == 1
    assert state["allocations"][0][2] == pytest.approx(BOOKED_QTY)


def test_edit_round_trip_keeps_booked_and_due_orientation(seeded, app):
    assert "Direct sale added successfully" in _post_sale(seeded).get_data(as_text=True)
    state = _sale_state(app)
    sale_id = state["sale"]["id"]

    modal = seeded.get(f"/direct_sales/{sale_id}/edit-modal")
    assert modal.status_code == 200
    html = modal.get_data(as_text=True)
    # Material column must be pre-filled with the BOOKED material and the
    # alternate column with what was delivered (not the other way round).
    assert f'value="{ORIGINAL}"' in html
    assert f'value="{ALTERNATE}"' in html
    assert 'Alternate material is only allowed' not in html

    resp = seeded.post(f"/edit_bill/DirectSale/{sale_id}", data={
        "client_code": "STEEL-1",
        "category": "Mixed Transaction",
        "driver_name": "Driver One",
        "manual_bill_no": "ST-BILL-1",
        "paid_amount": "0",
        "discount": "0",
        # Row 1: booked slice as the modal renders it.
        "product_name[]": [ORIGINAL, ALTERNATE],
        "qty[]": [str(BOOKED_QTY), str(DUE_QTY)],
        "unit_rate[]": ["0", str(RATE)],
        "alternate_material[]": [ALTERNATE, ""],
        "ignore_booking_item[]": ["0", "0"],
    }, follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "Direct sale updated and resynced" in body, _flash(body)

    after = _sale_state(app)
    assert after["materials"][ALTERNATE] == pytest.approx(10_000 - DELIVERED_QTY)
    assert after["materials"][ORIGINAL] == pytest.approx(10_000)
    assert after["sale"]["amount"] == pytest.approx(DUE_QTY * RATE)


def _qty_text(value: float) -> str:
    """The way the app prints quantities (no float dust, no trailing zeros)."""
    num = round(float(value), 6)
    if abs(num - round(num)) <= 1e-9:
        return str(int(round(num)))
    return f"{num:.6f}".rstrip("0").rstrip(".")


def _row(html: str, label: str) -> str:
    """Whitespace-collapsed <td> that carries *label* (for display assertions)."""
    for cell in re.findall(r"<td[^>]*>.*?</td>", html, re.S):
        if label in re.sub(r"\s+", " ", cell):
            return re.sub(r"\s+", " ", cell)
    return ""


def test_one_row_everywhere_the_user_looks(seeded, app):
    """One row everywhere — and only the EXCESS over the booking is due.

    Entered row: Material = booked (Capital Steel), Alternate = ISM,
    Qty = 797.8 dispatched, Rate = 100 for the excess.  756 is deducted from
    the booking; only the remaining 41.8 may ever show up (or be charged) as
    due — never the full 797.8.
    """
    assert "Direct sale added successfully" in _post_sale(seeded).get_data(as_text=True)
    sale_id = _sale_state(app)["sale"]["id"]
    bill_no = "ST-BILL-1"
    expected_label = f"{ORIGINAL} &gt; ALT &gt; {ALTERNATE}"  # Jinja autoescaping

    # 1. Direct Sales list: one material line; qty shown as the due excess.
    page = seeded.get("/direct_sales").get_data(as_text=True)
    assert page.count(expected_label) == 1
    qty_cell = _row(page, 'data-label="Qty"')
    assert "41.8" in qty_cell
    assert "756 booked of 797.8 dispatched" in qty_cell
    assert "797.8" not in qty_cell.split("booked of")[0]

    # 2. Edit sheet: one row, qty = the due excess, booked qty kept separately.
    modal = seeded.get(f"/direct_sales/{sale_id}/edit-modal").get_data(as_text=True)
    assert modal.count('class="compact-item-row') == 1
    assert f'value="{ORIGINAL}"' in modal
    assert f'value="{ALTERNATE}"' in modal
    assert f'name="qty[]" value="{_qty_text(DUE_QTY)}"' in modal
    assert f'name="booked_qty[]" value="{_qty_text(BOOKED_QTY)}"' in modal
    assert f'value="{RATE}"' in modal
    assert "756 of 797.8 already covered by booking" in modal

    # 3. Bill: one line charging the excess only (41.8 x rate), with the
    #    booked part named in the line note.
    bill = seeded.get(f"/view_bill/{bill_no}").get_data(as_text=True)
    assert bill.count(expected_label) == 1
    bill_cell = _row(bill, expected_label)
    assert "756 booked" in bill_cell and "797.8 dispatched" in bill_cell
    nums = [c.strip() for c in re.findall(r'<td class="text-end">\s*([^<]*?)\s*</td>', bill)]
    assert "41.8" in nums, nums
    assert "797.8" not in nums, nums
    assert f"{DUE_QTY * RATE:,.2f}".rstrip('0').rstrip('.') in bill
    assert f"{DELIVERED_QTY * RATE:,.2f}" not in bill

    # 4. Storage/ledger unchanged: still exact after the display collapse.
    state = _sale_state(app)
    assert state["sale"]["amount"] == pytest.approx(DUE_QTY * RATE)
    assert state["materials"][ALTERNATE] == pytest.approx(10_000 - DELIVERED_QTY)
    assert state["materials"][ORIGINAL] == pytest.approx(10_000)
    assert len(state["allocations"]) == 1
    assert state["allocations"][0][2] == pytest.approx(BOOKED_QTY)


def test_edit_sheet_save_keeps_booking_deduction_and_charges_excess_only(seeded, app):
    """Saving the edit sheet must not turn the booked qty into due.

    The sheet submits qty = 41.8 (due) + booked_qty = 756 (booking).  The
    booking must still be consumed by exactly 756, the full 797.8 must still
    leave stock, and only 41.8 may be charged — even when the client's
    entry-derived booking balance looks consumed by another dispatch.
    """
    assert "Direct sale added successfully" in _post_sale(seeded).get_data(as_text=True)
    sale_id = _sale_state(app)["sale"]["id"]

    def _save(**overrides):
        data = {
            "client_code": "STEEL-1",
            "category": "Mixed Transaction",
            "driver_name": "Driver One",
            "manual_bill_no": "ST-BILL-1",
            "paid_amount": "0",
            "discount": "0",
            # exactly what the one-row edit sheet submits
            "product_name[]": [ORIGINAL],
            "alternate_material[]": [ALTERNATE],
            "qty[]": [_qty_text(DUE_QTY)],
            "booked_qty[]": [_qty_text(BOOKED_QTY)],
            "qty_is_due[]": ["1"],
            "unit_rate[]": [str(RATE)],
            "ignore_booking_item[]": ["0"],
        }
        data.update(overrides)
        resp = seeded.post(f"/edit_bill/DirectSale/{sale_id}", data=data, follow_redirects=True)
        assert "Direct sale updated and resynced" in resp.get_data(as_text=True), _flash(resp.get_data(as_text=True))

    _save()
    state = _sale_state(app)
    assert state["sale"]["amount"] == pytest.approx(DUE_QTY * RATE)
    assert [(n, round(q, 3), round(r, 2)) for n, q, r in state["sale"]["items"]] == [
        (ALTERNATE, BOOKED_QTY, 0.0),
        (ALTERNATE, round(DUE_QTY, 3), RATE),
    ]
    assert len(state["allocations"]) == 1
    assert state["allocations"][0][2] == pytest.approx(BOOKED_QTY)
    assert state["materials"][ALTERNATE] == pytest.approx(10_000 - DELIVERED_QTY)
    assert state["materials"][ORIGINAL] == pytest.approx(10_000)

    # The sheet still shows one row with the excess as the qty.
    modal = seeded.get(f"/direct_sales/{sale_id}/edit-modal").get_data(as_text=True)
    assert modal.count('class="compact-item-row') == 1
    assert f'name="qty[]" value="{_qty_text(DUE_QTY)}"' in modal
    assert f'name="booked_qty[]" value="{_qty_text(BOOKED_QTY)}"' in modal


def test_booking_consumed_elsewhere_does_not_make_booked_qty_due(seeded, app):
    """A second dispatch of the same booked material must not re-charge row 1.

    The entry-derived booking balance drops to 0 once another delivery consumes
    the pool; the sale's own booked qty stays booked (756 deducted from the
    booking, 41.8 due) instead of the whole 797.8 turning into due.
    """
    assert "Direct sale added successfully" in _post_sale(seeded).get_data(as_text=True)
    sale_id = _sale_state(app)["sale"]["id"]

    # Another booking delivery consumes the whole remaining booking pool.
    with app.app_context():
        from models import Booking, BookingItem, db
        second = Booking(client_name="Steel Client", amount=BOOKED_QTY * RATE)
        db.session.add(second)
        db.session.flush()
        db.session.add(BookingItem(
            booking_id=second.id, material_name=ORIGINAL, qty=BOOKED_QTY, price_at_time=RATE
        ))
        db.session.commit()

    resp = seeded.post("/edit_bill/DirectSale/%d" % sale_id, data={
        "client_code": "STEEL-1",
        "category": "Mixed Transaction",
        "driver_name": "Driver One",
        "manual_bill_no": "ST-BILL-1",
        "paid_amount": "0",
        "discount": "0",
        "product_name[]": [ORIGINAL],
        "alternate_material[]": [ALTERNATE],
        "qty[]": [_qty_text(DUE_QTY)],
        "booked_qty[]": [_qty_text(BOOKED_QTY)],
        "qty_is_due[]": ["1"],
        "unit_rate[]": [str(RATE)],
        "ignore_booking_item[]": ["0"],
    }, follow_redirects=True)
    assert "Direct sale updated and resynced" in resp.get_data(as_text=True), _flash(resp.get_data(as_text=True))

    state = _sale_state(app)
    assert state["sale"]["amount"] == pytest.approx(DUE_QTY * RATE)
    assert [(n, round(q, 3), round(r, 2)) for n, q, r in state["sale"]["items"]] == [
        (ALTERNATE, BOOKED_QTY, 0.0),
        (ALTERNATE, round(DUE_QTY, 3), RATE),
    ]
    assert len(state["allocations"]) == 1
    assert state["allocations"][0][2] == pytest.approx(BOOKED_QTY)


def test_fully_booked_row_on_the_edit_sheet_is_not_doubled(seeded, app):
    """A row fully covered by the booking posts due qty 0 + booked qty.

    The sheet must not read that as 0 + 756 + 756 of dispatch, and none of it
    may become due.
    """
    resp = _post_sale(seeded, **{
        "qty[]": str(BOOKED_QTY),
        "unit_rate[]": "0",
        "category": "Booking Delivery",
        "manual_bill_no": "ST-BILL-2",
    })
    assert "Direct sale added successfully" in resp.get_data(as_text=True), _flash(resp.get_data(as_text=True))
    sale_id = _sale_state(app)["sale"]["id"]

    modal = seeded.get(f"/direct_sales/{sale_id}/edit-modal").get_data(as_text=True)
    assert modal.count('class="compact-item-row') == 1
    assert f'name="qty[]" value="0"' in modal
    assert f'name="booked_qty[]" value="{_qty_text(BOOKED_QTY)}" in modal'

    resp = seeded.post(f"/edit_bill/DirectSale/{sale_id}", data={
        "client_code": "STEEL-1",
        "category": "Booking Delivery",
        "driver_name": "Driver One",
        "manual_bill_no": "ST-BILL-2",
        "paid_amount": "0",
        "discount": "0",
        "product_name[]": [ORIGINAL],
        "alternate_material[]": [ALTERNATE],
        "qty[]": ["0"],
        "booked_qty[]": [_qty_text(BOOKED_QTY)],
        "qty_is_due[]": ["1"],
        "unit_rate[]": ["0"],
        "ignore_booking_item[]": ["0"],
    }, follow_redirects=True)
    assert "Direct sale updated and resynced" in resp.get_data(as_text=True), _flash(resp.get_data(as_text=True))

    state = _sale_state(app)
    assert state["sale"]["amount"] == pytest.approx(0)
    assert [(n, round(q, 3), round(r, 2)) for n, q, r in state["sale"]["items"]] == [
        (ALTERNATE, BOOKED_QTY, 0.0),
    ]
    assert len(state["allocations"]) == 1
    assert state["allocations"][0][2] == pytest.approx(BOOKED_QTY)
    assert state["materials"][ALTERNATE] == pytest.approx(10_000 - BOOKED_QTY)
    assert state["materials"][ORIGINAL] == pytest.approx(10_000)


def test_edit_from_the_single_row_does_not_multiply_rows(seeded, app):
    """Re-saving the one visible row must not add another row or re-charge."""
    assert "Direct sale added successfully" in _post_sale(seeded).get_data(as_text=True)
    sale_id = _sale_state(app)["sale"]["id"]

    resp = seeded.post(f"/edit_bill/DirectSale/{sale_id}", data={
        "client_code": "STEEL-1",
        "category": "Mixed Transaction",
        "driver_name": "Driver One",
        "manual_bill_no": "ST-BILL-1",
        "paid_amount": "0",
        "discount": "0",
        # Exactly what the one-row edit sheet submits.
        "product_name[]": [ORIGINAL],
        "qty[]": [str(DELIVERED_QTY)],
        "unit_rate[]": [str(RATE)],
        "alternate_material[]": [ALTERNATE],
        "ignore_booking_item[]": ["0"],
    }, follow_redirects=True)
    assert "Direct sale updated and resynced" in resp.get_data(as_text=True)

    state = _sale_state(app)
    assert state["sale"]["amount"] == pytest.approx(DUE_QTY * RATE)
    assert [(n, round(q, 3), round(r, 2)) for n, q, r in state["sale"]["items"]] == [
        (ALTERNATE, BOOKED_QTY, 0.0),
        (ALTERNATE, round(DUE_QTY, 3), RATE),
    ]
    assert len(state["allocations"]) == 1

    modal = seeded.get(f"/direct_sales/{sale_id}/edit-modal").get_data(as_text=True)
    assert modal.count('class="compact-item-row') == 1
