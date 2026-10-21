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
    """Consistent supplied-material naming across list, edit sheet, and bill.

    Entered row: Material = booked (Capital Steel), Alternate = ISM,
    Qty = 797.8 dispatched, Rate = 100 for the excess.  756 is deducted from
    the booking; only the remaining 41.8 is charged as due — while the bill
    explicitly identifies ISM as the actual supplied material on both lines
    (756 against the Capital Steel booking + 41.8 additional = 797.8 total).
    """
    assert "Direct sale added successfully" in _post_sale(seeded).get_data(as_text=True)
    sale_id = _sale_state(app)["sale"]["id"]
    bill_no = "ST-BILL-1"
    expected_list_label = f"{ALTERNATE} (Alt against {ORIGINAL} booking)"

    # 1. Direct Sales list: one material line naming the supplied material first;
    #    qty shown as the due excess with clear dispatch breakdown.
    page = seeded.get("/direct_sales").get_data(as_text=True)
    assert page.count(expected_list_label) == 1
    assert f"{ORIGINAL} &gt; ALT &gt; {ALTERNATE}" not in page
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

    # 3. Bill: two accurate lines naming actual supplied material (ISM),
    #    totalling 797.8 supplied while charging ONLY the 41.8 excess.
    bill = seeded.get(f"/view_bill/{bill_no}").get_data(as_text=True)
    assert f"{ORIGINAL} &gt; ALT &gt; {ALTERNATE}" not in bill
    assert f"Alternative supplied against original {ORIGINAL} booking" in bill
    assert "Additional quantity supplied beyond booking" in bill
    assert f"Total {ALTERNATE} supplied" in bill
    nums = [c.strip() for c in re.findall(r'<td class="text-end[^"]*">\s*([^<]*?)\s*</td>', bill)]
    assert "756" in nums, nums
    assert "41.8" in nums, nums
    assert "797.8" in nums, nums
    assert f"{DUE_QTY * RATE:,.2f}".rstrip('0').rstrip('.') in bill
    assert f"{DELIVERED_QTY * RATE:,.2f}" not in bill

    # 4. Storage/ledger exact: explicit DirectSaleItem columns + stock/allocations.
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


