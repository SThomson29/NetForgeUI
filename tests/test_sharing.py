"""
Shared projects: access levels, groups, transfer, editing conflicts and
jobs that mustn't run twice.

The route sweep at the top is the important part — it walks every project
route the app has, so a route added later without the right level is
caught here rather than in production.
"""

import json
import os
import re
import threading
import time
from unittest.mock import patch

import pytest
from flask.testing import FlaskClient

from app import sharing, checks
from app.models import User
from app.project import create_project, project_dir, project_host_vars_dir
from app.utils import add_project_hosts, ensure_workspace


def _login(app, name):
    c = FlaskClient(app, use_cookies=True)
    c.post('/login', data={'username': name, 'password': 'password1'})
    return c


@pytest.fixture
def people(app, skeleton_dir):
    """alice owns 'site'; bob, carol, dave and erin are logged in."""
    with app.app_context():
        for name, admin in (('alice', False), ('bob', False), ('carol', False),
                            ('dave', False), ('erin', True)):
            User.create(name, 'password1', app, is_admin=admin)
            ensure_workspace(app, name)
        create_project(app, 'alice', 'site')
        add_project_hosts(app, 'alice', 'site', ['SW1', 'SW2'], 'none')
    return {n: _login(app, n) for n in ('alice', 'bob', 'carol', 'dave', 'erin')}


def share(app, who, level, kind='user', owner='alice', project='site'):
    sharing.set_share(app, owner, project, kind, who, level, owner)


BASE = '/projects/alice/site'


# ---------------------------------------------------------------------------
# Every route, every level
# ---------------------------------------------------------------------------

def _project_rules(app):
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith('/projects/<owner>/<project_name>'):
            yield rule


def _url(rule):
    url = rule.rule
    for arg, value in (('owner', 'alice'), ('project_name', 'site'), ('hostname', 'SW1'),
                       ('pool_id', 'x'), ('job_id', 'j1'), ('filename', 'SW1_FULL.ios')):
        url = url.replace('<%s>' % arg, value)
    return url


def _call(client, method, url):
    return client.open(url, method=method, json={}, headers={'Origin': 'http://localhost'})


class TestRouteSweep:

    def test_there_are_routes_to_sweep(self, app, people):
        assert len(list(_project_rules(app))) > 40

    def test_strangers_see_nothing(self, app, people):
        for rule in _project_rules(app):
            for method in rule.methods - {'HEAD', 'OPTIONS'}:
                res = _call(people['bob'], method, _url(rule))
                assert res.status_code == 404, (method, rule.rule, res.status_code)

    def test_viewers_cannot_change_anything(self, app, people):
        share(app, 'bob', 'view')
        for rule in _project_rules(app):
            for method in rule.methods - {'HEAD', 'OPTIONS'}:
                res = _call(people['bob'], method, _url(rule))
                wanted = 'projects.' + rule.endpoint.split('.')[-1]
                needs = __import__('app.projects', fromlist=['x']).required_level(wanted, method)
                if needs == sharing.VIEW:
                    assert res.status_code != 403, (method, rule.rule)
                else:
                    assert res.status_code == 403, (method, rule.rule, res.status_code)

    def test_editors_cannot_touch_switches_or_share(self, app, people):
        share(app, 'bob', 'edit')
        touching = {'deploy_dryrun', 'deploy_push', 'firmware_run', 'firmware_versions',
                    'verify_read',
                    'reachability', 'delete', 'share_project', 'unshare_project',
                    'transfer_project'}
        for rule in _project_rules(app):
            for method in rule.methods - {'HEAD', 'OPTIONS', 'GET'}:
                res = _call(people['bob'], method, _url(rule))
                name = rule.endpoint.split('.')[-1]
                if name in touching:
                    assert res.status_code == 403, (method, rule.rule)
                else:
                    assert res.status_code != 403, (method, rule.rule, res.status_code)

    def test_deployers_can_touch_switches_but_not_share(self, app, people):
        share(app, 'bob', 'deploy')
        owner_only = {'delete', 'share_project', 'unshare_project', 'transfer_project'}
        for rule in _project_rules(app):
            for method in rule.methods - {'HEAD', 'OPTIONS', 'GET'}:
                res = _call(people['bob'], method, _url(rule))
                name = rule.endpoint.split('.')[-1]
                assert (res.status_code == 403) == (name in owner_only), (method, rule.rule)

    def test_json_callers_get_a_reason(self, app, people):
        share(app, 'bob', 'view')
        res = people['bob'].post(BASE + '/api/pools', json={})
        assert res.status_code == 403
        assert 'view access' in res.get_json()['error']


