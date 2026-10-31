"""
Verify: inventory and LLDP read over REST, expected links inferred from the
design, and the comparison between them.
"""

import os

import pytest
import requests
import yaml

from app import cabling, switch_read
from app.models import User
from app.project import create_project, get_inventory, project_dir, record_inventory
from app.utils import add_project_hosts, ensure_workspace


# ── Reading a switch ───────────────────────────────────────

class FakeResponse:
    def __init__(self, status=200, data=None):
        self.status_code, self._data = status, data

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._data


class FakeSwitch:
    instances = []
    answers = {}

    def __init__(self):
        self.calls, self.verify = [], True
        FakeSwitch.instances.append(self)

    def _hit(self, url, **kw):
        path = '/' + url.split('://', 1)[1].split('/', 1)[1]
        self.calls.append(path)
        if path == '/rest':
            return FakeResponse(200, {'latest': {'prefix': '/rest/v10.13'}})
        if path.endswith(('/login', '/logout')):
            return FakeResponse(200, {})
        rel = path.split('/rest/v10.13', 1)[1]
        if rel in self.answers:
            a = self.answers[rel]
            return a if isinstance(a, FakeResponse) else FakeResponse(200, a)
        return FakeResponse(404, {})

    get = post = lambda self, url, **kw: self._hit(url, **kw)

    def close(self):
        pass


SYSTEM = {'hostname': 'CORE-01', 'platform_name': '6300', 'system_mac': '00:11:22:33:44:01',
          'boot_time': 1700000000, 'software_version': 'FL.10.13.1005'}
SUBSYSTEMS = {
    'chassis,1': {'product_info': {'serial_number': 'SG00001', 'part_number': 'JL659A',
                                   'product_name': '6300M 48SR5', 'base_mac_address': '00:11:22:33:44:01'}},
    'line_card,1/1': {'product_info': {'serial_number': 'LC1'}},
}
PORTS = {'1/1/1': {'name': '1/1/1', 'link_state': 'up'},
         '1/1/2': {'name': '1/1/2', 'link_state': 'down'},
         '1/1/49': {'name': '1/1/49', 'link_state': 'up'},
         'vlan10': {'name': 'vlan10', 'link_state': 'up'}}
NEIGHBOUR = {'00:11:22:33:44:02,1/1/49': {
    'chassis_id': '00:11:22:33:44:02', 'port_id': '1/1/49',
    'neighbor_info': {'chassis_name': 'ACC-01', 'port_description': 'uplink'}}}


@pytest.fixture
def fake(monkeypatch):
    FakeSwitch.instances = []
    FakeSwitch.answers = {
        '/system': SYSTEM, '/system/subsystems': SUBSYSTEMS, '/system/interfaces': PORTS,
        '/system/interfaces/1%2F1%2F1/lldp_neighbors': NEIGHBOUR,
        '/system/interfaces/1%2F1%2F49/lldp_neighbors': {},
    }
    monkeypatch.setattr(requests, 'Session', FakeSwitch)
    return FakeSwitch


class TestReadSwitch:

    def test_inventory_and_neighbours(self, fake):
        out = switch_read.read_switch('10.0.0.1', 'u', 'p')
        inv = out['inventory']
        assert (inv['serial'], inv['serials'], inv['model'], inv['part_number']) == \
            ('SG00001', ['SG00001'], '6300M 48SR5', 'JL659A')
        assert (inv['base_mac'], inv['firmware'], inv['members']) == \
            ('00:11:22:33:44:01', 'FL.10.13.1005', [])
        assert out['neighbors'] == {'1/1/1': [{'chassis_id': '00:11:22:33:44:02',
                                               'port_id': '1/1/49', 'sys_name': 'ACC-01',
                                               'port_desc': 'uplink'}]}

    def test_only_physical_ports_with_link_are_asked(self, fake):
        switch_read.read_switch('10.0.0.1', 'u', 'p')
        asked = [c for c in fake.instances[0].calls if 'lldp' in c]
        assert asked == ['/rest/v10.13/system/interfaces/1%2F1%2F1/lldp_neighbors',
                         '/rest/v10.13/system/interfaces/1%2F1%2F49/lldp_neighbors']
        assert fake.instances[0].calls[-1].endswith('/logout')

    def test_vsf_members(self, fake):
        fake.answers['/system/subsystems'] = {
            'chassis,1': {'product_info': {'serial_number': 'A', 'product_name': '6300F'}},
            'chassis,2': {'product_info': {'serial_number': 'B', 'product_name': '6300F'}}}
        inv = switch_read.read_switch('10.0.0.1', 'u', 'p')['inventory']
        assert inv['serials'] == ['A', 'B'] and [m['member'] for m in inv['members']] == ['1', '2']

    def test_missing_parts_dont_fail_the_read(self, fake):
        fake.answers.pop('/system/subsystems')
        fake.answers['/system/interfaces'] = FakeResponse(500, {})
        out = switch_read.read_switch('10.0.0.1', 'u', 'p')
        assert out['inventory']['serial'] is None and out['inventory']['model'] == '6300'
        assert out['neighbors'] == {}

    def test_read_all_keeps_failures_apart(self, monkeypatch):
        def one(ip, u, p):
            if ip == '10.0.0.2':
                raise switch_read.SwitchError('timed out')
            return {'inventory': {}, 'neighbors': {}}
        monkeypatch.setattr(switch_read, 'read_switch', one)
        out = switch_read.read_all([{'hostname': 'A', 'mgmt_ip': '10.0.0.1'},
                                    {'hostname': 'B', 'mgmt_ip': '10.0.0.2'}], 'u', 'p')
        assert out['A']['ok'] and out['B'] == {'ok': False, 'error': 'timed out'}

    @pytest.mark.parametrize('value, want', [
        ('00:11:22:AA:bb:cc', '001122aabbcc'), ('0011.22aa.bbcc', '001122aabbcc'),
        ('nope', ''), (None, '')])
    def test_norm_mac(self, value, want):
        assert switch_read.norm_mac(value) == want


