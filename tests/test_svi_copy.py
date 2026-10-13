"""
Copying SVIs between switches, including across sites.

Copying used to keep each SVI in the source switch's subnet, so copying
site A's SVIs onto a site B switch handed out site A addresses. Now each
SVI is matched by VLAN ID to a block in a chosen target supernet; anything
that cannot be placed comes over unaddressed rather than carrying an
address that can only be a duplicate.

The behavioural tests extract the real functions from the rendered editor
and run them under Node, so it is the shipped logic being checked.
"""

import json
import os
import re
import shutil
import subprocess

import pytest

FUNCTIONS = ('function subnetForIp', 'function ipToInt',
             'function detectSviSupernet', 'function copySvisFrom')


def _editor_page(app, auth_client):
    from app.project import create_project, project_host_vars_dir
    with app.app_context():
        create_project(app, 'admin', 'xs')
        base = project_host_vars_dir(app, 'admin', 'xs')
        for host in ('CTY-Core-01', 'RIV-Core-01'):
            os.makedirs(os.path.join(base, host), exist_ok=True)
    return auth_client.get('/projects/xs/editor?host=RIV-Core-01').data.decode()


def _extract(body):
    script = max(re.findall(r'<script[^>]*>(.*?)</script>', body, re.S), key=len)
    parts = []
    for name in ('function intToIP', 'function generateSubnetIPs'):
        i = script.index(name)
        parts.append(script[i:script.index('\n}\n', i) + 3])
    i = script.index('const MAX_ENUMERATED_IPS')
    parts.append(script[i:script.index('\n', i) + 1])
    for name in FUNCTIONS:
        i = script.index('  ' + name)
        parts.append(script[i:script.index('\n  }\n', i) + 4])
    return '\n'.join(parts)


ALLOCS = {
    'vlan_supernet': {
        'cty': {'10.20.10.0/24': {'vlan_id': '100'},
                '10.20.20.0/24': {'vlan_id': '200'}},
        'riv': {'10.21.10.0/24': {'vlan_id': '100'},
                '10.21.20.0/24': {'vlan_id': '200', 'hostname': 'RIV-Core-01'}},
    },
    'svi': {
        'cty': {'10.20.10.1': {'hostname': 'CTY-Core-01'},
                '10.20.20.1': {'hostname': 'CTY-Core-01'}},
        'riv': {'10.21.10.1': {'hostname': 'RIV-Core-02'}},
    },
}

SOURCE = {'vlanIfs': [
    {'name': 'vlan100', 'description': 'Users', 'ip_address': '10.20.10.1',
     'ip_prefix': '24', 'active_gw_ip': '10.20.10.254', 'vrf': 'PROD',
     'ospf_area': '0.0.0.0'},
    {'name': 'vlan200', 'ip_address': '10.20.20.1', 'ip_prefix': '24',
     'active_gw_ip': '10.20.20.250'},
    {'name': 'vlan999', 'ip_address': '10.20.99.1', 'ip_prefix': '24'},
]}


def _run(functions, call):
    if shutil.which('node') is None:
        pytest.skip('node not installed here')
    script = (
        "let out = null; const setVlanIfs = v => { out = v; };\n"
        "const SELECTED_HOST = 'RIV-Core-01';\n"
        "const PROJECT_CONVS = {svi:{gateway_offset:1, active_gateway_offset:254}};\n"
        "const PROJECT_ALLOCS = %s;\nlet vlanIfs = [];\n%s\n"
        "const msg = %s;\n"
        "console.log(JSON.stringify({msg, out}));\n"
    ) % (json.dumps(ALLOCS), functions, call)
    res = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@pytest.fixture
def functions(app, auth_client):
    return _extract(_editor_page(app, auth_client))


