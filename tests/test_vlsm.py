"""
Mixed-size supernets — carving blocks of any size on demand.

A supernet with a carve_prefix pre-carves into equal blocks, as before.
Without one it starts empty and the operator carves blocks themselves; the
tool's job is to refuse overlaps, not to plan the addressing.
"""

import os
import pytest

from app.project import (
    create_project, add_pool, remove_pool, get_carved_subnets,
    carve_supernet_block, remove_supernet_block, suggest_free_block,
    supernet_free_blocks, is_manual_supernet, get_project_config,
    PoolOverlapError,
)

MANUAL = {'id': 'sn', 'type': 'vlan_supernet', 'name': 'Wireless',
          'subnet': '10.50.0.0/16'}                      # no carve_prefix
FIXED = {'id': 'fx', 'type': 'vlan_supernet', 'name': 'Campus',
         'subnet': '10.60.0.0/16', 'carve_prefix': '24'}


@pytest.fixture
def proj(app):
    with app.app_context():
        create_project(app, 'admin', 'w')
        add_pool(app, 'admin', 'w', dict(MANUAL))
    return 'w'


class TestFreeSpaceMaths:

    def test_free_blocks_of_an_empty_supernet(self):
        free = supernet_free_blocks('10.50.0.0/16', [])
        assert [str(f) for f in free] == ['10.50.0.0/16']

    def test_free_blocks_around_used_space(self):
        free = supernet_free_blocks(
            '10.50.0.0/16', ['10.50.0.0/24', '10.50.64.0/18'])
        assert '10.50.1.0/24' in [str(f) for f in free]
        assert '10.50.128.0/17' in [str(f) for f in free]
        # nothing free may overlap what is used
        import ipaddress
        for f in free:
            assert not f.overlaps(ipaddress.ip_network('10.50.64.0/18'))

    def test_full_supernet_has_no_free_space(self):
        assert supernet_free_blocks('10.50.0.0/16', ['10.50.0.0/16']) == []

    def test_malformed_entries_are_skipped(self):
        """A bad key must not blow up the whole calculation."""
        free = supernet_free_blocks('10.50.0.0/16', ['nonsense', '10.50.0.0/24'])
        assert free, 'calculation aborted on a bad entry'


class TestManualMode:

    def test_manual_supernet_starts_empty(self, app, proj):
        with app.app_context():
            assert get_carved_subnets(app, 'admin', proj, 'sn') == {}

    def test_fixed_supernet_still_precarves(self, app, proj):
        with app.app_context():
            add_pool(app, 'admin', proj, dict(FIXED))
            carved = get_carved_subnets(app, 'admin', proj, 'fx')
        assert len(carved) == 256, 'existing behaviour must be unchanged'

    def test_mode_is_detected_from_carve_prefix(self):
        assert is_manual_supernet(MANUAL) is True
        assert is_manual_supernet(FIXED) is False

    def test_carve_blocks_of_different_sizes(self, app, proj):
        with app.app_context():
            carve_supernet_block(app, 'admin', proj, 'sn',
                                 '10.50.0.0/24', '10', 'Wifi MGMT')
            carve_supernet_block(app, 'admin', proj, 'sn',
                                 '10.50.64.0/18', '20', 'Trusted')
            carved = get_carved_subnets(app, 'admin', proj, 'sn')
        assert set(carved) == {'10.50.0.0/24', '10.50.64.0/18'}
        assert carved['10.50.64.0/18']['vlan_name'] == 'Trusted'
        assert carved['10.50.64.0/18']['status'] == 'assigned'

    def test_block_without_a_vlan_is_reserved_not_assigned(self, app, proj):
        with app.app_context():
            carve_supernet_block(app, 'admin', proj, 'sn', '10.50.0.0/24')
            carved = get_carved_subnets(app, 'admin', proj, 'sn')
        assert carved['10.50.0.0/24']['status'] == 'carved'

    def test_cannot_carve_from_a_fixed_supernet(self, app, proj):
        with app.app_context():
            add_pool(app, 'admin', proj, dict(FIXED))
            with pytest.raises(ValueError) as e:
                carve_supernet_block(app, 'admin', proj, 'fx', '10.60.5.0/22')
        assert 'fixed' in str(e.value)


