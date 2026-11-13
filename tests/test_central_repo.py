"""
The Central collection is a second repo kept the way NetForge is: cloned
at first start, then following a branch or held at CENTRAL_REPO_REF. It is
cloned to a path Ansible reads as an installed collection, and nothing
about it shows unless CENTRAL_REPO_URL is set.
"""

import os
import subprocess

import pytest

import config
from app import project_types, repo_sync, repo_version
from tests.test_repo_sync import commit, git

CENTRAL = repo_sync.CENTRAL


@pytest.fixture
def collection_origin(tmp_path):
    """Upstream collection: v1.0.0, then a module change tagged v1.1.0."""
    path = tmp_path / 'collection-origin'
    path.mkdir()
    git(path, 'init', '-q', '-b', 'main')
    commit(path, 'first', 'galaxy.yml')
    git(path, 'tag', 'v1.0.0')
    commit(path, 'add routing module', 'plugins/modules/query_central_routing.py')
    git(path, 'tag', 'v1.1.0')
    return path


@pytest.fixture
def central(app, collection_origin, tmp_path):
    """The app with Central configured and nothing cloned yet."""
    root = tmp_path / 'central'
    app.config.update(
        CENTRAL_REPO_URL=str(collection_origin),
        CENTRAL_REPO_REF='',
        CENTRAL_COLLECTIONS_PATH=str(root),
        CENTRAL_REPO=str(root / 'ansible_collections' / 'samthomson' / 'new_central'),
        SSH_KEY_PATH=str(tmp_path / 'no-key'),
    )
    repo_version.forget()
    return app


def label(app):
    return repo_version.read(app.config['CENTRAL_REPO'])['label']


class TestLayout:

    def test_cloned_where_ansible_finds_a_collection(self):
        assert config.Config.CENTRAL_REPO == os.path.join(
            config.Config.CENTRAL_COLLECTIONS_PATH, 'ansible_collections', 'samthomson', 'new_central')

    def test_image_puts_that_path_on_the_collections_path(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'Dockerfile')) as f:
            dockerfile = f.read()
        assert 'ANSIBLE_COLLECTIONS_PATH=/usr/share/ansible/collections:/app/service/central' in dockerfile

    def test_unconfigured_is_not_a_clone(self, app):
        """With no path set, the working directory's own .git must not be
        taken for the collection."""
        app.config.pop('CENTRAL_REPO', None)
        assert repo_sync.cloned(app, CENTRAL) is False
        assert repo_sync.state(app, CENTRAL)['cloned'] is False


class TestFirstStart:

    def test_clones_and_follows_the_branch(self, central):
        logs = []
        assert repo_sync.clone(central, CENTRAL, log=logs.append) is True
        state = repo_sync.state(central, CENTRAL)
        assert (state['cloned'], state['mode'], state['branch']) == (True, 'follow', 'main')
        assert label(central).startswith('v1.1.0')
        assert os.path.isfile(os.path.join(central.config['CENTRAL_REPO'], 'galaxy.yml'))

    def test_configured_ref_is_checked_out(self, central):
        central.config['CENTRAL_REPO_REF'] = 'v1.0.0'
        repo_sync.clone(central, CENTRAL, log=lambda _m: None)
        state = repo_sync.state(central, CENTRAL)
        assert (state['mode'], state['locked']) == ('pinned', 'v1.0.0')
        assert label(central).startswith('v1.0.0')

    def test_leftovers_of_an_interrupted_clone_are_cleared(self, central):
        repo = central.config['CENTRAL_REPO']
        os.makedirs(os.path.join(repo, 'plugins'))
        with open(os.path.join(repo, 'half-written'), 'w') as f:
            f.write('x')
        assert repo_sync.clone(central, CENTRAL, log=lambda _m: None) is True
        assert not os.path.exists(os.path.join(repo, 'half-written'))

    def test_failed_clone_says_so_without_the_token(self, central, tmp_path):
        central.config['CENTRAL_REPO_URL'] = 'https://user:s3cret@invalid.invalid/nothing.git'
        logs = []
        assert repo_sync.clone(central, CENTRAL, log=logs.append) is False
        assert not any('s3cret' in line for line in logs)
        assert repo_sync.cloned(central, CENTRAL) is False


