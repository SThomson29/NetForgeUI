"""
Shared pytest fixtures for all NetForgeUI tests.
"""

import os
import sys
import json
import shutil
import tempfile
import pytest

# Ensure the service root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ---------------------------------------------------------------------------
# Temporary data directory
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_data_dir():
    """Create a temporary data directory and clean up after each test."""
    d = tempfile.mkdtemp(prefix='netforgeui_test_')
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Minimal Flask app
# ---------------------------------------------------------------------------

@pytest.fixture
def app(tmp_data_dir, tmp_path):
    """Create a minimal Flask app wired to a temp data directory."""
    from flask import Flask
    from flask_login import LoginManager
    from app.models import User, init_db

    flask_app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), '..', 'app', 'templates'),
    )
    flask_app.config.update(
        SECRET_KEY='test-secret',
        TESTING=True,
        DATA_DIR=os.path.join(tmp_data_dir, 'users'),
        DATABASE=os.path.join(tmp_data_dir, 'users.db'),
        CONFIGGEN_REPO=str(tmp_path / 'configgen'),
        CONFIGGEN_REPO_URL='',
        SKELETON_DIR=str(tmp_path / 'configgen' / 'inventory' / 'skeleton'),
        PLAYBOOK=str(tmp_path / 'configgen' / 'playbooks' / 'generate_configs.yml'),
        ROLES_PATH=str(tmp_path / 'configgen' / 'roles'),
        ANSIBLE_BIN='ansible-playbook',
        WTF_CSRF_ENABLED=False,
    )

    login_manager = LoginManager()
    login_manager.init_app(flask_app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.get(int(user_id), flask_app)

    init_db(flask_app)

    from app.auth import auth_bp
    from app.admin import admin_bp
    from app.projects import projects_bp

    flask_app.register_blueprint(auth_bp)
    flask_app.register_blueprint(admin_bp)
    flask_app.register_blueprint(projects_bp)

    yield flask_app


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture
def auth_client(app, client):
    """Logged-in Flask test client (admin user)."""
    from app.models import User
    from app.utils import ensure_workspace

    with app.app_context():
        User.create('admin', 'testpass', app, is_admin=True)
        ensure_workspace(app, 'admin')

    client.post('/login', data={'username': 'admin', 'password': 'testpass'},
                follow_redirects=True)
    return client


# ---------------------------------------------------------------------------
# Skeleton directory fixture
# ---------------------------------------------------------------------------

SKELETON_FILES = {
    'general.yml': """\
hostname: __HOSTNAME__
platform: aoscx
profile: default
config_output_dir: ./generated_configs
timezone: Europe/London
ntp_servers: []
aruba:
  central:
    disabled: false
dns:
  domain_name:
  name_servers: []
""",
    'management.yml': """\
management:
  vrf:
  source_interface: loopback0
local_users: []
""",
    'banner.yml': """\
banner:
  motd:
  exec:
""",
    'snmp.yml': """\
snmp:
  version: v2c
  vrf:
  system_description:
  location:
  contact:
  community:
  v3_users: []
""",
    'aaa.yml': """\
radius_server_key:
radius_group_name: RADIUS_GROUP
dynamic_authorization: false
radius_servers: []
""",
    'vrfs.yml': "vrfs: []\n",
    'vlans.yml': "vlans: []\n",
    'static_routes.yml': "static_routes: []\n",
    'interfaces.yml': """\
interface_groups: []
physical_interfaces: []
lag_interfaces: []
loopback_interfaces: []
vlan_interfaces: []
""",
    'routing.yml': """\
ospf_instances: []
device_role:
bgp:
  asn:
  router_id:
  neighbors: []
""",
    'vxlan.yml': """\
loopback_interface: loopback1
loopback_ip:
ospf_area: 0.0.0.0
vxlan:
  vni_map: []
""",
    'vsx.yml': """\
vsx:
  enabled: false
  role: primary
  system_mac:
  isl_port: lag256
  keepalive:
    peer_ip:
    src_ip:
    vrf:
  peer_ip:
""",
    'vsf.yml': """\
vsf:
  enabled: false
  interface1:
  interface2:
  members: []
""",
}


@pytest.fixture
def skeleton_dir(tmp_path):
    """Create a minimal skeleton directory matching the real structure."""
    skel = tmp_path / 'configgen' / 'inventory' / 'skeleton' / 'aoscx'
    skel.mkdir(parents=True)
    for fname, content in SKELETON_FILES.items():
        (skel / fname).write_text(content)
    return str(skel)


@pytest.fixture
def app_with_skeleton(app, skeleton_dir):
    """App fixture that also has a skeleton directory available."""
    skel_parent = os.path.dirname(skeleton_dir)
    app.config['SKELETON_DIR'] = skel_parent
    return app


# ---------------------------------------------------------------------------
# Project helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_project_config():
    """A minimal valid project config dict."""
    return {
        'name': 'test-project',
        'created': '2026-01-01',
        'conventions': {
            'svi': {
                'gateway_offset': 1,
                'active_gateway_offset': 254,
                'reserved_from_start': 10,
            }
        },
        'pools': [],
        'common': {
            'dns_servers': [],
            'ntp_servers': [],
            'dhcp_servers': [],
            'radius_servers': [],
            'syslog_servers': [],
        }
    }


@pytest.fixture
def sample_pools():
    """Sample pool definitions for testing."""
    return {
        'unique': {
            'id': 'pool_loopbacks',
            'name': 'Loopbacks',
            'type': 'unique',
            'subnet': '10.255.0.0/24',
            'prefix': 32,
        },
        'ptp': {
            'id': 'pool_ptp',
            'name': 'Point-to-point',
            'type': 'point_to_point',
            'subnet': '10.254.0.0/24',
        },
        'vlan': {
            'id': 'pool_vlans',
            'name': 'Core VLANs',
            'type': 'vlan_supernet',
            'subnet': '10.100.0.0/16',
            'carve_prefix': 24,
        },
    }
