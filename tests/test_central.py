"""
Central projects: the design (drawn from and checked against the
collection's schema), the tenant's API client held for the session, and
plan and apply runs.

The collection is a small stand-in written into a temp folder, shaped like
playbooks/tenant_build in samthomson.new_central.
"""

import json
import os
import time

import pytest
import yaml
from flask.testing import FlaskClient

from app import audit, central_design, central_schema, central_session, project_types, sharing
from app.central_schema import MASK, CentralError
from app.models import User
from app.project import create_project, project_dir
from app.utils import ensure_workspace

SCHEMA = {
    'schema_version': 1,
    'label': 'New tenant build',
    'sections': [
        {'id': 'sites', 'stage': 'foundation', 'label': 'Sites', 'playbook': 'sites.yml', 'kind': 'rows',
         'var': 'sites', 'key': 'name', 'fields': [
             {'name': 'name', 'label': 'Site name', 'type': 'text', 'required': True},
             {'name': 'city', 'label': 'City', 'type': 'text'},
             {'name': 'timezone_offset', 'label': 'Offset', 'type': 'number', 'min': -43200, 'max': 50400}]},
        {'id': 'wireless', 'stage': 'configuration', 'label': 'Wireless networks', 'playbook': 'wireless.yml',
         'optional': True, 'needs': ['sites'], 'kind': 'rows', 'var': 'wlans', 'key': 'name',
         'profile_type': 'wlan-ssids', 'fields': [
             {'name': 'name', 'label': 'Profile name', 'type': 'text', 'required': True},
             {'name': 'security', 'label': 'Security', 'type': 'choice', 'choices': ['WPA2_PERSONAL', 'OPEN'],
              'default': 'WPA2_PERSONAL', 'required': True},
             {'name': 'passphrase', 'label': 'Passphrase', 'type': 'secret', 'required': True,
              'show_if': {'security': 'WPA2_PERSONAL'}},
             {'name': 'vlan', 'label': 'VLAN', 'type': 'number', 'min': 1, 'max': 4094, 'required': True},
             {'name': 'enabled', 'label': 'Enabled', 'type': 'toggle', 'default': True},
             {'name': 'device_functions', 'label': 'For', 'type': 'choices', 'choices': ['CAMPUS_AP', 'MICROBRANCH_AP'],
              'default': ['CAMPUS_AP'], 'required': True},
             # A list inside fields is spliced in, as the shared assignment fields are.
             [{'name': 'sites', 'label': 'Sites', 'type': 'ref', 'to': 'sites', 'multiple': True}]],
         'also': [{'var': 'roles', 'label': 'Roles', 'key': 'name', 'fields': [
             {'name': 'name', 'label': 'Role name', 'type': 'text', 'required': True}]}]},
        {'id': 'profiles', 'stage': 'configuration', 'label': 'Any other configuration', 'playbook': 'profiles.yml',
         'optional': True, 'kind': 'profiles', 'var': 'profiles', 'catalogue': 'profiles/index.yml', 'fields': [
             {'name': 'profile_type', 'label': 'Type', 'type': 'choice', 'from': 'catalogue', 'required': True},
             {'name': 'name', 'label': 'Profile name', 'type': 'text'},
             {'name': 'config', 'label': 'Settings', 'type': 'profile', 'required': True},
             {'name': 'servers', 'label': 'Servers', 'type': 'list'}]},
        {'id': 'devices', 'stage': 'devices', 'label': 'Devices', 'playbook': 'devices.yml', 'optional': True,
         'kind': 'rows', 'var': 'devices', 'key': 'serial', 'fields': [
             {'name': 'serial', 'label': 'Serial number', 'type': 'text', 'required': True},
             {'name': 'site', 'label': 'Site', 'type': 'ref', 'to': 'sites'}]},
    ],
}
INDEX = {'profiles': [
    {'type': 'snmp', 'label': 'Snmp', 'category': 'System', 'module': 'config_central_system', 'named': True,
     'secret_keys': ['auth-password'], 'fields': 6},
    {'type': 'lacp', 'label': 'Lacp', 'category': 'Interfaces', 'module': 'config_central_interface', 'named': False,
     'secret_keys': [], 'fields': 1},
]}
SNMP = {'type': 'snmp', 'label': 'Snmp', 'named': True, 'secret_keys': ['auth-password'], 'fields': [
    {'name': 'enable', 'label': 'Enable', 'type': 'toggle'},
    {'name': 'location', 'label': 'Location', 'type': 'text', 'max_length': 10},
    {'name': 'port', 'label': 'Port', 'type': 'number', 'min': 1, 'max': 65535},
    {'name': 'trap', 'label': 'Trap', 'type': 'group', 'fields': [
        {'name': 'version', 'label': 'Version', 'type': 'choice', 'choices': ['v2c', 'v3'], 'required': True}]},
    {'name': 'users', 'label': 'Users', 'type': 'rows', 'key': 'name', 'fields': [
        {'name': 'name', 'label': 'Name', 'type': 'text', 'required': True},
        {'name': 'auth-password', 'label': 'Auth password', 'type': 'secret'}]},
    {'name': 'vlans', 'label': 'VLANs', 'type': 'list', 'of': 'number'},
]}
LACP = {'type': 'lacp', 'label': 'Lacp', 'named': False, 'secret_keys': [], 'fields': [
    {'name': 'system-priority', 'label': 'System priority', 'type': 'number'}]}


