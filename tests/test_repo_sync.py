"""
Pinning NetForge to a release, or following a branch: what a check offers,
what a switch does, what a container start does in each case, and the
Admin page and routes around them.
"""

import os
import subprocess

import pytest

from app import audit, repo_sync, repo_version


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    return subprocess.run(['git', '-C', str(repo)] + list(args), check=True,
                          capture_output=True, text=True, env=env).stdout.strip()


def commit(repo, subject, path='README.md'):
    full = os.path.join(str(repo), path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'a') as f:
        f.write(subject + '\n')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', subject)


@pytest.fixture
def origin(tmp_path):
    """Upstream NetForge: v1.0.0, then a template change tagged v1.1.0."""
    path = tmp_path / 'origin'
    path.mkdir()
    git(path, 'init', '-q', '-b', 'main')
    commit(path, 'first')
    git(path, 'tag', 'v1.0.0')
    commit(path, 'change vlans template', 'roles/generate_config/templates/CX_vlans.j2')
    git(path, 'tag', 'v1.1.0')
    return path


@pytest.fixture
def cloned(app, origin, tmp_path):
    """The app with its repo cloned from origin, following main."""
    repo = tmp_path / 'clone'
    subprocess.run(['git', 'clone', '-q', str(origin), str(repo)], check=True,
                   capture_output=True)
    app.config['CONFIGGEN_REPO'] = str(repo)
    app.config['CONFIGGEN_REPO_REF'] = ''
    app.config['SSH_KEY_PATH'] = str(tmp_path / 'no-key')
    repo_version.forget()
    return app


@pytest.fixture(autouse=True)
def no_jobs_running():
    """Other tests leave fake unfinished jobs in the shared job stores,
    which a switch would rightly refuse to run alongside."""
    from app.projects import _jobs, _jobs_lock
    from app.deploy_routes import _deploy_jobs, _deploy_jobs_lock
    from app.firmware_routes import _fw_jobs, _fw_jobs_lock
    for store, lock in ((_jobs, _jobs_lock), (_deploy_jobs, _deploy_jobs_lock),
                        (_fw_jobs, _fw_jobs_lock)):
        with lock:
            store.clear()


def label(app):
    return repo_version.read(app.config['CONFIGGEN_REPO'])['label']


def by_ref(app):
    return {c['ref']: c for c in repo_sync.choices(app)}


class TestState:

    def test_not_cloned(self, app):
        assert repo_sync.state(app)['cloned'] is False

    def test_a_clone_follows_its_branch(self, cloned):
        s = repo_sync.state(cloned)
        assert (s['mode'], s['branch'], s['locked']) == ('follow', 'main', None)
        assert s['version'].startswith('v1.1.0 (')

    def test_on_a_tag_is_pinned(self, cloned):
        repo_sync.switch(cloned, 'v1.0.0')
        s = repo_sync.state(cloned)
        assert (s['mode'], s['branch']) == ('pinned', None)
        assert s['version'].startswith('v1.0.0 (')


class TestChoices:

    def test_lists_the_branch_then_releases_newest_first(self, cloned):
        assert [c['ref'] for c in repo_sync.choices(cloned)] == ['main', 'v1.1.0', 'v1.0.0']

    def test_marks_where_it_is(self, cloned):
        c = by_ref(cloned)
        assert c['main']['current'] and not c['main']['ahead']
        # Same commit as v1.1.0, but following main isn't being pinned to it.
        assert not c['v1.1.0']['current'] and not c['v1.1.0']['ahead']
        repo_sync.switch(cloned, 'v1.1.0')
        c = by_ref(cloned)
        assert c['v1.1.0']['current'] and not c['main']['current']

    def test_an_older_release_shows_what_would_be_dropped(self, cloned):
        old = by_ref(cloned)['v1.0.0']
        assert (old['ahead'], old['behind'], old['templates_changed']) == (0, 1, True)
        assert [k['subject'] for k in old['commits']] == ['change vlans template']

    def test_a_fetch_shows_a_new_release_and_its_commits(self, cloned, origin):
        repo_sync.switch(cloned, 'v1.1.0')
        commit(origin, 'docs only')
        git(origin, 'tag', 'v1.2.0')
        assert 'v1.2.0' not in by_ref(cloned)
        repo_sync.fetch(cloned)
        new = by_ref(cloned)['v1.2.0']
        assert (new['ahead'], new['behind'], new['templates_changed']) == (1, 0, False)
        assert [k['subject'] for k in new['commits']] == ['docs only']
        # Fetching moved nothing.
        assert label(cloned).startswith('v1.1.0 (')

    def test_one_entry_per_commit_with_the_highest_tag(self, cloned, origin):
        git(origin, 'tag', 'v1.1.1')       # same commit as v1.1.0
        repo_sync.fetch(cloned)
        assert [c['ref'] for c in repo_sync.choices(cloned)] == ['main', 'v1.1.1', 'v1.0.0']

    def test_an_unreachable_remote_is_an_error(self, cloned):
        git(cloned.config['CONFIGGEN_REPO'], 'remote', 'set-url', 'origin', '/nonexistent/x')
        with pytest.raises(repo_sync.RepoError):
            repo_sync.fetch(cloned)


