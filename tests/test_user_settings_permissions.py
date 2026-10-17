"""Settings is an admin-only, master-granted permission; standard users use module ticks."""
from models import User, db
from tests.conftest import make_csrf_client


def login(client, username, password='Secret123'):
    response = client.post('/login', data={'username': username, 'password': password})
    assert response.status_code == 302


def test_settings_requires_admin_role_and_master_checkbox(app, client):
    login(client, 'Admin', 'Admin@fbm12345')
    page = client.get('/settings')
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'Administrator Settings Access' in html
    assert 'data-admin-settings-permission' in html

    # Even a forged checkbox must not grant Settings to a standard user.
    response = client.post('/add_user', data={
        'username': 'worker', 'password': 'Secret123', 'role': 'user',
        'can_manage_grn': 'on', 'can_access_settings': 'on',
    })
    assert response.status_code == 302
    with app.app_context():
        worker = User.query.filter_by(username='worker').one()
        worker_id = worker.id
        assert worker.can_access_settings is False
        assert worker.can_manage_grn is True
        assert worker.can_view_stock is False
        # Simulate an old database row that already had Settings ticked.
        worker.can_access_settings = True
        db.session.commit()
    # Legacy grants must not make the checkbox appear checked for standard users.
    master_html = client.get('/settings').get_data(as_text=True)
    assert f'<div data-admin-settings-permission class="mt-3" hidden>' in master_html
    assert f'id="editUserSettings{worker_id}" >' in master_html

    worker_client = make_csrf_client(app)
    login(worker_client, 'worker')
    landing = worker_client.get('/')
    assert landing.status_code == 200
    assert 'Dashboard access is not enabled' in landing.get_data(as_text=True)
    assert 'href="/settings"' not in landing.get_data(as_text=True)
    for path in ('/settings', '/settings/activity', '/settings/sessions', '/void_audit'):
        assert worker_client.get(path).status_code == 302
    assert worker_client.post('/update_settings', data={'company_name': 'HACKED'}).status_code == 302
    # Other module permissions still work independently of Settings.
    assert worker_client.get('/grn').status_code == 200
    assert worker_client.get('/stock_summary').status_code == 302

    # Master changes role but leaves the Settings box unticked: no Settings.
    client.post(f'/edit_user_permissions/{worker_id}', data={'role': 'admin'})
    with app.app_context():
        worker = db.session.get(User, worker_id)
        assert worker.role == 'admin' and worker.can_access_settings is False
    assert 'href="/settings"' not in worker_client.get('/').get_data(as_text=True)
    assert worker_client.get('/settings').status_code == 302
    assert worker_client.post('/update_settings', data={'company_name': 'HACKED'}).status_code == 302
    # Admin-only module access does not imply permission to grant roles.
    assert worker_client.post('/add_user', data={'username': 'intruder', 'role': 'admin', 'can_access_settings': 'on'}).status_code == 302
    with app.app_context():
        assert User.query.filter_by(username='intruder').first() is None

    # The built-in Admin can explicitly grant Settings to an administrator.
    client.post(f'/edit_user_permissions/{worker_id}', data={'role': 'admin', 'can_access_settings': 'on'})
    assert 'href="/settings"' in worker_client.get('/').get_data(as_text=True)
    settings_html = worker_client.get('/settings').get_data(as_text=True)
    assert 'General Settings' in settings_html
    assert 'User Permissions' not in settings_html  # only master manages users
    assert worker_client.get('/settings/activity').status_code == 200
    assert worker_client.post('/update_settings', data={'company_name': 'Delegated'}).status_code == 302

    # Demotion clears an old grant, even if the forged checkbox is submitted.
    client.post(f'/edit_user_permissions/{worker_id}', data={'role': 'user', 'can_access_settings': 'on', 'can_manage_sales': 'on'})
    with app.app_context():
        worker = db.session.get(User, worker_id)
        assert worker.role == 'user' and worker.can_access_settings is False
        assert worker.can_manage_sales is True
    assert worker_client.get('/settings').status_code == 302
    assert 'href="/settings"' not in worker_client.get('/').get_data(as_text=True)


def test_new_admin_needs_explicit_settings_grant(app, client):
    login(client, 'Admin', 'Admin@fbm12345')
    client.post('/add_user', data={'username': 'assistant_admin', 'password': 'Secret123', 'role': 'admin'})
    with app.app_context():
        assistant = User.query.filter_by(username='assistant_admin').one()
        assert assistant.can_access_settings is False
    assistant_client = make_csrf_client(app)
    login(assistant_client, 'assistant_admin')
    assert assistant_client.get('/settings').status_code == 302
    assert assistant_client.get('/settings/sessions').status_code == 302
    assert assistant_client.post('/edit_user_permissions/1', data={'role': 'user'}).status_code == 302
    with app.app_context():
        assert db.session.get(User, 1).role == 'admin'
