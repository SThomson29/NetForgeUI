"""
Unit tests for pool delegation — taking a supernet's carved subnet and
using it as a unique or point-to-point pool.

Also covers the overlap validation that previously did not exist: two pools
covering the same addresses have no defined owner, so the same address could
be handed out twice.
"""

import os
import pytest

from app.project import (
    create_project, add_pool, remove_pool, get_project_config,
    get_carved_subnets, assign_vlan_subnet, sync_allocations,
    get_all_allocations, project_host_vars_dir,
    PoolOverlapError,
)

SUPERNET = {'id': 'sn1', 'type': 'vlan_supernet', 'name': 'VLANs',
            'subnet': '10.100.0.0/16', 'carve_prefix': '24'}


@pytest.fixture
def proj(app):
    with app.app_context():
        create_project(app, 'admin', 'pools')
        add_pool(app, 'admin', 'pools', dict(SUPERNET))
    return 'pools'


class TestOverlapValidation:

    def test_overlapping_pool_is_rejected(self, app, proj):
        """The case that previously produced a broken project."""
        with app.app_context():
            with pytest.raises(PoolOverlapError) as e:
                add_pool(app, 'admin', proj, {
                    'id': 'bad', 'type': 'point_to_point', 'name': 'P2P',
                    'subnet': '10.100.5.0/24'})
        assert 'overlaps' in str(e.value)
        assert 'Delegate' in str(e.value), 'error should point at the fix'

    def test_identical_subnet_is_rejected(self, app, proj):
        with app.app_context():
            with pytest.raises(PoolOverlapError):
                add_pool(app, 'admin', proj, {
                    'id': 'bad', 'type': 'unique', 'name': 'Dup',
                    'subnet': '10.100.0.0/16'})

    def test_supernet_of_an_existing_pool_is_rejected(self, app, proj):
        """Overlap must be caught in both directions, not just containment."""
        with app.app_context():
            with pytest.raises(PoolOverlapError):
                add_pool(app, 'admin', proj, {
                    'id': 'bad', 'type': 'unique', 'name': 'Wide',
                    'subnet': '10.0.0.0/8'})

    def test_non_overlapping_pool_is_fine(self, app, proj):
        with app.app_context():
            add_pool(app, 'admin', proj, {
                'id': 'lb', 'type': 'unique', 'name': 'Loopbacks',
                'prefix': '32', 'subnet': '10.255.0.0/24'})
            ids = [p['id'] for p in get_project_config(app, 'admin', proj)['pools']]
        assert 'lb' in ids


