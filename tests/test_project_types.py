"""
Project types: config (every project before types existed) and central.

The type is chosen at creation, kept in config.json, and decides which
pages a project has. Existing projects carry no type and must be read as
config projects without anything being written to them.
"""
import json
import os

import pytest

from app import project_types
from app.project import (create_project, delete_project, list_projects,
                         project_dir, project_summary)

CONFIG_PAGES = ['hosts', 'editor', 'files', 'resources', 'generate', 'checks',
                'deployment-ips', 'deploy', 'firmware', 'verify', 'status', 'export']
COMMON_PAGES = ['history', 'sharing']


def _config_json(app, name, owner='admin'):
    return os.path.join(project_dir(app, owner, name), 'config.json')


@pytest.fixture
def central_on(app):
    app.config['CENTRAL_REPO_URL'] = 'git@example.com:someone/new-central.git'
    return app


@pytest.fixture
def config_proj(app, auth_client, monkeypatch):
    create_project(app, 'admin', 'cfgproj')
    import app.deploy_routes as dr
    import app.firmware_routes as fr
    monkeypatch.setattr(dr, '_load_mapping', lambda a, u, p: {'hosts': [], 'settings': {}})
    monkeypatch.setattr(fr, '_load_mapping', lambda a, u, p: {'hosts': []})
    return 'cfgproj'


@pytest.fixture
def central_proj(app, auth_client):
    create_project(app, 'admin', 'cenproj', project_types.CENTRAL)
    return 'cenproj'


# ---------------------------------------------------------------------------
# What a project's type is
# ---------------------------------------------------------------------------

class TestTypeOf:

    def test_new_config_project_is_config(self, app):
        create_project(app, 'admin', 'p1')
        assert project_types.of(app, 'admin', 'p1') == project_types.CONFIG

    def test_config_project_records_no_type(self, app):
        """A config project's files are what they were before types, so an
        export still imports into an older NetForgeUI."""
        create_project(app, 'admin', 'p1')
        with open(_config_json(app, 'p1')) as f:
            assert 'type' not in json.load(f)

    def test_project_from_before_types_is_config(self, app):
        pdir = project_dir(app, 'admin', 'old')
        os.makedirs(pdir)
        with open(os.path.join(pdir, 'config.json'), 'w') as f:
            json.dump({'name': 'old', 'created': '2025-01-01', 'pools': []}, f)
        assert project_types.of(app, 'admin', 'old') == project_types.CONFIG

    def test_missing_or_unreadable_config_is_config(self, app):
        assert project_types.of(app, 'admin', 'nothing-here') == project_types.CONFIG
        pdir = project_dir(app, 'admin', 'broken')
        os.makedirs(pdir)
        with open(os.path.join(pdir, 'config.json'), 'w') as f:
            f.write('{not json')
        assert project_types.of(app, 'admin', 'broken') == project_types.CONFIG

    def test_central_project_records_its_type(self, app):
        create_project(app, 'admin', 'c1', project_types.CENTRAL)
        assert project_types.of(app, 'admin', 'c1') == project_types.CENTRAL
        with open(_config_json(app, 'c1')) as f:
            assert json.load(f)['type'] == 'central'

    def test_central_project_has_no_switch_files(self, app):
        create_project(app, 'admin', 'c1', project_types.CENTRAL)
        assert sorted(os.listdir(project_dir(app, 'admin', 'c1'))) == ['config.json']

    def test_unknown_type_is_refused(self, app):
        with pytest.raises(ValueError):
            create_project(app, 'admin', 'x1', 'wireless')
        assert not os.path.isdir(project_dir(app, 'admin', 'x1'))

    def test_summary_carries_type(self, app):
        create_project(app, 'admin', 'p1')
        create_project(app, 'admin', 'c1', project_types.CENTRAL)
        assert project_summary(app, 'admin', 'p1')['type'] == 'config'
        central = project_summary(app, 'admin', 'c1')
        assert (central['type'], central['type_label']) == ('central', 'Central')
        assert (central['host_count'], central['config_count']) == (0, 0)
        assert {p['name']: p['type'] for p in list_projects(app, 'admin')} == {
            'p1': 'config', 'c1': 'central'}


# ---------------------------------------------------------------------------
# Existing projects are left alone
# ---------------------------------------------------------------------------

