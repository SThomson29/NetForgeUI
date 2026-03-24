"""
Flask integration tests for NetForgeUI.

Tests key routes using Flask's test client with a temporary data directory.
Covers: auth, project CRUD, host management, hostvars save/load, generate.
"""

import os
import json
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------

class TestAuth:

    def test_login_page_loads(self, client):
        res = client.get('/login')
        assert res.status_code == 200

    def test_login_success_redirects(self, app, client):
        from app.models import User
        from app.utils import ensure_workspace
        with app.app_context():
            User.create('admin', 'pass123', app, is_admin=True)
            ensure_workspace(app, 'admin')
        res = client.post('/login', data={'username': 'admin', 'password': 'pass123'},
                          follow_redirects=False)
        assert res.status_code == 302

    def test_login_wrong_password(self, app, client):
        from app.models import User
        with app.app_context():
            User.create('admin', 'pass123', app)
        res = client.post('/login', data={'username': 'admin', 'password': 'wrong'},
                          follow_redirects=True)
        assert res.status_code == 200
        assert b'Invalid' in res.data

    def test_logout(self, auth_client):
        res = auth_client.get('/logout', follow_redirects=True)
        assert res.status_code == 200

    def test_projects_page_requires_login(self, client):
        res = client.get('/projects', follow_redirects=False)
        assert res.status_code == 302
        assert b'login' in res.headers['Location'].lower()


# ---------------------------------------------------------------------------
# Projects page
# ---------------------------------------------------------------------------

class TestProjectsPage:

    def test_projects_page_loads(self, auth_client):
        res = auth_client.get('/projects')
        assert res.status_code == 200

    def test_create_project(self, auth_client):
        res = auth_client.post('/projects/new',
                               data={'name': 'my-project'},
                               follow_redirects=True)
        assert res.status_code == 200
        assert b'my-project' in res.data

    def test_create_project_invalid_name(self, auth_client):
        res = auth_client.post('/projects/new',
                               data={'name': 'bad name!'},
                               follow_redirects=True)
        assert res.status_code == 200
        # Should show error flash
        assert b'only contain' in res.data.lower() or b'error' in res.data.lower()

    def test_create_duplicate_project(self, auth_client):
        auth_client.post('/projects/new', data={'name': 'dup'}, follow_redirects=True)
        res = auth_client.post('/projects/new', data={'name': 'dup'},
                               follow_redirects=True)
        assert res.status_code == 200

    def test_delete_project(self, auth_client):
        auth_client.post('/projects/new', data={'name': 'to-delete'},
                         follow_redirects=True)
        res = auth_client.post('/projects/to-delete/delete', follow_redirects=True)
        assert res.status_code == 200
        assert b'to-delete' not in res.data


# ---------------------------------------------------------------------------
# Hosts within a project
# ---------------------------------------------------------------------------

