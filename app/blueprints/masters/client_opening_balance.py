from ._common import *  # noqa

@bp.route('/client_opening_balance/<int:id>', methods=['POST'])
@login_required
def client_opening_balance(id):
    if not _user_can('can_manage_clients'):
        flash('Permission denied', 'danger')
        return redirect(url_for('financial_ledger', client_id=id))
    c = db.session.get(Client, id)
    if not c:
        flash('Client not found', 'danger')
        return redirect(url_for('clients'))

    # Capture the pre-change state so the opening-balance edit leaves the same
    # structured accounting-audit trail that /edit_client already writes.
    # Without this, balances changed through the ledger's "Set Opening Balance"
    # dialog were saved with no audit row at all.
    before = {'id': c.id, 'code': c.code, 'name': c.name, 'phone': c.phone,
              'address': c.address, 'opening_balance': c.opening_balance,
              'opening_balance_date': c.opening_balance_date,
              'is_active': bool(c.is_active)}
    amount_before = _to_float_or_zero(c.opening_balance)

    c.opening_balance = _to_float_or_zero(request.form.get('opening_balance', c.opening_balance))
    c.opening_balance_date = _resolve_opening_balance_date(
        request.form.get('opening_balance_date'),
        fallback_dt=(c.opening_balance_date or c.created_at)
    )

    from utils.accounting_audit import record_accounting_audit
    after = {'id': c.id, 'code': c.code, 'name': c.name, 'phone': c.phone,
             'address': c.address, 'opening_balance': c.opening_balance,
             'opening_balance_date': c.opening_balance_date,
             'is_active': bool(c.is_active)}
    record_accounting_audit(
        current_user, action='Edit', entity_type='Client', entity_id=c.id,
        before=before, after=after,
        amount_before=amount_before,
        amount_after=_to_float_or_zero(c.opening_balance),
        party_before_id=c.id, party_after_id=c.id,
        reason='Client opening balance updated', module='clients',
    )
    db.session.commit()
    flash('Opening balance updated', 'success')
    return redirect(url_for('financial_ledger', client_id=id))
