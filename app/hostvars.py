import os
import yaml


def _safe(d, *keys, default=''):
    """Safely traverse nested dict, returning default if any key is missing or None."""
    for key in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(key)
        if d is None:
            return default
    return d if d is not None else default


def _load_yaml(hvdir, filename):
    fpath = os.path.join(hvdir, filename)
    if not os.path.exists(fpath):
        return {}
    with open(fpath) as f:
        return yaml.load(f, Loader=yaml.BaseLoader) or {}


def _parse_state(hvdir):
    """Parse all host_vars YAML files into a form-state-compatible JSON object."""

    general    = _load_yaml(hvdir, 'general.yml')
    mgmt       = _load_yaml(hvdir, 'management.yml')
    banner     = _load_yaml(hvdir, 'banner.yml')
    snmp       = _load_yaml(hvdir, 'snmp.yml')
    aaa        = _load_yaml(hvdir, 'aaa.yml')
    vrfs       = _load_yaml(hvdir, 'vrfs.yml')
    vlans      = _load_yaml(hvdir, 'vlans.yml')
    routes     = _load_yaml(hvdir, 'static_routes.yml')
    interfaces = _load_yaml(hvdir, 'interfaces.yml')
    routing    = _load_yaml(hvdir, 'routing.yml')
    vxlan      = _load_yaml(hvdir, 'vxlan.yml')
    vsx        = _load_yaml(hvdir, 'vsx.yml')
    vsf        = _load_yaml(hvdir, 'vsf.yml')
    sflow      = _load_yaml(hvdir, 'sflow.yml')
    syslog     = _load_yaml(hvdir, 'syslog.yml')

    stacking_type = 'none'
    if vsx:
        stacking_type = 'vsx'
    elif vsf:
        stacking_type = 'vsf'

    def parse_physical(iface):
        routed = str(_safe(iface, 'routed', default=False)).lower()
        raw_port_type = str(_safe(iface, 'port_type', default=''))
        if raw_port_type:
            port_type = raw_port_type
        elif routed == 'true':
            port_type = 'routed'
        else:
            port_type = 'access'
        return {
            'name':              str(_safe(iface, 'name')),
            'description':       str(_safe(iface, 'description')),
            'admin':             str(_safe(iface, 'admin', default='up')),
            'mtu':               str(_safe(iface, 'mtu', default='9198')),
            'lag_member':        str(_safe(iface, 'lag_member')),
            'port_type':         port_type,
            'routed':            routed,
            'ip_address':        str(_safe(iface, 'ip_address')),
            'ip_prefix':         str(_safe(iface, 'ip_prefix')),
            'vrf':               str(_safe(iface, 'vrf')),
            'ospf_area':         str(_safe(iface, 'ospf_area')),
            'ospf_process_id':   str(_safe(iface, 'ospf_process_id', default='1')),
            'ospf_auth_key':     str(_safe(iface, 'ospf_auth_key')),
            'access_vlan':       str(_safe(iface, 'access_vlan')),
            'trunk_allowed':     str(_safe(iface, 'trunk_allowed_vlans')),
            'trunk_native':      str(_safe(iface, 'trunk_native_vlan', default='1')),
            'auth_default_vlan': str(_safe(iface, 'auth_default_vlan', default='1')),
        }

    def parse_lag(lag):
        lag_routed = str(_safe(lag, 'routed', default=False)).lower()
        raw_lag_port_type = str(_safe(lag, 'port_type', default=''))
        if raw_lag_port_type:
            lag_port_type = raw_lag_port_type
        elif lag_routed == 'true':
            lag_port_type = 'routed'
        else:
            lag_port_type = 'access'
        return {
            'name':            str(_safe(lag, 'name')),
            'description':     str(_safe(lag, 'description')),
            'admin':           str(_safe(lag, 'admin', default='up')),
            'lacp_mode':       str(_safe(lag, 'lacp_mode', default='active')),
            'routed':          lag_routed,
            'port_type':       lag_port_type,
            'ip_address':      str(_safe(lag, 'ip_address')),
            'ip_prefix':       str(_safe(lag, 'ip_prefix')),
            'vrf':             str(_safe(lag, 'vrf')),
            'ospf_area':       str(_safe(lag, 'ospf_area')),
            'ospf_process_id': str(_safe(lag, 'ospf_process_id', default='1')),
            'ospf_auth_key':   str(_safe(lag, 'ospf_auth_key')),
            'trunk_allowed':   str(_safe(lag, 'trunk_allowed_vlans', default='all')),
            'trunk_native':    str(_safe(lag, 'trunk_native_vlan', default='1')),
        }

    def parse_loopback(lo):
        return {
            'name':            str(_safe(lo, 'name')),
            'description':     str(_safe(lo, 'description')),
            'ip_address':      str(_safe(lo, 'ip_address')),
            'ip_prefix':       str(_safe(lo, 'ip_prefix', default='32')),
            'vrf':             str(_safe(lo, 'vrf')),
            'ospf_area':       str(_safe(lo, 'ospf_area')),
            'ospf_process_id': str(_safe(lo, 'ospf_process_id', default='1')),
        }

    def parse_vlan_if(vi):
        return {
            'name':              str(_safe(vi, 'name')),
            'description':       str(_safe(vi, 'description')),
            'vrf':               str(_safe(vi, 'vrf')),
            'ip_address':        str(_safe(vi, 'ip_address')),
            'ip_prefix':         str(_safe(vi, 'ip_prefix', default='24')),
            'ospf_area':         str(_safe(vi, 'ospf_area')),
            'ospf_process_id':   str(_safe(vi, 'ospf_process_id', default='1')),
            'ospf_auth_key':     str(_safe(vi, 'ospf_auth_key')),
            'ospf_passive':      bool(_safe(vi, 'ospf_passive', default=False)),
            'active_gw_ip':      str(_safe(vi, 'active_gateway_ip')),
            'active_gw_mac':     str(_safe(vi, 'active_gateway_mac')),
            'mtu_jumbo':         bool(_safe(vi, 'mtu_jumbo', default=False)),
            'helper_addresses':  [str(h) for h in (_safe(vi, 'helper_addresses') or [])],
        }

    snmp_data  = _safe(snmp,  'snmp')  or {}
    vsx_data   = _safe(vsx,   'vsx')   or {}
    vsf_data   = _safe(vsf,   'vsf')   or {}
    sflow_data = _safe(sflow, 'sflow')  or {}
    syslog_data = _safe(syslog, 'syslog') or {}
    bgp_data   = _safe(routing, 'bgp') or {}
    vxlan_data = _safe(vxlan, 'vxlan') or {}

    return {
        'hostname':        str(_safe(general, 'hostname')),
        'platform':        str(_safe(general, 'platform', default='aoscx')),
        'profile':         str(_safe(general, 'profile', default='default')),
        'timezone':        str(_safe(general, 'timezone')),
        'ntpServers':      [str(s) for s in (_safe(general, 'ntp_servers') or [])],
        'dnsDomain':       str(_safe(general, 'dns', 'domain_name')),
        'dnsServers':      [str(s) for s in (_safe(general, 'dns', 'name_servers') or [])],

        'mgmtVrf':         str(_safe(mgmt, 'management', 'vrf')),
        'mgmtSrc':         str(_safe(mgmt, 'management', 'source_interface')),
        'localUsers':      [
            {'username': str(_safe(u, 'username')), 'group': str(_safe(u, 'group', default='administrators')), 'password': str(_safe(u, 'password'))}
            for u in (_safe(mgmt, 'local_users') or [])
        ],

        'bannerMotd':      str(_safe(banner, 'banner', 'motd')),
        'bannerExec':      str(_safe(banner, 'banner', 'exec')),

        'snmpVersion':     str(_safe(snmp_data, 'version', default='v2c')),
        'snmpVrf':         str(_safe(snmp_data, 'vrf')),
        'snmpDesc':        str(_safe(snmp_data, 'system_description')),
        'snmpLocation':    str(_safe(snmp_data, 'location')),
        'snmpContact':     str(_safe(snmp_data, 'contact')),
        'snmpCommunity':   str(_safe(snmp_data, 'community')),
        'v3Users':         [
            {'username': str(_safe(u, 'username')), 'auth_password': str(_safe(u, 'auth_password')), 'priv_password': str(_safe(u, 'priv_password'))}
            for u in (_safe(snmp_data, 'v3_users') or [])
        ],

        'syslogServer':    str(_safe(syslog_data, 'server')),
        'syslogSeverity':  str(_safe(syslog_data, 'severity')),

        'sflowCollectorIp': str(_safe(sflow_data, 'collector_ip')),
        'sflowAgentIp':     str(_safe(sflow_data, 'agent_ip')),

        'radiusServerKey': str(_safe(aaa, 'radius_server_key')),
        'radiusGroup':     str(_safe(aaa, 'radius_group_name')),
        'dynAuth':         bool(_safe(aaa, 'dynamic_authorization', default=False)),
        'radiusServers':   [
            {'address': str(_safe(r, 'address')), 'key': str(_safe(r, 'key'))}
            for r in (_safe(aaa, 'radius_servers') or [])
        ],

        'vrfs':   [{'name': str(_safe(v, 'name'))} for v in (_safe(vrfs, 'vrfs') or [])],
        'vlans':  [{'id': str(_safe(v, 'id')), 'name': str(_safe(v, 'name'))} for v in (_safe(vlans, 'vlans') or [])],
        'routes': [
            {'prefix': str(_safe(r, 'prefix')), 'gateway': str(_safe(r, 'gateway')), 'vrf': str(_safe(r, 'vrf')), 'distance': str(_safe(r, 'distance'))}
            for r in (_safe(routes, 'static_routes') or [])
        ],

        'ifGroups':  [{'name': str(_safe(g, 'name')), 'speed': str(_safe(g, 'speed', default='10g'))} for g in (interfaces.get('interface_groups') or [])],
        'physical':  [parse_physical(i) for i in (interfaces.get('physical_interfaces') or [])],
        'lags':      [parse_lag(l) for l in (interfaces.get('lag_interfaces') or [])],
        'loopbacks': [parse_loopback(l) for l in (interfaces.get('loopback_interfaces') or [])],
        'vlanIfs':   [parse_vlan_if(v) for v in (interfaces.get('vlan_interfaces') or [])],

        'ospfInstances': [
            {
                'enabled':    bool(_safe(inst, 'enabled', default=False)),
                'process_id': str(_safe(inst, 'process_id', default='1')),
                'router_id':  str(_safe(inst, 'router_id')),
                'vrf':        str(_safe(inst, 'vrf')),
                'areas':      [{'area_id': str(_safe(a, 'area_id'))} for a in (_safe(inst, 'areas') or [])],
            }
            for inst in (routing.get('ospf_instances') or [])
        ],
        'deviceRole':   str(_safe(routing, 'device_role')),
        'bgpAsn':       str(_safe(bgp_data, 'asn')),
        'bgpRid':       str(_safe(bgp_data, 'router_id')),
        'bgpNeighbors': [
            {'ip': str(_safe(n, 'ip')), 'remote_asn': str(_safe(n, 'remote_asn')), 'update_source': str(_safe(n, 'update_source', default='loopback0'))}
            for n in (_safe(bgp_data, 'neighbors') or [])
        ],

        'vtepLoopback': str(_safe(vxlan, 'loopback_interface')),
        'vtepIp':       str(_safe(vxlan, 'loopback_ip')),
        'vtepArea':     str(_safe(vxlan, 'ospf_area')),
        'vnis':         [{'vni': str(_safe(v, 'vni')), 'vlan': str(_safe(v, 'vlan'))} for v in (_safe(vxlan_data, 'vni_map') or [])],

        'stackingType': stacking_type,
        'vsxEnabled':   bool(_safe(vsx_data, 'enabled', default=False)),
        'vsxRole':      str(_safe(vsx_data, 'role')),
        'vsxMac':       str(_safe(vsx_data, 'system_mac')),
        'vsxIsl':       str(_safe(vsx_data, 'isl_port')),
        'vsxKaPeer':    str(_safe(vsx_data, 'keepalive', 'peer_ip')),
        'vsxKaSrc':     str(_safe(vsx_data, 'keepalive', 'src_ip')),
        'vsxKaVrf':     str(_safe(vsx_data, 'keepalive', 'vrf')),
        'vsxPeer':      str(_safe(vsx_data, 'peer_ip')),
        'vsfEnabled':   bool(_safe(vsf_data, 'enabled', default=False)),
        'vsfIf1':       str(_safe(vsf_data, 'interface1')),
        'vsfIf2':       str(_safe(vsf_data, 'interface2')),
        'vsfMembers':   [{'id': str(_safe(m, 'id'))} for m in (_safe(vsf_data, 'members') or [])],
    }
