"""
The project Files page: listing and reading a project's files, and saving
host_vars as YAML through the same gates as the switch editor plus its own.
"""

import hashlib
import os

import pytest

from app import assets, checks, files, sharing
from app.models import User
from app.project import (add_pool, create_project, get_all_allocations,
                         get_project_config, project_generated_configs_dir,
                         project_host_vars_dir)
from app.utils import add_project_hosts, ensure_workspace

P = 'site'
BASE = '/projects/admin/%s' % P


@pytest.fixture
def project(app, auth_client, skeleton_dir):
    ensure_workspace(app, 'admin')
    create_project(app, 'admin', P)
    add_project_hosts(app, 'admin', P, ['SW1', 'SW2'], 'none')
    # A file the switch editor has no form for, as a newer NetForge
    # skeleton would add.
    with open(os.path.join(hvdir(app, 'SW1'), 'spanning_tree.yml'), 'w') as f:
        f.write('# stp\nspanning_tree:\n  mode: mstp\n')
    gcdir = project_generated_configs_dir(app, 'admin', P)
    os.makedirs(gcdir, exist_ok=True)
    with open(os.path.join(gcdir, 'SW1_FULL.ios'), 'w') as f:
        f.write('hostname SW1\n')
    return app


def hvdir(app, host):
    return os.path.join(project_host_vars_dir(app, 'admin', P), host)


def on_disk(app, host, name):
    with open(os.path.join(hvdir(app, host), name)) as f:
        return f.read()


def content(client, **params):
    return client.get(BASE + '/api/files/content', query_string=params)


def save(client, host, name, text, base='current', app=None):
    if base == 'current':
        base = checks.hostvars_fingerprint(hvdir(app, host))
    return client.post(BASE + '/api/files/save',
                       json={'host': host, 'name': name, 'content': text, 'base': base})


class TestTree:

    def test_lists_inventory_switch_files_and_configs(self, project):
        t = files.tree(project, 'admin', P)
        assert t['inventory'] is True and t['configs'] == ['SW1_FULL.ios']
        assert [h['hostname'] for h in t['hosts']] == ['SW1', 'SW2']
        sw1 = {f['name']: f['form'] for f in t['hosts'][0]['files']}
        assert sw1['vlans.yml'] is True and sw1['spanning_tree.yml'] is False

    def test_ignores_links_and_other_file_types(self, project):
        d = hvdir(project, 'SW1')
        os.symlink('/etc/hosts', os.path.join(d, 'linked.yml'))
        open(os.path.join(d, 'notes.txt'), 'w').close()
        open(os.path.join(d, '.hidden.yml'), 'w').close()
        names = [f['name'] for f in files.tree(project, 'admin', P)['hosts'][0]['files']]
        assert not {'linked.yml', 'notes.txt', '.hidden.yml'} & set(names)


class TestRead:

    def test_host_vars(self, project, auth_client):
        data = content(auth_client, kind='hostvars', host='SW1', name='vlans.yml').get_json()
        assert data['content'] == 'vlans: []\n'
        assert (data['editable'], data['form']) == (True, True)
        assert data['fingerprint'] == checks.hostvars_fingerprint(hvdir(project, 'SW1'))

    def test_a_file_without_a_form(self, project, auth_client):
        data = content(auth_client, kind='hostvars', host='SW1', name='spanning_tree.yml').get_json()
        assert (data['editable'], data['form']) == (True, False) and 'mstp' in data['content']

    def test_config_and_inventory_are_read_only(self, project, auth_client):
        cfg = content(auth_client, kind='config', name='SW1_FULL.ios').get_json()
        assert cfg['content'] == 'hostname SW1\n' and cfg['editable'] is False
        inv = content(auth_client, kind='inventory').get_json()
        assert '[cx]' in inv['content'] and inv['editable'] is False

    @pytest.mark.parametrize('params', [
        {'kind': 'hostvars', 'host': '..', 'name': 'vlans.yml'},
        {'kind': 'hostvars', 'host': 'SW9', 'name': 'vlans.yml'},
        {'kind': 'hostvars', 'host': 'SW1', 'name': '../SW2/vlans.yml'},
        {'kind': 'hostvars', 'host': 'SW1', 'name': '../../config.json'},
        {'kind': 'hostvars', 'host': 'SW1', 'name': 'nope.yml'},
        {'kind': 'hostvars', 'host': 'SW1'},
        {'kind': 'config', 'name': '../hosts.ini'},
        {'kind': 'config', 'name': '../../../../users.db'},
        {'kind': 'config', 'name': 'SW9_FULL.ios'},
    ])
    def test_only_files_the_project_holds(self, project, auth_client, params):
        res = content(auth_client, **params)
        assert res.status_code == 404 and res.get_json()['ok'] is False

    def test_unknown_kind(self, project, auth_client):
        assert content(auth_client, kind='database').status_code == 400
        assert content(auth_client).status_code == 400


