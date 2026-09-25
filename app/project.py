import os
import json
import ipaddress
import shutil
from datetime import date


# ---------------------------------------------------------------------------
# Project directory helpers
# ---------------------------------------------------------------------------

def projects_dir(app, username):
    return os.path.join(app.config['DATA_DIR'], username, 'projects')

def project_dir(app, username, project_name):
    return os.path.join(projects_dir(app, username), project_name)

def project_config_path(app, username, project_name):
    return os.path.join(project_dir(app, username, project_name), 'config.json')

def project_allocations_path(app, username, project_name):
    return os.path.join(project_dir(app, username, project_name), 'allocations.json')

def project_hosts_ini_path(app, username, project_name):
    return os.path.join(project_dir(app, username, project_name), 'hosts.ini')

def project_host_vars_dir(app, username, project_name):
    return os.path.join(project_dir(app, username, project_name), 'host_vars')

def project_generated_configs_dir(app, username, project_name):
    return os.path.join(project_dir(app, username, project_name), 'generated_configs')


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------

def list_projects(app, username):
    """Return list of project metadata dicts sorted by name."""
    pdir = projects_dir(app, username)
    os.makedirs(pdir, exist_ok=True)
    result = []
    for name in sorted(os.listdir(pdir)):
        full = os.path.join(pdir, name)
        if not os.path.isdir(full):
            continue
        cfg = _load_config(app, username, name)
        gcdir = project_generated_configs_dir(app, username, name)
        config_count = len([
            f for f in os.listdir(gcdir)
            if f.endswith('.ios')
        ]) if os.path.isdir(gcdir) else 0
        host_count = _count_hosts(app, username, name)
        result.append({
            'name':         name,
            'display_name': cfg.get('name', name),
            'created':      cfg.get('created', ''),
            'host_count':   host_count,
            'config_count': config_count,
        })
    return result


def create_project(app, username, project_name):
    """Create a new project directory with default config and blank hosts.ini."""
    import re
    if not re.match(r'^[A-Za-z0-9_\-]+$', project_name):
        raise ValueError('Project name may only contain letters, numbers, hyphens and underscores.')
    pdir = project_dir(app, username, project_name)
    if os.path.isdir(pdir):
        raise ValueError(f'Project {project_name} already exists.')

    os.makedirs(project_host_vars_dir(app, username, project_name), exist_ok=True)
    os.makedirs(project_generated_configs_dir(app, username, project_name), exist_ok=True)

    # Default config
    _save_config(app, username, project_name, {
        'name':    project_name,
        'created': str(date.today()),
        'conventions': {
            'svi': {
                'gateway_offset':        1,
                'active_gateway_offset': 254,
                'reserved_from_start':   10,
            }
        },
        'pools':  [],
        'common': {
            'dns_servers':    [],
            'ntp_servers':    [],
            'dhcp_servers':   [],
            'radius_servers': [],
            'syslog_servers': [],
        }
    })

    # Default allocations
    _save_allocations(app, username, project_name, {
        'unique':        {},
        'point_to_point': {},
        'vlan_supernet': {},
        'svi':           {},
    })

    # Blank hosts.ini
    ini = project_hosts_ini_path(app, username, project_name)
    with open(ini, 'w') as f:
        f.write('[cx_vsx]\n\n[cx_vsf]\n\n[cx]\n\n[cx:children]\ncx_vsx\ncx_vsf\n')


def delete_project(app, username, project_name):
    """Delete a project and all its data."""
    pdir = project_dir(app, username, project_name)
    if os.path.isdir(pdir):
        shutil.rmtree(pdir)


def get_project_config(app, username, project_name):
    return _load_config(app, username, project_name)


def save_project_config(app, username, project_name, config):
    _save_config(app, username, project_name, config)


# ---------------------------------------------------------------------------
# Pool management
# ---------------------------------------------------------------------------

