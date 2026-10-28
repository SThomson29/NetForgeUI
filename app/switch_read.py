"""
Read a switch's inventory and LLDP neighbours over the AOS-CX REST API, in
one session (see aoscx_rest).

    /system                        hostname, platform, system MAC, boot time,
                                   software version
    /system/subsystems (chassis)   serial, part number, product name and
                                   base MAC of each chassis (VSF members)
    /system/interfaces             which ports have link
    .../<port>/lldp_neighbors      who is on the other end of each one

Only ports with link are asked for neighbours, so a 48-port switch on a
bench with three cables costs three calls, not 48. Every part is optional:
whatever the switch answers is used, and what it doesn't is left empty
rather than failing the whole read.
"""

import concurrent.futures
import re

from .aoscx_rest import SwitchError, encode, run

MAX_PARALLEL = 16
PORT = re.compile(r'^\d+/\d+/\d+$')


def norm_mac(value):
    """'00:1A:1e:..' / '001a.1e..' -> '001a1e...', or '' if not a MAC."""
    hexes = re.sub(r'[^0-9a-f]', '', str(value or '').lower())
    return hexes if len(hexes) == 12 else ''


def _items(data):
    """(key, value) pairs from a REST collection, whether the switch
    returned it as a dict keyed by id or as a list."""
    if isinstance(data, dict):
        return list(data.items())
    if isinstance(data, list):
        return [(str(i), v) for i, v in enumerate(data)]
    return []


def _neighbor(obj):
    if not isinstance(obj, dict):
        return None
    info = obj.get('neighbor_info') if isinstance(obj.get('neighbor_info'), dict) else {}
    return {
        'chassis_id': str(obj.get('chassis_id') or ''),
        'port_id': str(obj.get('port_id') or ''),
        'sys_name': str(info.get('chassis_name') or obj.get('chassis_name') or ''),
        'port_desc': str(info.get('port_description') or ''),
    }


def _members(subsystems):
    out = []
    for key, sub in sorted(_items(subsystems)):
        if not str(key).startswith('chassis') or not isinstance(sub, dict):
            continue
        info = sub.get('product_info') if isinstance(sub.get('product_info'), dict) else {}
        out.append({
            'member': str(key).split(',')[-1],
            'serial': info.get('serial_number'),
            'part_number': info.get('part_number'),
            'product_name': info.get('product_name'),
            'base_mac': info.get('base_mac_address'),
        })
    return out


def read_switch(ip, username, password):
    """{inventory, neighbors: {port: [neighbor]}} for one switch.
    Raises SwitchError if it can't be logged in to."""
    def reads(get):
        system = get('/system', optional=True, params={
            'attributes': 'hostname,platform_name,system_mac,boot_time,software_version'}) or {}
        subsystems = get('/system/subsystems', optional=True,
                         params={'attributes': 'product_info', 'depth': '2'}) or {}
        ports = get('/system/interfaces', optional=True,
                    params={'attributes': 'name,link_state', 'depth': '1'}) or {}
        neighbors = {}
        for key, attrs in _items(ports):
            name = (attrs.get('name') if isinstance(attrs, dict) else None) or key
            if not PORT.match(str(name)):
                continue
            if isinstance(attrs, dict) and attrs.get('link_state') not in (None, 'up'):
                continue
            found = get('/system/interfaces/%s/lldp_neighbors' % encode(name),
                        params={'depth': '2'}, optional=True)
            seen = [n for n in (_neighbor(v) for _k, v in _items(found)) if n]
            if seen:
                neighbors[str(name)] = seen
        return system, subsystems, neighbors

    system, subsystems, neighbors = run(ip, username, password, reads)
    system = system if isinstance(system, dict) else {}
    members = _members(subsystems)
    first = members[0] if members else {}
    return {
        'inventory': {
            'serial': first.get('serial'),
            'serials': [m['serial'] for m in members if m.get('serial')],
            'model': first.get('product_name') or system.get('platform_name'),
            'part_number': first.get('part_number'),
            'base_mac': system.get('system_mac') or first.get('base_mac'),
            'firmware': system.get('software_version'),
            'boot_time': system.get('boot_time'),
            'reported_hostname': system.get('hostname'),
            'members': members if len(members) > 1 else [],
        },
        'neighbors': neighbors,
    }


def read_all(hosts, username, password):
    """{hostname: {ok, error?, inventory?, neighbors?}} for each
    {hostname, mgmt_ip}, read in parallel."""
    def one(h):
        try:
            return h['hostname'], dict(read_switch(h['mgmt_ip'], username, password), ok=True)
        except SwitchError as e:
            return h['hostname'], {'ok': False, 'error': str(e)}
        except Exception as e:                      # noqa: BLE001
            return h['hostname'], {'ok': False, 'error': 'failed (%s)' % e.__class__.__name__}

    if not hosts:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(hosts))) as ex:
        return dict(ex.map(one, hosts))
