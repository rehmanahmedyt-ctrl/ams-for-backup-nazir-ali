#!/usr/bin/env python3
"""Repair direct sales whose alternate-booking EXCESS was posted on the booked material.

The bug
-------
A sale row entered as::

    Material  = 12MM STEEL CAPITAL (booked)
    Alternate = ISM 12MM STEEL      (physically delivered)
    Qty       = 797.8  (> booked 756)

was split into two lines::

    ISM            756.0  @ 0      -> booked dispatch        (correct)
    CAPITAL         41.8  @ rate   -> due/excess             (WRONG material)

so stock was deducted from the wrong item: the delivered material was left
short by the excess and the booked material went negative for stock it never
shipped.  New saves keep the excess on the delivered material; this tool fixes
rows that were already stored the old way.

Safety
------
* Default is a read-only DRY RUN that lists every candidate and the stock
  correction it would make.
* Writing requires an explicit ``--sale-id`` **and** ``--confirm``.
* ``preflight`` takes a database backup and writes to the repair audit log
  before any change.
* Only the sale's own item rows are relabelled; amount, quantities, rates,
  booking allocations and the client ledger are untouched.  The derived
  material/bill rows are then rebuilt with the app's normal rebuild path.

Usage
-----
Dry run::

    python tools/repair_controlled/repair_alternate_booking_excess.py \
        --database instance/ahmed_cement.db

Apply one sale (comma-separated ids allowed)::

    python tools/repair_controlled/repair_alternate_booking_excess.py \
        --database instance/ahmed_cement.db --sale-id 3422 --confirm
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--database",
        type=Path,
        default=REPO_ROOT / "instance" / "ahmed_cement.db",
        help="SQLite database to inspect/repair",
    )
    parser.add_argument(
        "--sale-id",
        default="",
        help="Comma-separated DirectSale ids to repair (required with --confirm)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Apply the fixes (takes a backup first)",
    )
    args = parser.parse_args()
    if args.confirm and not args.sale_id.strip():
        parser.error("--confirm requires --sale-id (one or more comma-separated sale ids)")
    if not args.database.exists():
        parser.error(f"database not found: {args.database}")
    return args


def _ids(raw: str) -> list[int]:
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            raise SystemExit(f"invalid sale id: {part!r}")
    return out


def _load_app(database: Path):
    """Bind the ORM to *database* without booting the web app.

    A bare Flask app is enough for the models + rebuild helpers and, unlike
    ``create_app()``, it cannot apply migrations, start the backup scheduler or
    touch any other runtime hook.  Dry runs are therefore truly read-only.
    """
    from flask import Flask
    from models import db

    flask_app = Flask(__name__)
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{database}"
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(flask_app)
    return flask_app


def _active_entries(sale):
    from models import Entry
    return Entry.query.filter(
        Entry.source_module == "sales",
        Entry.source_table == "direct_sale",
        Entry.source_id == sale.id,
        Entry.is_void == False,  # noqa: E712
    ).order_by(Entry.id.asc()).all()


def _alternate_map(sale):
    """booked material -> delivered material, from this sale's own booking lines."""
    from models import BookingAllocation, BookingItem
    mapping = {}
    rows = db_session().query(
        BookingAllocation.sale_item_id,
        BookingItem.material_name,
    ).join(
        BookingItem, BookingAllocation.booking_item_id == BookingItem.id
    ).filter(
        BookingAllocation.sale_id == sale.id,
        BookingAllocation.is_void == False,  # noqa: E712
    ).all()
    delivered_by_item = {}
    for e in _active_entries(sale):
        booked = (e.booked_material or "").strip()
        delivered = (e.material or "").strip()
        if booked and delivered and booked.lower() != delivered.lower():
            delivered_by_item.setdefault(booked.lower(), delivered)
    for sale_item_id, booked_name in rows:
        booked = (booked_name or "").strip()
        if not booked:
            continue
        delivered = delivered_by_item.get(booked.lower())
        if delivered:
            mapping[booked.lower()] = (booked, delivered, sale_item_id)
    return mapping


