"""
Unit tests for app/project.py

Covers: project CRUD, pool management, all allocation types,
        sync_allocations IP validation, stale cleanup.
"""

import os
import json
import ipaddress
import pytest

from app.project import (
    create_project, delete_project, list_projects,
    get_project_config, save_project_config,
    add_pool, remove_pool,
    get_available_ips, allocate_unique, release_unique,
    get_available_ptp_pairs, allocate_ptp, release_ptp,
    get_carved_subnets, assign_vlan_subnet, release_vlan_subnet,
    get_common, save_common,
    get_conventions, save_conventions,
    get_all_allocations,
    sync_allocations,
    project_dir, project_config_path, project_allocations_path,
    project_host_vars_dir,
)


USERNAME = 'testuser'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_app(tmp_data_dir):
    """Create a minimal mock app object with required config."""
    class MockApp:
        config = {
            'DATA_DIR': os.path.join(tmp_data_dir, 'users'),
        }
    return MockApp()


def write_interfaces(app, project_name, hostname, content):
    """Write an interfaces.yml for a host in a project."""
    hvdir = os.path.join(
        project_host_vars_dir(app, USERNAME, project_name), hostname
    )
    os.makedirs(hvdir, exist_ok=True)
    with open(os.path.join(hvdir, 'interfaces.yml'), 'w') as f:
        f.write(content)


def write_vxlan(app, project_name, hostname, content):
    hvdir = os.path.join(
        project_host_vars_dir(app, USERNAME, project_name), hostname
    )
    os.makedirs(hvdir, exist_ok=True)
    with open(os.path.join(hvdir, 'vxlan.yml'), 'w') as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------

class TestProjectCRUD:

    def test_create_project(self, tmp_data_dir):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'my-project')
        projects = list_projects(app, USERNAME)
        assert any(p['name'] == 'my-project' for p in projects)

    def test_create_project_creates_directory_structure(self, tmp_data_dir):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'my-project')
        pdir = project_dir(app, USERNAME, 'my-project')
        assert os.path.isdir(pdir)
        assert os.path.isfile(project_config_path(app, USERNAME, 'my-project'))
        assert os.path.isfile(project_allocations_path(app, USERNAME, 'my-project'))

    def test_create_project_invalid_name(self, tmp_data_dir):
        app = make_app(tmp_data_dir)
        with pytest.raises(ValueError, match='only contain'):
            create_project(app, USERNAME, 'my project!')

    def test_create_project_duplicate(self, tmp_data_dir):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'dup')
        with pytest.raises(ValueError, match='already exists'):
            create_project(app, USERNAME, 'dup')

    def test_delete_project(self, tmp_data_dir):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'to-delete')
        delete_project(app, USERNAME, 'to-delete')
        projects = list_projects(app, USERNAME)
        assert not any(p['name'] == 'to-delete' for p in projects)

    def test_delete_nonexistent_project_is_safe(self, tmp_data_dir):
        app = make_app(tmp_data_dir)
        # Should not raise
        delete_project(app, USERNAME, 'does-not-exist')

    def test_list_projects_empty(self, tmp_data_dir):
        app = make_app(tmp_data_dir)
        assert list_projects(app, USERNAME) == []

    def test_list_projects_multiple(self, tmp_data_dir):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'alpha')
        create_project(app, USERNAME, 'beta')
        names = [p['name'] for p in list_projects(app, USERNAME)]
        assert 'alpha' in names
        assert 'beta' in names

    def test_get_project_config(self, tmp_data_dir):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        cfg = get_project_config(app, USERNAME, 'proj')
        assert cfg['name'] == 'proj'
        assert 'pools' in cfg
        assert 'conventions' in cfg
        assert 'common' in cfg

    def test_save_project_config(self, tmp_data_dir):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        cfg = get_project_config(app, USERNAME, 'proj')
        cfg['name'] = 'Updated Name'
        save_project_config(app, USERNAME, 'proj', cfg)
        cfg2 = get_project_config(app, USERNAME, 'proj')
        assert cfg2['name'] == 'Updated Name'


# ---------------------------------------------------------------------------
# Pool management
# ---------------------------------------------------------------------------

