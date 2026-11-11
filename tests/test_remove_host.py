"""
Deleting a switch frees what the project holds for it: its addresses in
Resources, its Deployment IPs row and what the project remembers about it.
Without that its addresses stay taken in every picker until released by
hand.
"""

import os

import pytest

from app import checks
from app.deploy_routes import _load_mapping, _mapping_path, _save_mapping
from app.project import (_load_allocations, _save_allocations, add_pool, allocate_ptp,
                         allocate_unique, assign_vlan_subnet, create_project,
                         get_project_config, record_host_save, record_inventory)
from app.utils import add_project_hosts, ensure_workspace, remove_project_host

P = 'site'
POOLS = [
    {'id': 'lo', 'name': 'loopbacks', 'type': 'unique', 'subnet': '10.9.0.0/24', 'prefix': 32},
    {'id': 'ptp', 'name': 'links', 'type': 'point_to_point', 'subnet': '10.8.0.0/24'},
    {'id': 'vl', 'name': 'vlans', 'type': 'vlan_supernet', 'subnet': '10.20.0.0/16',
     'carve_prefix': 24},
]


@pytest.fixture
def project(app, auth_client, skeleton_dir):
    ensure_workspace(app, 'admin')
    create_project(app, 'admin', P)
    add_project_hosts(app, 'admin', P, ['SW1', 'SW2', 'SW3'], 'none')
    for pool in POOLS:
        add_pool(app, 'admin', P, dict(pool))
    return app


def allocs(app):
    return _load_allocations(app, 'admin', P)


def remove(app, host):
    return remove_project_host(app, 'admin', P, host)


class TestAddresses:

    def test_unique_address_goes_back_to_the_pool(self, project):
        allocate_unique(project, 'admin', P, 'lo', '10.9.0.1', 'SW1', 'loopback0')
        allocate_unique(project, 'admin', P, 'lo', '10.9.0.2', 'SW2', 'loopback0')
        assert remove(project, 'SW1') == 1
        assert list(allocs(project)['unique']['lo']) == ['10.9.0.2']

    def test_a_lone_link_end_and_its_reservation_both_go(self, project):
        allocate_ptp(project, 'admin', P, 'ptp', '10.8.0.0', 'SW1', '1/1/1')
        assert remove(project, 'SW1') == 1
        assert allocs(project)['point_to_point']['ptp'] == {}

    def test_the_other_end_of_a_link_keeps_its_address(self, project):
        allocate_ptp(project, 'admin', P, 'ptp', '10.8.0.0', 'SW1', '1/1/1')
        allocate_ptp(project, 'admin', P, 'ptp', '10.8.0.1', 'SW2', '1/1/1')
        remove(project, 'SW1')
        link = allocs(project)['point_to_point']['ptp']
        assert link['10.8.0.1']['hostname'] == 'SW2'
        assert link['10.8.0.1']['peer_hostname'] is None
        # SW1's end is free again, held only as the far end of SW2's link.
        assert link['10.8.0.0']['status'] == 'reserved_for_peer'
        assert link['10.8.0.0']['hostname'] is None

    def test_a_reservation_held_for_it_goes(self, project):
        allocate_ptp(project, 'admin', P, 'ptp', '10.8.0.0', 'SW2', '1/1/1')
        data = allocs(project)
        data['point_to_point']['ptp']['10.8.0.4'] = {
            'hostname': None, 'interface': None, 'peer_ip': '10.8.0.5',
            'peer_hostname': 'SW1', 'peer_interface': '1/1/2', 'status': 'reserved_for_peer'}
        _save_allocations(project, 'admin', P, data)
        remove(project, 'SW1')
        assert '10.8.0.4' not in allocs(project)['point_to_point']['ptp']
        assert allocs(project)['point_to_point']['ptp']['10.8.0.0']['hostname'] == 'SW2'

    def test_a_vsx_peer_keeps_a_shared_vtep(self, project):
        data = allocs(project)
        data['unique']['lo'] = {'10.9.0.9': {'hostname': 'SW1', 'interface': 'loopback1',
                                             'shared': True, 'shared_with': 'SW2',
                                             'shared_with_interface': 'loopback1'}}
        _save_allocations(project, 'admin', P, data)
        remove(project, 'SW1')
        entry = allocs(project)['unique']['lo']['10.9.0.9']
        assert entry['hostname'] == 'SW2' and 'shared_with' not in entry

    def test_svi_addresses_are_released_and_a_shared_gateway_passes_on(self, project):
        assign_vlan_subnet(project, 'admin', P, 'vl', '10.20.5.0/24', 20, 'voice', 'SW1', 'SW2')
        svi = allocs(project)['svi']['vl']
        assert svi['10.20.5.1']['hostname'] == 'SW1'                 # gateway
        assert svi['10.20.5.254']['shared_with'] == 'SW2'            # active gateway
        remove(project, 'SW1')
        svi = allocs(project)['svi']['vl']
        assert '10.20.5.1' not in svi
        assert svi['10.20.5.254']['hostname'] == 'SW2' and 'shared_with' not in svi['10.20.5.254']

    def test_removing_the_peer_of_a_shared_gateway(self, project):
        assign_vlan_subnet(project, 'admin', P, 'vl', '10.20.5.0/24', 20, 'voice', 'SW1', 'SW2')
        remove(project, 'SW2')
        gw = allocs(project)['svi']['vl']['10.20.5.254']
        assert gw['hostname'] == 'SW1' and 'shared_with' not in gw

    def test_a_vlans_subnet_stays_assigned_without_the_switch(self, project):
        assign_vlan_subnet(project, 'admin', P, 'vl', '10.20.5.0/24', 20, 'voice', 'SW1', 'SW2')
        assign_vlan_subnet(project, 'admin', P, 'vl', '10.20.6.0/24', 30, 'data', 'SW3')
        remove(project, 'SW1')
        block = allocs(project)['vlan_supernet']['vl']['10.20.5.0/24']
        assert (block['status'], block['vlan_id']) == ('assigned', 20)
        assert (block['hostname'], block['peer_hostname']) == ('SW2', None)
        remove(project, 'SW3')
        block = allocs(project)['vlan_supernet']['vl']['10.20.6.0/24']
        assert (block['status'], block['vlan_id'], block['hostname']) == ('assigned', 30, None)

    def test_other_switches_are_untouched(self, project):
        allocate_unique(project, 'admin', P, 'lo', '10.9.0.2', 'SW2', 'loopback0')
        allocate_ptp(project, 'admin', P, 'ptp', '10.8.0.8', 'SW2', '1/1/5')
        before = allocs(project)
        assert remove(project, 'SW1') == 0
        assert allocs(project) == before

    def test_checks_no_longer_report_addresses_for_a_missing_switch(self, project):
        allocate_unique(project, 'admin', P, 'lo', '10.9.0.1', 'SW1', 'loopback0')
        remove(project, 'SW1')
        assert not [f for f in checks.run(project, 'admin', P)
                    if f['key'].startswith('alloc_drift:gone')]