class TestSave:

    def test_saves_and_returns_the_new_fingerprint(self, project, auth_client):
        text = 'vlans:\n  - id: 10\n    name: users\n'
        res = save(auth_client, 'SW1', 'vlans.yml', text, app=project)
        assert res.status_code == 200
        assert on_disk(project, 'SW1', 'vlans.yml') == text
        assert res.get_json()['fingerprint'] == checks.hostvars_fingerprint(hvdir(project, 'SW1'))

    def test_records_who_saved_the_switch(self, project, auth_client):
        save(auth_client, 'SW1', 'vlans.yml', 'vlans: []\n# x\n', app=project)
        assert get_project_config(project, 'admin', P)['host_saves']['SW1']['by'] == 'admin'

    def test_marks_the_generated_config_out_of_date(self, project, auth_client):
        from app.project import record_generated_from
        record_generated_from(project, 'admin', P, {
            'SW1_FULL.ios': checks.hostvars_fingerprint(hvdir(project, 'SW1'))})
        assert checks.stale_configs(project, 'admin', P) == {}
        save(auth_client, 'SW1', 'vlans.yml', 'vlans:\n  - id: 10\n    name: users\n', app=project)
        assert checks.stale_configs(project, 'admin', P) == {'SW1': ['SW1_FULL.ios']}

    def test_syncs_allocations_like_the_switch_editor(self, project, auth_client):
        add_pool(project, 'admin', P, {'id': 'pool_lo', 'name': 'loopbacks', 'type': 'unique',
                                       'subnet': '10.9.0.0/24', 'prefix': 32})
        text = ('loopback_interfaces:\n  - name: loopback0\n    ip_address: 10.9.0.7\n'
                '    ip_prefix: 32\n')
        assert save(auth_client, 'SW1', 'interfaces.yml', text, app=project).status_code == 200
        held = get_all_allocations(project, 'admin', P)['unique']['pool_lo']
        assert held['10.9.0.7']['hostname'] == 'SW1'

    def test_a_file_without_a_form_can_be_saved(self, project, auth_client):
        text = 'spanning_tree:\n  mode: rpvst\n'
        assert save(auth_client, 'SW1', 'spanning_tree.yml', text, app=project).status_code == 200
        assert on_disk(project, 'SW1', 'spanning_tree.yml') == text

    def test_empty_and_comment_only_are_allowed(self, project, auth_client):
        assert save(auth_client, 'SW1', 'vlans.yml', '', app=project).status_code == 200
        assert save(auth_client, 'SW1', 'vlans.yml', '# nothing yet\n', app=project).status_code == 200

    def test_windows_line_endings_are_normalised(self, project, auth_client):
        save(auth_client, 'SW1', 'vlans.yml', 'vlans:\r\n  - id: 10\r\n    name: a\r\n', app=project)
        assert '\r' not in on_disk(project, 'SW1', 'vlans.yml')

    @pytest.mark.parametrize('text, says', [
        ('vlans: [unclosed\n', 'Not valid YAML (line'),
        ('vlans:\n  - id: 10\n   name: bad indent\n', 'Not valid YAML'),
        ('- id: 10\n', 'not a list'),
        ('just some words\n', 'not a single value'),
        ('a: 1\x00\n', "can't be saved"),
    ])
    def test_refuses_what_generation_could_not_load(self, project, auth_client, text, says):
        before = on_disk(project, 'SW1', 'vlans.yml')
        res = save(auth_client, 'SW1', 'vlans.yml', text, app=project)
        assert res.status_code == 400 and says in res.get_json()['error']
        assert on_disk(project, 'SW1', 'vlans.yml') == before

    @pytest.mark.parametrize('name, text, says', [
        ('interfaces.yml', 'physical_interfaces:\n  a: 1\n  b: 2\n',
         'physical_interfaces must be a list, not nested settings'),
        ('interfaces.yml', 'vlan_interfaces: vlan10\n',
         'vlan_interfaces must be a list, not a single value'),
        ('vsf.yml', 'vsf:\n  - enabled: true\n', 'vsf must be nested settings, not a list'),
        ('vsf.yml', 'vsf:\n  members: 2\n', 'vsf.members must be a list, not a single value'),
        ('general.yml', 'hostname:\n  - SW1\n', 'hostname must be a single value, not a list'),
    ])
    def test_refuses_a_setting_of_the_wrong_kind(self, project, auth_client, name, text, says):
        """A list where the templates expect settings, and so on, going
        by the NetForge skeleton."""
        if name == 'vsf.yml':
            with open(os.path.join(hvdir(project, 'SW1'), name), 'w') as f:
                f.write('vsf:\n  enabled: false\n')
        before = on_disk(project, 'SW1', name)
        res = save(auth_client, 'SW1', name, text, app=project)
        assert res.status_code == 400 and says in res.get_json()['error']
        assert on_disk(project, 'SW1', name) == before

    def test_unset_and_extra_settings_are_not_judged(self, project, auth_client):
        # Empty where the skeleton has a list; a setting the skeleton lacks.
        text = 'physical_interfaces:\nlag_interfaces: []\nsomething_new:\n  - 1\n'
        assert save(auth_client, 'SW1', 'interfaces.yml', text, app=project).status_code == 200

    def test_a_file_the_skeleton_lacks_is_only_checked_as_yaml(self, project, auth_client):
        assert save(auth_client, 'SW1', 'spanning_tree.yml',
                    'physical_interfaces:\n  a: 1\n', app=project).status_code == 200

    def test_refuses_what_the_switch_editor_could_not_read(self, project, auth_client, monkeypatch):
        from app import hostvars
        def boom(_hvdir):
            raise ValueError('cannot read this')
        monkeypatch.setattr(hostvars, '_parse_state', boom)
        before = on_disk(project, 'SW1', 'vlans.yml')
        res = save(auth_client, 'SW1', 'vlans.yml', 'vlans:\n  - id: 10\n', app=project)
        assert res.status_code == 400 and 'switch editor' in res.get_json()['error']
        assert on_disk(project, 'SW1', 'vlans.yml') == before
        # A file that editor never reads isn't held to it.
        assert save(auth_client, 'SW1', 'spanning_tree.yml', 'a: 1\n', app=project).status_code == 200

    def test_refuses_an_oversized_file(self, project, auth_client):
        big = 'a: "%s"\n' % ('x' * (files.MAX_EDIT_BYTES + 1))
        res = save(auth_client, 'SW1', 'vlans.yml', big, app=project)
        assert res.status_code == 400 and 'larger' in res.get_json()['error']

    def test_content_must_be_text(self, project, auth_client):
        res = save(auth_client, 'SW1', 'vlans.yml', {'vlans': []}, app=project)
        assert res.status_code == 400

    @pytest.mark.parametrize('host, name', [
        ('SW1', 'brand_new.yml'), ('SW1', '../SW2/vlans.yml'), ('SW1', '../../config.json'),
        ('SW1', 'hosts.ini'), ('..', 'vlans.yml'), ('SW9', 'vlans.yml'),
    ])
    def test_only_existing_host_vars_files(self, project, auth_client, host, name):
        res = client_save = auth_client.post(BASE + '/api/files/save', json={
            'host': host, 'name': name, 'content': 'a: 1\n', 'base': 'x'})
        assert client_save.status_code == 404
        assert not os.path.exists(os.path.join(hvdir(project, 'SW1'), 'brand_new.yml'))

    def test_refused_if_the_switch_changed_since_it_was_opened(self, project, auth_client):
        opened = checks.hostvars_fingerprint(hvdir(project, 'SW1'))
        save(auth_client, 'SW1', 'vrfs.yml', 'vrfs:\n  - name: A\n', app=project)   # someone else
        res = save(auth_client, 'SW1', 'vlans.yml', 'vlans:\n  - id: 10\n', base=opened)
        data = res.get_json()
        assert res.status_code == 409 and data['conflict'] is True
        assert 'changed by admin' in data['error']
        assert on_disk(project, 'SW1', 'vlans.yml') == 'vlans: []\n'

    def test_refused_without_the_fingerprint_it_was_opened_with(self, project, auth_client):
        res = save(auth_client, 'SW1', 'vlans.yml', 'vlans: []\n', base=None)
        assert res.status_code == 409

    def test_a_save_in_the_switch_editor_also_counts_as_a_change(self, project, auth_client):
        opened = checks.hostvars_fingerprint(hvdir(project, 'SW1'))
        auth_client.post(BASE + '/api/hostvars/SW1/save_all',
                         json=[{'filename': 'vlans.yml', 'content': 'vlans:\n  - id: 5\n'}])
        assert save(auth_client, 'SW1', 'vrfs.yml', 'vrfs: []\n', base=opened).status_code == 409


