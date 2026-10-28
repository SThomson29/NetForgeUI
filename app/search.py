"""
Search across projects: where is this IP, subnet, VLAN, VNI or name used?

What the query looks like decides how it matches:

    10.20.30.5      that address, and interfaces/pools whose subnet holds it
    10.20.30.0/24   every address inside the subnet, and pools overlapping it
    30              VLAN IDs and VNIs only (lists and ranges like 10,20-40 too)
    anything else   text in switch names and host_vars values, ignoring case

Searched: each switch's host_vars, Resources pools, deployment IPs. Not
generated configs, which are built from those. Values under keys that hold
secrets (passwords, keys, communities) are neither searched nor shown.

Files are read at search time; there is no index to fall out of step.
"""

import ipaddress
import os
import re

import yaml

MAX_HITS = 300
MAX_QUERY = 200

_SECRET = re.compile(r'password|secret|key|community|psk|token', re.I)
_VLAN_NAME = re.compile(r'^vlan\s*(\d+)$', re.I)


class Query:
    """A parsed search: kind is 'ip', 'subnet', 'number' or 'text'."""

    def __init__(self, text):
        self.text = (text or '').strip()[:MAX_QUERY]
        self.kind = None
        self.value = None
        t = self.text
        if not t:
            return
        if '/' in t:
            try:
                self.kind, self.value = 'subnet', ipaddress.ip_network(t, strict=False)
                return
            except ValueError:
                pass
        try:
            self.kind, self.value = 'ip', ipaddress.ip_address(t)
            return
        except ValueError:
            pass
        if t.isdigit():
            self.kind, self.value = 'number', int(t)
        elif len(t) >= 2:
            self.kind, self.value = 'text', t.lower()

    @property
    def usable(self):
        return self.kind is not None

    @property
    def describe(self):
        return {
            'ip': 'address and subnets containing it',
            'subnet': 'addresses in this subnet',
            'number': 'VLAN IDs and VNIs',
            'text': 'names and values',
        }.get(self.kind, '')


# ── Reading host_vars ──────────────────────────────────────

def _item_label(item, index):
    if isinstance(item, dict):
        for k in ('name', 'id', 'vni', 'hostname'):
            v = item.get(k)
            if v not in (None, ''):
                return str(v)
    return '#%d' % (index + 1)


def _leaves(node, trail=()):
    """(trail, key, value, parent dict) for every scalar in a YAML tree."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                yield from _leaves(v, trail + (str(k),))
            else:
                yield trail, str(k), v, node
    elif isinstance(node, list):
        for i, item in enumerate(node):
            if isinstance(item, (dict, list)):
                yield from _leaves(item, trail + (_item_label(item, i),))
            else:
                yield trail, (trail[-1] if trail else ''), item, None


def _as_interface(value, key, parent):
    """An address as an ip_interface, using a sibling ip_prefix when the
    value is bare (SVIs keep the two apart). None if it isn't an address."""
    s = str(value).strip()
    if not s or len(s) > 64:
        return None
    try:
        if '/' in s:
            return ipaddress.ip_interface(s)
        addr = ipaddress.ip_address(s)
    except ValueError:
        return None
    prefix = parent.get('ip_prefix') if (parent and key == 'ip_address') else None
    if prefix not in (None, ''):
        try:
            return ipaddress.ip_interface('%s/%s' % (addr, prefix))
        except ValueError:
            pass
    return ipaddress.ip_interface(addr)


def _show(iface):
    """10.0.0.1/24 as is; a host address without its /32."""
    if iface.network.prefixlen == iface.max_prefixlen:
        return str(iface.ip)
    return str(iface)


def _vlan_ids(value):
    """VLAN IDs a value names: '30', '10,20-40', 'vlan30'. Empty for 'all'."""
    s = str(value).strip()
    m = _VLAN_NAME.match(s)
    if m:
        return {int(m.group(1))}
    ids = set()
    for part in s.split(','):
        part = part.strip()
        if re.fullmatch(r'\d{1,8}', part):
            ids.add(int(part))
        elif re.fullmatch(r'\d{1,4}-\d{1,4}', part):
            a, b = (int(x) for x in part.split('-'))
            if a <= b and b - a <= 4094:
                ids.update(range(a, b + 1))
    return ids


def _match_leaf(q, trail, key, value, parent):
    """(label, shown value) if this leaf matches, else None."""
    if value is None or _SECRET.search(key):
        return None
    if q.kind in ('ip', 'subnet'):
        iface = _as_interface(value, key, parent)
        if iface is None:
            return None
        if q.kind == 'ip':
            if iface.ip == q.value:
                return 'exact', _show(iface)
            net = iface.network
            if 0 < net.prefixlen < net.max_prefixlen and q.value in net:
                return 'in subnet', _show(iface)
            return None
        if iface.ip in q.value:
            return 'in range', _show(iface)
        return None
    if q.kind == 'number':
        k = key.lower()
        if 'vni' in k:
            kind = 'VNI'
        elif 'vlan' in k or (k == 'id' and 'vlans' in trail) or _VLAN_NAME.match(str(value)):
            kind = 'VLAN'
        else:
            return None
        if kind == 'VNI':
            try:
                hit = int(str(value).strip()) == q.value
            except ValueError:
                hit = False
        else:
            hit = q.value in _vlan_ids(value)
        return (kind, str(value)) if hit else None
    if q.kind == 'text':
        s = str(value)
        return ('text', s) if q.value in s.lower() else None
    return None