class TestProjectHosts:

    def setup_project(self, auth_client, app):
        """Create a project and set up skeleton dir."""
        auth_client.post('/projects/new', data={'name': 'test-proj'},
                         follow_redirects=True)
        # Create skeleton directory
        skel = os.path.join(app.config['SKELETON_DIR'], 'aoscx')
        os.makedirs(skel, exist_ok=True)
        skeleton_files = {
            'general.yml': 'hostname: __HOSTNAME__\nplatform: aoscx\nprofile: default\n'
                           'config_output_dir: ./generated_configs\ntimezone: Europe/London\n'
                           'ntp_servers: []\naruba:\n  central:\n    disabled: false\n'
                           'dns:\n  domain_name:\n  name_servers: []\n',
            'management.yml': 'management:\n  vrf:\n  source_interface: loopback0\nlocal_users: []\n',
            'banner.yml': 'banner:\n  motd:\n  exec:\n',
            'snmp.yml': 'snmp:\n  version: v2c\n  vrf:\n  system_description:\n'
                        '  location:\n  contact:\n  community:\n  v3_users: []\n',
            'aaa.yml': 'radius_server_key:\nradius_group_name: RADIUS_GROUP\n'
                       'dynamic_authorization: false\nradius_servers: []\n',
            'vrfs.yml': 'vrfs: []\n',
            'vlans.yml': 'vlans: []\n',
            'static_routes.yml': 'static_routes: []\n',
            'interfaces.yml': 'interface_groups: []\nphysical_interfaces: []\n'
                               'lag_interfaces: []\nloopback_interfaces: []\nvlan_interfaces: []\n',
            'routing.yml': 'ospf_instances: []\ndevice_role:\nbgp:\n  asn:\n  router_id:\n  neighbors: []\n',
            'vxlan.yml': 'loopback_interface: loopback1\nloopback_ip:\nospf_area: 0.0.0.0\nvxlan:\n  vni_map: []\n',
        }
        for fname, content in skeleton_files.items():
            with open(os.path.join(skel, fname), 'w') as f:
                f.write(content)

    def test_hosts_page_loads(self, app, auth_client):
        self.setup_project(auth_client, app)
        res = auth_client.get('/projects/test-proj/hosts')
        assert res.status_code == 200

    def test_add_host(self, app, auth_client):
        self.setup_project(auth_client, app)
        res = auth_client.post('/projects/test-proj/hosts/add',
                               data={'hostname': 'Core-01', 'stacking': 'none'},
                               follow_redirects=True)
        assert res.status_code == 200
        assert b'Core-01' in res.data

    def test_add_host_invalid_name(self, app, auth_client):
        self.setup_project(auth_client, app)
        res = auth_client.post('/projects/test-proj/hosts/add',
                               data={'hostname': 'bad host!', 'stacking': 'none'},
                               follow_redirects=True)
        assert res.status_code == 200
        assert b'bad host!' not in res.data

    def test_add_duplicate_host(self, app, auth_client):
        self.setup_project(auth_client, app)
        auth_client.post('/projects/test-proj/hosts/add',
                         data={'hostname': 'Core-01', 'stacking': 'none'},
                         follow_redirects=True)
        res = auth_client.post('/projects/test-proj/hosts/add',
                               data={'hostname': 'Core-01', 'stacking': 'none'},
                               follow_redirects=True)
        assert res.status_code == 200
        # Should show already exists error

    def test_delete_host(self, app, auth_client):
        self.setup_project(auth_client, app)
        auth_client.post('/projects/test-proj/hosts/add',
                         data={'hostname': 'Core-01', 'stacking': 'none'},
                         follow_redirects=True)
        res = auth_client.post('/projects/test-proj/hosts/delete/Core-01',
                               follow_redirects=True)
        assert res.status_code == 200
        assert b'Core-01' not in res.data

    def test_add_vsx_host_creates_vsx_file(self, app, auth_client):
        self.setup_project(auth_client, app)
        # Need vsx.yml in skeleton
        skel = os.path.join(app.config['SKELETON_DIR'], 'aoscx')
        with open(os.path.join(skel, 'vsx.yml'), 'w') as f:
            f.write('vsx:\n  enabled: false\n  role: primary\n')
        auth_client.post('/projects/test-proj/hosts/add',
                         data={'hostname': 'VSX-01', 'stacking': 'vsx'},
                         follow_redirects=True)
        from app.project import project_host_vars_dir
        hvdir = os.path.join(
            project_host_vars_dir(app, 'admin', 'test-proj'), 'VSX-01'
        )
        assert os.path.exists(os.path.join(hvdir, 'vsx.yml'))

    def test_hostname_substituted_in_general_yml(self, app, auth_client):
        self.setup_project(auth_client, app)
        auth_client.post('/projects/test-proj/hosts/add',
                         data={'hostname': 'Core-99', 'stacking': 'none'},
                         follow_redirects=True)
        from app.project import project_host_vars_dir
        hvdir = os.path.join(
            project_host_vars_dir(app, 'admin', 'test-proj'), 'Core-99'
        )
        with open(os.path.join(hvdir, 'general.yml')) as f:
            content = f.read()
        assert 'Core-99' in content
        assert '__HOSTNAME__' not in content


# ---------------------------------------------------------------------------
# Hostvars API
# ---------------------------------------------------------------------------