def write_collection(root, schema=SCHEMA):
    build = os.path.join(root, 'playbooks', 'tenant_build')
    os.makedirs(os.path.join(build, 'profiles'), exist_ok=True)
    for name, data in (('schema.yml', schema), ('profiles/index.yml', INDEX),
                       ('profiles/snmp.yml', SNMP), ('profiles/lacp.yml', LACP)):
        with open(os.path.join(build, name), 'w') as f:
            yaml.safe_dump(data, f, sort_keys=False)
    for playbook in ('build.yml', 'sites.yml', 'wireless.yml', 'profiles.yml', 'devices.yml'):
        with open(os.path.join(build, playbook), 'w') as f:
            f.write('---\n')
    return build


@pytest.fixture
def central(app, tmp_path):
    """The app with the stand-in collection cloned and Central switched on."""
    root = tmp_path / 'central' / 'ansible_collections' / 'samthomson' / 'new_central'
    write_collection(str(root))
    app.config.update(CENTRAL_REPO_URL='git@example.com:someone/new-central.git', CENTRAL_REPO=str(root),
                      CENTRAL_COLLECTIONS_PATH=str(tmp_path / 'central'))
    central_session.forget_all()
    yield app
    central_session.forget_all()


@pytest.fixture
def proj(central, auth_client):
    create_project(central, 'admin', 'tenant', project_types.CENTRAL)
    return '/projects/admin/tenant/central'


def sect(app, name):
    return central_schema.section(app, name)


WLAN = {'name': 'corp', 'security': 'WPA2_PERSONAL', 'passphrase': 'hunter2222', 'vlan': 100,
        'device_functions': ['CAMPUS_AP'], 'sites': ['london']}
DESIGN = {'sites': [{'name': 'london'}, {'name': 'leeds'}]}


# ---------------------------------------------------------------------------
# Reading the schema
# ---------------------------------------------------------------------------

class TestSchema:

    def test_sections_lists_and_spliced_fields(self, central):
        schema = central_schema.load(central)
        assert [s['id'] for s in schema['sections']] == ['sites', 'wireless', 'profiles', 'devices']
        wireless = schema['sections'][1]
        assert [p['var'] for p in wireless['lists']] == ['wlans', 'roles']
        assert [f['name'] for f in wireless['lists'][0]['fields']][-1] == 'sites'
        assert wireless['needs'] == ['sites'] and wireless['stage'] == 'configuration'

    def test_not_cloned(self, app):
        app.config['CENTRAL_REPO'] = ''
        with pytest.raises(CentralError, match='not cloned'):
            central_schema.load(app)

    def test_newer_schema_version_is_refused_with_what_to_do(self, central):
        write_collection(central.config['CENTRAL_REPO'], dict(SCHEMA, schema_version=2))
        with pytest.raises(CentralError, match='schema version 2'):
            central_schema.load(central)

    def test_a_changed_file_is_read_again(self, central):
        assert len(central_schema.load(central)['sections']) == 4
        time.sleep(0.01)
        write_collection(central.config['CENTRAL_REPO'], dict(SCHEMA, sections=SCHEMA['sections'][:1]))
        assert len(central_schema.load(central)['sections']) == 1

    def test_profile_type_must_be_in_the_catalogue(self, central):
        assert central_schema.profile_type(central, 'snmp')['fields'][0]['name'] == 'enable'
        for bad in ('../schema', 'nope', '', None):
            with pytest.raises(CentralError):
                central_schema.profile_type(central, bad)


# ---------------------------------------------------------------------------
# Checking what is saved
# ---------------------------------------------------------------------------

