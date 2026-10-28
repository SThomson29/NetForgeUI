"""
Change summaries built from a dry run's audit entry.
"""

import os

import pytest
import yaml

from app import audit
from app.models import User
from app.project import create_project, project_dir
from tests.test_regenerate_and_push_state import (  # noqa: F401
    P, gcdir, generate, project)


@pytest.fixture
def mapped(project):
    with open(os.path.join(project_dir(project, 'admin', P), 'deploy_mapping.yml'), 'w') as f:
        yaml.safe_dump({'hosts': [{'hostname': h, 'mgmt_ip': '10.0.0.%d' % i}
                                  for i, h in enumerate(('SW1', 'SW2', 'SW3'), 1)],
                        'settings': {'rollback_timeout': 7}}, f)
    for h in ('SW1', 'SW2', 'SW3'):
        generate(project, h, content='config for %s v1\n' % h)
    return project


def dry_run(app, results, hosts=('SW1', 'SW2', 'SW3'), project=P, owner='admin'):
    targets = audit.deploy_targets(
        [{'hostname': h, 'mgmt_ip': '10.0.0.%d' % i} for i, h in enumerate(hosts, 1)],
        gcdir(app))
    entry = audit.record_start(app, username='admin', owner=owner, project=project,
                               action='deploy_dryrun', switch_user='netops',
                               targets=targets, params={})
    audit.record_finish(app, entry, {'status': 'done', 'returncode': 0, 'results': results})
    return entry


RESULTS = [
    {'hostname': 'SW1', 'changed': True,
     'commands': ['vlan 30', '    name PRINTERS', 'radius-server host 1.1.1.1 key plaintext S3cret']},
    {'hostname': 'SW2', 'changed': False, 'commands': []},
]


def url(entry, **q):
    from urllib.parse import urlencode
    return '/projects/admin/%s/history/%d/summary' % (P, entry) + ('?' + urlencode(q) if q else '')


class TestSummary:

    def test_lists_commands_with_secrets_masked(self, mapped, auth_client):
        body = auth_client.get(url(dry_run(mapped, RESULTS))).get_data(as_text=True)
        assert 'vlan 30' in body and 'name PRINTERS' in body
        assert 'S3cret' not in body and audit.MASK in body
        assert '3, 1 with changes' in body
        assert 'within 7 minutes' in body
        assert '<strong>No changes:</strong> SW2' in body
        assert 'The dry run returned no result for this switch.' in body     # SW3

    def test_switch_with_changes_comes_first(self, mapped, auth_client):
        body = auth_client.get(url(dry_run(mapped, RESULTS))).get_data(as_text=True)
        assert body.index('<strong>SW1</strong>') < body.index('<strong>SW3</strong>')

    def test_reference_and_notes_are_shown_and_escaped(self, mapped, auth_client):
        e = dry_run(mapped, RESULTS)
        body = auth_client.get(url(e, ref='CHG001', notes='<b>Sat 02:00</b>')).get_data(as_text=True)
        assert 'CHG001' in body and '&lt;b&gt;Sat 02:00&lt;/b&gt;' in body
        assert '<b>Sat' not in body

    def test_regenerated_since_the_dry_run_is_flagged(self, mapped, auth_client):
        e = dry_run(mapped, RESULTS)
        assert 'regenerated since this dry run' not in \
            auth_client.get(url(e)).get_data(as_text=True)
        generate(mapped, 'SW1', content='config for SW1 v2\n')
        body = auth_client.get(url(e)).get_data(as_text=True)
        assert 'Configs regenerated since this dry run:</strong> SW1.' in body

    def test_dropped_commands_are_explained(self, mapped, auth_client):
        e = dry_run(mapped, [{'hostname': 'SW1', 'changed': True}])
        body = auth_client.get(url(e)).get_data(as_text=True)
        assert 'audit log drops them after' in body

    def test_text_download(self, mapped, auth_client):
        e = dry_run(mapped, RESULTS)
        res = auth_client.get(url(e, format='text', ref='CHG001', notes='Sat 02:00'))
        assert res.mimetype == 'text/plain'
        assert 'attachment; filename="change-summary-site-%d.txt"' % e == \
            res.headers['Content-Disposition']
        text = res.get_data(as_text=True)
        assert text.startswith('CHANGE SUMMARY: admin / site')
        assert 'Change reference: CHG001' in text and 'Sat 02:00' in text
        assert '  vlan 30' in text and 'S3cret' not in text
        assert 'within 7 minutes' in text

    def test_text_flags_regenerated(self, mapped, auth_client):
        e = dry_run(mapped, RESULTS)
        generate(mapped, 'SW2', content='new\n')
        text = auth_client.get(url(e, format='text')).get_data(as_text=True)
        assert 'WARNING: configs regenerated since this dry run: SW2.' in text

    @pytest.mark.parametrize('kind', ['push', 'other project', 'missing'])
    def test_only_this_projects_dry_runs(self, mapped, auth_client, kind):
        if kind == 'push':
            e = audit.record_start(mapped, username='admin', owner='admin', project=P,
                                   action='deploy_push', switch_user='x', targets=[], params={})
        elif kind == 'other project':
            create_project(mapped, 'admin', 'other')
            e = dry_run(mapped, RESULTS, project='other')
        else:
            e = 999
        assert auth_client.get(url(e)).status_code == 404

    def test_view_access_is_enough(self, mapped, auth_client):
        from app import sharing
        e = dry_run(mapped, RESULTS)
        User.create('viewer', 'password1', mapped)
        sharing.set_share(mapped, 'admin', P, 'user', 'viewer', sharing.VIEW, 'admin')
        c = mapped.test_client()
        c.post('/login', data={'username': 'viewer', 'password': 'password1'})
        assert c.get(url(e)).status_code == 200

    def test_linked_from_history(self, mapped, auth_client):
        e = dry_run(mapped, RESULTS)
        body = auth_client.get('/projects/admin/%s/history' % P).get_data(as_text=True)
        assert url(e) in body and 'Change summary' in body

    def test_deploy_page_links_after_a_dry_run(self, mapped, auth_client):
        body = auth_client.get('/projects/admin/%s/deploy' % P).get_data(as_text=True)
        assert '/summary`' in body and "link.textContent = 'Change summary'" in body
