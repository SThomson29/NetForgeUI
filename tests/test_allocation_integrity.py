"""
Allocation integrity: concurrent writes and /31 links.

config.json and allocations.json were read, changed and rewritten with no
lock and no atomic replace. The editor sends several allocation calls at
once and gunicorn runs them on parallel threads, so writes were lost and
readers could see a half-written file.

Taking the second end of a /31 from the editor overwrote the first switch's
end with a reservation, so the editor then offered that address as free.
"""

import json
import os
import threading

import pytest

from app.project import (
    create_project, delete_project, list_projects, add_pool,
    allocate_unique, release_unique, allocate_ptp, release_ptp,
    get_available_ptp_pairs, get_all_allocations, sync_allocations,
    project_dir, project_allocations_path, projects_dir, project_lock,
)
from tests.test_project import USERNAME, make_app, write_interfaces, write_vxlan


@pytest.fixture
def proj(tmp_data_dir, sample_pools):
    app = make_app(tmp_data_dir)
    create_project(app, USERNAME, 'proj')
    add_pool(app, USERNAME, 'proj', sample_pools['unique'])
    add_pool(app, USERNAME, 'proj', sample_pools['ptp'])
    return app


def _ptp(app):
    return get_all_allocations(app, USERNAME, 'proj')['point_to_point']['pool_ptp']


def _run_threads(fns):
    errors = []

    def wrap(fn):
        try:
            fn()
        except Exception as e:          # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=wrap, args=(fn,)) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

class TestConcurrentWrites:

    def test_parallel_allocations_are_all_kept(self, proj, sample_pools):
        pool = sample_pools['unique']['id']
        net = sample_pools['unique']['subnet'].rsplit('.', 1)[0]
        errors = _run_threads([
            (lambda i=i: allocate_unique(proj, USERNAME, 'proj', pool,
                                         '%s.%d' % (net, i), 'sw-%d' % i, 'lo0'))
            for i in range(1, 41)])
        assert errors == []
        assert len(get_all_allocations(proj, USERNAME, 'proj')['unique'][pool]) == 40

    def test_release_and_allocate_together(self, proj, sample_pools):
        """What the editor does when an IP is changed: both calls at once."""
        pool = sample_pools['unique']['id']
        net = sample_pools['unique']['subnet'].rsplit('.', 1)[0]
        for round_ in range(20):
            old, new = '%s.%d' % (net, 1 + round_ % 2), '%s.%d' % (net, 2 - round_ % 2)
            allocate_unique(proj, USERNAME, 'proj', pool, old, 'sw-01', 'lo0')
            errors = _run_threads([
                lambda: release_unique(proj, USERNAME, 'proj', pool, old),
                lambda: allocate_unique(proj, USERNAME, 'proj', pool, new, 'sw-01', 'lo0'),
            ])
            assert errors == []
            held = get_all_allocations(proj, USERNAME, 'proj')['unique'][pool]
            assert new in held and old not in held, round_

    def test_readers_never_see_a_partial_file(self, proj, sample_pools):
        pool = sample_pools['unique']['id']
        net = sample_pools['unique']['subnet'].rsplit('.', 1)[0]
        path = project_allocations_path(proj, USERNAME, 'proj')
        stop = threading.Event()
        bad = []

        def reader():
            while not stop.is_set():
                try:
                    with open(path) as f:
                        json.load(f)
                except ValueError as e:
                    bad.append(e)

        r = threading.Thread(target=reader)
        r.start()
        try:
            for i in range(1, 120):
                allocate_unique(proj, USERNAME, 'proj', pool,
                                '%s.%d' % (net, i % 200 + 1), 'sw', 'lo%d' % i)
        finally:
            stop.set()
            r.join()
        assert bad == []

    def test_no_temporary_files_left_behind(self, proj, sample_pools):
        allocate_unique(proj, USERNAME, 'proj', sample_pools['unique']['id'],
                        sample_pools['unique']['subnet'].rsplit('.', 1)[0] + '.5',
                        'sw', 'lo0')
        leftovers = [f for f in os.listdir(project_dir(proj, USERNAME, 'proj'))
                     if f.startswith('.tmp-')]
        assert leftovers == []

    def test_lock_is_reentrant(self, proj):
        with project_lock(proj, USERNAME, 'proj'):
            with project_lock(proj, USERNAME, 'proj'):
                pass

    def test_lock_file_is_not_a_project(self, proj):
        names = [p['name'] for p in list_projects(proj, USERNAME)]
        assert names == ['proj']

    def test_delete_removes_lock_file(self, proj):
        delete_project(proj, USERNAME, 'proj')
        assert not any(f.endswith('.lock')
                       for f in os.listdir(projects_dir(proj, USERNAME)))


