"""Every project page must render the project sidebar nav."""
import pytest

PAGES = [
    ('/projects/{p}/hosts',     'hosts'),
    ('/projects/{p}/editor',    'editor'),
    ('/projects/{p}/resources', 'resources'),
    ('/projects/{p}/generate',  'generate'),
    ('/projects/{p}/deploy',    'deploy'),
    ('/projects/{p}/firmware',  'firmware'),
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