def sort_by_address(items):
    """Sort CIDR or bare-IP keys numerically rather than lexicographically.

    Plain string ordering puts 10.100.10.0/24 before 10.100.2.0/24, which
    reads as random in a picker. Accepts a dict (returns sorted items) or an
    iterable of strings.
    """
    def key(value):
        text = value[0] if isinstance(value, tuple) else value
        try:
            if '/' in str(text):
                net = ipaddress.ip_network(str(text), strict=False)
                return (0, int(net.network_address), net.prefixlen)
            return (0, int(ipaddress.ip_address(str(text))), 0)
        except ValueError:
            # Anything unparseable sorts last, in string order, rather than
            # blowing up the page.
            return (1, 0, 0)

    if isinstance(items, dict):
        return sorted(items.items(), key=key)
    return sorted(items, key=key)


class PoolOverlapError(ValueError):
    """Raised when a new pool would overlap an existing one."""


def _overlapping_pool(cfg, subnet, ignore_id=None):
    """Return the first existing pool whose range overlaps `subnet`, if any."""
    try:
        net = ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return None
    for existing in cfg.get('pools', []):
        if ignore_id and existing['id'] == ignore_id:
            continue
        try:
            other = ipaddress.ip_network(existing['subnet'], strict=False)
        except (ValueError, KeyError):
            continue
        if net.overlaps(other):
            return existing
    return None


def add_pool(app, username, project_name, pool):
    """Add a pool to the project config and pre-carve if vlan_supernet.

    Overlapping ranges are rejected. Two pools covering the same addresses
    have no defined owner — allocations attribute to whichever pool happens
    to be listed first, so the same address can be handed out twice.

    The one legitimate overlap is delegation: a unique or point-to-point pool
    whose range is one carved subnet of an existing supernet. That subnet is
    then marked delegated so the VLAN picker stops offering it. Pass
    `parent_pool_id` to take that path.
    """
    cfg = _load_config(app, username, project_name)

    parent_id = pool.get('parent_pool_id')
    if parent_id:
        _validate_delegation(cfg, pool, parent_id)
    else:
        clash = _overlapping_pool(cfg, pool.get('subnet', ''))
        if clash:
            raise PoolOverlapError(
                '%s overlaps the existing pool "%s" (%s). Delegate it from '
                'that pool instead of defining it separately.'
                % (pool.get('subnet', '?'), clash.get('name', clash['id']),
                   clash['subnet']))

    cfg['pools'].append(pool)
    _save_config(app, username, project_name, cfg)

    if pool['type'] == 'vlan_supernet':
        _precarve_supernet(app, username, project_name, pool)
    elif parent_id:
        _mark_subnet_delegated(app, username, project_name,
                               parent_id, pool['subnet'], pool['id'])


def _validate_delegation(cfg, pool, parent_id):
    """Check a delegated pool really is one carved subnet of its parent."""
    parent = _find_pool(cfg, parent_id)
    if not parent:
        raise PoolOverlapError('Parent pool %s no longer exists.' % parent_id)
    if parent['type'] != 'vlan_supernet':
        raise PoolOverlapError(
            'Only a VLAN supernet can be delegated from; "%s" is a %s pool.'
            % (parent.get('name', parent_id), parent['type']))
    if pool['type'] not in ('unique', 'point_to_point'):
        raise PoolOverlapError(
            'Only unique and point-to-point pools can be delegated.')

    try:
        child = ipaddress.ip_network(pool['subnet'], strict=False)
        supernet = ipaddress.ip_network(parent['subnet'], strict=False)
    except ValueError as e:
        raise PoolOverlapError('Invalid subnet: %s' % e)

    if not child.subnet_of(supernet):
        raise PoolOverlapError(
            '%s is not inside %s.' % (child, supernet))
    if child.prefixlen != int(parent['carve_prefix']):
        raise PoolOverlapError(
            'Delegated range must be exactly one /%s subnet of the supernet '
            '(got /%s).' % (parent['carve_prefix'], child.prefixlen))

    # Must not collide with another pool other than the parent itself.
    clash = _overlapping_pool(cfg, pool['subnet'], ignore_id=parent_id)
    if clash:
        raise PoolOverlapError(
            '%s overlaps the existing pool "%s" (%s).'
            % (pool['subnet'], clash.get('name', clash['id']), clash['subnet']))