class TestCheck:

    def check(self, app, section, data, design=None):
        return central_schema.check(app, sect(app, section), data, design if design is not None else DESIGN)

    def test_good_rows_come_back_clean(self, central):
        clean, problems = self.check(central, 'wireless', {'wlans': [dict(WLAN, vlan='100', city='')], 'roles': []})
        assert problems == ['Wireless networks corp has no field called city.']
        clean, problems = self.check(central, 'wireless', {'wlans': [dict(WLAN, vlan='100')], 'roles': []})
        assert problems == []
        assert clean['wlans'][0]['vlan'] == 100 and clean['roles'] == []

    def test_unset_values_are_dropped(self, central):
        clean, problems = self.check(central, 'sites', {'sites': [{'name': 'york', 'city': '', 'timezone_offset': None}]})
        assert (clean, problems) == ({'sites': [{'name': 'york'}]}, [])

    @pytest.mark.parametrize('change,said', [
        ({'vlan': 5000}, 'between 1 and 4094'),
        ({'vlan': 'ten'}, 'whole number'),
        ({'vlan': True}, 'whole number'),
        ({'security': 'WEP'}, 'must be one of'),
        ({'enabled': 'yes'}, 'on or off'),
        ({'device_functions': ['SWITCH']}, 'must be one of'),
        ({'device_functions': 'CAMPUS_AP'}, 'must be a list'),
        ({'sites': ['paris']}, 'paris, which is not in the design'),
        ({'name': ''}, 'needs Profile name'),
        ({'passphrase': ''}, 'needs Passphrase'),
    ])
    def test_problems(self, central, change, said):
        _clean, problems = self.check(central, 'wireless', {'wlans': [dict(WLAN, **change)]})
        assert any(said in p for p in problems), problems

    def test_a_field_hidden_by_another_is_not_required(self, central):
        row = {k: v for k, v in WLAN.items() if k != 'passphrase'}
        _clean, problems = self.check(central, 'wireless', {'wlans': [dict(row, security='OPEN')]})
        assert problems == []

    def test_a_default_stands_in_for_a_required_field(self, central):
        row = {k: v for k, v in WLAN.items() if k not in ('security', 'device_functions')}
        _clean, problems = self.check(central, 'wireless', {'wlans': [row]})
        assert problems == []

    def test_duplicates(self, central):
        _clean, problems = self.check(central, 'sites', {'sites': [{'name': 'a'}, {'name': 'a'}]})
        assert problems == ['Sites a is listed twice.']

    def test_a_reference_can_point_at_a_row_saved_in_the_same_request(self, central):
        _clean, problems = central_schema.check(
            central, sect(central, 'devices'), {'devices': [{'serial': 'CN1', 'site': 'london'}]}, DESIGN)
        assert problems == []
        clean, _ = central_schema.check(
            central, sect(central, 'devices'), {'devices': [{'serial': 'CN1', 'site': 'london'}]}, DESIGN)
        assert clean['devices'][0]['site'] == 'london'

    def test_lists_that_are_not_the_sections(self, central):
        _clean, problems = self.check(central, 'sites', {'sites': [], 'wlans': []})
        assert problems == ['wlans is not part of Sites.']

    def test_profile_settings_are_checked_against_the_type(self, central):
        row = {'profile_type': 'snmp', 'name': 'main', 'config': {
            'enable': True, 'port': '161', 'location': 'x', 'trap': {'version': 'v3'},
            'users': [{'name': 'ops', 'auth-password': 'pw'}, {}], 'vlans': ['10', 20]}}
        clean, problems = self.check(central, 'profiles', {'profiles': [row]})
        assert problems == []
        assert clean['profiles'][0]['config'] == {
            'enable': True, 'port': 161, 'location': 'x', 'trap': {'version': 'v3'},
            'users': [{'name': 'ops', 'auth-password': 'pw'}], 'vlans': [10, 20]}

    @pytest.mark.parametrize('config,said', [
        ({'colour': 'red'}, 'no setting called colour'),
        ({'location': 'far too long a value'}, 'longer than 10'),
        ({'trap': {}}, None),
        ({'trap': {'version': 'v1'}}, 'must be one of'),
        ({'trap': 'v3'}, 'must be a set of settings'),
        ({'users': [{'auth-password': 'x'}]}, 'needs Name'),
        ({'users': 'ops'}, 'must be a list'),
    ])
    def test_profile_setting_problems(self, central, config, said):
        _clean, problems = self.check(central, 'profiles', {'profiles': [
            {'profile_type': 'snmp', 'name': 'main', 'config': config}]})
        if said is None:
            assert problems == []
        else:
            assert any(said in p for p in problems), problems

    def test_named_type_needs_a_name_and_a_single_one_does_not(self, central):
        _c, problems = self.check(central, 'profiles', {'profiles': [{'profile_type': 'snmp', 'config': {'enable': True}}]})
        assert problems == ['Snmp needs a profile name.']
        _c, problems = self.check(central, 'profiles', {'profiles': [
            {'profile_type': 'lacp', 'config': {'system-priority': 1}}]})
        assert problems == []

    def test_unknown_profile_type(self, central):
        _c, problems = self.check(central, 'profiles', {'profiles': [{'profile_type': 'nope', 'name': 'x'}]})
        assert any('not a profile type' in p for p in problems)


# ---------------------------------------------------------------------------
# Secrets never go to the browser
# ---------------------------------------------------------------------------