class TestGuardrails:

    def _carve(self, app, proj, cidr):
        carve_supernet_block(app, 'admin', proj, 'sn', cidr)

    @pytest.mark.parametrize('bad,fragment', [
        ('10.50.64.0/20', 'overlaps'),      # inside an existing block
        ('10.50.0.0/16',  'overlaps'),      # swallows everything
        ('10.99.0.0/24',  'not inside'),    # outside the supernet
        ('10.50.65.0/18', 'not a valid network'),
        ('10.50.1.0',     'must include a prefix'),
    ])
    def test_bad_blocks_are_refused(self, app, proj, bad, fragment):
        with app.app_context():
            self._carve(app, proj, '10.50.64.0/18')
            with pytest.raises(ValueError) as e:
                self._carve(app, proj, bad)
        assert fragment in str(e.value)

    def test_adjacent_blocks_are_allowed(self, app, proj):
        """Touching is fine; only overlapping is not."""
        with app.app_context():
            self._carve(app, proj, '10.50.0.0/24')
            self._carve(app, proj, '10.50.1.0/24')
            assert len(get_carved_subnets(app, 'admin', proj, 'sn')) == 2


class TestSuggestion:

    def test_suggests_the_first_free_block(self, app, proj):
        with app.app_context():
            carve_supernet_block(app, 'admin', proj, 'sn', '10.50.0.0/24')
            carve_supernet_block(app, 'admin', proj, 'sn', '10.50.64.0/18')
            carve_supernet_block(app, 'admin', proj, 'sn', '10.50.128.0/18')
            assert suggest_free_block(app, 'admin', proj, 'sn', 18) == '10.50.192.0/18'
            assert suggest_free_block(app, 'admin', proj, 'sn', 24) == '10.50.1.0/24'

    def test_suggestion_is_actually_free(self, app, proj):
        with app.app_context():
            carve_supernet_block(app, 'admin', proj, 'sn', '10.50.0.0/24')
            s = suggest_free_block(app, 'admin', proj, 'sn', 24)
            carve_supernet_block(app, 'admin', proj, 'sn', s)   # must not clash

    def test_returns_none_when_it_will_not_fit(self, app, proj):
        with app.app_context():
            carve_supernet_block(app, 'admin', proj, 'sn', '10.50.0.0/17')
            carve_supernet_block(app, 'admin', proj, 'sn', '10.50.128.0/17')
            assert suggest_free_block(app, 'admin', proj, 'sn', 24) is None


class TestRelease:

    def test_removing_a_block_returns_its_space(self, app, proj):
        with app.app_context():
            carve_supernet_block(app, 'admin', proj, 'sn', '10.50.64.0/18')
            assert suggest_free_block(app, 'admin', proj, 'sn', 18) == '10.50.0.0/18'
            remove_supernet_block(app, 'admin', proj, 'sn', '10.50.64.0/18')
            carved = get_carved_subnets(app, 'admin', proj, 'sn')
        assert carved == {}

    def test_freed_space_merges_with_neighbours(self, app, proj):
        """Free space is computed, so there is no fragmentation to clean up."""
        with app.app_context():
            for c in ('10.50.0.0/18', '10.50.64.0/18', '10.50.128.0/18'):
                carve_supernet_block(app, 'admin', proj, 'sn', c)
            remove_supernet_block(app, 'admin', proj, 'sn', '10.50.64.0/18')
            free = supernet_free_blocks(
                '10.50.0.0/16',
                list(get_carved_subnets(app, 'admin', proj, 'sn')))
        assert '10.50.64.0/18' in [str(f) for f in free]

    def test_cannot_remove_a_delegated_block(self, app, proj):
        with app.app_context():
            add_pool(app, 'admin', proj, {
                'id': 'ptp', 'type': 'point_to_point', 'name': 'P2P',
                'subnet': '10.50.200.0/24', 'parent_pool_id': 'sn'})
            with pytest.raises(ValueError) as e:
                remove_supernet_block(app, 'admin', proj, 'sn', '10.50.200.0/24')
        assert 'delegated' in str(e.value)


class TestDelegationFromManualSupernet:

    def test_any_size_may_be_delegated(self, app, proj):
        """A manual supernet has no fixed block size to match."""
        with app.app_context():
            add_pool(app, 'admin', proj, {
                'id': 'lb', 'type': 'unique', 'name': 'Loopbacks', 'prefix': '32',
                'subnet': '10.50.252.0/24', 'parent_pool_id': 'sn'})
            carved = get_carved_subnets(app, 'admin', proj, 'sn')
        assert carved['10.50.252.0/24']['status'] == 'delegated'

    def test_delegated_space_is_not_offered_again(self, app, proj):
        """The block must count as used, or it could be handed out twice."""
        with app.app_context():
            add_pool(app, 'admin', proj, {
                'id': 'lb', 'type': 'unique', 'name': 'LB', 'prefix': '32',
                'subnet': '10.50.0.0/24', 'parent_pool_id': 'sn'})
            with pytest.raises(ValueError) as e:
                carve_supernet_block(app, 'admin', proj, 'sn', '10.50.0.0/24')
        assert 'overlaps' in str(e.value)

    def test_removing_the_child_frees_the_space(self, app, proj):
        with app.app_context():
            add_pool(app, 'admin', proj, {
                'id': 'lb', 'type': 'unique', 'name': 'LB', 'prefix': '32',
                'subnet': '10.50.0.0/24', 'parent_pool_id': 'sn'})
            remove_pool(app, 'admin', proj, 'lb')
            carved = get_carved_subnets(app, 'admin', proj, 'sn')
        assert carved == {}, 'delegated block left behind after removal'


