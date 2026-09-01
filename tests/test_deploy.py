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
        assert res.status_code == 200
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
        assert res.status_code == 200
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
        assert res.status_code == 200
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
        return patch(
            'app.deploy_routes._run_playbook',
            return_value=(True, '', results)
        )

    def _mock_playbook_failure(self, error_output='Connection refused'):
        return patch(
            'app.deploy_routes._run_playbook',
            return_value=(False, error_output, [])
        )

    def test_dryrun_returns_diffs(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        with self._mock_playbook_success():
            res = auth_client.post(
                '/projects/deploy-test/deploy/dryrun',
                json={
                    'hosts': [
                        {'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'},
                    ],
                    'username': 'admin',
                    'password': 'testpass',
                },
                content_type='application/json',
            )
        assert res.status_code == 200
        data = json.loads(res.data)
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
            res = auth_client.post(
                '/projects/deploy-test/deploy/dryrun',
                json={
                    'hosts': [{'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'}],
                    'username': 'admin',
                    'password': 'testpass',
                },
                content_type='application/json',
            )
        data = json.loads(res.data)
        assert data['ok'] is True
        assert data['results'][0]['changed'] is False

    def test_dryrun_playbook_failure(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        with self._mock_playbook_failure('SSH timeout'):
            res = auth_client.post(
                '/projects/deploy-test/deploy/dryrun',
                json={
                    'hosts': [{'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'}],
                    'username': 'admin',
                    'password': 'testpass',
                },
                content_type='application/json',
            )
        data = json.loads(res.data)
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
            res = auth_client.post(
                '/projects/deploy-test/deploy/dryrun',
                json={
                    'hosts': [
                        {'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'},
                        {'hostname': 'CO-ACC-SW-01', 'mgmt_ip': '10.1.100.2'},
                    ],
                    'username': 'admin',
                    'password': 'testpass',
                },
                content_type='application/json',
            )
        data = json.loads(res.data)
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
        return patch(
            'app.deploy_routes._run_playbook',
            return_value=(True, '', results)
        )

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
        return patch(
            'app.deploy_routes._run_playbook',
            return_value=(True, '', results)
        )

    def test_push_success(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        with self._mock_push_success():
            res = auth_client.post(
                '/projects/deploy-test/deploy/push',
                json={
                    'hosts': [
                        {'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'},
                    ],
                    'username': 'admin',
                    'password': 'testpass',
                    'rollback_timeout': 5,
                },
                content_type='application/json',
            )
        assert res.status_code == 200
        data = json.loads(res.data)
        assert data['ok'] is True
        assert len(data['results']) == 1
        assert data['results'][0]['confirmed'] is True
        assert data['results'][0]['rollback_pending'] is False

    def test_push_partial_failure(self, app, auth_client):
        setup_deploy_project(app, auth_client)
        with self._mock_push_partial_failure():
            res = auth_client.post(
                '/projects/deploy-test/deploy/push',
                json={
                    'hosts': [
                        {'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'},
                        {'hostname': 'CO-ACC-SW-01', 'mgmt_ip': '10.1.100.2'},
                    ],
                    'username': 'admin',
                    'password': 'testpass',
                    'rollback_timeout': 5,
                },
                content_type='application/json',
            )
        data = json.loads(res.data)
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
            res = auth_client.post(
                '/projects/deploy-test/deploy/push',
                json={
                    'hosts': [
                        {'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'},
                    ],
                    'username': 'admin',
                    'password': 'testpass',
                    'rollback_timeout': 5,
                },
                content_type='application/json',
            )
        data = json.loads(res.data)
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

        def mock_run(playbook, inventory, extra_vars, limit_hosts=None):
            captured_extra_vars.update(extra_vars)
            return (True, '', [{
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
            }])

        with patch('app.deploy_routes._run_playbook', side_effect=mock_run):
            auth_client.post(
                '/projects/deploy-test/deploy/push',
                json={
                    'hosts': [{'hostname': 'CO-CORE-SW-01', 'mgmt_ip': '10.1.100.1'}],
                    'username': 'admin',
                    'password': 'testpass',
                    'rollback_timeout': 10,
                },
                content_type='application/json',
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