# ── Expected links ─────────────────────────────────────────

def phy(name, **kw):
    return dict({'name': name, 'description': '', 'lag_member': '', 'port_type': 'access',
                 'routed': 'false', 'ip_address': '', 'ip_prefix': ''}, **kw)


def state(physical=(), lags=(), isl=''):
    return {'physical': list(physical), 'lags': list(lags), 'vsxIsl': isl}


class TestExpectations:

    def test_point_to_point(self):
        states = {
            'CORE-01': state([phy('1/1/49', port_type='routed', ip_address='10.0.0.0', ip_prefix='31')]),
            'ACC-01': state([phy('1/1/52', port_type='routed', ip_address='10.0.0.1/31')]),
        }
        e = cabling.expectations(states, [])
        assert e[('CORE-01', '1/1/49')] == {'peer': 'ACC-01', 'peer_port': '1/1/52',
                                            'source': 'point-to-point 10.0.0.0/31'}
        assert e[('ACC-01', '1/1/52')]['peer_port'] == '1/1/49'

    def test_a_third_address_in_the_subnet_makes_it_ambiguous(self):
        states = {h: state([phy('1/1/1', port_type='routed', ip_address='10.0.0.%d/30' % i)])
                  for i, h in enumerate(('A', 'B', 'C'), 1)}
        assert cabling.expectations(states, []) == {}

    def test_description_with_and_without_a_port(self):
        states = {
            'ACC-01': state([phy('1/1/49', description='Uplink to core-01 1/1/1'),
                             phy('1/1/50', description='to CORE-01'),
                             phy('1/1/51', description='1/1/51 spare')]),
            'CORE-01': state(),
        }
        e = cabling.expectations(states, [])
        assert e[('ACC-01', '1/1/49')]['peer_port'] == '1/1/1'
        assert e[('ACC-01', '1/1/50')] == {'peer': 'CORE-01', 'peer_port': None,
                                           'source': 'description'}
        assert ('ACC-01', '1/1/51') not in e

    def test_name_must_be_whole(self):
        states = {'ACC-1': state([phy('1/1/1', description='to ACC-10')]), 'ACC-10': state(),
                  'X': state([phy('1/1/1', description='to acc-1')])}
        e = cabling.expectations(states, [])
        assert e[('ACC-1', '1/1/1')]['peer'] == 'ACC-10'
        assert e[('X', '1/1/1')]['peer'] == 'ACC-1'

    def test_two_switches_named_is_ambiguous(self):
        states = {'A': state([phy('1/1/1', description='between B and C')]), 'B': state(),
                  'C': state()}
        assert cabling.expectations(states, []) == {}

    def test_vsx_isl(self):
        states = {'C1': state([phy('1/1/53', lag_member='256'), phy('1/1/54', lag_member='256')],
                              isl='lag256'),
                  'C2': state([phy('1/1/53', lag_member='256')], isl='lag256')}
        e = cabling.expectations(states, [('C1', 'C2')])
        assert {k: v['peer'] for k, v in e.items()} == {
            ('C1', '1/1/53'): 'C2', ('C1', '1/1/54'): 'C2', ('C2', '1/1/53'): 'C1'}
        assert e[('C1', '1/1/53')]['source'] == 'VSX ISL'

    def test_lag_description_covers_its_members(self):
        states = {'ACC-01': state([phy('1/1/49', lag_member='1'), phy('1/1/50', lag_member='1')],
                                  lags=[{'name': 'lag1', 'description': 'Uplink to CORE-01'}]),
                  'CORE-01': state()}
        e = cabling.expectations(states, [])
        assert set(e) == {('ACC-01', '1/1/49'), ('ACC-01', '1/1/50')}
        assert e[('ACC-01', '1/1/49')]['source'] == 'LAG lag1 description'

    def test_stronger_source_wins(self):
        states = {
            'A': state([phy('1/1/1', port_type='routed', ip_address='10.0.0.0/31',
                            description='to C')]),
            'B': state([phy('1/1/9', port_type='routed', ip_address='10.0.0.1/31')]),
            'C': state(),
        }
        assert cabling.expectations(states, [])[('A', '1/1/1')]['peer'] == 'B'


