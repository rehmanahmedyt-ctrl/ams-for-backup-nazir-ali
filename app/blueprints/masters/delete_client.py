from ._common import *  # noqa
from app.services.void_rebuild import (
    client_purge_preview as _client_purge_preview,
    hard_delete_client as _do_hard_delete_client,
)


@bp.route('/delete_client/<int:id>', methods=['POST'])
@login_required
def delete_client(id):
    if not _user_can('can_manage_clients'):
        flash('Permission denied', 'danger')
        return redirect(url_for('clients'))
    c = db.session.get(Client, id)
    if c:
        from utils.accounting_audit import record_accounting_audit
        before = {'id': c.id, 'code': c.code, 'name': c.name, 'is_active': bool(c.is_active)}
        c.is_active = False
        record_accounting_audit(
            current_user, action='Suspend', entity_type='Client', entity_id=c.id,
            before=before, after={**before, 'is_active': False},
            party_before_id=c.id, party_after_id=c.id, reason='Client suspended', module='clients',
        )
        db.session.commit()
        flash('Client suspended; historical transactions were preserved.', 'warning')
    return redirect(url_for('clients'))


@bp.route('/hard_delete_client/<int:id>/preview', methods=['GET'])
@login_required
def hard_delete_client_preview(id):
    """Master-admin-only: what a clean purge of this client would remove.

    Read-only.  Feeds the confirmation dialog so the blast radius is visible
    *before* anything is typed or submitted.
    """
    if not _is_master_admin():
        return jsonify({'error': 'Permission denied'}), 403

    client = db.session.get(Client, id)
    if not client:
        return jsonify({'error': 'Client not found.'}), 404
    try:
        preview = _client_purge_preview(client)
    except Exception:
        logging.exception('Client purge preview failed for client %s', id)
        return jsonify({'error': 'Unable to build the purge preview.'}), 500
    if preview is None:
        return jsonify({'error': 'Client has no usable code or name.'}), 422
    return jsonify(preview)


@bp.route('/hard_delete_client/<int:id>', methods=['POST'])
@login_required
def hard_delete_client(id):
    """Master-admin-only special power: permanently delete a client, cleanly.

    This is deliberately *not* governed by ``can_manage_clients``.  Where
    ``delete_client`` merely suspends a client and preserves their history, this
    route erases the client and reverses every transaction made for them —
    sales, bookings, payments, material returns, pending bills, waive-offs,
    invoices, stock rows, deliveries and the account transactions behind them —
    leaving no voided rows and no orphans.  Money is reversed out of the
    cash/bank accounts it was posted to, FIFO lots and booked quantities are
    released, and stock totals are recomputed.

    Access is gated on ``_is_master_admin()``: the built-in Admin account alone
    (role ``admin`` *and* username ``DEFAULT_ADMIN_USER``).  Every other user,
    including other admins, is refused and nothing is mutated.

    Because the result is irreversible the caller must retype the client's name.
    A failed confirmation, a protected system bucket, or any error rolls the
    whole transaction back — the purge is all-or-nothing.
    """
    if not _is_master_admin():
        try:
            audit_log(current_user, 'client.hard_delete.denied',
                      f'Blocked hard delete attempt on client #{id}')
        except Exception:
            logging.exception('Could not record denied client hard delete #%s', id)
        flash('Permission denied: only the master admin can permanently delete a client.', 'danger')
        return redirect(url_for('clients'))

    client = db.session.get(Client, id)
    if not client:
        flash('Client not found.', 'warning')
        return redirect(url_for('clients'))

    expected = (client.name or '').strip()
    supplied = (request.form.get('confirm_name') or '').strip()
    if not expected or supplied.lower() != expected.lower():
        flash(
            f'Confirmation did not match — nothing was deleted. '
            f'Type the client name exactly ({expected}) to permanently delete.',
            'danger',
        )
        return redirect(url_for('clients'))

    client_name = client.name
    try:
        res = _do_hard_delete_client(client, actor=current_user)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
        return redirect(url_for('clients'))
    except Exception:
        db.session.rollback()
        logging.exception('Client hard delete failed for client %s', id)
        flash('Unable to permanently delete client. Nothing was changed.', 'danger')
        return redirect(url_for('clients'))

    # Written only after the purge has committed: ``audit_log`` uses its own
    # database connection, which cannot take the SQLite write lock while the
    # request transaction is still open.  The structured accounting audit event
    # was already recorded inside the transaction by the service.
    try:
        audit_log(current_user, 'client.hard_delete', res.get('audit_summary') or
                  f"Hard deleted client '{client_name}' (#{id}).")
    except Exception:
        logging.exception('Audit entry for hard-deleted client %s failed', id)

    flash(
        f"Client '{client_name}' was permanently deleted and all their transactions "
        f"were cleanly reversed: {res.get('sales_deleted', 0)} sale(s), "
        f"{res.get('bookings_deleted', 0)} booking(s), "
        f"{res.get('payments_deleted', 0)} payment(s), "
        f"{res.get('pending_bills_deleted', 0)} pending bill(s), "
        f"{res.get('stock_entries_deleted', 0)} stock row(s) and "
        f"{res.get('account_transactions_deleted', 0)} account transaction(s) removed. "
        f"Cash/bank balances were reversed and stock recomputed. "
        f"You can now add this client again from scratch.",
        'success',
    )
    return redirect(url_for('clients'))