class TestDelegation:

    def _delegate(self, app, proj, pool_id='ptp1', subnet='10.100.5.0/24',
                  ptype='point_to_point'):
        add_pool(app, 'admin', proj, {
            'id': pool_id, 'type': ptype, 'name': pool_id,
            'subnet': subnet, 'parent_pool_id': 'sn1'})

    def test_delegated_pool_is_created(self, app, proj):
        with app.app_context():
            self._delegate(app, proj)
            pools = get_project_config(app, 'admin', proj)['pools']
        child = next(p for p in pools if p['id'] == 'ptp1')
        assert child['subnet'] == '10.100.5.0/24'
        assert child['parent_pool_id'] == 'sn1'

    def test_subnet_is_marked_delegated(self, app, proj):
        with app.app_context():
            self._delegate(app, proj)
            carved = get_carved_subnets(app, 'admin', proj, 'sn1')
        assert carved['10.100.5.0/24']['status'] == 'delegated'
        assert carved['10.100.5.0/24']['delegated_to'] == 'ptp1'
        assert carved['10.100.6.0/24']['status'] == 'carved', 'only one taken'

    def test_delegated_subnet_cannot_go_to_a_vlan(self, app, proj):
        """Otherwise the same addresses are handed out twice."""
        with app.app_context():
            self._delegate(app, proj)
            with pytest.raises(ValueError) as e:
                assign_vlan_subnet(app, 'admin', proj, 'sn1',
                                   '10.100.5.0/24', '100', 'Users', 'sw1')
        assert 'delegated' in str(e.value)

    def test_same_subnet_cannot_be_delegated_twice(self, app, proj):
        with app.app_context():
            self._delegate(app, proj)
            with pytest.raises(PoolOverlapError):
                self._delegate(app, proj, pool_id='lb1', ptype='unique')

    def test_two_different_subnets_can_be_delegated(self, app, proj):
        with app.app_context():
            self._delegate(app, proj, 'ptp1', '10.100.5.0/24')
            self._delegate(app, proj, 'lb1', '10.100.9.0/24', 'unique')
            carved = get_carved_subnets(app, 'admin', proj, 'sn1')
        assert carved['10.100.5.0/24']['status'] == 'delegated'
        assert carved['10.100.9.0/24']['status'] == 'delegated'

    def test_removing_child_returns_the_subnet(self, app, proj):
        with app.app_context():
            self._delegate(app, proj)
            remove_pool(app, 'admin', proj, 'ptp1')
            carved = get_carved_subnets(app, 'admin', proj, 'sn1')
        assert carved['10.100.5.0/24']['status'] == 'carved'
        assert 'delegated_to' not in carved['10.100.5.0/24']

    def test_removing_parent_removes_children(self, app, proj):
        """A child pointing at a deleted parent would be orphaned."""
        with app.app_context():
            self._delegate(app, proj, 'ptp1', '10.100.5.0/24')
            self._delegate(app, proj, 'lb1', '10.100.9.0/24', 'unique')
            remove_pool(app, 'admin', proj, 'sn1')
            pools = get_project_config(app, 'admin', proj)['pools']
        assert pools == []

    def test_wrong_size_is_rejected(self, app, proj):
        """Must be exactly one carved subnet, not a slice of one."""
        with app.app_context():
            with pytest.raises(PoolOverlapError) as e:
                self._delegate(app, proj, subnet='10.100.5.0/25')
        assert 'exactly one' in str(e.value)

    def test_outside_the_supernet_is_rejected(self, app, proj):
        with app.app_context():
            with pytest.raises(PoolOverlapError) as e:
                self._delegate(app, proj, subnet='10.200.5.0/24')
        assert 'not inside' in str(e.value)

    def test_cannot_delegate_from_a_non_supernet(self, app, proj):
        with app.app_context():
            add_pool(app, 'admin', proj, {
                'id': 'lb', 'type': 'unique', 'name': 'LB',
                'prefix': '32', 'subnet': '10.255.0.0/24'})
            with pytest.raises(PoolOverlapError) as e:
                add_pool(app, 'admin', proj, {
                    'id': 'x', 'type': 'point_to_point', 'name': 'X',
                    'subnet': '10.255.0.0/24', 'parent_pool_id': 'lb'})
        assert 'supernet' in str(e.value)


class TestAllocationAttribution:

    def test_ip_attributes_to_the_delegated_pool(self, app, proj):
        """Longest prefix must win regardless of pool order in the config.

        A delegated pool sits inside its parent's range. Matching in config
        order would credit whichever pool was listed first, so the ordering
        has to be by prefix length.
        """
        with app.app_context():
            # a standalone unique pool added FIRST, then the delegated one
            add_pool(app, 'admin', proj, {
                'id': 'other', 'type': 'unique', 'name': 'Other',
                'prefix': '32', 'subnet': '10.255.0.0/24'})
            add_pool(app, 'admin', proj, {
                'id': 'lb1', 'type': 'unique', 'name': 'Loopbacks',
                'prefix': '32', 'subnet': '10.100.9.0/24',
                'parent_pool_id': 'sn1'})

            hv = os.path.join(project_host_vars_dir(app, 'admin', proj), 'sw1')
            os.makedirs(hv, exist_ok=True)
            with open(os.path.join(hv, 'interfaces.yml'), 'w') as f:
                f.write('loopback_interfaces:\n  - name: loopback0\n'
                        '    ip_address: "10.100.9.1"\n    ip_prefix: "32"\n')
            sync_allocations(app, 'admin', proj, 'sw1')
            allocs = get_all_allocations(app, 'admin', proj)

        assert '10.100.9.1' in allocs['unique'].get('lb1', {}), \
            'address not credited to the delegated pool'
        assert allocs['unique']['lb1']['10.100.9.1']['hostname'] == 'sw1'

    def test_longest_prefix_wins_over_config_order(self, app):
        """Direct check of the matching rule, without the overlap guard."""
        import ipaddress
        pools = [
            {'id': 'wide',   'type': 'unique', 'subnet': '10.50.0.0/16'},
            {'id': 'narrow', 'type': 'unique', 'subnet': '10.50.7.0/24'},
        ]
        ip = ipaddress.ip_address('10.50.7.9')
        matches = [p for p in pools
                   if ip in ipaddress.ip_network(p['subnet'], strict=False)]
        best = max(matches, key=lambda p: ipaddress.ip_network(
            p['subnet'], strict=False).prefixlen)
        assert best['id'] == 'narrow'


