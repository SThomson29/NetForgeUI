"""
Audit log: changes that aren't runs — sharing, transfer, deletion, user and
SSO admin, password changes, firmware images and repo pulls — are recorded
with who did what, and shown in the project's History or on the admin page.
"""

import time
from unittest.mock import patch

from app import audit
from app.models import User
from tests.test_firmware_images import fw, upload            # noqa: F401
from tests.test_sharing import BASE, people, share           # noqa: F401


def _events(app, **kw):
    return [e for e in audit.entries(app, **kw) if e['event']]


def _summaries(app, **kw):
    return [(e['action'], e['params']['summary']) for e in _events(app, **kw)]


class TestProjectEvents:

    def test_share_and_unshare(self, app, people):
        people['alice'].post(BASE + '/sharing/add',
                             data={'kind': 'user', 'principal': 'Bob', 'level': 'deploy'})
        people['alice'].post(BASE + '/sharing/add',
                             data={'kind': 'group', 'principal': 'netops', 'level': 'view'})
        people['alice'].post(BASE + '/sharing/remove', data={'kind': 'user', 'principal': 'bob'})
        assert _summaries(app, owner='alice', project='site') == [
            ('unshare', 'User bob removed'),
            ('share', 'Group netops: View'),
            ('share', 'User bob: Deploy'),
        ]
        e = _events(app, owner='alice', project='site')[0]
        assert e['username'] == 'alice' and e['status'] == 'done'

    def test_a_refused_share_is_not_recorded(self, app, people):
        people['alice'].post(BASE + '/sharing/add',
                             data={'kind': 'user', 'principal': 'nobody', 'level': 'edit'})
        assert _events(app) == []

    def test_transfer_is_in_the_new_owners_history(self, app, people):
        people['erin'].post(BASE + '/sharing/transfer', data={'new_owner': 'bob'})
        [e] = _events(app, owner='bob', project='site')
        assert e['action'] == 'transfer' and e['username'] == 'erin'
        assert e['params']['summary'] == 'From alice to bob'
        body = people['bob'].get('/projects/bob/site/history').get_data(as_text=True)
        assert 'Transferred' in body and 'From alice to bob' in body

    def test_delete(self, app, people):
        people['alice'].post(BASE + '/delete')
        assert _summaries(app) == [('project_delete', 'Deleted site')]
        assert _events(app)[0]['owner'] == 'alice'

    def test_members_see_sharing_changes_in_history(self, app, people):
        share(app, 'bob', 'view')
        people['alice'].post(BASE + '/sharing/add',
                             data={'kind': 'user', 'principal': 'carol', 'level': 'edit'})
        body = people['bob'].get(BASE + '/history').get_data(as_text=True)
        assert 'User carol: Edit' in body


class TestAdminEvents:

    def test_user_create_and_delete(self, app, auth_client):
        auth_client.post('/admin/create', data={'username': 'newbie', 'password': 'password1',
                                                'is_admin': 'on'})
        uid = User.get_by_username('newbie', app).id
        auth_client.post('/admin/delete/%d' % uid)
        assert _summaries(app) == [('user_delete', 'newbie'),
                                   ('user_create', 'newbie (admin)')]
        assert all(e['project'] == '' and e['owner'] == '' for e in _events(app))

    def test_refused_create_is_not_recorded(self, app, auth_client):
        auth_client.post('/admin/create', data={'username': 'x', 'password': 'short'})
        assert _events(app) == []

    def test_sso_link_and_unlink(self, app, auth_client):
        User.create('local1', 'password1', app)
        uid = User.get_by_username('local1', app).id
        auth_client.post('/admin/link/%d' % uid, data={'email': 'l1@example.com'})
        auth_client.post('/admin/unlink/%d' % uid)
        assert _summaries(app) == [
            ('sso_unlink', 'local1'),
            ('sso_link', 'local1 will link to l1@example.com at next SSO sign-in'),
        ]

    def test_password_change_records_no_password(self, app, auth_client):
        auth_client.post('/account', data={'current_password': 'testpass',
                                           'new_password': 'NewSecret99',
                                           'confirm_password': 'NewSecret99'})
        [e] = _events(app)
        assert e['action'] == 'password_change' and e['username'] == 'admin'
        assert 'NewSecret99' not in repr(e) and 'testpass' not in repr(e)

    def test_firmware_upload_and_delete(self, app, auth_client, fw):  # noqa: F811
        assert upload(auth_client, 'FL_10_13_1005.swi', b'x' * 100).status_code == 200
        auth_client.post('/admin/firmware/delete', data={'name': 'FL_10_13_1005.swi'})
        [deleted, uploaded] = _events(app)
        assert deleted['action'] == 'firmware_delete'
        assert uploaded['params']['summary'] == 'FL_10_13_1005.swi'
        assert uploaded['params']['version'] == '10.13.1005'
        assert len(uploaded['params']['sha256']) == 64

    def test_admin_events_only_on_the_admin_page(self, app, people):
        people['erin'].post('/admin/create', data={'username': 'zed', 'password': 'password1'})
        admin = people['erin'].get('/admin/audit').get_data(as_text=True)
        assert 'User created' in admin and 'zed' in admin
        history = people['alice'].get(BASE + '/history').get_data(as_text=True)
        assert 'User created' not in history

    def test_event_text_is_escaped(self, app, people):
        audit.record_event(app, username='alice', action='share', owner='alice',
                           project='site', summary='<script>alert(1)</script>',
                           note='<img src=x>')
        body = people['alice'].get(BASE + '/history').get_data(as_text=True)
        assert '<script>alert(1)' not in body and '<img src=x>' not in body
        assert '&lt;script&gt;' in body


class TestRepoPull:

    def _pull(self, app, auth_client, tmp_path, stdout, rc):
        import subprocess
        app.config['CONFIGGEN_REPO'] = str(tmp_path / 'repo')
        app.config['CONFIGGEN_REPO_URL'] = 'https://user:tok123@git.example/x.git'
        done = subprocess.CompletedProcess([], rc, stdout=stdout)
        with patch('app.admin.subprocess.run', return_value=done), \
                patch('app.admin.get_git_env', return_value={}):
            assert auth_client.post('/admin/pull').get_json()['ok']
            for _ in range(200):
                [e] = audit.entries(app)
                if e['status'] != 'running':
                    return e
                time.sleep(0.01)
        raise AssertionError('pull never finished')

    def test_recorded_with_output_and_credentials_masked(self, app, auth_client, tmp_path):
        e = self._pull(app, auth_client, tmp_path,
                       'Cloning into https://user:tok123@git.example/x.git\n', 0)
        assert e['action'] == 'repo_pull' and e['username'] == 'admin'
        assert e['outcome'] == ('Pulled', 'green')
        assert 'tok123' not in e['output_tail'] and '********@git.example' in e['output_tail']

    def test_failure(self, app, auth_client, tmp_path):
        e = self._pull(app, auth_client, tmp_path, 'fatal: nope\n', 128)
        assert e['status'] == 'failed' and e['outcome'] == ('Failed', 'red')
        assert e['returncode'] == 128