# ── Comparing ──────────────────────────────────────────────

def nb(name, port, mac=''):
    return {'sys_name': name, 'port_id': port, 'chassis_id': mac, 'port_desc': ''}


class TestCompare:

    STATES = {'CORE-01': state(), 'ACC-01': state(), 'ACC-02': state()}

    def run(self, expected, neighbors, macs=None, failed=()):
        reads = {h: ({'ok': False, 'error': 'x'} if h in failed
                     else {'ok': True, 'neighbors': neighbors.get(h, {})})
                 for h in self.STATES}
        return cabling.compare(self.STATES, expected, reads, macs or {})

    E = {('ACC-01', '1/1/49'): {'peer': 'CORE-01', 'peer_port': '1/1/1', 'source': 'description'}}

    def result(self, rows, host='ACC-01', port='1/1/49'):
        return [(r['result'], r['seen']) for r in rows if (r['host'], r['port']) == (host, port)]

    def test_ok(self):
        rows = self.run(self.E, {'ACC-01': {'1/1/49': [nb('CORE-01', '1/1/1')]}})
        assert self.result(rows) == [('ok', 'CORE-01 1/1/1')]

    def test_name_with_a_domain(self):
        rows = self.run(self.E, {'ACC-01': {'1/1/49': [nb('core-01.corp.local', '1/1/1')]}})
        assert self.result(rows)[0][0] == 'ok'

    def test_wrong_port(self):
        rows = self.run(self.E, {'ACC-01': {'1/1/49': [nb('CORE-01', '1/1/2')]}})
        assert self.result(rows) == [('wrong_port', 'CORE-01 1/1/2')]

    def test_any_port_when_none_is_named(self):
        e = {k: dict(v, peer_port=None) for k, v in self.E.items()}
        rows = self.run(e, {'ACC-01': {'1/1/49': [nb('CORE-01', '1/1/7')]}})
        assert self.result(rows)[0][0] == 'ok'

    def test_wrong_switch(self):
        rows = self.run(self.E, {'ACC-01': {'1/1/49': [nb('ACC-02', '1/1/1')]}})
        assert self.result(rows) == [('wrong_switch', 'ACC-02 1/1/1')]

    def test_unknown_device(self):
        rows = self.run(self.E, {'ACC-01': {'1/1/49': [nb('printer', 'eth0', 'aa:bb:cc:dd:ee:ff')]}})
        assert self.result(rows) == [('wrong_switch', 'printer eth0')]

    def test_missing(self):
        assert self.result(self.run(self.E, {})) == [('missing', '')]

    def test_not_read(self):
        assert self.result(self.run(self.E, {}, failed=['ACC-01']))[0][0] == 'not_read'

    def test_factory_switch_recognised_by_mac(self):
        rows = self.run(self.E, {'ACC-01': {'1/1/49': [nb('6300', '1/1/1', '00:11:22:33:44:01')]}},
                        macs={'00-11-22-33-44-01': 'CORE-01'})
        assert self.result(rows) == [('ok', 'CORE-01 1/1/1')]

    def test_link_not_in_the_design(self):
        rows = self.run({}, {'ACC-02': {'1/1/3': [nb('CORE-01', '1/1/9')],
                                        '1/1/4': [nb('some-phone', '1')]}})
        assert [(r['host'], r['port'], r['result']) for r in rows] == \
            [('ACC-02', '1/1/3', 'not_in_design')]


# ── Inventory records ──────────────────────────────────────

