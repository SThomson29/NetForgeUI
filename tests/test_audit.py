"""
Audit log: deploy dry runs, pushes and firmware runs are recorded with who,
what, where and the outcome, and secrets in the pushed config are masked.
"""

import json
import sqlite3
import time
from unittest.mock import patch

import pytest

from app import audit
from tests.test_deploy import setup_deploy_project


SECRET_COMMANDS = [
    'radius-server host 10.0.0.5 key plaintext RADSECRET',
    'snmp-server community COMMSECRET',
    'user admin group administrators password plaintext USERSECRET',
    'tacacs-server host 10.0.0.6 key ciphertext AQBapCIPHER',
    'interface 1/1/1',
    'description <script>alert(1)</script>',
]
SECRETS = ['RADSECRET', 'COMMSECRET', 'USERSECRET', 'AQBapCIPHER']


def _fake_runner(results, output='', rc=0):
    import app.deploy_routes as dr

    def _fake(job_id, *a, **kw):
        with dr._deploy_jobs_lock:
            dr._deploy_jobs[job_id].update(
                status='done' if rc == 0 else 'failed', returncode=rc,
                results=results, output=output)
    return patch('app.deploy_routes._run_playbook_job', _fake)


def _wait_entries(app, n=1, **kw):
    for _ in range(200):
        rows = audit.entries(app, **kw)
        if len(rows) >= n and all(r['status'] != 'running' for r in rows):
            return rows
        time.sleep(0.01)
    raise AssertionError('audit entries did not complete: %r' % audit.entries(app, **kw))


def _raw_rows(app):
    con = sqlite3.connect(app.config['DATABASE'])
    try:
        return con.execute('SELECT * FROM audit_log').fetchall()
    finally:
        con.close()


PUSH_BODY = {
    'hosts': [{'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'}],
    'username': 'netops', 'password': 'SWITCHPASS', 'rollback_timeout': 7,
}


class TestMasking:

    @pytest.mark.parametrize('line,secret', [
        ('radius-server host 1.1.1.1 key plaintext S3cr3t', 'S3cr3t'),
        ('user admin password ciphertext AQBxyz', 'AQBxyz'),
        ('snmp-server community public123', 'public123'),
        ('ip ospf authentication message-digest-key 1 md5 plaintext ok', 'ok'),
        ('+ tacacs-server host 2.2.2.2 key plaintext T4c', 'T4c'),
    ])
    def test_secret_values_are_masked(self, line, secret):
        out = audit.mask_secrets(line)
        assert secret not in out
        assert audit.MASK in out

    def test_ordinary_lines_are_untouched(self):
        line = 'interface 1/1/1\n    ip address 10.0.0.1/31'
        assert audit.mask_secrets(line) == line