class TestCrossSite:

    def test_vlans_land_in_the_target_sites_blocks(self, functions):
        r = _run(functions, "copySvisFrom(%s, 'riv')" % json.dumps(SOURCE))
        svis = {s['name']: s for s in r['out']}
        assert svis['vlan100']['subnet'] == '10.21.10.0/24'
        assert svis['vlan200']['subnet'] == '10.21.20.0/24'

    def test_addresses_come_from_the_target_and_skip_taken_ones(self, functions):
        r = _run(functions, "copySvisFrom(%s, 'riv')" % json.dumps(SOURCE))
        svis = {s['name']: s for s in r['out']}
        assert svis['vlan100']['ip_address'] == '10.21.10.2', '.1 belongs to RIV-Core-02'
        assert svis['vlan200']['ip_address'] == '10.21.20.1'

    def test_active_gateway_keeps_its_position_not_its_address(self, functions):
        r = _run(functions, "copySvisFrom(%s, 'riv')" % json.dumps(SOURCE))
        svis = {s['name']: s for s in r['out']}
        assert svis['vlan100']['active_gw_ip'] == '10.21.10.254'
        assert svis['vlan200']['active_gw_ip'] == '10.21.20.250'

    def test_settings_carry_over(self, functions):
        r = _run(functions, "copySvisFrom(%s, 'riv')" % json.dumps(SOURCE))
        svi = [s for s in r['out'] if s['name'] == 'vlan100'][0]
        assert svi['vrf'] == 'PROD'
        assert svi['ospf_area'] == '0.0.0.0'
        assert svi['description'] == 'Users'

    def test_unmatched_vlan_comes_over_unaddressed_and_is_named(self, functions):
        r = _run(functions, "copySvisFrom(%s, 'riv')" % json.dumps(SOURCE))
        svi = [s for s in r['out'] if s['name'] == 'vlan999'][0]
        assert svi['subnet'] == '' and svi['ip_address'] == ''
        assert 'vlan999' in r['msg']

    def test_no_source_address_survives_a_cross_site_copy(self, functions):
        r = _run(functions, "copySvisFrom(%s, 'riv')" % json.dumps(SOURCE))
        for s in r['out']:
            assert not str(s.get('ip_address', '')).startswith('10.20.'), s
            assert not str(s.get('active_gw_ip', '')).startswith('10.20.'), s


class TestSameSite:

    def test_next_free_address_in_the_source_subnet(self, functions):
        r = _run(functions, "copySvisFrom(%s, '')" % json.dumps(SOURCE))
        svis = {s['name']: s for s in r['out']}
        assert svis['vlan100']['ip_address'] == '10.20.10.2'
        assert svis['vlan100']['active_gw_ip'] == '10.20.10.254'

    def test_two_svis_in_one_subnet_get_different_addresses(self, functions):
        src = {'vlanIfs': [
            {'name': 'vlan100', 'ip_address': '10.20.10.1'},
            {'name': 'vlan101', 'ip_address': '10.20.10.9'},
        ]}
        r = _run(functions, "copySvisFrom(%s, '')" % json.dumps(src))
        addrs = [s['ip_address'] for s in r['out']]
        assert addrs == ['10.20.10.2', '10.20.10.3']

    def test_address_outside_any_block_is_not_duplicated(self, functions):
        """It was copied verbatim, which can only ever clash."""
        r = _run(functions, "copySvisFrom(%s, '')" % json.dumps(SOURCE))
        svi = [s for s in r['out'] if s['name'] == 'vlan999'][0]
        assert svi['ip_address'] == ''
        assert 'vlan999' in r['msg']


class TestDetection:

    def test_defaults_to_the_supernet_this_switch_already_uses(self, functions):
        r = _run(functions, 'detectSviSupernet()')
        assert r['msg'] == 'riv'


class TestPanel:

    def test_addressing_selector_is_offered(self, app, auth_client):
        body = _editor_page(app, auth_client)
        assert 'svi-copy-target' in body
        assert "Same subnets as the source switch" in body
