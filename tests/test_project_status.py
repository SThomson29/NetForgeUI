"""
Status badges on the projects list: configs out of date, changes not yet
pushed, and the last push.
"""

import os

import pytest
import yaml

from app import audit
from app.models import User
from app.project import project_dir
from tests.test_regenerate_and_push_state import (  # noqa: F401
    P, change, gcdir, generate, project)


@pytest.fixture
def mapped(project):
    with open(os.path.join(project_dir(project, 'admin', P), 'deploy_mapping.yml'), 'w') as f:
        yaml.safe_dump({'hosts': [{'hostname': h, 'mgmt_ip': '10.0.0.%d' % i}
                                  for i, h in enumerate(('SW1', 'SW2', 'SW3'), 1)]}, f)
    for h in ('SW1', 'SW2', 'SW3'):
        generate(project, h, content='config for %s v1\n' % h)
    return project


class TestStatus:

    def status(self, client):
        return client.get('/projects/admin/%s/status' % P).get_json()

    def test_fresh_project(self, mapped, auth_client):
        s = self.status(auth_client)
        assert (s['out_of_date'], s['changed'], s['never'], s['last_push']) == (0, 0, 3, None)

    def test_after_pushes_and_edits(self, mapped, auth_client):
        targets = audit.deploy_targets([{'hostname': h} for h in ('SW1', 'SW2')], gcdir(mapped))
        e = audit.record_start(mapped, username='sam', owner='admin', project=P,
                               action='deploy_push', switch_user='x', targets=targets, params={})
        audit.record_finish(mapped, e, {'status': 'done', 'returncode': 0, 'results': [
            {'hostname': 'SW1', 'confirmed': True}, {'hostname': 'SW2', 'confirmed': True}]})
        generate(mapped, 'SW2', content='config for SW2 v2\n')
        change(mapped, 'SW3')
        s = self.status(auth_client)
        assert (s['out_of_date'], s['changed'], s['never']) == (1, 1, 1)
        assert s['last_push']['by'] == 'sam'

    def test_projects_page_has_status_hooks(self, mapped, auth_client):
        body = auth_client.get('/projects').get_data(as_text=True)
        assert 'data-status-url="/projects/admin/%s/status"' % P in body
        assert 'data-deploy-url="/projects/admin/%s/deploy"' % P in body

    def test_needs_access(self, mapped):
        User.create('nobody', 'password1', mapped)
        c = mapped.test_client()
        c.post('/login', data={'username': 'nobody', 'password': 'password1'})
        assert c.get('/projects/admin/%s/status' % P).status_code == 404
