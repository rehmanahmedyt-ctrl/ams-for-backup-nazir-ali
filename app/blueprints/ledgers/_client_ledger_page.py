"""client — split from ledgers.py."""
from ._common import *  # noqa
from app.services.permissions import _is_master_admin

@bp.route('/ledger')
@login_required
def ledger_page():
    clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    materials = Material.query.order_by(Material.name.asc()).all()
    return render_template('ledger.html', clients=clients, materials=materials,
                           is_master_admin=_is_master_admin())

