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
        ('10.50.0.0/16',  'would contain the existing block'),
        ('10.99.0.0/24',  'not inside'),
        ('10.50.65.0/18', 'not a valid network'),
        ('10.50.1.0',     'must include a prefix'),
        ('10.50.64.0/18', 'already carved'),
    ])
    def test_bad_blocks_are_refused(self, app, proj, bad, fragment):
        with app.app_context():
            self._carve(app, proj, '10.50.64.0/18')
            with pytest.raises(ValueError) as e:
                self._carve(app, proj, bad)
        assert fragment in str(e.value)

    def test_a_smaller_block_inside_an_unused_one_is_nesting(self, app, proj):
        """With CIDR, two blocks either nest or are disjoint.

        Since an unused block may be subdivided, containment is nesting
        rather than an overlap — there is no such thing as two overlapping
        siblings.
        """
        with app.app_context():
            self._carve(app, proj, '10.50.64.0/18')
            self._carve(app, proj, '10.50.64.0/20')
            blocks = get_carved_subnets(app, 'admin', proj, 'sn')
        assert blocks['10.50.64.0/18']['status'] == 'container'

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
        assert 'already carved' in str(e.value)

    def test_cannot_nest_inside_delegated_space(self, app, proj):
        with app.app_context():
            add_pool(app, 'admin', proj, {
                'id': 'lb', 'type': 'unique', 'name': 'LB', 'prefix': '32',
                'subnet': '10.50.0.0/24', 'parent_pool_id': 'sn'})
            with pytest.raises(ValueError) as e:
                carve_supernet_block(app, 'admin', proj, 'sn', '10.50.0.0/28')
        assert 'delegated' in str(e.value)

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

    def test_outside_in_rule_via_api(self, app, auth_client, proj):
        auth_client.post('/projects/%s/api/pools/sn/blocks' % proj,
                         json={'subnet': '10.50.64.0/20'})
        res = auth_client.post('/projects/%s/api/pools/sn/blocks' % proj,
                               json={'subnet': '10.50.64.0/18'})
        assert res.status_code == 400
        assert 'would contain' in res.get_json()['error']

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


