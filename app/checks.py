"""
Project checks: mistakes across switches that no single editor tab can see.

Every check reads the saved host_vars, the allocations in Resources, the
deploy mapping and the generated configs. Nothing is changed and nothing
is blocked — a finding is a prompt to look, because a half-built design is
normal mid-project:

    Addressing        an address used twice; an SVI outside its VLAN's
                      subnet; Resources out of step with the configs; an
                      address outside every pool
    VSX and VSF       a pair whose keepalive, role, system MAC or anycast
                      VTEP don't agree; duplicate VSF member numbers
    VLANs, VXLAN, VRF a VNI mapped to different VLANs; a VLAN or VRF used
                      but not defined on that switch
    Routing           a router ID used by two switches
    Readiness         a config generated before the latest changes; a
                      switch with no deployment IP

A finding can be dismissed. It is stored with a fingerprint of what it
said, so it comes back if its details change — a dismissed "VLAN 30 not
defined on SW1" reappears if VLAN 40 goes missing too.

Deliberately not checked, as legitimate mid-build or by design: VLANs that
differ between VSX members, half-built /31s, BGP neighbours outside the
project, route reflector clients without a reflector, shared deployment
IPs and empty optional settings.
"""

import hashlib
import ipaddress
import json
import os
import re

from . import hostvars
from .project import (_load_config, _load_allocations,
                      project_host_vars_dir, project_generated_configs_dir)
from .utils import read_project_hosts, _HOSTNAME_RE


ERROR, WARNING = 'error', 'warning'

CATEGORIES = ['Addressing', 'VSX and VSF', 'VLANs, VXLAN and VRFs',
              'Routing', 'Readiness']

CHECKS = {
    'dup_ip':       'Addresses used more than once',
    'svi_subnet':   'SVI addresses outside their VLAN subnet',
    'alloc_drift':  'Resources out of step with the configs',
    'outside_pool': 'Addresses outside every pool',
    'vsx_pair':     'VSX pairs that don\'t line up',
    'vsx_vtep':     'VSX pairs with different anycast VTEPs',
    'vsf_members':  'VSF members',
    'vni_map':      'VNI mapping',
    'vlan_ref':     'VLANs used but not defined',
    'vrf_ref':      'VRFs used but not defined',
    'router_id':    'Duplicate router IDs',
    'stale_config': 'Configs generated before the latest changes',
    'no_mgmt_ip':   'Switches without a deployment IP',
    'unreadable':   'Unreadable host_vars',
}

_CATEGORY = {
    'dup_ip': 'Addressing', 'svi_subnet': 'Addressing',
    'alloc_drift': 'Addressing', 'outside_pool': 'Addressing',
    'vsx_pair': 'VSX and VSF', 'vsx_vtep': 'VSX and VSF',
    'vsf_members': 'VSX and VSF',
    'vni_map': 'VLANs, VXLAN and VRFs', 'vlan_ref': 'VLANs, VXLAN and VRFs',
    'vrf_ref': 'VLANs, VXLAN and VRFs',
    'router_id': 'Routing',
    'stale_config': 'Readiness', 'no_mgmt_ip': 'Readiness',
    'unreadable': 'Readiness',
}

# Present on every AOS-CX switch without being configured.
_BUILTIN_VRFS = {'', 'default', 'mgmt'}

_CONFIG_NAME = re.compile(r'^(.+)_(FULL|PARTIAL_.+)\.ios$')


# ── Small helpers ──────────────────────────────────────────

def _finding(check, key, severity, title, details=(), hosts=()):
    f = {'check': check, 'key': '%s:%s' % (check, key), 'severity': severity,
         'category': _CATEGORY[check], 'title': title,
         'details': list(details), 'hosts': sorted(set(hosts))}
    f['fingerprint'] = hashlib.sha256(
        json.dumps([title, f['details']]).encode()).hexdigest()[:16]
    return f


def _addr(value):
    """The bare address from '10.0.0.1' or '10.0.0.1/31', or None."""
    s = str(value or '').strip().split('/')[0]
    if not s:
        return None
    try:
        return str(ipaddress.ip_address(s))
    except ValueError:
        return None


def _vlan_ids(text):
    """VLAN IDs in '10,20,30-40'. 'all' and blanks give nothing."""
    out = set()
    for part in str(text or '').replace(' ', '').split(','):
        if not part or part.lower() in ('all', 'none'):
            continue
        lo, _, hi = part.partition('-')
        try:
            lo = int(lo)
            hi = int(hi) if hi else lo
        except ValueError:
            continue
        if 1 <= lo <= hi <= 4094:
            out.update(range(lo, hi + 1))
    return out


