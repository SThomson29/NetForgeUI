"""
The generation container used to mount the whole data volume read-write.

That volume holds every user's projects and users.db, and Ansible templates
variable values, so a crafted value in one user's host_vars could read other
users' credentials or rewrite the user database. These tests pin the
replacement: only a copy of the one project goes in, and only plain .ios
files come back out.

A fake Docker client stands in for the daemon. It keeps a real temporary
directory as the container's filesystem, so the archive handling in both
directions is exercised for real rather than mocked away.
"""

import io
import os
import tarfile

import pytest

import app.docker_runner as dr


class FakeContainer:
    def __init__(self, fs_root, run):
        self.fs = fs_root
        self.run = run          # callable(fs_root) -> exit code, plays the playbook
        self.removed = False
        self.put_calls = []

    def put_archive(self, path, data):
        self.put_calls.append(path)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(os.path.join(self.fs, path.lstrip('/')),
                           filter='data')
        return True

    def start(self):
        self.rc = self.run(self.fs)

    def logs(self, stream=True, follow=True):
        return iter([b'PLAY [generate] ***\n', b'PLAY RECAP ***\n'])

    def wait(self):
        return {'StatusCode': self.rc}

    def get_archive(self, path):
        src = os.path.join(self.fs, path.lstrip('/'))
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w') as tar:
            tar.add(src, arcname=os.path.basename(src))
        return iter([buf.getvalue()]), {}

    def remove(self, force=False):
        self.removed = True


class FakeClient:
    def __init__(self, fs_root, run):
        self.fs_root = fs_root
        self.run = run
        self.create_kwargs = None
        self.container = None

        outer = self

        class _Images:
            def get(self, name):
                return object()

            def pull(self, name):
                return object()

        class _Containers:
            def create(self, **kwargs):
                outer.create_kwargs = kwargs
                outer.container = FakeContainer(outer.fs_root, outer.run)
                return outer.container

        self.images = _Images()
        self.containers = _Containers()