# ---------------------------------------------------------------------------
# Who has what
# ---------------------------------------------------------------------------

class TestLevels:

    def test_owner(self, app, people):
        assert sharing.level_for(app, User.get_by_username('alice', app), 'alice', 'site') == 'owner'

    def test_best_of_user_and_group(self, app, people):
        bob = User.get_by_username('bob', app)
        bob.groups = ['net-team']
        share(app, 'bob', 'view')
        share(app, 'net-team', 'deploy', kind='group')
        assert sharing.level_for(app, bob, 'alice', 'site') == 'deploy'

    def test_group_membership_comes_from_the_last_sso_sign_in(self, app, people):
        share(app, 'net-team', 'edit', kind='group')
        bob = User.get_by_username('bob', app)
        assert sharing.level_for(app, bob, 'alice', 'site') is None
        User.record_sso_login(bob.id, sub='b', email='b@x.com', groups={'net-team'},
                              sso_admin=False, when='2026-01-01T00:00:00Z', app=app)
        bob = User.get_by_username('bob', app)
        assert sharing.level_for(app, bob, 'alice', 'site') == 'edit'
        assert people['bob'].get(BASE + '/hosts').status_code == 200

    def test_admin_can_view_anything_but_not_change_it(self, app, people):
        assert people['erin'].get(BASE + '/editor').status_code == 200
        assert people['erin'].post(BASE + '/api/pools', json={}).status_code == 403

    def test_bad_shares_are_refused(self, app, people):
        for args in (('user', 'nobody', 'view'), ('user', 'alice', 'view'),
                     ('user', 'bob', 'admin'), ('team', 'bob', 'view'), ('user', '', 'view')):
            with pytest.raises(sharing.SharingError):
                sharing.set_share(app, 'alice', 'site', args[0], args[1], args[2], 'alice')

    def test_resharing_changes_the_level(self, app, people):
        share(app, 'bob', 'view')
        share(app, 'bob', 'deploy')
        (s,) = sharing.shares_for(app, 'alice', 'site')
        assert s['level'] == 'deploy'


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

class TestPages:

    def test_projects_page_lists_shared(self, app, people):
        share(app, 'bob', 'edit')
        body = people['bob'].get('/projects').get_data(as_text=True)
        assert 'Shared with you' in body and 'alice · Edit access' in body
        assert '/projects/alice/site/hosts' in body
        assert 'projects/alice/site/delete' not in body

    def test_admin_sees_everyone_elses(self, app, people):
        body = people['erin'].get('/projects').get_data(as_text=True)
        assert "Other users' projects (1)" in body
        assert '/projects/alice/site/hosts' in body

    def test_links_inside_a_shared_project_keep_the_owner(self, app, people):
        share(app, 'bob', 'edit')
        body = people['bob'].get(BASE + '/hosts').get_data(as_text=True)
        assert 'const PROJECT_BASE = "/projects/alice/site"' in body
        assert 'action="/projects/alice/site/hosts/add"' in body
        assert 'Shared by alice' in body

    def test_sharing_page_for_owner_and_member(self, app, people):
        share(app, 'bob', 'view')
        owner = people['alice'].get(BASE + '/sharing').get_data(as_text=True)
        member = people['bob'].get(BASE + '/sharing').get_data(as_text=True)
        assert 'Transfer ownership' in owner and 'action="/projects/alice/site/sharing/add"' in owner
        assert 'bob' in member and 'sharing/add' not in member

    def test_share_and_unshare_through_the_page(self, app, people):
        people['alice'].post(BASE + '/sharing/add',
                             data={'kind': 'user', 'principal': 'Bob', 'level': 'edit'})
        assert sharing.shares_for(app, 'alice', 'site')[0]['principal'] == 'bob'
        people['alice'].post(BASE + '/sharing/remove', data={'kind': 'user', 'principal': 'bob'})
        assert sharing.shares_for(app, 'alice', 'site') == []
        assert people['bob'].get(BASE + '/hosts').status_code == 404