def db_session():
    from models import db
    return db.session


def _candidates(sale):
    """Due rows (rate > 0) that still carry the booked material instead of the alternate."""
    alt = _alternate_map(sale)
    out = []
    for item in sale.items or []:
        if float(item.price_at_time or 0) <= 0:
            continue
        name = (item.product_name or "").strip()
        hit = alt.get(name.lower())
        if not hit:
            continue
        booked, delivered, booking_sale_item_id = hit
        # The booking slice must be a DIFFERENT line of this sale; otherwise a
        # genuine extra sale of the booked material would be misread.
        if item.id == booking_sale_item_id:
            continue
        if name.lower() == delivered.lower():
            continue
        out.append({"item": item, "booked": booked, "delivered": delivered})
    return out


def _pretty(label, before, after, mats):
    print(f"  {label}")
    for name in mats:
        b = before.get(name, 0.0)
        a = after.get(name, 0.0)
        if abs(a - b) < 0.0001:
            continue
        print(f"    {name}: {b:,.3f} -> {a:,.3f}  ({a - b:+,.3f})")


def _describe(sale, candidates):
    bill = sale.manual_bill_no or sale.auto_bill_no or f"DS-{sale.id}"
    print(f"Sale #{sale.id}  bill {bill}  client {sale.client_name}  category {sale.category}")
    for c in candidates:
        item = c["item"]
        print(
            f"   due line #{item.id}: {item.qty:g} @ {item.price_at_time:g} "
            f"posted as {c['booked']!r} -> should be {c['delivered']!r}"
        )
    return {c["delivered"] for c in candidates} | {c["booked"] for c in candidates}


def main() -> int:
    args = _args()
    app = _load_app(args.database)

    from models import DirectSale, Material
    from app.services.void_rebuild import rebuild_direct_sale_effects

    with app.app_context():
        if args.sale_id.strip():
            sales = DirectSale.query.filter(DirectSale.id.in_(_ids(args.sale_id))).order_by(DirectSale.id).all()
        else:
            sales = DirectSale.query.filter(
                DirectSale.is_void == False,  # noqa: E712
                DirectSale.category == "Mixed Transaction",
            ).order_by(DirectSale.id).all()

        found = []
        for sale in sales:
            cands = _candidates(sale)
            if cands:
                found.append((sale, cands))

        if not found:
            print("No alternate-booking excess rows found. Nothing to repair.")
            return 0

        print(f"{'DRY RUN — no changes written.' if not args.confirm else 'REPAIR MODE'}")
        print(f"Candidate sales: {len(found)}\n")

        before = {m.name: float(m.total or 0) for m in Material.query.all()}
        for sale, cands in found:
            _describe(sale, cands)
        print()

        if not args.confirm:
            print("Re-run with --sale-id <id> --confirm to apply (backup is taken first).")
            return 0

        # Guarded write path: backup + audit log before the first mutation.
        from tools.repair_controlled.repair_guard import preflight
        preflight(
            script_name=__file__,
            description="Relabel alternate-booking excess rows and rebuild direct-sale effects",
            db_path=args.database,
            backup_dir=args.database.parent / "reconcile_backups",
        )

        touched = set()
        for sale, cands in found:
            for c in cands:
                c["item"].product_name = c["delivered"]
                touched.add(c["booked"])
                touched.add(c["delivered"])
            db_session().flush()
            rebuild_direct_sale_effects(sale, rebuild_stock=True)
        db_session().commit()

        after = {m.name: float(m.total or 0) for m in Material.query.all()}
        _pretty(f"Stock corrections applied to {len(found)} sale(s):", before, after, sorted(touched))
        for sale, cands in found:
            bill = sale.manual_bill_no or sale.auto_bill_no or f"DS-{sale.id}"
            print(f"  repaired sale #{sale.id} ({bill}) — {len(cands)} due line(s)")
        print("\nDone. Reopen the affected bills in the app to confirm.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
