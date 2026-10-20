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