class TestNestedBlocks:
    """Carving inside a block — a /16 into /18s, then /24s inside one.

    The hierarchy is derived from the addresses rather than stored, so there
    are no parent pointers and existing flat data needs no migration.
    """

    def _carve(self, app, proj, cidr, vlan=None, name=None):
        carve_supernet_block(app, 'admin', proj, 'sn', cidr, vlan, name)

    def test_tree_is_derived_from_containment(self):
        from app.project import build_block_tree
        tree = build_block_tree(['10.50.0.0/18', '10.50.0.0/24',
                                 '10.50.1.0/24', '10.50.64.0/18'])
        assert [str(n) for n, _ in tree] == ['10.50.0.0/18', '10.50.64.0/18']
        kids = [str(n) for n, _ in tree[0][1]]
        assert kids == ['10.50.0.0/24', '10.50.1.0/24']

    def test_carve_inside_an_unused_block(self, app, proj):
        with app.app_context():
            self._carve(app, proj, '10.50.0.0/18')
            self._carve(app, proj, '10.50.0.0/24', '110', 'MGMT')
            blocks = get_carved_subnets(app, 'admin', proj, 'sn')
        assert blocks['10.50.0.0/18']['status'] == 'container'
        assert blocks['10.50.0.0/24']['vlan_name'] == 'MGMT'

    def test_cannot_subdivide_an_assigned_block(self, app, proj):
        """It is in use as a leaf; subdividing it would be ambiguous."""
        with app.app_context():
            self._carve(app, proj, '10.50.64.0/18', '20', 'Trusted')
            with pytest.raises(ValueError) as e:
                self._carve(app, proj, '10.50.64.0/24')
        assert 'Release it before subdividing' in str(e.value)

    def test_cannot_subdivide_a_delegated_block(self, app, proj):
        with app.app_context():
            add_pool(app, 'admin', proj, {
                'id': 'lb', 'type': 'unique', 'name': 'LB', 'prefix': '32',
                'subnet': '10.50.200.0/24', 'parent_pool_id': 'sn'})
            with pytest.raises(ValueError) as e:
                self._carve(app, proj, '10.50.200.0/28')
        assert 'delegated' in str(e.value)

    def test_carving_must_go_outside_in(self, app, proj):
        """Wrapping a block around existing ones would silently reparent."""
        with app.app_context():
            self._carve(app, proj, '10.50.0.0/24')
            with pytest.raises(ValueError) as e:
                self._carve(app, proj, '10.50.0.0/18')
        assert 'would contain the existing block' in str(e.value)

    def test_depth_is_capped(self, app):
        from app.project import create_project, MAX_BLOCK_DEPTH
        with app.app_context():
            create_project(app, 'admin', 'deep')
            add_pool(app, 'admin', 'deep', {
                'id': 'sn', 'type': 'vlan_supernet', 'name': 'C',
                'subnet': '10.0.0.0/8'})
            for cidr in ('10.0.0.0/16', '10.0.0.0/20', '10.0.0.0/24'):
                carve_supernet_block(app, 'admin', 'deep', 'sn', cidr)
            with pytest.raises(ValueError) as e:
                carve_supernet_block(app, 'admin', 'deep', 'sn', '10.0.0.0/28')
        assert 'limited to %s levels' % MAX_BLOCK_DEPTH in str(e.value)

    def test_suggestions_are_per_level(self, app, proj):
        with app.app_context():
            self._carve(app, proj, '10.50.0.0/18')
            self._carve(app, proj, '10.50.0.0/24')
            top = suggest_free_block(app, 'admin', proj, 'sn', 24)
            inside = suggest_free_block(app, 'admin', proj, 'sn', 24,
                                        within='10.50.0.0/18')
        assert top == '10.50.64.0/24', 'top level must skip the container'
        assert inside == '10.50.1.0/24', 'inside must continue after the child'

    def test_container_with_children_cannot_be_removed(self, app, proj):
        with app.app_context():
            self._carve(app, proj, '10.50.0.0/18')
            self._carve(app, proj, '10.50.0.0/24')
            with pytest.raises(ValueError) as e:
                remove_supernet_block(app, 'admin', proj, 'sn', '10.50.0.0/18')
        assert 'contains 1 block' in str(e.value)

    def test_emptying_a_container_makes_it_usable_again(self, app, proj):
        with app.app_context():
            self._carve(app, proj, '10.50.0.0/18')
            self._carve(app, proj, '10.50.0.0/24')
            remove_supernet_block(app, 'admin', proj, 'sn', '10.50.0.0/24')
            blocks = get_carved_subnets(app, 'admin', proj, 'sn')
        assert blocks['10.50.0.0/18']['status'] == 'carved'

    def test_duplicate_block_is_refused(self, app, proj):
        with app.app_context():
            self._carve(app, proj, '10.50.0.0/24')
            with pytest.raises(ValueError) as e:
                self._carve(app, proj, '10.50.0.0/24')
        assert 'already carved' in str(e.value)

    def test_page_indents_nested_blocks(self, app, auth_client, proj):
        with app.app_context():
            self._carve(app, proj, '10.50.0.0/18')
            self._carve(app, proj, '10.50.0.0/24', '110', 'MGMT')
        body = auth_client.get('/projects/%s/resources' % proj).data.decode()
        assert 'padding-left:32px' in body, 'child block is not indented'
        assert 'Container' in body
        assert "carveInto('sn', '10.50.0.0/18')" in body


