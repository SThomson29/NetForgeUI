"""
Project checks: cross-switch mistakes, reported as warnings only.

Each check is exercised for what it should flag and for the legitimate
setups it must leave alone — a check that cries wolf on a normal design
gets ignored.
"""

import os
import time
from unittest.mock import patch

import pytest

from app import checks
from app.project import (create_project, save_project_config, get_project_config,
                         _save_allocations, _load_allocations, sync_allocations,
                         project_host_vars_dir, project_generated_configs_dir,
                         record_generated_from)
from app.utils import add_project_hosts

P = 'p'


@pytest.fixture
def project(app, skeleton_dir):
    """A project with a builder for switches and their host_vars."""
    with app.app_context():
        create_project(app, 'admin', P)

    class Builder:
        def add(self, *names, stacking='none'):
            add_project_hosts(app, 'admin', P, list(names), stacking)
            return self

        def write(self, host, fname, text):
            path = os.path.join(project_host_vars_dir(app, 'admin', P), host, fname)
            with open(path, 'w') as f:
                f.write(text)
            return self

        def pools(self, *pools):
            cfg = get_project_config(app, 'admin', P)
            cfg['pools'] = list(pools)
            save_project_config(app, 'admin', P, cfg)
            return self

        def allocs(self, **parts):
            a = _load_allocations(app, 'admin', P)
            a.update(parts)
            _save_allocations(app, 'admin', P, a)
            return self

        def run(self):
            return checks.run(app, 'admin', P)

        def keys(self, check=None):
            return {f['key'] for f in self.run() if check in (None, f['check'])}

    return Builder()


def loopbacks(*pairs):
    """interfaces.yml with loopbacks [(name, ip)]."""
    body = ''.join('  - name: %s\n    ip_address: "%s"\n' % p for p in pairs)
    return ('physical_interfaces: []\nlag_interfaces: []\nvlan_interfaces: []\n'
            'loopback_interfaces:\n' + body)


def svis(*rows):
    """interfaces.yml with SVIs [(name, ip, prefix, agw)]."""
    body = ''.join('  - name: %s\n    ip_address: "%s"\n    ip_prefix: %s\n'
                   '    active_gateway_ip: "%s"\n' % r for r in rows)
    return ('physical_interfaces: []\nlag_interfaces: []\nloopback_interfaces: []\n'
            'vlan_interfaces:\n' + body)


def vsx(role='primary', mac='', src='', peer='', vrf=''):
    return ('vsx:\n  enabled: true\n  role: %s\n  system_mac: "%s"\n  isl_port: lag256\n'
            '  keepalive:\n    peer_ip: "%s"\n    src_ip: "%s"\n    vrf: "%s"\n'
            % (role, mac, peer, src, vrf))


def vtep(ip, vnis=()):
    body = ''.join('    - vni: %s\n      vlan: %s\n' % v for v in vnis) or '    []\n'
    return ('loopback_interface: loopback1\nloopback_ip: "%s"\nvxlan:\n  vni_map:\n%s'
            % (ip, body))


def vlans(*ids):
    return 'vlans:\n' + ''.join('  - id: %s\n    name: V%s\n' % (i, i) for i in ids)


class TestCleanProject:

    def test_fresh_switches_raise_nothing(self, project):
        project.add('SW1', 'SW2').add('VX1', 'VX2', stacking='vsx').add('ST1', stacking='vsf')
        assert project.run() == []

    def test_no_switches(self, project):
        assert project.run() == []