class TestInventory:

    @pytest.fixture
    def proj(self, app, auth_client, skeleton_dir):
        ensure_workspace(app, 'admin')
        create_project(app, 'admin', 'p')
        return app

    def test_stored_with_who_and_when(self, proj):
        assert record_inventory(proj, 'admin', 'p', {'A': {'serials': ['S1']}}, 'sam') == {}
        rec = get_inventory(proj, 'admin', 'p')['A']
        assert rec['serials'] == ['S1'] and rec['read_by'] == 'sam' and rec['read_at']

    def test_different_hardware_is_flagged_until_read_again(self, proj):
        record_inventory(proj, 'admin', 'p', {'A': {'serials': ['S1']}}, 'sam')
        assert record_inventory(proj, 'admin', 'p', {'A': {'serials': ['S2']}}, 'sam') == {'A': ['S1']}
        assert get_inventory(proj, 'admin', 'p')['A']['previous_serials'] == ['S1']
        assert record_inventory(proj, 'admin', 'p', {'A': {'serials': ['S2']}}, 'sam') == {}
        assert 'previous_serials' not in get_inventory(proj, 'admin', 'p')['A']

    def test_unread_serial_is_not_a_swap(self, proj):
        record_inventory(proj, 'admin', 'p', {'A': {'serials': ['S1']}}, 'sam')
        assert record_inventory(proj, 'admin', 'p', {'A': {'serials': []}}, 'sam') == {}


# ── The page and route ─────────────────────────────────────

class TestRoute:

    @pytest.fixture
    def proj(self, app, auth_client, skeleton_dir, monkeypatch):
        ensure_workspace(app, 'admin')
        create_project(app, 'admin', 'p')
        add_project_hosts(app, 'admin', 'p', ['CORE-01', 'ACC-01'], 'none')
        hv = os.path.join(project_dir(app, 'admin', 'p'), 'host_vars')
        with open(os.path.join(hv, 'ACC-01', 'interfaces.yml'), 'w') as f:
            yaml.safe_dump({'physical_interfaces': [{'name': '1/1/49', 'description': 'to CORE-01 1/1/1'}],
                            'lag_interfaces': [], 'loopback_interfaces': [], 'vlan_interfaces': [],
                            'interface_groups': []}, f)
        with open(os.path.join(project_dir(app, 'admin', 'p'), 'deploy_mapping.yml'), 'w') as f:
            yaml.safe_dump({'hosts': [{'hostname': 'CORE-01', 'mgmt_ip': '10.0.0.1'},
                                      {'hostname': 'ACC-01', 'mgmt_ip': '10.0.0.2'}]}, f)
        reads = {
            'CORE-01': {'ok': True, 'inventory': {'serials': ['SC1'], 'serial': 'SC1', 'model': '6300M',
                                                  'base_mac': '00:11:22:33:44:01'}, 'neighbors': {}},
            'ACC-01': {'ok': True, 'inventory': {'serials': ['SA1'], 'serial': 'SA1', 'model': '6200F'},
                       'neighbors': {'1/1/49': [nb('6300', '1/1/1', '00:11:22:33:44:01')]}},
        }
        seen = []
        monkeypatch.setattr(switch_read, 'read_all',
                            lambda hosts, u, p: seen.append((hosts, u, p)) or reads)
        return seen

    BODY = {'switch_username': 'netops', 'switch_password': 'pw'}

    def test_reads_stores_and_compares(self, app, auth_client, proj):
        data = auth_client.post('/projects/admin/p/verify/read', json=self.BODY).get_json()
        assert data['ok']
        assert [h['hostname'] for h in proj[0][0]] == ['CORE-01', 'ACC-01']
        assert [(r['host'], r['port'], r['result']) for r in data['cabling']] == \
            [('ACC-01', '1/1/49', 'ok')]
        assert get_inventory(app, 'admin', 'p')['ACC-01']['serials'] == ['SA1']

    def test_needs_credentials(self, auth_client, proj):
        assert auth_client.post('/projects/admin/p/verify/read',
                                json={'switch_username': 'x'}).status_code == 400
        assert proj == []

    def test_needs_deploy_access(self, app, auth_client, proj):
        from app import sharing
        User.create('ed', 'password1', app)
        sharing.set_share(app, 'admin', 'p', 'user', 'ed', sharing.EDIT, 'admin')
        c = app.test_client()
        c.post('/login', data={'username': 'ed', 'password': 'password1'})
        assert c.post('/projects/admin/p/verify/read', json=self.BODY).status_code == 403
        body = c.get('/projects/admin/p/verify').get_data(as_text=True)
        assert 'needs deploy access' in body and 'id="read-btn"' not in body

    def test_page_and_hosts_show_stored_inventory(self, app, auth_client, proj):
        auth_client.post('/projects/admin/p/verify/read', json=self.BODY)
        body = auth_client.get('/projects/admin/p/verify').get_data(as_text=True)
        assert 'SA1' in body and '6200F' in body and 'id="read-btn"' in body
        hosts = auth_client.get('/projects/admin/p/hosts').get_data(as_text=True)
        assert 'SA1' in hosts and '<th>Hardware</th>' in hosts

    def test_in_the_project_menu(self, auth_client, proj):
        body = auth_client.get('/projects/admin/p/hosts').get_data(as_text=True)
        assert '${PROJECT_BASE}/verify' in body
