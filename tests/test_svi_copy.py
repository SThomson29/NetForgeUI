"""
Copying SVIs between switches, including across sites.

Copying used to keep each SVI in the source switch's subnet, so copying
site A's SVIs onto a site B switch handed out site A addresses. Now each
SVI is matched by VLAN ID to a block in a chosen target supernet; anything
that cannot be placed comes over unaddressed rather than carrying an
address that can only be a duplicate.

Like the other editor tests, these render the page and check the shipped
JavaScript for the logic each behaviour depends on.
"""

import os

import pytest


@pytest.fixture
def page(app, auth_client):
    from app.project import create_project, project_host_vars_dir
    with app.app_context():
        create_project(app, 'admin', 'xs')
        base = project_host_vars_dir(app, 'admin', 'xs')
        for host in ('CTY-Core-01', 'RIV-Core-01'):
            os.makedirs(os.path.join(base, host), exist_ok=True)
    return auth_client.get('/projects/xs/editor?host=RIV-Core-01').data.decode()


def _function(body, name):
    start = body.index('function %s(' % name)
    return body[start:body.index('\n  }\n', start)]


class TestPanel:

    def test_addressing_selector_is_offered(self, page):
        assert 'svi-copy-target' in page
        assert 'Same subnets as the source switch' in page

    def test_selector_lists_the_supernets(self, page):
        assert "p.type === 'vlan_supernet'" in page

    def test_defaults_to_the_switchs_own_supernet(self, page):
        assert 'defaultValue: detectSviSupernet()' in page

    def test_chosen_target_is_passed_to_the_copy(self, page):
        assert "getElementById('svi-copy-target').value" in page


class TestCrossSiteMatching:

    def test_matches_blocks_by_vlan_id(self, page):
        fn = _function(page, 'copySvisFrom')
        assert 'byVlan[vlanId]' in fn
        assert "/^vlan\\s*(\\d+)$/i" in fn

    def test_active_gateway_keeps_its_position(self, page):
        """.250 at one site becomes .250 at the other, not an assumed .254."""
        fn = _function(page, 'copySvisFrom')
        assert 'agw - srcNet' in fn

    def test_prefix_follows_the_target_block(self, page):
        fn = _function(page, 'copySvisFrom')
        assert "out.ip_prefix = subnet.split('/')[1]" in fn


class TestNoDuplicates:

    def test_unplaceable_svis_are_cleared_and_named(self, page):
        """An address outside any block is the source switch's own."""
        fn = _function(page, 'copySvisFrom')
        assert 'unmatched.push(' in fn
        assert "out.ip_address = ''" in fn
        assert 'address these by hand' in fn

    def test_addresses_are_claimed_as_they_are_assigned(self, page):
        """Two SVIs in one subnet must not get the same address."""
        fn = _function(page, 'copySvisFrom')
        assert 'taken[candidate] = true' in fn

    def test_existing_allocations_are_skipped(self, page):
        fn = _function(page, 'copySvisFrom')
        assert 'PROJECT_ALLOCS.svi' in fn


class TestDetection:

    def test_uses_the_switchs_addresses_and_records(self, page):
        fn = _function(page, 'detectSviSupernet')
        assert 'subnetForIp(' in fn
        assert 'a.hostname === SELECTED_HOST' in fn
        assert 'b.hostname === SELECTED_HOST' in fn