def test_acceptance_12mm_booking_1100kg_20mm_alternative_with_open_khata_grn_void_edit(client, app):
    """Full end-to-end acceptance test:
    - GRN stock exists for both 12 mm steel and 20 mm steel (including multi-lot FIFO on 20 mm).
    - Open Khata credit customer books 1,000 kg of 12 mm steel @ 240 (with 100,000 advance paid).
    - Actual delivery supplies 1,100 kg of 20 mm steel via Alternative Material:
        * 1,000 kg against the 12 mm steel booking (Rs. 0 additional charge)
        * 100 kg additional quantity beyond the booking @ 260 (= Rs. 26,000)
    - Verifies bill lines, invoice print/download, client history receipt, stock balances,
      GRN allocations, booking status API, financial & material ledgers, void/unvoid stock
      restoration, and edit round-tripping.
    """
    login(client)
    logged_in_client = client
    with app.app_context():
        mat_12 = Material(code="STL-12MM", name="12 mm steel", unit="kg", total=0, unit_price=240.0)
        mat_20 = Material(code="STL-20MM", name="20 mm steel", unit="kg", total=0, unit_price=260.0)
        cust = Client(code="OK-STEEL-01", name="Open Khata Steel Customer", category="Open Khata")
        db.session.add_all([mat_12, mat_20, cust])
        db.session.flush()

        from models import GRN, GRNAllocation, GRNItem, Payment
        grn1 = GRN(supplier="Prime Mill", manual_bill_no="GRN-12-1", auto_bill_no="SB-GRN-2001")
        grn2 = GRN(supplier="Prime Mill", manual_bill_no="GRN-20-1", auto_bill_no="SB-GRN-2002")
        grn3 = GRN(supplier="Prime Mill", manual_bill_no="GRN-20-2", auto_bill_no="SB-GRN-2003")
        db.session.add_all([grn1, grn2, grn3])
        db.session.flush()
        gi_12 = GRNItem(grn_id=grn1.id, mat_name="12 mm steel", qty=5000.0, price_at_time=210.0)
        gi_20a = GRNItem(grn_id=grn2.id, mat_name="20 mm steel", qty=60.0, price_at_time=225.0)
        gi_20b = GRNItem(grn_id=grn3.id, mat_name="20 mm steel", qty=4940.0, price_at_time=230.0)
        db.session.add_all([gi_12, gi_20a, gi_20b])
        db.session.add_all([
            Entry(date="2026-10-01", time="09:00:00", type="IN", material="12 mm steel",
                  qty=5000.0, bill_no="GRN-12-1", nimbus_no="GRN", client_category="Supplier"),
            Entry(date="2026-10-01", time="09:05:00", type="IN", material="20 mm steel",
                  qty=60.0, bill_no="GRN-20-1", nimbus_no="GRN", client_category="Supplier"),
            Entry(date="2026-10-02", time="09:10:00", type="IN", material="20 mm steel",
                  qty=4940.0, bill_no="GRN-20-2", nimbus_no="GRN", client_category="Supplier"),
        ])
        from app.services.void_rebuild import _rebuild_material_totals
        db.session.flush()
        _rebuild_material_totals()

        booking = Booking(
            client_name="Open Khata Steel Customer",
            manual_bill_no="BK-12MM-1000",
            amount=240_000.0,
            paid_amount=100_000.0,
        )
        db.session.add(booking)
        db.session.flush()
        db.session.add(BookingItem(
            booking_id=booking.id,
            material_name="12 mm steel",
            qty=1000.0,
            price_at_time=240.0,
        ))
        db.session.add(Payment(
            client_id=cust.id,
            client_name=cust.name,
            amount=20_000.0,
            method="Cash",
            manual_bill_no="PAY-OK-01",
        ))
        db.session.commit()
        cust_id = cust.id
        mat_12_id = mat_12.id
        mat_20_id = mat_20.id
        gi_12_id = gi_12.id

    resp = logged_in_client.post("/add_direct_sale", data={
        "client_code": "OK-STEEL-01",
        "category": "Mixed Transaction",
        "driver_name": "Ustad Driver",
        "manual_bill_no": "BILL-ALT-1100",
        "paid_amount": "0",
        "discount": "0",
        "product_name[]": "12 mm steel",
        "alternate_material[]": "20 mm steel",
        "qty[]": "1100",
        "unit_rate[]": "260",
        "ignore_booking_item[]": "0",
    }, follow_redirects=True)
    assert "Direct sale added successfully" in resp.get_data(as_text=True)

    with app.app_context():
        from models import DirectSaleItem, GRN, GRNAllocation
        from app.services.ledgers import _build_client_ledger_rows, _render_client_history_receipt

        sale = DirectSale.query.order_by(DirectSale.id.desc()).first()
        sale_id = sale.id
        assert float(sale.amount) == pytest.approx(26_000.0)

        items = DirectSaleItem.query.filter_by(sale_id=sale.id).order_by(DirectSaleItem.id.asc()).all()
        booking_slices = [i for i in items if i.is_booking]
        excess_slices = [i for i in items if not i.is_booking]
        assert len(booking_slices) == 1
        assert booking_slices[0].product_name == "20 mm steel"
        assert booking_slices[0].material_id == mat_20_id
        assert booking_slices[0].booked_material == "12 mm steel"
        assert booking_slices[0].booked_material_id == mat_12_id
        assert booking_slices[0].is_alternate is True
        assert float(booking_slices[0].qty) == pytest.approx(1000.0)
        assert float(booking_slices[0].price_at_time) == pytest.approx(0.0)

        assert sum(float(i.qty) for i in excess_slices) == pytest.approx(100.0)
        for ex in excess_slices:
            assert ex.product_name == "20 mm steel"
            assert ex.material_id == mat_20_id
            assert ex.booked_material == "12 mm steel"
            assert ex.booked_material_id == mat_12_id
            assert ex.is_alternate is True
            assert float(ex.price_at_time) == pytest.approx(260.0)
            assert ex.grn_item_id != gi_12_id

        grn_allocs = GRNAllocation.query.filter_by(sale_id=sale.id, is_void=False).all()
        assert sum(float(a.qty) for a in grn_allocs) == pytest.approx(100.0)
        assert all(a.grn_item.mat_name == "20 mm steel" for a in grn_allocs)
        assert GRN.query.count() == 3

        assert float(db.session.get(Material, mat_20_id).total) == pytest.approx(3900.0)
        assert float(db.session.get(Material, mat_12_id).total) == pytest.approx(5000.0)

        out_entries = Entry.query.filter_by(
            source_table="direct_sale", source_id=sale.id, is_void=False
        ).order_by(Entry.id.asc()).all()
        assert all(e.material == "20 mm steel" for e in out_entries)
        assert sum(float(e.qty) for e in out_entries) == pytest.approx(1100.0)
        assert sum(float(e.qty) for e in out_entries if e.client_category == "Booking Delivery") == pytest.approx(1000.0)
        assert sum(float(e.qty) for e in out_entries if e.client_category != "Booking Delivery") == pytest.approx(100.0)

        _, _, total_debit, total_credit, total_balance, mat_hist = _build_client_ledger_rows(
            db.session.get(Client, cust_id)
        )
        assert total_debit == pytest.approx(266_000.0)
        assert total_credit == pytest.approx(120_000.0)
        assert total_balance == pytest.approx(146_000.0)
        assert mat_hist["12 mm steel"][-1]["balance"] == pytest.approx(0.0)

        with app.test_request_context():
            receipt_html = _render_client_history_receipt(db.session.get(Client, cust_id), "DirectSale", sale)
        assert "Alternative supplied against original 12 mm steel booking" in receipt_html
        assert "Additional quantity supplied beyond booking" in receipt_html
        assert "Total 20 mm steel supplied" in receipt_html

    for url in (
        "/view_bill/BILL-ALT-1100",
        f"/view_bill_detail/DirectSale/{sale_id}",
        "/download_invoice/BILL-ALT-1100?action=print",
    ):
        html = logged_in_client.get(url).get_data(as_text=True)
        items_section = html.split('<div class="rcpt-section-title">Items</div>', 1)[1].split("<tfoot>", 1)[0]
        assert items_section.count("20 mm steel") >= 3
        assert "Alternative supplied against original 12 mm steel booking" in items_section
        assert "Additional quantity supplied beyond booking" in items_section
        assert "Total 20 mm steel supplied" in items_section
        assert "12 mm steel &gt; ALT &gt; 20 mm steel" not in html
        row_cells = [
            re.sub(r"\s+", " ", m).strip()
            for m in re.findall(r"<tr[^>]*>(.*?)</tr>", items_section, re.S)
        ]
        assert any(
            "20 mm steel" in r
            and "Alternative supplied against original 12 mm steel booking" in r
            and ">1,000<" in r.replace(" ", "")
            for r in row_cells
        ), row_cells
        assert any(
            "20 mm steel" in r
            and "Additional quantity supplied beyond booking" in r
            and ">100<" in r.replace(" ", "")
            and ">26,000<" in r.replace(" ", "")
            for r in row_cells
        ), row_cells
        assert any(
            "Total 20 mm steel supplied" in r and ">1,100<" in r.replace(" ", "")
            for r in row_cells
        ), row_cells

    status_json = logged_in_client.get("/api/client_booking_status/OK-STEEL-01").get_json()
    mat_rows = {r["material"]: r for r in status_json}
    assert mat_rows["12 mm steel"]["booked"] == pytest.approx(1000.0)
    assert mat_rows["12 mm steel"]["delivered"] == pytest.approx(1000.0)
    assert mat_rows["12 mm steel"]["balance"] == pytest.approx(0.0)

    # Verify BOTH client ledger routes:
    # 1) Financial + Booking Material Ledger (/ledger/<id>)
    ledger_page = logged_in_client.get(f"/ledger/{cust_id}").get_data(as_text=True)
    assert "20 mm steel (Alt against 12 mm steel booking)" in ledger_page
    assert "146,000.00" in ledger_page
    # 2) Physical Movement Ledger (/client_ledger/<id>)
    movement_page = logged_in_client.get(f"/client_ledger/{cust_id}").get_data(as_text=True)
    assert "20 mm steel" in movement_page
    assert "ALT of 12 mm steel" in movement_page

    # Void & Unvoid must restore/re-apply physical stock on 20 mm steel, leaving 12 mm steel untouched
    with app.app_context():
        from app.services.void_rebuild import (
            _set_direct_sale_void_state,
            _unvoid_direct_sale_entries_and_apply_stock,
            _void_direct_sale_entries_and_restore_stock,
        )
        sale = db.session.get(DirectSale, sale_id)
        _void_direct_sale_entries_and_restore_stock(sale)
        db.session.flush()
        assert float(db.session.get(Material, mat_20_id).total) == pytest.approx(5000.0)
        assert float(db.session.get(Material, mat_12_id).total) == pytest.approx(5000.0)

        _unvoid_direct_sale_entries_and_apply_stock(sale)
        db.session.flush()
        assert float(db.session.get(Material, mat_20_id).total) == pytest.approx(3900.0)
        assert float(db.session.get(Material, mat_12_id).total) == pytest.approx(5000.0)

        _set_direct_sale_void_state(sale, True)
        db.session.commit()
        assert float(db.session.get(Material, mat_20_id).total) == pytest.approx(5000.0)
        assert float(db.session.get(Material, mat_12_id).total) == pytest.approx(5000.0)

        _set_direct_sale_void_state(sale, False)
        db.session.commit()
        assert float(db.session.get(Material, mat_20_id).total) == pytest.approx(3900.0)
        assert float(db.session.get(Material, mat_12_id).total) == pytest.approx(5000.0)

    # Edit round-trip: update excess from 100 kg -> 150 kg (1,150 kg total 20 mm steel)
    edit_resp = logged_in_client.post(f"/edit_bill/DirectSale/{sale_id}", data={
        "client_code": "OK-STEEL-01",
        "category": "Mixed Transaction",
        "driver_name": "Ustad Driver",
        "manual_bill_no": "BILL-ALT-1100",
        "paid_amount": "0",
        "discount": "0",
        "product_name[]": ["12 mm steel"],
        "alternate_material[]": ["20 mm steel"],
        "qty[]": ["150"],
        "booked_qty[]": ["1000"],
        "qty_is_due[]": ["1"],
        "unit_rate[]": ["260"],
        "ignore_booking_item[]": ["0"],
    }, follow_redirects=True)
    assert "Direct sale updated and resynced" in edit_resp.get_data(as_text=True)

    with app.app_context():
        assert float(db.session.get(Material, mat_20_id).total) == pytest.approx(3850.0)
        assert float(db.session.get(Material, mat_12_id).total) == pytest.approx(5000.0)
        assert float(db.session.get(DirectSale, sale_id).amount) == pytest.approx(39_000.0)