class TestSecrets:

    def test_mask_and_unmask_round_trip(self, central):
        s = sect(central, 'wireless')
        saved = {'wlans': [WLAN], 'roles': []}
        masked = central_schema.mask(central, s, saved)
        assert masked['wlans'][0]['passphrase'] == MASK
        assert 'hunter2222' not in json.dumps(masked)
        assert central_schema.unmask(s, masked, saved)['wlans'][0]['passphrase'] == 'hunter2222'

    def test_a_new_secret_replaces_the_saved_one(self, central):
        s = sect(central, 'wireless')
        sent = {'wlans': [dict(WLAN, passphrase='new-passphrase')]}
        assert central_schema.unmask(s, sent, {'wlans': [WLAN]})['wlans'][0]['passphrase'] == 'new-passphrase'

    def test_a_renamed_row_does_not_inherit_a_secret(self, central):
        s = sect(central, 'wireless')
        sent = {'wlans': [dict(WLAN, name='guest', passphrase=MASK)]}
        row = central_schema.unmask(s, sent, {'wlans': [WLAN]})['wlans'][0]
        assert row['passphrase'] is None
        _clean, problems = central_schema.check(central, s, {'wlans': [row]}, DESIGN)
        assert any('needs Passphrase' in p for p in problems)

    def test_secrets_inside_profile_settings(self, central):
        s = sect(central, 'profiles')
        saved = {'profiles': [{'profile_type': 'snmp', 'name': 'main', 'config': {
            'users': [{'name': 'ops', 'auth-password': 'pw1'}, {'name': 'noc', 'auth-password': 'pw2'}]}}]}
        masked = central_schema.mask(central, s, saved)
        assert [u['auth-password'] for u in masked['profiles'][0]['config']['users']] == [MASK, MASK]
        masked['profiles'][0]['config']['users'][1]['auth-password'] = 'changed'
        back = central_schema.unmask(s, masked, saved)
        assert [u['auth-password'] for u in back['profiles'][0]['config']['users']] == ['pw1', 'changed']


# ---------------------------------------------------------------------------
# The design on disk
# ---------------------------------------------------------------------------

class TestDesignFiles:

    def test_save_and_read(self, central, proj):
        s = sect(central, 'sites')
        mark = central_design.save_section(central, 'admin', 'tenant', s, {'sites': [{'name': 'london'}]}, '', 'admin')
        data, read_mark = central_design.read_section(central, 'admin', 'tenant', s)
        assert (data, read_mark) == ({'sites': [{'name': 'london'}]}, mark)
        assert os.path.isfile(os.path.join(project_dir(central, 'admin', 'tenant'), 'design', 'sites.yml'))
        assert central_design.saved_info(central, 'admin', 'tenant')['sites']['by'] == 'admin'

    def test_a_save_over_someone_elses_is_refused(self, central, proj):
        s = sect(central, 'sites')
        central_design.save_section(central, 'admin', 'tenant', s, {'sites': [{'name': 'a'}]}, '', 'alice')
        with pytest.raises(central_design.DesignConflict):
            central_design.save_section(central, 'admin', 'tenant', s, {'sites': [{'name': 'b'}]}, '', 'bob')
        assert central_design.read_section(central, 'admin', 'tenant', s)[0] == {'sites': [{'name': 'a'}]}

    def test_emptying_a_section_removes_its_file(self, central, proj):
        s = sect(central, 'sites')
        mark = central_design.save_section(central, 'admin', 'tenant', s, {'sites': [{'name': 'a'}]}, '', 'admin')
        assert central_design.save_section(central, 'admin', 'tenant', s, {'sites': []}, mark, 'admin') == ''
        assert not os.path.exists(os.path.join(project_dir(central, 'admin', 'tenant'), 'design', 'sites.yml'))

    def test_whole_design_and_counts(self, central, proj):
        central_design.save_section(central, 'admin', 'tenant', sect(central, 'sites'), DESIGN, '', 'admin')
        central_design.save_section(central, 'admin', 'tenant', sect(central, 'wireless'),
                                    {'wlans': [WLAN], 'roles': [{'name': 'staff'}]}, '', 'admin')
        schema = central_schema.load(central)
        design = central_design.read_design(central, 'admin', 'tenant', schema)
        assert set(design) == {'sites', 'wlans', 'roles', 'profiles', 'devices'}
        assert central_design.counts(central, 'admin', 'tenant', schema) == {
            'sites': 2, 'wireless': 2, 'profiles': 0, 'devices': 0}

    def test_the_type_survives_a_design_save(self, central, proj):
        central_design.save_section(central, 'admin', 'tenant', sect(central, 'sites'), DESIGN, '', 'admin')
        assert project_types.of(central, 'admin', 'tenant') == project_types.CENTRAL


# ---------------------------------------------------------------------------
# The tenant's API client
# ---------------------------------------------------------------------------

URL = 'https://de1.api.central.arubanetworks.com'


@pytest.fixture
def verified(monkeypatch):
    """HPE sign-in accepts any client except one called 'bad'."""
    def verify(client_id, client_secret):
        if client_id == 'bad':
            raise central_session.UnlockError('HPE sign-in refused the API client (HTTP 401).')
    monkeypatch.setattr(central_session, 'verify', verify)
    # Kept in the process, so one test's unlock would otherwise be the next one's.
    central_session.forget_all()
    yield
    central_session.forget_all()