def _mark_subnet_delegated(app, username, project_name,
                           parent_id, subnet, child_pool_id):
    """Flag a carved subnet as delegated so the VLAN picker skips it."""
    allocs = _load_allocations(app, username, project_name)
    carved = allocs.get('vlan_supernet', {}).get(parent_id, {})
    entry = carved.get(subnet)
    if entry is None:
        return
    entry['status'] = 'delegated'
    entry['delegated_to'] = child_pool_id
    _save_allocations(app, username, project_name, allocs)


def _release_delegated_subnet(app, username, project_name, pool):
    """Return a delegated subnet to the parent supernet as carved."""
    parent_id = pool.get('parent_pool_id')
    if not parent_id:
        return
    allocs = _load_allocations(app, username, project_name)
    carved = allocs.get('vlan_supernet', {}).get(parent_id, {})
    entry = carved.get(pool.get('subnet'))
    if entry is None:
        return
    entry['status'] = 'carved'
    entry.pop('delegated_to', None)
    _save_allocations(app, username, project_name, allocs)


def remove_pool(app, username, project_name, pool_id):
    """Remove a pool and its allocations.

    A supernet takes its delegated children with it — leaving them behind
    would orphan pools pointing at a parent that no longer exists.
    """
    cfg = _load_config(app, username, project_name)
    pool = _find_pool(cfg, pool_id)

    doomed = [pool_id]
    if pool and pool.get('type') == 'vlan_supernet':
        doomed += [p['id'] for p in cfg.get('pools', [])
                   if p.get('parent_pool_id') == pool_id]
    elif pool:
        _release_delegated_subnet(app, username, project_name, pool)

    cfg['pools'] = [p for p in cfg['pools'] if p['id'] not in doomed]
    _save_config(app, username, project_name, cfg)

    allocs = _load_allocations(app, username, project_name)
    for pool_type in allocs:
        for pid in doomed:
            allocs[pool_type].pop(pid, None)
    _save_allocations(app, username, project_name, allocs)


def _precarve_supernet(app, username, project_name, pool):
    """Pre-carve all subnets from a vlan_supernet pool."""
    allocs = _load_allocations(app, username, project_name)
    supernet = ipaddress.ip_network(pool['subnet'], strict=False)
    carve_prefix = int(pool['carve_prefix'])
    pool_id = pool['id']

    if pool_id not in allocs['vlan_supernet']:
        allocs['vlan_supernet'][pool_id] = {}

    for subnet in supernet.subnets(new_prefix=carve_prefix):
        key = str(subnet)
        if key not in allocs['vlan_supernet'][pool_id]:
            allocs['vlan_supernet'][pool_id][key] = {
                'status':       'carved',
                'vlan_id':      None,
                'vlan_name':    None,
                'hostname':     None,
                'peer_hostname': None,
            }

    _save_allocations(app, username, project_name, allocs)


# ---------------------------------------------------------------------------
# Allocation helpers — unique pools
# ---------------------------------------------------------------------------

def get_available_ips(app, username, project_name, pool_id):
    """Return list of available IPs for a unique pool."""
    cfg   = _load_config(app, username, project_name)
    allocs = _load_allocations(app, username, project_name)
    pool  = _find_pool(cfg, pool_id)
    if not pool or pool['type'] not in ('unique',):
        return []

    network = ipaddress.ip_network(pool['subnet'], strict=False)
    prefix  = int(pool['prefix'])
    used    = allocs['unique'].get(pool_id, {})

    available = []
    for host in network.hosts():
        ip_str = str(host) + '/' + str(prefix) if prefix != 32 else str(host)
        plain  = str(host)
        if plain not in used:
            available.append(ip_str)
    return available