# ---------------------------------------------------------------------------
# /31 links
# ---------------------------------------------------------------------------

class TestPtpLinks:

    def test_second_end_keeps_the_first_switch(self, proj):
        allocate_ptp(proj, USERNAME, 'proj', 'pool_ptp', '10.254.0.0', 'sw-a', '1/1/1')
        allocate_ptp(proj, USERNAME, 'proj', 'pool_ptp', '10.254.0.1', 'sw-b', '1/1/49')
        a, b = _ptp(proj)['10.254.0.0'], _ptp(proj)['10.254.0.1']
        assert (a['hostname'], a['interface']) == ('sw-a', '1/1/1')
        assert (b['hostname'], b['interface']) == ('sw-b', '1/1/49')
        assert 'status' not in a and 'status' not in b
        assert (a['peer_hostname'], a['peer_interface']) == ('sw-b', '1/1/49')
        assert (b['peer_hostname'], b['peer_interface']) == ('sw-a', '1/1/1')

    def test_linked_pair_is_not_offered_as_available(self, proj):
        allocate_ptp(proj, USERNAME, 'proj', 'pool_ptp', '10.254.0.0', 'sw-a', '1/1/1')
        allocate_ptp(proj, USERNAME, 'proj', 'pool_ptp', '10.254.0.1', 'sw-b', '1/1/49')
        pair = next(p for p in get_available_ptp_pairs(proj, USERNAME, 'proj', 'pool_ptp')
                    if p['ip_a'] == '10.254.0.0')
        assert pair['status'] == 'allocated'

    def test_releasing_one_end_hands_it_back_to_the_other(self, proj):
        allocate_ptp(proj, USERNAME, 'proj', 'pool_ptp', '10.254.0.0', 'sw-a', '1/1/1')
        allocate_ptp(proj, USERNAME, 'proj', 'pool_ptp', '10.254.0.1', 'sw-b', '1/1/49')
        release_ptp(proj, USERNAME, 'proj', 'pool_ptp', '10.254.0.1')
        a, b = _ptp(proj)['10.254.0.0'], _ptp(proj)['10.254.0.1']
        assert a['hostname'] == 'sw-a' and a['peer_hostname'] is None
        assert b['status'] == 'reserved_for_peer' and b['peer_hostname'] == 'sw-a'

    def test_releasing_the_only_end_frees_the_pair(self, proj):
        allocate_ptp(proj, USERNAME, 'proj', 'pool_ptp', '10.254.0.0', 'sw-a', '1/1/1')
        release_ptp(proj, USERNAME, 'proj', 'pool_ptp', '10.254.0.0')
        assert _ptp(proj) == {}

    def _routed(self, ip):
        return ('interface_groups: []\nphysical_interfaces:\n'
                '  - name: "1/1/49"\n    routed: "true"\n'
                '    ip_address: "%s"\n    ip_prefix: "31"\n'
                'lag_interfaces: []\nloopback_interfaces: []\n'
                'vlan_interfaces: []\n' % ip)

    def test_editor_then_save_agree(self, proj):
        """The instant allocation and the save-time sync share one routine,
        so saving sw-b after picking its end changes nothing for sw-a."""
        allocate_ptp(proj, USERNAME, 'proj', 'pool_ptp', '10.254.0.0', 'sw-a', '1/1/1')
        allocate_ptp(proj, USERNAME, 'proj', 'pool_ptp', '10.254.0.1', 'sw-b', '1/1/49')
        before = _ptp(proj)
        write_interfaces(proj, 'proj', 'sw-b', self._routed('10.254.0.1'))
        write_vxlan(proj, 'proj', 'sw-b', 'loopback_interface: loopback1\nloopback_ip:\n')
        sync_allocations(proj, USERNAME, 'proj', 'sw-b')
        assert _ptp(proj) == before

    def test_removing_the_interface_on_save_hands_the_end_back(self, proj):
        allocate_ptp(proj, USERNAME, 'proj', 'pool_ptp', '10.254.0.0', 'sw-a', '1/1/1')
        write_vxlan(proj, 'proj', 'sw-b', 'loopback_interface: loopback1\nloopback_ip:\n')
        write_interfaces(proj, 'proj', 'sw-b', self._routed('10.254.0.1'))
        sync_allocations(proj, USERNAME, 'proj', 'sw-b')
        write_interfaces(proj, 'proj', 'sw-b', self._routed(''))
        sync_allocations(proj, USERNAME, 'proj', 'sw-b')
        a, b = _ptp(proj)['10.254.0.0'], _ptp(proj)['10.254.0.1']
        assert a['hostname'] == 'sw-a' and a['peer_hostname'] is None
        assert b['status'] == 'reserved_for_peer' and b['peer_hostname'] == 'sw-a'


