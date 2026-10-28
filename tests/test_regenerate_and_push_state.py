"""
Generate offers to regenerate switches changed since their full config was
generated; Deploy ticks switches whose config is new or has changed since
their last confirmed push.
"""

import os

import pytest
import yaml

from app import audit, checks
from app.project import (create_project, project_dir, project_generated_configs_dir,
                         project_host_vars_dir, record_generated_from)
from app.utils import add_project_hosts, ensure_workspace

P = 'site'


def gcdir(app):
    d = project_generated_configs_dir(app, 'admin', P)
    os.makedirs(d, exist_ok=True)
    return d


def generate(app, host, name=None, content='hostname X\n'):
    """As if a generation wrote host's config from its current settings."""
    name = name or '%s_FULL.ios' % host
    with open(os.path.join(gcdir(app), name), 'w') as f:
        f.write(content)
    fp = checks.hostvars_fingerprint(os.path.join(project_host_vars_dir(app, 'admin', P), host))
    record_generated_from(app, 'admin', P, {name: fp})


def change(app, host):
    path = os.path.join(project_host_vars_dir(app, 'admin', P), host, 'general.yml')
    with open(path, 'a') as f:
        f.write('# edited\n')


@pytest.fixture
def project(app, auth_client, skeleton_dir):
    ensure_workspace(app, 'admin')
    create_project(app, 'admin', P)
    add_project_hosts(app, 'admin', P, ['SW1', 'SW2', 'SW3'], 'none')
    return app


class TestStaleConfigs:

    def test_only_changed_switches(self, project):
        for h in ('SW1', 'SW2', 'SW3'):
            generate(project, h)
        change(project, 'SW2')
        assert checks.stale_configs(project, 'admin', P) == {'SW2': ['SW2_FULL.ios']}

    def test_never_generated_is_not_stale(self, project):
        change(project, 'SW1')
        assert checks.stale_configs(project, 'admin', P) == {}

    def test_check_still_reports_them(self, project):
        generate(project, 'SW1')
        change(project, 'SW1')
        found = [f for f in checks.run(project, 'admin', P) if f['check'] == 'stale_config']
        assert [f['hosts'] for f in found] == [['SW1']]


class TestGeneratePage:

    def test_offers_the_changed_switches(self, project, auth_client):
        for h in ('SW1', 'SW2', 'SW3'):
            generate(project, h)
        change(project, 'SW1')
        change(project, 'SW3')
        body = auth_client.get('/projects/admin/%s/generate' % P).get_data(as_text=True)
        assert '2 switches have changed since their configs were generated' in body
        assert 'const STALE_HOSTS = ["SW1", "SW3"];' in body
        assert 'Regenerate these' in body

    def test_nothing_offered_when_current(self, project, auth_client):
        generate(project, 'SW1')
        body = auth_client.get('/projects/admin/%s/generate' % P).get_data(as_text=True)
        assert 'stale-card' not in body and 'const STALE_HOSTS = [];' in body

    def test_partial_only_is_mentioned_not_offered(self, project, auth_client):
        generate(project, 'SW1')
        generate(project, 'SW2', name='SW2_PARTIAL_vlans.ios')
        change(project, 'SW2')
        body = auth_client.get('/projects/admin/%s/generate' % P).get_data(as_text=True)
        assert 'stale-card' not in body
        assert 'Partial configs are out of date for SW2' in body

    def test_view_access_sees_it_but_no_button(self, project, auth_client):
        from app import sharing
        from app.models import User
        generate(project, 'SW1')
        change(project, 'SW1')
        User.create('viewer', 'password1', project)
        sharing.set_share(project, 'admin', P, 'user', 'viewer', sharing.VIEW, 'admin')
        c = project.test_client()
        c.post('/login', data={'username': 'viewer', 'password': 'password1'})
        body = c.get('/projects/admin/%s/generate' % P).get_data(as_text=True)
        assert '1 switch has changed' in body and 'id="stale-btn"' not in body


class TestDeployPushState:

    @pytest.fixture
    def deploy(self, project):
        with open(os.path.join(project_dir(project, 'admin', P), 'deploy_mapping.yml'), 'w') as f:
            yaml.safe_dump({'hosts': [{'hostname': h, 'mgmt_ip': '10.0.0.%d' % i}
                                      for i, h in enumerate(('SW1', 'SW2', 'SW3'), 1)]}, f)
        for h in ('SW1', 'SW2'):
            generate(project, h, content='config for %s v1\n' % h)
        return project

    def push(self, app, hosts, confirmed=True):
        targets = audit.deploy_targets([{'hostname': h} for h in hosts], gcdir(app))
        entry = audit.record_start(app, username='admin', owner='admin', project=P,
                                   action='deploy_push', switch_user='netops',
                                   targets=targets, params={})
        audit.record_finish(app, entry, {'status': 'done', 'returncode': 0, 'results': [
            {'hostname': h, 'confirmed': confirmed} for h in hosts]})

    def states(self, client):
        import re
        body = client.get('/projects/admin/%s/deploy' % P).get_data(as_text=True)
        rows = re.findall(r'data-hostname="(\w+)" data-push-state="(\w+)">\s*'
                          r'<td><input type="checkbox" class="map-check" ?(checked)?>', body)
        return {h: (state, bool(checked)) for h, state, checked in rows}, body

    def test_never_pushed_is_ticked(self, deploy, auth_client):
        states, _ = self.states(auth_client)
        assert states == {'SW1': ('never', True), 'SW2': ('never', True), 'SW3': ('none', False)}

    def test_same_as_last_push_is_not_ticked(self, deploy, auth_client):
        self.push(deploy, ['SW1', 'SW2'])
        states, body = self.states(auth_client)
        assert states['SW1'] == ('same', False) and states['SW2'] == ('same', False)
        assert 'Same as last push' in body
        assert '0 switches with a new or changed config are ticked' in body

    def test_regenerated_since_push_is_ticked(self, deploy, auth_client):
        self.push(deploy, ['SW1', 'SW2'])
        generate(deploy, 'SW2', content='config for SW2 v2\n')
        states, body = self.states(auth_client)
        assert states['SW1'] == ('same', False) and states['SW2'] == ('changed', True)
        assert 'Changed since last push' in body

    def test_unconfirmed_push_does_not_count(self, deploy, auth_client):
        self.push(deploy, ['SW1'], confirmed=False)
        assert self.states(auth_client)[0]['SW1'] == ('never', True)

    def test_newest_confirmed_push_wins(self, deploy, auth_client):
        self.push(deploy, ['SW1'])
        generate(deploy, 'SW1', content='config for SW1 v2\n')
        self.push(deploy, ['SW1'], confirmed=False)     # rolled back
        assert self.states(auth_client)[0]['SW1'] == ('changed', True)
        self.push(deploy, ['SW1'])
        assert self.states(auth_client)[0]['SW1'] == ('same', False)

    def test_select_changed_button(self, deploy, auth_client):
        _, body = self.states(auth_client)
        assert 'onclick="selectChanged()"' in body and 'function selectChanged()' in body

    def test_other_projects_pushes_dont_count(self, deploy, auth_client):
        create_project(deploy, 'admin', 'other')
        targets = audit.deploy_targets([{'hostname': 'SW1'}], gcdir(deploy))
        entry = audit.record_start(deploy, username='admin', owner='admin', project='other',
                                   action='deploy_push', switch_user='x', targets=targets, params={})
        audit.record_finish(deploy, entry, {'status': 'done', 'returncode': 0,
                                            'results': [{'hostname': 'SW1', 'confirmed': True}]})
        assert self.states(auth_client)[0]['SW1'] == ('never', True)
