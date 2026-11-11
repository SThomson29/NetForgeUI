"""
Which NetForge made each config: the version label, the configs flagged as
generated with older templates, and where that shows.
"""

import os
import subprocess

import pytest

from app import audit, checks, repo_version
from app.project import (create_project, get_project_config,
                         project_generated_configs_dir, project_host_vars_dir,
                         record_generated_from)
from app.utils import add_project_hosts, ensure_workspace

P = 'site'
OLD = {'commit': 'a' * 40, 'label': 'v1.4.0 (aaaaaaa)'}
NEW = {'commit': 'b' * 40, 'label': 'v1.4.1 (bbbbbbb)'}


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    return subprocess.run(['git', '-C', str(repo)] + list(args), check=True,
                          capture_output=True, text=True, env=env).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / 'netforge'
    path.mkdir()
    git(path, 'init', '-q')
    git(path, 'commit', '-q', '--allow-empty', '-m', 'one')
    return path


class TestLabel:

    def test_not_a_checkout(self, tmp_path):
        assert repo_version.read(str(tmp_path)) is None
        assert repo_version.read('') is None

    def test_no_tags_is_the_short_commit(self, repo):
        v = repo_version.read(str(repo))
        assert v['commit'] == git(repo, 'rev-parse', 'HEAD')
        assert v['label'] == v['commit'][:7]

    def test_tag_on_the_commit(self, repo):
        git(repo, 'tag', 'v1.4.1')
        v = repo_version.read(str(repo))
        assert v['label'] == 'v1.4.1 (%s)' % v['commit'][:7]

    def test_highest_of_several_tags_on_one_commit(self, repo):
        for tag in ('v1.3.1', 'v1.10.0', 'v1.4.0'):
            git(repo, 'tag', tag)
        assert repo_version.read(str(repo))['label'].startswith('v1.10.0 (')

    def test_past_the_last_release(self, repo):
        git(repo, 'tag', 'v1.3.1')
        git(repo, 'tag', 'v1.4.0')
        git(repo, 'commit', '-q', '--allow-empty', '-m', 'two')
        git(repo, 'commit', '-q', '--allow-empty', '-m', 'three')
        v = repo_version.read(str(repo))
        assert v['label'] == 'v1.4.0 +2 (%s)' % v['commit'][:7]

    def test_tagging_later_renames_but_keeps_the_commit(self, repo):
        before = repo_version.read(str(repo))
        git(repo, 'tag', 'v2.0.0')
        after = repo_version.read(str(repo))
        assert after['commit'] == before['commit'] and after['label'] != before['label']

    def test_current_reads_the_configured_repo(self, app, repo):
        app.config['CONFIGGEN_REPO'] = str(repo)
        repo_version.forget()
        git(repo, 'tag', 'v1.0.0')
        assert repo_version.current(app)['label'].startswith('v1.0.0 (')


@pytest.fixture
def project(app, auth_client, skeleton_dir):
    ensure_workspace(app, 'admin')
    create_project(app, 'admin', P)
    add_project_hosts(app, 'admin', P, ['SW1', 'SW2'], 'none')
    return app


@pytest.fixture
def netforge(monkeypatch):
    """Set which NetForge the app sees as checked out."""
    def set_(version):
        monkeypatch.setattr(repo_version, 'current', lambda app: version)
    return set_


def generate(app, host, with_, name=None):
    name = name or '%s_FULL.ios' % host
    gcdir = project_generated_configs_dir(app, 'admin', P)
    os.makedirs(gcdir, exist_ok=True)
    with open(os.path.join(gcdir, name), 'w') as f:
        f.write('hostname X\n')
    fp = checks.hostvars_fingerprint(os.path.join(project_host_vars_dir(app, 'admin', P), host))
    record_generated_from(app, 'admin', P, {name: fp}, with_)


def change(app, host):
    path = os.path.join(project_host_vars_dir(app, 'admin', P), host, 'general.yml')
    with open(path, 'a') as f:
        f.write('# edited\n')


def stale_findings(app):
    return [f for f in checks.run(app, 'admin', P) if f['check'] == 'stale_config']