class TestRoutes:

    def test_carve_via_api(self, app, auth_client, proj):
        res = auth_client.post('/projects/%s/api/pools/sn/blocks' % proj,
                               json={'subnet': '10.50.64.0/18',
                                     'vlan_id': '20', 'vlan_name': 'Trusted'})
        assert res.get_json() == {'ok': True, 'subnet': '10.50.64.0/18'}

    def test_overlap_via_api(self, app, auth_client, proj):
        auth_client.post('/projects/%s/api/pools/sn/blocks' % proj,
                         json={'subnet': '10.50.64.0/18'})
        res = auth_client.post('/projects/%s/api/pools/sn/blocks' % proj,
                               json={'subnet': '10.50.64.0/20'})
        assert res.status_code == 400
        assert 'overlaps' in res.get_json()['error']

    def test_suggest_via_api(self, app, auth_client, proj):
        auth_client.post('/projects/%s/api/pools/sn/blocks' % proj,
                         json={'subnet': '10.50.0.0/24'})
        res = auth_client.get('/projects/%s/api/pools/sn/suggest?prefix=24' % proj)
        assert res.get_json()['subnet'] == '10.50.1.0/24'

    def test_suggest_rejects_a_silly_prefix(self, app, auth_client, proj):
        res = auth_client.get('/projects/%s/api/pools/sn/suggest?prefix=99' % proj)
        assert res.status_code == 400

    def test_remove_via_api(self, app, auth_client, proj):
        auth_client.post('/projects/%s/api/pools/sn/blocks' % proj,
                         json={'subnet': '10.50.0.0/24'})
        res = auth_client.delete('/projects/%s/api/pools/sn/blocks' % proj,
                                 json={'subnet': '10.50.0.0/24'})
        assert res.get_json()['ok'] is True

    def test_routes_require_login(self, client):
        assert client.post('/projects/w/api/pools/sn/blocks',
                           json={'subnet': '10.0.0.0/24'}).status_code == 401
        assert client.get('/projects/w/api/pools/sn/suggest').status_code == 401

    def test_page_shows_the_block_editor(self, app, auth_client, proj):
        body = auth_client.get('/projects/%s/resources' % proj).data.decode()
        assert 'Mixed-size supernet' in body
        assert "carveBlock('sn')" in body

    def test_page_hides_it_for_a_fixed_supernet(self, app, auth_client, proj):
        with app.app_context():
            add_pool(app, 'admin', proj, dict(FIXED))
        body = auth_client.get('/projects/%s/resources' % proj).data.decode()
        assert "carveBlock('fx')" not in body


class TestVlanOnDelegatedBlock:
    """A delegated block may still be tagged with a VLAN.

    The real shape this came from: a /16 supernet, a /24 delegated to a
    unique pool handing out /29s, and that /24 is also a VLAN.
    """

    def test_tag_survives_and_keeps_ownership(self, app, proj):
        from app.project import assign_vlan_subnet, get_all_allocations
        with app.app_context():
            add_pool(app, 'admin', proj, {
                'id': 'wm', 'type': 'unique', 'name': 'Wireless MGMT',
                'prefix': '29', 'subnet': '10.50.10.0/24',
                'parent_pool_id': 'sn'})
            assign_vlan_subnet(app, 'admin', proj, 'sn', '10.50.10.0/24',
                               '110', 'Wireless MGMT', 'sw1')
            entry = get_carved_subnets(app, 'admin', proj, 'sn')['10.50.10.0/24']
            allocs = get_all_allocations(app, 'admin', proj)

        assert entry['vlan_id'] == '110'
        assert entry['status'] == 'delegated'
        assert entry['delegated_to'] == 'wm'
        assert allocs['svi'].get('sn', {}) == {}, \
            'gateway addresses must not be derived inside a delegated block'

    def test_page_offers_the_button_on_a_delegated_block(
            self, app, auth_client, proj):
        with app.app_context():
            add_pool(app, 'admin', proj, {
                'id': 'wm', 'type': 'unique', 'name': 'Wireless MGMT',
                'prefix': '29', 'subnet': '10.50.10.0/24',
                'parent_pool_id': 'sn'})
        body = auth_client.get('/projects/%s/resources' % proj).data.decode()
        assert "openVlanAssign('sn', '10.50.10.0/24')" in body
