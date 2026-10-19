"""
API input handling and the health check.

API routes read fields straight off request.json, so a missing body, the
wrong content type or a missing field came back as a 500 HTML page. The
editor shows the server's error text in a toast, so they now answer 400
with a JSON reason.
"""

import pytest


@pytest.fixture
def proj(app, auth_client):
    from app.project import create_project, add_pool
    with app.app_context():
        create_project(app, 'admin', 'api')
        add_pool(app, 'admin', 'api', {
            'id': 'lb', 'type': 'unique', 'name': 'lo',
            'prefix': '32', 'subnet': '10.9.0.0/24'})
        add_pool(app, 'admin', 'api', {
            'id': 'pp', 'type': 'point_to_point', 'name': 'p2p',
            'prefix': '31', 'subnet': '10.8.0.0/24'})
    return '/projects/admin/api'


ROUTES = [
    ('POST', '/api/allocations/unique'),
    ('POST', '/api/allocations/unique/release'),
    ('POST', '/api/allocations/ptp'),
    ('POST', '/api/allocations/ptp/release'),
    ('POST', '/api/allocations/vlan'),
    ('POST', '/api/allocations/vlan/release'),
    ('POST', '/api/common'),
    ('POST', '/api/conventions'),
    ('POST', '/api/pools'),
    ('POST', '/api/pools/lb/blocks'),
    ('DELETE', '/api/pools/lb/blocks'),
    ('PATCH', '/api/pools/lb'),
    ('POST', '/api/hostvars/sw1/save_all'),
    ('POST', '/generate/run'),
    ('POST', '/deploy/dryrun'),
    ('POST', '/deploy/push'),
    ('POST', '/deploy/save-mapping'),
    ('POST', '/deployment-ips/save'),
    ('POST', '/firmware/run'),
]


def _send(client, method, url, **kw):
    return client.open(url, method=method, **kw)


class TestMalformedBodies:

    @pytest.mark.parametrize('method,path', ROUTES)
    def test_no_body_is_a_json_400(self, auth_client, proj, method, path):
        res = _send(auth_client, method, proj + path)
        assert res.status_code == 400, res.data[:200]
        assert res.get_json()['ok'] is False

    @pytest.mark.parametrize('method,path', ROUTES)
    def test_wrong_content_type_is_a_json_400(self, auth_client, proj, method, path):
        res = _send(auth_client, method, proj + path, data='pool_id=lb',
                    content_type='application/x-www-form-urlencoded')
        assert res.status_code == 400, res.data[:200]
        assert res.get_json()['ok'] is False

    @pytest.mark.parametrize('method,path', ROUTES)
    def test_wrong_json_type_is_a_json_400(self, auth_client, proj, method, path):
        body = [] if 'save_all' not in path else {'filename': 'x'}
        res = _send(auth_client, method, proj + path, json=body)
        assert res.status_code == 400, res.data[:200]
        assert res.get_json()['ok'] is False


class TestMissingFields:

    @pytest.mark.parametrize('path,body,missing', [
        ('/api/allocations/unique', {'pool_id': 'lb', 'ip': '10.9.0.1'}, 'hostname'),
        ('/api/allocations/ptp', {'pool_id': 'pp', 'hostname': 'a', 'interface': 'x'}, 'ip'),
        ('/api/allocations/unique/release', {'ip': '10.9.0.1'}, 'pool_id'),
        ('/api/allocations/vlan', {'pool_id': 'x', 'subnet': '10.0.0.0/24'}, 'vlan_id'),
    ])
    def test_names_the_missing_field(self, auth_client, proj, path, body, missing):
        res = auth_client.post(proj + path, json=body)
        assert res.status_code == 400
        assert missing in res.get_json()['error']

    def test_bad_ptp_address_is_a_400(self, auth_client, proj):
        res = auth_client.post(proj + '/api/allocations/ptp', json={
            'pool_id': 'pp', 'ip': 'not-an-ip', 'hostname': 'a', 'interface': 'x'})
        assert res.status_code == 400

    def test_save_all_reports_a_bad_item(self, auth_client, proj):
        res = auth_client.post(proj + '/api/hostvars/sw1/save_all',
                               json=['not an object', {'content': 'x'}])
        assert res.status_code == 400
        errors = res.get_json()['errors']
        assert any('object' in e for e in errors)
        assert any('filename' in e for e in errors)

    def test_valid_requests_still_work(self, auth_client, proj):
        assert auth_client.post(proj + '/api/allocations/unique', json={
            'pool_id': 'lb', 'ip': '10.9.0.1', 'hostname': 'a',
            'interface': 'lo0'}).status_code == 200
        assert auth_client.post(proj + '/api/allocations/ptp', json={
            'pool_id': 'pp', 'ip': '10.8.0.0', 'hostname': 'a',
            'interface': '1/1/1'}).get_json()['peer_ip'] == '10.8.0.1'
        assert auth_client.post(proj + '/api/common', json={
            'dns_servers': ['1.1.1.1']}).status_code == 200


class TestFirmwareHostSelection:

    def test_string_hosts_is_refused(self, app, auth_client, tmp_path):
        """A string would be matched by substring: 'core' picking core-01."""
        fw = tmp_path / 'fw'
        fw.mkdir()
        (fw / 'img.swi').write_text('x')
        app.config['FIRMWARE_DIR'] = str(fw)
        res = auth_client.post('/projects/admin/p/firmware/run', json={
            'image': 'img.swi', 'hosts': 'core',
            'switch_username': 'u', 'switch_password': 'p'})
        assert res.status_code == 400
        assert 'list' in res.get_json()['error']


class TestHealthz:

    def test_ok_without_login(self, client):
        res = client.get('/healthz')
        assert res.status_code == 200
        assert res.get_json() == {'ok': True}

    def test_unhealthy_when_the_database_is_unreadable(self, app, client, tmp_path):
        app.config['DATABASE'] = str(tmp_path / 'missing' / 'users.db')
        res = client.get('/healthz')
        assert res.status_code == 503

    def test_dockerfile_probes_it(self):
        import os
        with open(os.path.join(os.path.dirname(__file__), '..', 'Dockerfile')) as f:
            src = f.read()
        assert 'HEALTHCHECK' in src and '/healthz' in src


class TestCiGatesRelease:

    def _wf(self, name):
        import os
        import yaml
        with open(os.path.join(os.path.dirname(__file__), '..', '.github',
                               'workflows', name)) as f:
            return yaml.safe_load(f)

    # Tests run once per release, on the PR into main, which tests the
    # merged result; tags are cut from main afterwards, so the image build
    # doesn't repeat them.

    def test_tests_run_on_prs_into_main(self):
        # PyYAML reads the bare key `on` as True.
        on = self._wf('test.yml')[True]
        assert on['pull_request']['branches'] == ['main']
        assert 'workflow_dispatch' in on
        assert 'push' not in on

    def test_tests_are_cached_and_superseded_runs_cancelled(self):
        wf = self._wf('test.yml')
        assert wf['concurrency']['cancel-in-progress'] is True
        python = next(s for s in wf['jobs']['test']['steps']
                      if s.get('uses', '').startswith('actions/setup-python'))
        assert python['with']['cache'] == 'pip'

    def test_release_builds_only_from_version_tags(self):
        wf = self._wf('release.yml')
        assert wf[True] == {'push': {'tags': ['v*.*.*']}}
        assert list(wf['jobs']) == ['build-and-push']