class TestDuplicateAddresses:

    def test_same_loopback_on_two_switches(self, project):
        project.add('SW1', 'SW2')
        project.write('SW1', 'interfaces.yml', loopbacks(('loopback0', '10.0.0.1')))
        project.write('SW2', 'interfaces.yml', loopbacks(('loopback0', '10.0.0.1/32')))
        (f,) = project.run()
        assert f['key'] == 'dup_ip:10.0.0.1' and f['severity'] == 'error'
        assert f['details'] == ['SW1 loopback0', 'SW2 loopback0']

    def test_two_interfaces_on_one_switch(self, project):
        project.add('SW1')
        project.write('SW1', 'interfaces.yml',
                      loopbacks(('loopback0', '10.0.0.1'), ('loopback2', '10.0.0.1')))
        assert project.keys() == {'dup_ip:10.0.0.1'}

    def test_shared_active_gateway_is_fine(self, project):
        project.add('SW1', 'SW2', 'SW3')
        for i, h in enumerate(('SW1', 'SW2', 'SW3'), start=1):
            project.write(h, 'interfaces.yml',
                          svis(('vlan20', '10.20.0.%d' % i, 24, '10.20.0.254')))
            project.write(h, 'vlans.yml', vlans(20))
        assert project.run() == []

    def test_active_gateway_on_different_vlans_is_flagged(self, project):
        project.add('SW1', 'SW2')
        project.write('SW1', 'interfaces.yml', svis(('vlan20', '', 24, '10.0.0.254')))
        project.write('SW2', 'interfaces.yml', svis(('vlan30', '', 24, '10.0.0.254')))
        project.write('SW1', 'vlans.yml', vlans(20))
        project.write('SW2', 'vlans.yml', vlans(30))
        assert project.keys() == {'dup_ip:10.0.0.254'}

    def test_anycast_vtep_pair_is_fine_but_not_three(self, project):
        project.add('A', 'B', 'C')
        project.write('A', 'vxlan.yml', vtep('10.9.9.1'))
        project.write('B', 'vxlan.yml', vtep('10.9.9.1'))
        assert project.run() == []
        project.write('C', 'vxlan.yml', vtep('10.9.9.1'))
        assert project.keys() == {'dup_ip:10.9.9.1'}

    def test_vtep_also_listed_as_its_loopback_counts_once(self, project):
        project.add('A')
        project.write('A', 'vxlan.yml', vtep('10.9.9.1'))
        project.write('A', 'interfaces.yml', loopbacks(('loopback1', '10.9.9.1')))
        assert project.run() == []


SUPERNET = {'id': 'sn', 'name': 'Users', 'type': 'vlan_supernet', 'subnet': '10.20.0.0/16'}


class TestSviSubnets:

    def _blocks(self, project, **extra):
        project.pools(SUPERNET).allocs(vlan_supernet={'sn': {
            '10.20.20.0/24': {'status': 'assigned', 'vlan_id': 20, 'hostname': None},
            '10.20.30.0/24': {'status': 'assigned', 'vlan_id': '30', 'hostname': None},
            **extra}})

    def test_address_in_another_vlans_subnet(self, project):
        project.add('SW1')
        self._blocks(project)
        project.write('SW1', 'vlans.yml', vlans(20, 30))
        project.write('SW1', 'interfaces.yml', svis(('vlan20', '10.20.30.1', 24, '')))
        (f,) = [f for f in project.run() if f['check'] == 'svi_subnet']
        assert "VLAN 30's subnet" in f['title']

    def test_address_outside_its_own_vlans_subnet(self, project):
        project.add('SW1')
        self._blocks(project)
        project.write('SW1', 'vlans.yml', vlans(20))
        project.write('SW1', 'interfaces.yml', svis(('vlan20', '10.20.99.1', 24, '')))
        (f,) = [f for f in project.run() if f['check'] == 'svi_subnet']
        assert "outside VLAN 20's subnet" in f['title']

    def test_block_for_another_switch_is_ignored(self, project):
        project.add('SW1')
        self._blocks(project, **{'10.20.40.0/24': {
            'status': 'assigned', 'vlan_id': 40, 'hostname': 'OTHER'}})
        project.write('SW1', 'vlans.yml', vlans(40))
        project.write('SW1', 'interfaces.yml', svis(('vlan40', '10.20.41.1', 24, '')))
        assert not project.keys('svi_subnet')

    def test_active_gateway_outside_the_svi_subnet(self, project):
        project.add('SW1')
        project.write('SW1', 'vlans.yml', vlans(20))
        project.write('SW1', 'interfaces.yml', svis(('vlan20', '10.20.20.1', 24, '10.20.21.254')))
        assert project.keys('svi_subnet') == {'svi_subnet:SW1:vlan20:active gateway'}

    def test_correct_svi_is_quiet(self, project):
        project.add('SW1')
        self._blocks(project)
        project.write('SW1', 'vlans.yml', vlans(20))
        project.write('SW1', 'interfaces.yml', svis(('vlan20', '10.20.20.1', 24, '10.20.20.254')))
        assert not project.keys('svi_subnet')