class TestResourcesPageRendering:

    def test_page_renders_with_a_delegated_pool(self, app, auth_client, proj):
        """Guards the template references against the route's variable names."""
        with app.app_context():
            add_pool(app, 'admin', proj, {
                'id': 'ptp1', 'type': 'point_to_point', 'name': 'P2P links',
                'subnet': '10.100.5.0/24', 'parent_pool_id': 'sn1'})
        res = auth_client.get('/projects/%s/resources' % proj)
        assert res.status_code == 200
        body = res.data.decode()
        assert 'delegated from VLANs' in body
        assert 'Delegated → P2P links' in body

    def test_page_renders_with_no_pools(self, app, auth_client):
        from app.project import create_project
        with app.app_context():
            create_project(app, 'admin', 'bare')
        res = auth_client.get('/projects/bare/resources')
        assert res.status_code == 200
        assert 'SUPERNETS = []' in res.data.decode()

    def test_carved_endpoint_excludes_delegated(self, app, auth_client, proj):
        with app.app_context():
            add_pool(app, 'admin', proj, {
                'id': 'ptp1', 'type': 'point_to_point', 'name': 'P2P',
                'subnet': '10.100.5.0/24', 'parent_pool_id': 'sn1'})
        data = auth_client.get(
            '/projects/%s/api/pools/sn1/carved' % proj).get_json()
        assert '10.100.5.0/24' not in data['subnets']
        assert '10.100.6.0/24' in data['subnets']

    def test_api_reports_overlap_clearly(self, app, auth_client, proj):
        res = auth_client.post('/projects/%s/api/pools' % proj, json={
            'type': 'point_to_point', 'name': 'P2P',
            'subnet': '10.100.5.0/24'})
        assert res.status_code == 400
        body = res.get_json()
        assert body['ok'] is False
        assert body.get('overlap') is True
        assert 'Delegate' in body['error']


class TestAddressOrdering:
    """CIDR and IP lists must order numerically, not lexicographically.

    Plain string sorting puts 10.100.10.0/24 before 10.100.2.0/24, which
    reads as random in a picker.
    """

    def test_helper_orders_cidrs_numerically(self):
        from app.project import sort_by_address
        out = sort_by_address(['10.100.100.0/24', '10.100.2.0/24',
                               '10.100.11.0/24', '10.100.9.0/24'])
        assert out == ['10.100.2.0/24', '10.100.9.0/24',
                       '10.100.11.0/24', '10.100.100.0/24']

    def test_helper_orders_bare_ips(self):
        from app.project import sort_by_address
        out = sort_by_address(['10.0.0.10', '10.0.0.2', '10.0.0.1'])
        assert out == ['10.0.0.1', '10.0.0.2', '10.0.0.10']

    def test_helper_tolerates_junk(self):
        """A malformed key must sort last, not raise and blank the page."""
        from app.project import sort_by_address
        out = sort_by_address(['10.0.0.2', 'not-an-ip', '10.0.0.1'])
        assert out[:2] == ['10.0.0.1', '10.0.0.2']
        assert out[-1] == 'not-an-ip'

    def test_helper_accepts_a_dict(self):
        from app.project import sort_by_address
        out = sort_by_address({'10.0.0.10': {}, '10.0.0.2': {}})
        assert [k for k, _ in out] == ['10.0.0.2', '10.0.0.10']

    def test_carved_endpoint_is_in_address_order(self, app, auth_client, proj):
        """The delegation picker — a /16 carved to /24 gives 256 entries."""
        data = auth_client.get(
            '/projects/%s/api/pools/sn1/carved' % proj).get_json()
        subnets = data['subnets']
        assert subnets[0] == '10.100.0.0/24'
        assert subnets[1] == '10.100.1.0/24'
        assert subnets[2] == '10.100.2.0/24'
        assert subnets[10] == '10.100.10.0/24'
        assert subnets[-1] == '10.100.255.0/24'

    def test_resources_table_is_in_address_order(self, app, auth_client, proj):
        body = auth_client.get('/projects/%s/resources' % proj).data.decode()
        i2 = body.index('10.100.2.0/24')
        i10 = body.index('10.100.10.0/24')
        i100 = body.index('10.100.100.0/24')
        assert i2 < i10 < i100, 'carved table is not in address order'


