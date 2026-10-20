"""Lazy direct-sale edit modal used by the paginated list."""
from ._common import *  # noqa


@bp.route('/direct_sales/<int:sale_id>/edit-modal')
@login_required
def direct_sale_edit_modal(sale_id):
    sale = DirectSale.query.options(
        selectinload(DirectSale.items),
        selectinload(DirectSale.invoice),
    ).filter_by(id=sale_id).first_or_404()
    materials = Material.query.filter_by(is_active=True).order_by(Material.name.asc()).all()
    clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    delivery_persons = DeliveryPerson.query.order_by(DeliveryPerson.name.asc()).all()
    grns = GRN.query.filter_by(is_void=False).options(
        selectinload(GRN.items)
    ).order_by(GRN.date_posted.desc()).limit(100).all()
    accounts = Account.query.filter(
        func.coalesce(Account.is_active, True) == True
    ).order_by(Account.name.asc()).all()

    delivery_allocations = SaleDeliveryPerson.query.options(
        selectinload(SaleDeliveryPerson.delivery_person)
    ).filter_by(sale_id=sale.id, is_void=False).all()
    rent_total = sum(float(row.rent_amount or 0) for row in delivery_allocations)
    if not delivery_allocations:
        rent_row = DeliveryRent.query.filter_by(
            sale_id=sale.id, is_void=False
        ).order_by(DeliveryRent.id.desc()).first()
        fallback_rent = float(getattr(sale, 'delivery_rent_cost', 0) or 0)
        if rent_row and rent_row.amount is not None:
            fallback_rent = float(rent_row.amount or 0)
        if (sale.driver_name or '').strip() or fallback_rent > 0:
            person = next((
                row for row in delivery_persons
                if (row.name or '').strip().casefold() == (sale.driver_name or '').strip().casefold()
            ), None)
            delivery_allocations = [{
                'delivery_person': person,
                'delivery_person_id': person.id if person else None,
                'delivery_person_name': (sale.driver_name or '').strip(),
                'bags_delivered': 0,
                'rent_amount': fallback_rent,
            }]
            rent_total = fallback_rent

    matched_client = next((
        row for row in clients
        if (row.name or '').strip().casefold() == (sale.client_name or '').strip().casefold()
    ), None)

    # The add form treats the Material column as the *booked* material and the
    # Alternate column as what was physically delivered.  The edit modal must
    # pre-fill in the same orientation or a no-op edit inverts the booking
    # (delivered material becomes the booked one, and the row is rejected with
    # "no booking balance").  BookingAllocation gives the exact per-line source;
    # the old entry scan is kept as a fallback for legacy lines.
    booked_by_sale_item = {}
    sale_item_ids = [it.id for it in (sale.items or []) if it.id]
    if sale_item_ids:
        alloc_rows = db.session.query(
            BookingAllocation.sale_item_id,
            BookingItem.material_name,
        ).join(
            BookingItem, BookingAllocation.booking_item_id == BookingItem.id
        ).filter(
            BookingAllocation.sale_item_id.in_(sale_item_ids),
            BookingAllocation.is_void == False,
        ).order_by(BookingAllocation.id.asc()).all()
        for sale_item_id, material_name in alloc_rows:
            name = (material_name or '').strip()
            if name and sale_item_id not in booked_by_sale_item:
                booked_by_sale_item[sale_item_id] = name

    refs = _direct_sale_bill_refs(sale)
    old_entries = Entry.query.filter(
        Entry.bill_no.in_(refs),
        Entry.nimbus_no == 'Direct Sale',
        Entry.is_void == False,
    ).all()
    alt_by_delivered = {}
    for e in old_entries:
        delivered = (e.material or '').strip()
        booked = (e.booked_material or '').strip()
        if delivered and booked and booked != delivered:
            alt_by_delivered[delivered] = booked

    # Legacy fallback: match a rate-0 line to the entry it produced.
    for it in (sale.items or []):
        if it.id in booked_by_sale_item:
            continue
        if float(it.price_at_time or 0) > 0:
            continue
        booked = alt_by_delivered.get((it.product_name or '').strip())
        if booked:
            booked_by_sale_item[it.id] = booked

    return render_template(
        '_direct_sale_edit_modal.html',
        sale=sale,
        materials=materials,
        clients=clients,
        delivery_persons=delivery_persons,
        grns=grns,
        accounts=accounts,
        delivery_allocations_by_sale={sale.id: delivery_allocations},
        delivery_rent_totals_by_sale={sale.id: rent_total},
        sale_client_code_by_id={sale.id: matched_client.code if matched_client else sale.client_code},
        sale_booked_material_by_item=booked_by_sale_item,
    )