LOOPS = {'id': 'lo', 'name': 'Loopbacks', 'type': 'unique', 'subnet': '10.0.0.0/24'}


class TestAllocationDrift:

    def test_after_a_save_resources_match(self, app, project):
        project.add('SW1').pools(LOOPS)
        project.write('SW1', 'interfaces.yml', loopbacks(('loopback0', '10.0.0.1')))
        sync_allocations(app, 'admin', P, 'SW1')
        assert project.run() == []

    def test_used_but_not_recorded(self, project):
        project.add('SW1').pools(LOOPS)
        project.write('SW1', 'interfaces.yml', loopbacks(('loopback0', '10.0.0.1')))
        (f,) = project.run()
        assert f['key'] == 'alloc_drift:unrecorded:SW1' and f['severity'] == 'warning'
        assert f['details'] == ['loopback0 10.0.0.1 (Loopbacks)']

    def test_recorded_but_not_used(self, project):
        project.add('SW1').pools(LOOPS).allocs(unique={'lo': {
            '10.0.0.5': {'hostname': 'SW1', 'interface': 'loopback0'}}})
        assert project.keys() == {'alloc_drift:unused:SW1'}

    def test_allocated_to_a_removed_switch(self, project):
        project.add('SW1').pools(LOOPS).allocs(unique={'lo': {
            '10.0.0.5': {'hostname': 'GONE', 'interface': 'loopback0'}}})
        assert project.keys() == {'alloc_drift:gone:GONE'}

    def test_reservation_for_a_removed_switch(self, project):
        ptp = {'id': 'pp', 'name': 'Links', 'type': 'point_to_point', 'subnet': '10.1.0.0/24'}
        project.add('SW1').pools(ptp).allocs(point_to_point={'pp': {
            '10.1.0.1': {'hostname': None, 'status': 'reserved_for_peer',
                         'peer_hostname': 'GONE', 'peer_ip': '10.1.0.0'}}})
        assert project.keys() == {'alloc_drift:gone:GONE'}

    def test_shared_vtep_held_by_both(self, app, project):
        vt = {'id': 'vt', 'name': 'VTEPs', 'type': 'unique', 'subnet': '10.9.9.0/24'}
        project.add('A', 'B').pools(vt)
        for h in ('A', 'B'):
            project.write(h, 'vxlan.yml', vtep('10.9.9.1'))
            sync_allocations(app, 'admin', P, h)
        assert project.run() == []


class TestOutsidePools:

    def test_only_when_pools_exist(self, project):
        project.add('SW1')
        project.write('SW1', 'interfaces.yml', loopbacks(('loopback0', '192.168.1.1')))
        assert project.run() == []
        project.pools(LOOPS)
        (f,) = project.run()
        assert f['key'] == 'outside_pool:SW1' and f['severity'] == 'warning'