def _host_vars(hvdir):
    """{file stem: parsed YAML} for one switch, skipping unreadable files."""
    out = {}
    try:
        names = sorted(f for f in os.listdir(hvdir) if f.endswith(('.yml', '.yaml')))
    except OSError:
        return out
    for name in names:
        try:
            with open(os.path.join(hvdir, name)) as f:
                out[name.rsplit('.', 1)[0]] = yaml.load(f, Loader=yaml.BaseLoader) or {}
        except (OSError, yaml.YAMLError):
            continue
    return out


# ── Searching a project ────────────────────────────────────

def search_project(app, owner, project, q):
    """Hits in one project: [{kind, host, where, value, match}]. kind is
    'switch' (host_vars or its name), 'pool' or 'deploy'."""
    from .project import project_host_vars_dir, project_dir, get_project_config
    from .utils import read_project_hosts
    hits = []

    hvroot = project_host_vars_dir(app, owner, project)
    for h in read_project_hosts(app, owner, project):
        host = h['hostname']
        if q.kind == 'text' and q.value in host.lower():
            hits.append({'kind': 'switch', 'host': host, 'where': 'Switch name',
                         'value': host, 'match': 'text'})
        for stem, data in _host_vars(os.path.join(hvroot, host)).items():
            for trail, key, value, parent in _leaves(data):
                m = _match_leaf(q, trail, key, value, parent)
                if m:
                    # vlans.yml holds `vlans:`; say it once.
                    path = trail[1:] if trail and trail[0] == stem else trail
                    where = ' › '.join((stem,) + path + ((key,) if parent is not None else ()))
                    hits.append({'kind': 'switch', 'host': host, 'where': where,
                                 'value': m[1], 'match': m[0]})

    if q.kind in ('ip', 'subnet', 'text'):
        try:
            pools = get_project_config(app, owner, project).get('pools', [])
        except Exception:                           # noqa: BLE001
            pools = []
        for p in pools:
            name, subnet = str(p.get('name') or ''), str(p.get('subnet') or '')
            try:
                net = ipaddress.ip_network(subnet, strict=False)
            except ValueError:
                net = None
            match = None
            if q.kind == 'ip' and net is not None and q.value in net:
                match = 'in pool'
            elif q.kind == 'subnet' and net is not None and net.version == q.value.version \
                    and net.overlaps(q.value):
                match = 'overlaps'
            elif q.kind == 'text' and q.value in name.lower():
                match = 'text'
            if match:
                hits.append({'kind': 'pool', 'host': None,
                             'where': 'Resources › %s pool' % (p.get('type') or 'IP').replace('_', '-'),
                             'value': '%s  %s' % (name, subnet), 'match': match})

        mapping = os.path.join(project_dir(app, owner, project), 'deploy_mapping.yml')
        try:
            with open(mapping) as f:
                deploy = (yaml.safe_load(f) or {}).get('hosts') or []
        except (OSError, yaml.YAMLError, AttributeError):
            deploy = []
        for d in deploy:
            if not isinstance(d, dict):
                continue
            ip, host = str(d.get('mgmt_ip') or ''), str(d.get('hostname') or '')
            m = _match_leaf(q, (), 'mgmt_ip', ip, None) if q.kind != 'text' else (
                ('text', ip) if q.value in ip.lower() else None)
            if m:
                hits.append({'kind': 'deploy', 'host': host, 'where': 'Deployment IPs',
                             'value': m[1], 'match': m[0]})
    return hits


def projects_to_search(app, user, include_all=False, only=None):
    """[(owner, project, level)] the user may search, in a stable order.
    only = (owner, project) narrows it to that one, if they can see it."""
    from . import sharing
    from .models import User
    from .project import list_projects, project_dir
    if only:
        owner, project = only
        level = sharing.level_for(app, user, owner, project)
        if level and os.path.isdir(project_dir(app, owner, project)):
            return [(owner, project, level)]
        return []
    out = [(user.username, p['name'], sharing.OWNER)
           for p in list_projects(app, user.username)]
    seen = {(o, p) for o, p, _ in out}
    for s in sharing.shared_with(app, user):
        key = (s['owner'], s['project'])
        if key not in seen and os.path.isdir(project_dir(app, *key)):
            out.append((s['owner'], s['project'], s['level']))
            seen.add(key)
    if include_all and user.is_admin:
        for u in User.all_users(app):
            for p in list_projects(app, u.username):
                key = (u.username, p['name'])
                if key not in seen:
                    out.append((u.username, p['name'], sharing.VIEW))
                    seen.add(key)
    return out


_MATCH_ORDER = {'exact': 0, 'VLAN': 0, 'VNI': 0, 'in subnet': 1, 'in range': 1,
                'in pool': 2, 'overlaps': 2, 'text': 1}


def run(app, user, text, include_all=False, only=None):
    """{'query', 'results': [{owner, project, level, hits}], 'total',
    'truncated'}."""
    q = Query(text)
    results, total, truncated = [], 0, False
    if q.usable:
        for owner, project, level in projects_to_search(app, user, include_all, only):
            hits = search_project(app, owner, project, q)
            if not hits:
                continue
            hits.sort(key=lambda h: (h['kind'] != 'switch', h['host'] or '',
                                     _MATCH_ORDER.get(h['match'], 3), h['where']))
            room = MAX_HITS - total
            if len(hits) > room:
                hits, truncated = hits[:room], True
            results.append({'owner': owner, 'project': project, 'level': level, 'hits': hits})
            total += len(hits)
            if truncated:
                break
    return {'query': q, 'results': results, 'total': total, 'truncated': truncated}