class TestHostvarsAPI:

    def setup_host(self, app, auth_client, project='test-proj', hostname='Core-01'):
        """Create project and host, return hvdir path."""
        auth_client.post('/projects/new', data={'name': project}, follow_redirects=True)
        skel = os.path.join(app.config['SKELETON_DIR'], 'aoscx')
        os.makedirs(skel, exist_ok=True)
        files = {
            'general.yml': 'hostname: __HOSTNAME__\nplatform: aoscx\nprofile: default\n'
                           'config_output_dir: ./generated_configs\ntimezone: Europe/London\n'
                           'ntp_servers: []\naruba:\n  central:\n    disabled: false\n'
                           'dns:\n  domain_name:\n  name_servers: []\n',
            'management.yml': 'management:\n  vrf:\n  source_interface: loopback0\nlocal_users: []\n',
            'banner.yml': 'banner:\n  motd:\n  exec:\n',
            'snmp.yml': 'snmp:\n  version: v2c\n  vrf:\n  system_description:\n'
                        '  location:\n  contact:\n  community:\n  v3_users: []\n',
            'aaa.yml': 'radius_server_key:\nradius_group_name: RADIUS_GROUP\n'
                       'dynamic_authorization: false\nradius_servers: []\n',
            'vrfs.yml': 'vrfs: []\n',
            'vlans.yml': 'vlans: []\n',
            'static_routes.yml': 'static_routes: []\n',
            'interfaces.yml': 'interface_groups: []\nphysical_interfaces: []\n'
                               'lag_interfaces: []\nloopback_interfaces: []\nvlan_interfaces: []\n',
            'routing.yml': 'ospf_instances: []\ndevice_role:\nbgp:\n  asn:\n  router_id:\n  neighbors: []\n',
            'vxlan.yml': 'loopback_interface: loopback1\nloopback_ip:\nospf_area: 0.0.0.0\nvxlan:\n  vni_map: []\n',
        }
        for fname, content in files.items():
            with open(os.path.join(skel, fname), 'w') as f:
                f.write(content)
        auth_client.post(f'/projects/{project}/hosts/add',
                         data={'hostname': hostname, 'stacking': 'none'},
                         follow_redirects=True)
        from app.project import project_host_vars_dir
        return os.path.join(project_host_vars_dir(app, 'admin', project), hostname)

    def test_get_state_returns_json(self, app, auth_client):
        self.setup_host(app, auth_client)
        res = auth_client.get('/projects/test-proj/api/hostvars/Core-01/state')
        assert res.status_code == 200
        data = json.loads(res.data)
        assert 'hostname' in data
        assert data['hostname'] == 'Core-01'

    def test_get_state_404_for_missing_host(self, app, auth_client):
        auth_client.post('/projects/new', data={'name': 'test-proj'}, follow_redirects=True)
        res = auth_client.get('/projects/test-proj/api/hostvars/NoSuchHost/state')
        assert res.status_code == 404

    def test_save_all_hostvars(self, app, auth_client):
        hvdir = self.setup_host(app, auth_client)
        payload = [
            {'filename': 'general.yml',
             'content': 'hostname: Core-01\nplatform: aoscx\nprofile: default\n'
                        'config_output_dir: ./generated_configs\ntimezone: UTC\n'
                        'ntp_servers:\n  - 10.0.0.1\naruba:\n  central:\n    disabled: false\n'
                        'dns:\n  domain_name: test.local\n  name_servers: []\n'},
        ]
        res = auth_client.post(
            '/projects/test-proj/api/hostvars/Core-01/save_all',
            json=payload,
            content_type='application/json',
        )
        assert res.status_code == 200
        data = json.loads(res.data)
        assert data['ok'] is True
        assert data['saved'] == 1
        # Verify the file was actually written
        with open(os.path.join(hvdir, 'general.yml')) as f:
            content = f.read()
        assert 'timezone: UTC' in content

    def test_save_all_rejects_unknown_filename(self, app, auth_client):
        self.setup_host(app, auth_client)
        payload = [{'filename': '../../etc/passwd', 'content': 'bad'}]
        res = auth_client.post(
            '/projects/test-proj/api/hostvars/Core-01/save_all',
            json=payload,
            content_type='application/json',
        )
        assert res.status_code == 400

    def test_save_then_reload_round_trip(self, app, auth_client):
        """Save modified general.yml then reload state and verify values."""
        self.setup_host(app, auth_client)
        new_content = ('hostname: Core-01\nplatform: aoscx\nprofile: default\n'
                       'config_output_dir: ./generated_configs\ntimezone: Europe/Paris\n'
                       'ntp_servers:\n  - 10.0.0.1\n  - 10.0.0.2\naruba:\n  central:\n'
                       '    disabled: true\ndns:\n  domain_name: mynet.local\n'
                       '  name_servers:\n    - 1.1.1.1\n')
        auth_client.post(
            '/projects/test-proj/api/hostvars/Core-01/save_all',
            json=[{'filename': 'general.yml', 'content': new_content}],
            content_type='application/json',
        )
        res = auth_client.get('/projects/test-proj/api/hostvars/Core-01/state')
        state = json.loads(res.data)
        assert state['timezone'] == 'Europe/Paris'
        assert state['ntpServers'] == ['10.0.0.1', '10.0.0.2']
        assert state['centralDisabled'] is True
        assert state['dnsDomain'] == 'mynet.local'