def _vlan_id(text):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def _compress(ids):
    """[10, 11, 12, 20] -> '10-12, 20'."""
    ids = sorted(ids)
    runs, start = [], None
    for i, n in enumerate(ids):
        if start is None:
            start = n
        if i + 1 == len(ids) or ids[i + 1] != n + 1:
            runs.append(str(start) if start == n else '%d-%d' % (start, n))
            start = None
    return ', '.join(runs)


def _svi_vlan(name):
    """'vlan20' -> 20."""
    m = re.fullmatch(r'vlan\s*(\d+)', str(name).strip(), re.I)
    return int(m.group(1)) if m else None


def _is_routed(iface):
    return iface.get('port_type') == 'routed' or iface.get('routed') == 'true'


def hostvars_fingerprint(hvdir):
    """sha256 of a switch's saved host_vars, so a generated config can be
    compared with what the switch's settings are now. File times would not
    do: every save rewrites every file, and every generation rewrites every
    config. None if the directory is missing."""
    if not os.path.isdir(hvdir):
        return None
    h = hashlib.sha256()
    for name in sorted(os.listdir(hvdir)):
        path = os.path.join(hvdir, name)
        if not name.endswith('.yml') or not os.path.isfile(path):
            continue
        h.update(name.encode() + b'\0')
        with open(path, 'rb') as f:
            h.update(f.read())
        h.update(b'\0')
    return h.hexdigest()


def generated_name(hostname, tags):
    """The file a generation writes for hostname, as the NetForge role
    names it: FULL for a full run, PARTIAL_<sorted tags> otherwise."""
    tags = sorted({t.strip() for t in str(tags or '').split(',') if t.strip()})
    if not tags or {'full', 'all', 'untagged'} & set(tags):
        return '%s_FULL.ios' % hostname
    return '%s_PARTIAL_%s.ios' % (hostname, '-'.join(tags))


# ── Loading ────────────────────────────────────────────────

def _addresses(state):
    """Every interface address on a switch: [(ip, where, kind)].

    kind is 'vtep', 'agw' (VSX active gateway), 'svi' or 'iface'. A VTEP
    that is also listed as a loopback counts once, as the VTEP.
    """
    found = {}

    def add(ip, where, kind):
        ip = _addr(ip)
        if not ip or not where:
            return
        key = (ip, where)
        if key not in found or kind == 'vtep':
            found[key] = kind

    for lo in state['loopbacks']:
        add(lo['ip_address'], lo['name'], 'iface')
    for phy in state['physical']:
        if _is_routed(phy) and not phy.get('lag_member'):
            add(phy['ip_address'], phy['name'], 'iface')
    for lag in state['lags']:
        if _is_routed(lag):
            add(lag['ip_address'], lag['name'], 'iface')
    for svi in state['vlanIfs']:
        add(svi['ip_address'], svi['name'], 'svi')
        add(svi['active_gw_ip'], svi['name'], 'agw')
    add(state['vtepIp'], state['vtepLoopback'] or 'loopback1', 'vtep')
    return [(ip, where, kind) for (ip, where), kind in found.items()]


def _load(app, username, project_name):
    """Everything the checks read, gathered once."""
    hosts = [h for h in read_project_hosts(app, username, project_name)
             if _HOSTNAME_RE.match(h['hostname'])]
    base = project_host_vars_dir(app, username, project_name)
    states, unreadable = {}, []
    for h in hosts:
        hvdir = os.path.join(base, h['hostname'])
        if not os.path.isdir(hvdir):
            continue
        try:
            states[h['hostname']] = hostvars._parse_state(hvdir)
        except Exception as e:                      # noqa: BLE001
            unreadable.append((h['hostname'], str(e).splitlines()[0][:200]))

    cfg = _load_config(app, username, project_name)
    pools = []
    for p in cfg.get('pools', []):
        try:
            pools.append(dict(p, net=ipaddress.ip_network(p['subnet'], strict=False)))
        except (KeyError, ValueError):
            pass

    from .deploy_routes import _load_mapping
    return {
        'hosts': [h['hostname'] for h in hosts],
        'states': states,
        'unreadable': unreadable,
        'addresses': {name: _addresses(s) for name, s in states.items()},
        'cfg': cfg,
        'pools': pools,
        'allocs': _load_allocations(app, username, project_name),
        'mapping': _load_mapping(app, username, project_name),
        'base': base,
        'gcdir': project_generated_configs_dir(app, username, project_name),
    }


