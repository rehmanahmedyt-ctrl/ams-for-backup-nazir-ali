"""Tests for the alternate-booking excess repair tool.

Reproduces the pre-fix stored shape (excess due line on the BOOKED material) and
checks the controlled repair moves it to the delivered material without touching
the amount or the booking consumption.
"""
from __future__ import annotations

import pytest

from models import (
    Booking,
    BookingAllocation,
    BookingItem,
    Client,
    DirectSale,
    DirectSaleItem,
    Entry,
    Material,
    db,
)

ORIGINAL = "Capital Steel"
ALTERNATE = "ISM"
BOOKED_QTY = 756.0
DUE_QTY = 41.8
RATE = 100.0


@pytest.fixture()
def buggy_sale(app):
    with app.app_context():
        db.session.add_all([
            Material(code="CAP-ST", name=ORIGINAL, unit="Kg", total=10_000, unit_price=RATE),
            Material(code="ISM-01", name=ALTERNATE, unit="Kg", total=10_000, unit_price=RATE),
            Client(code="STEEL-1", name="Steel Client", category="General"),
        ])
        for mat_name in (ORIGINAL, ALTERNATE):
            db.session.add(Entry(
                date="2026-01-01", time="00:00:00", type="IN", material=mat_name,
                qty=10_000, note="Opening stock", nimbus_no="Opening Stock",
                client_category="Opening",
            ))
        booking = Booking(client_name="Steel Client", amount=BOOKED_QTY * RATE)
        db.session.add(booking)
        db.session.flush()
        booking_item = BookingItem(
            booking_id=booking.id, material_name=ORIGINAL, qty=BOOKED_QTY, price_at_time=RATE
        )
        db.session.add(booking_item)

        # ---- exactly what the old code wrote ----
        sale = DirectSale(
            client_name="Steel Client", client_code="STEEL-1",
            category="Mixed Transaction", amount=DUE_QTY * RATE,
            paid_amount=0, manual_bill_no="ST-BILL-9",
        )
        db.session.add(sale)
        db.session.flush()
        booked_line = DirectSaleItem(sale_id=sale.id, product_name=ALTERNATE, qty=BOOKED_QTY, price_at_time=0)
        due_line = DirectSaleItem(sale_id=sale.id, product_name=ORIGINAL, qty=DUE_QTY, price_at_time=RATE)
        db.session.add_all([booked_line, due_line])
        db.session.flush()
        db.session.add(BookingAllocation(
            sale_id=sale.id, sale_item_id=booked_line.id, booking_item_id=booking_item.id, qty=BOOKED_QTY
        ))
        db.session.add_all([
            Entry(date="2026-10-12", time="12:00:00", type="OUT", material=ALTERNATE,
                  booked_material=ORIGINAL, client="Steel Client", client_code="STEEL-1",
                  qty=BOOKED_QTY, bill_no="MB NO.ST-BILL-9", nimbus_no="Direct Sale",
                  client_category="Booking Delivery", source_module="sales",
                  source_table="direct_sale", source_id=sale.id),
            Entry(date="2026-10-12", time="12:00:00", type="OUT", material=ORIGINAL,
                  client="Steel Client", client_code="STEEL-1",
                  qty=DUE_QTY, bill_no="MB NO.ST-BILL-9", nimbus_no="Direct Sale",
                  client_category="Credit Customer", source_module="sales",
                  source_table="direct_sale", source_id=sale.id),
        ])
        db.session.commit()
        from app.services.void_rebuild import _rebuild_material_totals
        _rebuild_material_totals()
        db.session.commit()
        return sale.id


def _state(app):
    with app.app_context():
        sale = db.session.get(DirectSale, DirectSale.query.one().id)
        return {
            "sale_id": sale.id,
            "amount": float(sale.amount or 0),
            "items": [(i.product_name, float(i.qty), float(i.price_at_time)) for i in sale.items],
            "entries": [
                (e.material, e.booked_material, float(e.qty), e.client_category)
                for e in Entry.query.filter(Entry.type == "OUT").order_by(Entry.id).all()
            ],
            "totals": {m.name: float(m.total or 0) for m in Material.query.all()},
        }


def test_repair_moves_excess_to_delivered_material(app, buggy_sale):
    from tools.repair_controlled import repair_alternate_booking_excess as repair

    before = _state(app)
    assert before["totals"][ORIGINAL] == pytest.approx(10_000 - DUE_QTY)
    assert before["totals"][ALTERNATE] == pytest.approx(10_000 - BOOKED_QTY)

    with app.app_context():
        from models import DirectSale
        sale = db.session.get(DirectSale, buggy_sale)
        candidates = repair._candidates(sale)
        assert len(candidates) == 1
        assert candidates[0]["booked"] == ORIGINAL
        assert candidates[0]["delivered"] == ALTERNATE

        from app.services.void_rebuild import rebuild_direct_sale_effects
        candidates[0]["item"].product_name = ALTERNATE
        db.session.flush()
        rebuild_direct_sale_effects(sale, rebuild_stock=True)
        db.session.commit()

    after = _state(app)
    # Amount and quantities are untouched.
    assert after["amount"] == pytest.approx(before["amount"])
    # Both lines now leave as the delivered material.
    assert sorted((n, round(q, 3), round(r, 2)) for n, q, r in after["items"]) == sorted([
        (ALTERNATE, BOOKED_QTY, 0.0),
        (ALTERNATE, round(DUE_QTY, 3), RATE),
    ])
    assert after["totals"][ORIGINAL] == pytest.approx(10_000)
    assert after["totals"][ALTERNATE] == pytest.approx(10_000 - BOOKED_QTY - DUE_QTY)
    # Booking identity is preserved on the booked slice.
    assert (ALTERNATE, ORIGINAL, BOOKED_QTY, "Booking Delivery") in after["entries"]