class TestPoolManagement:

    def test_add_unique_pool(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['unique'])
        cfg = get_project_config(app, USERNAME, 'proj')
        assert any(p['id'] == 'pool_loopbacks' for p in cfg['pools'])

    def test_add_ptp_pool(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['ptp'])
        cfg = get_project_config(app, USERNAME, 'proj')
        assert any(p['type'] == 'point_to_point' for p in cfg['pools'])

    def test_add_vlan_supernet_pool_carves_subnets(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['vlan'])
        subnets = get_carved_subnets(app, USERNAME, 'proj', 'pool_vlans')
        # 10.100.0.0/16 carved to /24 = 256 subnets
        assert len(subnets) == 256
        assert '10.100.0.0/24' in subnets
        assert '10.100.255.0/24' in subnets

    def test_all_carved_subnets_have_status_carved(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['vlan'])
        subnets = get_carved_subnets(app, USERNAME, 'proj', 'pool_vlans')
        for subnet, entry in subnets.items():
            assert entry['status'] == 'carved', f'{subnet} not carved'

    def test_remove_pool(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['unique'])
        remove_pool(app, USERNAME, 'proj', 'pool_loopbacks')
        cfg = get_project_config(app, USERNAME, 'proj')
        assert not any(p['id'] == 'pool_loopbacks' for p in cfg['pools'])

    def test_remove_pool_clears_allocations(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['unique'])
        allocate_unique(app, USERNAME, 'proj', 'pool_loopbacks',
                        '10.255.0.1', 'sw-01', 'loopback0')
        remove_pool(app, USERNAME, 'proj', 'pool_loopbacks')
        allocs = get_all_allocations(app, USERNAME, 'proj')
        assert 'pool_loopbacks' not in allocs['unique']

    def test_multiple_pools(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        for pool in sample_pools.values():
            add_pool(app, USERNAME, 'proj', pool)
        cfg = get_project_config(app, USERNAME, 'proj')
        assert len(cfg['pools']) == 3


# ---------------------------------------------------------------------------
# Unique pool allocations
# ---------------------------------------------------------------------------

class TestUniqueAllocations:

    def test_allocate_unique(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['unique'])
        allocate_unique(app, USERNAME, 'proj', 'pool_loopbacks',
                        '10.255.0.1', 'sw-01', 'loopback0')
        allocs = get_all_allocations(app, USERNAME, 'proj')
        entry = allocs['unique']['pool_loopbacks']['10.255.0.1']
        assert entry['hostname'] == 'sw-01'
        assert entry['interface'] == 'loopback0'

    def test_release_unique(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['unique'])
        allocate_unique(app, USERNAME, 'proj', 'pool_loopbacks',
                        '10.255.0.1', 'sw-01', 'loopback0')
        release_unique(app, USERNAME, 'proj', 'pool_loopbacks', '10.255.0.1')
        allocs = get_all_allocations(app, USERNAME, 'proj')
        assert '10.255.0.1' not in allocs['unique'].get('pool_loopbacks', {})

    def test_available_ips_excludes_allocated(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['unique'])
        allocate_unique(app, USERNAME, 'proj', 'pool_loopbacks',
                        '10.255.0.1', 'sw-01', 'loopback0')
        available = get_available_ips(app, USERNAME, 'proj', 'pool_loopbacks')
        assert '10.255.0.1' not in available

    def test_available_ips_count(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['unique'])
        available = get_available_ips(app, USERNAME, 'proj', 'pool_loopbacks')
        # 10.255.0.0/24 has 254 host addresses
        assert len(available) == 254

    def test_multiple_allocations_same_pool(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['unique'])
        allocate_unique(app, USERNAME, 'proj', 'pool_loopbacks',
                        '10.255.0.1', 'sw-01', 'loopback0')
        allocate_unique(app, USERNAME, 'proj', 'pool_loopbacks',
                        '10.255.0.2', 'sw-02', 'loopback0')
        allocs = get_all_allocations(app, USERNAME, 'proj')
        pool_allocs = allocs['unique']['pool_loopbacks']
        assert '10.255.0.1' in pool_allocs
        assert '10.255.0.2' in pool_allocs


# ---------------------------------------------------------------------------
# Point-to-point allocations
# ---------------------------------------------------------------------------

class TestPtpAllocations:

    def test_allocate_ptp_creates_peer_reservation(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['ptp'])
        peer_ip = allocate_ptp(app, USERNAME, 'proj', 'pool_ptp',
                               '10.254.0.0', 'sw-01', '1/1/1')
        assert peer_ip == '10.254.0.1'
        allocs = get_all_allocations(app, USERNAME, 'proj')
        pool_allocs = allocs['point_to_point']['pool_ptp']
        assert pool_allocs['10.254.0.0']['hostname'] == 'sw-01'
        assert pool_allocs['10.254.0.1']['status'] == 'reserved_for_peer'

    def test_allocate_ptp_odd_ip(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['ptp'])
        peer_ip = allocate_ptp(app, USERNAME, 'proj', 'pool_ptp',
                               '10.254.0.1', 'sw-02', '1/1/1')
        assert peer_ip == '10.254.0.0'
        allocs = get_all_allocations(app, USERNAME, 'proj')
        pool_allocs = allocs['point_to_point']['pool_ptp']
        assert pool_allocs['10.254.0.1']['hostname'] == 'sw-02'
        assert pool_allocs['10.254.0.0']['status'] == 'reserved_for_peer'

    def test_allocate_ptp_both_ends_links_them(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['ptp'])
        allocate_ptp(app, USERNAME, 'proj', 'pool_ptp',
                     '10.254.0.0', 'sw-01', '1/1/1')
        allocate_ptp(app, USERNAME, 'proj', 'pool_ptp',
                     '10.254.0.1', 'sw-02', '1/1/1')
        allocs = get_all_allocations(app, USERNAME, 'proj')
        pool_allocs = allocs['point_to_point']['pool_ptp']
        # Both ends should now be fully allocated with peer info
        assert pool_allocs['10.254.0.0']['peer_hostname'] == 'sw-02'
        assert pool_allocs['10.254.0.1']['peer_hostname'] == 'sw-01'
        assert 'status' not in pool_allocs['10.254.0.1'] or \
               pool_allocs['10.254.0.1'].get('status') != 'reserved_for_peer'

    def test_release_ptp_removes_reservation(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['ptp'])
        allocate_ptp(app, USERNAME, 'proj', 'pool_ptp',
                     '10.254.0.0', 'sw-01', '1/1/1')
        release_ptp(app, USERNAME, 'proj', 'pool_ptp', '10.254.0.0')
        allocs = get_all_allocations(app, USERNAME, 'proj')
        pool_allocs = allocs['point_to_point'].get('pool_ptp', {})
        assert '10.254.0.0' not in pool_allocs
        assert '10.254.0.1' not in pool_allocs

    def test_ptp_available_pairs(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['ptp'])
        pairs = get_available_ptp_pairs(app, USERNAME, 'proj', 'pool_ptp')
        # 10.254.0.0/24 has 128 /31 pairs
        assert len(pairs) == 128
        all_available = [p for p in pairs if p['status'] == 'available']
        assert len(all_available) == 128


# ---------------------------------------------------------------------------
# VLAN supernet allocations
# ---------------------------------------------------------------------------

class TestVlanSupernet:

    def test_carved_subnets_correct_count(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['vlan'])
        subnets = get_carved_subnets(app, USERNAME, 'proj', 'pool_vlans')
        assert len(subnets) == 256

    def test_assign_vlan_subnet(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['vlan'])
        assign_vlan_subnet(app, USERNAME, 'proj', 'pool_vlans',
                           '10.100.5.0/24', 100, 'Management', 'sw-01')
        subnets = get_carved_subnets(app, USERNAME, 'proj', 'pool_vlans')
        assert subnets['10.100.5.0/24']['status'] == 'assigned'
        assert subnets['10.100.5.0/24']['vlan_id'] == 100
        assert subnets['10.100.5.0/24']['vlan_name'] == 'Management'

    def test_assign_vlan_derives_svi_ips(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['vlan'])
        # Conventions: gateway_offset=1, active_gateway_offset=254
        assign_vlan_subnet(app, USERNAME, 'proj', 'pool_vlans',
                           '10.100.5.0/24', 100, 'Management', 'sw-01', 'sw-02')
        allocs = get_all_allocations(app, USERNAME, 'proj')
        svi_allocs = allocs['svi'].get('pool_vlans', {})
        # Gateway at offset 1 = .1
        assert '10.100.5.1' in svi_allocs
        assert svi_allocs['10.100.5.1']['role'] == 'gateway'
        # Active gateway at offset 254 = .254
        assert '10.100.5.254' in svi_allocs
        assert svi_allocs['10.100.5.254']['role'] == 'active_gateway'

    def test_release_vlan_subnet(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['vlan'])
        assign_vlan_subnet(app, USERNAME, 'proj', 'pool_vlans',
                           '10.100.5.0/24', 100, 'Management', 'sw-01')
        release_vlan_subnet(app, USERNAME, 'proj', 'pool_vlans', '10.100.5.0/24')
        subnets = get_carved_subnets(app, USERNAME, 'proj', 'pool_vlans')
        assert subnets['10.100.5.0/24']['status'] == 'carved'
        # SVI IPs should be removed
        allocs = get_all_allocations(app, USERNAME, 'proj')
        svi_allocs = allocs['svi'].get('pool_vlans', {})
        assert '10.100.5.1' not in svi_allocs

    def test_assign_nonexistent_subnet_raises(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['vlan'])
        with pytest.raises(ValueError):
            assign_vlan_subnet(app, USERNAME, 'proj', 'pool_vlans',
                               '192.168.99.0/24', 999, 'Bad', 'sw-01')


# ---------------------------------------------------------------------------
# Conventions and common infra
# ---------------------------------------------------------------------------

class TestConventionsAndCommon:

    def test_save_and_get_conventions(self, tmp_data_dir):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        save_conventions(app, USERNAME, 'proj', {
            'svi': {'gateway_offset': 2, 'active_gateway_offset': 253,
                    'reserved_from_start': 5}
        })
        conv = get_conventions(app, USERNAME, 'proj')
        assert conv['svi']['gateway_offset'] == 2
        assert conv['svi']['active_gateway_offset'] == 253

    def test_save_and_get_common(self, tmp_data_dir):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        save_common(app, USERNAME, 'proj', {
            'dns_servers': ['10.0.0.53'],
            'ntp_servers': ['10.0.0.123', '10.0.0.124'],
            'dhcp_servers': [],
            'radius_servers': ['10.0.0.100'],
            'syslog_servers': [],
        })
        common = get_common(app, USERNAME, 'proj')
        assert common['dns_servers'] == ['10.0.0.53']
        assert len(common['ntp_servers']) == 2


# ---------------------------------------------------------------------------
# sync_allocations
# ---------------------------------------------------------------------------

class TestSyncAllocations:

    def _make_app_with_pool(self, tmp_data_dir, sample_pools):
        app = make_app(tmp_data_dir)
        create_project(app, USERNAME, 'proj')
        add_pool(app, USERNAME, 'proj', sample_pools['unique'])
        add_pool(app, USERNAME, 'proj', sample_pools['ptp'])
        return app

    def test_sync_loopback_ip(self, tmp_data_dir, sample_pools):
        app = self._make_app_with_pool(tmp_data_dir, sample_pools)
        write_interfaces(app, 'proj', 'sw-01', """\
interface_groups: []
physical_interfaces: []
lag_interfaces: []
loopback_interfaces:
  - name: loopback0
    ip_address: "10.255.0.1"
    ip_prefix: "32"
vlan_interfaces: []
""")
        write_vxlan(app, 'proj', 'sw-01', "loopback_interface: loopback1\nloopback_ip:\n")
        sync_allocations(app, USERNAME, 'proj', 'sw-01')
        allocs = get_all_allocations(app, USERNAME, 'proj')
        assert '10.255.0.1' in allocs['unique'].get('pool_loopbacks', {})
        assert allocs['unique']['pool_loopbacks']['10.255.0.1']['hostname'] == 'sw-01'
        assert allocs['unique']['pool_loopbacks']['10.255.0.1']['interface'] == 'loopback0'

    def test_sync_routed_physical_interface(self, tmp_data_dir, sample_pools):
        app = self._make_app_with_pool(tmp_data_dir, sample_pools)
        write_interfaces(app, 'proj', 'sw-01', """\
interface_groups: []
physical_interfaces:
  - name: "1/1/1"
    routed: "true"
    ip_address: "10.254.0.0"
    ip_prefix: "31"
lag_interfaces: []
loopback_interfaces: []
vlan_interfaces: []
""")
        write_vxlan(app, 'proj', 'sw-01', "loopback_interface: loopback1\nloopback_ip:\n")
        sync_allocations(app, USERNAME, 'proj', 'sw-01')
        allocs = get_all_allocations(app, USERNAME, 'proj')
        assert '10.254.0.0' in allocs['point_to_point'].get('pool_ptp', {})
        assert allocs['point_to_point']['pool_ptp']['10.254.0.0']['interface'] == '1/1/1'

    def test_sync_empty_ip_not_allocated(self, tmp_data_dir, sample_pools):
        app = self._make_app_with_pool(tmp_data_dir, sample_pools)
        write_interfaces(app, 'proj', 'sw-01', """\
interface_groups: []
physical_interfaces:
  - name: "1/1/1"
    routed: "true"
    ip_address: ""
    ip_prefix: "31"
lag_interfaces: []
loopback_interfaces: []
vlan_interfaces: []
""")
        write_vxlan(app, 'proj', 'sw-01', "loopback_interface: loopback1\nloopback_ip:\n")
        sync_allocations(app, USERNAME, 'proj', 'sw-01')
        allocs = get_all_allocations(app, USERNAME, 'proj')
        assert not allocs['point_to_point'].get('pool_ptp', {})

    def test_sync_invalid_ip_not_allocated(self, tmp_data_dir, sample_pools):
        app = self._make_app_with_pool(tmp_data_dir, sample_pools)
        write_interfaces(app, 'proj', 'sw-01', """\
interface_groups: []
physical_interfaces:
  - name: "1/1/1"
    routed: "true"
    ip_address: "not-an-ip"
    ip_prefix: "31"
lag_interfaces: []
loopback_interfaces: []
vlan_interfaces: []
""")
        write_vxlan(app, 'proj', 'sw-01', "loopback_interface: loopback1\nloopback_ip:\n")
        sync_allocations(app, USERNAME, 'proj', 'sw-01')
        allocs = get_all_allocations(app, USERNAME, 'proj')
        assert not allocs['point_to_point'].get('pool_ptp', {})

    def test_sync_clears_stale_allocations(self, tmp_data_dir, sample_pools):
        """Saving a host with no IPs should clear its previous allocations."""
        app = self._make_app_with_pool(tmp_data_dir, sample_pools)
        # First save with an IP
        write_interfaces(app, 'proj', 'sw-01', """\
interface_groups: []
physical_interfaces: []
lag_interfaces: []
loopback_interfaces:
  - name: loopback0
    ip_address: "10.255.0.1"
    ip_prefix: "32"
vlan_interfaces: []
""")
        write_vxlan(app, 'proj', 'sw-01', "loopback_interface: loopback1\nloopback_ip:\n")
        sync_allocations(app, USERNAME, 'proj', 'sw-01')
        assert '10.255.0.1' in get_all_allocations(app, USERNAME, 'proj')['unique'].get('pool_loopbacks', {})

        # Now remove the IP and sync again
        write_interfaces(app, 'proj', 'sw-01', """\
interface_groups: []
physical_interfaces: []
lag_interfaces: []
loopback_interfaces:
  - name: loopback0
    ip_address: ""
    ip_prefix: "32"
vlan_interfaces: []
""")
        sync_allocations(app, USERNAME, 'proj', 'sw-01')
        allocs = get_all_allocations(app, USERNAME, 'proj')
        assert '10.255.0.1' not in allocs['unique'].get('pool_loopbacks', {})

    def test_sync_stale_ptp_reservation_cleared(self, tmp_data_dir, sample_pools):
        """Clearing a P2P IP should also remove the reserved_for_peer entry."""
        app = self._make_app_with_pool(tmp_data_dir, sample_pools)
        write_interfaces(app, 'proj', 'sw-01', """\
interface_groups: []
physical_interfaces:
  - name: "1/1/1"
    routed: "true"
    ip_address: "10.254.0.0"
    ip_prefix: "31"
lag_interfaces: []
loopback_interfaces: []
vlan_interfaces: []
""")
        write_vxlan(app, 'proj', 'sw-01', "loopback_interface: loopback1\nloopback_ip:\n")
        sync_allocations(app, USERNAME, 'proj', 'sw-01')

        # Confirm reservation exists
        allocs = get_all_allocations(app, USERNAME, 'proj')
        assert '10.254.0.1' in allocs['point_to_point']['pool_ptp']
        assert allocs['point_to_point']['pool_ptp']['10.254.0.1']['status'] == 'reserved_for_peer'

        # Now clear the IP
        write_interfaces(app, 'proj', 'sw-01', """\
interface_groups: []
physical_interfaces:
  - name: "1/1/1"
    routed: "true"
    ip_address: ""
    ip_prefix: "31"
lag_interfaces: []
loopback_interfaces: []
vlan_interfaces: []
""")
        sync_allocations(app, USERNAME, 'proj', 'sw-01')
        allocs = get_all_allocations(app, USERNAME, 'proj')
        pool_allocs = allocs['point_to_point'].get('pool_ptp', {})
        assert '10.254.0.0' not in pool_allocs
        assert '10.254.0.1' not in pool_allocs

    def test_sync_ip_outside_pool_ignored(self, tmp_data_dir, sample_pools):
        """IPs not in any pool subnet should not create allocations."""
        app = self._make_app_with_pool(tmp_data_dir, sample_pools)
        write_interfaces(app, 'proj', 'sw-01', """\
interface_groups: []
physical_interfaces: []
lag_interfaces: []
loopback_interfaces:
  - name: loopback0
    ip_address: "192.168.1.1"
    ip_prefix: "32"
vlan_interfaces: []
""")
        write_vxlan(app, 'proj', 'sw-01', "loopback_interface: loopback1\nloopback_ip:\n")
        sync_allocations(app, USERNAME, 'proj', 'sw-01')
        allocs = get_all_allocations(app, USERNAME, 'proj')
        assert not allocs['unique'].get('pool_loopbacks')

    def test_sync_vtep_ip(self, tmp_data_dir, sample_pools):
        app = self._make_app_with_pool(tmp_data_dir, sample_pools)
        write_interfaces(app, 'proj', 'sw-01', """\
interface_groups: []
physical_interfaces: []
lag_interfaces: []
loopback_interfaces: []
vlan_interfaces: []
""")
        write_vxlan(app, 'proj', 'sw-01',
                    "loopback_interface: loopback1\nloopback_ip: \"10.255.0.2\"\n")
        sync_allocations(app, USERNAME, 'proj', 'sw-01')
        allocs = get_all_allocations(app, USERNAME, 'proj')
        assert '10.255.0.2' in allocs['unique'].get('pool_loopbacks', {})

    def test_sync_recreated_host_clears_old_allocations(self, tmp_data_dir, sample_pools):
        """Deleting and recreating a host with same name should not retain old allocations."""
        app = self._make_app_with_pool(tmp_data_dir, sample_pools)
        # Old Core-01 had an IP
        write_interfaces(app, 'proj', 'Core-01', """\
interface_groups: []
physical_interfaces: []
lag_interfaces: []
loopback_interfaces:
  - name: loopback0
    ip_address: "10.255.0.5"
    ip_prefix: "32"
vlan_interfaces: []
""")
        write_vxlan(app, 'proj', 'Core-01', "loopback_interface: loopback1\nloopback_ip:\n")
        sync_allocations(app, USERNAME, 'proj', 'Core-01')

        # Simulate delete + recreate: new Core-01 has no IPs yet
        write_interfaces(app, 'proj', 'Core-01', """\
interface_groups: []
physical_interfaces: []
lag_interfaces: []
loopback_interfaces: []
vlan_interfaces: []
""")
        sync_allocations(app, USERNAME, 'proj', 'Core-01')
        allocs = get_all_allocations(app, USERNAME, 'proj')
        assert '10.255.0.5' not in allocs['unique'].get('pool_loopbacks', {})