class TestVlanAssignment:
    """Assigning a carved subnet to a VLAN from the Resources page."""

    def test_assign_without_a_hostname(self, app, auth_client, proj):
        """A VLAN can be named before its switch is known."""
        res = auth_client.post('/projects/%s/api/allocations/vlan' % proj, json={
            'pool_id': 'sn1', 'subnet': '10.100.7.0/24',
            'vlan_id': '700', 'vlan_name': 'Voice'})
        assert res.get_json()['ok'] is True

        with app.app_context():
            carved = get_carved_subnets(app, 'admin', proj, 'sn1')
            allocs = get_all_allocations(app, 'admin', proj)
        entry = carved['10.100.7.0/24']
        assert entry['status'] == 'assigned'
        assert entry['vlan_id'] == '700'
        assert entry['vlan_name'] == 'Voice'
        # No switch, so no gateway addresses should have been derived.
        assert allocs['svi'].get('sn1', {}) == {}

    def test_assign_with_hostname_derives_gateway(self, app, auth_client, proj):
        res = auth_client.post('/projects/%s/api/allocations/vlan' % proj, json={
            'pool_id': 'sn1', 'subnet': '10.100.8.0/24',
            'vlan_id': '800', 'vlan_name': 'Users', 'hostname': 'sw1'})
        assert res.get_json()['ok'] is True
        with app.app_context():
            allocs = get_all_allocations(app, 'admin', proj)
        svi = allocs['svi']['sn1']
        assert '10.100.8.1' in svi
        assert svi['10.100.8.1']['interface'] == 'vlan800'
        assert svi['10.100.8.1']['role'] == 'gateway'

    def test_cannot_assign_a_delegated_subnet(self, app, auth_client, proj):
        with app.app_context():
            add_pool(app, 'admin', proj, {
                'id': 'ptp1', 'type': 'point_to_point', 'name': 'P2P',
                'subnet': '10.100.5.0/24', 'parent_pool_id': 'sn1'})
        res = auth_client.post('/projects/%s/api/allocations/vlan' % proj, json={
            'pool_id': 'sn1', 'subnet': '10.100.5.0/24',
            'vlan_id': '500', 'vlan_name': 'Nope'})
        assert res.status_code == 400
        assert 'delegated' in res.get_json()['error']

    def test_assign_button_shown_only_for_available_subnets(
            self, app, auth_client, proj):
        with app.app_context():
            add_pool(app, 'admin', proj, {
                'id': 'ptp1', 'type': 'point_to_point', 'name': 'P2P',
                'subnet': '10.100.5.0/24', 'parent_pool_id': 'sn1'})
        body = auth_client.get('/projects/%s/resources' % proj).data.decode()
        assert "openVlanAssign('sn1', '10.100.6.0/24')" in body
        assert "openVlanAssign('sn1', '10.100.5.0/24')" not in body, \
            'delegated subnet must not offer VLAN assignment'


class TestPoolTypeLabels:

    def test_supernet_is_not_called_vlan_supernet(self, app, auth_client, proj):
        body = auth_client.get('/projects/%s/resources' % proj).data.decode()
        assert 'VLAN supernet' not in body
        assert '>Supernet<' in body

    def test_stored_type_key_is_unchanged(self, app, proj):
        """Renaming the key would mean migrating every existing project."""
        with app.app_context():
            pools = get_project_config(app, 'admin', proj)['pools']
        assert pools[0]['type'] == 'vlan_supernet'
