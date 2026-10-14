"""
Reachability check for mapped switches, run before a deploy or firmware job.

A dry run to a wrong or unreachable address otherwise sits waiting for SSH
to time out before saying anything. This answers in a few seconds whether
each switch accepts connections on the ports those jobs use:

    22   SSH  — deploy (network_cli) and firmware's non-failsafe step
    443  HTTPS — firmware's REST upload and boot

It is a TCP connect, not a ping: the container has no ping, and ICMP would
need CAP_NET_RAW. Connecting proves the port is open and nothing more — no
credentials are sent.

Addresses come from the project's saved mapping, never from the request,
and only these two ports are tried, so the route cannot be pointed at
arbitrary hosts or ports.
"""

import socket
import time
from concurrent.futures import ThreadPoolExecutor

PORTS = {'ssh': 22, 'https': 443}
CONNECT_TIMEOUT_SECS = 3
MAX_WORKERS = 32
MAX_HOSTS = 256


def _probe(address, port, timeout):
    """Try one TCP connection. Returns {ok, ms, error}."""
    start = time.monotonic()
    try:
        with socket.create_connection((address, port), timeout=timeout):
            pass
    except socket.gaierror:
        return {'ok': False, 'ms': None, 'error': 'name not resolved'}
    except (socket.timeout, TimeoutError):
        return {'ok': False, 'ms': None, 'error': 'timed out'}
    except ConnectionRefusedError:
        return {'ok': False, 'ms': None, 'error': 'refused'}
    except OSError as e:
        return {'ok': False, 'ms': None, 'error': e.strerror or str(e)}
    return {'ok': True, 'ms': round((time.monotonic() - start) * 1000), 'error': None}


def check(hosts, timeout=CONNECT_TIMEOUT_SECS):
    """Probe every port on every host in parallel.

    hosts: [{'hostname', 'mgmt_ip'}]. Hosts without an address are
    reported as such rather than probed. Results keep the input order.
    """
    hosts = list(hosts)[:MAX_HOSTS]
    jobs = [(i, name, port) for i, h in enumerate(hosts) if h.get('mgmt_ip')
            for name, port in PORTS.items()]
    results = [{'hostname': h['hostname'], 'mgmt_ip': h.get('mgmt_ip') or '',
                **{name: None for name in PORTS}} for h in hosts]
    if jobs:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(jobs))) as pool:
            futures = {pool.submit(_probe, hosts[i]['mgmt_ip'], port, timeout):
                       (i, name) for i, name, port in jobs}
            for fut, (i, name) in futures.items():
                results[i][name] = fut.result()
    return results