class TestAccess:

    def login(self, app, name, level):
        User.create(name, 'password1', app)
        sharing.set_share(app, 'admin', P, 'user', name, level, 'admin')
        c = app.test_client()
        c.post('/login', data={'username': name, 'password': 'password1'})
        return c

    def test_view_access_reads_but_cannot_save(self, project):
        c = self.login(project, 'viewer', sharing.VIEW)
        assert 'const CAN_EDIT = false;' in c.get(BASE + '/files').get_data(as_text=True)
        assert content(c, kind='hostvars', host='SW1', name='vlans.yml').status_code == 200
        res = save(c, 'SW1', 'vlans.yml', 'vlans:\n  - id: 10\n', app=project)
        assert res.status_code == 403
        assert on_disk(project, 'SW1', 'vlans.yml') == 'vlans: []\n'

    def test_edit_access_saves(self, project):
        c = self.login(project, 'editor', sharing.EDIT)
        assert 'const CAN_EDIT = true;' in c.get(BASE + '/files').get_data(as_text=True)
        assert save(c, 'SW1', 'vlans.yml', 'vlans:\n  - id: 10\n', app=project).status_code == 200
        assert get_project_config(project, 'admin', P)['host_saves']['SW1']['by'] == 'editor'

    def test_no_access_sees_nothing(self, project):
        User.create('stranger', 'password1', project)
        c = project.test_client()
        c.post('/login', data={'username': 'stranger', 'password': 'password1'})
        assert c.get(BASE + '/files').status_code == 404
        assert content(c, kind='inventory').status_code == 404