def allocate_unique(app, username, project_name, pool_id, ip, hostname, interface):
    """Allocate a unique IP."""
    allocs = _load_allocations(app, username, project_name)
    if pool_id not in allocs['unique']:
        allocs['unique'][pool_id] = {}
    allocs['unique'][pool_id][ip] = {
        'hostname':  hostname,
        'interface': interface,
    }
    _save_allocations(app, username, project_name, allocs)


def release_unique(app, username, project_name, pool_id, ip):
    """Release a unique IP allocation."""
    allocs = _load_allocations(app, username, project_name)
    allocs['unique'].get(pool_id, {}).pop(ip, None)
    _save_allocations(app, username, project_name, allocs)


# ---------------------------------------------------------------------------
# Allocation helpers — point-to-point pools
# ---------------------------------------------------------------------------

def get_available_ptp_pairs(app, username, project_name, pool_id):
    """Return list of available /31 pairs and reservations for dropdown."""
    cfg    = _load_config(app, username, project_name)
    allocs = _load_allocations(app, username, project_name)
    pool   = _find_pool(cfg, pool_id)
    if not pool or pool['type'] != 'point_to_point':
        return []

    network = ipaddress.ip_network(pool['subnet'], strict=False)
    used    = allocs['point_to_point'].get(pool_id, {})
    result  = []

    for subnet in network.subnets(new_prefix=31):
        hosts  = list(subnet.hosts()) or list(subnet)
        ip_a   = str(hosts[0])
        ip_b   = str(hosts[1])
        alloc_a = used.get(ip_a)
        alloc_b = used.get(ip_b)

        if not alloc_a and not alloc_b:
            # Fully available
            result.append({
                'subnet':    str(subnet),
                'ip_a':      ip_a,
                'ip_b':      ip_b,
                'status':    'available',
                'alloc_a':   None,
                'alloc_b':   None,
            })
        elif alloc_a and alloc_b and alloc_b.get('status') == 'reserved_for_peer':
            # One end allocated, other reserved — peer end available to assign
            result.append({
                'subnet':    str(subnet),
                'ip_a':      ip_a,
                'ip_b':      ip_b,
                'status':    'partial',
                'alloc_a':   alloc_a,
                'alloc_b':   alloc_b,
            })
        else:
            # Fully allocated
            result.append({
                'subnet':    str(subnet),
                'ip_a':      ip_a,
                'ip_b':      ip_b,
                'status':    'allocated',
                'alloc_a':   alloc_a,
                'alloc_b':   alloc_b,
            })
    return result


def allocate_ptp(app, username, project_name, pool_id, ip, hostname, interface, peer_note=None):
    """Allocate one end of a /31. Automatically reserves the peer end."""
    allocs = _load_allocations(app, username, project_name)
    if pool_id not in allocs['point_to_point']:
        allocs['point_to_point'][pool_id] = {}

    # Determine peer IP
    host_obj = ipaddress.ip_address(ip)
    if int(host_obj) % 2 == 0:
        peer_ip = str(host_obj + 1)
    else:
        peer_ip = str(host_obj - 1)

    existing_peer = allocs['point_to_point'][pool_id].get(peer_ip)

    # Record this end
    allocs['point_to_point'][pool_id][ip] = {
        'hostname':       hostname,
        'interface':      interface,
        'peer_ip':        peer_ip,
        'peer_hostname':  existing_peer['hostname'] if existing_peer else None,
        'peer_interface': existing_peer['interface'] if existing_peer else None,
        'peer_note':      peer_note,
    }

    if existing_peer and existing_peer.get('status') == 'reserved_for_peer':
        # Complete the link — update the peer record
        allocs['point_to_point'][pool_id][peer_ip] = {
            'hostname':       hostname,
            'interface':      interface,
            'peer_ip':        ip,
            'peer_hostname':  hostname,
            'peer_interface': interface,
            'peer_note':      existing_peer.get('peer_note'),
        }
        # Now update this end with peer details
        allocs['point_to_point'][pool_id][ip]['peer_hostname']  = existing_peer.get('hostname') or ''
        allocs['point_to_point'][pool_id][ip]['peer_interface'] = existing_peer.get('interface') or ''
    else:
        # Reserve the peer end
        allocs['point_to_point'][pool_id][peer_ip] = {
            'hostname':       None,
            'interface':      None,
            'peer_ip':        ip,
            'peer_hostname':  hostname,
            'peer_interface': interface,
            'peer_note':      peer_note,
            'status':         'reserved_for_peer',
        }

    _save_allocations(app, username, project_name, allocs)
    return peer_ip