class TestDeploymentIps:

    def test_its_row_goes_and_the_rest_stays(self, project):
        _save_mapping(project, 'admin', P, {
            'hosts': [{'hostname': 'SW1', 'mgmt_ip': '192.0.2.1'},
                      {'hostname': 'SW2', 'mgmt_ip': '192.0.2.2'}],
            'settings': {'rollback_timeout': 9}})
        remove(project, 'SW1')
        mapping = _load_mapping(project, 'admin', P)
        assert mapping['hosts'] == [{'hostname': 'SW2', 'mgmt_ip': '192.0.2.2'}]
        assert mapping['settings'] == {'rollback_timeout': 9}

    def test_no_mapping_file_is_created(self, project):
        remove(project, 'SW1')
        assert not os.path.exists(_mapping_path(project, 'admin', P))


class TestWhatTheProjectRemembers:

    def test_last_save_and_inventory_are_dropped(self, project):
        record_host_save(project, 'admin', P, 'SW1', 'admin')
        record_host_save(project, 'admin', P, 'SW2', 'admin')
        record_inventory(project, 'admin', P, {'SW1': {'serials': ['A']}, 'SW2': {'serials': ['B']}},
                         'admin')
        remove(project, 'SW1')
        cfg = get_project_config(project, 'admin', P)
        assert list(cfg['host_saves']) == ['SW2'] and list(cfg['inventory']) == ['SW2']

    def test_a_readded_switch_starts_clean(self, project):
        allocate_unique(project, 'admin', P, 'lo', '10.9.0.1', 'SW1', 'loopback0')
        remove(project, 'SW1')
        add_project_hosts(project, 'admin', P, ['SW1'], 'none')
        assert allocs(project)['unique']['lo'] == {}


class TestRoute:

    def test_says_how_many_addresses_were_released(self, project, auth_client):
        allocate_unique(project, 'admin', P, 'lo', '10.9.0.1', 'SW1', 'loopback0')
        allocate_ptp(project, 'admin', P, 'ptp', '10.8.0.0', 'SW1', '1/1/1')
        body = auth_client.post('/projects/admin/%s/hosts/delete/SW1' % P,
                                follow_redirects=True).get_data(as_text=True)
        assert 'SW1 removed; 2 addresses released in Resources.' in body

    def test_plain_message_when_it_held_nothing(self, project, auth_client):
        body = auth_client.post('/projects/admin/%s/hosts/delete/SW2' % P,
                                follow_redirects=True).get_data(as_text=True)
        assert 'SW2 removed.' in body and 'released' not in body