class TestSwitch:

    def test_pin_then_follow_again(self, cloned, origin):
        before, after = repo_sync.switch(cloned, 'v1.0.0')
        assert before['label'].startswith('v1.1.0 (') and after['label'].startswith('v1.0.0 (')
        commit(origin, 'later')
        repo_sync.fetch(cloned)
        repo_sync.switch(cloned, 'main')
        assert repo_sync.state(cloned)['mode'] == 'follow'
        assert label(cloned).startswith('v1.1.0 +1 (')

    @pytest.mark.parametrize('ref', ['', 'nope', '-q', '--upload-pack=x', 'v1.0.0..main',
                                     'HEAD', 'origin/main', 'a b', '../x'])
    def test_only_real_releases_and_branches(self, cloned, ref):
        with pytest.raises(repo_sync.RepoError):
            repo_sync.switch(cloned, ref)
        assert label(cloned).startswith('v1.1.0 (')


class TestStartup:

    def quiet(self, app):
        return repo_sync.sync_at_startup(app, log=lambda _m: None)

    def test_following_pulls(self, cloned, origin):
        commit(origin, 'later')
        rc, _out, before, after = self.quiet(cloned)
        assert rc == 0 and before['commit'] != after['commit']
        assert after['label'].startswith('v1.1.0 +1 (')

    def test_pinned_fetches_but_stays(self, cloned, origin):
        repo_sync.switch(cloned, 'v1.1.0')
        commit(origin, 'later')
        git(origin, 'tag', 'v1.2.0')
        rc, _out, before, after = self.quiet(cloned)
        assert rc == 0 and before['commit'] == after['commit']
        assert 'v1.2.0' in by_ref(cloned)

    def test_configured_ref_is_checked_out(self, cloned, origin):
        commit(origin, 'later')
        git(origin, 'tag', 'v1.2.0')
        cloned.config['CONFIGGEN_REPO_REF'] = 'v1.2.0'
        rc, _out, _before, after = self.quiet(cloned)
        assert rc == 0 and after['label'].startswith('v1.2.0 (')
        assert repo_sync.state(cloned)['mode'] == 'pinned'

    def test_configured_ref_overrides_a_pin_made_in_admin(self, cloned):
        repo_sync.switch(cloned, 'v1.0.0')
        cloned.config['CONFIGGEN_REPO_REF'] = 'v1.1.0'
        self.quiet(cloned)
        assert label(cloned).startswith('v1.1.0 (')

    def test_configured_branch_follows_it(self, cloned, origin):
        repo_sync.switch(cloned, 'v1.0.0')
        commit(origin, 'later')
        cloned.config['CONFIGGEN_REPO_REF'] = 'main'
        self.quiet(cloned)
        assert repo_sync.state(cloned)['mode'] == 'follow'
        assert label(cloned).startswith('v1.1.0 +1 (')

    def test_unknown_configured_ref_fails_and_stays_put(self, cloned):
        cloned.config['CONFIGGEN_REPO_REF'] = 'v9.9.9'
        rc, out, before, after = self.quiet(cloned)
        assert rc != 0 and 'v9.9.9' in out and before['commit'] == after['commit']

    def test_configured_ref_survives_an_unreachable_remote(self, cloned):
        """The release is already in the clone, so being offline doesn't
        stop the app starting on it."""
        git(cloned.config['CONFIGGEN_REPO'], 'remote', 'set-url', 'origin', '/nonexistent/x')
        cloned.config['CONFIGGEN_REPO_REF'] = 'v1.0.0'
        self.quiet(cloned)
        assert label(cloned).startswith('v1.0.0 (')