class TestNothingIsRewritten:

    def _snapshot(self, root):
        out = {}
        for folder, _dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(folder, name)
                with open(path, 'rb') as f:
                    out[os.path.relpath(path, root)] = f.read()
        return out

    def test_opening_a_project_from_before_types_changes_no_file(self, app, auth_client, config_proj):
        pdir = project_dir(app, 'admin', config_proj)
        # As an older version left it: no type anywhere.
        with open(_config_json(app, config_proj)) as f:
            assert 'type' not in json.load(f)
        before = self._snapshot(pdir)
        assert auth_client.get('/projects').status_code == 200
        for page in ('hosts', 'history', 'sharing', 'generate', 'resources'):
            assert auth_client.get('/projects/admin/%s/%s' % (config_proj, page)).status_code == 200
        assert self._snapshot(pdir) == before


# ---------------------------------------------------------------------------
# Which pages a project has
# ---------------------------------------------------------------------------

class TestPagesByType:

    @pytest.mark.parametrize('page', CONFIG_PAGES)
    def test_central_project_has_no_config_pages(self, auth_client, central_proj, page):
        assert auth_client.get('/projects/admin/%s/%s' % (central_proj, page)).status_code == 404

    def test_central_project_refuses_config_writes(self, app, auth_client, central_proj):
        res = auth_client.post('/projects/admin/%s/hosts/add' % central_proj,
                               data={'hostname': 'sw1', 'stacking': 'none'})
        assert res.status_code == 404
        assert sorted(os.listdir(project_dir(app, 'admin', central_proj))) == ['config.json']

    @pytest.mark.parametrize('page', COMMON_PAGES + ['central'])
    def test_central_project_pages(self, auth_client, central_proj, page):
        res = auth_client.get('/projects/admin/%s/%s' % (central_proj, page))
        assert res.status_code == 200, page

    def test_config_project_has_no_central_page(self, auth_client, config_proj):
        assert auth_client.get('/projects/admin/%s/central' % config_proj).status_code == 404

    @pytest.mark.parametrize('page', ['hosts', 'generate', 'deploy', 'firmware'] + COMMON_PAGES)
    def test_config_project_pages_unchanged(self, auth_client, config_proj, page):
        assert auth_client.get('/projects/admin/%s/%s' % (config_proj, page)).status_code == 200

    def test_type_this_version_does_not_know_keeps_only_common_pages(self, app, auth_client, central_proj):
        with open(_config_json(app, central_proj), 'w') as f:
            json.dump({'name': central_proj, 'type': 'something-newer'}, f)
        base = '/projects/admin/%s/' % central_proj
        assert auth_client.get(base + 'history').status_code == 200
        assert auth_client.get(base + 'central').status_code == 404
        assert auth_client.get(base + 'hosts').status_code == 404

    def test_listed_endpoints_exist(self, app):
        """A page renamed without updating project_types would silently
        become a config page."""
        known = {rule.endpoint for rule in app.url_map.iter_rules()}
        listed = (project_types.COMMON_ENDPOINTS | project_types.CENTRAL_ENDPOINTS
                  | set(project_types.HOME_ENDPOINTS.values()))
        assert listed <= known, listed - known

    def test_central_project_can_be_deleted(self, app, auth_client, central_proj):
        res = auth_client.post('/projects/admin/%s/delete' % central_proj)
        assert res.status_code == 302
        assert not os.path.isdir(project_dir(app, 'admin', central_proj))


# ---------------------------------------------------------------------------
# Navigation and the projects list
# ---------------------------------------------------------------------------