# ── Addressing ─────────────────────────────────────────────

def check_duplicate_ips(d):
    """One address on two interfaces. An active gateway shared across
    switches on the same VLAN, and an anycast VTEP shared by a pair, are
    how those features work and are left alone."""
    by_ip = {}
    for host, addrs in d['addresses'].items():
        for ip, where, kind in addrs:
            by_ip.setdefault(ip, []).append((host, where, kind))
    out = []
    for ip, uses in sorted(by_ip.items()):
        if len(uses) < 2:
            continue
        kinds = {k for _, _, k in uses}
        if kinds == {'agw'} and len({w.lower() for _, w, _ in uses}) == 1:
            continue
        if kinds == {'vtep'} and len(uses) == 2 and uses[0][0] != uses[1][0]:
            continue
        out.append(_finding(
            'dup_ip', ip, ERROR, '%s is used on %d interfaces' % (ip, len(uses)),
            ['%s %s' % (h, w) for h, w, _ in sorted(uses)],
            [h for h, _, _ in uses]))
    return out


def _vlan_blocks(d):
    """Subnets assigned to a VLAN in Resources: [(net, vlan, host, peer)]."""
    blocks = []
    for entries in d['allocs'].get('vlan_supernet', {}).values():
        for subnet, e in (entries or {}).items():
            vlan = _vlan_id(e.get('vlan_id'))
            if vlan is None:
                continue
            try:
                net = ipaddress.ip_network(subnet, strict=False)
            except ValueError:
                continue
            blocks.append((net, vlan, e.get('hostname'), e.get('peer_hostname')))
    return blocks


def check_svi_subnets(d):
    """An SVI address in another VLAN's subnet, outside the subnet its own
    VLAN was given in Resources, or an active gateway outside the SVI's
    own subnet."""
    blocks = _vlan_blocks(d)
    out = []
    for host, state in sorted(d['states'].items()):
        for svi in state['vlanIfs']:
            vlan = _svi_vlan(svi['name'])
            own = None
            ip = _addr(svi['ip_address'])
            if ip:
                prefix = (svi['ip_address'].split('/')[1] if '/' in svi['ip_address']
                          else svi['ip_prefix'] or '24')
                try:
                    own = ipaddress.ip_interface('%s/%s' % (ip, prefix)).network
                except ValueError:
                    own = None
            for role, value in (('address', svi['ip_address']),
                                ('active gateway', svi['active_gw_ip'])):
                addr = _addr(value)
                if not addr or vlan is None:
                    continue
                a = ipaddress.ip_address(addr)
                key = '%s:%s:%s' % (host, svi['name'], role)
                other = [b for b in blocks if a in b[0] and b[1] != vlan]
                mine = [b for b in blocks if b[1] == vlan
                        and (not b[2] or host in (b[2], b[3]))]
                if other:
                    net, other_vlan = other[0][0], other[0][1]
                    out.append(_finding(
                        'svi_subnet', key, ERROR,
                        '%s %s %s is in VLAN %d\'s subnet' % (host, svi['name'], role, other_vlan),
                        ['%s is inside %s, assigned to VLAN %d in Resources'
                         % (addr, net, other_vlan)], [host]))
                elif mine and not any(a in b[0] for b in mine):
                    out.append(_finding(
                        'svi_subnet', key, ERROR,
                        '%s %s %s is outside VLAN %d\'s subnet' % (host, svi['name'], role, vlan),
                        ['%s is not inside %s' % (addr, ', '.join(sorted({str(b[0]) for b in mine})))],
                        [host]))
                elif role == 'active gateway' and own and a not in own:
                    out.append(_finding(
                        'svi_subnet', key, ERROR,
                        '%s %s active gateway is outside the SVI\'s subnet' % (host, svi['name']),
                        ['%s is not inside %s' % (addr, own)], [host]))
    return out


def _owning_pool(d, addr, types=('unique', 'point_to_point')):
    """The most specific pool of the given types holding addr, as the save
    sync picks it — a delegated pool beats its parent supernet."""
    a = ipaddress.ip_address(addr)
    matches = [p for p in d['pools'] if p.get('type') in types and a in p['net']]
    return max(matches, key=lambda p: p['net'].prefixlen) if matches else None