class TestSession:

    def test_unlock_keeps_it_and_status_never_shows_the_secret(self, verified):
        status = central_session.unlock('ann', 'ann', 'p', URL + '/', 'client-id-1234567', 's3cret')
        assert status == {'unlocked': True, 'base_url': URL, 'client_id': 'clie…4567',
                          'idle_minutes': central_session.IDLE_SECS // 60}
        assert central_session.get('ann', 'ann', 'p')['client_secret'] == 's3cret'
        assert 's3cret' not in json.dumps(central_session.status('ann', 'ann', 'p'))

    def test_kept_per_person_and_per_project(self, verified):
        central_session.unlock('ann', 'ann', 'p', URL, 'id', 'secret')
        assert central_session.get('bob', 'ann', 'p') is None
        assert central_session.get('ann', 'ann', 'other') is None

    @pytest.mark.parametrize('url', [
        'http://de1.api.central.arubanetworks.com', 'https://example.com',
        'https://de1.api.central.arubanetworks.com.evil.example', 'https://169.254.169.254',
        'https://de1.api.central.arubanetworks.com/path', 'https://user@de1.api.central.arubanetworks.com', ''])
    def test_only_a_central_api_address(self, verified, url):
        with pytest.raises(central_session.UnlockError):
            central_session.unlock('ann', 'ann', 'p', url, 'id', 'secret')
        assert central_session.get('ann', 'ann', 'p') is None

    def test_refused_client_is_not_kept(self, verified):
        with pytest.raises(central_session.UnlockError, match='refused'):
            central_session.unlock('ann', 'ann', 'p', URL, 'bad', 'secret')
        assert central_session.status('ann', 'ann', 'p') == {'unlocked': False}

    def test_forgotten_after_going_unused(self, verified):
        now = [1000.0]
        clock = lambda: now[0]                                                  # noqa: E731
        central_session.unlock('ann', 'ann', 'p', URL, 'id', 'secret', clock=clock)
        now[0] += central_session.IDLE_SECS - 1
        assert central_session.get('ann', 'ann', 'p', clock=clock) is not None      # use renews it
        now[0] += central_session.IDLE_SECS - 1
        assert central_session.get('ann', 'ann', 'p', clock=clock) is not None
        now[0] += central_session.IDLE_SECS + 1
        assert central_session.get('ann', 'ann', 'p', clock=clock) is None

    def test_lock_and_sign_out(self, verified):
        central_session.unlock('ann', 'ann', 'p', URL, 'id', 'secret')
        central_session.unlock('ann', 'ann', 'q', URL, 'id', 'secret')
        central_session.lock('ann', 'ann', 'p')
        assert central_session.get('ann', 'ann', 'p') is None
        central_session.forget_user('ann')
        assert central_session.get('ann', 'ann', 'q') is None

    def test_signing_out_forgets_it(self, central, proj, auth_client, verified):
        auth_client.post(proj + '/unlock', json={'base_url': URL, 'client_id': 'id', 'client_secret': 'secret'})
        assert central_session.get('admin', 'admin', 'tenant') is not None
        auth_client.post('/logout')
        assert central_session.get('admin', 'admin', 'tenant') is None


# ---------------------------------------------------------------------------
# Pages and the design API
# ---------------------------------------------------------------------------

class TestPages:

    @pytest.mark.parametrize('page', ['', '/design', '/apply'])
    def test_pages_render(self, auth_client, proj, page):
        res = auth_client.get(proj + page)
        assert res.status_code == 200
        body = res.data.decode()
        assert '${PROJECT_BASE}/central/design' in body and '${PROJECT_BASE}/central/apply' in body

    @pytest.mark.parametrize('page', ['/design', '/apply', '/api/schema', '/api/design/sites'])
    def test_a_config_project_has_none_of_it(self, central, auth_client, page):
        create_project(central, 'admin', 'switches')
        assert auth_client.get('/projects/admin/switches/central' + page).status_code == 404

    def test_pages_say_why_when_the_collection_is_missing(self, central, auth_client, proj):
        central.config['CENTRAL_REPO'] = ''
        for page in ('', '/design', '/apply'):
            res = auth_client.get(proj + page)
            assert res.status_code == 200 and b'not cloned' in res.data
        assert auth_client.get(proj + '/api/schema').status_code == 400

    def test_overview_links_each_section(self, central, auth_client, proj):
        central_design.save_section(central, 'admin', 'tenant', sect(central, 'sites'), DESIGN, '', 'admin')
        body = auth_client.get(proj).data.decode()
        assert 'central/design?section=sites' in body and 'Sites · 2' in body

    def test_schema_api(self, auth_client, proj):
        body = auth_client.get(proj + '/api/schema').get_json()
        assert [s['id'] for s in body['schema']['sections']] == ['sites', 'wireless', 'profiles', 'devices']
        assert body['mask'] == MASK
        assert body['profile_types'][0] == {'type': 'snmp', 'label': 'Snmp', 'category': 'System',
                                            'named': True, 'fields': 6}
        assert 'module' not in body['profile_types'][0]

    def test_profile_type_api(self, auth_client, proj):
        assert auth_client.get(proj + '/api/profile-type/snmp').get_json()['profile']['label'] == 'Snmp'
        assert auth_client.get(proj + '/api/profile-type/nope').status_code == 400
        assert auth_client.get(proj + '/api/profile-type/..%2Fschema').status_code == 404


class TestDesignApi:

    def save(self, client, proj, section, data, base=''):
        return client.post('%s/api/design/%s/save' % (proj, section), json={'data': data, 'base': base})

    def test_save_then_read_back_with_the_secret_hidden(self, central, auth_client, proj):
        self.save(auth_client, proj, 'sites', DESIGN)
        res = self.save(auth_client, proj, 'wireless', {'wlans': [WLAN], 'roles': []})
        body = res.get_json()
        assert res.status_code == 200 and body['ok']
        assert 'hunter2222' not in res.data.decode()
        assert body['data']['wlans'][0]['passphrase'] == MASK

        got = auth_client.get(proj + '/api/design/wireless')
        assert 'hunter2222' not in got.data.decode()
        loaded = got.get_json()
        assert loaded['base'] == body['base']
        assert 'passphrase: \'********\'' in loaded['yaml'] or 'passphrase: "********"' in loaded['yaml']
        assert loaded['refs']['sites'] == ['london', 'leeds']

        # On disk the secret is what was typed.
        saved = central_design.read_section(central, 'admin', 'tenant', sect(central, 'wireless'))[0]
        assert saved['wlans'][0]['passphrase'] == 'hunter2222'

    def test_saving_the_mask_back_keeps_the_secret(self, central, auth_client, proj):
        self.save(auth_client, proj, 'sites', DESIGN)
        first = self.save(auth_client, proj, 'wireless', {'wlans': [WLAN]}).get_json()
        sent = first['data']
        sent['wlans'][0]['vlan'] = 200
        assert self.save(auth_client, proj, 'wireless', sent, first['base']).status_code == 200
        saved = central_design.read_section(central, 'admin', 'tenant', sect(central, 'wireless'))[0]
        assert (saved['wlans'][0]['vlan'], saved['wlans'][0]['passphrase']) == (200, 'hunter2222')

    def test_problems_are_listed_and_nothing_is_saved(self, central, auth_client, proj):
        res = self.save(auth_client, proj, 'wireless', {'wlans': [dict(WLAN, vlan=9999)]})
        body = res.get_json()
        assert res.status_code == 400 and not body['ok']
        assert any('between 1 and 4094' in p for p in body['problems'])
        assert any('london, which is not in the design' in p for p in body['problems'])
        assert central_design.read_section(central, 'admin', 'tenant', sect(central, 'wireless'))[0]['wlans'] == []

    def test_conflict(self, auth_client, proj):
        base = self.save(auth_client, proj, 'sites', DESIGN).get_json()['base']
        assert self.save(auth_client, proj, 'sites', {'sites': [{'name': 'x'}]}, base).status_code == 200
        res = self.save(auth_client, proj, 'sites', {'sites': [{'name': 'y'}]}, base)
        assert res.status_code == 409 and 'someone else' in res.get_json()['error']

    def test_unknown_section(self, auth_client, proj):
        assert auth_client.get(proj + '/api/design/nope').status_code == 404
        assert self.save(auth_client, proj, 'nope', {}).status_code == 404

    def test_template_text_is_saved_as_text(self, central, auth_client, proj):
        """Whatever is typed is data. It is tagged unsafe when a run passes it to Ansible."""
        self.save(auth_client, proj, 'sites', {'sites': [{'name': 'x', 'city': '{{ lookup("env","HOME") }}'}]})
        saved = central_design.read_section(central, 'admin', 'tenant', sect(central, 'sites'))[0]
        assert saved['sites'][0]['city'] == '{{ lookup("env","HOME") }}'


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

@pytest.fixture
def people(central, verified):
    for name in ('alice', 'viewer', 'editor', 'deployer'):
        User.create(name, 'password1', central)
        ensure_workspace(central, name)
    create_project(central, 'alice', 'tenant', project_types.CENTRAL)
    for name in ('viewer', 'editor', 'deployer'):
        sharing.set_share(central, 'alice', 'tenant', 'user', name,
                          {'viewer': 'view', 'editor': 'edit', 'deployer': 'deploy'}[name], 'alice')
    clients = {}
    for name in ('alice', 'viewer', 'editor', 'deployer'):
        clients[name] = FlaskClient(central, use_cookies=True)
        clients[name].post('/login', data={'username': name, 'password': 'password1'})
    return clients


class TestAccess:
    BASE = '/projects/alice/tenant/central'
    CREDS = {'base_url': URL, 'client_id': 'id', 'client_secret': 'secret'}

    def test_view_reads_but_does_not_save(self, people):
        viewer = people['viewer']
        assert viewer.get(self.BASE + '/design').status_code == 200
        assert viewer.get(self.BASE + '/api/design/sites').status_code == 200
        assert viewer.post(self.BASE + '/api/design/sites/save', json={'data': DESIGN}).status_code == 403

    def test_edit_saves_but_does_not_touch_the_tenant(self, people):
        editor = people['editor']
        assert editor.post(self.BASE + '/api/design/sites/save', json={'data': DESIGN}).status_code == 200
        assert editor.post(self.BASE + '/unlock', json=self.CREDS).status_code == 403
        assert editor.post(self.BASE + '/run', json={'mode': 'plan'}).status_code == 403
        assert editor.post(self.BASE + '/lock', json={}).status_code == 403
        assert b'need deploy access' in editor.get(self.BASE + '/apply').data

    def test_deploy_unlocks(self, people):
        res = people['deployer'].post(self.BASE + '/unlock', json=self.CREDS)
        assert res.status_code == 200 and res.get_json()['tenant']['unlocked']
        assert 'secret' not in json.dumps(res.get_json()['tenant'])

    def test_one_persons_unlock_is_not_anothers(self, people):
        people['deployer'].post(self.BASE + '/unlock', json=self.CREDS)
        people['alice'].post(self.BASE + '/api/design/sites/save', json={'data': DESIGN})
        res = people['alice'].post(self.BASE + '/run', json={'mode': 'plan'})
        assert res.status_code == 409 and res.get_json()['locked']

    def test_a_stranger_sees_nothing(self, central, people):
        User.create('mallory', 'password1', central)
        mallory = FlaskClient(central, use_cookies=True)
        mallory.post('/login', data={'username': 'mallory', 'password': 'password1'})
        assert mallory.get(self.BASE + '/design').status_code == 404
        assert mallory.post(self.BASE + '/unlock', json=self.CREDS).status_code == 404


# ---------------------------------------------------------------------------
# Plan and apply
# ---------------------------------------------------------------------------

@pytest.fixture
def runs(monkeypatch, verified):
    """Capture what a run would hand to ansible-playbook, and finish it with a report per section."""
    import app.central_routes as routes
    calls = []

    def fake(store, lock, job_id, **kw):
        calls.append(kw)
        sections = ['sites'] if kw['playbook_path'].endswith('sites.yml') else ['wireless', 'sites']
        with lock:
            store[job_id].update(
                status='done', returncode=0,
                output='PLAY RECAP ok — secret-value should never be here: %s' % kw['extra_env']['API_CLIENT_SECRET'],
                results=[{'section': s, 'changed': True, 'failed': False, 'entries': [
                    {'step': 'Sites', 'item': 'london', 'changed': True, 'failed': False, 'skipped': False,
                     'diff': {'before': {}, 'after': {'city': 'London'}}}]} for s in sections])

    monkeypatch.setattr(routes, 'run_ansible_job', fake)
    with routes._central_jobs_lock:
        routes._central_jobs.clear()
    return calls


def wait_for(client, proj, job_id):
    """The finished job, as the page would fetch it. The worker thread
    orders the reports and masks the output just after the playbook
    returns, so wait for that rather than for the playbook's status."""
    from app.central_routes import _central_jobs, _central_jobs_lock
    for _ in range(250):
        with _central_jobs_lock:
            finished = (_central_jobs.get(job_id) or {}).get('_finished_at') is not None
        if finished:
            return client.get('%s/run/%s' % (proj, job_id)).get_json()
        time.sleep(0.02)
    raise AssertionError('the run never finished')


class TestRuns:
    CREDS = {'base_url': URL, 'client_id': 'the-client-id', 'client_secret': 'the-client-secret'}

    @pytest.fixture
    def ready(self, central, auth_client, proj, runs):
        auth_client.post(proj + '/api/design/sites/save', json={'data': DESIGN})
        auth_client.post(proj + '/api/design/wireless/save', json={'data': {'wlans': [WLAN]}})
        auth_client.post(proj + '/unlock', json=self.CREDS)
        return runs

    def test_locked_tenant_refuses(self, auth_client, proj, runs):
        auth_client.post(proj + '/api/design/sites/save', json={'data': DESIGN})
        res = auth_client.post(proj + '/run', json={'mode': 'plan'})
        assert res.status_code == 409 and res.get_json()['locked'] and runs == []

    def test_plan_of_everything(self, central, auth_client, proj, ready):
        job_id = auth_client.post(proj + '/run', json={'mode': 'plan'}).get_json()['job_id']
        job = wait_for(auth_client, proj, job_id)
        call = ready[0]
        assert call['playbook_path'].endswith(os.path.join('tenant_build', 'build.yml'))
        assert call['extra_args'] == ['--check', '--diff']
        # The design is data; the API client is in the environment only.
        assert set(call['extra_vars']) == {'design'}
        assert call['extra_vars']['design']['wlans'][0]['passphrase'] == 'hunter2222'
        assert 'the-client-secret' not in json.dumps(call['extra_vars']) + json.dumps(call['inventory'])
        env = call['extra_env']
        assert (env['API_CLIENT_ID'], env['API_CLIENT_SECRET'], env['CENTRAL_BASE_URL']) == (
            'the-client-id', 'the-client-secret', URL)
        assert env['ANSIBLE_COLLECTIONS_PATH'].split(os.pathsep)[0] == central.config['CENTRAL_COLLECTIONS_PATH']
        assert os.path.basename(env['TMPDIR']).startswith('central-run-')
        assert call['inventory']['all']['hosts']['localhost']['ansible_connection'] == 'local'
        # Reports come back in the design's order, and the secret is masked in the output.
        assert [r['section'] for r in job['results']] == ['sites', 'wireless']
        assert 'the-client-secret' not in json.dumps(job)
        assert job['mode'] == 'plan'

    def test_apply_of_one_section_runs_its_own_playbook_with_only_its_rows(self, auth_client, proj, ready):
        job_id = auth_client.post(proj + '/run', json={'mode': 'apply', 'sections': ['sites']}).get_json()['job_id']
        wait_for(auth_client, proj, job_id)
        call = ready[0]
        assert call['playbook_path'].endswith('sites.yml')
        assert call['extra_args'] == []
        assert call['extra_vars'] == {'design': {'sites': DESIGN['sites']}}

    def test_recorded_in_history_without_the_secret(self, central, auth_client, proj, ready):
        job_id = auth_client.post(proj + '/run', json={'mode': 'apply'}).get_json()['job_id']
        wait_for(auth_client, proj, job_id)
        for _ in range(100):
            entry = audit.entries(central, owner='admin', project='tenant')[0]
            if entry['status'] != 'running':
                break
            time.sleep(0.02)
        assert entry['action'] == 'central_apply' and entry['status'] == 'done'
        assert entry['params']['tenant'] == URL
        assert entry['outcome'][0] == '2 of 2 sections changed'
        assert 'the-client-secret' not in json.dumps(entry, default=str)
        assert 'the-client-id' not in json.dumps(entry, default=str)       # only its ends are kept
        assert all('diff' not in e for r in entry['results'] for e in r['entries'])
        page = auth_client.get('/projects/admin/tenant/history').data.decode()
        assert 'Central apply' in page and 'API client' in page and 'london' in page

    def test_design_problems_stop_a_run(self, central, auth_client, proj, ready):
        s = sect(central, 'wireless')
        _d, mark = central_design.read_section(central, 'admin', 'tenant', s)
        central_design.save_section(central, 'admin', 'tenant', s, {'wlans': [dict(WLAN, sites=['paris'])]}, mark, 'x')
        res = auth_client.post(proj + '/run', json={'mode': 'plan'})
        assert res.status_code == 400 and any('paris' in p for p in res.get_json()['problems'])
        assert ready == []

    def test_nothing_to_do(self, auth_client, proj, runs):
        auth_client.post(proj + '/unlock', json=self.CREDS)
        res = auth_client.post(proj + '/run', json={'mode': 'plan'})
        assert res.status_code == 400 and 'nothing in the design' in res.get_json()['error']

    @pytest.mark.parametrize('body', [{'mode': 'destroy'}, {'mode': 'plan', 'sections': ['nope']},
                                      {'mode': 'plan', 'sections': 'sites'}, {}])
    def test_bad_requests(self, auth_client, proj, ready, body):
        assert auth_client.post(proj + '/run', json=body).status_code == 400

    def test_one_run_at_a_time(self, auth_client, proj, ready):
        import app.central_routes as routes
        with routes._central_jobs_lock:
            routes._central_jobs['stuck'] = {'status': 'running', '_owner': 'someone', '_project': 'admin/tenant',
                                             '_finished_at': None}
        res = auth_client.post(proj + '/run', json={'mode': 'plan'})
        assert res.status_code == 409 and 'someone already has a run' in res.get_json()['error']

    def test_a_run_is_only_its_starters_to_see(self, central, auth_client, proj, ready):
        job_id = auth_client.post(proj + '/run', json={'mode': 'plan'}).get_json()['job_id']
        wait_for(auth_client, proj, job_id)
        User.create('erin', 'password1', central, is_admin=True)
        erin = FlaskClient(central, use_cookies=True)
        erin.post('/login', data={'username': 'erin', 'password': 'password1'})
        # An admin can open the project read-only, but polling a run needs deploy access.
        assert erin.get('%s/run/%s' % (proj, job_id)).status_code in (403, 404)

    def test_a_running_central_job_blocks_a_repo_move(self, auth_client, proj):
        import app.central_routes as routes
        from app.admin import _jobs_running
        with routes._central_jobs_lock:
            routes._central_jobs.clear()
            routes._central_jobs['x'] = {'status': 'running', '_finished_at': None}
        try:
            assert 'Central run' in _jobs_running()
        finally:
            with routes._central_jobs_lock:
                routes._central_jobs.clear()