class TestVsx:

    def _pair(self, project, a=None, b=None):
        project.add('A', 'B', stacking='vsx')
        project.write('A', 'vsx.yml', a or vsx('primary', '02:00:00:00:00:01', '192.168.0.0', '192.168.0.1'))
        project.write('B', 'vsx.yml', b or vsx('secondary', '02:00:00:00:00:01', '192.168.0.1', '192.168.0.0'))

    def test_good_pair(self, project):
        self._pair(project)
        assert project.run() == []

    def test_both_primary(self, project):
        self._pair(project, b=vsx('primary', '02:00:00:00:00:01', '192.168.0.1', '192.168.0.0'))
        assert project.keys() == {'vsx_pair:A+B:role'}

    def test_system_mac_differs(self, project):
        self._pair(project, b=vsx('secondary', '02:00:00:00:00:02', '192.168.0.1', '192.168.0.0'))
        assert project.keys() == {'vsx_pair:A+B:mac'}

    def test_mac_formats_compare_equal(self, project):
        self._pair(project, b=vsx('secondary', '0200.0000.0001', '192.168.0.1', '192.168.0.0'))
        assert project.run() == []

    def test_keepalive_not_mirrored(self, project):
        self._pair(project, b=vsx('secondary', '02:00:00:00:00:01', '192.168.0.1', '192.168.0.9'))
        assert project.keys() == {'vsx_pair:A+B:keepalive'}

    def test_peer_not_in_project(self, project):
        project.add('A', stacking='vsx')
        project.write('A', 'vsx.yml', vsx('primary', '', '192.168.0.0', '192.168.0.1'))
        (f,) = project.run()
        assert f['key'] == 'vsx_pair:no-peer:A' and f['severity'] == 'warning'

    def test_paired_by_mac_when_keepalives_are_blank(self, project):
        self._pair(project, vsx('primary', '02:00:00:00:00:01'), vsx('primary', '02:00:00:00:00:01'))
        assert project.keys() == {'vsx_pair:A+B:role'}

    def test_different_anycast_vteps(self, project):
        self._pair(project)
        project.write('A', 'vxlan.yml', vtep('10.9.9.1'))
        project.write('B', 'vxlan.yml', vtep('10.9.9.2'))
        assert project.keys() == {'vsx_vtep:A+B'}

    def test_disabled_vsx_is_ignored(self, project):
        project.add('A', 'B', stacking='vsx')
        project.write('A', 'vsx.yml', vsx('primary', '', '1.1.1.1', '2.2.2.2').replace(
            'enabled: true', 'enabled: false'))
        assert project.run() == []


class TestVsf:

    def _vsf(self, *ids):
        body = ''.join('    - id: %s\n' % i for i in ids) if ids else '    []\n'
        return 'vsf:\n  enabled: true\n  members:\n' + body

    def test_duplicates_and_range(self, project):
        project.add('ST', stacking='vsf')
        project.write('ST', 'vsf.yml', self._vsf(1, 2, 2, 11))
        assert project.keys() == {'vsf_members:ST:dup', 'vsf_members:ST:range'}

    def test_enabled_without_members(self, project):
        project.add('ST', stacking='vsf')
        project.write('ST', 'vsf.yml', self._vsf())
        assert project.keys() == {'vsf_members:ST:empty'}

    def test_good_stack(self, project):
        project.add('ST', stacking='vsf')
        project.write('ST', 'vsf.yml', self._vsf(1, 2, 3))
        assert project.run() == []


class TestVniMap:

    def test_vni_on_two_vlans_and_vlan_on_two_vnis(self, project):
        project.add('A', 'B')
        project.write('A', 'vxlan.yml', vtep('', [(10020, 20)]))
        project.write('B', 'vxlan.yml', vtep('', [(10020, 21), (10099, 20)]))
        project.write('A', 'vlans.yml', vlans(20))
        project.write('B', 'vlans.yml', vlans(20, 21))
        assert project.keys('vni_map') == {'vni_map:vni:10020', 'vni_map:vlan:20'}

    def test_consistent_map(self, project):
        project.add('A', 'B')
        for h in ('A', 'B'):
            project.write(h, 'vxlan.yml', vtep('', [(10020, 20)]))
            project.write(h, 'vlans.yml', vlans(20))
        assert project.run() == []


