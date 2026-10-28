"""
Cabling check: the links the design implies, against what LLDP sees.

Nothing new is entered for this. Expected links are only those the design
already pins down, strongest first:

  1. point-to-point     two routed ports on different switches sharing a
                        /31 or /30 — both ends known exactly
  2. description+port   'CORE-01 1/1/49' in a port's description
  3. VSX ISL            ports in a VSX pair's ISL LAG see the other member
  4. description        a project switch named in a port's description
  5. LAG description    a project switch named in a LAG's description,
                        for each of its member ports

A port takes the strongest one; anything else is only shown as seen.

A neighbour is recognised as a project switch by its LLDP system name
(once its config is on) or, before that, by its chassis MAC matching a
switch's base MAC from an inventory read.
"""

import ipaddress
import re

from .switch_read import norm_mac

_PORT_IN_TEXT = re.compile(r'(?<![\d/])(\d+/\d+/\d+)(?![\d/])')


def _routed(phy):
    return phy.get('port_type') == 'routed' or phy.get('routed') == 'true'


def _iface(phy):
    ip, prefix = str(phy.get('ip_address') or ''), str(phy.get('ip_prefix') or '')
    if not ip:
        return None
    try:
        return ipaddress.ip_interface(ip if '/' in ip else '%s/%s' % (ip, prefix or 32))
    except ValueError:
        return None


def _named_hosts(text, hosts_lower, me):
    """[(host, end of the name in text)] for project switches named in
    text as whole names, other than me."""
    t = str(text or '').lower()
    found = []
    for low, host in hosts_lower.items():
        if host == me:
            continue
        m = re.search(r'(?<![a-z0-9_-])%s(?![a-z0-9_-])' % re.escape(low), t)
        if m:
            found.append((host, m.end()))
    return found


def expectations(states, vsx_pairs):
    """{(host, port): {peer, peer_port, source}} from the design."""
    hosts_lower = {h.lower(): h for h in states}
    out = {}

    def expect(host, port, peer, peer_port, source):
        out.setdefault((host, port), {'peer': peer, 'peer_port': peer_port, 'source': source})

    # 1. Point-to-point subnets with exactly two ends on different switches.
    ends = {}
    for host, s in states.items():
        for phy in s.get('physical', []):
            if not _routed(phy) or phy.get('lag_member'):
                continue
            iface = _iface(phy)
            if iface and iface.network.prefixlen in (30, 31, 126, 127):
                ends.setdefault(iface.network, []).append((host, phy['name']))
    for net, pair in ends.items():
        if len(pair) == 2 and pair[0][0] != pair[1][0]:
            (a, pa), (b, pb) = pair
            expect(a, pa, b, pb, 'point-to-point %s' % net)
            expect(b, pb, a, pa, 'point-to-point %s' % net)

    # 2 and 4. Descriptions naming one project switch, with or without a port.
    later = []
    for host, s in states.items():
        for phy in s.get('physical', []):
            named = _named_hosts(phy.get('description'), hosts_lower, host)
            if len(named) != 1:
                continue
            peer, end = named[0]
            # A port after the name is the far end's ('to CORE-01 1/1/49');
            # one before it is usually this end's own, so it's ignored.
            port = _PORT_IN_TEXT.search(phy['description'][end:])
            if port:
                expect(host, phy['name'], peer, port.group(1), 'description')
            else:
                later.append((host, phy['name'], peer, None, 'description'))

    # 3. VSX ISL members see the other member of the pair.
    for a, b in vsx_pairs:
        for me, peer in ((a, b), (b, a)):
            isl = str(states[me].get('vsxIsl') or '').lower().removeprefix('lag')
            for phy in states[me].get('physical', []):
                if isl and phy.get('lag_member') == isl:
                    expect(me, phy['name'], peer, None, 'VSX ISL')

    for args in later:
        expect(*args)

    # 5. A LAG's description applies to its member ports.
    for host, s in states.items():
        for lag in s.get('lags', []):
            named = _named_hosts(lag.get('description'), hosts_lower, host)
            if len(named) != 1:
                continue
            num = str(lag.get('name') or '').lower().removeprefix('lag')
            for phy in s.get('physical', []):
                if num and phy.get('lag_member') == num:
                    expect(host, phy['name'], named[0][0], None, 'LAG %s description' % lag['name'])
    return out


def _identify(n, by_name, by_mac):
    name = n['sys_name'].split('.')[0].lower()
    return by_name.get(name) or by_mac.get(norm_mac(n['chassis_id']))


def compare(states, expected, reads, known_macs):
    """Rows for the Verify page: {host, port, peer, peer_port, source,
    result, seen}. result is 'ok', 'wrong_port', 'wrong_switch',
    'missing', 'not_read' or 'not_in_design'.

    known_macs: {base MAC: host} from inventory, current or stored.
    """
    by_name = {h.lower(): h for h in states}
    by_mac = {norm_mac(m): h for m, h in known_macs.items() if norm_mac(m)}
    rows = []
    for (host, port), e in sorted(expected.items()):
        r = reads.get(host)
        row = dict(e, host=host, port=port, seen='')
        if not r or not r.get('ok'):
            rows.append(dict(row, result='not_read'))
            continue
        seen = r['neighbors'].get(port, [])
        if not seen:
            rows.append(dict(row, result='missing'))
            continue
        who = [(n, _identify(n, by_name, by_mac)) for n in seen]
        mine = [(n, h) for n, h in who if h == e['peer']]
        if mine:
            n = mine[0][0]
            ok = not e['peer_port'] or n['port_id'] == e['peer_port']
            rows.append(dict(row, result='ok' if ok else 'wrong_port',
                             seen='%s %s' % (e['peer'], n['port_id'] or '?')))
        else:
            n, h = who[0]
            rows.append(dict(row, result='wrong_switch',
                             seen='%s %s' % (h or n['sys_name'] or n['chassis_id'] or 'unknown',
                                             n['port_id'] or '')))

    for host, r in sorted(reads.items()):
        if not r.get('ok'):
            continue
        for port, seen in sorted(r['neighbors'].items()):
            if (host, port) in expected:
                continue
            for n in seen:
                h = _identify(n, by_name, by_mac)
                if h and h != host:
                    rows.append({'host': host, 'port': port, 'peer': h, 'peer_port': None,
                                 'source': '', 'result': 'not_in_design',
                                 'seen': '%s %s' % (h, n['port_id'] or '')})
    return rows