def test_cli_dry_run_is_readonly_and_confirm_repairs_legacy_unallocated_sale(tmp_path, app_factory):
    """CLI verification:
    - Relative --database path works cleanly.
    - Dry-run leaves database file SHA-256 bit-for-bit unchanged.
    - Detects pre-BookingAllocation legacy sales via Entry.booked_material as well.
    - --sale-id <id> --confirm creates backup, populates DirectSaleItem columns,
      fixes stock totals, and leaves financial amount untouched.
    """
    import hashlib
    import subprocess
    import sys
    from pathlib import Path

    db_file = tmp_path / "legacy_case.db"
    test_app = app_factory(db_file=db_file)
    with test_app.app_context():
        db.session.add_all([
            Material(code="M12", name="ISM 12MM STEEL", unit="Kg", total=1000.0, unit_price=230.0),
            Material(code="M20", name="20MM STEEL", unit="Kg", total=1000.0, unit_price=250.0),
            Client(code="C-1106", name="Legacy Steel Client", category="General"),
            Entry(date="2026-01-01", time="00:00:00", type="IN", material="ISM 12MM STEEL",
                  qty=1000.0, nimbus_no="Opening Stock", client_category="Opening"),
            Entry(date="2026-01-01", time="00:00:00", type="IN", material="20MM STEEL",
                  qty=1000.0, nimbus_no="Opening Stock", client_category="Opening"),
        ])
        bk = Booking(client_name="Legacy Steel Client", amount=152.1 * 230.0)
        db.session.add(bk)
        db.session.flush()
        db.session.add(BookingItem(booking_id=bk.id, material_name="ISM 12MM STEEL", qty=152.1, price_at_time=230.0))
        sale = DirectSale(
            client_name="Legacy Steel Client",
            client_code="C-1106",
            category="Mixed Transaction",
            amount=115.0,
            paid_amount=0.0,
            manual_bill_no="MB NO.8877-2",
        )
        db.session.add(sale)
        db.session.flush()
        # No BookingAllocation rows (simulating pre-June-2026 sales #703 / #739 / #1106)
        db.session.add_all([
            DirectSaleItem(sale_id=sale.id, product_name="20MM STEEL", qty=152.1, price_at_time=0.0),
            DirectSaleItem(sale_id=sale.id, product_name="ISM 12MM STEEL", qty=0.5, price_at_time=230.0),
            Entry(date="2026-06-08", time="08:41:00", type="OUT", material="20MM STEEL",
                  booked_material="ISM 12MM STEEL", client="Legacy Steel Client", client_code="C-1106",
                  qty=152.1, bill_no="MB NO.8877-2", nimbus_no="Direct Sale",
                  client_category="Booking Delivery", is_alternate=True,
                  source_module="sales", source_table="direct_sale", source_id=sale.id),
            Entry(date="2026-06-08", time="08:41:00", type="OUT", material="ISM 12MM STEEL",
                  client="Legacy Steel Client", client_code="C-1106",
                  qty=0.5, bill_no="MB NO.8877-2", nimbus_no="Direct Sale",
                  client_category="Credit Customer", is_alternate=False,
                  source_module="sales", source_table="direct_sale", source_id=sale.id),
        ])
        db.session.commit()
        sale_id = sale.id
        db.engine.dispose()

    script = Path(__file__).resolve().parents[1] / "tools" / "repair_controlled" / "repair_alternate_booking_excess.py"
    sha_before = hashlib.sha256(db_file.read_bytes()).hexdigest()

    # Dry run from tmp_path with a relative --database argument
    dry = subprocess.run(
        [sys.executable, str(script), "--database", "legacy_case.db"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "DRY RUN" in dry.stdout
    assert "MB NO.8877-2" in dry.stdout
    assert hashlib.sha256(db_file.read_bytes()).hexdigest() == sha_before

    # Confirmed repair
    applied = subprocess.run(
        [sys.executable, str(script), "--database", "legacy_case.db", "--sale-id", str(sale_id), "--confirm"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "repaired sale" in applied.stdout
    assert list((tmp_path / "reconcile_backups").iterdir())

    with test_app.app_context():
        repaired_sale = db.session.get(DirectSale, sale_id)
        assert float(repaired_sale.amount) == pytest.approx(115.0)
        for it in repaired_sale.items:
            assert it.product_name == "20MM STEEL"
            assert it.booked_material == "ISM 12MM STEEL"
            assert it.is_alternate is True
        m12 = Material.query.filter_by(name="ISM 12MM STEEL").one()
        m20 = Material.query.filter_by(name="20MM STEEL").one()
        assert float(m12.total) == pytest.approx(1000.0)
        assert float(m20.total) == pytest.approx(1000.0 - 152.6)
        db.engine.dispose()