class TestVlanAndVrfReferences:

    def test_missing_vlans_are_listed_compactly(self, project):
        project.add('SW1')
        project.write('SW1', 'vlans.yml', vlans(10, 20))
        project.write('SW1', 'interfaces.yml', (
            'loopback_interfaces: []\nlag_interfaces: []\nvlan_interfaces:\n'
            '  - name: vlan30\n    ip_address: ""\n'
            'physical_interfaces:\n'
            '  - name: 1/1/1\n    port_type: trunk\n    trunk_allowed_vlans: "1,10,20-23"\n'
            '    trunk_native_vlan: 1\n'
            '  - name: 1/1/2\n    port_type: trunk\n    trunk_allowed_vlans: "all"\n'
            '  - name: 1/1/3\n    port_type: access\n    access_vlan: "10"\n'))
        (f,) = project.run()
        assert f['key'] == 'vlan_ref:SW1'
        assert f['details'] == ['SVI vlan30: 30', '1/1/1 allowed: 21-23']

    def test_lag_members_and_routed_ports_are_skipped(self, project):
        project.add('SW1')
        project.write('SW1', 'interfaces.yml', (
            'loopback_interfaces: []\nlag_interfaces: []\nvlan_interfaces: []\n'
            'physical_interfaces:\n'
            '  - name: 1/1/1\n    lag_member: "1"\n    port_type: access\n    access_vlan: "99"\n'
            '  - name: 1/1/2\n    routed: true\n    ip_address: ""\n'))
        assert project.run() == []

    def test_undefined_vrf(self, project):
        project.add('SW1')
        project.write('SW1', 'vrfs.yml', 'vrfs:\n  - name: KEEPALIVE\n')
        project.write('SW1', 'management.yml', 'management:\n  vrf: mgmt\nlocal_users: []\n')
        project.write('SW1', 'interfaces.yml', (
            'physical_interfaces: []\nlag_interfaces: []\nvlan_interfaces: []\n'
            'loopback_interfaces:\n'
            '  - name: loopback0\n    vrf: "default"\n    ip_address: ""\n'
            '  - name: loopback5\n    vrf: "TENANT_A"\n    ip_address: ""\n'
            '  - name: loopback6\n    vrf: "KEEPALIVE"\n    ip_address: ""\n'))
        (f,) = project.run()
        assert f['key'] == 'vrf_ref:SW1' and f['details'] == ['TENANT_A: loopback5']


class TestRouterIds:

    def _routing(self, bgp_rid='', ospf=()):
        inst = ''.join('  - enabled: true\n    process_id: 1\n    router_id: "%s"\n'
                       '    vrf: "%s"\n    areas: []\n' % o for o in ospf) or '  []\n'
        return ('ospf_instances:\n%sdevice_role: ""\nbgp:\n  asn: "65000"\n'
                '  router_id: "%s"\n  neighbors: []\n' % (inst, bgp_rid))

    def test_duplicate_bgp_and_ospf(self, project):
        project.add('A', 'B')
        project.write('A', 'routing.yml', self._routing('10.0.0.1', [('10.0.0.1', '')]))
        project.write('B', 'routing.yml', self._routing('10.0.0.1', [('10.0.0.1', '')]))
        assert project.keys() == {'router_id:BGP:10.0.0.1', 'router_id:OSPF:10.0.0.1'}

    def test_same_switch_using_one_id_for_both_is_fine(self, project):
        project.add('A')
        project.write('A', 'routing.yml', self._routing('10.0.0.1', [('10.0.0.1', '')]))
        assert project.run() == []


