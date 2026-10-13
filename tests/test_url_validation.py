"""
Names arrive from the URL and are joined into filesystem paths. Flask's
default converter accepts '..' as a whole segment, so before this check
POST /projects/%2E%2E/delete ran shutil.rmtree on the user's entire
directory and deleted every project they owned.
"""

import os

import pytest


@pytest.fixture
def two_projects(app, auth_client):
    from app.project import create_project, projects_dir
    with app.app_context():
        create_project(app, 'admin', 'real')
        create_project(app, 'admin', 'other')
        root = projects_dir(app, 'admin')
    return root


class TestTraversalIsRefused:

    def test_deleting_dotdot_project_deletes_nothing(self, auth_client, two_projects):
        res = auth_client.post('/projects/%2E%2E/delete')
        assert res.status_code == 404
        assert sorted(os.listdir(two_projects)) == ['other', 'real'], \
            'projects were deleted through a .. segment'

    def test_deleting_dotdot_host_leaves_the_project(self, app, auth_client,
                                                      two_projects):
        from app.project import project_dir
        res = auth_client.post('/projects/real/hosts/delete/%2E%2E')
        assert res.status_code == 404
        with app.app_context():
            assert os.path.isdir(project_dir(app, 'admin', 'real'))

    @pytest.mark.parametrize('path', [
        '/projects/%2E%2E/editor',
        '/projects/real/api/hostvars/%2E%2E/state',
        '/projects/real/generate/download/%2E%2E',
        '/projects/real/generate/download/.hidden',
        '/projects/real/api/pools/%2E%2E/carved',
        '/projects/real/deploy/status/%2E%2E',
    ])
    def test_every_parameter_is_checked(self, auth_client, two_projects, path):
        assert auth_client.get(path).status_code == 404


class TestOrdinaryNamesStillWork:

    def test_normal_project_pages_load(self, auth_client, two_projects):
        for page in ('hosts', 'resources', 'generate', 'deploy'):
            res = auth_client.get('/projects/real/%s' % page)
            assert res.status_code == 200, page

    def test_normal_project_can_still_be_deleted(self, auth_client, two_projects):
        res = auth_client.post('/projects/real/delete', follow_redirects=False)
        assert res.status_code in (200, 302)
        assert os.listdir(two_projects) == ['other']

    def test_realistic_config_filename_is_accepted(self):
        from app.projects import _SAFE_URL_VALUES
        assert _SAFE_URL_VALUES['filename'].match('CTY-01-Core-01_FULL.ios')
        assert _SAFE_URL_VALUES['filename'].match('sw1_PARTIAL_aaa-vlans.ios')