# ---------------------------------------------------------------------------
# Editor
# ---------------------------------------------------------------------------

class TestEditorReportsFailedAllocations:
    """The editor updated its pickers and fired the request with errors
    swallowed, so a failed call left the pickers showing a state the
    server never recorded."""

    @pytest.fixture
    def script(self):
        with open(os.path.join(os.path.dirname(__file__), '..', 'app',
                               'templates', 'project_editor.html')) as f:
            src = f.read()
        return src[src.index('var __triggerAllocRefresh'):
                   src.index('// PooledIPField')]

    def test_errors_are_not_swallowed(self, script):
        # Only the reload may ignore its own failure — there is nothing
        # further to fall back to. The allocation requests must not.
        rest = script[:script.index('function poolReloadAllocations(')] + \
            script[script.index('function poolAllocateIP('):]
        assert 'catch(function(){})' not in rest.replace(' ', '')

    def test_both_calls_go_through_the_checked_request(self, script):
        for fn in ('poolAllocateIP', 'poolReleaseIP'):
            body = script[script.index('function %s(' % fn):]
            body = body[:body.index('\n}\n')]
            assert 'poolRequest(' in body, fn
            assert 'fetch(' not in body, fn
            # local update before the request, so a failure's reload wins
            assert body.index('__triggerAllocRefresh()') < body.index('poolRequest('), fn

    def test_failure_checks_status_and_reloads(self, script):
        body = script[script.index('function poolRequest('):]
        assert 'res.ok' in body
        assert 'data.ok === false' in body
        assert 'poolReloadAllocations()' in body
        assert 'toast(' in body

    def test_reload_endpoint_returns_allocations(self, app, auth_client):
        from app.project import create_project
        with app.app_context():
            create_project(app, 'admin', 'reload')
        data = auth_client.get('/projects/reload/api/allocations').get_json()
        assert {'unique', 'point_to_point'} <= set(data)


# ---------------------------------------------------------------------------
# Shared anycast VTEP — editor path
# ---------------------------------------------------------------------------