def check_allocation_drift(d):
    """Resources and the saved configs disagree.

    - an address is allocated to a switch that is no longer in the project
    - an address is allocated to a switch whose saved config doesn't use it
      (also the case briefly after picking one in the editor, until saved)
    - a switch's config uses an address its pool doesn't record against it,
      so the pool would offer it to another switch
    """
    allocs, hosts = d['allocs'], set(d['hosts'])
    pool_names = {p['id']: p.get('name') or p['subnet'] for p in d['pools']}
    gone, unused, unrecorded = {}, {}, {}

    def held_by(entry):
        if entry.get('status') == 'reserved_for_peer':
            return []
        return [h for h in (entry.get('hostname'), entry.get('shared_with')) if h]

    for ptype in ('unique', 'point_to_point', 'svi'):
        for pid, entries in (allocs.get(ptype) or {}).items():
            for ip, e in (entries or {}).items():
                if not isinstance(e, dict):
                    continue
                names = held_by(e)
                if e.get('status') == 'reserved_for_peer' and e.get('peer_hostname'):
                    names = [e['peer_hostname']]
                for h in names:
                    where = '%s (%s)' % (ip, pool_names.get(pid, pid))
                    if h not in hosts:
                        gone.setdefault(h, []).append(where)
                    elif (ptype != 'svi' and e.get('status') != 'reserved_for_peer'
                          and h in d['addresses']
                          and ip not in {a for a, _, _ in d['addresses'][h]}):
                        unused.setdefault(h, []).append(where)

    for host, addrs in d['addresses'].items():
        for ip, where, kind in addrs:
            if kind == 'agw':
                continue        # one record per VLAN, not per switch sharing it
            pool = _owning_pool(d, ip)
            ptype = pool['type'] if pool else None
            if not pool and kind == 'svi':
                pool = _owning_pool(d, ip, ('vlan_supernet',))
                ptype = 'svi' if pool else None
            if not pool:
                continue
            entry = (allocs.get(ptype) or {}).get(pool['id'], {}).get(ip) or {}
            if host not in held_by(entry):
                unrecorded.setdefault(host, []).append(
                    '%s %s (%s)' % (where, ip, pool.get('name') or pool['subnet']))

    out = []
    for h, items in sorted(gone.items()):
        out.append(_finding('alloc_drift', 'gone:' + h, WARNING,
                            'Addresses allocated to %s, which is not in the project' % h,
                            sorted(items)))
    for h, items in sorted(unused.items()):
        out.append(_finding('alloc_drift', 'unused:' + h, WARNING,
                            'Allocated to %s in Resources but not in its saved config' % h,
                            sorted(items), [h]))
    for h, items in sorted(unrecorded.items()):
        out.append(_finding('alloc_drift', 'unrecorded:' + h, WARNING,
                            'In %s\'s config but not recorded against it in Resources' % h,
                            sorted(items), [h]))
    return out


def check_outside_pools(d):
    if not d['pools']:
        return []
    out = []
    for host, addrs in sorted(d['addresses'].items()):
        stray = sorted('%s %s' % (where, ip) for ip, where, _ in addrs
                       if not any(ipaddress.ip_address(ip) in p['net'] for p in d['pools']))
        if stray:
            out.append(_finding('outside_pool', host, WARNING,
                                '%s has addresses outside every pool' % host, stray, [host]))
    return out


# ── VSX and VSF ────────────────────────────────────────────

def _mac(value):
    return re.sub(r'[^0-9a-f]', '', str(value or '').lower())