# ---------------------------------------------------------------------------
# Resources API
# ---------------------------------------------------------------------------

class TestResourcesAPI:

    def setup_project(self, auth_client):
        auth_client.post('/projects/new', data={'name': 'res-proj'}, follow_redirects=True)

    def test_add_unique_pool(self, auth_client):
        self.setup_project(auth_client)
        res = auth_client.post(
            '/projects/res-proj/api/pools',
            json={'name': 'Loopbacks', 'type': 'unique',
                  'subnet': '10.255.0.0/24', 'prefix': 32},
            content_type='application/json',
        )
        assert res.status_code == 200
        data = json.loads(res.data)
        assert data['ok'] is True
        assert 'id' in data

    def test_add_vlan_supernet_pool(self, auth_client):
        self.setup_project(auth_client)
        res = auth_client.post(
            '/projects/res-proj/api/pools',
            json={'name': 'Core VLANs', 'type': 'vlan_supernet',
                  'subnet': '10.100.0.0/16', 'carve_prefix': 24},
            content_type='application/json',
        )
        assert res.status_code == 200
        assert json.loads(res.data)['ok'] is True

    def test_remove_pool(self, auth_client):
        self.setup_project(auth_client)
        res = auth_client.post(
            '/projects/res-proj/api/pools',
            json={'name': 'Loopbacks', 'type': 'unique',
                  'subnet': '10.255.0.0/24', 'prefix': 32},
            content_type='application/json',
        )
        pool_id = json.loads(res.data)['id']
        res = auth_client.delete(f'/projects/res-proj/api/pools/{pool_id}')
        assert res.status_code == 200
        assert json.loads(res.data)['ok'] is True

    def test_save_conventions(self, auth_client):
        self.setup_project(auth_client)
        res = auth_client.post(
            '/projects/res-proj/api/conventions',
            json={'svi': {'gateway_offset': 2, 'active_gateway_offset': 253,
                          'reserved_from_start': 5}},
            content_type='application/json',
        )
        assert res.status_code == 200
        res2 = auth_client.get('/projects/res-proj/api/conventions')
        conv = json.loads(res2.data)
        assert conv['svi']['gateway_offset'] == 2

    def test_save_and_get_common(self, auth_client):
        self.setup_project(auth_client)
        auth_client.post(
            '/projects/res-proj/api/common',
            json={'dns_servers': ['10.0.0.53'], 'ntp_servers': ['10.0.0.1'],
                  'dhcp_servers': [], 'radius_servers': [], 'syslog_servers': []},
            content_type='application/json',
        )
        res = auth_client.get('/projects/res-proj/api/common')
        common = json.loads(res.data)
        assert common['dns_servers'] == ['10.0.0.53']

    def test_allocations_endpoint(self, auth_client):
        self.setup_project(auth_client)
        res = auth_client.get('/projects/res-proj/api/allocations')
        assert res.status_code == 200
        data = json.loads(res.data)
        assert 'unique' in data
        assert 'point_to_point' in data


# ---------------------------------------------------------------------------
# Generate page
# ---------------------------------------------------------------------------