# ---------------------------------------------------------------------------
# Transfer and deletion
# ---------------------------------------------------------------------------

class TestOwnership:

    def test_transfer_moves_everything(self, app, people):
        from app import audit
        share(app, 'carol', 'view')
        audit.record_start(app, username='alice', project='site', action='generate',
                           switch_user=None, targets=[], params={})
        res = people['alice'].post(BASE + '/sharing/transfer', data={'new_owner': 'bob'})
        assert res.headers['Location'].endswith('/projects/bob/site/sharing')
        assert os.path.isdir(project_dir(app, 'bob', 'site'))
        assert not os.path.exists(project_dir(app, 'alice', 'site'))
        shares = {(s['principal'], s['level']) for s in sharing.shares_for(app, 'bob', 'site')}
        assert shares == {('carol', 'view'), ('alice', 'edit')}
        assert {e['owner'] for e in audit.entries(app, owner='bob', project='site')} == {'bob'}
        assert people['alice'].get('/projects/bob/site/hosts').status_code == 200

    def test_transfer_to_the_current_member_drops_their_share(self, app, people):
        share(app, 'bob', 'edit')
        people['alice'].post(BASE + '/sharing/transfer', data={'new_owner': 'bob'})
        assert {s['principal'] for s in sharing.shares_for(app, 'bob', 'site')} == {'alice'}

    def test_transfer_refuses_a_name_clash(self, app, people):
        create_project(app, 'bob', 'site')
        body = people['alice'].post(BASE + '/sharing/transfer', data={'new_owner': 'bob'},
                                    follow_redirects=True).get_data(as_text=True)
        assert 'already has a project called site' in body
        assert os.path.isdir(project_dir(app, 'alice', 'site'))

    def test_admin_can_transfer(self, app, people):
        res = people['erin'].post(BASE + '/sharing/transfer', data={'new_owner': 'bob'})
        assert res.status_code == 302 and os.path.isdir(project_dir(app, 'bob', 'site'))

    def test_deleting_a_project_removes_its_shares(self, app, people):
        share(app, 'bob', 'edit')
        people['alice'].post(BASE + '/delete')
        assert sharing.shares_for(app, 'alice', 'site') == []

    def test_owner_of_shared_projects_cannot_be_deleted(self, app, people):
        share(app, 'bob', 'edit')
        alice = User.get_by_username('alice', app)
        body = people['erin'].post('/admin/delete/%d' % alice.id,
                                   follow_redirects=True).get_data(as_text=True)
        assert 'owns shared projects (site)' in body
        assert User.get_by_username('alice', app) is not None

    def test_deleting_a_member_removes_their_shares(self, app, people):
        share(app, 'bob', 'edit')
        people['erin'].post('/admin/delete/%d' % User.get_by_username('bob', app).id)
        assert sharing.shares_for(app, 'alice', 'site') == []


# ---------------------------------------------------------------------------
# Two people, one switch
# ---------------------------------------------------------------------------

GENERAL = {'filename': 'general.yml', 'content': 'hostname: SW1\n'}


