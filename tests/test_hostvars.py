"""
Unit tests for app/hostvars.py

Covers: _parse_state correctly parses all YAML sections,
        default values, edge cases, and round-trip fidelity.
"""

import os
import pytest
import tempfile
import shutil

from app.hostvars import _parse_state, _load_yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_files(base_dir, files):
    """Write a dict of {filename: content} to base_dir."""
    os.makedirs(base_dir, exist_ok=True)
    for fname, content in files.items():
        with open(os.path.join(base_dir, fname), 'w') as f:
            f.write(content)


@pytest.fixture
def hvdir(tmp_path):
    """Return a temp host_vars directory pre-populated with minimal skeleton files."""
    d = tmp_path / 'host_vars' / 'test-switch'
    d.mkdir(parents=True)
    write_files(str(d), {
        'general.yml': """\
hostname: test-switch
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
        'banner.yml': "banner:\n  motd:\n  exec:\n",
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
    })
    return str(d)


# ---------------------------------------------------------------------------
# General section
# ---------------------------------------------------------------------------

class TestGeneral:

    def test_hostname_parsed(self, hvdir):
        state = _parse_state(hvdir)
        assert state['hostname'] == 'test-switch'

    def test_platform_default(self, hvdir):
        state = _parse_state(hvdir)
        assert state['platform'] == 'aoscx'

    def test_empty_ntp_servers(self, hvdir):
        state = _parse_state(hvdir)
        assert state['ntpServers'] == []

    def test_ntp_servers_populated(self, hvdir):
        write_files(hvdir, {'general.yml': """\
hostname: test-switch
platform: aoscx
profile: default
timezone: Europe/London
ntp_servers:
  - 10.0.0.1
  - 10.0.0.2
aruba:
  central:
    disabled: false
dns:
  domain_name: example.com
  name_servers:
    - 8.8.8.8
"""})
        state = _parse_state(hvdir)
        assert state['ntpServers'] == ['10.0.0.1', '10.0.0.2']
        assert state['dnsServers'] == ['8.8.8.8']
        assert state['dnsDomain'] == 'example.com'

# ---------------------------------------------------------------------------
# Management section
# ---------------------------------------------------------------------------

class TestManagement:

    def test_mgmt_source_interface_default(self, hvdir):
        state = _parse_state(hvdir)
        assert state['mgmtSrc'] == 'loopback0'

    def test_local_users_empty(self, hvdir):
        state = _parse_state(hvdir)
        assert state['localUsers'] == []

    def test_local_users_parsed(self, hvdir):
        write_files(hvdir, {'management.yml': """\
management:
  vrf: mgmt
  source_interface: loopback0
local_users:
  - username: admin
    group: administrators
    password: secret123
  - username: monitor
    group: operators
    password: watch456
"""})
        state = _parse_state(hvdir)
        assert len(state['localUsers']) == 2
        assert state['localUsers'][0]['username'] == 'admin'
        assert state['localUsers'][1]['username'] == 'monitor'
        assert state['mgmtVrf'] == 'mgmt'


# ---------------------------------------------------------------------------
# SNMP section
# ---------------------------------------------------------------------------

class TestSNMP:

    def test_snmp_version_default(self, hvdir):
        state = _parse_state(hvdir)
        assert state['snmpVersion'] == 'v2c'

    def test_snmp_v3_users_empty(self, hvdir):
        state = _parse_state(hvdir)
        assert state['v3Users'] == []

    def test_snmp_populated(self, hvdir):
        write_files(hvdir, {'snmp.yml': """\
snmp:
  version: v2c
  vrf: mgmt
  system_description: Core Switch
  location: DataCentre
  contact: noc@example.com
  community: public
  v3_users: []
"""})
        state = _parse_state(hvdir)
        assert state['snmpLocation'] == 'DataCentre'
        assert state['snmpCommunity'] == 'public'
        assert state['snmpContact'] == 'noc@example.com'


# ---------------------------------------------------------------------------
# Syslog / sFlow section
# ---------------------------------------------------------------------------

class TestSyslog:

    def test_syslog_absent_defaults_empty(self, hvdir):
        state = _parse_state(hvdir)
        assert state['syslogServer'] == ''
        assert state['syslogSeverity'] == ''

    def test_syslog_skeleton_defaults_empty(self, hvdir):
        write_files(hvdir, {'syslog.yml': """\
syslog:
  server: ""
  severity: ""
"""})
        state = _parse_state(hvdir)
        assert state['syslogServer'] == ''
        assert state['syslogSeverity'] == ''

    def test_syslog_populated(self, hvdir):
        write_files(hvdir, {'syslog.yml': """\
syslog:
  server: "10.1.1.1"
  severity: "info"
"""})
        state = _parse_state(hvdir)
        assert state['syslogServer'] == '10.1.1.1'
        assert state['syslogSeverity'] == 'info'


class TestSflow:

    def test_sflow_absent_defaults_empty(self, hvdir):
        state = _parse_state(hvdir)
        assert state['sflowCollectorIp'] == ''
        assert state['sflowAgentIp'] == ''

    def test_sflow_skeleton_defaults_empty(self, hvdir):
        write_files(hvdir, {'sflow.yml': """\
sflow:
  collector_ip: ""
  agent_ip: ""
"""})
        state = _parse_state(hvdir)
        assert state['sflowCollectorIp'] == ''
        assert state['sflowAgentIp'] == ''

    def test_sflow_populated(self, hvdir):
        write_files(hvdir, {'sflow.yml': """\
sflow:
  collector_ip: "10.2.2.2"
  agent_ip: "10.0.0.1"
"""})
        state = _parse_state(hvdir)
        assert state['sflowCollectorIp'] == '10.2.2.2'
        assert state['sflowAgentIp'] == '10.0.0.1'


# ---------------------------------------------------------------------------
# AAA section
# ---------------------------------------------------------------------------

class TestAAA:

    def test_radius_servers_empty(self, hvdir):
        state = _parse_state(hvdir)
        assert state['radiusServers'] == []

    def test_radius_servers_parsed(self, hvdir):
        write_files(hvdir, {'aaa.yml': """\
radius_server_key: secretkey
radius_group_name: RADIUS_GRP
dynamic_authorization: true
radius_servers:
  - address: 10.0.0.100
    key: serverkey1
  - address: 10.0.0.101
    key: serverkey2
"""})
        state = _parse_state(hvdir)
        assert len(state['radiusServers']) == 2
        assert state['radiusServers'][0]['address'] == '10.0.0.100'
        assert state['radiusServerKey'] == 'secretkey'
        assert state['dynAuth'] is True


# ---------------------------------------------------------------------------
# VRFs and VLANs
# ---------------------------------------------------------------------------

class TestVrfsVlans:

    def test_vrfs_empty(self, hvdir):
        state = _parse_state(hvdir)
        assert state['vrfs'] == []

    def test_vrfs_parsed(self, hvdir):
        write_files(hvdir, {'vrfs.yml': """\
vrfs:
  - name: MGMT
  - name: PROD
"""})
        state = _parse_state(hvdir)
        assert len(state['vrfs']) == 2
        assert state['vrfs'][0]['name'] == 'MGMT'

    def test_vlans_empty(self, hvdir):
        state = _parse_state(hvdir)
        assert state['vlans'] == []

    def test_vlans_parsed(self, hvdir):
        write_files(hvdir, {'vlans.yml': """\
vlans:
  - id: 100
    name: Management
  - id: 200
    name: Production
  - id: 999
    name: Native
"""})
        state = _parse_state(hvdir)
        assert len(state['vlans']) == 3
        assert state['vlans'][0]['id'] == '100'
        assert state['vlans'][1]['name'] == 'Production'


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------

class TestInterfaces:

    def test_all_interface_types_empty(self, hvdir):
        state = _parse_state(hvdir)
        assert state['physical'] == []
        assert state['lags'] == []
        assert state['loopbacks'] == []
        assert state['vlanIfs'] == []

    def test_physical_interface_access_parsed(self, hvdir):
        write_files(hvdir, {'interfaces.yml': """\
interface_groups: []
physical_interfaces:
  - name: "1/1/1"
    description: "Uplink"
    admin: up
    mtu: 9198
    routed: false
    port_type: access
    access_vlan: "100"
    trunk_allowed_vlans: ""
    trunk_native_vlan: 1
lag_interfaces: []
loopback_interfaces: []
vlan_interfaces: []
"""})
        state = _parse_state(hvdir)
        assert len(state['physical']) == 1
        p = state['physical'][0]
        assert p['name'] == '1/1/1'
        assert p['port_type'] == 'access'
        assert p['access_vlan'] == '100'

    def test_physical_interface_routed_parsed(self, hvdir):
        write_files(hvdir, {'interfaces.yml': """\
interface_groups: []
physical_interfaces:
  - name: "1/1/2"
    description: "P2P Link"
    admin: up
    mtu: 9198
    routed: true
    port_type: routed
    ip_address: "10.254.0.0"
    ip_prefix: "31"
    vrf: ""
    ospf_area: "0.0.0.0"
    ospf_process_id: 1
lag_interfaces: []
loopback_interfaces: []
vlan_interfaces: []
"""})
        state = _parse_state(hvdir)
        p = state['physical'][0]
        assert p['port_type'] == 'routed'
        assert p['ip_address'] == '10.254.0.0'
        assert p['ip_prefix'] == '31'

    def test_routed_derived_from_routed_flag(self, hvdir):
        """port_type should be derived from routed:true if port_type not set."""
        write_files(hvdir, {'interfaces.yml': """\
interface_groups: []
physical_interfaces:
  - name: "1/1/2"
    admin: up
    mtu: 9198
    routed: true
    ip_address: "10.254.0.0"
    ip_prefix: "31"
lag_interfaces: []
loopback_interfaces: []
vlan_interfaces: []
"""})
        state = _parse_state(hvdir)
        p = state['physical'][0]
        assert p['port_type'] == 'routed'

    def test_authenticated_port_mtu_not_required(self, hvdir):
        """Authenticated ports carry no mtu; parsing must still succeed."""
        write_files(hvdir, {'interfaces.yml': """\
interface_groups: []
physical_interfaces:
  - name: "1/1/1"
    description: "NAC port"
    admin: up
    routed: false
    port_type: authenticated
    auth_default_vlan: "999"
lag_interfaces: []
loopback_interfaces: []
vlan_interfaces: []
"""})
        state = _parse_state(hvdir)
        p = state['physical'][0]
        assert p['port_type'] == 'authenticated'
        assert p['auth_default_vlan'] == '999'

    def test_lag_has_no_mtu(self, hvdir):
        """MTU is not applicable to a LAG on AOS-CX; it must not reach form state."""
        write_files(hvdir, {'interfaces.yml': """\
interface_groups: []
physical_interfaces: []
lag_interfaces:
  - name: lag1
    admin: up
    mtu: 9198
    lacp_mode: active
    routed: false
loopback_interfaces: []
vlan_interfaces: []
"""})
        state = _parse_state(hvdir)
        assert 'mtu' not in state['lags'][0]

    def test_lag_parsed(self, hvdir):
        write_files(hvdir, {'interfaces.yml': """\
interface_groups: []
physical_interfaces: []
lag_interfaces:
  - name: lag1
    description: "VSX ISL"
    admin: up
    mtu: 9198
    lacp_mode: active
    routed: false
    port_type: access
    trunk_allowed_vlans: "all"
    trunk_native_vlan: 1
loopback_interfaces: []
vlan_interfaces: []
"""})
        state = _parse_state(hvdir)
        assert len(state['lags']) == 1
        assert state['lags'][0]['name'] == 'lag1'
        assert state['lags'][0]['lacp_mode'] == 'active'

    def test_loopback_parsed(self, hvdir):
        write_files(hvdir, {'interfaces.yml': """\
interface_groups: []
physical_interfaces: []
lag_interfaces: []
loopback_interfaces:
  - name: loopback0
    description: "Router ID"
    ip_address: "10.255.0.1"
    ip_prefix: "32"
    vrf: ""
    ospf_area: "0.0.0.0"
    ospf_process_id: 1
vlan_interfaces: []
"""})
        state = _parse_state(hvdir)
        assert len(state['loopbacks']) == 1
        lo = state['loopbacks'][0]
        assert lo['ip_address'] == '10.255.0.1'
        assert lo['ip_prefix'] == '32'

    def test_vlan_interface_parsed(self, hvdir):
        write_files(hvdir, {'interfaces.yml': """\
interface_groups: []
physical_interfaces: []
lag_interfaces: []
loopback_interfaces: []
vlan_interfaces:
  - name: vlan100
    description: "Management"
    admin: up
    vrf: MGMT
    ip_address: "10.100.0.1"
    ip_prefix: 24
    helper_addresses:
      - 10.0.0.67
    ospf_area: "0.0.0.0"
    ospf_process_id: 1
    ospf_passive: true
    active_gateway_ip: "10.100.0.254"
    active_gateway_mac: "00:02:00:00:00:01"
    mtu_jumbo: false
"""})
        state = _parse_state(hvdir)
        assert len(state['vlanIfs']) == 1
        vi = state['vlanIfs'][0]
        assert vi['ip_address'] == '10.100.0.1'
        assert vi['active_gw_ip'] == '10.100.0.254'
        assert vi['helper_addresses'] == ['10.0.0.67']
        assert vi['ospf_passive'] is True

    def test_helper_addresses_empty_by_default(self, hvdir):
        write_files(hvdir, {'interfaces.yml': """\
interface_groups: []
physical_interfaces: []
lag_interfaces: []
loopback_interfaces: []
vlan_interfaces:
  - name: vlan100
    ip_address: "10.100.0.1"
    ip_prefix: 24
    helper_addresses: []
"""})
        state = _parse_state(hvdir)
        assert state['vlanIfs'][0]['helper_addresses'] == []


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

class TestRouting:

    def test_ospf_instances_empty(self, hvdir):
        state = _parse_state(hvdir)
        assert state['ospfInstances'] == []

    def test_ospf_instance_parsed(self, hvdir):
        write_files(hvdir, {'routing.yml': """\
ospf_instances:
  - enabled: true
    process_id: 1
    router_id: "10.255.0.1"
    vrf: ""
    areas:
      - area_id: "0.0.0.0"
  - enabled: false
    process_id: 2
    router_id: ""
    vrf: PROD
    areas: []
device_role:
bgp:
  asn:
  router_id:
  neighbors: []
"""})
        state = _parse_state(hvdir)
        assert len(state['ospfInstances']) == 2
        assert state['ospfInstances'][0]['process_id'] == '1'
        assert state['ospfInstances'][0]['router_id'] == '10.255.0.1'
        assert len(state['ospfInstances'][0]['areas']) == 1

    def test_bgp_parsed(self, hvdir):
        write_files(hvdir, {'routing.yml': """\
ospf_instances: []
device_role: spine
bgp:
  asn: "65001"
  router_id: "10.255.0.1"
  neighbors:
    - ip: "10.255.0.2"
      remote_asn: "65002"
      update_source: loopback0
"""})
        state = _parse_state(hvdir)
        assert state['bgpAsn'] == '65001'
        assert state['bgpRid'] == '10.255.0.1'
        assert len(state['bgpNeighbors']) == 1
        assert state['bgpNeighbors'][0]['ip'] == '10.255.0.2'


# ---------------------------------------------------------------------------
# VXLAN
# ---------------------------------------------------------------------------

class TestVXLAN:

    def test_vxlan_defaults(self, hvdir):
        state = _parse_state(hvdir)
        assert state['vtepLoopback'] == 'loopback1'
        assert state['vtepIp'] == ''

    def test_vxlan_populated(self, hvdir):
        write_files(hvdir, {'vxlan.yml': """\
loopback_interface: loopback2
loopback_ip: "10.253.0.1"
ospf_area: "0.0.0.0"
vxlan:
  vni_map:
    - vni: 10100
      vlan: 100
    - vni: 10200
      vlan: 200
"""})
        state = _parse_state(hvdir)
        assert state['vtepLoopback'] == 'loopback2'
        assert state['vtepIp'] == '10.253.0.1'
        assert len(state['vnis']) == 2
        assert state['vnis'][0]['vni'] == '10100'


# ---------------------------------------------------------------------------
# Stacking
# ---------------------------------------------------------------------------

class TestStacking:

    def test_no_stacking_file_means_none(self, hvdir):
        state = _parse_state(hvdir)
        assert state['stackingType'] == 'none'

    def test_vsx_file_sets_stacking_type(self, hvdir):
        write_files(hvdir, {'vsx.yml': """\
vsx:
  enabled: true
  role: primary
  system_mac: "00:01:00:00:00:01"
  isl_port: lag256
  keepalive:
    peer_ip: 192.168.1.2
    src_ip: 192.168.1.1
    vrf: mgmt
  peer_ip: 10.255.0.2
"""})
        state = _parse_state(hvdir)
        assert state['stackingType'] == 'vsx'
        assert state['vsxEnabled'] is True
        assert state['vsxRole'] == 'primary'
        assert state['vsxMac'] == '00:01:00:00:00:01'

    def test_vsf_file_sets_stacking_type(self, hvdir):
        write_files(hvdir, {'vsf.yml': """\
vsf:
  enabled: true
  interface1: "1/1/50"
  interface2: "1/1/51"
  members:
    - id: 1
    - id: 2
"""})
        state = _parse_state(hvdir)
        assert state['stackingType'] == 'vsf'
        assert state['vsfEnabled'] is True
        assert state['vsfIf1'] == '1/1/50'
        assert len(state['vsfMembers']) == 2


# ---------------------------------------------------------------------------
# Missing files — should not raise
# ---------------------------------------------------------------------------

class TestMissingFiles:

    def test_missing_syslog_does_not_raise(self, hvdir):
        state = _parse_state(hvdir)
        assert state['syslogServer'] == ''

    def test_missing_sflow_does_not_raise(self, hvdir):
        state = _parse_state(hvdir)
        assert state['sflowCollectorIp'] == ''

    def test_missing_snmp_does_not_raise(self, hvdir):
        os.remove(os.path.join(hvdir, 'snmp.yml'))
        state = _parse_state(hvdir)
        assert state['snmpVersion'] == 'v2c'

    def test_missing_vxlan_does_not_raise(self, hvdir):
        os.remove(os.path.join(hvdir, 'vxlan.yml'))
        state = _parse_state(hvdir)
        assert state['vnis'] == []

    def test_empty_directory_does_not_raise(self, tmp_path):
        empty = str(tmp_path / 'empty')
        os.makedirs(empty)
        state = _parse_state(empty)
        assert state['hostname'] == ''
        assert state['ospfInstances'] == []


# ---------------------------------------------------------------------------
# OSPF authentication key
# ---------------------------------------------------------------------------

class TestOspfAuthKey:

    def test_absent_key_parses_as_empty(self, hvdir):
        """Interfaces written before this field existed must still load."""
        write_files(hvdir, {'interfaces.yml': """\
interface_groups: []
physical_interfaces:
  - name: "1/1/1"
    routed: true
    port_type: routed
    ip_address: "10.0.0.0"
    ip_prefix: "31"
    ospf_area: "0.0.0.0"
    ospf_process_id: 1
lag_interfaces: []
loopback_interfaces: []
vlan_interfaces: []
"""})
        state = _parse_state(hvdir)
        assert state['physical'][0]['ospf_auth_key'] == ''

    def test_key_is_parsed_for_each_interface_type(self, hvdir):
        write_files(hvdir, {'interfaces.yml': """\
interface_groups: []
physical_interfaces:
  - name: "1/1/1"
    routed: true
    port_type: routed
    ospf_area: "0.0.0.0"
    ospf_auth_key: "PhyKey"
lag_interfaces:
  - name: lag1
    routed: true
    ospf_area: "0.0.0.0"
    ospf_auth_key: "LagKey"
loopback_interfaces: []
vlan_interfaces:
  - name: vlan100
    ospf_area: "0.0.0.0"
    ospf_auth_key: "SviKey"
"""})
        state = _parse_state(hvdir)
        assert state['physical'][0]['ospf_auth_key'] == 'PhyKey'
        assert state['lags'][0]['ospf_auth_key'] == 'LagKey'
        assert state['vlanIfs'][0]['ospf_auth_key'] == 'SviKey'

    def test_loopbacks_have_no_auth_key(self, hvdir):
        """Not applicable to loopbacks — must not appear in form state."""
        write_files(hvdir, {'interfaces.yml': """\
interface_groups: []
physical_interfaces: []
lag_interfaces: []
loopback_interfaces:
  - name: loopback0
    ip_address: "10.255.0.1"
    ip_prefix: "32"
    ospf_area: "0.0.0.0"
vlan_interfaces: []
"""})
        state = _parse_state(hvdir)
        assert 'ospf_auth_key' not in state['loopbacks'][0]


# ---------------------------------------------------------------------------
# Multi-chassis LAGs (VSX)
# ---------------------------------------------------------------------------

class TestMultiChassisLag:

    def test_absent_field_parses_as_false(self, hvdir):
        """LAGs written before this field existed must still load."""
        write_files(hvdir, {'interfaces.yml': """\
interface_groups: []
physical_interfaces: []
lag_interfaces:
  - name: lag256
    admin: up
    lacp_mode: active
    routed: false
loopback_interfaces: []
vlan_interfaces: []
"""})
        state = _parse_state(hvdir)
        assert state['lags'][0]['multi_chassis'] is False

    def test_flag_is_parsed(self, hvdir):
        write_files(hvdir, {'interfaces.yml': """\
interface_groups: []
physical_interfaces: []
lag_interfaces:
  - name: lag10
    admin: up
    lacp_mode: active
    routed: false
    multi_chassis: true
  - name: lag256
    admin: up
    lacp_mode: active
    routed: false
    multi_chassis: false
loopback_interfaces: []
vlan_interfaces: []
"""})
        state = _parse_state(hvdir)
        lags = {l['name']: l['multi_chassis'] for l in state['lags']}
        assert lags == {'lag10': True, 'lag256': False}

    def test_applies_to_routed_lags_too(self, hvdir):
        write_files(hvdir, {'interfaces.yml': """\
interface_groups: []
physical_interfaces: []
lag_interfaces:
  - name: lag20
    admin: up
    lacp_mode: active
    routed: true
    ip_address: "10.0.0.0"
    ip_prefix: "31"
    multi_chassis: true
loopback_interfaces: []
vlan_interfaces: []
"""})
        state = _parse_state(hvdir)
        assert state['lags'][0]['multi_chassis'] is True
        assert state['lags'][0]['routed'] == 'true'


class TestBooleanParsing:
    """yaml.BaseLoader returns every scalar as a string, so bool('false')
    is True. Anything written explicitly as false read as enabled — VSX,
    VSF, dynamic authorisation, OSPF passive and jumbo MTU were all affected.
    """

    def test_explicit_false_is_false(self, hvdir):
        write_files(hvdir, {
            'vsx.yml': 'vsx:\n  enabled: false\n',
            'vsf.yml': 'vsf:\n  enabled: false\n',
            'aaa.yml': 'dynamic_authorization: false\n',
            'interfaces.yml': """\
interface_groups: []
physical_interfaces: []
lag_interfaces: []
loopback_interfaces: []
vlan_interfaces:
  - name: vlan100
    ospf_passive: false
    mtu_jumbo: false
""",
        })
        state = _parse_state(hvdir)
        assert state['vsxEnabled'] is False
        assert state['vsfEnabled'] is False
        assert state['dynAuth'] is False
        assert state['vlanIfs'][0]['ospf_passive'] is False
        assert state['vlanIfs'][0]['mtu_jumbo'] is False

    def test_explicit_true_is_true(self, hvdir):
        write_files(hvdir, {
            'vsx.yml': 'vsx:\n  enabled: true\n',
            'aaa.yml': 'dynamic_authorization: true\n',
        })
        state = _parse_state(hvdir)
        assert state['vsxEnabled'] is True
        assert state['dynAuth'] is True

    def test_missing_key_is_false(self, hvdir):
        state = _parse_state(hvdir)
        assert state['vsxEnabled'] is False
        assert state['dynAuth'] is False


class TestSviAllocationVisibility:
    """The SVI address picker listed bare IPs with no allocation state, so
    every address looked free — unlike the unique and point-to-point
    pickers, which label who holds each one.
    """

    def _page(self, app, auth_client):
        from app.project import (create_project, add_pool, assign_vlan_subnet,
                                 project_host_vars_dir)
        import os
        with app.app_context():
            create_project(app, 'admin', 'svi')
            add_pool(app, 'admin', 'svi', {
                'id': 'sn', 'type': 'vlan_supernet', 'name': 'Core',
                'subnet': '10.50.0.0/16', 'carve_prefix': '24'})
            assign_vlan_subnet(app, 'admin', 'svi', 'sn', '10.50.5.0/24',
                               '100', 'Users', 'sw1', 'sw2')
            hv = os.path.join(project_host_vars_dir(app, 'admin', 'svi'), 'sw1')
            os.makedirs(hv, exist_ok=True)
        return auth_client.get(
            '/projects/svi/editor?host=sw1').data.decode()

    def test_picker_reads_svi_allocations(self, app, auth_client):
        body = self._page(app, auth_client)
        assert 'sviAllocs' in body, 'picker still ignores existing allocations'
        assert 'sviOptions' in body

    def test_allocations_reach_the_page(self, app, auth_client):
        """The derived gateway addresses must be in the payload to label."""
        import json, re
        body = self._page(app, auth_client)
        allocs = json.loads(
            re.search(r'const PROJECT_ALLOCS\s*=\s*(.*?);\n', body, re.S).group(1))
        svi = allocs['svi']['sn']
        assert svi['10.50.5.1']['role'] == 'gateway'
        assert svi['10.50.5.254']['role'] == 'active_gateway'
        assert svi['10.50.5.254']['shared_with'] == 'sw2'


class TestDynAuthServers:
    """CoA originates from a ClearPass node's own address, not the cluster
    VIP, so the dynamic-authorisation targets can differ from the
    authentication ones. Empty means the template reuses radius_servers.
    """

    def test_absent_list_parses_empty(self, hvdir):
        write_files(hvdir, {'aaa.yml': """\
radius_server_key: "SHARED"
radius_group_name: CPPM
dynamic_authorization: true
radius_servers:
  - address: 10.1.1.10
    key: "SHARED"
"""})
        state = _parse_state(hvdir)
        assert state['radiusDynauthServers'] == []
        assert len(state['radiusServers']) == 1

    def test_node_addresses_are_parsed(self, hvdir):
        write_files(hvdir, {'aaa.yml': """\
radius_server_key: "SHARED"
radius_group_name: CPPM
dynamic_authorization: true
radius_servers:
  - address: 10.1.1.10
    key: "SHARED"
radius_dynauth_servers:
  - address: 10.1.1.11
  - address: 10.1.1.12
    key: "OTHER"
"""})
        state = _parse_state(hvdir)
        nodes = state['radiusDynauthServers']
        assert [n['address'] for n in nodes] == ['10.1.1.11', '10.1.1.12']
        assert nodes[0]['key'] == '', 'blank key falls back to the global one'
        assert nodes[1]['key'] == 'OTHER'

    def test_auth_and_coa_lists_are_independent(self, hvdir):
        """The VIP answers auth; the nodes answer CoA."""
        write_files(hvdir, {'aaa.yml': """\
radius_server_key: "SHARED"
radius_group_name: CPPM
dynamic_authorization: true
radius_servers:
  - address: 10.1.1.10
    key: "SHARED"
  - address: 10.2.1.10
    key: "SHARED"
radius_dynauth_servers:
  - address: 10.1.1.11
  - address: 10.1.1.12
  - address: 10.2.1.11
  - address: 10.2.1.12
"""})
        state = _parse_state(hvdir)
        assert len(state['radiusServers']) == 2
        assert len(state['radiusDynauthServers']) == 4
        auth = {r['address'] for r in state['radiusServers']}
        coa = {r['address'] for r in state['radiusDynauthServers']}
        assert not (auth & coa), 'VIPs must not appear in the CoA list'