def release_ptp(app, username, project_name, pool_id, ip):
    """Release one end of a /31. If peer is reserved_for_peer, release that too."""
    allocs = _load_allocations(app, username, project_name)
    pool_allocs = allocs['point_to_point'].get(pool_id, {})
    entry = pool_allocs.pop(ip, None)
    if entry:
        peer_ip = entry.get('peer_ip')
        if peer_ip:
            peer = pool_allocs.get(peer_ip)
            if peer and peer.get('status') == 'reserved_for_peer':
                pool_allocs.pop(peer_ip, None)
    _save_allocations(app, username, project_name, allocs)


# ---------------------------------------------------------------------------
# Allocation helpers — vlan supernet pools
# ---------------------------------------------------------------------------

def get_carved_subnets(app, username, project_name, pool_id):
    """Return all carved subnets for a supernet pool."""
    allocs = _load_allocations(app, username, project_name)
    return allocs['vlan_supernet'].get(pool_id, {})


def assign_vlan_subnet(app, username, project_name, pool_id, subnet, vlan_id,
                       vlan_name, hostname=None, peer_hostname=None):
    """Assign a carved subnet to a VLAN.

    hostname is optional: a VLAN can be named and reserved at the supernet
    level before it is known which switch carries it. SVI gateway addresses
    are only derived once a hostname is given, since they are per-switch.
    """
    allocs = _load_allocations(app, username, project_name)
    pool_allocs = allocs['vlan_supernet'].get(pool_id, {})
    if subnet not in pool_allocs:
        raise ValueError(f'{subnet} not found in pool {pool_id}')
    # A delegated subnet belongs to a child pool now; assigning it to a VLAN
    # would double-book the same addresses.
    if pool_allocs[subnet].get('status') == 'delegated':
        raise ValueError(
            f'{subnet} is delegated to another pool and cannot be assigned '
            f'to a VLAN.')
    pool_allocs[subnet] = {
        'status':       'assigned',
        'vlan_id':      vlan_id,
        'vlan_name':    vlan_name,
        'hostname':     hostname,
        'peer_hostname': peer_hostname,
    }
    _save_allocations(app, username, project_name, allocs)

    # Auto-derive SVI IPs from conventions — only meaningful once we know
    # which switch owns the SVI.
    if not hostname:
        return

    cfg = _load_config(app, username, project_name)
    conv = cfg.get('conventions', {}).get('svi', {})
    gw_offset  = int(conv.get('gateway_offset', 1))
    agw_offset = int(conv.get('active_gateway_offset', 254))

    network  = ipaddress.ip_network(subnet, strict=False)
    hosts    = list(network.hosts())
    gw_ip    = str(hosts[gw_offset - 1])  if gw_offset  <= len(hosts) else None
    agw_ip   = str(hosts[agw_offset - 1]) if agw_offset <= len(hosts) else None

    svi_allocs = allocs['svi'].setdefault(pool_id, {})
    if gw_ip:
        svi_allocs[gw_ip] = {
            'hostname':  hostname,
            'interface': f'vlan{vlan_id}',
            'role':      'gateway',
        }
    if agw_ip and peer_hostname:
        svi_allocs[agw_ip] = {
            'hostname':    hostname,
            'interface':   f'vlan{vlan_id}',
            'role':        'active_gateway',
            'shared_with': peer_hostname,
        }

    _save_allocations(app, username, project_name, allocs)