class TestAdminRoutes:

    def test_check_lists_choices(self, cloned, auth_client):
        res = auth_client.post('/admin/repo/check')
        data = res.get_json()
        assert res.status_code == 200 and data['state']['mode'] == 'follow'
        assert [c['ref'] for c in data['choices']] == ['main', 'v1.1.0', 'v1.0.0']

    def test_check_needs_a_clone(self, app, auth_client):
        assert auth_client.post('/admin/repo/check').status_code == 400

    def test_switch_pins_and_is_audited(self, cloned, auth_client):
        res = auth_client.post('/admin/repo/switch', json={'ref': 'v1.0.0'})
        data = res.get_json()
        assert res.status_code == 200 and data['state']['mode'] == 'pinned'
        assert data['version'].startswith('v1.0.0 (')
        [e] = audit.entries(cloned, limit=5)
        assert (e['action'], e['username'], e['status']) == ('repo_pull', 'admin', 'done')
        assert e['params']['to'] == 'v1.0.0' and e['params']['from'].startswith('v1.1.0 (')

    def test_switch_to_nonsense_is_refused_and_audited_as_failed(self, cloned, auth_client):
        res = auth_client.post('/admin/repo/switch', json={'ref': '--force'})
        assert res.status_code == 400
        assert audit.entries(cloned, limit=5)[0]['status'] == 'failed'
        assert label(cloned).startswith('v1.1.0 (')

    def test_switch_refused_when_the_environment_decides(self, cloned, auth_client):
        cloned.config['CONFIGGEN_REPO_REF'] = 'v1.1.0'
        res = auth_client.post('/admin/repo/switch', json={'ref': 'v1.0.0'})
        assert res.status_code == 409 and 'CONFIGGEN_REPO_REF' in res.get_json()['error']
        assert label(cloned).startswith('v1.1.0 (')

    def test_switch_refused_while_a_job_runs(self, cloned, auth_client):
        from app.projects import _jobs, _jobs_lock
        with _jobs_lock:
            _jobs['j1'] = {'status': 'running'}
        try:
            res = auth_client.post('/admin/repo/switch', json={'ref': 'v1.0.0'})
            assert res.status_code == 409 and 'generation is running' in res.get_json()['error']
        finally:
            with _jobs_lock:
                _jobs.pop('j1', None)

    def test_pull_refused_when_pinned(self, cloned, auth_client):
        repo_sync.switch(cloned, 'v1.0.0')
        data = auth_client.post('/admin/pull').get_json()
        assert data['ok'] is False and 'pinned to v1.0.0' in data['error']

    def test_routes_are_admin_only(self, cloned, client):
        from app.models import User
        User.create('bob', 'password1', cloned)
        client.post('/login', data={'username': 'bob', 'password': 'password1'})
        assert client.post('/admin/repo/check').status_code == 403
        assert client.post('/admin/repo/switch', json={'ref': 'v1.0.0'}).status_code == 403


class TestAdminPage:

    def page(self, client):
        return client.get('/admin/repo').get_data(as_text=True)

    def test_following_offers_pull_and_check(self, cloned, auth_client):
        body = self.page(auth_client)
        assert 'Following main' in body
        assert 'id="pull-btn"' in body and 'id="check-btn"' in body

    def test_pinned_offers_check_only(self, cloned, auth_client):
        repo_sync.switch(cloned, 'v1.0.0')
        body = self.page(auth_client)
        assert 'Pinned to this release' in body
        assert 'id="pull-btn"' not in body and 'id="check-btn"' in body

    def test_says_when_the_environment_decides(self, cloned, auth_client):
        cloned.config['CONFIGGEN_REPO_REF'] = 'v1.1.0'
        body = self.page(auth_client)
        assert 'CONFIGGEN_REPO_REF=v1.1.0' in body
        assert 'const REPO_LOCKED = "v1.1.0";' in body
