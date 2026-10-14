"""
Reachability check: TCP-probes mapped switches on SSH and HTTPS.

Addresses come from the saved mapping, never the request, so the route
cannot be used to probe arbitrary hosts or ports.
"""

import socket

import pytest

import app.reachability as reach


@pytest.fixture
def listener():
    """A real open port on localhost, and a closed one."""
    srv = socket.socket()
    srv.bind(('127.0.0.1', 0))
    srv.listen(8)
    closed = socket.socket()
    closed.bind(('127.0.0.1', 0))
    closed_port = closed.getsockname()[1]
    closed.close()                      # bound then released: nothing listens
    yield srv.getsockname()[1], closed_port
    srv.close()


class TestProbe:

    def test_open_and_refused(self, listener, monkeypatch):
        open_port, closed_port = listener
        monkeypatch.setattr(reach, 'PORTS', {'ssh': open_port, 'https': closed_port})
        (r,) = reach.check([{'hostname': 'sw1', 'mgmt_ip': '127.0.0.1'}])
        assert r['ssh']['ok'] is True and isinstance(r['ssh']['ms'], int)
        assert r['https'] == {'ok': False, 'ms': None, 'error': 'refused'}

    def test_unresolvable_name(self):
        (r,) = reach.check([{'hostname': 'sw1', 'mgmt_ip': 'no-such-switch.invalid'}],
                           timeout=1)
        assert r['ssh']['error'] == 'name not resolved'

    def test_timeout(self, monkeypatch):
        def slow(*a, **kw):
            raise socket.timeout()
        monkeypatch.setattr(reach.socket, 'create_connection', slow)
        (r,) = reach.check([{'hostname': 'sw1', 'mgmt_ip': '10.0.0.1'}])
        assert r['ssh']['error'] == 'timed out'

    def test_host_without_address_is_not_probed(self, monkeypatch):
        monkeypatch.setattr(reach.socket, 'create_connection',
                            lambda *a, **kw: pytest.fail('probed'))
        (r,) = reach.check([{'hostname': 'sw1', 'mgmt_ip': ''}])
        assert r['ssh'] is None and r['https'] is None

    def test_order_is_kept_and_runs_in_parallel(self, monkeypatch):
        import threading
        import time
        seen = []

        def fake(addr, timeout):
            seen.append(threading.get_ident())
            time.sleep(0.2)
            raise ConnectionRefusedError()
        monkeypatch.setattr(reach.socket, 'create_connection', fake)
        hosts = [{'hostname': 'sw%d' % i, 'mgmt_ip': '10.0.0.%d' % i} for i in range(10)]
        t0 = time.monotonic()
        results = reach.check(hosts)
        assert [r['hostname'] for r in results] == [h['hostname'] for h in hosts]
        assert time.monotonic() - t0 < 1.5       # 20 probes x 0.2s if serial = 4s
        assert len(set(seen)) > 1

    def test_host_count_is_capped(self, monkeypatch):
        monkeypatch.setattr(reach, 'MAX_HOSTS', 3)
        monkeypatch.setattr(reach.socket, 'create_connection',
                            lambda *a, **kw: (_ for _ in ()).throw(ConnectionRefusedError()))
        hosts = [{'hostname': 'sw%d' % i, 'mgmt_ip': '10.0.0.1'} for i in range(10)]
        assert len(reach.check(hosts)) == 3


class TestRoute:

    @pytest.fixture
    def mapped(self, app, auth_client, monkeypatch):
        import app.deploy_routes as dr
        mapping = {'hosts': [
            {'hostname': 'core-01', 'mgmt_ip': '10.1.0.1'},
            {'hostname': 'core-02', 'mgmt_ip': '10.1.0.2'},
            {'hostname': 'bad name', 'mgmt_ip': '10.1.0.3'},     # predates validation
        ], 'settings': {}}
        monkeypatch.setattr(dr, '_load_mapping', lambda a, u, p: mapping)
        probed = []

        def fake_check(hosts, timeout=3):
            probed.extend(hosts)
            return [{'hostname': h['hostname'], 'mgmt_ip': h['mgmt_ip'],
                     'ssh': {'ok': True, 'ms': 1, 'error': None}, 'https': None}
                    for h in hosts]
        monkeypatch.setattr(reach, 'check', fake_check)
        return probed

    def test_named_switches_only(self, auth_client, mapped):
        res = auth_client.post('/projects/admin/p/reachability', json={'hosts': ['core-02']})
        assert res.status_code == 200
        assert [h['hostname'] for h in mapped] == ['core-02']

    def test_empty_means_all_valid_mapped(self, auth_client, mapped):
        auth_client.post('/projects/admin/p/reachability', json={})
        assert [h['hostname'] for h in mapped] == ['core-01', 'core-02']

    def test_addresses_come_from_the_mapping_not_the_request(self, auth_client, mapped):
        auth_client.post('/projects/admin/p/reachability',
                         json={'hosts': ['core-01'], 'mgmt_ip': '8.8.8.8',
                               'addresses': {'core-01': '8.8.8.8'}})
        assert [h['mgmt_ip'] for h in mapped] == ['10.1.0.1']

    def test_unknown_switch_is_not_probed(self, auth_client, mapped):
        auth_client.post('/projects/admin/p/reachability', json={'hosts': ['8.8.8.8']})
        assert mapped == []

    def test_hosts_must_be_a_list(self, auth_client, mapped):
        res = auth_client.post('/projects/admin/p/reachability', json={'hosts': 'core-01'})
        assert res.status_code == 400

    def test_requires_login(self, client):
        assert client.post('/projects/admin/p/reachability', json={}).status_code in (302, 401)


class TestPages:

    @pytest.fixture
    def proj(self, app, auth_client, monkeypatch):
        import app.deploy_routes as dr
        import app.firmware_routes as fr
        from app.project import create_project
        with app.app_context():
            create_project(app, 'admin', 'rp')
        monkeypatch.setattr(dr, '_load_mapping', lambda a, u, p: {'hosts': [], 'settings': {}})
        monkeypatch.setattr(fr, '_load_mapping', lambda a, u, p: {'hosts': []})
        return 'rp'

    @pytest.mark.parametrize('page', ['deploy', 'firmware'])
    def test_button_and_results_area(self, auth_client, proj, page):
        body = auth_client.get('/projects/admin/%s/%s' % (proj, page)).data.decode()
        assert 'Check reachability' in body
        assert 'id="reach-results"' in body
        assert 'function checkReachability(' in body

    def test_results_are_escaped(self):
        with open('app/templates/_reachability.html') as f:
            src = f.read()
        for field in ('r.hostname', "r.mgmt_ip || '—'", 'r.error', 'r.ms'):
            assert 'esc(%s)' % field in src, field