def _write(path, text='x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)


@pytest.fixture
def layout(tmp_path):
    """Two users' data on the 'volume', as the real DATA_DIR holds it."""
    data_dir = tmp_path / 'data' / 'users'
    mine = data_dir / 'alice' / 'projects' / 'core'
    _write(str(mine / 'hosts.ini'), '[cx]\nsw1\n')
    _write(str(mine / 'host_vars' / 'sw1' / 'general.yml'), 'hostname: sw1\n')
    _write(str(mine / 'generated_configs' / 'sw1_08_vlans.ios'), 'vlan 999\n')
    _write(str(mine / 'config.json'), '{"secret": "pool data"}')
    # Someone else's credentials, and the user database, on the same volume
    _write(str(data_dir / 'bob' / 'projects' / 'other' / 'host_vars'
               / 'sw9' / 'aaa.yml'), 'radius_server_key: "BOBS-SECRET"\n')
    _write(str(tmp_path / 'data' / 'users.db'), 'password hashes')
    return {'data_dir': str(data_dir), 'project': str(mine),
            'container_fs': str(tmp_path / 'container')}


def _run(monkeypatch, layout, playbook):
    os.makedirs(layout['container_fs'], exist_ok=True)
    client = FakeClient(layout['container_fs'], playbook)
    monkeypatch.setattr(dr, '_get_client', lambda: client)
    rc, out = dr.run_generate('job1', 'alice', 'core', layout['data_dir'])
    return client, rc, out


def _container_files(fs):
    found = []
    for root, _dirs, files in os.walk(fs):
        for f in files:
            found.append(os.path.relpath(os.path.join(root, f), fs))
    return sorted(found)


def _normal_playbook(fs):
    out = os.path.join(fs, 'work/project/generated_configs')
    _write(os.path.join(out, 'sw1_03_general.ios'), 'hostname sw1\n')
    _write(os.path.join(out, 'sw1_FULL.ios'), 'hostname sw1\n')
    # the real playbook removes sections that rendered empty
    os.remove(os.path.join(out, 'sw1_08_vlans.ios'))
    return 0


class TestIsolation:

    def test_data_volume_is_not_mounted(self, monkeypatch, layout):
        client, _rc, _ = _run(monkeypatch, layout, _normal_playbook)
        mounts = client.create_kwargs['volumes']
        assert dr.DATA_VOLUME not in mounts, 'whole data volume still mounted'
        assert list(mounts) == [dr.REPO_VOLUME]
        assert mounts[dr.REPO_VOLUME]['mode'] == 'ro'

    def test_no_network(self, monkeypatch, layout):
        client, _rc, _ = _run(monkeypatch, layout, _normal_playbook)
        assert client.create_kwargs['network_mode'] == 'none'

    def test_only_this_project_is_copied_in(self, monkeypatch, layout):
        seen = {}

        def capture(fs):
            seen['files'] = _container_files(fs)
            return 0

        _run(monkeypatch, layout, capture)
        files = seen['files']
        assert 'work/project/hosts.ini' in files
        assert 'work/project/host_vars/sw1/general.yml' in files
        # nothing from anyone else, and not the user database
        assert not any('bob' in f or 'BOBS' in f for f in files)
        assert not any(f.endswith('users.db') for f in files)
        # config.json carries pool data generation does not need
        assert 'work/project/config.json' not in files

    def test_symlinks_are_not_followed_into_the_container(
            self, monkeypatch, layout, tmp_path):
        """A link in host_vars pointing at another user's data must not
        carry that data across."""
        bob = (tmp_path / 'data' / 'users' / 'bob' / 'projects' / 'other'
               / 'host_vars' / 'sw9' / 'aaa.yml')
        os.symlink(str(bob), os.path.join(layout['project'], 'host_vars',
                                          'sw1', 'aaa.yml'))
        seen = {}

        def capture(fs):
            seen['files'] = _container_files(fs)
            return 0

        _run(monkeypatch, layout, capture)
        assert 'work/project/host_vars/sw1/aaa.yml' not in seen['files']

    def test_container_is_removed(self, monkeypatch, layout):
        client, _rc, _ = _run(monkeypatch, layout, _normal_playbook)
        assert client.container.removed

    def test_container_is_removed_when_the_run_fails(self, monkeypatch, layout):
        def boom(fs):
            raise RuntimeError('playbook exploded')

        client, rc, out = _run(monkeypatch, layout, boom)
        assert rc == -1
        assert 'playbook exploded' in out
        assert client.container.removed


class TestOutputsComeBack:

    def test_new_configs_land_in_the_project(self, monkeypatch, layout):
        _client, rc, _ = _run(monkeypatch, layout, _normal_playbook)
        assert rc == 0
        out = os.path.join(layout['project'], 'generated_configs')
        assert sorted(os.listdir(out)) == ['sw1_03_general.ios', 'sw1_FULL.ios']

    def test_sections_removed_by_the_playbook_disappear(self, monkeypatch, layout):
        """A merge would leave the stale section to be reassembled forever."""
        _run(monkeypatch, layout, _normal_playbook)
        out = os.path.join(layout['project'], 'generated_configs')
        assert 'sw1_08_vlans.ios' not in os.listdir(out)

    def test_output_is_mirrored_even_when_the_run_fails(self, monkeypatch, layout):
        def partial(fs):
            _write(os.path.join(fs, 'work/project/generated_configs',
                                'sw1_03_general.ios'), 'hostname sw1\n')
            return 2

        _client, rc, _ = _run(monkeypatch, layout, partial)
        assert rc == 2
        out = os.path.join(layout['project'], 'generated_configs')
        assert 'sw1_03_general.ios' in os.listdir(out)


class TestUntrustedArchive:
    """The archive comes out of a container that ran user-influenced
    templates, so its contents are not trusted."""

    def _archive(self, members):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w') as tar:
            for name, kind, data in members:
                info = tarfile.TarInfo(name)
                if kind == 'file':
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
                elif kind == 'link':
                    info.type = tarfile.SYMTYPE
                    info.linkname = data
                    tar.addfile(info)
                elif kind == 'dir':
                    info.type = tarfile.DIRTYPE
                    tar.addfile(info)
        return [buf.getvalue()]

    def test_only_plain_ios_files_are_accepted(self):
        stream = self._archive([
            ('generated_configs', 'dir', None),
            ('generated_configs/sw1_FULL.ios', 'file', b'ok'),
            ('generated_configs/../../../etc/cron.d/x.ios', 'file', b'bad'),
            ('generated_configs/sub/nested.ios', 'file', b'bad'),
            ('generated_configs/notes.txt', 'file', b'bad'),
            ('generated_configs/evil.ios', 'link', '/data/users.db'),
            ('other/sw1_FULL.ios', 'file', b'bad'),
        ])
        files = dr._read_output_archive(stream)
        assert files == {'sw1_FULL.ios': b'ok'}

    def test_oversized_output_is_refused(self, monkeypatch):
        monkeypatch.setattr(dr, 'MAX_OUTPUT_BYTES', 10)
        with pytest.raises(RuntimeError, match='exceeded'):
            dr._read_output_archive(iter([b'x' * 11]))

    def test_mirror_never_touches_non_config_files(self, tmp_path):
        out = tmp_path / 'gc'
        out.mkdir()
        (out / 'keep.txt').write_text('not ours')
        (out / 'old.ios').write_text('stale')
        dr._mirror_outputs(str(out), {'new.ios': b'fresh'})
        assert sorted(os.listdir(out)) == ['keep.txt', 'new.ios']


class TestResourceLimits:
    """host_vars are templated by Ansible, so a value can run commands in the
    container. It has no network and nothing worth reaching, but without
    limits it could hang a worker thread forever or exhaust the host."""

    def test_container_is_capped(self, monkeypatch, layout):
        client, _rc, _ = _run(monkeypatch, layout, _normal_playbook)
        kw = client.create_kwargs
        assert kw['mem_limit'] == '2g'
        assert kw['memswap_limit'] == kw['mem_limit']
        assert kw['pids_limit'] == 512
        assert kw['cap_drop'] == ['ALL']
        assert 'no-new-privileges' in kw['security_opt']

    def test_files_go_in_owned_by_root(self, layout):
        """Without CAP_DAC_OVERRIDE, root in the container can only overwrite
        the previous run's configs if it owns them."""
        data = dr._build_project_archive(layout['project'])
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            members = tar.getmembers()
        assert members
        assert all(m.uid == 0 and m.gid == 0 for m in members)

    def test_a_run_that_never_ends_is_killed(self, monkeypatch, layout):
        import threading
        monkeypatch.setattr(dr, 'GENERATE_TIMEOUT_SECS', 0.2)
        killed = threading.Event()

        class Hanging(FakeContainer):
            def logs(self, stream=True, follow=True):
                yield b'PLAY [generate] ***\n'
                killed.wait(5)      # the stream ends only when killed

            def kill(self):
                killed.set()

            def wait(self):
                return {'StatusCode': 137}

        client = FakeClient(layout['container_fs'], _normal_playbook)
        os.makedirs(layout['container_fs'], exist_ok=True)
        client.containers.create = lambda **kw: setattr(
            client, 'container', Hanging(client.fs_root, client.run)) \
            or client.container
        monkeypatch.setattr(dr, '_get_client', lambda: client)

        rc, out = dr.run_generate('job1', 'alice', 'core', layout['data_dir'])
        assert killed.is_set()
        assert rc == -1
        assert 'longer than' in out
        assert client.container.removed
        # a killed run must not replace the project's configs
        assert os.listdir(os.path.join(layout['project'], 'generated_configs')) \
            == ['sw1_08_vlans.ios']

    def test_out_of_memory_is_explained(self, monkeypatch, layout):
        class OOM(FakeContainer):
            attrs = {}

            def reload(self):
                self.attrs = {'State': {'OOMKilled': True}}

        client = FakeClient(layout['container_fs'], lambda fs: 137)
        os.makedirs(layout['container_fs'], exist_ok=True)
        client.containers.create = lambda **kw: setattr(
            client, 'container', OOM(client.fs_root, client.run)) \
            or client.container
        monkeypatch.setattr(dr, '_get_client', lambda: client)

        rc, out = dr.run_generate('job1', 'alice', 'core', layout['data_dir'])
        assert rc == 137
        assert 'more than 2g of memory' in out