class TestLaterStarts:

    @pytest.fixture
    def cloned(self, central):
        repo_sync.clone(central, CENTRAL, log=lambda _m: None)
        return central

    def test_following_pulls(self, cloned, collection_origin):
        commit(collection_origin, 'fix a module', 'plugins/modules/query_central_ap.py')
        rc, _out, before, after = repo_sync.sync_at_startup(cloned, log=lambda _m: None, source=CENTRAL)
        assert rc == 0
        assert before['commit'] != after['commit']

    def test_configured_ref_holds_it_there(self, cloned, collection_origin):
        cloned.config['CENTRAL_REPO_REF'] = 'v1.0.0'
        commit(collection_origin, 'newer', 'plugins/modules/query_central_ap.py')
        repo_sync.sync_at_startup(cloned, log=lambda _m: None, source=CENTRAL)
        assert label(cloned).startswith('v1.0.0')

    def test_netforge_is_a_separate_repo(self, cloned):
        """Moving the collection leaves NetForge's state alone."""
        assert repo_sync.state(cloned)['cloned'] is False
        assert repo_sync.state(cloned, CENTRAL)['cloned'] is True

    def test_errors_name_the_collection(self, cloned):
        with pytest.raises(repo_sync.RepoError) as e:
            repo_sync.switch(cloned, 'v9.9.9', CENTRAL)
        assert 'Central collection' in str(e.value)
        assert 'NetForge' not in str(e.value)

    def test_choices_flag_changes_to_what_a_run_reads(self, cloned):
        by_ref = {c['ref']: c for c in repo_sync.choices(cloned, CENTRAL)}
        # v1.0.0 lacks the module added since: plugins/ differs.
        assert by_ref['v1.0.0']['templates_changed'] is True


class TestPages:

    def test_admin_page_says_nothing_when_unconfigured(self, app, auth_client):
        body = auth_client.get('/admin/repo').data.decode()
        assert 'Central collection' not in body

    def test_admin_page_before_the_clone(self, central, auth_client):
        body = auth_client.get('/admin/repo').data.decode()
        assert 'Central collection' in body and 'Not cloned yet' in body

    def test_admin_page_shows_version_and_how_it_updates(self, central, auth_client):
        central.config['CENTRAL_REPO_REF'] = 'v1.0.0'
        repo_sync.clone(central, CENTRAL, log=lambda _m: None)
        body = auth_client.get('/admin/repo').data.decode()
        assert 'v1.0.0 (' in body
        assert 'CENTRAL_REPO_REF=v1.0.0' in body

    def test_overview_shows_the_collection_version(self, central, auth_client):
        repo_sync.clone(central, CENTRAL, log=lambda _m: None)
        from app.project import create_project
        create_project(central, 'admin', 'tenant-a', project_types.CENTRAL)
        body = auth_client.get('/projects/admin/tenant-a/central').data.decode()
        assert 'samthomson.new_central' in body and 'v1.1.0 (' in body

    def test_overview_before_the_clone(self, central, auth_client):
        from app.project import create_project
        create_project(central, 'admin', 'tenant-a', project_types.CENTRAL)
        body = auth_client.get('/projects/admin/tenant-a/central').data.decode()
        assert 'has not been cloned yet' in body


class TestSshSetup:

    def test_central_repo_alone_sets_up_the_key(self, app, tmp_path, monkeypatch):
        from app import ssh_setup
        key = tmp_path / 'id_rsa'
        key.write_text('not a real key')
        home = tmp_path / 'home'
        home.mkdir()
        monkeypatch.setattr(os.path, 'expanduser', lambda p: p.replace('~', str(home)))
        monkeypatch.setattr(ssh_setup, '_ensure_known_hosts', lambda path, host: None)
        app.config.update(SSH_KEY_PATH=str(key), SSH_KNOWN_HOSTS=str(home / '.ssh' / 'known_hosts'),
                          CONFIGGEN_REPO_URL='https://github.com/someone/NetForge.git',
                          CENTRAL_REPO_URL='git@git.example.com:someone/new-central.git')
        ssh_setup.setup_ssh(app)
        with open(home / '.ssh' / 'config') as f:
            assert 'Host git.example.com' in f.read()

    def test_https_only_needs_no_setup(self, app, tmp_path, monkeypatch):
        from app import ssh_setup
        home = tmp_path / 'home'
        home.mkdir()
        monkeypatch.setattr(os.path, 'expanduser', lambda p: p.replace('~', str(home)))
        app.config.update(SSH_KEY_PATH=str(tmp_path / 'id_rsa'), SSH_KNOWN_HOSTS=str(home / 'kh'),
                          CONFIGGEN_REPO_URL='https://github.com/someone/NetForge.git',
                          CENTRAL_REPO_URL='https://github.com/someone/new-central.git')
        ssh_setup.setup_ssh(app)
        assert not (home / '.ssh').exists()
