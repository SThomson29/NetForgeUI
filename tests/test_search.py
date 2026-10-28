"""
Search across projects: IPs, subnets, VLAN/VNI numbers and text, over
host_vars, Resources pools and deployment IPs, limited to what the caller
can see.
"""

import os

import pytest
import yaml

from app import search, sharing
from app.models import User
from app.project import add_pool, create_project, project_dir, project_host_vars_dir
from app.utils import add_project_hosts, ensure_workspace


def write_hv(app, owner, project, host, fname, data):
    path = os.path.join(project_host_vars_dir(app, owner, project), host, fname)
    with open(path, 'w') as f:
        yaml.safe_dump(data, f)


@pytest.fixture
def site(app, skeleton_dir):
    """admin/site1 with two switches, a pool and deployment IPs."""
    ensure_workspace(app, 'admin')
    create_project(app, 'admin', 'site1')
    add_project_hosts(app, 'admin', 'site1', ['CORE-01', 'ACC-01'], 'none')
    write_hv(app, 'admin', 'site1', 'CORE-01', 'interfaces.yml', {
        'loopback_interfaces': [{'name': 'loopback0', 'ip_address': '10.255.0.1/32'}],
        'vlan_interfaces': [{'name': 'vlan30', 'description': 'Printers',
                             'ip_address': '10.20.30.1', 'ip_prefix': '24'}],
        'physical_interfaces': [{'name': '1/1/1', 'trunk_allowed_vlans': '10,20-40',
                                 'description': 'Uplink to ACC-01'}],
        'lag_interfaces': [], 'interface_groups': [],
    })
    write_hv(app, 'admin', 'site1', 'CORE-01', 'vlans.yml',
             {'vlans': [{'id': '30', 'name': 'PRINTERS'}, {'id': '300', 'name': 'CCTV'}]})
    write_hv(app, 'admin', 'site1', 'CORE-01', 'vxlan.yml',
             {'vxlan': {'vni_map': [{'vni': '10030', 'vlan': '30'}]}})
    write_hv(app, 'admin', 'site1', 'CORE-01', 'aaa.yml',
             {'radius_server_key': 'Printers-secret', 'radius_servers': ['10.1.1.10']})
    write_hv(app, 'admin', 'site1', 'ACC-01', 'general.yml',
             {'hostname': 'ACC-01', 'ntp_servers': ['10.1.1.5'], 'mtu': '30'})
    add_pool(app, 'admin', 'site1', {'id': 'p1', 'name': 'Loopbacks', 'type': 'unique',
                                     'subnet': '10.255.0.0/24'})
    with open(os.path.join(project_dir(app, 'admin', 'site1'), 'deploy_mapping.yml'), 'w') as f:
        yaml.safe_dump({'hosts': [{'hostname': 'ACC-01', 'mgmt_ip': '192.168.50.11'}]}, f)
    return app


def hits(app, text, user='admin', **kw):
    found = search.run(app, User.get_by_username(user, app), text, **kw)
    return [(r['owner'], r['project'], h['kind'], h['host'], h['match'], h['value'], h['where'])
            for r in found['results'] for h in r['hits']]


class TestQuery:

    @pytest.mark.parametrize('text, kind', [
        ('10.1.1.5', 'ip'), ('10.20.30.0/24', 'subnet'), ('10.20.30.7/24', 'subnet'),
        ('2001:db8::1', 'ip'), ('30', 'number'), ('core', 'text'), ('x', None), ('', None),
        ('  vlan30 ', 'text'),
    ])
    def test_kinds(self, text, kind):
        assert search.Query(text).kind == kind


class TestMatching:

    def test_exact_ip(self, site, auth_client):
        found = hits(site, '10.1.1.5')
        assert [(h[3], h[4], h[6]) for h in found] == [('ACC-01', 'exact', 'general › ntp_servers')]

    def test_ip_inside_an_svi_subnet(self, site, auth_client):
        found = hits(site, '10.20.30.5')
        assert ('CORE-01', 'in subnet', '10.20.30.1/24') in [(h[3], h[4], h[5]) for h in found]

    def test_svi_address_itself_is_exact(self, site, auth_client):
        found = hits(site, '10.20.30.1')
        assert [(h[4], h[5]) for h in found if h[2] == 'switch'] == [('exact', '10.20.30.1/24')]

    def test_ip_in_a_pool(self, site, auth_client):
        found = hits(site, '10.255.0.1')
        kinds = {(h[2], h[4]) for h in found}
        assert ('switch', 'exact') in kinds and ('pool', 'in pool') in kinds

    def test_subnet(self, site, auth_client):
        found = hits(site, '10.255.0.0/16')
        assert {(h[2], h[4]) for h in found} == {('switch', 'in range'), ('pool', 'overlaps')}

    def test_deployment_ip(self, site, auth_client):
        found = hits(site, '192.168.50.11')
        assert [(h[2], h[3], h[4]) for h in found] == [('deploy', 'ACC-01', 'exact')]

    def test_number_is_vlans_and_vnis_only(self, site, auth_client):
        found = hits(site, '30')
        where = {h[6] for h in found}
        assert 'vlans › PRINTERS › id' in where                  # the VLAN itself
        assert 'interfaces › vlan_interfaces › vlan30 › name' in where   # the SVI
        assert 'interfaces › physical_interfaces › 1/1/1 › trunk_allowed_vlans' in where
        assert 'vxlan › vni_map › 10030 › vlan' in where
        # mtu: 30 is not a VLAN; VLAN 300 is not 30.
        assert not any('mtu' in w or 'CCTV' in w for w in where)

    def test_vni(self, site, auth_client):
        found = hits(site, '10030')
        assert [(h[4], h[6]) for h in found] == [('VNI', 'vxlan › vni_map › 10030 › vni')]

    def test_text_finds_names_and_descriptions(self, site, auth_client):
        found = hits(site, 'printers')
        values = {h[5] for h in found}
        assert {'Printers', 'PRINTERS'} <= values

    def test_text_finds_switch_names(self, site, auth_client):
        found = hits(site, 'acc-0')
        assert ('switch', 'ACC-01', 'Switch name') in [(h[2], h[3], h[6]) for h in found]

    def test_secrets_are_not_searched_or_shown(self, site, auth_client):
        assert not any('radius_server_key' in h[6] for h in hits(site, 'secret'))
        assert not any(h[5] == 'Printers-secret' for h in hits(site, 'printers'))

    def test_pool_names(self, site, auth_client):
        found = hits(site, 'loopbacks')
        assert [(h[2], h[5]) for h in found] == [('pool', 'Loopbacks  10.255.0.0/24')]

    def test_ipv4_query_ignores_ipv6_values(self, site, auth_client):
        write_hv(site, 'admin', 'site1', 'ACC-01', 'v6.yml', {'addr': '2001:db8::1/64'})
        assert hits(site, '10.0.0.0/8') and not any('v6' in h[6] for h in hits(site, '10.0.0.0/8'))
        assert [h[4] for h in hits(site, '2001:db8::5')] == ['in subnet']

    def test_broken_yaml_is_skipped(self, site, auth_client):
        path = os.path.join(project_host_vars_dir(site, 'admin', 'site1'), 'ACC-01', 'bad.yml')
        with open(path, 'w') as f:
            f.write('a: [unclosed\n')
        assert hits(site, '10.1.1.5')

    def test_results_are_capped(self, site, auth_client, monkeypatch):
        monkeypatch.setattr(search, 'MAX_HITS', 2)
        found = search.run(site, User.get_by_username('admin', site), '30')
        assert found['total'] == 2 and found['truncated']


