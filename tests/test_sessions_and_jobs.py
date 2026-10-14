"""
Session invalidation on password change, and job visibility.

A password change used to leave every existing session valid, so a stolen
cookie outlived the reset. Job status was readable by anyone logged in who
had the job id, and generate/deploy/firmware output carries configs and
switch details.
"""

import json
import sqlite3
from unittest.mock import patch

import pytest

from app.models import User, init_db


def _login(app, username, password):
    c = app.test_client()
    c.post('/login', data={'username': username, 'password': password})
    return c


def _logged_in(client):
    return client.get('/projects').status_code == 200


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

class TestPasswordChangeEndsSessions:

    @pytest.fixture
    def two_sessions(self, app):
        with app.app_context():
            User.create('carol', 'oldpassword', app)
        return _login(app, 'carol', 'oldpassword'), _login(app, 'carol', 'oldpassword')

    def test_other_sessions_are_signed_out(self, app, two_sessions):
        mine, stolen = two_sessions
        assert _logged_in(mine) and _logged_in(stolen)
        res = mine.post('/account', data={
            'current_password': 'oldpassword',
            'new_password': 'newpassword',
            'confirm_password': 'newpassword'})
        assert b'Password changed' in res.data
        assert not _logged_in(stolen)

    def test_the_changing_session_stays_signed_in(self, app, two_sessions):
        mine, _ = two_sessions
        mine.post('/account', data={
            'current_password': 'oldpassword',
            'new_password': 'newpassword',
            'confirm_password': 'newpassword'})
        assert _logged_in(mine)

    def test_failed_change_signs_nobody_out(self, app, two_sessions):
        mine, other = two_sessions
        mine.post('/account', data={
            'current_password': 'wrong',
            'new_password': 'newpassword',
            'confirm_password': 'newpassword'})
        assert _logged_in(mine) and _logged_in(other)


class TestSessionIds:

    def test_pre_upgrade_session_still_loads(self, app):
        """A cookie from before versioning holds a bare id."""
        with app.app_context():
            User.create('dave', 'password1', app)
            uid = User.get_by_username('dave', app).id
            assert User.from_session_id(str(uid), app).username == 'dave'

    def test_pre_upgrade_session_dies_after_a_password_change(self, app):
        with app.app_context():
            User.create('dave', 'password1', app)
            uid = User.get_by_username('dave', app).id
            User.change_password(uid, 'password2', app)
            assert User.from_session_id(str(uid), app) is None

    @pytest.mark.parametrize('value', ['', 'x', '1:x', ':1', '1:2:3'])
    def test_malformed_ids_load_nobody(self, app, value):
        with app.app_context():
            User.create('dave', 'password1', app)
            assert User.from_session_id(value, app) is None

    def test_existing_database_gains_the_column(self, app, tmp_path):
        db_path = tmp_path / 'old.db'
        con = sqlite3.connect(db_path)
        con.execute('CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                    'username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, '
                    'is_admin INTEGER NOT NULL DEFAULT 0)')
        con.execute("INSERT INTO users (username, password) VALUES ('old', 'h')")
        con.commit()
        con.close()

        app.config['DATABASE'] = str(db_path)
        init_db(app)
        init_db(app)        # and running it again is harmless
        user = User.get_by_username('old', app)
        assert user.session_version == 0


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@pytest.fixture
def other_user(app):
    with app.app_context():
        User.create('mallory', 'password1', app)
    return _login(app, 'mallory', 'password1')


class TestJobVisibility:

    def test_visible_job_strips_owner_fields(self):
        from app.security import job_tag, visible_job
        job = {'status': 'done', **job_tag('alice', 'p')}
        assert visible_job(job, 'alice', 'p') == {'status': 'done'}

    @pytest.mark.parametrize('user,project', [
        ('bob', 'p'), ('alice', 'other'), (None, 'p')])
    def test_visible_job_hides_from_anyone_else(self, user, project):
        from app.security import job_tag, visible_job
        job = {'status': 'done', **job_tag('alice', 'p')}
        assert visible_job(job, user, project) is None

    def test_untagged_job_is_hidden(self):
        from app.security import visible_job
        assert visible_job({'status': 'done'}, 'alice', 'p') is None

    def _start_deploy(self, app, auth_client):
        from tests.test_deploy import setup_deploy_project
        import app.deploy_routes as dr
        setup_deploy_project(app, auth_client)

        def _fake(job_id, *a, **kw):
            with dr._deploy_jobs_lock:
                dr._deploy_jobs[job_id].update(
                    status='done', returncode=0, results=[],
                    output='hostname CO-CORE-SW-01')

        with patch('app.deploy_routes._run_playbook_job', _fake):
            res = auth_client.post('/projects/deploy-test/deploy/dryrun', json={
                'hosts': [{'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'}],
                'username': 'u', 'password': 'p'})
        return json.loads(res.data)['job_id']

    def test_deploy_job_visible_to_owner(self, app, auth_client):
        job_id = self._start_deploy(app, auth_client)
        res = auth_client.get('/projects/deploy-test/deploy/status/' + job_id)
        assert res.status_code == 200
        assert '_owner' not in json.loads(res.data)

    def test_deploy_job_hidden_from_other_user(self, app, auth_client, other_user):
        job_id = self._start_deploy(app, auth_client)
        res = other_user.get('/projects/deploy-test/deploy/status/' + job_id)
        assert res.status_code == 404

    def test_deploy_job_hidden_under_another_project(self, app, auth_client):
        job_id = self._start_deploy(app, auth_client)
        res = auth_client.get('/projects/elsewhere/deploy/status/' + job_id)
        assert res.status_code == 404

    def test_generate_job_hidden_from_other_user(self, app, auth_client, other_user):
        import app.projects as P
        from app.security import job_tag
        with P._jobs_lock:
            P._jobs['g1'] = {'status': 'done', **job_tag('admin', 'proj')}
        assert auth_client.get('/projects/proj/generate/status/g1').status_code == 200
        assert other_user.get('/projects/proj/generate/status/g1').status_code == 404

    def test_firmware_job_hidden_from_other_user(self, app, auth_client, other_user):
        import app.firmware_routes as F
        from app.security import job_tag
        with F._fw_jobs_lock:
            F._fw_jobs['f1'] = {'status': 'done', **job_tag('admin', 'proj')}
        assert auth_client.get('/projects/proj/firmware/status/f1').status_code == 200
        assert other_user.get('/projects/proj/firmware/status/f1').status_code == 404