class TestNavigation:

    def test_central_sidebar_has_its_own_pages_only(self, auth_client, central_proj):
        body = auth_client.get('/projects/admin/%s/central' % central_proj).data.decode()
        assert 'project-ctx' in body
        assert '${PROJECT_BASE}/central' in body
        assert '${PROJECT_BASE}/history' in body and '${PROJECT_BASE}/sharing' in body
        for page in ('hosts', 'editor', 'generate', 'deploy', 'firmware', 'verify'):
            assert '${PROJECT_BASE}/%s"' % page not in body, page

    def test_config_sidebar_is_as_before(self, auth_client, config_proj):
        body = auth_client.get('/projects/admin/%s/hosts' % config_proj).data.decode()
        for page in ('hosts', 'editor', 'files', 'resources', 'generate', 'checks',
                     'deployment-ips', 'deploy', 'firmware', 'verify', 'history', 'sharing'):
            assert '${PROJECT_BASE}/%s"' % page in body, page
        assert '${PROJECT_BASE}/central' not in body
        assert 'Active project' in body

    def test_breadcrumb_opens_the_projects_own_home(self, auth_client, central_proj, config_proj):
        central = auth_client.get('/projects/admin/%s/history' % central_proj).data.decode()
        assert 'href="/projects/admin/%s/central"' % central_proj in central
        config = auth_client.get('/projects/admin/%s/history' % config_proj).data.decode()
        assert 'href="/projects/admin/%s/hosts"' % config_proj in config

    def test_projects_list(self, auth_client, central_proj, config_proj):
        body = auth_client.get('/projects').data.decode()
        assert 'href="/projects/admin/%s/central"' % central_proj in body
        assert 'href="/projects/admin/%s/hosts"' % config_proj in body
        # Status badges and export are fetched for the config project only.
        assert '/projects/admin/%s/status' % config_proj in body
        assert '/projects/admin/%s/status' % central_proj not in body
        assert '/projects/admin/%s/export' % central_proj not in body


# ---------------------------------------------------------------------------
# Creating a project
# ---------------------------------------------------------------------------

class TestCreating:

    def test_no_type_choice_without_central(self, auth_client):
        body = auth_client.get('/projects').data.decode()
        assert 'name="type"' not in body

    def test_type_choice_with_central(self, central_on, auth_client):
        body = auth_client.get('/projects').data.decode()
        assert 'name="type" value="config" checked' in body
        assert 'name="type" value="central"' in body

    def test_form_without_type_makes_a_config_project(self, app, auth_client):
        auth_client.post('/projects/new', data={'name': 'plain'})
        assert project_types.of(app, 'admin', 'plain') == project_types.CONFIG
        assert os.path.isfile(os.path.join(project_dir(app, 'admin', 'plain'), 'hosts.ini'))

    def test_central_refused_where_not_configured(self, app, auth_client):
        auth_client.post('/projects/new', data={'name': 'nope', 'type': 'central'})
        assert not os.path.isdir(project_dir(app, 'admin', 'nope'))

    def test_central_created_where_configured(self, central_on, auth_client):
        auth_client.post('/projects/new', data={'name': 'tenant-a', 'type': 'central'})
        assert project_types.of(central_on, 'admin', 'tenant-a') == project_types.CENTRAL

    def test_unknown_type_refused(self, central_on, auth_client):
        auth_client.post('/projects/new', data={'name': 'odd', 'type': 'wireless'})
        assert not os.path.isdir(project_dir(central_on, 'admin', 'odd'))

    def test_existing_central_project_still_opens_if_central_is_turned_off(self, app, auth_client, central_proj):
        assert not project_types.central_available(app)
        res = auth_client.get('/projects/admin/%s/central' % central_proj)
        assert res.status_code == 200
        assert b'CENTRAL_REPO_URL' in res.data


# ---------------------------------------------------------------------------
# Features that read switches skip other types
# ---------------------------------------------------------------------------

class TestConfigOnlyFeatures:

    def test_search_skips_central_projects(self, app, auth_client, central_proj, config_proj):
        from app.models import User
        from app.search import projects_to_search
        user = User.get_by_username('admin', app)
        assert [(o, p) for o, p, _lv in projects_to_search(app, user)] == [('admin', config_proj)]
        assert projects_to_search(app, user, only=('admin', central_proj)) == []
        assert auth_client.get('/search?q=10.0.0.1').status_code == 200

    def test_admin_skeleton_check_skips_central_projects(self, app, central_proj, monkeypatch):
        import app.utils as utils
        from app.admin import _projects_missing_skeleton_files
        seen = []

        def fake(app_, owner, project, dry_run=True):
            seen.append(project)
            return {'total': 0, 'hosts': []}

        monkeypatch.setattr(utils, 'reconcile_host_vars', fake)
        create_project(app, 'admin', 'cfg2')
        _projects_missing_skeleton_files(app)
        assert seen == ['cfg2']

    def test_delete_helper_removes_a_central_project(self, app, central_proj):
        delete_project(app, 'admin', central_proj)
        assert not os.path.isdir(project_dir(app, 'admin', central_proj))
