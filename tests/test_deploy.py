"""
Tests for the deploy feature.

Covers:
  - Mapping file CRUD (load, save, auto-populate)
  - Dynamic inventory generation
  - Deploy page rendering
  - Dry-run route (mocked playbook)
  - Push route (mocked playbook)
  - Credential handling (not persisted)
  - Edge cases (missing configs, empty mappings, bad input)
"""

import os
import json
import yaml
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers — create a project with hosts and generated configs
# ---------------------------------------------------------------------------

def setup_deploy_project(app, auth_client, project_name='deploy-test',
                          hosts=None, generate_configs=True):
    """Create a project with hosts and optionally stub generated configs."""
    # Create project
    auth_client.post('/projects/new', data={'name': project_name},
                     follow_redirects=True)

    if hosts is None:
        hosts = [
            ('CO-CORE-SW-01', 'vsf-2'),
            ('CO-ACC-SW-01', 'none'),
        ]

    # Add hosts
    for hostname, stacking in hosts:
        auth_client.post(
            f'/projects/{project_name}/hosts/add',
            data={'hostname': hostname, 'stacking': stacking},
            follow_redirects=True,
        )

    # Stub generated config files
    if generate_configs:
        from app.project import project_generated_configs_dir
        with app.app_context():
            gcdir = project_generated_configs_dir(app, 'admin', project_name)
            os.makedirs(gcdir, exist_ok=True)
            for hostname, _ in hosts:
                config_path = os.path.join(gcdir, f'{hostname}_FULL.ios')
                with open(config_path, 'w') as f:
                    f.write(f'! Generated config\nhostname {hostname}\n')

    return project_name


def write_mapping(app, project_name, mapping_data):
    from app.deploy_routes import _save_mapping
    with app.app_context():
        _save_mapping(app, 'admin', project_name, mapping_data)


def read_mapping(app, project_name):
    from app.deploy_routes import _load_mapping
    with app.app_context():
        return _load_mapping(app, 'admin', project_name)


# ---------------------------------------------------------------------------
# Unit tests — inventory builder
# ---------------------------------------------------------------------------

def post_and_wait(client, url, payload, timeout=5.0):
    """Start a deploy job and poll until it finishes, as the page does.

    Deploy runs in a background thread now, so the POST returns only a job id
    and the results arrive from the status endpoint.
    """
    import time
    res = client.post(url, json=payload, content_type='application/json')
    assert res.status_code == 200, res.data
    started = json.loads(res.data)
    if not started.get('ok'):
        return started
    job_id = started['job_id']
    deadline = time.time() + timeout
    while time.time() < deadline:
        poll = client.get(url.rsplit('/', 1)[0] + '/status/' + job_id)
        job = json.loads(poll.data)
        if job['status'] not in ('starting', 'running'):
            job['ok'] = job['status'] == 'done'
            return job
        time.sleep(0.02)
    raise AssertionError('job did not finish within %ss' % timeout)


class TestBuildInventory:
    """Test the _build_inventory helper function."""

    def test_builds_valid_inventory(self):
        from app.deploy_routes import _build_inventory

        hosts = [
            {'hostname': 'SW-01', 'mgmt_ip': '10.1.1.1'},
            {'hostname': 'SW-02', 'mgmt_ip': '10.1.1.2'},
        ]
        inv = _build_inventory(hosts, 'admin', 's3cret')

        assert 'all' in inv
        assert 'hosts' in inv['all']
        assert 'SW-01' in inv['all']['hosts']
        assert 'SW-02' in inv['all']['hosts']
        assert inv['all']['hosts']['SW-01']['ansible_host'] == '10.1.1.1'
        assert inv['all']['hosts']['SW-02']['ansible_host'] == '10.1.1.2'

    def test_inventory_sets_connection_vars(self):
        from app.deploy_routes import _build_inventory

        inv = _build_inventory(
            [{'hostname': 'SW-01', 'mgmt_ip': '10.1.1.1'}],
            'admin', 's3cret'
        )
        vars_ = inv['all']['vars']
        assert vars_['ansible_connection'] == 'network_cli'
        assert vars_['ansible_network_os'] == 'arubanetworks.aoscx.aoscx'
        assert vars_['ansible_user'] == 'admin'
        assert vars_['ansible_password'] == 's3cret'
        assert vars_['ansible_become'] is True
        assert vars_['ansible_become_method'] == 'enable'

    def test_inventory_empty_hosts(self):
        from app.deploy_routes import _build_inventory

        inv = _build_inventory([], 'admin', 'pass')
        assert inv['all']['hosts'] == {}

    def test_credentials_not_in_host_vars(self):
        """Credentials should be in group vars, not per-host."""
        from app.deploy_routes import _build_inventory

        inv = _build_inventory(
            [{'hostname': 'SW-01', 'mgmt_ip': '10.1.1.1'}],
            'admin', 'pass'
        )
        host_vars = inv['all']['hosts']['SW-01']
        assert 'ansible_user' not in host_vars
        assert 'ansible_password' not in host_vars


# ---------------------------------------------------------------------------
# Unit tests — mapping file I/O
# ---------------------------------------------------------------------------