def _vsx_pairs(d):
    """Pair VSX switches: A's keepalive peer is B's keepalive source; when
    keepalives aren't filled in, a shared system MAC. Returns (pairs,
    findings for switches that can't be paired)."""
    vsx = {h: s for h, s in d['states'].items()
           if s['stackingType'] == 'vsx' and s['vsxEnabled']}
    by_src = {}
    for h, s in vsx.items():
        if _addr(s['vsxKaSrc']):
            by_src.setdefault(_addr(s['vsxKaSrc']), []).append(h)

    pairs, paired, out, no_match = set(), set(), [], []
    for h, s in sorted(vsx.items()):
        peer = _addr(s['vsxKaPeer'])
        if not peer or h in paired:
            continue
        matches = [o for o in by_src.get(peer, []) if o != h]
        if len(matches) == 1 and matches[0] not in paired:
            pairs.add(tuple(sorted((h, matches[0]))))
            paired.update((h, matches[0]))
        elif len(matches) > 1:
            out.append(_finding('vsx_pair', 'ka-many:' + h, ERROR,
                                '%s\'s keepalive peer %s is the source on %d switches'
                                % (h, peer, len(matches)), sorted(matches), [h] + matches))
        elif not matches:
            no_match.append((h, peer))

    by_mac = {}
    for h, s in vsx.items():
        if h not in paired and _mac(s['vsxMac']):
            by_mac.setdefault(_mac(s['vsxMac']), []).append(h)
    for mac, members in by_mac.items():
        if len(members) == 2:
            pairs.add(tuple(sorted(members)))
            paired.update(members)
        elif len(members) > 2:
            out.append(_finding('vsx_pair', 'mac-many:' + mac, ERROR,
                                '%d switches share VSX system MAC %s'
                                % (len(members), vsx[members[0]]['vsxMac']),
                                sorted(members), members))

    for h, peer in no_match:
        if h not in paired:
            out.append(_finding('vsx_pair', 'no-peer:' + h, WARNING,
                                '%s\'s VSX peer is not in the project' % h,
                                ['No switch has keepalive source %s' % peer], [h]))
    return sorted(pairs), vsx, out


def check_vsx(d):
    pairs, vsx, out = _vsx_pairs(d)
    for a, b in pairs:
        sa, sb = vsx[a], vsx[b]
        pair = '%s+%s' % (a, b)
        ka = [(sa['vsxKaPeer'], sa['vsxKaSrc']), (sb['vsxKaPeer'], sb['vsxKaSrc'])]
        if all(_addr(x) for k in ka for x in k) and not (
                _addr(sa['vsxKaPeer']) == _addr(sb['vsxKaSrc'])
                and _addr(sb['vsxKaPeer']) == _addr(sa['vsxKaSrc'])):
            out.append(_finding(
                'vsx_pair', pair + ':keepalive', ERROR,
                'Keepalive addresses on %s and %s don\'t mirror each other' % (a, b),
                ['%s: source %s, peer %s' % (a, sa['vsxKaSrc'], sa['vsxKaPeer']),
                 '%s: source %s, peer %s' % (b, sb['vsxKaSrc'], sb['vsxKaPeer'])], [a, b]))
        if (sa['vsxRole'] or 'primary') == (sb['vsxRole'] or 'primary'):
            out.append(_finding(
                'vsx_pair', pair + ':role', ERROR,
                '%s and %s are both VSX %s' % (a, b, sa['vsxRole'] or 'primary'),
                [], [a, b]))
        if _mac(sa['vsxMac']) and _mac(sb['vsxMac']) and _mac(sa['vsxMac']) != _mac(sb['vsxMac']):
            out.append(_finding(
                'vsx_pair', pair + ':mac', ERROR,
                '%s and %s have different VSX system MACs' % (a, b),
                ['%s: %s' % (a, sa['vsxMac']), '%s: %s' % (b, sb['vsxMac'])], [a, b]))
        va, vb = _addr(sa['vtepIp']), _addr(sb['vtepIp'])
        if va and vb and va != vb:
            out.append(_finding(
                'vsx_vtep', pair, ERROR,
                '%s and %s have different anycast VTEPs' % (a, b),
                ['%s: %s' % (a, va), '%s: %s' % (b, vb)], [a, b]))
    return out


def check_vsf(d):
    out = []
    for host, s in sorted(d['states'].items()):
        if s['stackingType'] != 'vsf' or not s['vsfEnabled']:
            continue
        ids = [m['id'].strip() for m in s['vsfMembers']]
        if not ids:
            out.append(_finding('vsf_members', host + ':empty', WARNING,
                                'VSF is enabled on %s with no members' % host, [], [host]))
            continue
        bad = sorted({i for i in ids if not (i.isdigit() and 1 <= int(i) <= 10)})
        if bad:
            out.append(_finding('vsf_members', host + ':range', ERROR,
                                '%s has VSF member numbers outside 1-10' % host,
                                ['Member %s' % (i or '(blank)') for i in bad], [host]))
        dups = sorted({i for i in ids if i and ids.count(i) > 1})
        if dups:
            out.append(_finding('vsf_members', host + ':dup', ERROR,
                                '%s has duplicate VSF member numbers' % host,
                                ['Member %s appears %d times' % (i, ids.count(i)) for i in dups],
                                [host]))
    return out


