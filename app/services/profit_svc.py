"""Profit/Loss calculation service — exact replica of profit_reports logic for dashboard KPI.

This module provides a single authoritative profit calculation used by both:
  * Profit Reports page (detailed breakdown)
  * Dashboard KPI card (exact current profit/loss amount)

The calculation mirrors _profit_profit_reports.py verbatim to ensure the dashboard
KPI shows the identical "exact amount" as the Profit Reports page for the same
date range.
"""
from __future__ import annotations

from datetime import datetime, date, timedelta
import logging
import re

from sqlalchemy import func, or_, and_, exists
from sqlalchemy.orm import selectinload

from models import db, Client, Material, GRN, GRNItem, Booking, BookingItem, DirectSale, DirectSaleItem, Payment, WaiveOff, AccountTransaction, Entry, SupplierPayment, DeliveryPersonPayment, SaleDeliveryPerson, DeliveryRent, DeliveryPerson

from app.services.api import (
    _norm_text,
    _material_norm_key,
    _is_rent_material_name,
    normalize_sale_category,
    _direct_sale_default_bill_ref,
    _direct_sale_bill_refs,
    calculate_grn_total,
    pk_now,
    pk_today,
)


logger = logging.getLogger(__name__)


def _safe_parse_date(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except Exception:
        return None


def _safe_pct(num, den):
    if not den:
        return 0.0
    return (float(num or 0) / float(den or 0)) * 100.0


def _bill_ref_variants(ref_value):
    ref = (ref_value or '').strip()
    if not ref:
        return set()
    variants = {ref}
    if ref.startswith('#') and len(ref) > 1:
        variants.add(ref[1:])
    elif ref.isdigit():
        variants.add(f"#{ref}")
    return {v.strip().lower() for v in variants if v}


def _add_waive_to_map(target_map, bill_ref, amount):
    amt = float(amount or 0)
    if amt <= 0:
        return
    variants = _bill_ref_variants(bill_ref)
    if not variants:
        return
    for k in variants:
        target_map[k] = target_map.get(k, 0.0) + amt


def compute_profit_summary(
    start_date_str: str | None = None,
    end_date_str: str | None = None,
    material_query: str | None = None,
    client_query: str | None = None,
    grn_id_raw: str | None = None,
) -> dict:
    """
    Compute exact profit/loss summary for the given date range.

    Mirrors profit_reports() logic exactly. Returns dict with:
      total_revenue, total_discount_loss, total_cogs, total_profit,
      known_cost_revenue, unknown_cost_revenue, unknown_cost_rows,
      profit_rows, loss_rows, margin_pct, markup_pct,
      transactions (list), plus auxiliary breakdowns.

    Dates are strings 'YYYY-MM-DD'. If None, defaults to current month
    (month start to today) — same as profit_reports default.
    """
    today_str = pk_today().strftime('%Y-%m-%d')
    month_start_str = pk_today().replace(day=1).strftime('%Y-%m-%d')

    start_date = (start_date_str or '').strip() if start_date_str else ''
    end_date = (end_date_str or '').strip() if end_date_str else ''
    material_query = (material_query or '').strip()
    client_query = (client_query or '').strip()
    grn_id_raw = (grn_id_raw or '').strip()

    if not start_date:
        start_date = month_start_str
    if not end_date:
        end_date = today_str

    start_dt = _safe_parse_date(start_date)
    end_dt = _safe_parse_date(end_date)

    if not start_dt or not end_dt:
        # fallback to month range
        start_dt = _safe_parse_date(month_start_str)
        end_dt = _safe_parse_date(today_str)
        start_date = month_start_str
        end_date = today_str

    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt
        start_date = start_dt.strftime('%Y-%m-%d')
        end_date = end_dt.strftime('%Y-%m-%d')

    resolved_client = client_query
    resolved_client_obj = None
    if client_query:
        try:
            code_match = Client.query.filter(Client.code.ilike(f'%{client_query}%')).first()
            if code_match:
                resolved_client_obj = code_match
                resolved_client = code_match.name
            else:
                resolved_client_obj = Client.query.filter(func.lower(func.trim(Client.name)) == client_query.lower()).first()
        except Exception:
            pass

    # --- Purchase index for cost resolution ---
    purchase_query = db.session.query(GRNItem, GRN).join(GRN, GRNItem.grn_id == GRN.id).filter(
        GRN.is_void == False,
        GRNItem.is_void == False,
    )
    if material_query:
        purchase_query = purchase_query.filter(GRNItem.mat_name.ilike(f'%{material_query}%'))

    purchase_index = {}
    purchase_cost_rows = []
    material_unit_cost_index = {}
    try:
        purchase_rows_for_cost = purchase_query.order_by(GRN.date_posted.desc()).all()
    except Exception:
        purchase_rows_for_cost = []
    for item, grn in purchase_rows_for_cost:
        mat_name = (item.mat_name or '').strip()
        if not mat_name:
            continue
        mat_key = _norm_text(mat_name)
        posted_dt = grn.date_posted or datetime.min
        rate = float(item.price_at_time or 0)
        purchase_index.setdefault(mat_key, []).append((posted_dt, rate))
        purchase_cost_rows.append((mat_name.casefold(), posted_dt, rate))

    for key in purchase_index:
        purchase_index[key].sort(key=lambda x: x[0])

    try:
        material_cost_rows = Material.query.with_entities(Material.name, Material.unit_price).all()
    except Exception:
        material_cost_rows = []
    for name, unit_price in material_cost_rows:
        mk = _norm_text(name)
        if mk and float(unit_price or 0) > 0:
            material_unit_cost_index[mk] = float(unit_price or 0)

    report_cost_cache = {}

    def _report_cost_for_material(material_name, tx_date=None):
        needle = (material_name or '').strip().casefold()
        date_key = tx_date.isoformat() if hasattr(tx_date, 'isoformat') else str(tx_date or '')
        cache_key = (needle, date_key)
        if cache_key in report_cost_cache:
            return report_cost_cache[cache_key]
        if not needle:
            report_cost_cache[cache_key] = (0.0, False)
            return report_cost_cache[cache_key]
        matching = [row for row in purchase_cost_rows if needle in row[0]]
        if tx_date:
            for _name, posted, rate in matching:
                if posted.date() <= tx_date and rate > 0:
                    report_cost_cache[cache_key] = (rate, True)
                    return report_cost_cache[cache_key]
        if matching and matching[0][2] > 0:
            report_cost_cache[cache_key] = (matching[0][2], True)
            return report_cost_cache[cache_key]
        for name, unit_price in material_cost_rows:
            if needle in (name or '').casefold() and float(unit_price or 0) > 0:
                report_cost_cache[cache_key] = (float(unit_price), True)
                return report_cost_cache[cache_key]
        report_cost_cache[cache_key] = (0.0, False)
        return report_cost_cache[cache_key]

    grn_id = None
    if grn_id_raw:
        try:
            grn_id = int(grn_id_raw)
        except (TypeError, ValueError):
            grn_id = None

    def _waive_for_bill(ref_value, waive_map):
        total = 0.0
        for k in _bill_ref_variants(ref_value):
            total += float(waive_map.get(k, 0.0) or 0.0)
        return total

    # --- Waive-off index ---
    waive_off_by_bill = {}
    try:
        waive_query = WaiveOff.query.filter(
            WaiveOff.is_void == False,
            func.date(WaiveOff.date_posted) >= start_date,
            func.date(WaiveOff.date_posted) <= end_date
        )
        waive_query = waive_query.filter(
            ~func.lower(func.coalesce(WaiveOff.note, '')).like('[direct_sale_discount:%')
        )
        waive_query = waive_query.filter(
            or_(
                WaiveOff.payment_id.is_(None),
                exists().where(Payment.id == WaiveOff.payment_id)
            )
        )
        if resolved_client:
            waive_query = waive_query.filter(WaiveOff.client_name.ilike(f'%{resolved_client}%'))
        waive_rows = waive_query.all()
    except Exception:
        waive_rows = []
    represented_payment_ids = set()
    waive_events = []
    for w in waive_rows:
        if w.payment_id:
            represented_payment_ids.add(w.payment_id)
        amount = float(w.amount or 0)
        _add_waive_to_map(waive_off_by_bill, w.bill_no, amount)
        waive_events.append({
            'client_norm': _norm_text(w.client_name),
            'ref_variants': _bill_ref_variants(w.bill_no),
            'amount': amount
        })

    # Legacy fallback
    try:
        legacy_discount_q = Payment.query.filter(Payment.is_void == False, Payment.discount > 0)
        legacy_discount_q = legacy_discount_q.filter(
            func.date(Payment.date_posted) >= start_date,
            func.date(Payment.date_posted) <= end_date
        )
        if resolved_client:
            if resolved_client_obj:
                legacy_discount_q = legacy_discount_q.filter(or_(
                    Payment.client_id == resolved_client_obj.id,
                    and_(Payment.client_id.is_(None), Payment.client_name.ilike(f'%{resolved_client}%')),
                ))
            else:
                legacy_discount_q = legacy_discount_q.filter(Payment.client_name.ilike(f'%{resolved_client}%'))
        legacy_rows = legacy_discount_q.all()
    except Exception:
        legacy_rows = []
    for p in legacy_rows:
        if p.id in represented_payment_ids:
            continue
        bill_ref = p.manual_bill_no or p.auto_bill_no or f"PAY-{p.id}"
        amount = float(p.discount or 0)
        _add_waive_to_map(waive_off_by_bill, bill_ref, amount)
        waive_events.append({
            'client_norm': _norm_text(p.client_name),
            'ref_variants': _bill_ref_variants(bill_ref),
            'amount': amount
        })

    transactions = []
    direct_sale_grn_map = {}

    # --- Bookings ---
    try:
        booking_query = db.session.query(BookingItem, Booking).join(
            Booking, BookingItem.booking_id == Booking.id
        ).filter(
            Booking.is_void == False,
            func.date(Booking.date_posted) >= start_date,
            func.date(Booking.date_posted) <= end_date
        )
        if resolved_client:
            booking_query = booking_query.filter(Booking.client_name.ilike(f'%{resolved_client}%'))
        if material_query:
            booking_query = booking_query.filter(BookingItem.material_name.ilike(f'%{material_query}%'))
        booking_rows = booking_query.all()
    except Exception:
        booking_rows = []

    booking_gross_map = {}
    booking_waive_map = {}
    for item, booking in booking_rows:
        item_value = float(item.qty or 0) * float(item.price_at_time or 0)
        booking_gross_map[booking.id] = booking_gross_map.get(booking.id, 0) + item_value
        if booking.id not in booking_waive_map:
            booking_ref = booking.manual_bill_no or booking.auto_bill_no or f"BK-{booking.id}"
            booking_waive_map[booking.id] = _waive_for_bill(booking_ref, waive_off_by_bill)

    booking_gross_total = 0.0
    for item, booking in booking_rows:
        qty = float(item.qty or 0)
        sale_rate = float(item.price_at_time or 0)
        gross_revenue = qty * sale_rate
        booking_gross_total += gross_revenue
        booking_gross = float(booking_gross_map.get(booking.id, 0) or 0)
        booking_discount = float(booking.discount or 0)
        booking_waive = float(booking_waive_map.get(booking.id, 0) or 0)
        total_adjustment = booking_discount + booking_waive
        discount_share = (gross_revenue / booking_gross) * total_adjustment if booking_gross > 0 else 0
        net_revenue = max(0.0, gross_revenue - discount_share)
        tx_date = booking.date_posted.date() if booking.date_posted else None
        cost_rate, cogs_known = _report_cost_for_material(item.material_name, tx_date)
        cogs = qty * cost_rate
        profit = net_revenue - cogs
        transactions.append({
            'date': booking.date_posted,
            'source': 'Booking',
            'reference': booking.manual_bill_no or booking.auto_bill_no or f'BK-{booking.id}',
            'client': booking.client_name,
            '_client_norm': _norm_text(booking.client_name),
            '_ref_variants': _bill_ref_variants(booking.manual_bill_no or booking.auto_bill_no or f'BK-{booking.id}'),
            'material': item.material_name,
            'qty': qty,
            'sale_rate': sale_rate,
            'cost_rate': cost_rate,
            'discount_loss': discount_share,
            'revenue': net_revenue,
            'cogs': cogs,
            'profit': profit,
            'is_loss': profit < 0,
            'cogs_known': cogs_known
        })

    # --- Direct Sales ---
    try:
        direct_query = db.session.query(DirectSaleItem, DirectSale).join(
            DirectSale, DirectSaleItem.sale_id == DirectSale.id
        ).filter(
            DirectSale.is_void == False,
            func.date(DirectSale.date_posted) >= start_date,
            func.date(DirectSale.date_posted) <= end_date
        )
        if resolved_client:
            direct_query = direct_query.filter(DirectSale.client_name.ilike(f'%{resolved_client}%'))
        if material_query:
            direct_query = direct_query.filter(DirectSaleItem.product_name.ilike(f'%{material_query}%'))
        if grn_id:
            direct_query = direct_query.join(
                GRNItem, DirectSaleItem.grn_item_id == GRNItem.id
            ).filter(GRNItem.grn_id == grn_id)
        direct_rows = direct_query.all()
    except Exception:
        direct_rows = []

    direct_sale_ids = sorted({int(s.id) for _item, s in direct_rows}) if direct_rows else []
    if direct_sale_ids:
        try:
            DirectSale.query.options(selectinload(DirectSale.invoice)).filter(
                DirectSale.id.in_(direct_sale_ids)
            ).all()
        except Exception:
            pass
    try:
        direct_grn_ids = sorted({int(it.grn_item_id) for it, _s in direct_rows if getattr(it, 'grn_item_id', None)})
    except Exception:
        direct_grn_ids = []
    if direct_grn_ids:
        try:
            grn_link_rows = db.session.query(GRNItem, GRN).join(
                GRN, GRNItem.grn_id == GRN.id
            ).filter(GRNItem.id.in_(direct_grn_ids)).all()
            for gi, grn in grn_link_rows:
                if not gi or not grn:
                    continue
                direct_sale_grn_map[int(gi.id)] = {
                    'grn_id': int(grn.id),
                    'grn_bill': (grn.manual_bill_no or grn.auto_bill_no or f"GRN-{grn.id}"),
                    'grn_date': grn.date_posted,
                    'supplier': (grn.supplier or '').strip(),
                    'grn_is_void': bool(getattr(grn, 'is_void', False)),
                    'grn_item_is_void': bool(getattr(gi, 'is_void', False)),
                    'cost_rate': float(getattr(gi, 'price_at_time', 0) or 0),
                }
        except Exception:
            pass

    sale_gross_map = {}
    sale_waive_map = {}
    for item, sale in direct_rows:
        try:
            sale_category = normalize_sale_category(getattr(sale, 'category', None))
        except Exception:
            sale_category = 'Credit Customer'
        if sale_category == 'Booking Delivery':
            continue
        if float(item.price_at_time or 0) <= 0:
            continue
        item_value = float(item.qty or 0) * float(item.price_at_time or 0)
        sale_gross_map[sale.id] = sale_gross_map.get(sale.id, 0) + item_value
        if sale.id not in sale_waive_map:
            try:
                sale_ref = (_direct_sale_default_bill_ref(sale) or sale.auto_bill_no or f"DS-{sale.id}").strip()
            except Exception:
                sale_ref = sale.auto_bill_no or f"DS-{sale.id}"
            sale_waive_map[sale.id] = _waive_for_bill(sale_ref, waive_off_by_bill)

    direct_gross_total = 0.0
    for item, sale in direct_rows:
        try:
            sale_category = normalize_sale_category(getattr(sale, 'category', None))
        except Exception:
            sale_category = 'Credit Customer'
        if sale_category == 'Booking Delivery':
            continue
        qty = float(item.qty or 0)
        sale_rate = float(item.price_at_time or 0)
        if sale_rate <= 0:
            continue
        gross_revenue = qty * sale_rate
        direct_gross_total += gross_revenue
        sale_gross = float(sale_gross_map.get(sale.id, 0) or 0)
        sale_discount = float(sale.discount or 0)
        sale_waive = float(sale_waive_map.get(sale.id, 0) or 0)
        total_adjustment = sale_discount + sale_waive
        discount_share = (gross_revenue / sale_gross) * total_adjustment if sale_gross > 0 else 0
        net_revenue = max(0.0, gross_revenue - discount_share)
        tx_date = sale.date_posted.date() if sale.date_posted else None
        frozen_cost = float(getattr(item, 'cost_rate_at_sale', 0) or 0)
        cost_rate, cogs_known = (frozen_cost, True) if frozen_cost > 0 else (0.0, False)
        grn_info = direct_sale_grn_map.get(int(item.grn_item_id)) if getattr(item, 'grn_item_id', None) else None
        if not cogs_known and grn_info and not grn_info['grn_is_void'] and not grn_info['grn_item_is_void']:
            grn_cost = float(grn_info.get('cost_rate') or 0)
            if grn_cost > 0:
                cost_rate, cogs_known = grn_cost, True
        if not cogs_known:
            cost_rate, cogs_known = _report_cost_for_material(item.product_name, tx_date)
        cogs = qty * cost_rate
        profit = net_revenue - cogs
        try:
            sale_ref = (_direct_sale_default_bill_ref(sale) or sale.auto_bill_no or f"DS-{sale.id}").strip()
        except Exception:
            sale_ref = sale.auto_bill_no or f"DS-{sale.id}"
        sale_ref_variants = set()
        try:
            for r in _direct_sale_bill_refs(sale):
                sale_ref_variants |= _bill_ref_variants(r)
        except Exception:
            sale_ref_variants = _bill_ref_variants(sale_ref)
        grn_info2 = direct_sale_grn_map.get(int(item.grn_item_id)) if getattr(item, 'grn_item_id', None) else None
        transactions.append({
            'date': sale.date_posted,
            'source': 'Direct Sale',
            'reference': sale_ref,
            'client': sale.client_name,
            '_client_norm': _norm_text(sale.client_name),
            '_ref_variants': sale_ref_variants,
            'material': item.product_name,
            'qty': qty,
            'sale_rate': sale_rate,
            'cost_rate': cost_rate,
            'discount_loss': discount_share,
            'revenue': net_revenue,
            'cogs': cogs,
            'profit': profit,
            'is_loss': profit < 0,
            'cogs_known': cogs_known,
            'note': (sale.note or '').strip(),
            'grn_item_id': (int(item.grn_item_id) if getattr(item, 'grn_item_id', None) else None),
            'grn_id': (int(grn_info2.get('grn_id')) if grn_info2 else None),
            'grn_bill': (grn_info2.get('grn_bill') if grn_info2 else None),
            'grn_supplier': (grn_info2.get('supplier') if grn_info2 else None),
        })

    # --- Delivery rent variance ---
    include_rent_variance = (not material_query) or _is_rent_material_name(material_query)
    if include_rent_variance:
        try:
            rent_loss_query = DirectSale.query.filter(
                DirectSale.is_void == False,
                func.date(DirectSale.date_posted) >= start_date,
                func.date(DirectSale.date_posted) <= end_date
            )
            if resolved_client:
                rent_loss_query = rent_loss_query.filter(DirectSale.client_name.ilike(f'%{resolved_client}%'))
            rent_sales = rent_loss_query.options(
                selectinload(DirectSale.items),
                selectinload(DirectSale.invoice),
            ).all()
        except Exception:
            rent_sales = []
        rent_sale_ids = [sale.id for sale in rent_sales] if rent_sales else []
        fallback_rent_by_sale = {}
        if rent_sale_ids:
            try:
                for rent_row in DeliveryRent.query.filter(
                    DeliveryRent.sale_id.in_(rent_sale_ids),
                    DeliveryRent.is_void == False,
                ).order_by(DeliveryRent.id.desc()).all():
                    fallback_rent_by_sale.setdefault(rent_row.sale_id, rent_row)
            except Exception:
                pass
        needs_any_booking_rate = any(
            float(item.qty or 0) > 0
            and float(item.price_at_time or 0) <= 0
            and _is_rent_material_name(item.product_name)
            for sale in rent_sales for item in (sale.items or [])
        ) if rent_sales else False
        rent_client_name_map = {}
        booking_rate_by_client = {}
        if needs_any_booking_rate:
            try:
                client_rows_for_rent = Client.query.all()
                by_code = {}
                by_name = {}
                by_folded = {}
                for client_row in client_rows_for_rent:
                    by_code.setdefault((client_row.code or '').strip(), client_row)
                    by_name.setdefault((client_row.name or '').strip(), client_row)
                    by_folded.setdefault((client_row.code or '').strip().casefold(), client_row)
                    by_folded.setdefault((client_row.name or '').strip().casefold(), client_row)
                for sale in rent_sales:
                    value = (sale.client_code or '').strip() or (sale.client_name or '').strip()
                    resolved = by_code.get(value) or by_name.get(value) or by_folded.get(value.casefold())
                    if not resolved and sale.client_name:
                        resolved = by_folded.get((sale.client_name or '').strip().casefold())
                    if resolved:
                        rent_client_name_map[sale.id] = resolved.name
                wanted_client_names = set(rent_client_name_map.values())
                latest_booking_dates = {}
                if wanted_client_names:
                    booking_rate_rows = db.session.query(BookingItem, Booking).join(
                        Booking, BookingItem.booking_id == Booking.id
                    ).filter(
                        Booking.is_void == False,
                        Booking.client_name.in_(wanted_client_names),
                    ).all()
                    for booked_item, booked_sale in booking_rate_rows:
                        material_key = _material_norm_key(booked_item.material_name)
                        if not material_key:
                            continue
                        key = (booked_sale.client_name, material_key)
                        posted = booked_sale.date_posted
                        previous = latest_booking_dates.get(key)
                        if key not in booking_rate_by_client or (posted and (not previous or posted > previous)):
                            latest_booking_dates[key] = posted
                            booking_rate_by_client[key] = float(booked_item.price_at_time or 0)
            except Exception:
                pass
        for sale in rent_sales:
            try:
                sale_items_payload = [
                    {
                        'product_name': it.product_name,
                        'qty': it.qty,
                        'price_at_time': it.price_at_time
                    }
                    for it in (sale.items or [])
                ]
                fallback_rent_row = fallback_rent_by_sale.get(sale.id)
                fallback_delivery_cost = float(fallback_rent_row.amount or 0) if fallback_rent_row else 0.0
                effective_delivery_cost = float(getattr(sale, 'delivery_rent_cost', 0) or 0)
                if effective_delivery_cost <= 0:
                    effective_delivery_cost = fallback_delivery_cost
                rent_item_revenue = 0.0
                needs_booking_rate = False
                for rent_item in sale_items_payload:
                    if not _is_rent_material_name(rent_item.get('product_name')):
                        continue
                    qty = float(rent_item.get('qty') or 0)
                    rate = float(rent_item.get('price_at_time') or 0)
                    if qty > 0 and rate > 0:
                        rent_item_revenue += qty * rate
                    elif qty > 0:
                        needs_booking_rate = True
                if needs_booking_rate:
                    rent_item_revenue = 0.0
                    canonical_client_name = rent_client_name_map.get(sale.id)
                    for rent_item in sale_items_payload:
                        material_name = (rent_item.get('product_name') or '').strip()
                        if not _is_rent_material_name(material_name):
                            continue
                        qty = float(rent_item.get('qty') or 0)
                        rate = float(rent_item.get('price_at_time') or 0)
                        if rate <= 0 and canonical_client_name:
                            rate = float(booking_rate_by_client.get(
                                (canonical_client_name, _material_norm_key(material_name)), 0
                            ) or 0)
                        if qty > 0 and rate > 0:
                            rent_item_revenue += qty * rate
                rent_rec = {
                    'rent_item_revenue': rent_item_revenue,
                    'delivery_rent_cost': max(0.0, effective_delivery_cost),
                }
                rent_revenue = float(getattr(sale, 'rent_item_revenue', 0) or rent_rec['rent_item_revenue'])
                rent_cost = float(getattr(sale, 'delivery_rent_cost', 0) or rent_rec['delivery_rent_cost'])
                variance = rent_revenue - rent_cost
                if abs(variance) <= 0.0001:
                    continue
                sale_ref = sale.manual_bill_no or sale.auto_bill_no or f"DS-{sale.id}"
                if getattr(sale, 'invoice', None) and sale.invoice and sale.invoice.invoice_no:
                    sale_ref = sale.invoice.invoice_no
                transactions.append({
                    'date': sale.date_posted,
                    'source': ('Delivery Rent Variance (Company Profit)' if variance > 0 else 'Delivery Rent Variance (Company Loss)'),
                    'reference': sale_ref,
                    'client': sale.client_name,
                    '_client_norm': _norm_text(sale.client_name),
                    '_ref_variants': _bill_ref_variants(sale_ref),
                    'material': 'Delivery Rent Difference',
                    'qty': 0.0,
                    'sale_rate': 0.0,
                    'cost_rate': 0.0,
                    'discount_loss': (abs(variance) if variance < 0 else 0.0),
                    'revenue': 0.0,
                    'cogs': 0.0,
                    'profit': variance,
                    'is_loss': variance < 0,
                    'cogs_known': True
                })
            except Exception:
                continue

        # DeliveryPerson waive-off profit
        try:
            waive_q = db.session.query(
                DeliveryPersonPayment,
                SaleDeliveryPerson,
                DirectSale
            ).join(
                SaleDeliveryPerson, DeliveryPersonPayment.allocation_id == SaleDeliveryPerson.id
            ).join(
                DirectSale, SaleDeliveryPerson.sale_id == DirectSale.id
            ).filter(
                DeliveryPersonPayment.is_void == False,
                DeliveryPersonPayment.waive_off_amount > 0,
                func.date(DeliveryPersonPayment.date_posted) >= start_date,
                func.date(DeliveryPersonPayment.date_posted) <= end_date
            )
            if resolved_client:
                waive_q = waive_q.filter(DirectSale.client_name.ilike(f'%{resolved_client}%'))
            waive_rows2 = waive_q.all()
        except Exception:
            waive_rows2 = []
        for pay, alloc, sale in waive_rows2:
            try:
                sale_ref = sale.manual_bill_no or sale.auto_bill_no or f"DS-{sale.id}"
                if getattr(sale, 'invoice', None) and sale.invoice and sale.invoice.invoice_no:
                    sale_ref = sale.invoice.invoice_no
                amt = float(pay.waive_off_amount or 0)
                if amt <= 0:
                    continue
                transactions.append({
                    'date': pay.date_posted,
                    'source': 'Delivery Person Waive-Off (Profit)',
                    'reference': sale_ref,
                    'client': sale.client_name,
                    '_client_norm': _norm_text(sale.client_name),
                    '_ref_variants': _bill_ref_variants(sale_ref),
                    'material': 'Delivery Rent Waive-Off',
                    'qty': 0.0,
                    'sale_rate': 0.0,
                    'cost_rate': 0.0,
                    'discount_loss': 0.0,
                    'revenue': 0.0,
                    'cogs': 0.0,
                    'profit': amt,
                    'is_loss': False,
                    'cogs_known': True
                })
            except Exception:
                continue

    # --- Unallocated waive-off allocation ---
    try:
        matched_event_idx = set()
        for idx, ev in enumerate(waive_events):
            ev_refs = ev.get('ref_variants') or set()
            ev_client = ev.get('client_norm') or ''
            if not ev_refs:
                continue
            for tx in transactions:
                tx_refs = tx.get('_ref_variants') or set()
                tx_client = tx.get('_client_norm') or ''
                if ev_client and tx_client and ev_client != tx_client:
                    continue
                if ev_refs & tx_refs:
                    matched_event_idx.add(idx)
                    break
        unallocated_by_client = {}
        for idx, ev in enumerate(waive_events):
            if idx in matched_event_idx:
                continue
            ckey = ev.get('client_norm') or ''
            if not ckey:
                continue
            unallocated_by_client[ckey] = unallocated_by_client.get(ckey, 0.0) + float(ev.get('amount') or 0.0)
        for ckey, amount in unallocated_by_client.items():
            if amount <= 0:
                continue
            candidates = [t for t in transactions if (t.get('_client_norm') == ckey and float(t.get('revenue') or 0) > 0)]
            if not candidates:
                transactions.append({
                    'date': datetime.combine(end_dt, datetime.min.time()),
                    'source': 'Waive-Off',
                    'reference': f'LOSS-{ckey[:10].upper() or "UNLINKED"}',
                    'client': ckey or 'Unlinked',
                    'material': '-',
                    'qty': 0.0,
                    'sale_rate': 0.0,
                    'cost_rate': 0.0,
                    'discount_loss': amount,
                    'revenue': 0.0,
                    'cogs': 0.0,
                    'profit': -float(amount or 0),
                    'is_loss': True,
                    'cogs_known': True
                })
                continue
            total_rev = sum(float(t.get('revenue') or 0) for t in candidates)
            if total_rev <= 0:
                transactions.append({
                    'date': datetime.combine(end_dt, datetime.min.time()),
                    'source': 'Waive-Off',
                    'reference': f'LOSS-{ckey[:10].upper() or "UNLINKED"}',
                    'client': ckey or 'Unlinked',
                    'material': '-',
                    'qty': 0.0,
                    'sale_rate': 0.0,
                    'cost_rate': 0.0,
                    'discount_loss': amount,
                    'revenue': 0.0,
                    'cogs': 0.0,
                    'profit': -float(amount or 0),
                    'is_loss': True,
                    'cogs_known': True
                })
                continue
            allocated = 0.0
            for i, t in enumerate(candidates):
                if i == len(candidates) - 1:
                    share = amount - allocated
                else:
                    share = (float(t.get('revenue') or 0) / total_rev) * amount
                    allocated += share
                t['revenue'] = max(0.0, float(t.get('revenue') or 0) - share)
                t['discount_loss'] = float(t.get('discount_loss') or 0) + float(share or 0)
                t['profit'] = float(t.get('revenue') or 0) - float(t.get('cogs') or 0)
                t['is_loss'] = bool(t.get('cogs_known')) and (float(t.get('profit') or 0) < 0)
    except Exception as e:
        logger.warning("waive allocation failed: %s", e)

    # --- Account reconciliation P/L ---
    if not resolved_client and not material_query:
        try:
            reconciliation_q = AccountTransaction.query.filter(
                AccountTransaction.is_void == False,
                AccountTransaction.transaction_type.in_(['Reconciliation Loss', 'Reconciliation Excess']),
                func.date(AccountTransaction.date_posted) >= start_date,
                func.date(AccountTransaction.date_posted) <= end_date,
            )
            for adjustment in reconciliation_q.order_by(AccountTransaction.date_posted.asc(), AccountTransaction.id.asc()).all():
                amount = float(adjustment.amount or 0)
                is_loss = adjustment.transaction_type == 'Reconciliation Loss'
                account = adjustment.from_account if is_loss else adjustment.to_account
                transactions.append({
                    'date': adjustment.date_posted,
                    'source': ('Account Reconciliation Loss' if is_loss else 'Account Reconciliation Profit / Excess'),
                    'reference': f'RECON-{adjustment.reconciliation_id or adjustment.source_id or adjustment.id}',
                    'client': account.name if account else 'Account',
                    'material': 'Account Reconciliation',
                    'qty': 0.0, 'sale_rate': 0.0, 'cost_rate': 0.0,
                    'discount_loss': amount if is_loss else 0.0,
                    'revenue': 0.0, 'cogs': 0.0,
                    'profit': -amount if is_loss else amount,
                    'is_loss': is_loss, 'cogs_known': True,
                })
        except Exception:
            pass

    # Remove helper keys before totals
    for t in transactions:
        t.pop('_client_norm', None)
        t.pop('_ref_variants', None)

    try:
        transactions.sort(key=lambda x: x['date'] or datetime.min, reverse=True)
    except Exception:
        pass

    total_revenue = sum(float(t.get('revenue') or 0) for t in transactions)
    total_discount_loss = sum(float(t.get('discount_loss') or 0) for t in transactions)
    total_cogs = sum(float(t.get('cogs') or 0) for t in transactions if t.get('cogs_known'))
    known_cost_revenue = sum(float(t.get('revenue') or 0) for t in transactions if t.get('cogs_known'))
    unknown_cost_revenue = max(0.0, total_revenue - known_cost_revenue)
    total_profit = sum(float(t.get('profit') or 0) for t in transactions if t.get('cogs_known'))
    unknown_cost_rows = sum(1 for t in transactions if not t.get('cogs_known'))
    profit_rows = sum(1 for t in transactions if t.get('cogs_known') and float(t.get('profit') or 0) >= 0)
    loss_rows = sum(1 for t in transactions if t.get('cogs_known') and float(t.get('profit') or 0) < 0)
    margin_pct = (total_profit / known_cost_revenue * 100.0) if known_cost_revenue > 0 else 0.0
    markup_pct = (total_profit / total_cogs * 100.0) if total_cogs > 0 else 0.0

    return {
        'transactions': transactions,
        'total_revenue': total_revenue,
        'total_discount_loss': total_discount_loss,
        'total_cogs': total_cogs,
        'total_profit': total_profit,
        'known_cost_revenue': known_cost_revenue,
        'unknown_cost_revenue': unknown_cost_revenue,
        'unknown_cost_rows': unknown_cost_rows,
        'profit_rows': profit_rows,
        'loss_rows': loss_rows,
        'margin_pct': margin_pct,
        'markup_pct': markup_pct,
        'start_date': start_date,
        'end_date': end_date,
    }


def get_dashboard_profit() -> dict:
    """
    Dashboard KPI helper — exact current profit/loss amount.

    Returns dict with:
      amount (float): net profit/loss (positive = profit, negative = loss)
      is_profit (bool): True if profit, False if loss
      label (str): "Net Profit" or "Net Loss"
      is_loss (bool): True if loss
      margin_pct, total_revenue, total_cogs, total_discount_loss, etc.
      period_label: human readable period (e.g. "Oct 2026 (MTD)")

    Defaults to current month-to-date (same as Profit Reports default).
    Falls back to 0 on any error so dashboard never breaks.
    """
    try:
        today = pk_today()
        month_start = today.replace(day=1)
        start_str = month_start.strftime('%Y-%m-%d')
        end_str = today.strftime('%Y-%m-%d')
        result = compute_profit_summary(start_str, end_str)
        amount = float(result.get('total_profit') or 0)
        return {
            'amount': amount,
            'total_profit': amount,
            'is_profit': amount >= 0,
            'is_loss': amount < 0,
            'label': 'Net Profit' if amount >= 0 else 'Net Loss',
            'total_revenue': float(result.get('total_revenue') or 0),
            'total_cogs': float(result.get('total_cogs') or 0),
            'total_discount_loss': float(result.get('total_discount_loss') or 0),
            'margin_pct': float(result.get('margin_pct') or 0),
            'markup_pct': float(result.get('markup_pct') or 0),
            'start_date': start_str,
            'end_date': end_str,
            'period_label': f"{month_start.strftime('%b %Y')} (MTD: {month_start.strftime('%d %b')} – {today.strftime('%d %b %Y')})",
            'period_short': f"{month_start.strftime('%b %Y')}",
            'profit_rows': int(result.get('profit_rows') or 0),
            'loss_rows': int(result.get('loss_rows') or 0),
            'unknown_cost_rows': int(result.get('unknown_cost_rows') or 0),
            'unknown_cost_revenue': float(result.get('unknown_cost_revenue') or 0),
        }
    except Exception as e:
        logger.exception("dashboard profit calculation failed: %s", e)
        return {
            'amount': 0.0,
            'total_profit': 0.0,
            'is_profit': True,
            'is_loss': False,
            'label': 'Net Profit',
            'total_revenue': 0.0,
            'total_cogs': 0.0,
            'total_discount_loss': 0.0,
            'margin_pct': 0.0,
            'markup_pct': 0.0,
            'start_date': '',
            'end_date': '',
            'period_label': 'Current Month',
            'period_short': 'Current Month',
            'profit_rows': 0,
            'loss_rows': 0,
            'unknown_cost_rows': 0,
            'unknown_cost_revenue': 0.0,
        }


def get_lifetime_profit() -> dict:
    """
    Lifetime profit — from earliest transaction to today.
    Useful as secondary tooltip/value on dashboard.
    """
    try:
        # Find earliest date across relevant tables
        earliest = None
        for model, field in [
            (GRN, GRN.date_posted),
            (Booking, Booking.date_posted),
            (DirectSale, DirectSale.date_posted),
        ]:
            try:
                row = db.session.query(func.min(func.date(field))).filter(field.isnot(None)).scalar()
                if row:
                    dt = _safe_parse_date(str(row))
                    if dt and (earliest is None or dt < earliest):
                        earliest = dt
            except Exception:
                continue
        if not earliest:
            earliest = pk_today().replace(day=1)
        start_str = earliest.strftime('%Y-%m-%d')
        end_str = pk_today().strftime('%Y-%m-%d')
        result = compute_profit_summary(start_str, end_str)
        amount = float(result.get('total_profit') or 0)
        return {
            'amount': amount,
            'total_profit': amount,
            'is_profit': amount >= 0,
            'is_loss': amount < 0,
            'label': 'Net Profit' if amount >= 0 else 'Net Loss',
            'total_revenue': float(result.get('total_revenue') or 0),
            'total_cogs': float(result.get('total_cogs') or 0),
            'total_discount_loss': float(result.get('total_discount_loss') or 0),
            'margin_pct': float(result.get('margin_pct') or 0),
            'markup_pct': float(result.get('markup_pct') or 0),
            'start_date': start_str,
            'end_date': end_str,
            'period_label': f"Lifetime ({start_str} – {end_str})",
            'period_short': "Lifetime",
        }
    except Exception as e:
        logger.exception("lifetime profit calculation failed: %s", e)
        return {
            'amount': 0.0,
            'total_profit': 0.0,
            'is_profit': True,
            'is_loss': False,
            'label': 'Net Profit',
            'total_revenue': 0.0,
            'total_cogs': 0.0,
            'total_discount_loss': 0.0,
            'margin_pct': 0.0,
            'markup_pct': 0.0,
            'start_date': '',
            'end_date': '',
            'period_label': 'Lifetime',
            'period_short': 'Lifetime',
        }
