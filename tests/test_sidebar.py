"""Every project page must render the project sidebar nav."""
import pytest

PAGES = [
    ('/projects/admin/{p}/hosts',     'hosts'),
    ('/projects/admin/{p}/editor',    'editor'),
    ('/projects/admin/{p}/resources', 'resources'),
    ('/projects/admin/{p}/generate',  'generate'),
    ('/projects/admin/{p}/deployment-ips', 'deployment ips'),
    ('/projects/admin/{p}/deploy',    'deploy'),
    ('/projects/admin/{p}/firmware',  'firmware'),
    ('/projects/admin/{p}/history',   'history'),
]

@pytest.fixture
def proj(app, auth_client, monkeypatch):
    from app.project import create_project
    with app.app_context():
        create_project(app, 'admin', 'sbproj')
    import app.deploy_routes as dr, app.firmware_routes as fr
    monkeypatch.setattr(dr, '_load_mapping', lambda a,u,p: {'hosts':[], 'settings':{}})
    monkeypatch.setattr(fr, '_load_mapping', lambda a,u,p: {'hosts':[]})
    return 'sbproj'

@pytest.mark.parametrize('path,label', PAGES)
def test_sidebar_present(auth_client, proj, path, label):
    res = auth_client.get(path.format(p=proj))
    assert res.status_code == 200, (label, res.status_code)
    body = res.data.decode()
    # the project sidebar is injected by base_project's extra_head block
    assert 'project-ctx' in body, f'{label}: project sidebar missing'
    assert '/firmware' in body,   f'{label}: nav items missing'


# ---------------------------------------------------------------------------
# Deployment IPs — the single place management addresses are edited
# ---------------------------------------------------------------------------

class TestDeploymentIps:

    @pytest.fixture
    def proj2(self, app, auth_client):
        from app.project import create_project
        with app.app_context():
            create_project(app, 'admin', 'ipproj')
        return 'ipproj'

    def test_page_renders(self, auth_client, proj2):
        res = auth_client.get('/projects/admin/%s/deployment-ips' % proj2)
        assert res.status_code == 200
        assert b'Host &#39;' not in res.data          # no broken escaping
        assert b'Management IP' in res.data

    def test_save_and_reload_round_trip(self, app, auth_client, proj2):
        res = auth_client.post('/projects/admin/%s/deployment-ips/save' % proj2, json={
            'mappings': [{'hostname': 'core-01', 'mgmt_ip': '10.0.0.1'},
                         {'hostname': 'core-02', 'mgmt_ip': ''}]})
        assert res.get_json() == {'ok': True, 'count': 2}

        page = auth_client.get('/projects/admin/%s/deployment-ips' % proj2).data.decode()
        assert 'core-01' in page and '10.0.0.1' in page

    def test_blank_hostnames_are_dropped(self, auth_client, proj2):
        res = auth_client.post('/projects/admin/%s/deployment-ips/save' % proj2, json={
            'mappings': [{'hostname': '', 'mgmt_ip': '10.0.0.9'},
                         {'hostname': 'real', 'mgmt_ip': '10.0.0.1'}]})
        assert res.get_json()['count'] == 1

    def test_duplicate_hostnames_are_collapsed(self, auth_client, proj2):
        res = auth_client.post('/projects/admin/%s/deployment-ips/save' % proj2, json={
            'mappings': [{'hostname': 'dup', 'mgmt_ip': '10.0.0.1'},
                         {'hostname': 'dup', 'mgmt_ip': '10.0.0.2'}]})
        assert res.get_json()['count'] == 1

    def test_save_preserves_other_settings(self, app, auth_client, proj2):
        """Rollback timeout lives in the same file and must survive."""
        from app.deploy_routes import _load_mapping, _save_mapping
        with app.app_context():
            _save_mapping(app, 'admin', proj2,
                          {'hosts': [], 'settings': {'rollback_timeout': 17}})
        auth_client.post('/projects/admin/%s/deployment-ips/save' % proj2, json={
            'mappings': [{'hostname': 'core-01', 'mgmt_ip': '10.0.0.1'}]})
        with app.app_context():
            m = _load_mapping(app, 'admin', proj2)
        assert m['settings']['rollback_timeout'] == 17
        assert m['hosts'][0]['hostname'] == 'core-01'

    def test_requires_login(self, client):
        assert client.get('/projects/admin/p/deployment-ips').status_code == 401
        assert client.post('/projects/admin/p/deployment-ips/save',
                           json={'mappings': []}).status_code == 401