class TestEditConflicts:

    def _load(self, client):
        res = client.get(BASE + '/api/hostvars/SW1/state')
        assert res.status_code == 200
        return res.headers['X-Hostvars-Fingerprint']

    def _save(self, client, fp, content='hostname: SW1\n'):
        headers = {'X-Base-Fingerprint': fp} if fp is not None else {}
        return client.post(BASE + '/api/hostvars/SW1/save_all', headers=headers,
                           json=[dict(GENERAL, content=content)])

    def test_second_save_from_a_stale_load_is_refused(self, app, people):
        share(app, 'bob', 'edit')
        fp_alice, fp_bob = self._load(people['alice']), self._load(people['bob'])
        assert self._save(people['bob'], fp_bob, 'hostname: SW1\ntimezone: UTC\n').status_code == 200
        res = self._save(people['alice'], fp_alice)
        assert res.status_code == 409
        data = res.get_json()
        assert data['conflict'] and 'changed by bob at' in data['error']
        hv = os.path.join(project_host_vars_dir(app, 'alice', 'site'), 'SW1', 'general.yml')
        with open(hv) as f:
            assert 'timezone: UTC' in f.read()

    def test_saving_again_after_your_own_save(self, app, people):
        fp = self._load(people['alice'])
        res = self._save(people['alice'], fp, 'hostname: SW1\ntimezone: A\n')
        fp2 = res.headers['X-Hostvars-Fingerprint']
        assert fp2 and fp2 != fp
        assert self._save(people['alice'], fp2, 'hostname: SW1\ntimezone: B\n').status_code == 200

    def test_no_header_saves_as_before(self, app, people):
        assert self._save(people['alice'], None).status_code == 200

    def test_fingerprint_matches_the_checks_one(self, app, people):
        hv = os.path.join(project_host_vars_dir(app, 'alice', 'site'), 'SW1')
        assert self._load(people['alice']) == checks.hostvars_fingerprint(hv)


# ---------------------------------------------------------------------------
# Jobs that mustn't overlap
# ---------------------------------------------------------------------------

class TestOneAtATime:

    def test_one_generation_per_project(self, app, people):
        import app.docker_runner as D
        share(app, 'bob', 'edit')
        started, release = threading.Event(), threading.Event()

        def slow(**kw):
            started.set()
            release.wait(5)
            return 0, ''

        with patch.object(D, 'run_generate', slow):
            first = people['alice'].post(BASE + '/generate/run', json={})
            assert first.status_code == 200 and started.wait(5)
            second = people['bob'].post(BASE + '/generate/run', json={})
            assert second.status_code == 409
            assert 'started by alice' in second.get_json()['error']
            release.set()
            for _ in range(200):
                again = people['bob'].post(BASE + '/generate/run', json={})
                if again.status_code == 200:
                    break
                time.sleep(0.02)
            assert again.status_code == 200

    def test_other_projects_are_not_held_up(self, app, people):
        import app.projects as P
        from app.security import job_tag
        with P._jobs_lock:
            P._jobs['busy'] = {'status': 'running', **job_tag('bob', 'bob/other')}
        import app.docker_runner as D
        with patch.object(D, 'run_generate', lambda **kw: (0, '')):
            assert people['alice'].post(BASE + '/generate/run', json={}).status_code == 200
        with P._jobs_lock:
            del P._jobs['busy']

    def test_push_and_firmware_wait_for_a_busy_switch(self, app, people):
        import app.deploy_routes as dr
        import app.firmware_routes as fr
        from app.security import job_tag
        with dr._deploy_jobs_lock:
            dr._deploy_jobs['p1'] = {'status': 'running', 'playbook': 'deploy_push.yml',
                                     '_hosts': ['SW1'], **job_tag('bob', 'alice/site')}
        try:
            msg = dr.switches_in_use('alice/site', ['SW1', 'SW2'])
            assert msg and 'SW1 (started by bob)' in msg and 'SW2' not in msg
            assert dr.switches_in_use('alice/site', ['SW2']) is None
            assert dr.switches_in_use('carol/site', ['SW1']) is None
            with fr._fw_jobs_lock:
                fr._fw_jobs['f1'] = {'status': 'starting', '_hosts': ['SW2'],
                                     **job_tag('carol', 'alice/site')}
            assert 'SW2 (started by carol)' in dr.switches_in_use('alice/site', ['SW2'])
        finally:
            dr._deploy_jobs.pop('p1', None)
            fr._fw_jobs.pop('f1', None)

    def test_dry_runs_never_block(self, app, people):
        import app.deploy_routes as dr
        from app.security import job_tag
        with dr._deploy_jobs_lock:
            dr._deploy_jobs['d1'] = {'status': 'running', 'playbook': 'deploy_dryrun.yml',
                                     '_hosts': ['SW1'], **job_tag('bob', 'alice/site')}
        try:
            assert dr.switches_in_use('alice/site', ['SW1']) is None
        finally:
            dr._deploy_jobs.pop('d1', None)

    def test_push_route_refuses_a_busy_switch(self, app, people):
        import app.deploy_routes as dr
        from app.security import job_tag
        gc = os.path.join(project_dir(app, 'alice', 'site'), 'generated_configs')
        with open(os.path.join(gc, 'SW1_FULL.ios'), 'w') as f:
            f.write('hostname SW1\n')
        with dr._deploy_jobs_lock:
            dr._deploy_jobs['p2'] = {'status': 'running', 'playbook': 'deploy_push.yml',
                                     '_hosts': ['SW1'], **job_tag('alice', 'alice/site')}
        try:
            res = people['alice'].post(BASE + '/deploy/push', json={
                'hosts': [{'hostname': 'SW1', 'mgmt_ip': '10.0.0.1'}],
                'username': 'u', 'password': 'p'})
            assert res.status_code == 409 and 'Already being changed' in res.get_json()['error']
        finally:
            dr._deploy_jobs.pop('p2', None)