class TestMappingIO:
    """Test mapping file load/save."""

    def test_load_empty_mapping(self, app, auth_client):
        """Loading a mapping that doesn't exist returns defaults."""
        setup_deploy_project(app, auth_client)
        with app.app_context():
            from app.deploy_routes import _load_mapping
            mapping = _load_mapping(app, 'admin', 'deploy-test')
            assert mapping['hosts'] == []
            assert mapping['settings']['rollback_timeout'] == 5

    def test_save_and_load_mapping(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        mapping_data = {
            'hosts': [
                {'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'},
                {'hostname': 'CO-ACC-SW-01', 'mgmt_ip': '10.1.100.2'},
            ],
            'settings': {'rollback_timeout': 10}
        }
        with app.app_context():
            from app.deploy_routes import _save_mapping, _load_mapping
            _save_mapping(app, 'admin', 'deploy-test', mapping_data)
            loaded = _load_mapping(app, 'admin', 'deploy-test')

        assert len(loaded['hosts']) == 2
        assert loaded['hosts'][0]['hostname'] == 'CO-CORE-SW-01'
        assert loaded['hosts'][0]['mgmt_ip'] == '10.1.100.1'
        assert loaded['settings']['rollback_timeout'] == 10

    def test_save_mapping_overwrites(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        with app.app_context():
            from app.deploy_routes import _save_mapping, _load_mapping

            _save_mapping(app, 'admin', 'deploy-test', {
                'hosts': [{'hostname': 'OLD', 'mgmt_ip': '1.1.1.1'}],
                'settings': {'rollback_timeout': 5}
            })
            _save_mapping(app, 'admin', 'deploy-test', {
                'hosts': [{'hostname': 'NEW', 'mgmt_ip': '2.2.2.2'}],
                'settings': {'rollback_timeout': 3}
            })
            loaded = _load_mapping(app, 'admin', 'deploy-test')

        assert len(loaded['hosts']) == 1
        assert loaded['hosts'][0]['hostname'] == 'NEW'


# ---------------------------------------------------------------------------
# Integration tests — deploy page
# ---------------------------------------------------------------------------

class TestDeployPage:
    """Test the deploy tab page rendering."""

    def test_deploy_page_loads(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        res = auth_client.get('/projects/deploy-test/deploy')
        assert res.status_code == 200
        assert b'Deploy' in res.data

    def test_deploy_page_shows_mapping(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        with app.app_context():
            write_mapping(app, 'deploy-test', {
                'hosts': [
                    {'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'},
                ],
                'settings': {'rollback_timeout': 5}
            })
        res = auth_client.get('/projects/deploy-test/deploy')
        assert res.status_code == 200
        assert b'CO-CORE-SW-01' in res.data
        assert b'10.1.100.1' in res.data

    def test_deploy_page_shows_config_availability(self, app, auth_client):
        """Hosts with generated configs should show a green badge."""
        setup_deploy_project(app, auth_client, generate_configs=True)
        with app.app_context():
            write_mapping(app, 'deploy-test', {
                'hosts': [
                    {'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'},
                ],
                'settings': {'rollback_timeout': 5}
            })
        res = auth_client.get('/projects/deploy-test/deploy')
        assert b'.ios' in res.data

    def test_deploy_page_requires_login(self, client):
        res = client.get('/projects/deploy-test/deploy', follow_redirects=False)
        assert res.status_code == 401 


# ---------------------------------------------------------------------------
# Integration tests — save mapping route
# ---------------------------------------------------------------------------

class TestSaveMappingRoute:

    def test_save_mapping(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        res = auth_client.post(
            '/projects/deploy-test/deploy/save-mapping',
            json={
                'mappings': [
                    {'hostname': 'SW-01', 'mgmt_ip': '10.1.1.1'},
                    {'hostname': 'SW-02', 'mgmt_ip': '10.1.1.2'},
                ],
                'rollback_timeout': 7,
            },
            content_type='application/json',
        )
        data = json.loads(res.data)
        assert data['ok'] is True

        with app.app_context():
            saved = read_mapping(app, 'deploy-test')
        assert len(saved['hosts']) == 2
        assert saved['settings']['rollback_timeout'] == 7

    def test_save_empty_mapping(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        res = auth_client.post(
            '/projects/deploy-test/deploy/save-mapping',
            json={'mappings': [], 'rollback_timeout': 5},
            content_type='application/json',
        )
        data = json.loads(res.data)
        assert data['ok'] is True

    def test_save_mapping_no_body(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        res = auth_client.post(
            '/projects/deploy-test/deploy/save-mapping',
            data='',
            content_type='application/json',
        )
        assert res.status_code == 400


# ---------------------------------------------------------------------------
# Integration tests — auto-populate
# ---------------------------------------------------------------------------

class TestAutoPopulate:

    def test_auto_populate_from_hosts(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        res = auth_client.get('/projects/deploy-test/deploy/auto-populate')
        data = json.loads(res.data)
        assert data['ok'] is True

        with app.app_context():
            mapping = read_mapping(app, 'deploy-test')
        hostnames = [h['hostname'] for h in mapping['hosts']]
        assert 'CO-CORE-SW-01' in hostnames
        assert 'CO-ACC-SW-01' in hostnames

    def test_auto_populate_does_not_duplicate(self, app, auth_client):
        """Running auto-populate twice should not create duplicates."""
        setup_deploy_project(app, auth_client)
        auth_client.get('/projects/deploy-test/deploy/auto-populate')
        auth_client.get('/projects/deploy-test/deploy/auto-populate')

        with app.app_context():
            mapping = read_mapping(app, 'deploy-test')
        hostnames = [h['hostname'] for h in mapping['hosts']]
        assert hostnames.count('CO-CORE-SW-01') == 1

    def test_auto_populate_preserves_existing_ips(self, app, auth_client):
        """Existing mappings with IPs should not be overwritten."""
        setup_deploy_project(app, auth_client)
        with app.app_context():
            write_mapping(app, 'deploy-test', {
                'hosts': [
                    {'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.99.99.99'},
                ],
                'settings': {'rollback_timeout': 5}
            })
        auth_client.get('/projects/deploy-test/deploy/auto-populate')

        with app.app_context():
            mapping = read_mapping(app, 'deploy-test')
        core = next(h for h in mapping['hosts'] if h['hostname'] == 'CO-CORE-SW-01')
        assert core['mgmt_ip'] == '10.99.99.99'


# ---------------------------------------------------------------------------
# Integration tests — dry run
# ---------------------------------------------------------------------------

class TestDryRun:

    def _mock_playbook_success(self, results=None):
        """Return a patcher that mocks _run_playbook to succeed."""
        if results is None:
            results = [
                {
                    'hostname': 'CO-CORE-SW-01',
                    'mgmt_ip': '10.1.100.1',
                    'changed': True,
                    'diff': {'prepared': '+ hostname CO-CORE-SW-01'},
                    'commands': ['hostname CO-CORE-SW-01'],
                },
            ]
        import app.deploy_routes as dr

        def _fake(job_id, playbook_name, repo_dir, ansible_bin,
                  inventory, extra_vars, limit_hosts=None):
            with dr._deploy_jobs_lock:
                dr._deploy_jobs[job_id].update(
                    status='done', returncode=0, results=results, output='')

        return patch('app.deploy_routes._run_playbook_job', _fake)

    def _mock_playbook_failure(self, error_output='Connection refused'):
        import app.deploy_routes as dr

        def _fake(job_id, playbook_name, repo_dir, ansible_bin,
                  inventory, extra_vars, limit_hosts=None):
            with dr._deploy_jobs_lock:
                dr._deploy_jobs[job_id].update(
                    status='failed', returncode=2, results=[],
                    output=error_output)

        return patch('app.deploy_routes._run_playbook_job', _fake)

    def test_dryrun_returns_diffs(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        with self._mock_playbook_success():
            data = post_and_wait(
                auth_client,
                '/projects/deploy-test/deploy/dryrun',
                {
                    'hosts': [
                        {'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'},
                    ],
                    'username': 'admin',
                    'password': 'testpass',
                },
            )
        assert data['ok'] is True
        assert len(data['results']) == 1
        assert data['results'][0]['changed'] is True
        assert data['results'][0]['hostname'] == 'CO-CORE-SW-01'

    def test_dryrun_no_changes(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        no_change = [{
            'hostname': 'CO-CORE-SW-01',
            'mgmt_ip': '10.1.100.1',
            'changed': False,
            'diff': {},
            'commands': [],
        }]
        with self._mock_playbook_success(results=no_change):
            data = post_and_wait(
                auth_client,
                '/projects/deploy-test/deploy/dryrun',
                {
                    'hosts': [{'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'}],
                    'username': 'admin',
                    'password': 'testpass',
                },
            )
        assert data['ok'] is True
        assert data['results'][0]['changed'] is False

    def test_dryrun_playbook_failure(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        with self._mock_playbook_failure('SSH timeout'):
            data = post_and_wait(
                auth_client,
                '/projects/deploy-test/deploy/dryrun',
                {
                    'hosts': [{'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'}],
                    'username': 'admin',
                    'password': 'testpass',
                },
            )
        assert data['ok'] is False
        assert 'SSH timeout' in data['output']

    def test_dryrun_missing_credentials(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        res = auth_client.post(
            '/projects/deploy-test/deploy/dryrun',
            json={
                'hosts': [{'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'}],
                'username': '',
                'password': '',
            },
            content_type='application/json',
        )
        assert res.status_code == 400

    def test_dryrun_missing_hosts(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        res = auth_client.post(
            '/projects/deploy-test/deploy/dryrun',
            json={
                'hosts': [],
                'username': 'admin',
                'password': 'pass',
            },
            content_type='application/json',
        )
        assert res.status_code == 400

    def test_dryrun_no_body(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        res = auth_client.post(
            '/projects/deploy-test/deploy/dryrun',
            data='',
            content_type='application/json',
        )
        assert res.status_code == 400

    def test_dryrun_multiple_hosts(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        multi_results = [
            {
                'hostname': 'CO-CORE-SW-01',
                'mgmt_ip': '10.1.100.1',
                'changed': True,
                'diff': {'prepared': '+ hostname CO-CORE-SW-01'},
                'commands': ['hostname CO-CORE-SW-01'],
            },
            {
                'hostname': 'CO-ACC-SW-01',
                'mgmt_ip': '10.1.100.2',
                'changed': False,
                'diff': {},
                'commands': [],
            },
        ]
        with self._mock_playbook_success(results=multi_results):
            data = post_and_wait(
                auth_client,
                '/projects/deploy-test/deploy/dryrun',
                {
                    'hosts': [
                        {'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'},
                        {'hostname': 'CO-ACC-SW-01', 'mgmt_ip': '10.1.100.2'},
                    ],
                    'username': 'admin',
                    'password': 'testpass',
                },
            )
        assert data['ok'] is True
        assert len(data['results']) == 2
        changed_hosts = [r['hostname'] for r in data['results'] if r['changed']]
        assert 'CO-CORE-SW-01' in changed_hosts
        assert 'CO-ACC-SW-01' not in changed_hosts


# ---------------------------------------------------------------------------
# Integration tests — deploy push
# ---------------------------------------------------------------------------

class TestDeployPush:

    def _mock_push_success(self, results=None):
        if results is None:
            results = [
                {
                    'hostname': 'CO-CORE-SW-01',
                    'mgmt_ip': '10.1.100.1',
                    'checkpoint': 'netforge-pre-20260405T120000',
                    'config_pushed': True,
                    'reachable_after_push': True,
                    'confirmed': True,
                    'rollback_pending': False,
                    'rollback_timeout_mins': 5,
                    'diff': {},
                    'commands': [],
                },
            ]
        import app.deploy_routes as dr

        def _fake(job_id, playbook_name, repo_dir, ansible_bin,
                  inventory, extra_vars, limit_hosts=None):
            with dr._deploy_jobs_lock:
                dr._deploy_jobs[job_id].update(
                    status='done', returncode=0, results=results, output='')

        return patch('app.deploy_routes._run_playbook_job', _fake)

    def _mock_push_partial_failure(self):
        """One switch confirmed, one unreachable."""
        results = [
            {
                'hostname': 'CO-CORE-SW-01',
                'mgmt_ip': '10.1.100.1',
                'checkpoint': 'netforge-pre-20260405T120000',
                'config_pushed': True,
                'reachable_after_push': True,
                'confirmed': True,
                'rollback_pending': False,
                'rollback_timeout_mins': 5,
                'diff': {},
                'commands': [],
            },
            {
                'hostname': 'CO-ACC-SW-01',
                'mgmt_ip': '10.1.100.2',
                'checkpoint': 'netforge-pre-20260405T120000',
                'config_pushed': True,
                'reachable_after_push': False,
                'confirmed': False,
                'rollback_pending': True,
                'rollback_timeout_mins': 5,
                'diff': {},
                'commands': [],
            },
        ]
        import app.deploy_routes as dr

        def _fake(job_id, playbook_name, repo_dir, ansible_bin,
                  inventory, extra_vars, limit_hosts=None):
            with dr._deploy_jobs_lock:
                dr._deploy_jobs[job_id].update(
                    status='done', returncode=0, results=results, output='')

        return patch('app.deploy_routes._run_playbook_job', _fake)

    def test_push_success(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        with self._mock_push_success():
            data = post_and_wait(
                auth_client,
                '/projects/deploy-test/deploy/push',
                {
                    'hosts': [
                        {'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'},
                    ],
                    'username': 'admin',
                    'password': 'testpass',
                    'rollback_timeout': 5,
                },
            )
        assert data['ok'] is True
        assert len(data['results']) == 1
        assert data['results'][0]['confirmed'] is True
        assert data['results'][0]['rollback_pending'] is False

    def test_push_partial_failure(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        with self._mock_push_partial_failure():
            data = post_and_wait(
                auth_client,
                '/projects/deploy-test/deploy/push',
                {
                    'hosts': [
                        {'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'},
                        {'hostname': 'CO-ACC-SW-01', 'mgmt_ip': '10.1.100.2'},
                    ],
                    'username': 'admin',
                    'password': 'testpass',
                    'rollback_timeout': 5,
                },
            )
        assert data['ok'] is True
        confirmed = [r for r in data['results'] if r['confirmed']]
        rollback = [r for r in data['results'] if r['rollback_pending']]
        assert len(confirmed) == 1
        assert len(rollback) == 1
        assert rollback[0]['hostname'] == 'CO-ACC-SW-01'
        assert rollback[0]['rollback_timeout_mins'] == 5

    def test_push_includes_checkpoint_name(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        with self._mock_push_success():
            data = post_and_wait(
                auth_client,
                '/projects/deploy-test/deploy/push',
                {
                    'hosts': [
                        {'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'},
                    ],
                    'username': 'admin',
                    'password': 'testpass',
                    'rollback_timeout': 5,
                },
            )
        assert data['results'][0]['checkpoint'].startswith('netforge-pre-')

    def test_push_missing_credentials(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        res = auth_client.post(
            '/projects/deploy-test/deploy/push',
            json={
                'hosts': [{'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'}],
                'username': '',
                'password': '',
                'rollback_timeout': 5,
            },
            content_type='application/json',
        )
        assert res.status_code == 400

    def test_push_custom_rollback_timeout(self, app, auth_client):
        """Verify rollback_timeout is passed through to the playbook."""
        setup_deploy_project(app, auth_client)

        captured_extra_vars = {}

        import app.deploy_routes as dr

        def mock_run(job_id, playbook, repo_dir, ansible_bin,
                     inventory, extra_vars, limit_hosts=None):
            captured_extra_vars.update(extra_vars)
            _results = [{
                'hostname': 'CO-CORE-SW-01',
                'mgmt_ip': '10.1.100.1',
                'checkpoint': 'netforge-pre-test',
                'config_pushed': True,
                'reachable_after_push': True,
                'confirmed': True,
                'rollback_pending': False,
                'rollback_timeout_mins': 10,
                'diff': {},
                'commands': [],
            }]
            with dr._deploy_jobs_lock:
                dr._deploy_jobs[job_id].update(
                    status='done', returncode=0, results=_results, output='')

        # Wait for the job rather than asserting straight after the POST —
        # the worker runs in a thread, so a bare post is a race.
        with patch('app.deploy_routes._run_playbook_job', side_effect=mock_run):
            post_and_wait(
                auth_client,
                '/projects/deploy-test/deploy/push',
                {
                    'hosts': [{'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'}],
                    'username': 'admin',
                    'password': 'testpass',
                    'rollback_timeout': 10,
                },
            )

        assert captured_extra_vars['rollback_timeout'] == 10

    def test_push_no_body(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        res = auth_client.post(
            '/projects/deploy-test/deploy/push',
            data='',
            content_type='application/json',
        )
        assert res.status_code == 400


# ---------------------------------------------------------------------------
# Integration tests — _run_playbook (subprocess layer)
# ---------------------------------------------------------------------------

class TestRunPlaybook:
    """Test the _run_playbook helper that wraps subprocess."""

    def test_builds_correct_command(self, app, auth_client):
        setup_deploy_project(app, auth_client)

        captured_cmd = []

        def mock_subprocess_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = '{}'
            mock.stderr = ''
            return mock

        with app.app_context():
            from app.deploy_routes import _run_playbook

            # Create a dummy playbook file
            repo_dir = app.config['CONFIGGEN_REPO']
            os.makedirs(os.path.join(repo_dir, 'playbooks'), exist_ok=True)
            pb_path = os.path.join(repo_dir, 'playbooks', 'deploy_dryrun.yml')
            with open(pb_path, 'w') as f:
                f.write('---\n- hosts: all\n')

            inventory = {
                'all': {
                    'hosts': {'SW-01': {'ansible_host': '10.1.1.1'}},
                    'vars': {'ansible_connection': 'network_cli'},
                }
            }

            with patch('subprocess.run', side_effect=mock_subprocess_run):
                _run_playbook(
                    'deploy_dryrun.yml',
                    inventory,
                    {'config_dir': '/tmp/configs'},
                    limit_hosts=['SW-01']
                )

        assert 'ansible-playbook' in captured_cmd
        assert '--check' in captured_cmd
        assert '--diff' in captured_cmd
        assert '--limit' in captured_cmd
        assert 'SW-01' in captured_cmd

    def test_dryrun_adds_check_diff_flags(self, app, auth_client):
        """The dryrun playbook should be called with --check --diff."""
        setup_deploy_project(app, auth_client)

        captured_cmd = []

        def mock_subprocess_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = '{}'
            mock.stderr = ''
            return mock

        with app.app_context():
            from app.deploy_routes import _run_playbook

            repo_dir = app.config['CONFIGGEN_REPO']
            os.makedirs(os.path.join(repo_dir, 'playbooks'), exist_ok=True)
            with open(os.path.join(repo_dir, 'playbooks', 'deploy_dryrun.yml'), 'w') as f:
                f.write('---\n')

            with patch('subprocess.run', side_effect=mock_subprocess_run):
                _run_playbook('deploy_dryrun.yml', {'all': {'hosts': {}}}, {})

        assert '--check' in captured_cmd
        assert '--diff' in captured_cmd

    def test_push_does_not_add_check_flag(self, app, auth_client):
        """The push playbook should NOT have --check."""
        setup_deploy_project(app, auth_client)

        captured_cmd = []

        def mock_subprocess_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = '{}'
            mock.stderr = ''
            return mock

        with app.app_context():
            from app.deploy_routes import _run_playbook

            repo_dir = app.config['CONFIGGEN_REPO']
            os.makedirs(os.path.join(repo_dir, 'playbooks'), exist_ok=True)
            with open(os.path.join(repo_dir, 'playbooks', 'deploy_push.yml'), 'w') as f:
                f.write('---\n')

            with patch('subprocess.run', side_effect=mock_subprocess_run):
                _run_playbook('deploy_push.yml', {'all': {'hosts': {}}}, {})

        assert '--check' not in captured_cmd
        assert '--diff' not in captured_cmd

    def test_timeout_returns_failure(self, app, auth_client):
        import subprocess as sp
        setup_deploy_project(app, auth_client)

        with app.app_context():
            from app.deploy_routes import _run_playbook

            repo_dir = app.config['CONFIGGEN_REPO']
            os.makedirs(os.path.join(repo_dir, 'playbooks'), exist_ok=True)
            with open(os.path.join(repo_dir, 'playbooks', 'deploy_push.yml'), 'w') as f:
                f.write('---\n')

            with patch('subprocess.run', side_effect=sp.TimeoutExpired('cmd', 300)):
                ok, output, results = _run_playbook(
                    'deploy_push.yml', {'all': {'hosts': {}}}, {}
                )

        assert ok is False
        assert 'timed out' in output.lower()

    def test_missing_playbook_returns_failure(self, app, auth_client):
        setup_deploy_project(app, auth_client)

        with app.app_context():
            from app.deploy_routes import _run_playbook
            ok, output, results = _run_playbook(
                'nonexistent.yml', {'all': {'hosts': {}}}, {}
            )

        assert ok is False
        assert 'not found' in output.lower()

    def test_host_key_checking_disabled(self, app, auth_client):
        """Ansible should run with ANSIBLE_HOST_KEY_CHECKING=False."""
        setup_deploy_project(app, auth_client)

        captured_env = {}

        def mock_subprocess_run(cmd, **kwargs):
            captured_env.update(kwargs.get('env', {}))
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = '{}'
            mock.stderr = ''
            return mock

        with app.app_context():
            from app.deploy_routes import _run_playbook

            repo_dir = app.config['CONFIGGEN_REPO']
            os.makedirs(os.path.join(repo_dir, 'playbooks'), exist_ok=True)
            with open(os.path.join(repo_dir, 'playbooks', 'deploy_push.yml'), 'w') as f:
                f.write('---\n')

            with patch('subprocess.run', side_effect=mock_subprocess_run):
                _run_playbook('deploy_push.yml', {'all': {'hosts': {}}}, {})

        assert captured_env.get('ANSIBLE_HOST_KEY_CHECKING') == 'False'


# ---------------------------------------------------------------------------
# Security — credential handling
# ---------------------------------------------------------------------------

class TestCredentialSecurity:
    """Ensure credentials are not persisted anywhere."""

    def test_credentials_not_in_mapping_file(self, app, auth_client):
        """After a dry run, credentials should not appear in deploy_mapping.yml."""
        setup_deploy_project(app, auth_client)

        with patch('app.deploy_routes._run_playbook', return_value=(True, '', [])):
            auth_client.post(
                '/projects/deploy-test/deploy/dryrun',
                json={
                    'hosts': [{'hostname': 'SW-01', 'mgmt_ip': '10.1.1.1'}],
                    'username': 'supersecret_user',
                    'password': 'supersecret_pass',
                },
                content_type='application/json',
            )

        with app.app_context():
            mapping = read_mapping(app, 'deploy-test')

        # Mapping may or may not exist, but if it does, no creds
        if mapping:
            raw = yaml.dump(mapping)
            assert 'supersecret_user' not in raw
            assert 'supersecret_pass' not in raw

    def test_credentials_not_in_project_config(self, app, auth_client):
        """Credentials should never be written to project.json."""
        setup_deploy_project(app, auth_client)

        with patch('app.deploy_routes._run_playbook', return_value=(True, '', [])):
            auth_client.post(
                '/projects/deploy-test/deploy/push',
                json={
                    'hosts': [{'hostname': 'SW-01', 'mgmt_ip': '10.1.1.1'}],
                    'username': 'admin',
                    'password': 'secret123',
                    'rollback_timeout': 5,
                },
                content_type='application/json',
            )

        # Walk the entire project directory and check no file contains creds
        with app.app_context():
            data_dir = app.config.get('DATA_DIR', 'data')
            project_dir = os.path.join(data_dir, 'admin', 'projects', 'deploy-test')
            for root, dirs, files in os.walk(project_dir):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath) as f:
                            content = f.read()
                        assert 'secret123' not in content, \
                            f'Credential found in {fpath}'
                    except (UnicodeDecodeError, IsADirectoryError):
                        pass  # skip binary files


# ---------------------------------------------------------------------------
# Config file selection — full vs per-host override
# ---------------------------------------------------------------------------

class TestDeployConfigFileSelection:

    def test_has_config_false_without_full_file(self, app, auth_client):
        """A bare <hostname>.ios is not what NetForge writes — must not count."""
        setup_deploy_project(app, auth_client, generate_configs=False)
        with app.app_context():
            from app.project import project_generated_configs_dir
            gcdir = project_generated_configs_dir(app, 'admin', 'deploy-test')
            os.makedirs(gcdir, exist_ok=True)
            with open(os.path.join(gcdir, 'CO-CORE-SW-01.ios'), 'w') as f:
                f.write('! stale name\n')
            write_mapping(app, 'deploy-test', {
                'hosts': [{'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'}],
                'settings': {'rollback_timeout': 5}
            })
        res = auth_client.get('/projects/deploy-test/deploy')
        assert b'CO-CORE-SW-01_FULL.ios' not in res.data

    def test_inventory_omits_config_file_by_default(self):
        """No override means the playbook default applies."""
        from app.deploy_routes import _build_inventory
        inv = _build_inventory(
            [{'hostname': 'sw1', 'mgmt_ip': '10.0.0.1'}], 'u', 'p')
        assert 'deploy_config_file' not in inv['all']['hosts']['sw1']

    def test_inventory_carries_per_host_config_file(self):
        """An explicit file is set per host, not globally."""
        from app.deploy_routes import _build_inventory
        inv = _build_inventory([
            {'hostname': 'sw1', 'mgmt_ip': '10.0.0.1',
             'config_file': '/out/sw1_PARTIAL_syslog.ios'},
            {'hostname': 'sw2', 'mgmt_ip': '10.0.0.2'},
        ], 'u', 'p')
        assert inv['all']['hosts']['sw1']['deploy_config_file'] == \
            '/out/sw1_PARTIAL_syslog.ios'
        assert 'deploy_config_file' not in inv['all']['hosts']['sw2']
        assert 'deploy_config_file' not in inv['all']['vars']


# ---------------------------------------------------------------------------
# Playbook location
# ---------------------------------------------------------------------------

class TestPlaybookPath:

    def test_uses_absolute_repo_path_from_config(self, app, tmp_path, monkeypatch):
        """The playbook must be located via CONFIGGEN_REPO, not a relative guess.

        A relative 'configgen' only resolves when the process happens to run
        from /app/service, which it does not under gunicorn — the failure is a
        confusing "playbook could not be found".
        """
        import app.deploy_routes as dr

        repo = tmp_path / 'cfgrepo'
        (repo / 'playbooks').mkdir(parents=True)
        (repo / 'playbooks' / 'deploy_dryrun.yml').write_text('---\n')
        app.config['CONFIGGEN_REPO'] = str(repo)

        seen = {}

        def fake_run(cmd, **kwargs):
            seen['cmd'] = cmd
            seen['cwd'] = kwargs.get('cwd')
            class R:
                returncode = 0
                stdout = '{}'
                stderr = ''
            return R()

        monkeypatch.setattr(dr.subprocess, 'run', fake_run)

        with app.test_request_context():
            dr._run_playbook('deploy_dryrun.yml',
                             {'all': {'hosts': {}}}, {'config_dir': '/x'})

        # the playbook argument must be the absolute path under the repo
        assert str(repo / 'playbooks' / 'deploy_dryrun.yml') in seen['cmd']
        assert seen['cwd'] == str(repo)

    def test_missing_playbook_reports_the_resolved_path(self, app, tmp_path):
        import app.deploy_routes as dr
        app.config['CONFIGGEN_REPO'] = str(tmp_path / 'nope')
        with app.test_request_context():
            ok, msg, _ = dr._run_playbook('deploy_dryrun.yml',
                                          {'all': {'hosts': {}}}, {})
        assert ok is False
        assert str(tmp_path / 'nope') in msg


class TestStdoutCallback:

    def test_does_not_request_a_collection_only_callback(self, app, tmp_path, monkeypatch):
        """Only callbacks bundled with ansible-core may be requested.

        'json' ships in ansible.posix, not ansible-core, so asking for it
        fails with "Could not load 'json' callback plugin". Nothing here
        parses stdout — per-host data comes from the result files — so the
        default callback is correct and gives a readable log.
        """
        import app.deploy_routes as dr

        repo = tmp_path / 'repo'
        (repo / 'playbooks').mkdir(parents=True)
        (repo / 'playbooks' / 'deploy_dryrun.yml').write_text('---\n')
        app.config['CONFIGGEN_REPO'] = str(repo)

        seen = {}

        def fake_run(cmd, **kwargs):
            seen['env'] = kwargs.get('env', {})
            class R:
                returncode = 0
                stdout = ''
                stderr = ''
            return R()

        monkeypatch.setattr(dr.subprocess, 'run', fake_run)
        with app.test_request_context():
            dr._run_playbook('deploy_dryrun.yml', {'all': {'hosts': {}}}, {})

        CORE_CALLBACKS = {'default', 'junit', 'minimal', 'oneline', 'tree'}
        requested = seen['env'].get('ANSIBLE_STDOUT_CALLBACK')
        assert requested is None or requested in CORE_CALLBACKS, (
            '%r is not bundled with ansible-core' % requested)


class TestMappingTableMarkup:

    def test_config_badge_uses_full_filename(self, app, auth_client):
        """The badge must name the file has_config actually looks for."""
        setup_deploy_project(app, auth_client)
        write_mapping(app, 'deploy-test', {
            'hosts': [{'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'}],
            'settings': {'rollback_timeout': 5}})
        res = auth_client.get('/projects/deploy-test/deploy')
        body = res.data.decode()
        assert 'CO-CORE-SW-01_FULL.ios' in body
        assert 'CO-CORE-SW-01.ios<' not in body

    def test_checkboxes_are_not_sized_inline(self, app, auth_client):
        """Sizing belongs in CSS scoped to the input type, not per element."""
        setup_deploy_project(app, auth_client)
        write_mapping(app, 'deploy-test', {
            'hosts': [{'hostname': 'SW-01', 'mgmt_ip': '10.0.0.1'}],
            'settings': {}})
        body = auth_client.get('/projects/deploy-test/deploy').data.decode()
        # no checkbox should carry an inline width override
        for chunk in body.split('type="checkbox"')[1:]:
            tag = chunk.split('>')[0]
            assert 'width:14px' not in tag, 'inline sizing on a checkbox'
        assert 'input[type="checkbox"]' in body, 'checkbox CSS rule missing'
        assert 'table-layout: fixed' in body


class TestSshTransportDependency:

    def test_an_ssh_transport_is_pinned(self):
        """network_cli needs pylibssh or paramiko; ansible-core pulls neither.

        Without one, every deploy task fails with "Failed to import the
        required Python library (paramiko)" the moment it tries to reach a
        switch — which is not obvious from the playbook.
        """
        import os
        req = os.path.join(os.path.dirname(__file__), '..', 'requirements.txt')
        with open(req) as f:
            body = f.read()
        assert 'ansible-pylibssh' in body or 'paramiko' in body, (
            'no SSH transport pinned for the network_cli connection')

    def test_ssh_transport_is_importable_where_ansible_is(self):
        """If the Ansible runtime is installed, a transport must be too.

        Catches a wheel that failed to install for the build architecture.
        Skipped in environments without the runtime, where the question does
        not arise.
        """
        import importlib.util
        if not importlib.util.find_spec('ansible'):
            pytest.skip('ansible-core not installed in this environment')
        assert (importlib.util.find_spec('pylibsshext')
                or importlib.util.find_spec('paramiko')), (
            'ansible-core is installed but neither pylibssh nor paramiko is')


# ---------------------------------------------------------------------------
# Job model — deploy runs in the background with streaming output
# ---------------------------------------------------------------------------

class TestDeployJobModel:

    def _stub(self, tmp_path, script):
        p = tmp_path / 'stub-ansible'
        p.write_text(script)
        p.chmod(0o755)
        return str(p)

    def _repo(self, tmp_path, app):
        repo = tmp_path / 'repo'
        (repo / 'playbooks').mkdir(parents=True)
        for pb in ('deploy_dryrun.yml', 'deploy_push.yml'):
            (repo / 'playbooks' / pb).write_text('---\n')
        app.config['CONFIGGEN_REPO'] = str(repo)
        return str(repo)

    def test_output_streams_while_running(self, app, tmp_path):
        """The point of the job model: progress is visible mid-run.

        A synchronous request showed only a spinner, so a slow SSH connect
        was indistinguishable from a hung page.
        """
        import time
        import app.deploy_routes as dr

        self._repo(tmp_path, app)
        app.config['ANSIBLE_BIN'] = self._stub(tmp_path, (
            '#!/bin/sh\n'
            'echo "PLAY [Dry Run] ***"\n'
            'sleep 0.5\n'
            'echo "TASK [Grab current running config] ***"\n'
            'sleep 0.5\n'
            'echo "PLAY RECAP ***"\n'
        ))

        job_id = dr._start_deploy_job(
            app, 'deploy_dryrun.yml',
            {'all': {'hosts': {'SW-01': {'ansible_host': '10.0.0.1'}}}},
            {'config_dir': '/x'}, ['SW-01'])

        time.sleep(0.3)
        with dr._deploy_jobs_lock:
            mid = dict(dr._deploy_jobs[job_id])
        assert mid['status'] == 'running'
        assert 'PLAY [Dry Run]' in mid['output'], 'no output before completion'

        for _ in range(60):
            time.sleep(0.1)
            with dr._deploy_jobs_lock:
                final = dict(dr._deploy_jobs[job_id])
            if final['status'] not in ('starting', 'running'):
                break
        assert final['status'] == 'done'
        assert 'PLAY RECAP' in final['output']

    def test_failure_keeps_output_for_diagnosis(self, app, tmp_path):
        import time
        import app.deploy_routes as dr

        self._repo(tmp_path, app)
        app.config['ANSIBLE_BIN'] = self._stub(tmp_path, (
            '#!/bin/sh\n'
            'echo "fatal: [SW-01]: ssh connect failed: Connection refused"\n'
            'exit 2\n'
        ))

        job_id = dr._start_deploy_job(
            app, 'deploy_dryrun.yml', {'all': {'hosts': {}}}, {}, None)
        for _ in range(60):
            time.sleep(0.05)
            with dr._deploy_jobs_lock:
                job = dict(dr._deploy_jobs[job_id])
            if job['status'] not in ('starting', 'running'):
                break
        assert job['status'] == 'failed'
        assert job['returncode'] == 2
        assert 'Connection refused' in job['output']

    def test_missing_playbook_is_reported(self, app, tmp_path):
        import time
        import app.deploy_routes as dr
        app.config['CONFIGGEN_REPO'] = str(tmp_path / 'absent')
        job_id = dr._start_deploy_job(
            app, 'deploy_dryrun.yml', {'all': {'hosts': {}}}, {}, None)
        for _ in range(40):
            time.sleep(0.05)
            with dr._deploy_jobs_lock:
                job = dict(dr._deploy_jobs[job_id])
            if job['status'] not in ('starting', 'running'):
                break
        assert job['status'] == 'failed'
        assert 'Playbook not found' in job['output']

    def test_status_requires_login(self, client):
        assert client.get('/projects/p/deploy/status/abc').status_code == 401

    def test_unknown_job_is_404(self, auth_client, app):
        setup_deploy_project(app, auth_client)
        assert auth_client.get(
            '/projects/deploy-test/deploy/status/nope').status_code == 404


class TestSingleWorkerRequirement:

    def test_dockerfile_runs_one_worker(self):
        """In-process job stores break with more than one gunicorn worker.

        The POST that starts a job and the GETs that poll it would land on
        different processes, so polling 404s and live output stops — the
        symptom being intermittent, depending which worker answers.
        """
        import os
        dockerfile = os.path.join(os.path.dirname(__file__), '..', 'Dockerfile')
        with open(dockerfile) as f:
            body = f.read()
        cmd = [l for l in body.splitlines() if l.startswith('CMD')]
        assert cmd, 'no CMD line found'
        assert '--workers 1' in cmd[0], (
            'gunicorn must run a single worker until job state is shared')