class TestOlderTemplates:

    def test_same_version_is_current(self, project, netforge):
        netforge(NEW)
        generate(project, 'SW1', NEW)
        assert checks.stale_configs(project, 'admin', P) == {}
        assert stale_findings(project) == []

    def test_older_version_is_out_of_date(self, project, netforge):
        generate(project, 'SW1', OLD)
        generate(project, 'SW2', NEW)
        netforge(NEW)
        why = {}
        assert checks.stale_configs(project, 'admin', P, why) == {'SW1': ['SW1_FULL.ios']}
        assert why == {'SW1': {'settings': False, 'templates': ['v1.4.0 (aaaaaaa)']}}
        [f] = stale_findings(project)
        assert f['title'] == "SW1's config was generated with older NetForge templates"
        assert f['details'] == ['SW1_FULL.ios',
                                'Generated with NetForge v1.4.0 (aaaaaaa); now v1.4.1 (bbbbbbb)']

    def test_settings_and_templates_both_changed(self, project, netforge):
        generate(project, 'SW1', OLD)
        change(project, 'SW1')
        netforge(NEW)
        why = {}
        checks.stale_configs(project, 'admin', P, why)
        assert why['SW1'] == {'settings': True, 'templates': ['v1.4.0 (aaaaaaa)']}
        [f] = stale_findings(project)
        assert f['title'] == 'SW1 has changed since its config was generated'

    def test_unrecorded_version_is_not_judged(self, project, netforge):
        """Configs from before versions were recorded, or made when the
        repo's version couldn't be read."""
        generate(project, 'SW1', None)
        netforge(NEW)
        assert checks.stale_configs(project, 'admin', P) == {}

    def test_unknown_current_version_judges_nothing(self, project, netforge):
        generate(project, 'SW1', OLD)
        netforge(None)
        assert checks.stale_configs(project, 'admin', P) == {}
        assert stale_findings(project) == []

    def test_regenerating_clears_it(self, project, netforge):
        generate(project, 'SW1', OLD)
        netforge(NEW)
        generate(project, 'SW1', NEW)
        assert checks.stale_configs(project, 'admin', P) == {}

    def test_regenerating_without_a_version_forgets_the_old_one(self, project, netforge):
        generate(project, 'SW1', OLD)
        generate(project, 'SW1', None)
        assert get_project_config(project, 'admin', P)['generated_with'] == {}

    def test_record_is_dropped_with_the_config(self, project, netforge):
        generate(project, 'SW1', OLD)
        os.remove(os.path.join(project_generated_configs_dir(project, 'admin', P), 'SW1_FULL.ios'))
        generate(project, 'SW2', NEW)
        assert list(get_project_config(project, 'admin', P)['generated_with']) == ['SW2_FULL.ios']

    def test_project_badge_counts_it(self, project, netforge, auth_client):
        generate(project, 'SW1', OLD)
        netforge(NEW)
        res = auth_client.get('/projects/admin/%s/status' % P).get_json()
        assert res['out_of_date'] == 1


class TestGeneratePage:

    def page(self, client):
        return client.get('/projects/admin/%s/generate' % P).get_data(as_text=True)

    def test_shows_the_templates_in_use(self, project, netforge, auth_client):
        netforge(NEW)
        assert 'Templates: NetForge v1.4.1 (bbbbbbb)' in self.page(auth_client)

    def test_no_version_no_line(self, project, netforge, auth_client):
        netforge(None)
        assert 'Templates: NetForge' not in self.page(auth_client)

    def test_offers_configs_from_older_templates(self, project, netforge, auth_client):
        generate(project, 'SW1', OLD)
        generate(project, 'SW2', OLD)
        netforge(NEW)
        body = self.page(auth_client)
        assert '2 configs were generated with older NetForge templates' in body
        assert 'const STALE_HOSTS = ["SW1", "SW2"];' in body

    def test_mixed_reasons_keep_the_changed_wording(self, project, netforge, auth_client):
        generate(project, 'SW1', OLD)
        generate(project, 'SW2', NEW)
        change(project, 'SW2')
        netforge(NEW)
        body = self.page(auth_client)
        assert '2 switches have changed since their configs were generated' in body
        assert 'templates have also been updated since SW1 was generated' in body


class TestGenerationRecordsVersion:

    def test_audit_entry_and_config_carry_the_version(self, project, netforge,
                                                      auth_client, monkeypatch):
        import time
        from app import docker_runner
        netforge(NEW)

        def fake_run(job_id, username, project_name, data_dir, limit=None, tags=None,
                     progress_callback=None):
            gcdir = project_generated_configs_dir(project, 'admin', P)
            os.makedirs(gcdir, exist_ok=True)
            with open(os.path.join(gcdir, 'SW1_FULL.ios'), 'w') as f:
                f.write('hostname SW1\n')
            return 0, 'ok'
        monkeypatch.setattr(docker_runner, 'run_generate', fake_run)

        res = auth_client.post('/projects/admin/%s/generate/run' % P, json={'limit': 'SW1'})
        assert res.status_code == 200
        for _ in range(100):
            made = get_project_config(project, 'admin', P).get('generated_with') or {}
            if made:
                break
            time.sleep(0.05)
        assert made == {'SW1_FULL.ios': NEW}
        entry = audit.entries(project, owner='admin', project=P)[0]
        assert entry['params']['templates'] == 'NetForge v1.4.1 (bbbbbbb)'


class TestStartupPull:

    def test_a_pull_that_moved_the_repo_is_recorded(self, app):
        audit.record_startup_pull(app, OLD, NEW, 0,
                                  'From https://user:tok3n@example.invalid/x\nUpdating a..b\n')
        [e] = audit.entries(app, limit=5)
        assert (e['action'], e['username'], e['status']) == ('repo_pull', '(startup)', 'done')
        assert e['params'] == {'from': 'v1.4.0 (aaaaaaa)', 'to': 'v1.4.1 (bbbbbbb)'}
        assert 'tok3n' not in e['output_tail'] and 'Updating a..b' in e['output_tail']

    def test_nothing_new_is_not_recorded(self, app):
        assert audit.record_startup_pull(app, NEW, dict(NEW), 0, 'Already up to date.') is None
        assert audit.entries(app, limit=5) == []

    def test_a_failed_pull_is_recorded(self, app):
        audit.record_startup_pull(app, OLD, OLD, 1, 'fatal: unable to access')
        [e] = audit.entries(app, limit=5)
        assert e['status'] == 'failed' and e['params']['to'] is None