class TestDeployIsRecorded:

    def test_dry_run(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        with _fake_runner([{'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1',
                            'changed': True, 'diff': {'prepared': 'whole config'},
                            'commands': SECRET_COMMANDS}],
                          output='+radius-server host 1.1.1.1 key plaintext RADSECRET\n'):
            auth_client.post('/projects/deploy-test/deploy/dryrun', json=dict(
                PUSH_BODY, rollback_timeout=None))
        (e,) = _wait_entries(app)
        assert e['action'] == 'deploy_dryrun'
        assert (e['username'], e['project'], e['switch_user']) == \
            ('admin', 'deploy-test', 'netops')
        assert e['status'] == 'done'
        assert e['outcome'][0] == '1 of 1 with changes'
        t = e['targets'][0]
        assert t['config_file'] == 'CO-CORE-SW-01_FULL.ios'
        assert len(t['sha256']) == 64
        assert 'diff' not in e['results'][0]

    def test_push_records_parameters_and_outcome(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        with _fake_runner([{'hostname': 'CO-CORE-SW-01', 'confirmed': True,
                            'rollback_pending': False, 'checkpoint': 'cp1',
                            'commands': []}]):
            auth_client.post('/projects/deploy-test/deploy/push', json=PUSH_BODY)
        (e,) = _wait_entries(app)
        assert e['action'] == 'deploy_push'
        assert e['params'] == {'rollback_timeout': 7}
        assert e['outcome'] == ('1 of 1 confirmed', 'green')

    def test_rollback_pending_is_flagged(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        with _fake_runner([{'hostname': 'CO-CORE-SW-01', 'confirmed': False,
                            'rollback_pending': True}], rc=2):
            auth_client.post('/projects/deploy-test/deploy/push', json=PUSH_BODY)
        (e,) = _wait_entries(app)
        assert e['outcome'][1] == 'red'
        assert 'rollback pending' in e['outcome'][0]

    def test_no_secret_or_password_is_stored(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        with _fake_runner([{'hostname': 'CO-CORE-SW-01', 'changed': True,
                            'commands': SECRET_COMMANDS}],
                          output='\n'.join('+' + c for c in SECRET_COMMANDS)):
            auth_client.post('/projects/deploy-test/deploy/dryrun', json=PUSH_BODY)
        _wait_entries(app)
        stored = json.dumps(_raw_rows(app))
        for s in SECRETS + ['SWITCHPASS']:
            assert s not in stored, s

    def test_a_runner_crash_is_recorded_as_failed(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        with patch('app.deploy_routes._run_playbook_job',
                   side_effect=RuntimeError('boom')):
            auth_client.post('/projects/deploy-test/deploy/push', json=PUSH_BODY)
        (e,) = _wait_entries(app)
        assert e['status'] == 'failed'
        assert 'boom' in e['output_tail']

    def test_an_audit_failure_does_not_break_the_job(self, app, auth_client):
        from tests.test_deploy import post_and_wait
        setup_deploy_project(app, auth_client)
        with _fake_runner([]), \
                patch('app.audit.record_finish', side_effect=RuntimeError('db')):
            job = post_and_wait(auth_client, '/projects/deploy-test/deploy/push',
                                PUSH_BODY)
        assert job['status'] == 'done'


class TestFirmwareIsRecorded:

    def test_firmware_run(self, app, auth_client, tmp_path, monkeypatch):
        import app.firmware_routes as fr
        fw = tmp_path / 'fw'
        fw.mkdir()
        (fw / 'PL_10_15.swi').write_text('x')
        app.config['FIRMWARE_DIR'] = str(fw)
        repo = tmp_path / 'repo' / 'playbooks'
        repo.mkdir(parents=True)
        (repo / 'firmware_upgrade.yml').write_text('---\n')
        app.config['CONFIGGEN_REPO'] = str(tmp_path / 'repo')
        monkeypatch.setattr(fr, '_load_mapping', lambda a, u, p: {
            'hosts': [{'hostname': 'core-01', 'mgmt_ip': '10.0.0.1'}]})

        def _fake(job_id, *a, **kw):
            with fr._fw_jobs_lock:
                fr._fw_jobs[job_id].update(status='done', returncode=0, results=[
                    {'hostname': 'core-01', 'reachable_after_boot': True}])
        monkeypatch.setattr(fr, '_run_firmware_playbook', _fake)

        res = auth_client.post('/projects/p/firmware/run', json={
            'image': 'PL_10_15.swi', 'partition': 'primary',
            'switch_username': 'netops', 'switch_password': 'SWITCHPASS'})
        assert res.status_code == 200
        (e,) = _wait_entries(app)
        assert e['action'] == 'firmware'
        assert e['params']['image'] == 'PL_10_15.swi'
        assert e['params']['partition'] == 'primary'
        assert e['outcome'] == ('1 of 1 back online', 'green')
        assert 'SWITCHPASS' not in json.dumps(_raw_rows(app))


class TestInterrupted:

    def test_running_entries_are_closed_at_startup(self, app):
        with app.app_context():
            audit.record_start(app, username='u', project='p',
                               action='deploy_push', switch_user='s',
                               targets=[], params={})
            audit.mark_interrupted(app)
            (e,) = audit.entries(app)
        assert e['status'] == 'interrupted'
        assert e['finished_at']


class TestVisibility:

    def _entry(self, app, user, project):
        audit.record_start(app, username=user, project=project,
                           action='deploy_push', switch_user='s',
                           targets=[{'hostname': 'SW-%s' % user}], params={})

    def test_project_page_shows_only_own_entries(self, app, auth_client):
        from app.project import create_project
        with app.app_context():
            create_project(app, 'admin', 'mine')
            self._entry(app, 'admin', 'mine')
            self._entry(app, 'admin', 'other-project')
            self._entry(app, 'mallory', 'mine')
        body = auth_client.get('/projects/mine/history').data.decode()
        assert 'SW-admin' in body
        assert 'SW-mallory' not in body
        assert body.count('class="audit-entry"') == 1

    def test_admin_sees_everyone_and_can_filter(self, app, auth_client):
        with app.app_context():
            self._entry(app, 'admin', 'a')
            self._entry(app, 'mallory', 'b')
        body = auth_client.get('/admin/audit').data.decode()
        assert 'SW-admin' in body and 'SW-mallory' in body
        body = auth_client.get('/admin/audit?user=mallory').data.decode()
        assert 'SW-admin' not in body and 'SW-mallory' in body

    def test_admin_page_is_admin_only(self, app, client):
        from app.models import User
        with app.app_context():
            User.create('plain', 'password1', app)
        client.post('/login', data={'username': 'plain', 'password': 'password1'})
        assert client.get('/admin/audit').status_code == 403

    def test_history_requires_login(self, client):
        assert client.get('/projects/p/history').status_code in (302, 401)

    def test_recorded_text_is_escaped(self, app, auth_client):
        from app.project import create_project
        with app.app_context():
            create_project(app, 'admin', 'esc')
            eid = audit.record_start(app, username='admin', project='esc',
                                     action='deploy_dryrun', switch_user='s',
                                     targets=[{'hostname': 'sw1'}], params={})
            audit.record_finish(app, eid, {'status': 'done', 'returncode': 0,
                                           'results': [{'hostname': 'sw1',
                                                        'commands': SECRET_COMMANDS}],
                                           'output': '<img src=x onerror=alert(1)>'})
        body = auth_client.get('/projects/esc/history').data.decode()
        assert '<script>alert(1)</script>' not in body
        assert '<img src=x' not in body
        assert '&lt;script&gt;' in body