# ── VLANs, VXLAN and VRFs ──────────────────────────────────

def check_vni_map(d):
    vni_vlans, vlan_vnis = {}, {}
    for host, s in d['states'].items():
        for m in s['vnis']:
            vni, vlan = str(m['vni']).strip(), _vlan_id(m['vlan'])
            if not vni or vlan is None:
                continue
            vni_vlans.setdefault(vni, {}).setdefault(vlan, set()).add(host)
            vlan_vnis.setdefault(vlan, {}).setdefault(vni, set()).add(host)
    out = []
    for vni, vlans in sorted(vni_vlans.items()):
        if len(vlans) > 1:
            out.append(_finding(
                'vni_map', 'vni:' + vni, ERROR, 'VNI %s is mapped to %d VLANs' % (vni, len(vlans)),
                ['VLAN %d on %s' % (v, ', '.join(sorted(h))) for v, h in sorted(vlans.items())],
                [h for hs in vlans.values() for h in hs]))
    for vlan, vnis in sorted(vlan_vnis.items()):
        if len(vnis) > 1:
            out.append(_finding(
                'vni_map', 'vlan:%d' % vlan, ERROR, 'VLAN %d is mapped to %d VNIs' % (vlan, len(vnis)),
                ['VNI %s on %s' % (v, ', '.join(sorted(h))) for v, h in sorted(vnis.items())],
                [h for hs in vnis.values() for h in hs]))
    return out


def check_vlan_refs(d):
    """VLANs a switch uses — SVIs, VNI map, access, native, allowed and
    authentication VLANs — that its VLAN list doesn't define. VLAN 1
    always exists."""
    out = []
    for host, s in sorted(d['states'].items()):
        defined = {_vlan_id(v['id']) for v in s['vlans']} | {1}
        refs = []          # (where, {ids})

        for svi in s['vlanIfs']:
            v = _svi_vlan(svi['name'])
            if v is not None:
                refs.append(('SVI %s' % svi['name'], {v}))
        for m in s['vnis']:
            v = _vlan_id(m['vlan'])
            if v is not None:
                refs.append(('VNI %s' % m['vni'], {v}))
        for phy in s['physical']:
            if phy.get('lag_member') or _is_routed(phy):
                continue
            pt = phy['port_type']
            if pt == 'trunk':
                refs.append(('%s allowed' % phy['name'], _vlan_ids(phy['trunk_allowed'])))
                refs.append(('%s native' % phy['name'], _vlan_ids(phy['trunk_native'])))
            elif pt == 'authenticated':
                refs.append(('%s auth default' % phy['name'], _vlan_ids(phy['auth_default_vlan'])))
            else:
                refs.append(('%s access' % phy['name'], _vlan_ids(phy['access_vlan'])))
        for lag in s['lags']:
            if _is_routed(lag):
                continue
            refs.append(('%s allowed' % lag['name'], _vlan_ids(lag['trunk_allowed'])))
            refs.append(('%s native' % lag['name'], _vlan_ids(lag['trunk_native'])))

        lines = []
        for where, ids in refs:
            missing = ids - defined
            if missing:
                lines.append('%s: %s' % (where, _compress(missing)))
        if lines:
            out.append(_finding('vlan_ref', host, ERROR,
                                '%s uses VLANs it doesn\'t define' % host, lines, [host]))
    return out


def check_vrf_refs(d):
    out = []
    for host, s in sorted(d['states'].items()):
        defined = {v['name'].strip() for v in s['vrfs']} | _BUILTIN_VRFS
        used = {}

        def use(vrf, where):
            vrf = str(vrf or '').strip()
            if vrf not in defined:
                used.setdefault(vrf, []).append(where)

        for lo in s['loopbacks']:
            use(lo['vrf'], lo['name'])
        for phy in s['physical']:
            if _is_routed(phy) and not phy.get('lag_member'):
                use(phy['vrf'], phy['name'])
        for lag in s['lags']:
            if _is_routed(lag):
                use(lag['vrf'], lag['name'])
        for svi in s['vlanIfs']:
            use(svi['vrf'], svi['name'])
        for r in s['routes']:
            use(r['vrf'], 'static route %s' % r['prefix'])
        for inst in s['ospfInstances']:
            use(inst['vrf'], 'OSPF process %s' % inst['process_id'])
        use(s['mgmtVrf'], 'management')
        use(s['snmpVrf'], 'SNMP')
        if s['stackingType'] == 'vsx' and s['vsxEnabled']:
            use(s['vsxKaVrf'], 'VSX keepalive')

        if used:
            out.append(_finding(
                'vrf_ref', host, ERROR, '%s uses VRFs it doesn\'t define' % host,
                ['%s: %s' % (vrf, ', '.join(where)) for vrf, where in sorted(used.items())],
                [host]))
    return out