class TestSubdivideButton:

    def test_offered_on_a_mixed_size_supernet(self, app, auth_client, proj):
        with app.app_context():
            carve_supernet_block(app, 'admin', proj, 'sn', '10.50.0.0/18')
        body = auth_client.get('/projects/%s/resources' % proj).data.decode()
        assert "carveInto('sn', '10.50.0.0/18')" in body

    def test_not_offered_on_a_fixed_supernet(self, app, auth_client, proj):
        """There is no block editor there, so the button would do nothing."""
        with app.app_context():
            add_pool(app, 'admin', proj, dict(FIXED))
        body = auth_client.get('/projects/%s/resources' % proj).data.decode()
        assert "carveInto('fx'" not in body

    def test_click_target_exists_for_every_button(self, app, auth_client, proj):
        """Each carveInto call must have a matching input to focus."""
        import re
        with app.app_context():
            carve_supernet_block(app, 'admin', proj, 'sn', '10.50.0.0/18')
            add_pool(app, 'admin', proj, dict(FIXED))
        body = auth_client.get('/projects/%s/resources' % proj).data.decode()
        for pool_id in set(re.findall(r"carveInto\('([^']+)'", body)):
            assert 'id="blk-subnet-%s"' % pool_id in body, (
                'carveInto offered for %s with no block editor' % pool_id)


class TestStrandedContainerRepair:
    """A container with no children is stranded — no Remove button, and it
    cannot be subdivided usefully. That state was reachable by removing a
    delegated pool nested inside one.
    """

    def test_removing_a_nested_delegation_demotes_the_parent(self, app, proj):
        from app.project import remove_pool
        with app.app_context():
            carve_supernet_block(app, 'admin', proj, 'sn', '10.50.0.0/18')
            carve_supernet_block(app, 'admin', proj, 'sn', '10.50.0.0/24')
            add_pool(app, 'admin', proj, {
                'id': 'wm', 'type': 'unique', 'name': 'WM', 'prefix': '29',
                'subnet': '10.50.0.0/24', 'parent_pool_id': 'sn'})
            remove_pool(app, 'admin', proj, 'wm')
            blocks = get_carved_subnets(app, 'admin', proj, 'sn')

        assert '10.50.0.0/24' not in blocks
        assert blocks['10.50.0.0/18']['status'] == 'carved', \
            'empty container left stranded with no way to remove it'

    def test_existing_stranded_state_is_repaired_on_read(self, app, proj):
        """Projects already in this state must heal without manual editing."""
        import json, os
        from app.project import project_dir
        with app.app_context():
            path = os.path.join(project_dir(app, 'admin', proj), 'allocations.json')
            data = json.load(open(path))
            data['vlan_supernet']['sn'] = {'10.50.0.0/18': {
                'status': 'container', 'vlan_id': None, 'vlan_name': None,
                'hostname': None, 'peer_hostname': None}}
            json.dump(data, open(path, 'w'))

            blocks = get_carved_subnets(app, 'admin', proj, 'sn')
        assert blocks['10.50.0.0/18']['status'] == 'carved'

    def test_a_real_container_is_left_alone(self, app, proj):
        with app.app_context():
            carve_supernet_block(app, 'admin', proj, 'sn', '10.50.0.0/18')
            carve_supernet_block(app, 'admin', proj, 'sn', '10.50.0.0/24')
            blocks = get_carved_subnets(app, 'admin', proj, 'sn')
        assert blocks['10.50.0.0/18']['status'] == 'container'

    def test_repaired_block_is_removable(self, app, auth_client, proj):
        from app.project import remove_pool
        with app.app_context():
            carve_supernet_block(app, 'admin', proj, 'sn', '10.50.0.0/18')
            carve_supernet_block(app, 'admin', proj, 'sn', '10.50.0.0/24')
            add_pool(app, 'admin', proj, {
                'id': 'wm', 'type': 'unique', 'name': 'WM', 'prefix': '29',
                'subnet': '10.50.0.0/24', 'parent_pool_id': 'sn'})
            remove_pool(app, 'admin', proj, 'wm')
        body = auth_client.get('/projects/%s/resources' % proj).data.decode()
        assert "removeBlock('sn', '10.50.0.0/18')" in body


