"""
Admin navigation: Users, Audit log and Config repo are separate pages, and
the Config repo page says what the app needs from NetForge and which
projects lack newer skeleton files.
"""

import os

from app.models import User
from app.project import create_project
from app.utils import add_project_hosts, ensure_workspace


def test_sidebar_has_each_admin_page(auth_client):
    body = auth_client.get('/projects').get_data(as_text=True)
    for href, label in (('/admin', 'Users'), ('/admin/audit', 'Audit log'),
                        ('/admin/repo', 'Config repo')):
        assert 'href="%s"' % href in body and label in body
    assert 'Account' in body and '>\n        Settings' not in body


def test_users_page_is_just_users(auth_client):
    body = auth_client.get('/admin').get_data(as_text=True)
    assert 'Create user' in body
    assert 'pull-btn' not in body and 'View audit log' not in body


def test_non_admins_see_none_of_it(app, client):
    User.create('plain', 'password1', app)
    client.post('/login', data={'username': 'plain', 'password': 'password1'})
    assert client.get('/admin/repo').status_code == 403
    assert 'Config repo' not in client.get('/projects').get_data(as_text=True)


def test_repo_page_reports_playbooks_and_skeleton(app, auth_client, skeleton_dir):
    pb = os.path.join(app.config['CONFIGGEN_REPO'], 'playbooks')
    os.makedirs(pb)
    open(os.path.join(pb, 'generate_configs.yml'), 'w').close()
    body = auth_client.get('/admin/repo').get_data(as_text=True)
    assert 'pull-btn' in body
    assert body.count('>found<') == 1 and body.count('>missing<') == 3
    assert 'playbooks/firmware_upgrade.yml' in body
    from tests.conftest import SKELETON_FILES
    assert '%d files' % len(SKELETON_FILES) in body


def test_repo_page_lists_projects_missing_skeleton_files(app, auth_client, skeleton_dir):
    ensure_workspace(app, 'admin')
    create_project(app, 'admin', 'old')
    add_project_hosts(app, 'admin', 'old', ['SW1', 'SW2'], 'none')
    body = auth_client.get('/admin/repo').get_data(as_text=True)
    assert 'Every project has the current skeleton files' in body

    # NetForge adds a new file after the project was made.
    with open(os.path.join(skeleton_dir, 'ntp_extra.yml'), 'w') as f:
        f.write('x: 1\n')
    body = auth_client.get('/admin/repo').get_data(as_text=True)
    assert 'admin / old' in body and '/projects/admin/old/hosts' in body