def release_vlan_subnet(app, username, project_name, pool_id, subnet):
    """Release a VLAN subnet assignment and remove derived SVI allocations."""
    allocs = _load_allocations(app, username, project_name)
    pool_allocs = allocs['vlan_supernet'].get(pool_id, {})
    if subnet in pool_allocs:
        pool_allocs[subnet] = {
            'status':       'carved',
            'vlan_id':      None,
            'vlan_name':    None,
            'hostname':     None,
            'peer_hostname': None,
        }

    # Remove SVI allocations for this subnet
    network = ipaddress.ip_network(subnet, strict=False)
    svi_allocs = allocs['svi'].get(pool_id, {})
    for ip in list(svi_allocs.keys()):
        if ipaddress.ip_address(ip) in network:
            del svi_allocs[ip]

    _save_allocations(app, username, project_name, allocs)


# ---------------------------------------------------------------------------
# Common infrastructure
# ---------------------------------------------------------------------------

def get_common(app, username, project_name):
    cfg = _load_config(app, username, project_name)
    return cfg.get('common', {})


def save_common(app, username, project_name, common):
    cfg = _load_config(app, username, project_name)
    cfg['common'] = common
    _save_config(app, username, project_name, cfg)


# ---------------------------------------------------------------------------
# Conventions
# ---------------------------------------------------------------------------

def get_conventions(app, username, project_name):
    cfg = _load_config(app, username, project_name)
    return cfg.get('conventions', {})


def save_conventions(app, username, project_name, conventions):
    cfg = _load_config(app, username, project_name)
    cfg['conventions'] = conventions
    _save_config(app, username, project_name, cfg)


# ---------------------------------------------------------------------------
# Full allocations read (for the allocations tab)
# ---------------------------------------------------------------------------

def get_all_allocations(app, username, project_name):
    """All allocations, with each pool's entries in address order.

    Sorted here rather than in a template filter so every consumer — the
    Resources tables, the editor payload, the API — gets the same ordering
    without depending on how the Flask app was built.
    """
    allocs = _load_allocations(app, username, project_name)
    for pool_type, pools in allocs.items():
        if not isinstance(pools, dict):
            continue
        for pool_id, entries in pools.items():
            if isinstance(entries, dict):
                pools[pool_id] = dict(sort_by_address(entries))
    return allocs


# ---------------------------------------------------------------------------
# Allocation sync from host_vars
# ---------------------------------------------------------------------------