class TestConvertToMixedSizes:
    """A fixed-carve supernet cannot hold a /24 and a /18 side by side, and
    none of the carve or subdivide controls apply to it. Converting has to
    be possible in place — removing and recreating would take the blocks and
    their allocations with it.
    """

    @pytest.fixture
    def fixed(self, app):
        from app.project import assign_vlan_subnet
        with app.app_context():
            create_project(app, 'admin', 'f')
            add_pool(app, 'admin', 'f', {
                'id': 'sn', 'type': 'vlan_supernet', 'name': 'Wireless',
                'subnet': '10.50.0.0/16', 'carve_prefix': '18'})
            assign_vlan_subnet(app, 'admin', 'f', 'sn',
                               '10.50.64.0/18', '20', 'Trusted')
            add_pool(app, 'admin', 'f', {
                'id': 'wm', 'type': 'unique', 'name': 'WM', 'prefix': '29',
                'subnet': '10.50.0.0/18', 'parent_pool_id': 'sn'})
        return 'f'

    def test_blocks_in_use_are_kept(self, app, fixed):
        from app.project import convert_supernet_to_mixed
        with app.app_context():
            kept, dropped = convert_supernet_to_mixed(app, 'admin', fixed, 'sn')
        assert kept == ['10.50.0.0/18', '10.50.64.0/18']
        assert dropped == ['10.50.128.0/18', '10.50.192.0/18']

    def test_vlan_and_delegation_survive(self, app, fixed):
        from app.project import convert_supernet_to_mixed
        with app.app_context():
            convert_supernet_to_mixed(app, 'admin', fixed, 'sn')
            blocks = get_carved_subnets(app, 'admin', fixed, 'sn')
        assert blocks['10.50.64.0/18']['vlan_name'] == 'Trusted'
        assert blocks['10.50.0.0/18']['delegated_to'] == 'wm'

    def test_carve_prefix_is_removed(self, app, fixed):
        from app.project import convert_supernet_to_mixed, is_manual_supernet
        with app.app_context():
            convert_supernet_to_mixed(app, 'admin', fixed, 'sn')
            pool = [p for p in get_project_config(app, 'admin', fixed)['pools']
                    if p['id'] == 'sn'][0]
        assert is_manual_supernet(pool)

    def test_freed_space_accepts_other_sizes(self, app, fixed):
        """The point of converting."""
        from app.project import convert_supernet_to_mixed
        with app.app_context():
            convert_supernet_to_mixed(app, 'admin', fixed, 'sn')
            carve_supernet_block(app, 'admin', fixed, 'sn',
                                 '10.50.128.0/24', '30', 'MGMT')
            blocks = get_carved_subnets(app, 'admin', fixed, 'sn')
        assert blocks['10.50.128.0/24']['vlan_name'] == 'MGMT'

    def test_converting_twice_is_refused(self, app, fixed):
        from app.project import convert_supernet_to_mixed
        with app.app_context():
            convert_supernet_to_mixed(app, 'admin', fixed, 'sn')
            with pytest.raises(ValueError) as e:
                convert_supernet_to_mixed(app, 'admin', fixed, 'sn')
        assert 'already uses mixed sizes' in str(e.value)

    def test_button_offered_only_on_fixed_supernets(self, app, auth_client, fixed):
        body = auth_client.get('/projects/%s/resources' % fixed).data.decode()
        assert "convertSupernet('sn'" in body

    def test_button_gone_after_conversion(self, app, auth_client, fixed):
        from app.project import convert_supernet_to_mixed
        with app.app_context():
            convert_supernet_to_mixed(app, 'admin', fixed, 'sn')
        body = auth_client.get('/projects/%s/resources' % fixed).data.decode()
        assert "convertSupernet('sn'" not in body
        assert "carveBlock('sn')" in body, 'block editor should now be available'

    def test_convert_via_api(self, app, auth_client, fixed):
        res = auth_client.post('/projects/%s/api/pools/sn/convert' % fixed)
        body = res.get_json()
        assert body['ok'] is True
        assert body['dropped'] == 2

    def test_convert_requires_login(self, client):
        assert client.post('/projects/f/api/pools/sn/convert').status_code == 401
