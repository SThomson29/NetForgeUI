"""
What each switch is running, read over the AOS-CX REST API, and how that
compares with the image chosen on the Firmware page.

One short session per switch: discover the API version (GET /rest needs no
login), log in, read /firmware (and the platform name), log out. AOS-CX
allows only a few REST sessions per user, so logout always runs.

The certificate isn't verified: switches serve self-signed ones, the same
trust the deploy and firmware playbooks already extend. Credentials are
used for the call and not kept.
"""

import concurrent.futures
import warnings

from .firmware_images import parse_version

TIMEOUT = (5, 15)          # connect, read
MAX_PARALLEL = 16
FALLBACK_PREFIX = '/rest/v10.04'


class SwitchError(Exception):
    """A switch that couldn't be read, with a short reason."""


def _base(ip):
    return 'https://[%s]' % ip if ':' in ip else 'https://%s' % ip


def _prefix(session, base):
    """The newest REST API the switch offers, e.g. /rest/v10.13."""
    try:
        res = session.get(base + '/rest', timeout=TIMEOUT)
        data = res.json() if res.ok else {}
    except ValueError:
        data = {}
    latest = data.get('latest') if isinstance(data, dict) else None
    if isinstance(latest, dict) and str(latest.get('prefix', '')).startswith('/rest/'):
        return latest['prefix']
    versions = sorted(k for k in (data or {}) if str(k).startswith('v10.'))
    return '/rest/' + versions[-1] if versions else FALLBACK_PREFIX


def read_versions(ip, username, password):
    """{current, primary, secondary, booted, platform_name} for one switch.
    Raises SwitchError."""
    import requests
    import urllib3
    base = _base(ip)
    session = requests.Session()
    session.verify = False
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', urllib3.exceptions.InsecureRequestWarning)
        try:
            prefix = _prefix(session, base)
            res = session.post(base + prefix + '/login', timeout=TIMEOUT,
                               data={'username': username, 'password': password})
            if res.status_code in (401, 403):
                raise SwitchError('the switch refused the username or password')
            if not res.ok:
                raise SwitchError('login failed (HTTP %d)' % res.status_code)
            try:
                fw = session.get(base + prefix + '/firmware', timeout=TIMEOUT)
                if not fw.ok:
                    raise SwitchError('could not read firmware details (HTTP %d)' % fw.status_code)
                fw = fw.json()
                platform_name = None
                try:
                    sysinfo = session.get(base + prefix + '/system', timeout=TIMEOUT,
                                          params={'attributes': 'platform_name'})
                    if sysinfo.ok:
                        platform_name = sysinfo.json().get('platform_name')
                except (requests.RequestException, ValueError):
                    pass
            finally:
                try:
                    session.post(base + prefix + '/logout', timeout=TIMEOUT)
                except requests.RequestException:
                    pass
        except requests.exceptions.SSLError:
            raise SwitchError('TLS handshake failed')
        except requests.exceptions.Timeout:
            raise SwitchError('timed out')
        except requests.exceptions.ConnectionError:
            raise SwitchError("can't connect on port 443; is the REST API (https-server) enabled?")
        except ValueError:
            raise SwitchError('unexpected response from the switch')
        finally:
            session.close()
    if not isinstance(fw, dict):
        raise SwitchError('unexpected response from the switch')
    return {
        'current':   fw.get('current_version'),
        'primary':   fw.get('primary_version'),
        'secondary': fw.get('secondary_version'),
        'booted':    fw.get('booted_image'),
        'platform_name': platform_name,
    }


def assess(sw, image):
    """How a switch stands against the chosen image.

    sw: read_versions() output; image: {platform, version} (either may be
    None). Returns {verdict, tick, note, other_partition} where verdict is
    'current', 'staged', 'wrong_platform', 'upgrade' or 'unknown'.
    """
    booted = sw.get('booted')
    other = {'primary': 'secondary', 'secondary': 'primary'}.get(booted)
    plat, running = parse_version(sw.get('current'))
    img_plat, img_ver = (image or {}).get('platform'), (image or {}).get('version')
    out = {'other_partition': other}
    if not img_ver:
        return dict(out, verdict='unknown', tick=None,
                    note="the image's version isn't known, so it can't be compared")
    if img_plat and plat and img_plat != plat:
        return dict(out, verdict='wrong_platform', tick=False,
                    note='image is for %s switches; this one runs %s' % (img_plat, plat))
    if running == img_ver:
        return dict(out, verdict='current', tick=False, note='already running %s' % img_ver)
    staged = sw.get(other) if other else None
    if parse_version(staged)[1] == img_ver:
        return dict(out, verdict='staged', tick=True,
                    note='%s is already on %s but not booted' % (img_ver, other))
    return dict(out, verdict='upgrade', tick=True,
                note='%s → %s' % (running or sw.get('current') or '?', img_ver))


def check(hosts, username, password, image):
    """[{hostname, mgmt_ip, ok, error?, versions?, ...assess()}] for each
    {hostname, mgmt_ip}, read in parallel."""
    def one(h):
        row = {'hostname': h['hostname'], 'mgmt_ip': h['mgmt_ip']}
        try:
            versions = read_versions(h['mgmt_ip'], username, password)
        except SwitchError as e:
            return dict(row, ok=False, error=str(e))
        except Exception as e:                      # noqa: BLE001
            return dict(row, ok=False, error='failed (%s)' % e.__class__.__name__)
        return dict(row, ok=True, versions=versions, **assess(versions, image))

    if not hosts:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(hosts))) as ex:
        return list(ex.map(one, hosts))