class TestPage:

    def test_page_and_sidebar_entry(self, project, auth_client):
        body = auth_client.get(BASE + '/files').get_data(as_text=True)
        assert '${PROJECT_BASE}/files' in body
        assert '/static/vendor/monaco-0.57.0/vs/loader.js' in body
        assert '"spanning_tree.yml"' in body and 'const CAN_EDIT = true;' in body


# The published monaco-editor@0.57.0 files, as on npm.
MONACO = 'vendor/monaco-0.57.0'
MONACO_SHA256 = {
    'vs/loader.js': '35b59df80b41a8b73bf1ceb159b44f040567c2ab53f4e77a3d002d9dc39d55d1',
    'vs/editor/editor.main.js': '60b192eb811710dcc4090fddf63ac5365b46e538dc73c51b6a27c8eaffc48b12',
    'vs/editor-BdtEMBbM.js': 'e1e745003eb416b7ca6a1956a333e6f8434ff48381824858cb63c99e9c8f8acf',
    'vs/assets/editor.worker-_vAIFJDs.js':
        '9605fb8c175da6d0491fed859f874f18d55a1052b52f396b817f66930a2562d3',
    'vs/yaml-C-RIivnc.js': 'ffc2b36910e71fdbf72f551ae60e1b754987f6949a04ce0364c1fd40a85b3468',
    'codicon.ttf': '77f902557231f95c4c71ae517d09a1a842753656f5f665086b3e5ad8095538da',
}


class TestVendoredMonaco:

    @pytest.mark.parametrize('name, digest', sorted(MONACO_SHA256.items()))
    def test_files_are_the_published_builds(self, name, digest):
        with open(os.path.join(assets.STATIC_DIR, MONACO, name), 'rb') as f:
            assert hashlib.sha256(f.read()).hexdigest() == digest

    def test_stylesheet_loads_the_icon_font_from_a_file(self):
        """The one changed file: the policy's font-src blocks data: URIs."""
        with open(os.path.join(assets.STATIC_DIR, MONACO, 'vs/editor/editor.main.css')) as f:
            css = f.read()
        assert 'data:font' not in css and 'url(../../codicon.ttf)' in css

    def test_the_worker_the_page_starts_is_shipped(self):
        with open(os.path.join(os.path.dirname(assets.STATIC_DIR), 'templates',
                               'project_files.html')) as f:
            page = f.read()
        assert "'/assets/editor.worker-_vAIFJDs.js'" in page