class TestGenerate:

    def setup_project_with_host(self, app, auth_client):
        auth_client.post('/projects/new', data={'name': 'gen-proj'}, follow_redirects=True)
        skel = os.path.join(app.config['SKELETON_DIR'], 'aoscx')
        os.makedirs(skel, exist_ok=True)
        for fname, content in [
            ('general.yml', 'hostname: __HOSTNAME__\nplatform: aoscx\nprofile: default\n'
                            'config_output_dir: ./generated_configs\ntimezone: Europe/London\n'
                            'ntp_servers: []\naruba:\n  central:\n    disabled: false\n'
                            'dns:\n  domain_name:\n  name_servers: []\n'),
            ('interfaces.yml', 'interface_groups: []\nphysical_interfaces: []\n'
                               'lag_interfaces: []\nloopback_interfaces: []\nvlan_interfaces: []\n'),
        ]:
            with open(os.path.join(skel, fname), 'w') as f:
                f.write(content)
        # Add remaining skeleton files
        for fname in ['management.yml', 'banner.yml', 'snmp.yml', 'aaa.yml',
                      'vrfs.yml', 'vlans.yml', 'static_routes.yml',
                      'routing.yml', 'vxlan.yml']:
            fpath = os.path.join(skel, fname)
            if not os.path.exists(fpath):
                with open(fpath, 'w') as f:
                    f.write('')
        auth_client.post('/projects/gen-proj/hosts/add',
                         data={'hostname': 'Core-01', 'stacking': 'none'},
                         follow_redirects=True)

    def test_generate_page_loads(self, app, auth_client):
        self.setup_project_with_host(app, auth_client)
        res = auth_client.get('/projects/gen-proj/generate')
        assert res.status_code == 200

    def test_run_generate_returns_job_id(self, app, auth_client):
        self.setup_project_with_host(app, auth_client)
        # Mock subprocess.run to avoid needing Ansible
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = 'Playbook complete.\n'
        with patch('app.projects.subprocess.run', return_value=mock_result):
            res = auth_client.post(
                '/projects/gen-proj/generate/run',
                json={'limit': '', 'tags': ''},
                content_type='application/json',
            )
        assert res.status_code == 200
        data = json.loads(res.data)
        assert 'job_id' in data

    def test_generate_status_endpoint(self, app, auth_client):
        self.setup_project_with_host(app, auth_client)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = 'Done\n'
        with patch('app.projects.subprocess.run', return_value=mock_result):
            run_res = auth_client.post(
                '/projects/gen-proj/generate/run',
                json={'limit': 'Core-01', 'tags': ''},
                content_type='application/json',
            )
        job_id = json.loads(run_res.data)['job_id']

        import time
        time.sleep(0.2)  # Give the thread a moment to complete

        status_res = auth_client.get(f'/projects/gen-proj/generate/status/{job_id}')
        assert status_res.status_code == 200
        status = json.loads(status_res.data)
        assert status['status'] in ('running', 'done', 'failed')

    def test_generate_status_404_for_unknown_job(self, app, auth_client):
        self.setup_project_with_host(app, auth_client)
        res = auth_client.get('/projects/gen-proj/generate/status/no-such-job')
        assert res.status_code == 404

    def test_download_all_404_when_no_configs(self, app, auth_client):
        self.setup_project_with_host(app, auth_client)
        res = auth_client.get('/projects/gen-proj/download_all')
        assert res.status_code == 404

    def test_download_all_returns_zip(self, app, auth_client):
        self.setup_project_with_host(app, auth_client)
        from app.project import project_generated_configs_dir
        gcdir = project_generated_configs_dir(app, 'admin', 'gen-proj')
        os.makedirs(gcdir, exist_ok=True)
        with open(os.path.join(gcdir, 'Core-01_FULL.ios'), 'w') as f:
            f.write('! config\nhostname Core-01\n')
        res = auth_client.get('/projects/gen-proj/download_all')
        assert res.status_code == 200
        assert res.content_type == 'application/zip'

    def test_delete_config(self, app, auth_client):
        self.setup_project_with_host(app, auth_client)
        from app.project import project_generated_configs_dir
        gcdir = project_generated_configs_dir(app, 'admin', 'gen-proj')
        os.makedirs(gcdir, exist_ok=True)
        fname = 'Core-01_FULL.ios'
        with open(os.path.join(gcdir, fname), 'w') as f:
            f.write('! config\n')
        res = auth_client.post(f'/projects/gen-proj/generate/delete/{fname}')
        assert res.status_code == 200
        assert not os.path.exists(os.path.join(gcdir, fname))