def sync_allocations(app, username, project_name, hostname):
    """Scan host_vars for a hostname and update allocations.json to match."""
    import yaml as _yaml
 
    cfg    = _load_config(app, username, project_name)
    allocs = _load_allocations(app, username, project_name)
    pools  = cfg.get('pools', [])
 
    if not pools:
        return
 
    # Build a map of subnet -> pool for quick lookup
    pool_map = {}
    for pool in pools:
        if pool['type'] in ('unique', 'point_to_point'):
            pool_map[pool['subnet']] = pool
 
    def ip_in_pool(ip, pool):
        try:
            net = ipaddress.ip_network(pool['subnet'], strict=False)
            return ipaddress.ip_address(ip) in net
        except Exception:
            return False
 
    def find_pool(ip):
        """Most specific match wins.

        A delegated pool sits inside its parent supernet, so matching in
        config order would attribute its addresses to whichever pool was
        added first. Longest prefix is the only stable answer.
        """
        if not ip or not str(ip).strip():
            return None
        try:
            ipaddress.ip_address(str(ip).strip())
        except ValueError:
            return None
        matches = [p for p in pools
                   if p['type'] in ('unique', 'point_to_point')
                   and ip_in_pool(ip, p)]
        if not matches:
            return None
        return max(matches,
                   key=lambda p: ipaddress.ip_network(
                       p['subnet'], strict=False).prefixlen)
 
    def load_hv(filename):
        hvdir = os.path.join(project_host_vars_dir(app, username, project_name), hostname)
        fpath = os.path.join(hvdir, filename)
        if not os.path.exists(fpath):
            return {}
        with open(fpath) as f:
            return _yaml.load(f, Loader=_yaml.BaseLoader) or {}
 
    interfaces = load_hv('interfaces.yml')
    vxlan      = load_hv('vxlan.yml')
 
    def register_unique(ip, interface_name, pool):
        pid = pool['id']
        if pid not in allocs['unique']:
            allocs['unique'][pid] = {}
        # Remove any old allocation for this hostname+interface in this pool
        to_remove = [k for k, v in allocs['unique'][pid].items()
                     if v.get('hostname') == hostname and v.get('interface') == interface_name]
        for k in to_remove:
            del allocs['unique'][pid][k]
        allocs['unique'][pid][ip] = {'hostname': hostname, 'interface': interface_name}
 
    def register_ptp(ip, interface_name, pool):
        pid = pool['id']
        if pid not in allocs['point_to_point']:
            allocs['point_to_point'][pid] = {}
 
        host_obj = ipaddress.ip_address(ip)
        peer_ip  = str(host_obj + 1) if int(host_obj) % 2 == 0 else str(host_obj - 1)
 
        # Remove old entries for this hostname+interface
        to_remove = [k for k, v in allocs['point_to_point'][pid].items()
                     if v.get('hostname') == hostname and v.get('interface') == interface_name]
        for k in to_remove:
            old_entry = allocs['point_to_point'][pid].pop(k)
            # Clean up reserved peer if it was ours
            old_peer = old_entry.get('peer_ip')
            if old_peer and old_peer in allocs['point_to_point'][pid]:
                if allocs['point_to_point'][pid][old_peer].get('status') == 'reserved_for_peer':
                    del allocs['point_to_point'][pid][old_peer]
 
        existing_peer = allocs['point_to_point'][pid].get(peer_ip)
 
        allocs['point_to_point'][pid][ip] = {
            'hostname':       hostname,
            'interface':      interface_name,
            'peer_ip':        peer_ip,
            'peer_hostname':  existing_peer.get('hostname') if existing_peer else None,
            'peer_interface': existing_peer.get('interface') if existing_peer else None,
            'peer_note':      None,
        }
 
        if existing_peer and existing_peer.get('status') == 'reserved_for_peer':
            # Complete the link — peer was reserved, now fill it in
            allocs['point_to_point'][pid][peer_ip] = {
                'hostname':       existing_peer.get('hostname'),
                'interface':      existing_peer.get('interface'),
                'peer_ip':        ip,
                'peer_hostname':  hostname,
                'peer_interface': interface_name,
                'peer_note':      existing_peer.get('peer_note'),
            }
            allocs['point_to_point'][pid][ip]['peer_hostname']  = existing_peer.get('hostname')
            allocs['point_to_point'][pid][ip]['peer_interface'] = existing_peer.get('interface')
        elif existing_peer and existing_peer.get('hostname'):
            # Peer already has a real allocation — just update our peer info, don't overwrite theirs
            allocs['point_to_point'][pid][ip]['peer_hostname']  = existing_peer.get('hostname')
            allocs['point_to_point'][pid][ip]['peer_interface'] = existing_peer.get('interface')
        else:
            # No peer allocation yet — reserve the peer end
            allocs['point_to_point'][pid][peer_ip] = {
                'hostname':       None,
                'interface':      None,
                'peer_ip':        ip,
                'peer_hostname':  hostname,
                'peer_interface': interface_name,
                'peer_note':      None,
                'status':         'reserved_for_peer',
            }
 
    def strip_prefix(ip_str):
        """Remove CIDR prefix length if present: '10.0.0.20/31' -> '10.0.0.20'"""
        s = str(ip_str).strip()
        return s.split('/')[0] if '/' in s else s
 
    def process_ip(ip, interface_name):
        if not ip or not str(ip).strip():
            return
        ip = strip_prefix(ip)
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return
        pool = find_pool(ip)
        if not pool:
            return
        if pool['type'] == 'unique':
            register_unique(ip, interface_name, pool)
        elif pool['type'] == 'point_to_point':
            register_ptp(ip, interface_name, pool)
 
    def is_valid_ip(ip):
        if not ip or not str(ip).strip():
            return False
        try:
            ipaddress.ip_address(strip_prefix(ip))
            return True
        except ValueError:
            return False
 
    # Clear all existing allocations for this hostname before re-scanning
    # This ensures deleted or renamed interfaces don't leave stale reservations
    for pid in list(allocs['unique'].keys()):
        for ip in list(allocs['unique'][pid].keys()):
            if allocs['unique'][pid][ip].get('hostname') == hostname:
                del allocs['unique'][pid][ip]
 
    for pid in list(allocs['point_to_point'].keys()):
        # Collect all IPs to remove first, then delete — avoids mutation during iteration
        to_delete = set()
        pool_allocs = allocs['point_to_point'][pid]
        for ip, entry in list(pool_allocs.items()):
            if entry.get('hostname') == hostname:
                to_delete.add(ip)
                # Also queue the reserved peer end for deletion
                peer_ip = entry.get('peer_ip')
                if peer_ip and peer_ip in pool_allocs:
                    peer = pool_allocs[peer_ip]
                    if peer.get('status') == 'reserved_for_peer' and peer.get('peer_hostname') == hostname:
                        to_delete.add(peer_ip)
            elif entry.get('status') == 'reserved_for_peer' and entry.get('peer_hostname') == hostname:
                to_delete.add(ip)
        for ip in to_delete:
            pool_allocs.pop(ip, None)
 
    # Scan all interface types — only process explicitly set valid IPs
    for lo in (interfaces.get('loopback_interfaces') or []):
        ip = lo.get('ip_address', '')
        if is_valid_ip(ip):
            process_ip(ip, lo.get('name', 'loopback0'))
 
    for phy in (interfaces.get('physical_interfaces') or []):
        if str(phy.get('routed', '')).lower() == 'true' or phy.get('port_type') == 'routed':
            ip = phy.get('ip_address', '')
            if is_valid_ip(ip):
                process_ip(ip, phy.get('name', ''))
 
    for lag in (interfaces.get('lag_interfaces') or []):
        if str(lag.get('routed', '')).lower() == 'true' or lag.get('port_type') == 'routed':
            ip = lag.get('ip_address', '')
            if is_valid_ip(ip):
                process_ip(ip, lag.get('name', ''))
 
    for svi in (interfaces.get('vlan_interfaces') or []):
        ip = svi.get('ip_address', '')
        if is_valid_ip(ip):
            process_ip(ip, svi.get('name', ''))
        agw = svi.get('active_gateway_ip', '')
        if is_valid_ip(agw):
            process_ip(agw, svi.get('name', '') + ':active_gw')
 
    vtep_ip = vxlan.get('loopback_ip', '')
    if is_valid_ip(vtep_ip):
        process_ip(vtep_ip, vxlan.get('loopback_interface', 'loopback1'))
 
    _save_allocations(app, username, project_name, allocs)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_config(app, username, project_name):
    path = project_config_path(app, username, project_name)
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def _save_config(app, username, project_name, config):
    path = project_config_path(app, username, project_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(config, f, indent=2)


def _load_allocations(app, username, project_name):
    path = project_allocations_path(app, username, project_name)
    if not os.path.exists(path):
        return {'unique': {}, 'point_to_point': {}, 'vlan_supernet': {}, 'svi': {}}
    with open(path) as f:
        return json.load(f)


def _save_allocations(app, username, project_name, allocations):
    path = project_allocations_path(app, username, project_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(allocations, f, indent=2)


def _find_pool(config, pool_id):
    for pool in config.get('pools', []):
        if pool['id'] == pool_id:
            return pool
    return None


def _count_hosts(app, username, project_name):
    import configparser
    ini = project_hosts_ini_path(app, username, project_name)
    if not os.path.exists(ini):
        return 0
    cp = configparser.ConfigParser(allow_no_value=True)
    cp.optionxform = str
    cp.read(ini)
    count = 0
    for group in ('cx_vsx', 'cx_vsf', 'cx'):
        if cp.has_section(group):
            count += len([h for h in cp.options(group) if h])
    return count