# ── Routing ────────────────────────────────────────────────

def check_router_ids(d):
    seen = {}
    for host, s in d['states'].items():
        rid = _addr(s['bgpRid'])
        if rid:
            seen.setdefault(('BGP', rid), set()).add(host)
        for inst in s['ospfInstances']:
            rid = _addr(inst['router_id'])
            if rid:
                label = 'OSPF (%s VRF)' % inst['vrf'] if inst['vrf'].strip() else 'OSPF'
                seen.setdefault((label, rid), set()).add(host)
    out = []
    for (proto, rid), hosts in sorted(seen.items()):
        if len(hosts) > 1:
            out.append(_finding('router_id', '%s:%s' % (proto, rid), ERROR,
                                '%s router ID %s is used on %d switches' % (proto, rid, len(hosts)),
                                sorted(hosts), hosts))
    return out


# ── Readiness ──────────────────────────────────────────────

def check_stale_configs(d):
    """Configs whose switch's host_vars have changed since they were
    generated. Configs generated before this was recorded aren't judged."""
    recorded = d['cfg'].get('generated_from') or {}
    stale = {}
    for name, fp in recorded.items():
        m = _CONFIG_NAME.match(name)
        if not m or not os.path.isfile(os.path.join(d['gcdir'], name)):
            continue
        host = m.group(1)
        if host not in d['states']:
            continue
        if hostvars_fingerprint(os.path.join(d['base'], host)) != fp:
            stale.setdefault(host, []).append(name)
    return [_finding('stale_config', host, WARNING,
                     '%s has changed since its config was generated' % host,
                     sorted(files), [host])
            for host, files in sorted(stale.items())]


def check_deployment_ips(d):
    """Only once Deployment IPs is in use — a project that is only
    generating configs has no reason to fill it in."""
    rows = d['mapping'].get('hosts') or []
    if not rows:
        return []
    ips = {r.get('hostname'): str(r.get('mgmt_ip') or '').strip()
           for r in rows if isinstance(r, dict)}
    return [_finding('no_mgmt_ip', h, WARNING, '%s has no deployment IP' % h, [], [h])
            for h in sorted(d['hosts']) if not ips.get(h)]


def check_unreadable(d):
    return [_finding('unreadable', h, ERROR, 'Couldn\'t read %s\'s host_vars' % h, [err], [h])
            for h, err in d['unreadable']]


ALL_CHECKS = [
    check_unreadable,
    check_duplicate_ips, check_svi_subnets, check_allocation_drift, check_outside_pools,
    check_vsx, check_vsf,
    check_vni_map, check_vlan_refs, check_vrf_refs,
    check_router_ids,
    check_stale_configs, check_deployment_ips,
]


# ── Entry points ───────────────────────────────────────────

def run(app, username, project_name):
    """All findings, errors first, then by category."""
    d = _load(app, username, project_name)
    findings = []
    for check in ALL_CHECKS:
        findings.extend(check(d))
    order = {c: i for i, c in enumerate(CATEGORIES)}
    findings.sort(key=lambda f: (f['severity'] != ERROR, order[f['category']]))
    return findings


def split_dismissed(findings, dismissed):
    """(active, dismissed). A dismissal only holds while the finding still
    says the same thing."""
    active, hidden = [], []
    for f in findings:
        (hidden if dismissed.get(f['key']) == f['fingerprint'] else active).append(f)
    return active, hidden


def summary(app, username, project_name):
    """Counts of undismissed findings, for the banners. Never raises: a
    check failing must not take the Generate or Deploy page down with it."""
    try:
        findings = run(app, username, project_name)
        dismissed = _load_config(app, username, project_name).get('dismissed_checks') or {}
        active, _ = split_dismissed(findings, dismissed)
    except Exception:                               # noqa: BLE001
        return None
    return {'errors': sum(1 for f in active if f['severity'] == ERROR),
            'warnings': sum(1 for f in active if f['severity'] == WARNING)}