class TestWhoSeesWhat:

    @pytest.fixture
    def bob(self, site, auth_client):
        User.create('bob', 'password1', site)
        ensure_workspace(site, 'bob')
        create_project(site, 'bob', 'lab')
        add_project_hosts(site, 'bob', 'lab', ['LAB-SW'], 'none')
        write_hv(site, 'bob', 'lab', 'LAB-SW', 'general.yml', {'ntp_servers': ['10.1.1.5']})
        return site

    def test_only_your_own_and_shared_projects(self, bob):
        assert {(h[0], h[1]) for h in hits(bob, '10.1.1.5', user='bob')} == {('bob', 'lab')}
        sharing.set_share(bob, 'admin', 'site1', 'user', 'bob', sharing.VIEW, 'admin')
        assert {(h[0], h[1]) for h in hits(bob, '10.1.1.5', user='bob')} == \
            {('bob', 'lab'), ('admin', 'site1')}

    def test_admins_search_everything_only_when_asked(self, bob):
        assert {h[0] for h in hits(bob, '10.1.1.5')} == {'admin'}
        assert {h[0] for h in hits(bob, '10.1.1.5', include_all=True)} == {'admin', 'bob'}

    def test_include_all_is_ignored_for_non_admins(self, bob):
        assert {h[0] for h in hits(bob, '10.1.1.5', user='bob', include_all=True)} == {'bob'}

    def test_one_project(self, bob):
        assert {h[1] for h in hits(bob, '10.1.1.5', only=('admin', 'site1'))} == {'site1'}

    def test_one_project_you_cannot_see(self, bob):
        assert hits(bob, '10.1.1.5', user='bob', only=('admin', 'site1')) == []
        assert hits(bob, '10.1.1.5', user='bob', only=('..', '..')) == []


class TestPage:

    def test_sidebar_box(self, site, auth_client):
        body = auth_client.get('/projects').get_data(as_text=True)
        assert 'action="/search"' in body and 'name="in"' not in body

    def test_sidebar_box_inside_a_project_scopes_to_it(self, site, auth_client):
        body = auth_client.get('/projects/admin/site1/hosts').get_data(as_text=True)
        assert 'name="in" value="admin/site1"' in body

    def test_results_link_to_the_editor(self, site, auth_client):
        body = auth_client.get('/search?q=10.1.1.5').get_data(as_text=True)
        assert '/projects/admin/site1/editor?host=ACC-01' in body
        assert 'general › ntp_servers' in body and '1 match in 1 project' in body

    def test_pool_and_deploy_links(self, site, auth_client):
        body = auth_client.get('/search?q=10.255.0.1').get_data(as_text=True)
        assert '/projects/admin/site1/resources' in body
        body = auth_client.get('/search?q=192.168.50.11').get_data(as_text=True)
        assert '/projects/admin/site1/deployment-ips' in body

    def test_empty_and_too_short(self, site, auth_client):
        assert 'What you can search for' in auth_client.get('/search').get_data(as_text=True)
        assert 'at least two characters' in auth_client.get('/search?q=x').get_data(as_text=True)
        assert 'Nothing found' in auth_client.get('/search?q=zzzz').get_data(as_text=True)

    def test_query_is_escaped(self, site, auth_client):
        body = auth_client.get('/search?q=<script>x</script>').get_data(as_text=True)
        assert '<script>x</script>' not in body

    def test_admin_all_toggle(self, site, auth_client):
        assert "Search all users' projects" in auth_client.get('/search?q=10.1.1.5').get_data(as_text=True)

    def test_needs_login(self, client):
        assert client.get('/search?q=10.1.1.5').status_code in (302, 401)