# ---------------------------------------------------------------------------
# History and jobs record who acted, in whose project
# ---------------------------------------------------------------------------

class TestHistory:

    def test_member_generation_lands_in_the_owners_history(self, app, people):
        import app.docker_runner as D
        from app import audit
        share(app, 'bob', 'edit')
        with patch.object(D, 'run_generate', lambda **kw: (0, '')):
            people['bob'].post(BASE + '/generate/run', json={})
        for _ in range(200):
            rows = audit.entries(app, owner='alice', project='site')
            if rows and rows[0]['status'] != 'running':
                break
            time.sleep(0.01)
        (e,) = rows
        assert (e['username'], e['owner']) == ('bob', 'alice')
        body = people['alice'].get(BASE + '/history').get_data(as_text=True)
        assert '· bob' in body

    def test_jobs_stay_with_whoever_started_them(self, app, people):
        import app.projects as P
        from app.security import job_tag
        share(app, 'bob', 'edit')
        with P._jobs_lock:
            P._jobs['g9'] = {'status': 'done', **job_tag('bob', 'alice/site')}
        assert people['bob'].get(BASE + '/generate/status/g9').status_code == 200
        assert people['alice'].get(BASE + '/generate/status/g9').status_code == 404

    def test_old_audit_rows_gain_an_owner(self, app, tmp_path):
        import sqlite3
        from app import audit
        db = tmp_path / 'old.db'
        con = sqlite3.connect(db)
        con.execute('CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                    'started_at TEXT NOT NULL, finished_at TEXT, username TEXT NOT NULL, '
                    'project TEXT NOT NULL, action TEXT NOT NULL, switch_user TEXT, '
                    'targets TEXT NOT NULL, params TEXT NOT NULL, status TEXT NOT NULL, '
                    'returncode INTEGER, results TEXT, output_tail TEXT)')
        con.execute("INSERT INTO audit_log (started_at, username, project, action, targets, "
                    "params, status) VALUES ('2026-01-01T00:00:00Z', 'sam', 'p', 'generate', "
                    "'[]', '{}', 'done')")
        con.commit()
        con.close()
        app.config['DATABASE'] = str(db)
        audit.init_audit(app)
        (e,) = audit.entries(app, owner='sam', project='p')
        assert e['owner'] == 'sam'