class TestStaleConfigs:

    def _generated(self, app, project, host='SW1', name=None):
        name = name or '%s_FULL.ios' % host
        gc = project_generated_configs_dir(app, 'admin', P)
        with open(os.path.join(gc, name), 'w') as f:
            f.write('config\n')
        fp = checks.hostvars_fingerprint(
            os.path.join(project_host_vars_dir(app, 'admin', P), host))
        record_generated_from(app, 'admin', P, {name: fp})

    def test_edit_after_generation(self, app, project):
        project.add('SW1')
        self._generated(app, project)
        assert project.run() == []
        project.write('SW1', 'vlans.yml', vlans(10))
        (f,) = project.run()
        assert f['key'] == 'stale_config:SW1' and f['details'] == ['SW1_FULL.ios']

    def test_saving_without_changes_is_not_an_edit(self, app, project):
        project.add('SW1')
        project.write('SW1', 'vlans.yml', vlans(10))
        self._generated(app, project)
        project.write('SW1', 'vlans.yml', vlans(10))
        assert project.run() == []

    def test_config_from_before_this_was_recorded_is_not_judged(self, app, project):
        project.add('SW1')
        gc = project_generated_configs_dir(app, 'admin', P)
        with open(os.path.join(gc, 'SW1_FULL.ios'), 'w') as f:
            f.write('config\n')
        project.write('SW1', 'vlans.yml', vlans(10))
        assert project.run() == []

    def test_deleted_config_is_forgotten(self, app, project):
        project.add('SW1')
        self._generated(app, project)
        os.remove(os.path.join(project_generated_configs_dir(app, 'admin', P), 'SW1_FULL.ios'))
        record_generated_from(app, 'admin', P, {})
        assert get_project_config(app, 'admin', P)['generated_from'] == {}

    @pytest.mark.parametrize('tags, name', [
        ('', 'SW1_FULL.ios'),
        ('full', 'SW1_FULL.ios'),
        ('vxlan', 'SW1_PARTIAL_vxlan.ios'),
        ('vxlan,bgp', 'SW1_PARTIAL_bgp-vxlan.ios'),
        (' vlans , aaa ', 'SW1_PARTIAL_aaa-vlans.ios'),
    ])
    def test_generated_name_matches_the_role(self, tags, name):
        assert checks.generated_name('SW1', tags) == name


def _wait_generated_from(app, want):
    for _ in range(200):
        got = get_project_config(app, 'admin', P).get('generated_from') or {}
        if set(want) <= set(got):
            return got
        time.sleep(0.01)
    raise AssertionError('generated_from never recorded %r' % want)


class TestGenerationRecordsFingerprints:

    def _generate(self, app, auth_client, files, body):
        import app.docker_runner as D

        def fake(**kw):
            gc = project_generated_configs_dir(app, 'admin', P)
            for name in files:
                with open(os.path.join(gc, name), 'w') as f:
                    f.write('generated %s\n' % time.time())
            return 0, 'PLAY RECAP\n'

        with patch.object(D, 'run_generate', fake):
            res = auth_client.post('/projects/%s/generate/run' % P, json=body)
        assert res.status_code == 200

    def test_full_run_then_edit(self, app, auth_client, project):
        project.add('SW1', 'SW2')
        self._generate(app, auth_client, ['SW1_FULL.ios', 'SW2_FULL.ios'],
                       {'limit': '', 'tags': ''})
        _wait_generated_from(app, ['SW1_FULL.ios', 'SW2_FULL.ios'])
        assert project.run() == []
        project.write('SW2', 'vlans.yml', vlans(5))
        assert project.keys() == {'stale_config:SW2'}

    def test_limit_by_group_and_partial_tags(self, app, auth_client, project):
        project.add('SW1').add('VX1', stacking='vsx')
        self._generate(app, auth_client, ['VX1_PARTIAL_vxlan.ios'],
                       {'limit': 'cx_vsx', 'tags': 'vxlan'})
        got = _wait_generated_from(app, ['VX1_PARTIAL_vxlan.ios'])
        assert set(got) == {'VX1_PARTIAL_vxlan.ios'}


class TestDeploymentIps:

    def _mapping(self, app, rows):
        from app.deploy_routes import _save_mapping
        _save_mapping(app, 'admin', P, {'hosts': rows, 'settings': {}})

    def test_only_once_the_mapping_is_used(self, app, project):
        project.add('SW1', 'SW2')
        assert project.run() == []
        self._mapping(app, [{'hostname': 'SW1', 'mgmt_ip': '10.0.0.1'},
                            {'hostname': 'SW2', 'mgmt_ip': ''}])
        assert project.keys() == {'no_mgmt_ip:SW2'}

    def test_shared_ips_are_fine(self, app, project):
        project.add('SW1', 'SW2')
        self._mapping(app, [{'hostname': 'SW1', 'mgmt_ip': '192.168.1.1'},
                            {'hostname': 'SW2', 'mgmt_ip': '192.168.1.1'}])
        assert project.run() == []


