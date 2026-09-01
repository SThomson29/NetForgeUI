"""
Unit tests for app/firmware_routes.py — firmware upgrade jobs.

Covers: image resolution (including traversal and symlink escape),
        request validation, auth, and the job runner lifecycle.
"""

import os, io, json, pytest
from app.firmware_routes import (
    resolve_image_path, list_firmware_images,
    _run_firmware_playbook, _fw_jobs,
)

@pytest.fixture
def fw_project(app, auth_client, tmp_path, monkeypatch):
    fw = tmp_path / 'firmware'; fw.mkdir()
    (fw / 'good.swi').write_text('x')
    app.config['FIRMWARE_DIR'] = str(fw)

    repo = tmp_path / 'repo' / 'playbooks'; repo.mkdir(parents=True)
    (repo / 'firmware_upgrade.yml').write_text('---\n')
    app.config['CONFIGGEN_REPO'] = str(tmp_path / 'repo')

    import app.firmware_routes as fr
    monkeypatch.setattr(fr, '_load_mapping',
        lambda a,u,p: {'hosts':[{'hostname':'core-01','mgmt_ip':'10.0.0.1'}]})
    # never actually launch ansible
    monkeypatch.setattr(fr, '_run_firmware_playbook',
        lambda *a, **k: None)
    return 'proj'

BASE = {'image':'good.swi','partition':'secondary',
        'switch_username':'admin','switch_password':'pw'}

@pytest.mark.parametrize('label,body,expect_ok,fragment', [
    ('valid',          BASE, True, None),
    ('no password',    {**BASE,'switch_password':''}, False, 'credentials'),
    ('no username',    {**BASE,'switch_username':''}, False, 'credentials'),
    ('bad partition',  {**BASE,'partition':'tertiary'}, False, 'primary or secondary'),
    ('traversal',      {**BASE,'image':'../secret.swi'}, False, 'Unknown firmware'),
    ('unknown image',  {**BASE,'image':'nope.swi'}, False, 'Unknown firmware'),
    ('window 0',       {**BASE,'allow_unsafe':True,'unsafe_window_mins':0}, False, 'between 1 and 120'),
    ('window 121',     {**BASE,'allow_unsafe':True,'unsafe_window_mins':121}, False, 'between 1 and 120'),
    ('window 30',      {**BASE,'allow_unsafe':True,'unsafe_window_mins':30}, True, None),
    ('window junk',    {**BASE,'allow_unsafe':True,'unsafe_window_mins':'abc'}, False, 'whole number'),
])
def test_validation(auth_client, fw_project, label, body, expect_ok, fragment):
    res = auth_client.post('/projects/proj/firmware/run', json=body)
    data = res.get_json()
    assert data['ok'] is expect_ok, (label, data)
    if fragment:
        assert fragment in data['error'], (label, data)

def test_images_listed(auth_client, fw_project):
    res = auth_client.get('/projects/proj/firmware/images')
    assert res.get_json()['images'] == ['good.swi']

def test_requires_login(app, client):
    """Fresh client with no session — must not reach the handler.

    Note this deliberately does not use the fw_project fixture: that depends
    on auth_client, which logs in using this same client object.
    """
    assert client.post('/projects/proj/firmware/run', json=BASE).status_code == 401
    assert client.get('/projects/proj/firmware/images').status_code == 401
    assert client.get('/projects/proj/firmware/status/abc').status_code == 401


# ---------------------------------------------------------------------------
# Image resolution — the boundary that stops a crafted name escaping
# ---------------------------------------------------------------------------

class TestImageResolution:

    @pytest.fixture
    def fw_dir(self, app, tmp_path):
        fw = tmp_path / 'fw'; fw.mkdir()
        (fw / 'real.swi').write_text('x')
        (fw / 'notes.txt').write_text('x')
        outside = tmp_path / 'secret.swi'; outside.write_text('x')
        os.symlink(str(outside), str(fw / 'link.swi'))
        app.config['FIRMWARE_DIR'] = str(fw)
        return app

    def test_plain_name_resolves(self, fw_dir):
        assert resolve_image_path(fw_dir, 'real.swi')

    @pytest.mark.parametrize('name', [
        '../secret.swi',
        '../../etc/passwd',
        '/etc/passwd',
        'sub/dir/image.swi',
        'notes.txt',
        'missing.swi',
        '',
        None,
    ])
    def test_rejected(self, fw_dir, name):
        assert resolve_image_path(fw_dir, name) is None

    def test_symlink_out_of_dir_rejected(self, fw_dir):
        """A .swi symlink pointing outside must not be usable."""
        assert resolve_image_path(fw_dir, 'link.swi') is None

    def test_listing_matches_what_is_selectable(self, fw_dir):
        """Anything listed must also pass resolution, or the UI lies."""
        listed = list_firmware_images(fw_dir)
        assert listed == ['real.swi']
        for name in listed:
            assert resolve_image_path(fw_dir, name)


# ---------------------------------------------------------------------------
# Job runner lifecycle
# ---------------------------------------------------------------------------

class TestJobRunner:

    @pytest.fixture
    def stub_repo(self, tmp_path):
        repo = tmp_path / 'repo' / 'playbooks'
        repo.mkdir(parents=True)
        (repo / 'firmware_upgrade.yml').write_text('---\n')
        return str(tmp_path / 'repo'), str(repo / 'firmware_upgrade.yml')

    def _make_bin(self, tmp_path, name, body):
        p = tmp_path / name
        p.write_text(body)
        p.chmod(0o755)
        return str(p)

    def test_success_collects_results_and_output(self, tmp_path, stub_repo):
        repo, pb = stub_repo
        binp = self._make_bin(tmp_path, 'ok.sh', """#!/bin/sh
echo "TASK [Upload image] ***"
VARS=$(echo "$@" | tr ' ' '\n' | grep '^@' | tr -d '@')
RES=$(grep '^results_file:' "$VARS" | awk '{print $2}')
echo '{"hostname":"core-01","booted":true}' > "$RES/core-01_firmware.json"
echo "PLAY RECAP ***"
exit 0
""")
        _fw_jobs['t1'] = {'status': 'starting', 'output': '', 'returncode': None, 'results': []}
        _run_firmware_playbook('t1', pb, repo, binp, {'all': {'hosts': {}}}, {})
        job = _fw_jobs['t1']
        assert job['status'] == 'done'
        assert job['returncode'] == 0
        assert job['results'][0]['hostname'] == 'core-01'
        assert 'PLAY RECAP' in job['output']

    def test_failure_is_reported(self, tmp_path, stub_repo):
        repo, pb = stub_repo
        binp = self._make_bin(tmp_path, 'fail.sh',
                              '#!/bin/sh\necho "fatal: unreachable"\nexit 2\n')
        _fw_jobs['t2'] = {'status': 'starting', 'output': '', 'returncode': None, 'results': []}
        _run_firmware_playbook('t2', pb, repo, binp, {'all': {'hosts': {}}}, {})
        assert _fw_jobs['t2']['status'] == 'failed'
        assert _fw_jobs['t2']['returncode'] == 2
        assert _fw_jobs['t2']['results'] == []

    def test_missing_ansible_binary_is_explained(self, stub_repo):
        repo, pb = stub_repo
        _fw_jobs['t3'] = {'status': 'starting', 'output': '', 'returncode': None, 'results': []}
        _run_firmware_playbook('t3', pb, repo, '/nonexistent/ansible-playbook',
                               {'all': {'hosts': {}}}, {})
        assert _fw_jobs['t3']['status'] == 'failed'
        assert 'ansible-core' in _fw_jobs['t3']['output']