def test_partial_alternative_and_same_material_split_edge_cases(client, app):
    """Edge cases:
    1. Partial alternative fulfillment (600 kg of 20 mm bar against 1,000 kg 12 mm bar booking)
       leaves 400 kg remaining on 12 mm bar booking and deducts 600 kg from 20 mm bar.
    2. Remaining 400 kg fulfilled by same-material 12 mm bar + 50 kg excess = 450 kg total
       shows two 12 mm bar lines (400 Supplied against booking + 50 Additional = Total 450).
    """
    login(client)
    logged_in_client = client
    with app.app_context():
        m12 = Material(code="E-12", name="12 mm bar", unit="kg", total=2000.0, unit_price=240.0)
        m20 = Material(code="E-20", name="20 mm bar", unit="kg", total=2000.0, unit_price=260.0)
        c = Client(code="EDGE-01", name="Edge Case Client", category="General")
        db.session.add_all([m12, m20, c])
        db.session.add_all([
            Entry(date="2026-10-01", time="08:00:00", type="IN", material="12 mm bar",
                  qty=2000.0, nimbus_no="Opening Stock", client_category="Opening"),
            Entry(date="2026-10-01", time="08:00:00", type="IN", material="20 mm bar",
                  qty=2000.0, nimbus_no="Opening Stock", client_category="Opening"),
        ])
        bk = Booking(client_name="Edge Case Client", manual_bill_no="BK-EDGE-1", amount=240_000.0, paid_amount=240_000.0)
        db.session.add(bk)
        db.session.flush()
        db.session.add(BookingItem(booking_id=bk.id, material_name="12 mm bar", qty=1000.0, price_at_time=240.0))
        db.session.commit()
        m12_id, m20_id = m12.id, m20.id

    r1 = logged_in_client.post("/add_direct_sale", data={
        "client_code": "EDGE-01",
        "category": "Booking Delivery",
        "driver_name": "Edge Driver",
        "manual_bill_no": "BILL-PART-600",
        "paid_amount": "0",
        "discount": "0",
        "product_name[]": "12 mm bar",
        "alternate_material[]": "20 mm bar",
        "qty[]": "600",
        "unit_rate[]": "0",
        "ignore_booking_item[]": "0",
    }, follow_redirects=True)
    assert "Direct sale added successfully" in r1.get_data(as_text=True), _flash(r1.get_data(as_text=True))

    st = logged_in_client.get("/api/client_booking_status/EDGE-01").get_json()
    st_by_mat = {r["material"]: r for r in st}
    assert st_by_mat["12 mm bar"]["balance"] == pytest.approx(400.0)
    bill_part = logged_in_client.get("/view_bill/BILL-PART-600").get_data(as_text=True)
    assert "20 mm bar" in bill_part
    assert "Alternative supplied against original 12 mm bar booking" in bill_part

    r2 = logged_in_client.post("/add_direct_sale", data={
        "client_code": "EDGE-01",
        "category": "Mixed Transaction",
        "driver_name": "Edge Driver",
        "manual_bill_no": "BILL-SAME-450",
        "paid_amount": "0",
        "discount": "0",
        "product_name[]": "12 mm bar",
        "alternate_material[]": "",
        "qty[]": "450",
        "unit_rate[]": "240",
        "ignore_booking_item[]": "0",
    }, follow_redirects=True)
    assert "Direct sale added successfully" in r2.get_data(as_text=True), _flash(r2.get_data(as_text=True))

    bill_same = logged_in_client.get("/view_bill/BILL-SAME-450").get_data(as_text=True)
    assert "Supplied against booking" in bill_same
    assert "Additional quantity supplied beyond booking" in bill_same
    assert "Total 12 mm bar supplied" in bill_same

    with app.app_context():
        assert float(db.session.get(Material, m20_id).total) == pytest.approx(1400.0)
        assert float(db.session.get(Material, m12_id).total) == pytest.approx(1550.0)