class TestUnreadable:

    def test_bad_yaml_is_a_finding_not_a_crash(self, project, auth_client):
        project.add('SW1', 'SW2')
        project.write('SW1', 'vlans.yml', 'vlans: [unclosed\n')
        assert project.keys() == {'unreadable:SW1'}
        assert auth_client.get('/projects/%s/checks' % P).status_code == 200


class TestPagesAndDismissal:

    def _dup(self, project):
        project.add('SW1', 'SW2')
        project.write('SW1', 'interfaces.yml', loopbacks(('loopback0', '10.0.0.1')))
        project.write('SW2', 'interfaces.yml', loopbacks(('loopback0', '10.0.0.1')))

    def test_page_lists_findings(self, project, auth_client):
        self._dup(project)
        body = auth_client.get('/projects/%s/checks' % P).get_data(as_text=True)
        assert '10.0.0.1 is used on 2 interfaces' in body
        assert 'SW2 loopback0' in body

    def test_page_with_nothing_found(self, project, auth_client):
        project.add('SW1')
        body = auth_client.get('/projects/%s/checks' % P).get_data(as_text=True)
        assert 'No open findings across 1 switch' in body

    def test_banner_on_generate_and_deploy(self, project, auth_client):
        self._dup(project)
        for page in ('generate', 'deploy'):
            body = auth_client.get('/projects/%s/%s' % (P, page)).get_data(as_text=True)
            assert 'Project checks:' in body and '1 error' in body, page

    def test_no_banner_when_clean(self, project, auth_client):
        project.add('SW1')
        body = auth_client.get('/projects/%s/generate' % P).get_data(as_text=True)
        assert 'Project checks:' not in body

    def test_dismiss_until_details_change(self, project, auth_client):
        self._dup(project)
        res = auth_client.post('/projects/%s/checks/dismiss' % P, json={'key': 'dup_ip:10.0.0.1'})
        assert res.get_json() == {'ok': True}
        body = auth_client.get('/projects/%s/generate' % P).get_data(as_text=True)
        assert 'Project checks:' not in body
        assert '1 dismissed' in auth_client.get('/projects/%s/checks' % P).get_data(as_text=True)

        # A third switch joins in: the finding says something new, so it's back.
        project.add('SW3')
        project.write('SW3', 'interfaces.yml', loopbacks(('loopback0', '10.0.0.1')))
        assert 'Project checks:' in auth_client.get(
            '/projects/%s/generate' % P).get_data(as_text=True)

    def test_restore(self, project, auth_client):
        self._dup(project)
        auth_client.post('/projects/%s/checks/dismiss' % P, json={'key': 'dup_ip:10.0.0.1'})
        auth_client.post('/projects/%s/checks/restore' % P, json={'key': 'dup_ip:10.0.0.1'})
        assert 'Project checks:' in auth_client.get(
            '/projects/%s/generate' % P).get_data(as_text=True)

    def test_only_real_findings_can_be_dismissed(self, project, auth_client):
        project.add('SW1')
        res = auth_client.post('/projects/%s/checks/dismiss' % P, json={'key': 'made:up'})
        assert res.status_code == 404
        assert auth_client.post('/projects/%s/checks/dismiss' % P, json={}).status_code == 400

    def test_banner_survives_a_failing_check(self, project, auth_client):
        project.add('SW1')
        with patch.object(checks, 'run', side_effect=RuntimeError('boom')):
            assert auth_client.get('/projects/%s/generate' % P).status_code == 200

    def test_sidebar_link(self, project, auth_client):
        project.add('SW1')
        body = auth_client.get('/projects/%s/hosts' % P).get_data(as_text=True)
        assert '/checks"' in body
