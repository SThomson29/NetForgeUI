"""
Editing a pool's name or range in place.

Without this, narrowing a delegation means deleting the pool — and
remove_pool takes its allocations with it. Allocation history is the one
thing that cannot be reconstructed.
"""

import pytest

from app.project import (
    create_project, add_pool, update_pool, allocate_unique,
    get_project_config, get_carved_subnets, get_all_allocations,
)


@pytest.fixture
def proj(app):
    with app.app_context():
        create_project(app, 'admin', 'p')
        add_pool(app, 'admin', 'p', {
            'id': 'sn', 'type': 'vlan_supernet', 'name': 'Core',
            'subnet': '10.50.0.0/16'})
        add_pool(app, 'admin', 'p', {
            'id': 'wm', 'type': 'unique', 'name': 'Wireless MGMT',
            'prefix': '29', 'subnet': '10.50.0.0/18', 'parent_pool_id': 'sn'})
        for ip in ('10.50.0.0', '10.50.0.8', '10.50.0.16'):
            allocate_unique(app, 'admin', 'p', 'wm', ip, 'sw1', 'vlan110')
    return 'p'


class TestNarrowingADelegation:

    def test_allocations_survive(self, app, proj):
        with app.app_context():
            update_pool(app, 'admin', proj, 'wm', subnet='10.50.0.0/24')
            allocs = get_all_allocations(app, 'admin', proj)['unique']['wm']
        assert sorted(allocs) == ['10.50.0.0', '10.50.0.16', '10.50.0.8']

    def test_pool_range_changes(self, app, proj):
        with app.app_context():
            update_pool(app, 'admin', proj, 'wm', subnet='10.50.0.0/24')
            pool = [p for p in get_project_config(app, 'admin', proj)['pools']
                    if p['id'] == 'wm'][0]
        assert pool['subnet'] == '10.50.0.0/24'

    def test_parent_block_moves_with_it(self, app, proj):
        """Otherwise the supernet still shows the old range as spoken for."""
        with app.app_context():
            update_pool(app, 'admin', proj, 'wm', subnet='10.50.0.0/24')
            blocks = get_carved_subnets(app, 'admin', proj, 'sn')
        assert '10.50.0.0/18' not in blocks
        assert blocks['10.50.0.0/24']['status'] == 'delegated'
        assert blocks['10.50.0.0/24']['delegated_to'] == 'wm'

    def test_stranded_allocations_block_the_change(self, app, proj):
        with app.app_context():
            with pytest.raises(ValueError) as e:
                update_pool(app, 'admin', proj, 'wm', subnet='10.50.0.0/29')
            allocs = get_all_allocations(app, 'admin', proj)['unique']['wm']
        assert 'fall outside' in str(e.value)
        assert len(allocs) == 3, 'allocations must be untouched on refusal'

    def test_range_outside_the_parent_is_refused(self, app, proj):
        """Checked on a pool with no allocations — the allocation check
        runs first and would otherwise mask this one."""
        with app.app_context():
            add_pool(app, 'admin', proj, {
                'id': 'p2', 'type': 'point_to_point', 'name': 'P2P',
                'subnet': '10.50.200.0/24', 'parent_pool_id': 'sn'})
            with pytest.raises(ValueError) as e:
                update_pool(app, 'admin', proj, 'p2', subnet='10.99.0.0/24')
        assert 'not inside' in str(e.value)


class TestRenaming:

    def test_name_only(self, app, proj):
        with app.app_context():
            update_pool(app, 'admin', proj, 'wm', name='WLAN MGMT')
            pool = [p for p in get_project_config(app, 'admin', proj)['pools']
                    if p['id'] == 'wm'][0]
        assert pool['name'] == 'WLAN MGMT'
        assert pool['subnet'] == '10.50.0.0/18', 'range must be untouched'


class TestGuards:

    def test_supernet_range_cannot_change(self, app, proj):
        """Blocks are carved from it; moving the range would orphan them."""
        with app.app_context():
            with pytest.raises(ValueError) as e:
                update_pool(app, 'admin', proj, 'sn', subnet='10.60.0.0/16')
        assert 'cannot be changed' in str(e.value)

    def test_missing_prefix_is_refused(self, app, proj):
        with app.app_context():
            with pytest.raises(ValueError) as e:
                update_pool(app, 'admin', proj, 'wm', subnet='10.50.0.0')
        assert 'must include a prefix' in str(e.value)

    def test_unknown_pool(self, app, proj):
        with app.app_context():
            with pytest.raises(ValueError):
                update_pool(app, 'admin', proj, 'nope', name='x')

    def test_overlap_with_another_pool_is_refused(self, app, proj):
        with app.app_context():
            add_pool(app, 'admin', proj, {
                'id': 'a', 'type': 'unique', 'name': 'A', 'prefix': '32',
                'subnet': '10.98.0.0/24'})
            add_pool(app, 'admin', proj, {
                'id': 'b', 'type': 'unique', 'name': 'B', 'prefix': '32',
                'subnet': '10.98.1.0/24'})
            # widening A to a /23 would swallow B
            with pytest.raises(ValueError) as e:
                update_pool(app, 'admin', proj, 'a', subnet='10.98.0.0/23')
        assert 'overlaps' in str(e.value)

    def test_widening_into_free_space_is_allowed(self, app, proj):
        with app.app_context():
            add_pool(app, 'admin', proj, {
                'id': 'c', 'type': 'unique', 'name': 'C', 'prefix': '32',
                'subnet': '10.97.0.0/24'})
            update_pool(app, 'admin', proj, 'c', subnet='10.97.0.0/23')
            pool = [p for p in get_project_config(app, 'admin', proj)['pools']
                    if p['id'] == 'c'][0]
        assert pool['subnet'] == '10.97.0.0/23'


class TestRoutes:

    def test_patch_via_api(self, app, auth_client, proj):
        res = auth_client.patch('/projects/%s/api/pools/wm' % proj,
                                json={'name': 'Wireless MGMT',
                                      'subnet': '10.50.0.0/24'})
        assert res.get_json()['ok'] is True

    def test_api_reports_stranded_allocations(self, app, auth_client, proj):
        res = auth_client.patch('/projects/%s/api/pools/wm' % proj,
                                json={'subnet': '10.50.0.0/29'})
        assert res.status_code == 400
        assert 'fall outside' in res.get_json()['error']

    def test_requires_login(self, client):
        assert client.patch('/projects/p/api/pools/wm',
                            json={'name': 'x'}).status_code == 401

    def test_edit_button_offered_for_pools_not_supernets(self, app, auth_client, proj):
        body = auth_client.get('/projects/%s/resources' % proj).data.decode()
        assert "editPool('wm'" in body
        assert "editPool('sn'" not in body