class TestSharedVtepFromEditor:
    """Picking the pair's VTEP for the second switch in the editor used to
    overwrite the first switch's record and drop the shared flag until the
    second switch was saved."""

    @pytest.fixture
    def vtep(self, app):
        with app.app_context():
            create_project(app, 'admin', 'v')
            add_pool(app, 'admin', 'v', {
                'id': 'lb', 'type': 'unique', 'name': 'VTEP',
                'prefix': '32', 'subnet': '10.255.1.0/24'})
        return app

    def _save_vtep(self, app, host, ip):
        from app.project import project_host_vars_dir
        hv = os.path.join(project_host_vars_dir(app, 'admin', 'v'), host)
        os.makedirs(hv, exist_ok=True)
        with open(os.path.join(hv, 'vxlan.yml'), 'w') as f:
            f.write('loopback_interface: loopback1\nloopback_ip: "%s"\n'
                    'ospf_area: "0.0.0.0"\nvxlan:\n  vni_map: []\n' % ip)
        sync_allocations(app, 'admin', 'v', host)

    def _lb(self, app):
        return get_all_allocations(app, 'admin', 'v')['unique']['lb']

    def test_second_switch_is_added_not_swapped_in(self, vtep):
        self._save_vtep(vtep, 'core-01', '10.255.1.1')
        allocate_unique(vtep, 'admin', 'v', 'lb', '10.255.1.1', 'core-02', 'Loopback IP')
        e = self._lb(vtep)['10.255.1.1']
        assert e['shared'] is True
        assert (e['hostname'], e['shared_with']) == ('core-02', 'core-01')

    def test_editor_then_save_agree(self, vtep):
        self._save_vtep(vtep, 'core-01', '10.255.1.1')
        allocate_unique(vtep, 'admin', 'v', 'lb', '10.255.1.1', 'core-02', 'Loopback IP')
        self._save_vtep(vtep, 'core-02', '10.255.1.1')
        e = self._lb(vtep)['10.255.1.1']
        assert e['shared'] is True
        assert {e['hostname'], e['shared_with']} == {'core-01', 'core-02'}

    def test_second_switch_moving_away_leaves_the_first(self, vtep):
        """What the editor sends when core-02's VTEP is changed."""
        self._save_vtep(vtep, 'core-01', '10.255.1.1')
        allocate_unique(vtep, 'admin', 'v', 'lb', '10.255.1.1', 'core-02', 'Loopback IP')
        release_unique(vtep, 'admin', 'v', 'lb', '10.255.1.1', 'core-02')
        e = self._lb(vtep)['10.255.1.1']
        assert e == {'hostname': 'core-01', 'interface': 'loopback1', 'shared': True}

    def test_first_switch_moving_away_leaves_the_second(self, vtep):
        self._save_vtep(vtep, 'core-01', '10.255.1.1')
        allocate_unique(vtep, 'admin', 'v', 'lb', '10.255.1.1', 'core-02', 'Loopback IP')
        release_unique(vtep, 'admin', 'v', 'lb', '10.255.1.1', 'core-01')
        e = self._lb(vtep)['10.255.1.1']
        assert e['hostname'] == 'core-02' and 'shared_with' not in e

    def test_last_holder_leaving_frees_it(self, vtep):
        self._save_vtep(vtep, 'core-01', '10.255.1.1')
        release_unique(vtep, 'admin', 'v', 'lb', '10.255.1.1', 'core-01')
        assert '10.255.1.1' not in self._lb(vtep)

    def test_unshared_address_is_still_overwritten(self, vtep):
        """Only addresses the sync marked shared are shared. A loopback0
        clash is a mistake to surface, not to accommodate."""
        allocate_unique(vtep, 'admin', 'v', 'lb', '10.255.1.5', 'core-01', 'loopback0')
        allocate_unique(vtep, 'admin', 'v', 'lb', '10.255.1.5', 'core-02', 'loopback0')
        assert self._lb(vtep)['10.255.1.5'] == {'hostname': 'core-02',
                                               'interface': 'loopback0'}

    def test_release_without_hostname_removes_it(self, vtep):
        """Older pages (cached before this change) send no hostname."""
        self._save_vtep(vtep, 'core-01', '10.255.1.1')
        allocate_unique(vtep, 'admin', 'v', 'lb', '10.255.1.1', 'core-02', 'Loopback IP')
        release_unique(vtep, 'admin', 'v', 'lb', '10.255.1.1')
        assert '10.255.1.1' not in self._lb(vtep)

    def test_release_route_passes_the_switch(self, vtep, auth_client):
        self._save_vtep(vtep, 'core-01', '10.255.1.1')
        allocate_unique(vtep, 'admin', 'v', 'lb', '10.255.1.1', 'core-02', 'Loopback IP')
        res = auth_client.post('/projects/v/api/allocations/unique/release',
                               json={'pool_id': 'lb', 'ip': '10.255.1.1',
                                     'hostname': 'core-02'})
        assert res.status_code == 200
        assert self._lb(vtep)['10.255.1.1']['hostname'] == 'core-01'

    def test_editor_sends_the_switch_on_release(self):
        with open(os.path.join(os.path.dirname(__file__), '..', 'app',
                               'templates', 'project_editor.html')) as f:
            src = f.read()
        assert 'poolReleaseIP(pool.id, pool.type, value, hostname)' in src
        assert 'poolReleaseIP(pool.id, pool.type, value)' not in src
